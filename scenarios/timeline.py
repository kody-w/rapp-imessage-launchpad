"""Python 3.9+ stdlib-only failure timelines.

Read-only and bounded; no service, network, or sender imports.
"""

import ast
import hashlib
import json
import os
import plistlib
import re
import stat
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from xml.parsers.expat import ExpatError


MAX_BYTES = 1024 * 1024
MAX_ROWS = 6000
MAX_FILES_PER_ROLE = 8
FRESHNESS = timedelta(hours=6)
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
_ID = re.compile(r"[A-Za-z0-9_./:@+-]{1,160}\Z")
_LOG = re.compile(r"^\[([^\]]+)\]\s+status=(\w+)\s+failing=(.+)$")
_FIXED = re.compile(r"^\[([^\]]+)\]\s+verified fixed:\s*(.+)$")
_REVISION = re.compile(
    r"running ([0-9a-f]{7,40}), (\d+) commit\(s\) behind origin/main ([0-9a-f]{7,40})"
)


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("expected an offset-aware ISO8601 timestamp")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _text(value, limit=400):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _ids(value):
    if not isinstance(value, list) or len(value) > 512:
        raise ValueError("expected a bounded list of check identifiers")
    if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in value):
        raise ValueError("invalid check identifier")
    return sorted(set(value))


def _within(path, root):
    return path == root or root in path.parents


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "timeline:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Evidence:
    def __init__(self, home, now):
        self.home = home
        self.now = now
        self.events = []
        self.schedules = []
        self.inventory = []
        self.issues = {}

    def issue(self, source, code, observation):
        key = (source, code)
        if key not in self.issues:
            self.issues[key] = {
                "source": source, "code": code, "observation": observation, "count": 0,
            }
        self.issues[key]["count"] += 1

    def time(self, row, source):
        values = [row[k] for k in ("at", "utc", "generated", "timestamp") if k in row]
        try:
            if not values:
                raise ValueError("timestamp is missing")
            times = {_instant(value) for value in values}
            if len(times) != 1:
                raise ValueError("timestamp fields disagree")
            at = times.pop()
            if at > self.now:
                self.issue(source, "future_timestamp", "Future-dated evidence excluded.")
                return None
            return at
        except (ValueError, TypeError, OverflowError):
            self.issue(
                source, "invalid_timestamp",
                "Missing, invalid, conflicting, or timezone-free timestamp; not ordered by mtime.",
            )
            return None

    def event(self, row, source, role, kind, **fields):
        at = self.time(row, source)
        if at is None:
            return
        device = row.get("device", "local")
        if not isinstance(device, str) or not _ID.fullmatch(device):
            raise ValueError("invalid device identifier")
        if role == "device_receipts" and device == "local":
            raise ValueError("other-device receipt must explicitly identify its device")
        if role != "device_receipts" and device != "local":
            raise ValueError("other devices require explicit device_receipts")
        self.events.append({
            "at": _stamp(at), "device": device, "kind": kind,
            "source": source, "role": role, **fields,
        })

    def snapshot(self, row, source, role, channel):
        checks = row.get("checks")
        details, passed, derived, critical = {}, [], [], []
        if checks is not None:
            if not isinstance(checks, list) or len(checks) > 512:
                raise ValueError("invalid checks array")
            seen = set()
            for check in checks:
                if not isinstance(check, dict) or type(check.get("ok")) is not bool:
                    raise ValueError("check must have a boolean ok field")
                name = _ids([check.get("id")])[0]
                if name in seen:
                    raise ValueError("duplicate check identifier")
                seen.add(name)
                (passed if check["ok"] else derived).append(name)
                if not check["ok"] and check.get("severity") == "critical":
                    critical.append(name)
                details[name] = _text(check.get("detail"))
        failed = _ids(row.get("failed", derived if checks is not None else None))
        if checks is not None and failed != sorted(derived):
            raise ValueError("failed list disagrees with check results")
        if row.get("status") == "healthy" and failed:
            raise ValueError("healthy status contradicts failed list")
        critical = _ids(row.get("critical", critical))
        if not set(critical).issubset(failed):
            raise ValueError("critical list is not a subset of failures")
        before = len(self.events)
        self.event(
            row, source, role, "snapshot", channel=channel, failed=failed,
            critical=critical, critical_known="critical" in row or checks is not None,
            passed=sorted(passed), details=details,
        )
        if len(self.events) == before:
            return
        detail = details.get("w_sentinel_current", "")
        match = _REVISION.search(detail)
        if match:
            self.event(
                row, source, role, "deployment_state", checks=["w_sentinel_current"],
                running_revision=match[1], published_revision=match[3],
                commits_behind=int(match[2]),
            )

    def record(self, row, source, role):
        if not isinstance(row, dict):
            raise ValueError("record is not an object")
        kind = row.get("kind", "")
        if kind == "sentinel.tick":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("invalid sentinel.tick payload")
            self.snapshot({**payload, "utc": row.get("utc"),
                           "device": row.get("device", "local")}, source, role, "chain")
            return
        if kind == "snapshot" or "failed" in row or role in ("verdict", "last_run"):
            channel = "receipt" if role == "device_receipts" else role
            self.snapshot(row, source, role, channel)
            return
        data = row.get("payload", row)
        if not isinstance(data, dict):
            raise ValueError("event payload is not an object")
        if kind == "repair.verified":
            cleared = _ids(data.get("cleared", []))
            remaining = _ids(data.get("still_failing", []))
            if set(cleared) & set(remaining) or (data.get("landed") is False and cleared):
                raise ValueError("contradictory repair probe")
            checks = sorted(set(cleared + remaining + self.check_keys(data)))
            if not checks:
                raise ValueError("repair probe has no check association")
            self.event(row, source, role, "repair_probe", checks=checks,
                       cleared=cleared, still_failing=remaining)
            return
        mode = data.get("act", data.get("mode"))
        if kind == "repair.attempt":
            mode = "repair"
        if kind == "neighbor.acted" and mode not in ("diagnose", "repair"):
            return
        if mode in ("diagnose", "repair") or data.get("skipped") is True:
            checks = self.check_keys(data)
            if not checks:
                raise ValueError("attempt has no check association")
            result = _text(data.get("result"))
            event_kind = "repair_attempt" if mode == "repair" else "diagnosis"
            if data.get("skipped") is True:
                event_kind = "diagnosis_skipped"
            self.event(
                row, source, role, event_kind, checks=checks,
                outcome="unknown" if not result or result.startswith("UNKNOWN") else "claim_only",
            )
            return
        if kind == "deployment" or role == "deployments":
            checks = self.check_keys(data)
            revision = data.get("revision")
            previous = data.get("previous_revision")
            if not checks or not isinstance(revision, str) or not _ID.fullmatch(revision):
                raise ValueError("deployment requires revision and explicit check association")
            if previous is not None and (not isinstance(previous, str)
                                         or not _ID.fullmatch(previous)):
                raise ValueError("invalid previous revision")
            self.event(row, source, role, "deployment", checks=checks,
                       revision=revision, previous_revision=previous)
            return
        if role == "repair_receipts" and row.get("spec") == "rapp/1" and isinstance(kind, str) and kind:
            return
        raise ValueError("unsupported evidence record")

    @staticmethod
    def check_keys(data):
        if "checks" in data:
            return _ids(data["checks"])
        value = data.get("issue", data.get("key", ""))
        if not isinstance(value, str):
            raise ValueError("invalid issue key")
        return _ids([part.strip() for part in value.split(",") if part.strip()])

    def log(self, line, source):
        match = _LOG.match(line)
        fixed = _FIXED.match(line)
        if match:
            failed = [] if match[3] == "none" else ast.literal_eval(match[3])
            self.snapshot({"at": match[1], "status": match[2], "failed": failed},
                          source, "logs", "logs")
        elif fixed:
            self.record({"at": fixed[1], "kind": "repair.verified",
                         "cleared": ast.literal_eval(fixed[2])}, source, "logs")
        elif "status=" in line and "failing=" in line:
            raise ValueError("unparseable failure snapshot")
        elif line.strip() and not line.startswith("["):
            self.issue(
                source, "undated_log", "Untimestamped log text cannot locate an event or prove a cause.",
            )

    def schedule(self, raw, source):
        row = plistlib.loads(raw)
        if not isinstance(row, dict):
            raise ValueError("LaunchAgent is not a dictionary")
        env = row.get("EnvironmentVariables", {})
        home = env.get("SENTINEL_HOME") if isinstance(env, dict) else None
        if not isinstance(home, str) or Path(home).expanduser().resolve() != self.home:
            self.issue(
                source, "unassociated_schedule",
                "LaunchAgent does not explicitly identify this SENTINEL_HOME; not correlated.",
            )
            return
        fields = {}
        interval = row.get("StartInterval")
        if interval is not None:
            if type(interval) is not int or interval <= 0:
                raise ValueError("invalid StartInterval")
            fields["StartInterval"] = interval
        calendar = row.get("StartCalendarInterval")
        if calendar is not None:
            calendar = [calendar] if isinstance(calendar, dict) else calendar
            if not isinstance(calendar, list) or not calendar or len(calendar) > 64:
                raise ValueError("invalid calendar schedule")
            ranges = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31),
                      "Month": (1, 12), "Weekday": (0, 7)}
            for entry in calendar:
                if not isinstance(entry, dict) or not entry:
                    raise ValueError("invalid calendar entry")
                for key, value in entry.items():
                    if (key not in ranges or type(value) is not int
                            or not ranges[key][0] <= value <= ranges[key][1]):
                        raise ValueError("invalid calendar field")
            fields["StartCalendarInterval"] = calendar
        for key in ("RunAtLoad", "Disabled"):
            if key in row:
                if type(row[key]) is not bool:
                    raise ValueError("invalid LaunchAgent boolean")
                fields[key] = row[key]
        self.schedules.append({
            "source": source, "label": _text(row.get("Label"), 160),
            "configuration": fields,
            "limit": "Configuration only: does not prove the job is loaded, fired, or succeeded.",
        })

    def read(self, value, role):
        if not isinstance(value, str) or not value:
            self.issue(role, "invalid_path", "Source path must be a nonempty string.")
            return
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.home / path
        try:
            path = path.resolve()
            source = str(path)
            permitted = _within(path, self.home)
            if role == "launch_agents":
                permitted |= _within(path, LAUNCH_AGENTS.resolve()) and path.suffix == ".plist"
            if not permitted:
                self.issue(source, "outside_scope", "Path or symlink escapes the authorized local roots.")
                return
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                self.issue(source, "not_regular", "Not a regular evidence file; not opened.")
                return
            if path.suffix == ".log" and role != "logs":
                raise ValueError("native logs must be selected through the logs role")
            lines = path.suffix in (".jsonl", ".log")
            if info.st_size > MAX_BYTES and not lines:
                self.issue(source, "size_limit", "Oversize structured file skipped, not partially decoded.")
                return
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("source changed to a non-regular file")
                offset = max(0, info.st_size - MAX_BYTES) if lines else 0
                handle.seek(offset)
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                self.issue(source, "size_limit", "Source grew beyond the read bound; retry a later snapshot.")
                return
            if offset:
                raw = raw.split(b"\n", 1)[-1] if b"\n" in raw else b""
                self.issue(source, "truncated", "Only the bounded file tail was read; earlier history is unknown.")
            self.inventory.append({"source": source, "role": role, "bytes_read": len(raw)})
            if role == "launch_agents":
                self.schedule(raw, source)
                return
            if lines:
                rows = raw.splitlines()
                if len(rows) > MAX_ROWS:
                    rows = rows[-MAX_ROWS:]
                    self.issue(source, "row_limit", "Only the bounded final rows were retained.")
                for raw_line in rows:
                    try:
                        line = raw_line.decode("utf-8")
                        if not line.strip():
                            continue
                        if path.suffix == ".log":
                            self.log(line, source)
                        else:
                            self.record(json.loads(line), source, role)
                    except (ValueError, TypeError, SyntaxError, RecursionError):
                        self.issue(source, "corrupt_row", "Malformed or unsupported evidence row skipped.")
            else:
                parsed = json.loads(raw.decode("utf-8"))
                rows = parsed if isinstance(parsed, list) else [parsed]
                if len(rows) > MAX_ROWS:
                    rows = rows[-MAX_ROWS:]
                    self.issue(source, "row_limit", "Only the bounded final records were retained.")
                for row in rows:
                    try:
                        self.record(row, source, role)
                    except (ValueError, TypeError, RecursionError):
                        self.issue(source, "corrupt_row", "Malformed or unsupported evidence record skipped.")
        except FileNotFoundError:
            self.issue(str(path), "missing", "Evidence file is missing; no absence or recovery inferred.")
        except (OSError, ValueError, TypeError, OverflowError, RuntimeError,
                plistlib.InvalidFileException, ExpatError):
            self.issue(str(path), "unreadable", "Evidence is unreadable or corrupt; it was not treated as healthy.")

    def load(self, config):
        previous_month = self.now.replace(day=1) - timedelta(days=1)
        defaults = {
            "logs": [f"logs/sentinel-{self.now:%Y-%m}.log",
                     f"logs/sentinel-{previous_month:%Y-%m}.log", "logs/launchd.err.log"],
            "verdict": "state/last_verdict.json",
            "last_run": "state/last_run.json",
            "repair_receipts": ["state/escalations.json", "neighborhood/copilot/chain.jsonl"],
            "deployments": ["state/deployments.jsonl"],
            "launch_agents": [str(LAUNCH_AGENTS / "com.rapp.storykeeper.plist")],
            "device_receipts": [],
        }
        for role, default in defaults.items():
            values = config.get(role, default)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                self.issue(role, "invalid_sources", "Source selection must be a path or a list of paths.")
                continue
            if len(values) > MAX_FILES_PER_ROLE:
                self.issue(role, "file_limit", "Source count exceeds the per-role bound; remaining files not read.")
            for value in values[:MAX_FILES_PER_ROLE]:
                self.read(value, role)
        self.events.sort(key=lambda event: (
            _instant(event["at"]), event["device"], event["kind"], event["source"],
        ))


def _recurrences(evidence):
    candidates, conflicts = [], []
    snapshots = [event for event in evidence.events if event["kind"] == "snapshot"]
    for device in sorted({event["device"] for event in snapshots}):
        local = [event for event in snapshots if event["device"] == device]
        channels = ("logs", "chain", "receipt")
        available = {name: len({event["at"] for event in local if event["channel"] == name})
                     for name in channels}
        channel = next((name for name in channels if available[name] >= 2), None)
        if channel is None:
            channel = next((name for name in channels if available[name]), None)
        primary = [event for event in local if event["channel"] == channel] if channel else []
        supplements = [event for event in local if event["channel"] in ("verdict", "last_run")]
        names = sorted({name for event in primary + supplements for name in event["failed"]})
        for check in names:
            observations = defaultdict(list)
            for event in primary + supplements:
                observations[event["at"]].append({
                    "failed": check in event["failed"], "countable": event["channel"] == channel,
                    "source": event["source"], "critical": check in event["critical"],
                    "critical_known": event["critical_known"],
                    "explicit_pass": check in event["passed"],
                })
            for event in evidence.events:
                if event["kind"] != "repair_probe" or event["device"] != device:
                    continue
                if check in event["cleared"] or check in event["still_failing"]:
                    observations[event["at"]].append({
                        "failed": check in event["still_failing"], "countable": False,
                        "source": event["source"], "critical": False, "critical_known": False,
                        "explicit_pass": check in event["cleared"],
                    })
            episodes, current, previous_clear, total = [], None, None, 0
            for at in sorted(observations, key=_instant):
                batch = observations[at]
                if len({item["failed"] for item in batch}) != 1:
                    conflicts.append({"device": device, "check": check, "at": at})
                    evidence.issue(
                        ", ".join(sorted({item["source"] for item in batch})),
                        "conflicting_samples", "Same-instant samples disagree; no order or recovery imposed.",
                    )
                    continue
                failed = batch[0]["failed"]
                if failed:
                    if current is None:
                        current = {
                            "device": device, "check": check, "first_failure": at,
                            "first_source": batch[0]["source"], "previous_clear": previous_clear,
                            "sample_count": 0, "active": True, "critical": False,
                        }
                        episodes.append(current)
                    countable = any(item["countable"] for item in batch)
                    total += int(countable)
                    current["sample_count"] += int(countable)
                    current.update(
                        last_failure=at, last_source=batch[0]["source"],
                    )
                    if any(item["critical_known"] for item in batch):
                        current["critical"] = any(item["critical"] for item in batch)
                else:
                    if current is not None:
                        current.update(
                            active=False, cleared_at=at, clearance_source=batch[0]["source"],
                            clearance="reported_probe_pass" if any(
                                item["explicit_pass"] for item in batch) else "not_listed_as_failing",
                        )
                        current = None
                    previous_clear = at
            if total >= 2 and episodes:
                candidate = dict(episodes[-1])
                candidate.update(total_samples=total, episodes=len(episodes), primary_channel=channel)
                candidates.append(candidate)
    candidates.sort(key=lambda item: (
        -_instant(item["last_failure"]).timestamp(),
        -_instant(item["first_failure"]).timestamp(), item["device"], item["check"],
    ))
    return candidates, conflicts


def _correlate(evidence, selected):
    related, hypotheses, recovery = [], [], []
    for event in evidence.events:
        if event["kind"] == "snapshot":
            continue
        same_device = event["device"] == selected["device"]
        associated = same_device and selected["check"] in event.get("checks", [])
        if not associated and event["kind"] != "deployment_state":
            continue
        entry = dict(event, associated_with_selected_check=associated)
        if event["kind"] == "deployment":
            when, first = _instant(event["at"]), _instant(selected["first_failure"])
            relation = "before" if when < first else "after" if when > first else "same_instant"
            entry["relation_to_first_failure"] = relation
            if relation == "before":
                hypotheses.append({
                    "hypothesis": "The recorded deployment is a candidate to investigate, not a proven cause.",
                    "source": event["source"], "revision": event["revision"],
                    "limit": "Temporal precedence and a shared check identifier do not establish causality.",
                })
            else:
                entry["limit"] = (
                    "This recorded deployment does not precede the earliest retained symptom; "
                    "it cannot explain that earlier observation by ordering alone."
                )
        elif event["kind"] == "deployment_state":
            entry["limit"] = (
                f"Recorded running revision {event['running_revision']} versus published "
                f"{event['published_revision']}; state observed at this timestamp, "
                "not the time a deployment occurred."
            )
            if not associated:
                entry["limit"] += " No check-specific link to the selected failure is established."
        elif event["kind"] == "repair_probe":
            entry["limit"] = "Recorded post-repair probe outcome, not proof that the repair caused it."
            prior_attempts = [other for other in evidence.events
                              if other["kind"] == "repair_attempt"
                              and other["device"] == selected["device"]
                              and selected["check"] in other["checks"]
                              and _instant(other["at"]) < _instant(event["at"])]
            if prior_attempts:
                entry["preceding_attempt_at"] = prior_attempts[-1]["at"]
            else:
                entry["limit"] += (
                    " No earlier matching repair attempt is retained; "
                    "a later action cannot cause this earlier probe outcome."
                )
            recovery.append({
                "at": event["at"], "source": event["source"],
                "outcome": "reported_clearance" if selected["check"] in event["cleared"]
                else "still_failing" if selected["check"] in event["still_failing"] else "unmeasured",
            })
        else:
            entry["limit"] = "Attempt or diagnosis receipt only; free-text cause/fix claims are not proof."
        related.append(entry)
    if not hypotheses:
        hypotheses.append({
            "hypothesis": "Cause remains undetermined from the retained local evidence.",
            "limit": "No causal attribution, rollback safety, or cross-device dependency has been established.",
        })
    return related, hypotheses, recovery


def _schedule_context(evidence, selected):
    if selected["device"] != "local":
        return []
    times = sorted({
        _instant(event["at"]) for event in evidence.events
        if event["kind"] == "snapshot" and event["device"] == "local"
        and event["channel"] == selected["primary_channel"]
        and selected["check"] in event["failed"]
        and _instant(event["at"]) >= _instant(selected["first_failure"])
    })
    gaps = [(right - left).total_seconds() for left, right in zip(times, times[1:])]
    observed = median(gaps) if gaps else None
    context = []
    for schedule in evidence.schedules:
        config = schedule["configuration"]
        configured = config.get("StartInterval")
        calendar = config.get("StartCalendarInterval", [])
        if configured is None and calendar and all(set(row) == {"Minute"} for row in calendar):
            minutes = sorted({row["Minute"] for row in calendar})
            steps = [right - left for left, right in zip(minutes, minutes[1:] + [minutes[0] + 60])]
            if len(set(steps)) == 1:
                configured = steps[0] * 60
        context.append({
            "source": schedule["source"], "label": schedule["label"],
            "configured_interval_seconds": configured,
            "median_recorded_failure_gap_seconds": observed,
            "limit": (
                "Cadence comparison describes the local reporter's saved schedule, not the failing "
                "target's execution. Similar timing is correlation, not evidence of a cause or a deadline."
            ),
        })
    return context


def _base(reason):
    return {
        "scenario": "timeline", "status": "blocked",
        "title": "Failure timeline needs evidence",
        "change": "A recurring failure has not been established.",
        "impact": "Missing evidence is not evidence that the system recovered.",
        "action": "Review the saved local evidence; do not restart, deploy, or repeat a repair automatically.",
        "decision": "hold_for_evidence", "evidence": [], "artifacts": [],
        "fingerprint": "", "urgency": "routine", "reason": reason,
    }


def _compact_ready(result, report, selected):
    if result["status"] != "ready":
        return result
    decision = "No repair approval requested; cause remains unproven."
    plan = {
        "prepared": "Ordered failure timeline, prior-clear boundary when retained, and saved repair outcomes.",
        "earliest_actionable_event": {
            "at": selected["first_failure"], "source": selected["first_source"],
            "check": selected["check"],
            "limit": "First retained failure in the latest observed episode, not a proven outage onset.",
        },
        "prior_not_failing_at": selected["previous_clear"],
        "safest_recovery_option": (
            "Keep services unchanged: the retained evidence does not establish a safe restart, "
            "rollback, redeployment, or repair replay."
        ),
        "agent_next_step": (
            f"Read the next saved result for {selected['check']} from the same authorized sources "
            f"and compare it with the retained failure at {selected['last_failure']}. "
            "Do not run a service probe or contact another device."
        ),
        "prevention": "Require a saved recovery observation; do not treat diagnosis claims or liveness as a fix.",
        "repair_approval_requested": False,
    }
    report["recovery_plan"] = plan
    report["detailed_proposal"] = dict(
        result, decision=decision,
        action="I prepared the failure timeline and recovery plan. "
        + plan["safest_recovery_option"] + " " + plan["agent_next_step"],
    )
    result = dict(result)
    source_ids = {}

    def short(value, limit):
        return value if len(value) <= limit else value[:limit - 3] + "..."

    def source_id(path):
        if path not in source_ids:
            name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(path).name)
            source_ids[path] = f"s{len(source_ids) + 1}:" + short(name, 20)
        return source_ids[path]

    names = {
        "w_neighbor_moving": "Work-progress check",
        "rv_world_merging": "Merge-progress check",
        "rv_meaningful_activity": "Activity check",
        "rv_validation": "Validation check",
        "w_openrappter_spin": "Job-stability check",
        "w_sentinel_current": "Deployed-revision check",
        "alert_delivery": "Delivery-evidence check",
    }
    check = names.get(selected["check"], short(selected["check"], 28))
    if selected["device"] != "local":
        check = short(selected["device"], 16) + "/" + check
    reason = "Evidence saved."
    if report["evidence_gaps"]:
        reason = f"Evidence gaps: {len(report['evidence_gaps'])}."
    if len(report["equally_recent_checks"]) > 1:
        reason += " Ties are not causal priority."
    if not selected["previous_clear"]:
        reason = reason.rstrip(".") + "; actual onset is unknown."
    scope = ("Local evidence only." if not report["other_device_receipts"]
             else "Receipt clocks/claims unverified.")
    result.update(
        title="Machine timeline",
        change=f"{check}: repeated failures.",
        impact="Downstream impact unproven.",
        action="I prepared the timeline and recovery plan. Safest next: compare the next saved check read-only; no restart.",
        decision=decision,
        reason=reason,
        evidence=[
            {"source": source_id(selected["first_source"]),
             "observation": f"First retained failure: {selected['first_failure']}."},
            {"source": source_id(selected["last_source"]),
             "observation": f"Sampled at {selected['last_failure']}. {scope}"},
        ],
    )
    report["notification_sources"] = {label: path for path, label in source_ids.items()}
    return result


def _persist(result, report, artifact_dir):
    try:
        folder = Path(artifact_dir).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = [folder / "timeline-evidence.json", folder / "timeline.md"]
        if any(path.is_symlink() for path in paths):
            raise ValueError("artifact path is a symlink")
        inputs = {item["source"] for item in report.get("sources", [])}
        if any(str(path) in inputs for path in paths):
            raise ValueError("artifact path overlaps an input")
        result["artifacts"] = [str(path) for path in paths]
        report["envelope"] = result
        explanation = report.get("detailed_proposal", result)
        text = "# Make machines explain themselves\n\n" + "\n\n".join(
            f"**{key.title()}:** {explanation[key]}"
            for key in ("status", "title", "change", "impact", "action", "decision", "reason")
        )
        text += "\n\n## Evidence\n\n" + "\n".join(
            f"- `{item['source']}`: {item['observation']}" for item in explanation["evidence"]
        )
        text += "\n\nFull ordered observations, hypotheses, and limitations: `timeline-evidence.json`.\n"
        for path, content in zip(paths, [
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", text,
        ]):
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
    except (OSError, ValueError, TypeError, RuntimeError):
        result.update(
            status="blocked", decision="hold_for_evidence", artifacts=[], urgency="routine",
            reason="Timeline evidence could not be persisted to artifact_dir; no notification is ready.",
        )
        result["fingerprint"] = _digest({"assessment": result["fingerprint"], "artifact_write": "failed"})
    return result


def build(context: dict) -> dict:
    """Return a JSON-safe advisory envelope and persist evidence only in artifact_dir."""
    result = _base("No usable recurring-failure history was supplied.")
    try:
        if not isinstance(context, dict):
            raise ValueError("context must be a dictionary")
        now = _instant(context["now"])
        if not isinstance(context.get("home"), str) or not context["home"]:
            raise ValueError("home must be an explicit path")
        if not isinstance(context.get("artifact_dir"), str) or not context["artifact_dir"]:
            raise ValueError("artifact_dir must be an explicit path")
        home = Path(context["home"]).expanduser().resolve()
        config = context.get("sources", {})
        if not isinstance(config, dict):
            raise ValueError("sources must be a dictionary")
        config = config.get("timeline", config)
        if not isinstance(config, dict):
            raise ValueError("sources.timeline must be a dictionary")
    except (KeyError, ValueError, TypeError, OSError, OverflowError, RuntimeError):
        result["reason"] = "Invalid context: provide explicit home/artifact_dir, offset-aware now, and sources object."
        result["fingerprint"] = _digest({"status": "blocked", "reason": "invalid_context"})
        return result

    evidence = _Evidence(home, now)
    evidence.load(config)
    candidates, conflicts = _recurrences(evidence)
    selected = candidates[0] if candidates else None
    report = {
        "schema": "storykeeper.timeline/1", "generated_at": _stamp(now),
        "sources": evidence.inventory, "selected": selected,
        "root_cause": "not established", "hypotheses": [], "correlations": [],
        "recovery_receipts": [], "schedule_configuration": evidence.schedules,
        "observations": evidence.events,
        "limits": [
            "Observed order and correlation are not proven cause.",
            "Earliest retained failure is not necessarily the actual onset.",
            "Logs/receipts are source claims; no signatures or device clocks were independently verified.",
            "Mirrored log/chain/verdict records are not independent failures.",
            "No services, devices, network endpoints, permissions, or delivery channels were contacted.",
        ],
    }
    ties = []
    if selected:
        ties = [item for item in candidates if
                (item["last_failure"], item["first_failure"]) ==
                (selected["last_failure"], selected["first_failure"])]
        related, hypotheses, recovery = _correlate(evidence, selected)
        report.update(correlations=related, hypotheses=hypotheses, recovery_receipts=recovery)
        report["schedule_comparison"] = _schedule_context(evidence, selected)
        result.update(
            status="ready", title=f"Recurring failure: {selected['check']}",
            change=(
                f"{selected['device']}: {selected['check']} appeared in {selected['total_samples']} "
                f"distinct failure samples across {selected['episodes']} observed episode(s). "
                f"Earliest actionable evidence in the latest episode: {selected['first_failure']}; "
                f"latest failure: {selected['last_failure']}."
            ),
            impact="The check repeatedly reported a problem. Its downstream impact and root cause are not established.",
            action=(
                f"Safest recovery option: first compare {selected['check']}'s earliest failed observation "
                "with any retained prior clear record and saved repair probes, read-only. Preserve the evidence; "
                "request an owner-reviewed, reversible repair only after that check is validated. "
                "Do not restart, roll back, or replay a diagnosis as a repair."
            ),
            decision="review_local_timeline",
            reason="Repeated observations justify review; temporal ordering alone does not establish cause.",
            urgency="routine",
        )
        if not selected["active"]:
            result.update(
                status="suppressed", decision="observe_next_check", urgency="routine",
                reason=(
                    f"The latest recurring episode is {selected['clearance']} at {selected['cleared_at']}; "
                    "this is not proof of a permanent recovery or of what caused the change."
                ),
                action="No repair is justified by this historical episode alone; review the next saved check without changing services.",
            )
        elif now - _instant(selected["last_failure"]) > FRESHNESS:
            result.update(
                status="blocked", decision="hold_for_fresh_evidence", urgency="routine",
                reason="Latest recurring-failure evidence is over six hours old; current state is unknown.",
            )
        if any(item["device"] == selected["device"] and item["check"] == selected["check"]
               and _instant(item["at"]) >= _instant(selected["last_failure"]) for item in conflicts):
            result.update(
                status="blocked", decision="hold_for_consistent_evidence", urgency="routine",
                reason="Recent same-instant samples disagree about this check; current state is ambiguous.",
            )
        for at, source, label in (
            (selected["first_failure"], selected["first_source"], "Earliest retained actionable failure"),
            (selected["last_failure"], selected["last_source"], "Latest recorded failure"),
        ):
            result["evidence"].append({
                "source": source, "observation": f"{label}: {selected['check']} at {at}.",
            })
        detailed = [event for event in evidence.events if event["kind"] == "snapshot"
                    and event["device"] == selected["device"]
                    and event["details"].get(selected["check"])]
        if detailed:
            latest = detailed[-1]
            detail = latest["details"][selected["check"]]
            report["latest_recorded_symptom"] = {
                "at": latest["at"], "source": latest["source"],
                "detail": detail,
                "limit": "Recorded check output, not an independently verified causal explanation.",
            }
            if not re.search(r"\b(root cause|caused by|fixed by)\b", detail, re.IGNORECASE):
                symptom = re.split(r";\s*(?:deploy with|run|execute):", detail, maxsplit=1,
                                   flags=re.IGNORECASE)[0]
                result["impact"] = (
                    f"The saved check reports: {symptom}. "
                    "This is a recorded symptom, not a causal finding or measured downstream impact."
                )
                result["evidence"].append({
                    "source": latest["source"],
                    "observation": f"Recorded check output at {latest['at']}: {symptom}. Cause unproven.",
                })
        if not selected["previous_clear"]:
            result["reason"] += " No preceding clear sample is retained; actual onset is unknown."
        if len(ties) > 1:
            names = ", ".join(f"{item['device']}:{item['check']}" for item in ties)
            result["reason"] += f" Equally recent checks: {names}; deterministic display is not causal priority."
        if not any(event["kind"] in ("repair_attempt", "repair_probe") for event in related):
            result["evidence"].append({
                "source": "retained repair receipts",
                "observation": (
                    "No matching saved repair attempt or probe was found for this check within the "
                    "bounded sources; no demonstrated recovery procedure can be recommended."
                ),
            })
        for comparison in report["schedule_comparison"]:
            if comparison["median_recorded_failure_gap_seconds"] is not None:
                interval = comparison["configured_interval_seconds"]
                configured = f"{interval} seconds" if interval is not None else "not a fixed interval"
                result["evidence"].append({
                    "source": comparison["source"],
                    "observation": (
                        f"Latest-episode failure samples have median spacing "
                        f"{comparison['median_recorded_failure_gap_seconds']:g} seconds; "
                        f"configured reporter cadence is {configured}. " + comparison["limit"]
                    ),
                })
        for event in related[-8:]:
            result["evidence"].append({
                "source": event["source"],
                "observation": f"{event['at']} {event['kind']}: {event['limit']}"
                if "limit" in event else
                f"{event['at']} deployment preceded the retained failure; correlation only, not proven cause.",
            })
    else:
        snapshots = [event for event in evidence.events if event["kind"] == "snapshot"]
        if snapshots and not any(event["failed"] for event in snapshots):
            result.update(
                status="suppressed", title="No recurring failure established",
                decision="observe_next_check",
                reason="Retained snapshots list no failing checks; this does not prove complete coverage or permanent health.",
            )
        elif snapshots:
            result["reason"] = "Fewer than two independent timestamped failure samples establish any recurring check."

    report["equally_recent_checks"] = [{"device": item["device"], "check": item["check"]} for item in ties]
    devices = sorted({event["device"] for event in evidence.events if event["device"] != "local"})
    report["other_device_receipts"] = devices
    result["evidence"].append({
        "source": "scope",
        "observation": (
            "Existing local receipts name other devices: " + ", ".join(devices)
            + ". No device was contacted; clocks and claims are unverified."
            if devices else "Local evidence only; no usable other-device receipts were supplied."
        ),
    })
    for schedule in evidence.schedules:
        result["evidence"].append({
            "source": schedule["source"],
            "observation": f"Configured schedule for {schedule['label']}: "
            + json.dumps(schedule["configuration"], sort_keys=True) + ". " + schedule["limit"],
        })
    issues = sorted(evidence.issues.values(), key=lambda item: (item["source"], item["code"]))
    report["evidence_gaps"] = issues
    for issue in issues:
        result["evidence"].append({
            "source": issue["source"],
            "observation": f"{issue['code']}: {issue['observation']} ({issue['count']} occurrence(s))",
        })
    if issues:
        result["reason"] += " Evidence gaps are listed; missing/corrupt records are not silently treated as healthy."
    semantic = {
        "version": 1, "status": result["status"], "decision": result["decision"],
        "check": selected["check"] if selected else None,
        "device": selected["device"] if selected else None,
        "episode_after": selected["previous_clear"] if selected else None,
        "urgency": result["urgency"], "ties": report["equally_recent_checks"],
        "repair_outcomes": sorted({item["outcome"] for item in report["recovery_receipts"]}),
        "revisions": sorted({event.get("revision", event.get("running_revision", "")) + ":"
                             + event.get("published_revision", "") for event in report["correlations"]
                             if event["kind"] in ("deployment", "deployment_state")}),
        "cause": "not_established", "other_devices": devices,
    }
    result["fingerprint"] = _digest(semantic)
    result = _compact_ready(result, report, selected)
    return _persist(result, report, context["artifact_dir"])
