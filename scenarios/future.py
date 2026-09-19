"""Evidence-gated seven-day forecasts. No transport or live-state writes."""

import ast
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA = "storykeeper.future-plan/v1"
JSON_LIMIT = 1024 * 1024
LOG_LIMIT = 256 * 1024
LOOKBACK = timedelta(hours=48)
FRESH_VERDICT = timedelta(hours=2)
FRESH_PLAN = timedelta(hours=24)
MIN_STREAK = timedelta(minutes=15)
MESSAGE_CHARS = 650
MESSAGE_WORDS = 90
IMPACT = {"low": 1, "medium": 2, "high": 3, "critical": 4}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z")
STATUS_LINE = re.compile(
    r"^\[([^\]]+)\] status=(healthy|degraded|critical) failing=(.+)$")


class InputError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _stamp(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InputError("invalid_time", f"{label} needs an ISO8601 timezone.") from None


def _iso(stamp):
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "future:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _quiet(status, code, reason, evidence):
    return {
        "scenario": "future",
        "status": status,
        "title": "No supported message from the future",
        "change": "No seven-day event forecast has been asserted.",
        "impact": "Unknown; missing evidence is not evidence of safety.",
        "action": "No remediation, scheduling, or delivery was performed.",
        "decision": "",
        "evidence": evidence,
        "artifacts": [],
        "fingerprint": _fingerprint({"scenario": "future", "status": status, "code": code}),
        "urgency": "routine",
        "reason": reason,
    }


def _path(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InputError("source_config", f"{label} needs an explicit absolute path.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise InputError("source_config", f"{label} needs an explicit absolute path.")
    # Exact file authorization must not turn into a symlink-assisted wider read.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise InputError("source_config", f"{label} must not traverse symlinks.")
    return path


def _read(path, tail=False):
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise InputError("source_shape", f"{path.name} is not a regular file.")
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise InputError("source_shape", f"{path.name} is not a regular file.")
            if not tail and info.st_size > JSON_LIMIT:
                raise InputError("source_bound", f"{path.name} exceeds the 1 MiB JSON bound.")
            start = max(0, info.st_size - LOG_LIMIT) if tail else 0
            stream.seek(start)
            data = stream.read(LOG_LIMIT if tail else JSON_LIMIT + 1)
        if not tail and len(data) > JSON_LIMIT:
            raise InputError("source_bound", f"{path.name} exceeds the 1 MiB JSON bound.")
        if start:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8")
    except FileNotFoundError:
        raise InputError("source_missing", f"Missing configured evidence: {path.name}.") from None
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("source_unreadable", f"Cannot read evidence: {path.name}.") from None


def _json(path):
    def reject_constant(_):
        raise ValueError

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            _read(path), parse_constant=reject_constant, object_pairs_hook=unique_object)
    except InputError:
        raise
    except (ValueError, RecursionError):
        raise InputError("source_shape", f"Invalid JSON in {path.name}.") from None


def _text(value, label, limit=300):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InputError("plan_shape", f"{label} must be bounded, single-line text.")
    return value.strip()


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise InputError("source_shape", "Evidence contains an invalid identifier.")
    return value


def _hours(value, label, zero=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (not zero and value == 0) or value > 8760):
        raise InputError("plan_shape", f"{label} needs a finite, documented hour estimate.")
    return float(value)


def _fresh(stamp, now, window, label):
    if stamp > now or now - stamp > window:
        raise InputError("stale_evidence", f"{label} is stale or future-dated; refresh it first.")


def _verdict(path, now):
    doc = _json(path)
    if not isinstance(doc, dict):
        raise InputError("verdict_shape", "The native verdict must be an object.")
    stamp = _stamp(doc.get("generated"), "Verdict generated")
    _fresh(stamp, now, FRESH_VERDICT, "Native health verdict")
    checks = doc.get("checks")
    if not isinstance(checks, list) or not checks or len(checks) > 512:
        raise InputError("verdict_shape", "The native verdict needs 1–512 explicit checks.")
    by_id = {}
    for check in checks:
        if not isinstance(check, dict):
            raise InputError("verdict_shape", "Malformed native check.")
        cid = _id(check.get("id"))
        if (cid in by_id or type(check.get("ok")) is not bool
                or check.get("severity") not in ("warn", "critical")):
            raise InputError("verdict_shape", "Ambiguous native check status or identifier.")
        by_id[cid] = check
    failed = frozenset(cid for cid, check in by_id.items() if not check["ok"])
    declared = doc.get("failed")
    if (not isinstance(declared, list) or any(not isinstance(x, str) for x in declared)
            or len(set(declared)) != len(declared) or set(declared) != failed):
        raise InputError("verdict_shape", "Native failed IDs disagree with the check results.")
    expected = "critical" if any(
        by_id[cid]["severity"] == "critical" for cid in failed
    ) else ("degraded" if failed else "healthy")
    if doc.get("status") != expected:
        raise InputError("verdict_shape", "Native verdict status disagrees with its checks.")
    return stamp, by_id, failed


def _plan(path, now):
    doc = _json(path)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise InputError("plan_shape", f"The project plan must declare schema {SCHEMA}.")
    updated = _stamp(doc.get("updated_at"), "Plan updated_at")
    _fresh(updated, now, FRESH_PLAN, "Project plan")
    projects = doc.get("projects")
    if not isinstance(projects, list) or len(projects) > 50:
        raise InputError("plan_shape", "The plan needs a projects list of at most 50 entries.")
    parsed, seen = [], set()
    for project in projects:
        if not isinstance(project, dict):
            raise InputError("plan_shape", "Malformed project record.")
        pid = _id(project.get("id"))
        if pid in seen:
            raise InputError("plan_shape", "Duplicate project identifier.")
        seen.add(pid)
        status = project.get("status")
        if status not in ("active", "completed", "cancelled"):
            raise InputError("plan_shape", "Project status must be active, completed, or cancelled.")
        if status != "active":
            continue
        title = _text(project.get("title"), "Project title", 120)
        deadline = _stamp(project.get("deadline"), "Project deadline")
        impact = _text(project.get("impact"), "Project impact")
        level = project.get("impact_level")
        if not isinstance(level, str) or level not in IMPACT:
            raise InputError("plan_shape", "Project impact_level must be explicit.")
        dependencies = project.get("dependencies")
        if not isinstance(dependencies, list) or len(dependencies) > 16:
            raise InputError("plan_shape", "Each project needs at most 16 named dependencies.")
        deps, dep_ids = [], set()
        for dep in dependencies:
            if not isinstance(dep, dict) or type(dep.get("required")) is not bool:
                raise InputError("plan_shape", "Each dependency must declare whether it is required.")
            cid = _id(dep.get("check_id"))
            if cid in dep_ids:
                raise InputError("plan_shape", "Duplicate dependency in a project.")
            dep_ids.add(cid)
            if not dep["required"]:
                continue
            deps.append({
                "check_id": cid,
                "recovery_hours": _hours(dep.get("recovery_hours"), "recovery_hours"),
                "downstream_hours": _hours(
                    dep.get("downstream_hours"), "downstream_hours", zero=True),
                "recovery_basis": _text(dep.get("recovery_basis"), "Recovery estimate basis"),
            })
        parsed.append({
            "id": pid, "title": title, "deadline": deadline, "impact": impact,
            "impact_level": level, "dependencies": deps,
        })
    return updated, parsed


def _history(paths, now, optional=False):
    events, sources = {}, []
    for path in paths:
        if optional and not path.exists():
            continue
        text = _read(path, tail=True)
        count = 0
        for line in text.splitlines():
            match = STATUS_LINE.fullmatch(line)
            if not match:
                if "status=" in line and " failing=" in line:
                    raise InputError("history_shape", "Malformed native status-log record.")
                continue
            stamp = _stamp(match[1], "Log timestamp")
            if stamp > now:
                raise InputError("stale_evidence", "A status-log observation is future-dated.")
            if now - stamp > LOOKBACK:
                continue
            try:
                raw = [] if match[3] == "none" else ast.literal_eval(match[3])
            except (ValueError, SyntaxError, RecursionError):
                raise InputError("history_shape", "Malformed failing IDs in the native log.") from None
            if not isinstance(raw, list) or len(raw) > 512:
                raise InputError("history_shape", "Native failing IDs must be a bounded list.")
            failed = frozenset(_id(cid) for cid in raw)
            if len(failed) != len(raw) or (match[2] == "healthy") != (not failed):
                raise InputError("history_shape", "Native status-log fields disagree.")
            if stamp in events and events[stamp] != failed:
                raise InputError("history_shape", "Conflicting native observations at one timestamp.")
            events[stamp] = failed
            count += 1
        sources.append({
            "source": str(path),
            "observation": f"Read a bounded 256 KiB tail; {count} status observations within 48 hours.",
        })
    return events, sources


def _streak(cid, events):
    times = []
    for stamp, failed in sorted(events.items(), reverse=True):
        if cid not in failed or (times and times[-1] - stamp > FRESH_VERDICT):
            break
        times.append(stamp)
    return times if len(times) >= 2 and times[0] - times[-1] >= MIN_STREAK else []


def _check_notification_budget(proposal):
    parts = [proposal["title"]]
    for label, field in (
            ("Changed", "change"), ("Impact", "impact"),
            ("Action", "action"), ("Need", "decision")):
        parts.append(f"{label}: {proposal[field]}")
    parts.append("Evidence: " + "; ".join(
        f"{row['source']}: {row['observation']}" for row in proposal["evidence"]))
    parts.append("Deadline: " + proposal["deadline"])
    # Keep combining accents in this upper bound: the shared renderer can
    # shorten transliterations, but this scenario must never require truncation.
    bound = "\n".join(
        unicodedata.normalize("NFKD", " ".join(part.split()))
        .encode("ascii", "backslashreplace").decode("ascii") for part in parts)
    if len(bound) > MESSAGE_CHARS or len(bound.split()) > MESSAGE_WORDS:
        raise InputError(
            "notification_budget",
            "The complete forecast exceeds the 650-character/90-word notification budget; "
            "provide a concise project title and consequence. No evidence was truncated.")


def _artifact(directory, fingerprint, project, dep, times, proposal, evidence, source_map):
    root = _path(directory, "artifact_dir")
    name = "future-" + fingerprint.split(":")[1][:24] + ".md"
    destination = root / name
    staging = root / ("." + name + "." + uuid.uuid4().hex)
    deadline = _iso(project["deadline"])
    total = dep["recovery_hours"] + dep["downstream_hours"]
    # This is a reviewable work-order draft, never executable remediation.
    lines = [
        "# Recovery-first prevention draft", "",
        f"Project: {project['id']} — {project['title']}",
        f"Forecast event: likely deadline slip by {deadline}, conditional on no recovery.",
        f"Evaluation window: {proposal['change']}",
        f"Required gate: {dep['check_id']}",
        f"Nonpassing observations: {_iso(times[-1])} through {_iso(times[0])}.",
        f"Configured remaining work: {dep['recovery_hours']:g}h recovery + "
        f"{dep['downstream_hours']:g}h downstream = {total:g}h.",
        f"Estimate basis (local plan, not independently verified): {dep['recovery_basis']}",
        f"Consequence recorded in the plan: {project['impact']}", "",
        "## Uncertainty",
        "Moderate, conditional confidence; not a calibrated probability or a guarantee.",
        "A nonpassing check can mean missing visibility, not a broken service. The plan "
        "explicitly requires a passing gate. Faster recovery, work already completed, "
        "or a changed deadline could invalidate this forecast.", "",
        "## Smallest reversible prevention",
        "- Review the existing gate evidence and confirm the remaining-work estimate first.",
        "- Use this draft to prioritize dependency recovery before downstream project work; "
        "do not start discretionary additions while that critical path lacks a passing gate.",
        "- Recheck with the already-authorized workflow after any separately approved repair. "
        "Discard this draft if the gate passes or the plan changes.",
        "This file is only a local scheduling/recovery checklist. No calendar, project, "
        "service, configuration, or message has been changed. Deleting it reverses all "
        "scenario-created preparation.", "",
        "## One decision", proposal["decision"], "", "## Notification source labels",
    ]
    lines.extend(f"- {label}: {paths}" for label, paths in source_map.items())
    lines.extend(["", "## Detailed evidence"])
    lines.extend(f"- {row['source']}: {row['observation']}" for row in evidence)
    content = "\n".join(lines) + "\n"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_symlink():
            raise InputError("artifact_write", "The prevention artifact must not be a symlink.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(staging, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, destination)
    except OSError:
        raise InputError("artifact_write", "Cannot persist the private prevention draft.") from None
    finally:
        if staging.exists():
            staging.unlink()
    return str(destination)


def build(context: dict) -> dict:
    """Build one conditional forecast; read only exact authorized source files."""
    evidence = []
    try:
        if not isinstance(context, dict):
            raise InputError("context_shape", "A context object is required.")
        now = _stamp(context.get("now"), "Context now")
        horizon = now + timedelta(days=7)
        home = _path(context.get("home"), "home")
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            raise InputError("source_config", "sources must map names to explicit paths.")
        verdict_path = _path(
            sources.get("future_verdict", str(home / "state" / "last_verdict.json")),
            "future_verdict")
        generated, checks, failed = _verdict(verdict_path, now)
        evidence.append({
            "source": str(verdict_path),
            "observation": f"Native health verdict generated {_iso(generated)}: "
            f"{len(checks)} checks, {len(failed)} nonpassing. These are observations, not forecasts.",
        })
        plan_path = _path(
            sources.get("future_plan", sources.get("future", str(home / "state" / "future-plan.json"))),
            "future_plan")
        updated, projects = _plan(plan_path, now)
        evidence.append({
            "source": str(plan_path),
            "observation": f"Explicit project/dependency plan updated {_iso(updated)}; "
            f"{len(projects)} active projects. No deadlines inferred from health failures.",
        })
        eligible = [p for p in projects if now < p["deadline"] <= horizon]
        if not eligible:
            return _quiet(
                "suppressed", "no_horizon",
                f"No active project deadline falls after {_iso(now)} and through {_iso(horizon)}.",
                evidence)

        risks, unknown = [], False
        for project in eligible:
            if not project["dependencies"]:
                unknown = True
            for dep in project["dependencies"]:
                cid = dep["check_id"]
                if cid not in checks:
                    unknown = True
                elif not checks[cid]["ok"]:
                    work = timedelta(hours=dep["recovery_hours"] + dep["downstream_hours"])
                    if work >= project["deadline"] - now:
                        risks.append((project, dep))
        if unknown:
            raise InputError(
                "missing_causal_evidence",
                "An in-horizon project lacks required-gate evidence; cannot rank the most consequential event.")
        if not risks:
            return _quiet(
                "suppressed", "no_supported_risk",
                "No observed required-gate failure consumes the documented deadline runway. "
                "This is not a guarantee that all projects are safe.", evidence)

        configured = sources.get("future_history")
        if configured is None:
            months = sorted({now.strftime("%Y-%m"), (now - LOOKBACK).strftime("%Y-%m")})
            history_paths = [str(home / "logs" / f"sentinel-{month}.log") for month in months]
        elif isinstance(configured, str):
            history_paths = [configured]
        elif isinstance(configured, list) and 1 <= len(configured) <= 4:
            history_paths = configured
        else:
            raise InputError("source_config", "future_history needs one to four explicit log paths.")
        history_paths = [_path(value, "future_history") for value in history_paths]
        events, history_evidence = _history(history_paths, now, optional=configured is None)
        evidence.extend(history_evidence)
        if generated in events and events[generated] != failed:
            raise InputError("history_shape", "Verdict and status log disagree at the same timestamp.")
        events[generated] = failed
        candidates = []
        for project, dep in risks:
            times = _streak(dep["check_id"], events)
            if times:
                candidates.append((project, dep, times))
        if not candidates:
            raise InputError(
                "uncertain_persistence",
                "Insufficient corroboration: require distinct nonpassing observations spanning "
                "15 minutes, no intervening nonfailure, and no gap over two hours. No forecast invented.")
        candidates.sort(key=lambda item: (
            -IMPACT[item[0]["impact_level"]], item[0]["deadline"],
            item[0]["id"], item[1]["check_id"]))
        project, dep, times = candidates[0]
        deadline = _iso(project["deadline"])
        fingerprint = _fingerprint({
            "scenario": "future", "event": "required-gate-deadline-slip",
            "project": project["id"], "deadline": deadline,
            "required_gate": dep["check_id"],
            "recovery_hours": dep["recovery_hours"],
            "downstream_hours": dep["downstream_hours"],
            "impact_level": project["impact_level"], "impact": project["impact"],
        })
        evidence.append({
            "source": str(plan_path),
            "observation": f"{project['id']} requires a passing {dep['check_id']} gate by {deadline}; "
            f"remaining estimate {dep['recovery_hours']:g}h recovery plus "
            f"{dep['downstream_hours']:g}h downstream. Basis: {dep['recovery_basis']}",
        })
        evidence.append({
            "source": str(verdict_path),
            "observation": f"{dep['check_id']} is currently nonpassing; corroborated by "
            f"{len(times)} distinct native observations from {_iso(times[-1])} to {_iso(times[0])}.",
        })
        total = dep["recovery_hours"] + dep["downstream_hours"]
        plan_label = "plan:" + project["id"]
        gate_label = "verdict/log:" + dep["check_id"]
        proposal = {
            "scenario": "future",
            "status": "ready",
            "title": f"Future: {project['title']}",
            "change": "Likely deadline slip if the gate stays unready. "
            f"Horizon {_iso(now)}/{_iso(horizon)}.",
            "impact": project["impact"],
            "action": "Drafted recovery-first checklist; nothing applied.",
            "decision": "Prioritize the recovery-first checklist?",
            "evidence": [{
                "source": plan_label,
                "observation": f"Due {deadline}; {total:g}h critical path. {project['impact']}",
            }, {
                "source": gate_label,
                "observation": "Required gate repeatedly nonpassing.",
            }],
            "artifacts": [],
            "fingerprint": fingerprint,
            "urgency": (
                "time_sensitive" if project["deadline"] - now <= timedelta(hours=24)
                else "routine"),
            "deadline": deadline,
            "reason": "Moderate, conditional confidence, not a calibrated probability or a guarantee. "
            "Full source paths, observations, estimates, and caveats are in the private checklist.",
        }
        _check_notification_budget(proposal)
        source_map = {
            plan_label: str(plan_path),
            gate_label: "; ".join(
                [str(verdict_path)] + [row["source"] for row in history_evidence]),
        }
        proposal["artifacts"] = [_artifact(
            context.get("artifact_dir"), fingerprint, project, dep, times,
            proposal, evidence, source_map)]
        return proposal
    except InputError as exc:
        return _quiet("blocked", exc.code, str(exc), evidence)
    except (OSError, OverflowError):
        return _quiet(
            "blocked", "local_io", "Local evidence or artifact access failed; no forecast asserted.",
            evidence)
