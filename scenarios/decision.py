"""Turn repeated stall alerts into a bounded, read-only decision.

No Storykeeper modules are imported: several initialize state or delivery on
import. Only the caller's artifact directory is written.
"""

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


TARGET = "rappterverse"
CHECKS = ("rv_meaningful_activity", "rv_world_merging")
ANCILLARY = ("rv_validation", "alert_delivery", "w_openrappter_spin")
SOURCE_BOUNDS = {
    "document_bytes": 262144,
    "ledger_tail_bytes": 262144,
    "ledger_rows": 200,
    "verdict_checks": 200,
    "escalation_rows": 200,
    "exact_attempts": 6,
    "related_attempts": 2,
    "receipt_bytes": 65536,
    "verdict_max_age_seconds": 7200,
    "future_tolerance_seconds": 300,
}
DEFAULT_SOURCES = {
    "verdict": "state/last_verdict.json",
    "sent_ledger": "state/outbox-sent.jsonl",
    "unknown_ledger": "state/outbox-unknown.jsonl",
    "issues": "state/issues.json",
    "escalations": "state/escalations.json",
    "config": "config.json",
    "direction": "direction.json",
    "logs": "logs",
}


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _text(value, limit=700):
    return value[:limit] if isinstance(value, str) else ""


def _key(value):
    if not isinstance(value, str):
        return ()
    parts = value.split(",")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", p.strip()) for p in parts):
        return ()
    return tuple(sorted(set(p.strip() for p in parts)))


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


class _Sources:
    def __init__(self, home, overrides):
        self.paths = {}
        self.reads = []
        for name, default in {**DEFAULT_SOURCES, "intent": None,
                              "runtime": None}.items():
            value = overrides.get(name, default)
            if value is None and default is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError("source paths must be nonempty strings")
            path = Path(value).expanduser()
            self.paths[name] = (path if path.is_absolute() else home / path).resolve()

    def read(self, name, *, path=None, tail=False, receipt=False):
        path = path or self.paths.get(name)
        if path is None:
            return None, {"name": name, "status": "not_supplied"}
        limit = SOURCE_BOUNDS["receipt_bytes" if receipt else
                              "ledger_tail_bytes" if tail else "document_bytes"]
        info = {"name": name, "source": str(path), "status": "unreadable"}
        self.reads.append(info)
        try:
            # NONBLOCK plus fstat prevents a supplied FIFO/device from hanging.
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    info["status"] = "not_regular"
                    return None, info
                if not tail and metadata.st_size > limit:
                    info["status"] = "over_bound"
                    return None, info
                offset = max(0, metadata.st_size - limit) if tail else 0
                stream.seek(offset)
                data = stream.read(limit + 1)
            if len(data) > limit:
                info["status"] = "changed_or_over_bound"
                return None, info
            info.update(bytes_read=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        truncated=bool(offset))
            if offset:
                data = data.partition(b"\n")[2]
            text = data.decode("utf-8")
            info["status"] = "ok"
            return text, info
        except FileNotFoundError:
            info["status"] = "missing"
        except (OSError, UnicodeError, ValueError):
            info["status"] = "unreadable"
        return None, info

    def document(self, name, expected):
        raw, info = self.read(name)
        if raw is None:
            return None
        try:
            value = json.loads(raw, parse_constant=_reject_constant)
            if not isinstance(value, expected):
                raise ValueError("wrong JSON shape")
            return value
        except (ValueError, RecursionError):
            info["status"] = "corrupt"
            return None

    def ledger(self, name):
        raw, info = self.read(name, tail=True)
        if raw is None:
            return []
        lines = raw.splitlines()
        if len(lines) > SOURCE_BOUNDS["ledger_rows"]:
            info["truncated"] = True
        rows, bad = [], 0
        for line in lines[-SOURCE_BOUNDS["ledger_rows"]:]:
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_constant=_reject_constant)
                if not isinstance(row, dict):
                    raise ValueError("wrong row shape")
                rows.append(row)
            except (ValueError, RecursionError):
                bad += 1
        info["rows_read"] = len(rows)
        info["corrupt_rows"] = bad
        if bad:
            info["status"] = "partly_corrupt"
        return rows

    def source(self, name):
        return str(self.paths.get(name, name + " (not supplied)"))


def _attempt(sources, row, relation):
    at = _stamp(row.get("at"))
    result = {
        "at": _text(row.get("at")), "key": list(_key(row.get("key"))),
        "mode": row.get("mode") if row.get("mode") in ("diagnose", "repair") else "unknown",
        "reported_result": _text(row.get("result"), 1200),
        "relation": relation, "outcome": "unknown", "receipt_status": "missing",
    }
    if at is None:
        result["receipt_status"] = "invalid_timestamp"
        return result
    path = sources.paths["logs"] / ("escalation-" + at.strftime("%Y%m%d-%H%M%S") + ".log")
    raw, info = sources.read("attempt_receipt", path=path, receipt=True)
    result.update(source=str(path), receipt_status=info["status"])
    if raw is None:
        return result
    result.update(receipt=raw, sha256=info["sha256"])
    if not raw.strip():
        result["receipt_status"] = info["status"] = "empty"
        return result
    if re.fullmatch(r"\s*copilot timed out after \d+s\s*", raw):
        result["outcome"] = "timeout"
    else:
        markers = re.findall(
            r"(?m)^SENTINEL_RESULT:\s*(FIXED|PARTIAL|NO_ACTION|BLOCKED)\b([^\n]*)", raw)
        if markers:
            code, explanation = markers[-1]
            result["outcome"] = code.lower()
            result["terminal_claim"] = code + _text(explanation, 900)
    return result


def _signal(row):
    if not isinstance(row, dict) or type(row.get("ok")) is not bool:
        return "unavailable"
    if row["ok"]:
        return "healthy"
    detail = _text(row.get("detail")).lower()
    if any(term in detail for term in (
            "cannot read", "unreadable", "cannot audit", "unverified")):
        return "unreadable"
    if any(term in detail for term in (
            "stale", "last merge", "no state merges", "not run",
            "only ", "repeat ", "no messages")):
        return "stalled"
    return "failing"


def _runtime_kind(runtime):
    if not isinstance(runtime, dict) or runtime.get("subject") != "openrappter":
        return "unverified"
    job = runtime.get("job")
    if not isinstance(job, dict):
        return "unverified"
    runs, last_exit = job.get("runs"), job.get("last_exit")
    spinning = (job.get("state") in ("spawn scheduled", "not running", "exited")
                and type(runs) is int and runs >= 3
                and type(last_exit) is int and last_exit != 0)
    if not spinning:
        return "unverified"
    if (runtime.get("error_kind") == "runtime_lock_owned"
            and runtime.get("lock_owner_alive") is True
            and runtime.get("listener_alive") is True
            and type(runtime.get("lock_owner_pid")) is int
            and runtime.get("lock_owner_pid") > 0
            and runtime.get("lock_owner_pid") == runtime.get("listener_pid")):
        return "duplicate_launcher"
    return "crash_loop"


def _fresh(stamp, now):
    value = _stamp(stamp)
    return value is not None and -SOURCE_BOUNDS["future_tolerance_seconds"] <= (
        now - value).total_seconds() <= SOURCE_BOUNDS["verdict_max_age_seconds"]


def _brief(classification, measured_stall, mislabeled, count, timeouts, deadline):
    title, change, impact, action, question, why, source, observation = {
        "mismeasured": (
            "Pause the repeated alert",
            "Alert target conflicts with the declared scope.",
            "Public world may be stale; gateway and Messages failures are unproven.",
            "Compared target and scope; prepared an escalation-only pause proposal. Services untouched.",
            "Pause only this escalation while confirming its target?",
            "claim/scope mismatch",
            "verdict + direction",
            "Exclusive declared scope excludes this alert target."),
        "broken": (
            "Repair the required producer",
            "Active target has stale expected output.",
            "Users may see an unchanged world; root cause is unproven.",
            "Checked intent and activity receipts; prepared repair triage with genuine-output verification.",
            "Approve scoped repair triage for this producer before changing live services?",
            "required output stalled",
            "verdict + owner intent",
            "Owner expects activity; checks report stale output."),
        "asleep": (
            "Keep the pause explicit",
            "Owner explicitly paused this target.",
            "Intentional inactivity does not establish an outage.",
            "Reviewed the pause receipt; prepared a continued-pause proposal without restarting services.",
            "Keep only this escalation paused under the owner's existing pause decision?",
            "explicit pause intent",
            "owner intent",
            "Paused by the owner."),
        "retired": (
            "Retire the obsolete check",
            "Owner explicitly retired this target.",
            "Stall alerts no longer represent required work.",
            "Checked retirement authority; prepared a retirement proposal for this escalation, retaining evidence.",
            "Retire only this escalation, preserving live services and evidence?",
            "explicit owner decision",
            "owner intent",
            "Retirement authority supplied; no automatic changes."),
        "inconclusive": (
            "Confirm the target before retrying",
            "Stall alerts persist; repair completion is unverified.",
            "Silence cannot distinguish broken, asleep or retired.",
            "Reviewed bounded receipts; prepared a missing-evidence checklist. No service changes made.",
            "Hold only this escalation while the producer's role is confirmed?",
            "producer state unconfirmed",
            "verdict + issue records",
            "These receipts do not establish the current producer state."),
    }[classification]
    if not measured_stall:
        impact = "Current impact is unknown; unreadable checks do not prove an outage."
    if mislabeled:
        if classification == "mismeasured":
            change = (f"{count} diagnose runs, not verified repairs; "
                      f"{timeouts} timeout receipts.")
            action = ("Checked exact receipts; prepared an escalation-only pause proposal. "
                      "Live services untouched.")
            source = "attempt ledger + exact logs"
            observation = "Diagnose-mode attempts; completion unverified."
        else:
            source += " + exact logs"
            observation += " Diagnose attempts are not verified repairs."
    if deadline:
        observation += " Declared wake deadline: " + deadline.isoformat() + "."
    return {
        "title": title, "change": change, "impact": impact, "action": action,
        "decision": question,
        "reason": classification + ": " + why,
        "evidence": [{"source": source, "observation": observation}],
    }


def _empty(reason):
    return {
        "scenario": "decision", "status": "blocked",
        "title": "Decision needs readable evidence",
        "change": "No trustworthy current comparison is available.",
        "impact": "Unknown; missing evidence is not evidence of an outage or recovery.",
        "action": "Checked request validity; no source or service changes were made.",
        "decision": "Wait for readable evidence before any service changes?",
        "evidence": [], "artifacts": [],
        "fingerprint": "decision:" + _digest({"target": TARGET, "state": "unavailable"}),
        "urgency": "routine", "reason": reason,
    }


def build(context: dict) -> dict:
    """Return a decision, never enqueue/send it or remediate its sources.

    ``sources`` contains optional path overrides, relative to ``home`` unless
    absolute. Optional ``intent`` and ``runtime`` snapshots are documented in
    docs/scenarios/decision.md. The adapter supplies device-local paths through
    context; this module never guesses a user's home or permission state.
    No network, process, or Messages access occurs.
    """
    if not isinstance(context, dict):
        return _empty("inconclusive: context must be an object")
    now = _stamp(context.get("now"))
    if now is None:
        return _empty("inconclusive: now must be a timezone-aware ISO8601 timestamp")
    try:
        if not all(isinstance(context.get(k), str) and context[k].strip()
                   for k in ("home", "artifact_dir")):
            raise ValueError("home and artifact_dir are required")
        home = Path(context["home"]).expanduser().resolve()
        artifact_dir = Path(context["artifact_dir"]).expanduser().resolve()
        overrides = context.get("sources", {})
        if not isinstance(overrides, dict):
            raise ValueError("sources must be an object")
        sources = _Sources(home, overrides)
        if artifact_dir == home or home in artifact_dir.parents:
            raise ValueError("artifacts must be outside the live home")
        if artifact_dir == sources.paths["logs"] or sources.paths["logs"] in artifact_dir.parents:
            raise ValueError("artifacts must be outside the receipt directory")
    except (OSError, ValueError, RuntimeError):
        return _empty("inconclusive: invalid paths or artifact directory overlaps live evidence")

    verdict = sources.document("verdict", dict)
    sent = sources.ledger("sent_ledger")
    unknown = sources.ledger("unknown_ledger")
    issues = sources.document("issues", dict)
    escalations = sources.document("escalations", list)
    config = sources.document("config", dict) or {}
    direction = sources.document("direction", dict) or {}
    intent = sources.document("intent", dict) or {}
    runtime = sources.document("runtime", dict) or {}

    rows = verdict.get("checks") if verdict else None
    checks = {}
    if isinstance(rows, list) and len(rows) <= SOURCE_BOUNDS["verdict_checks"]:
        for row in rows:
            if isinstance(row, dict) and row.get("id") in CHECKS + ANCILLARY:
                if row["id"] in checks:
                    checks[row["id"]] = None
                else:
                    checks[row["id"]] = row
    signals = {key: _signal(checks.get(key)) for key in CHECKS}
    relevant = [row for row in sent if any(
        key in _text(row.get("text"), 8192) for key in CHECKS)]
    alert = relevant[-1] if relevant else {}
    claim = re.search(r"['\"]([a-z_,]+)['\"] survived (\d{1,4}) automated repairs",
                      _text(alert.get("text"), 8192))
    issue = _key(claim[1]) if claim else tuple(
        key for key in CHECKS if signals[key] not in ("healthy", "unavailable"))
    issue = issue if set(issue).intersection(CHECKS) else CHECKS
    claimed_repairs = int(claim[2]) if claim else None
    issue_record = (issues or {}).get(",".join(issue))
    expected = issue_record.get("attempts") if isinstance(issue_record, dict) else None
    if type(expected) is not int or expected < 0:
        expected = None

    history = [row for row in (escalations or [])[-SOURCE_BOUNDS["escalation_rows"]:]
               if isinstance(row, dict) and not row.get("skipped")]
    if escalations is not None:
        for read in sources.reads:
            if read["name"] == "escalations":
                read.update(rows_total=len(escalations),
                            truncated=len(escalations) > SOURCE_BOUNDS["escalation_rows"])
    exact = [row for row in history if _key(row.get("key")) == issue]
    if expected is not None and expected > 0:
        exact = exact[-min(expected, SOURCE_BOUNDS["exact_attempts"]):]
    related = [row for row in history if _key(row.get("key")) != issue
               and set(_key(row.get("key"))).intersection(CHECKS)]
    attempts = [_attempt(sources, row, "exact")
                for row in exact[-SOURCE_BOUNDS["exact_attempts"]:]]
    attempts += [_attempt(sources, row, "related")
                 for row in related[-SOURCE_BOUNDS["related_attempts"]:]]
    exact_attempts = [row for row in attempts if row["relation"] == "exact"]
    last_attempt = _stamp(issue_record.get("last_attempt")) if isinstance(issue_record, dict) else None
    complete = (expected is not None and expected > 0
                and len(exact_attempts) == expected
                and len({row.get("source") for row in exact_attempts}) == expected
                and last_attempt is not None
                and _stamp(exact_attempts[-1]["at"]) == last_attempt
                and all(row["receipt_status"] == "ok"
                        and _stamp(row["at"]) is not None
                        and _stamp(row["at"]) <= now + timedelta(
                            seconds=SOURCE_BOUNDS["future_tolerance_seconds"])
                        for row in exact_attempts))
    mislabeled = (claimed_repairs is not None and complete
                  and claimed_repairs == len(exact_attempts)
                  and all(row["mode"] == "diagnose" for row in exact_attempts))
    timeouts = sum(row["outcome"] == "timeout" for row in exact_attempts)
    situation = _text(direction.get("situation"), 4000)
    cares = direction.get("cares_about")
    cares = [item for item in cares[:40] if isinstance(item, str)] if isinstance(cares, list) else []
    scope_mismatch = bool(re.search(
        r"\bwatches (?:only |exactly )?one organism\b", situation, re.I)
        and TARGET not in (situation + " ".join(cares)).lower())
    lifecycle = intent.get("state") if (
        intent.get("target") == TARGET and _text(intent.get("reason")).strip()
        and intent.get("state") in ("active", "paused", "retired")) else None
    runtime_kind = _runtime_kind(runtime)
    runtime_current = _fresh(runtime.get("observed_at"), now)
    measured_stall = any(value == "stalled" for value in signals.values())
    if lifecycle == "retired":
        classification, decision = "retired", "retire"
    elif lifecycle == "paused":
        classification, decision = "asleep", "pause"
    elif lifecycle == "active" and measured_stall:
        classification, decision = "broken", "repair"
    elif mislabeled or scope_mismatch:
        classification, decision = "mismeasured", "pause"
    else:
        classification, decision = "inconclusive", "pause"

    evidence = []

    def note(name, observation):
        evidence.append({"source": sources.source(name), "observation": observation})

    if relevant:
        note("sent_ledger", f"{len(relevant)} relevant entries in the bounded tail; "
             f"latest claims {claimed_repairs} automated repairs."
             if claimed_repairs is not None else
             f"{len(relevant)} relevant entries in the bounded tail; no parsed repair count.")
    note("issues", f"Exact issue {','.join(issue)}: counter={expected}; "
         f"latest attempt={last_attempt.isoformat() if last_attempt else 'unknown'}; "
         f"{len(exact_attempts)} bounded matching records; complete receipts={complete}.")
    for key, row in checks.items():
        if isinstance(row, dict):
            note("verdict", key + ": " + _text(row.get("detail")))
    for attempt in attempts:
        label = f"{attempt['at']} [{attempt['relation']}, {attempt['mode']}]"
        detail = (("historical terminal claim (not independently verified): "
                   + attempt["terminal_claim"]) if attempt.get("terminal_claim") else
                  ("copilot timeout receipt; completion and side effects unverified"
                   if attempt["outcome"] == "timeout" else
                   "no terminal result established from the exact receipt"))
        evidence.append({
            "source": attempt.get("source", sources.source("escalations")),
            "observation": f"{label}: {detail}; receipt={attempt['receipt_status']}",
        })
    watchers = config.get("watchers")
    openrappter = watchers.get("openrappter") if isinstance(watchers, dict) else None
    probe_enabled = openrappter.get("enabled") if isinstance(openrappter, dict) else None
    probe_enabled = probe_enabled if type(probe_enabled) is bool else None
    level = config.get("level") if type(config.get("level")) is int else "unknown"
    note("config", f"level={level}; "
         f"openrappter port probe enabled={probe_enabled}. "
         "A disabled probe is not retirement intent and does not disable the spin audit.")
    if situation:
        note("direction", "Declared situation: " + situation[:900])
    if lifecycle:
        note("intent", f"Explicit target intent: {lifecycle}; " + _text(intent.get("reason")))
    if runtime:
        job = runtime.get("job") if isinstance(runtime.get("job"), dict) else {}
        runs = job.get("runs") if type(job.get("runs")) is int else "unknown"
        last_exit = job.get("last_exit") if type(job.get("last_exit")) is int else "unknown"
        note("runtime", f"{'Current' if runtime_current else 'Historical/unverified-age'} "
             f"snapshot: {runtime_kind}; job={_text(job.get('label'), 120)}, "
             f"state={_text(job.get('state'), 80)}, runs={runs}, "
             f"last_exit={last_exit}; error_kind="
             f"{_text(runtime.get('error_kind'), 80)}; observed_at="
             f"{_text(runtime.get('observed_at'))}. "
             "A listening process proves neither useful world activity nor who produces it.")
    delivery_unknown = bool(unknown) or (
        isinstance(checks.get("alert_delivery"), dict)
        and checks["alert_delivery"].get("ok") is False)
    if delivery_unknown or any(row.get("unverified") for row in relevant):
        note("unknown_ledger", f"{len(unknown)} UNKNOWN rows in the bounded tail; "
             "sent-ledger entries can also be unverified. Neither proves delivery or non-delivery. "
             "Rappterverse chat freshness is unrelated to the macOS Messages database.")
    gaps = [row for row in sources.reads if row["status"] != "ok"]
    for gap in gaps:
        evidence.append({"source": gap["source"],
                         "observation": "Evidence unavailable or incomplete: " + gap["status"]})

    status = "ready"
    reason = classification + ": "
    if classification == "mismeasured":
        reason += ("diagnose attempts were labeled as repairs; " if mislabeled else "")
        reason += ("declared scope and alert target differ; " if scope_mismatch else "")
        reason += ("a duplicate launcher is not a dead serving gateway; "
                   if runtime_kind == "duplicate_launcher" and runtime_current else "")
        reason += "intentional sleep/retirement and the producer relationship remain unproven"
    elif classification == "broken":
        reason += "explicit active intent conflicts with measured stale output; root cause is not proved"
    elif classification == "asleep":
        reason += "explicit pause intent, not silence alone"
    elif classification == "retired":
        reason += "explicit retirement intent, not a disabled watcher flag"
    else:
        reason += "stale output alone cannot distinguish broken, asleep, or retired"

    healthy = all(value == "healthy" for value in signals.values())
    if not verdict or not isinstance(rows, list) or len(rows) > SOURCE_BOUNDS["verdict_checks"]:
        status, reason = "blocked", "inconclusive: missing, corrupt, or invalid current verdict"
    elif not _fresh(verdict.get("generated"), now):
        status, reason = "blocked", "inconclusive: verdict is stale, future-dated, or lacks an aware timestamp"
    elif healthy:
        status = "suppressed"
        reason = "recovered: both target checks currently pass; historical alerts are not a new incident"
    elif any(value == "unavailable" for value in signals.values()):
        status, reason = "blocked", "inconclusive: target check coverage is missing or malformed"
    elif claimed_repairs is not None and (not complete or claimed_repairs != expected):
        status, reason = "blocked", "inconclusive: the claimed attempts lack complete exact receipts"
    if status != "blocked" and any(
            row["name"] == "sent_ledger" and row["status"] == "partly_corrupt" for row in gaps):
        status, reason = "blocked", "inconclusive: corrupt sent-ledger rows prevent a reliable comparison"

    if mislabeled:
        change = (f"The exact {len(exact_attempts)} attempts were diagnose runs, not verified repairs; "
                  f"{timeouts} have timeout receipts. The repeated repair claim is misleading.")
    elif exact_attempts:
        readable = sum(row["receipt_status"] == "ok" for row in exact_attempts)
        change = (f"Found {len(exact_attempts)} exact attempt records; {readable} receipts readable, "
                  f"{timeouts} timed out. "
                  "Terminal claims are not proof of verified repairs.")
    else:
        change = "The saved checks report stalled world activity; exact repair completion is unverified."
    title, action = {
        "mismeasured": (
            "Pause the misleading repeat alert",
            "Pause this target's repeat escalation pending a scope decision, not unrelated watchers "
            "or the serving gateway. Compare the named launchd job's stderr and lock owner; "
            "repair only a required producer, retire only with explicit owner intent."),
        "broken": (
            "Repair the required producer, not its timestamps",
            "Compare the failing job's stderr, lock owner, and actual producer path before authorizing "
            "a repair. Verify a new genuine action and state merge afterward; never reset timestamps "
            "or manufacture activity."),
        "asleep": (
            "Keep the intentional pause explicit",
            "Keep this target's repeat escalation paused for the declared pause. Confirm the owner "
            "and wake criterion before resuming; do not wake or fabricate activity automatically."),
        "retired": (
            "Retire the obsolete check deliberately",
            "Use the explicit retirement decision to remove only this target's escalation from the "
            "watch plan after approval; preserve its receipts and keep unrelated watchers intact."),
        "inconclusive": (
            "Pause retries until the target's role is clear",
            "Confirm whether this target is still required, then compare the named daemon's stderr "
            "and lock owner with the serving process. Choose repair if required, pause if intentional, "
            "or retire only after an explicit owner decision."),
    }[classification]
    impact = ("Saved checks suggest visitors may see an unchanged world/chat. They do not prove "
              "unrelated data loss, a dead serving gateway, or failed Messages delivery.")
    if not measured_stall:
        impact = ("Public-world freshness is unverified; unreadable measurements do not prove downtime. "
                  "No evidence here establishes unrelated data loss or failed Messages delivery.")
    if status == "blocked":
        title = "Decision needs a current, complete receipt"
        impact = "Current user impact is unverified; incomplete evidence is not an outage or a recovery."
        action = ("Read a current saved verdict and the missing exact escalation receipts before "
                  "authorizing repair, pause, or retirement. Do not rerun repairs to fill the evidence gap.")
    elif status == "suppressed":
        title = "No new decision to deliver"
        action = "Do not resend historical stall alerts; retain the bounded evidence for comparison."
        impact = "Both target checks currently pass; this does not certify every dependency."

    deadline = _stamp(intent.get("resume_at")) if lifecycle == "paused" else None
    semantic = {
        "version": 1, "home": str(home), "target": TARGET, "issue": list(issue),
        "signals": signals, "classification": classification, "decision": decision,
        "intent": lifecycle, "scope_mismatch": scope_mismatch,
        "owner_deadline": deadline.isoformat() if deadline else None,
        "mislabeled_repairs": mislabeled, "runtime_kind": runtime_kind,
        "attempts": [{"mode": row["mode"], "outcome": row["outcome"],
                      "relation": row["relation"], "receipt_status": row["receipt_status"]}
                     for row in attempts],
    }
    envelope = {
        "scenario": "decision", "status": status, "title": title, "change": change,
        "impact": impact, "action": action, "decision": decision, "evidence": evidence,
        "artifacts": [], "fingerprint": "decision:" + _digest(semantic),
        "urgency": "routine", "reason": reason,
    }
    if deadline:
        envelope["deadline"] = intent["resume_at"]
        if deadline <= now + timedelta(hours=24):
            envelope["urgency"] = "time_sensitive"

    prepared_plan = {
        "issue": list(issue), "recommendation": decision,
        "next_step": action, "executed": False,
    }
    brief = None
    if status == "ready":
        brief = _brief(classification, measured_stall, mislabeled,
                       len(exact_attempts), timeouts, deadline)
        envelope.update({key: brief[key] for key in ("action", "decision")})
    elif status == "blocked":
        envelope.update(
            decision="Wait for complete current evidence before changing this escalation?",
            action="Reviewed available receipts; identified evidence gaps. No service changes made.")
    else:
        envelope.update(
            decision="Keep this recovered incident closed?",
            action="Checked both target checks; no new intervention was prepared.")
    details = {key: envelope[key] for key in (
        "title", "change", "impact", "action", "decision", "reason", "evidence")}
    if brief:
        envelope.update(brief)

    artifact = artifact_dir / (envelope["fingerprint"].replace(":", "-") + ".json")
    if artifact in sources.paths.values():
        envelope.update(status="blocked", reason="artifact output overlaps a supplied source")
        return envelope
    envelope["artifacts"] = [str(artifact)]
    receipt = {
        "schema": "storykeeper.decision-evidence/v1",
        "observed_at": context["now"], "bounds": SOURCE_BOUNDS,
        "classification": classification, "recommendation": decision,
        "semantic_identity": semantic, "details": details,
        "prepared_plan": prepared_plan,
        "source_reads": sources.reads, "attempts": attempts,
        "issue_record": {"key": list(issue), "attempts": expected,
                         "last_attempt": last_attempt.isoformat() if last_attempt else None,
                         "claimed_repairs": claimed_repairs, "receipts_complete": complete},
        "runtime_snapshot": runtime,
        "latest_alert": {key: alert[key] for key in ("at", "sent_at", "text", "unverified")
                         if key in alert},
        "caveats": [
            "Read-only saved evidence, not a fresh remote platform probe.",
            "A timeout proves no terminal result was captured, not that no work ran.",
            "Historical NO_ACTION/root-cause narratives are claims, not current diagnoses.",
            "No repair.verified chain or delivery database was read.",
            "No intent is inferred from age or from a disabled port probe.",
            "No source, permission, timestamp, outbox, or workload was changed.",
        ],
        "envelope": envelope,
    }
    pending = None
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = artifact_dir / (".decision-" + uuid.uuid4().hex + ".pending")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(pending, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(pending, artifact)
        pending = None
    except (OSError, ValueError, RecursionError):
        envelope.update(status="blocked", artifacts=[],
                        reason="inconclusive: private evidence artifact could not be persisted")
    finally:
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
    return envelope
