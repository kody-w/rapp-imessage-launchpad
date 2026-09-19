"""Read-only, bounded parallel-universe experiment for alert identity policies.

No production modules are imported: importing the transport, cooldown, or paths
modules can couple an experiment to live state. The two workers only receive
in-memory replay inputs, never source paths or expected answers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit
import uuid


PROTOCOL = "parallel-alert-identity-v1"
POLICIES = ("exact_text", "semantic_condition")
WINDOW_SECONDS = 6 * 60 * 60
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 2048
MAX_TEXT_CHARS = 8192
MAX_CASES = 160
MAX_EVENTS_PER_CASE = 4
MAX_WORKER_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
WALL_SECONDS = 3.0
CPU_SECONDS = 2
MIN_RECORDS = 6
MIN_REPEAT_PAIRS = 2
ACCEPTANCE = {
    "minimum_duplicate_recall": 0.8,
    "maximum_false_suppressions": 0,
    "complete_identical_corpus": True,
}
CHECK_NAME = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*:")
REPORT_SEPARATOR = "\n\nStatic HTML report:\n"

# These are label certificates for visible production clauses, not the policy
# under test. Every other byte/number is conservatively treated as a change.
AGE_FIELDS = (
    re.compile(r"(\brv_world_merging: last merge )(\d+(?:\.\d+)?)(h ago\b)"),
    re.compile(r"(\brv_meaningful_activity: chat stale )(\d+(?:\.\d+)?)(h\b)"),
    re.compile(r"(\bagent state stale )(\d+(?:\.\d+)?)(h\b)"),
    re.compile(r"(\brv_validation: gate has not run in )(\d+(?:\.\d+)?)(h - stopped, not rejecting\b)"),
    re.compile(r"(\brv_pr_queue: oldest PR #\d+ has waited )(\d+(?:\.\d+)?)(h\b)"),
    re.compile(r"(\blast WORKED )(\d+(?:\.\d+)?)(m ago \(bar \d+m\))"),
)


class _Blocked(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _nonfinite_json(_value: str):
    raise ValueError("non-finite JSON constants are not valid evidence")


def _stamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("an ISO8601 timestamp is required")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.timestamp()


def _body(text: str) -> str:
    body, separator, tail = text.rpartition(REPORT_SEPARATOR)
    if separator and body and tail.strip():
        urls = tail.strip().splitlines()
        try:
            parsed = [urlsplit(url) for url in urls]
            if all(url.scheme in ("http", "https") and url.netloc
                   and re.fullmatch(r"/share/[A-Za-z0-9_-]+\.html", url.path)
                   and not url.query and not url.fragment for url in parsed):
                return body
        except ValueError:
            pass
    return text


def _certificate(body: str) -> tuple[str, tuple[float, ...]]:
    ages = []
    for pattern in AGE_FIELDS:
        def replace(match):
            amount = float(match[2])
            if not math.isfinite(amount):
                return match[0]
            ages.append(amount)
            return match[1] + "<elapsed>" + match[3]
        body = pattern.sub(replace, body)
    return body, tuple(ages)


def _same_visible_condition(left: dict, right: dict) -> bool:
    a, ages_a = _certificate(left["body"])
    b, ages_b = _certificate(right["body"])
    return (
        a == b and len(ages_a) == len(ages_b)
        and all(new >= old for old, new in zip(ages_a, ages_b))
        and all(left[key] == right[key] for key in ("route", "pipeline", "attachments"))
    )


def _read_source(path: Path) -> tuple[list[dict], str]:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise _Blocked("invalid_input", "The alert source must be a regular JSONL file.")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _Blocked("missing_input", "Alert history is absent; no experiment was invented.") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Blocked("invalid_input", "The alert source must be a regular JSONL file.")
        if info.st_size > MAX_SOURCE_BYTES:
            raise _Blocked("source_budget", "Alert history exceeds the fixed input byte budget.")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_SOURCE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_SOURCE_BYTES:
        raise _Blocked("source_budget", "Alert history grew beyond the input byte budget.")
    try:
        lines = raw.decode("utf-8").splitlines()
        if sum(bool(line.strip()) for line in lines) > MAX_RECORDS:
            raise _Blocked("record_budget", "Alert history exceeds the fixed record budget.")
        records = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            row = json.loads(line, parse_constant=_nonfinite_json)
            if not isinstance(row, dict):
                raise ValueError("record is not an object")
            text, recipient = row["text"], row["to"]
            if (not isinstance(text, str) or not text.strip()
                    or len(text) > MAX_TEXT_CHARS or not isinstance(recipient, str)
                    or not recipient.strip() or len(recipient) > 512):
                raise ValueError("invalid text or recipient")
            attachments = row.get("attachments", [])
            if (not isinstance(attachments, list) or len(attachments) > 8
                    or any(not isinstance(item, str) or len(item) > 1024 for item in attachments)):
                raise ValueError("invalid attachments")
            body = _body(text)
            family = row.get("pipeline", body.splitlines()[0])
            if not isinstance(family, str) or not family.strip() or len(family) > 1024:
                raise ValueError("invalid pipeline")
            records.append({
                "line": number, "body": body, "route": _hash(recipient),
                "pipeline": _hash(family), "attachments": _hash(attachments),
                "at": _stamp(row.get("sent_at") or row["at"]),
            })
        return records, hashlib.sha256(raw).hexdigest()
    except _Blocked:
        raise
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise _Blocked("corrupt_input", "Alert history is corrupt or has an unsupported record schema.") from exc


def _sample(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    return [items[index * len(items) // limit] for index in range(limit)]


def _event(record: dict, at: float) -> dict:
    return {key: record[key] for key in ("body", "route", "pipeline", "attachments")} | {"at": at}


def _corpus(records: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    families = {}
    for record in records:
        families.setdefault((record["pipeline"], record["route"]), []).append(record)
    if not families:
        return [], {"records": 0, "selected_records": 0, "repeat_pairs": 0}, []
    selected_key = min(families, key=lambda key: (-len(families[key]), key))
    selected = sorted(families[selected_key], key=lambda record: (record["at"], record["line"]))
    stats = {
        "records": len(records), "families": len(families),
        "selected_records": len(selected), "pipeline_id": selected_key[0],
        "repeat_pairs": 0, "within_window_repeat_pairs": 0,
        "outside_window_repeat_pairs": 0, "observed_change_pairs": 0,
    }
    repeated, changed, timeline, previous = [], [], [], {}
    for record in selected:
        key = (_certificate(record["body"])[0], record["attachments"])
        prior = previous.get(key)
        if prior and _same_visible_condition(prior, record):
            repeated.append((prior, record))
            gap = record["at"] - prior["at"]
            stats["within_window_repeat_pairs" if gap < WINDOW_SECONDS
                  else "outside_window_repeat_pairs"] += 1
        previous[key] = record
    for left, right in zip(selected, selected[1:]):
        if not _same_visible_condition(left, right):
            changed.append((left, right))
        timeline.append((left, right))
    stats["repeat_pairs"] = len(repeated)
    stats["observed_change_pairs"] = len(changed)
    if len(selected) < MIN_RECORDS or len(repeated) < MIN_REPEAT_PAIRS:
        return [], stats, selected
    cases = []

    def add(kind, partition, events, expected, rows, **details):
        cases.append({
            "id": f"case-{len(cases) + 1:03d}", "kind": kind, "partition": partition,
            "events": events, "expected": expected,
            "source_lines": [row["line"] for row in rows], **details,
        })

    for left, right in _sample(repeated, 48):
        add("observed_repeat", "observed", [_event(left, 0), _event(right, 60)],
            [True, False], [left, right],
            mode="counterfactual_same_window", original_gap_seconds=right["at"] - left["at"])
    for left, right in _sample(changed, 48):
        add("observed_change", "observed", [_event(left, 0), _event(right, 60)],
            [True, True], [left, right], mode="counterfactual_same_window")
    for left, right in _sample(timeline, 24):
        gap = right["at"] - left["at"]
        expected = gap >= WINDOW_SECONDS or not _same_visible_condition(left, right)
        add("original_timing", "observed", [_event(left, 0), _event(right, gap)],
            [True, expected], [left, right], mode="original_interval")

    # These frozen mutations are held out from the observed-pair certificates.
    # The worker receives neither these kinds nor their expected answers.
    numeric_guards = (
        ("exit_code", re.compile(r"\blast exit (\d+)\b")),
        ("delivery_count", re.compile(r"\b(\d+) UNKNOWN delivery\b")),
        ("pr_identity", re.compile(r"\bPR #(\d+)\b")),
        ("commit_distance", re.compile(r"\b(\d+) commit\(s\) behind\b")),
        ("threshold", re.compile(r"\bbar (\d+)m\b")),
    )
    exercised = []
    for kind, pattern in numeric_guards:
        for seed in selected:
            match = pattern.search(seed["body"])
            if match:
                body = seed["body"][:match.start(1)] + str(int(match[1]) + 1) + seed["body"][match.end(1):]
                other = {**seed, "body": body}
                add(kind, "held_out", [_event(seed, 0), _event(other, 60)],
                    [True, True], [seed], mode="source_derived_mutation")
                exercised.append(kind)
                break
    for seed in selected:
        if "stopped, not rejecting" in seed["body"]:
            other = {**seed, "body": seed["body"].replace(
                "stopped, not rejecting", "running, rejecting work", 1)}
            add("predicate_change", "held_out", [_event(seed, 0), _event(other, 60)],
                [True, True], [seed], mode="source_derived_mutation")
            exercised.append("predicate_change")
            break
    for seed in selected:
        match = AGE_FIELDS[0].search(seed["body"])
        if match and float(match[2]) > 0:
            other = {**seed, "body": seed["body"][:match.start(2)] + "0" + seed["body"][match.end(2):]}
            add("age_reset", "held_out", [_event(seed, 0), _event(other, 60)],
                [True, True], [seed], mode="source_derived_mutation")
            exercised.append("age_reset")
            break

    seed = selected[-1]
    start = _event(seed, 0)
    add("exact_repeat", "held_out", [start, _event(seed, 60)],
        [True, False], [seed], mode="source_derived_control")
    for field in ("route", "pipeline", "attachments"):
        other = {**_event(seed, 60), field: _hash(["different", seed[field]])}
        add(f"changed_{field}", "held_out", [start, other], [True, True], [seed],
            mode="source_derived_mutation")
    other = {**seed, "body": "⚠️ Changed severity\n" + seed["body"]}
    add("severity_change", "held_out", [start, _event(other, 60)], [True, True],
        [seed], mode="source_derived_mutation")
    other = {**seed, "body": seed["body"] + "; parallel_new_check: a new failure"}
    add("new_check", "held_out", [start, _event(other, 60)], [True, True],
        [seed], mode="source_derived_mutation")
    other = {**seed, "body": seed["body"] + "; condition changed: recovered"}
    add("condition_recurrence", "held_out",
        [start, _event(other, 60), _event(seed, 120)], [True, True, True], [seed],
        mode="source_derived_mutation")
    add("cooldown_boundary", "held_out", [start, _event(seed, WINDOW_SECONDS)],
        [True, True], [seed], mode="controlled_timing")
    add("non_sliding_cooldown", "held_out",
        [start, _event(seed, WINDOW_SECONDS - 1), _event(seed, WINDOW_SECONDS)],
        [True, False, True], [seed], mode="controlled_timing")
    add("clock_reversal", "held_out", [_event(seed, 60), _event(seed, 0)],
        [True, True], [seed], mode="controlled_timing")
    stats["numeric_guards_exercised"] = exercised
    stats["numeric_guards_unavailable"] = [name for name, _ in numeric_guards if name not in exercised]
    stats["cases"] = len(cases)
    stats["case_sampling"] = {"repeat_cap": 48, "change_cap": 48, "original_timing_cap": 24}
    if len(cases) > MAX_CASES:
        raise _Blocked("case_budget", "The replay corpus exceeds the shared case budget.")
    return cases, stats, selected


def _policy_key(policy: str, event: dict) -> object:
    body = event["body"]
    if policy == "exact_text":
        identity = body
    elif policy == "semantic_condition":
        checks = tuple(sorted(set(CHECK_NAME.findall(body))))
        identity = (body.splitlines()[0], checks) if checks else body
    else:
        raise ValueError("unknown policy")
    return event["attachments"], identity


def _predict(policy: str, cases: list[dict]) -> list[dict]:
    predictions = []
    for case in cases:
        state, decisions = {}, []
        for event in case["events"]:
            scope = (event["route"], event["pipeline"])
            key, at = _policy_key(policy, event), event["at"]
            prior = state.get(scope)
            emit = (prior is None or prior["key"] != key or at < prior["seen_at"]
                    or at - prior["sent_at"] >= WINDOW_SECONDS)
            state[scope] = {"key": key, "seen_at": at,
                            "sent_at": at if emit else prior["sent_at"]}
            decisions.append(emit)
        predictions.append({"id": case["id"], "emit": decisions})
    return predictions


def _worker() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--worker" or sys.argv[2] not in POLICIES:
        return 2
    cpu_limit = None
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        cpu_limit = CPU_SECONDS
    except (ImportError, ValueError, OSError):
        pass
    try:
        data = sys.stdin.buffer.read(MAX_WORKER_BYTES + 1)
        if len(data) > MAX_WORKER_BYTES:
            return 2
        payload = json.loads(data, parse_constant=_nonfinite_json)
        cases = payload["cases"]
        if payload["protocol"] != PROTOCOL or not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
            return 2
        for case in cases:
            if (not isinstance(case["id"], str) or len(case["id"]) > 100
                    or not 1 <= len(case["events"]) <= MAX_EVENTS_PER_CASE):
                return 2
            for event in case["events"]:
                if (not isinstance(event["body"], str) or not event["body"]
                        or len(event["body"]) > MAX_TEXT_CHARS + 256
                        or any(not isinstance(event[key], str) or len(event[key]) > 128
                               for key in ("route", "pipeline", "attachments"))
                        or isinstance(event["at"], bool)
                        or not isinstance(event["at"], (int, float))
                        or not math.isfinite(event["at"])):
                    return 2
        result = {"protocol": PROTOCOL, "policy": sys.argv[2],
                  "predictions": _predict(sys.argv[2], cases), "cpu_limit_seconds": cpu_limit}
        encoded = _json(result)
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return 2
        print(encoded)
        return 0
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return 2


def _write(path: Path, value: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _worker_environment(folder: Path) -> dict[str, str]:
    environment = {"HOME": str(folder), "USERPROFILE": str(folder), "PATH": os.defpath}
    # Windows Python may need these for system DLLs, but not the caller's tokens
    # or personal home. All experiment data still comes through stdin/context.
    for key in ("SystemRoot", "WINDIR"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _run_candidate(policy: str, payload: str, cases: list[dict], folder: Path) -> dict:
    folder.mkdir(mode=0o700)
    _write(folder / "protocol.json", {
        "protocol": PROTOCOL, "policy": policy, "input_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "cases": len(cases), "acceptance": ACCEPTANCE, "wall_seconds": WALL_SECONDS,
        "cpu_seconds": CPU_SECONDS, "cooldown_seconds": WINDOW_SECONDS,
        "max_output_bytes": MAX_OUTPUT_BYTES,
    })
    started = time.monotonic()
    result = {"policy": policy, "status": "failed"}
    try:
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", policy],
            input=payload, text=True, encoding="utf-8", capture_output=True,
            cwd=folder, timeout=WALL_SECONDS, start_new_session=True,
            env=_worker_environment(folder),
        )
        if process.returncode:
            result["status"] = "worker_failed"
            result["returncode"] = process.returncode
        elif len(process.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            result["status"] = "output_budget"
        else:
            answer = json.loads(process.stdout, parse_constant=_nonfinite_json)
            predictions = answer["predictions"]
            valid = (answer["protocol"] == PROTOCOL and answer["policy"] == policy
                     and isinstance(predictions, list) and len(predictions) == len(cases))
            if valid:
                for case, prediction in zip(cases, predictions):
                    if (prediction["id"] != case["id"] or not isinstance(prediction["emit"], list)
                            or len(prediction["emit"]) != len(case["events"])
                            or any(type(item) is not bool for item in prediction["emit"])):
                        valid = False
                        break
            if not valid:
                raise ValueError("worker did not process the identical corpus")
            result.update(status="ok", predictions=predictions,
                          cpu_limit_seconds=answer.get("cpu_limit_seconds"))
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        result["status"] = "invalid_output"
    except OSError:
        result["status"] = "launch_failed"
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    _write(folder / "result.json", result)
    return result


def _score(cases: list[dict], result: dict) -> dict:
    if result["status"] != "ok":
        return {"accepted": False, "status": result["status"], "partitions": {}}
    partitions = {}
    failures = {}
    for case, prediction in zip(cases, result["predictions"]):
        counts = partitions.setdefault(case["partition"], {
            "events": 0, "required_emissions": 0, "duplicate_targets": 0,
            "false_suppressions": 0, "suppressed_duplicates": 0,
        })
        for expected, emit in zip(case["expected"], prediction["emit"]):
            counts["events"] += 1
            counts["required_emissions"] += expected
            counts["duplicate_targets"] += not expected
            counts["false_suppressions"] += expected and not emit
            counts["suppressed_duplicates"] += not expected and not emit
            if expected and not emit:
                failures[case["kind"]] = failures.get(case["kind"], 0) + 1
    totals = {key: sum(counts[key] for counts in partitions.values())
              for key in next(iter(partitions.values()))}
    recall = totals["suppressed_duplicates"] / totals["duplicate_targets"] if totals["duplicate_targets"] else 0
    return {
        **totals, "duplicate_recall": round(recall, 6), "status": "ok",
        "accepted": (totals["false_suppressions"] == ACCEPTANCE["maximum_false_suppressions"]
                     and recall >= ACCEPTANCE["minimum_duplicate_recall"]),
        "false_suppression_kinds": failures, "partitions": partitions,
    }


def _adjudicate(scores: dict) -> str:
    accepted = [name for name, score in scores.items() if score["accepted"]]
    if not accepted:
        return "neither"
    best = max(scores[name]["suppressed_duplicates"] for name in accepted)
    winners = [name for name in accepted if scores[name]["suppressed_duplicates"] == best]
    return winners[0] if len(winners) == 1 else "tie"


def _envelope(status: str, reason: str, decision: str, evidence: list[dict],
              artifacts: list[str], semantic: object, change: str = "", impact: str = "") -> dict:
    return {
        "scenario": "parallel", "status": status, "title": "Parallel universe: alert dedupe policy",
        "change": change or "No live policy was changed.",
        "impact": impact or "No notification, publication, merge, or remediation was attempted.",
        "action": ("Keep live policy unchanged; review private receipts."
                   if status == "ready" else "Supply a valid bounded alert history before comparing policies."),
        "decision": decision, "evidence": evidence, "artifacts": artifacts,
        "fingerprint": "parallel:" + _hash({"protocol": PROTOCOL, "decision": semantic}),
        "urgency": "routine", "reason": reason,
    }


def build(context: dict) -> dict:
    """Build a transport-neutral envelope; all optional receipts stay in artifact_dir.

    sources.parallel_alerts overrides sources.outbox_sent, then the default
    home/state/outbox-sent.jsonl. Overrides are explicit read-only JSONL paths.
    """
    source = None
    try:
        if not isinstance(context, dict):
            raise ValueError("context must be a dictionary")
        _stamp(context["now"])
        home = Path(context["home"]).expanduser().resolve()
        root = Path(context["artifact_dir"]).expanduser().resolve()
        if root == home or home in root.parents:
            raise _Blocked("unsafe_artifacts", "Artifacts must be outside the read-only input home.")
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError("sources must be a dictionary")
        source = Path(sources.get("parallel_alerts", sources.get(
            "outbox_sent", home / "state" / "outbox-sent.jsonl"))).expanduser().resolve()
        if source == root or root in source.parents:
            raise _Blocked("unsafe_artifacts", "An input source cannot be inside the artifact directory.")
        records, source_hash = _read_source(source)
        cases, stats, selected = _corpus(records)
        evidence = [{
            "source": "parallel_alerts",
            "observation": (f"{stats['selected_records']} same-pipeline records; "
                            f"{stats['repeat_pairs']} certified repeat pairs."),
        }]
        if not cases:
            return _envelope(
                "suppressed", "insufficient_repetition",
                "No evidenced choice: at least six same-pipeline records and two repeat pairs are required.",
                evidence, [], {"state": "insufficient_repetition"})
        payload = _json({"protocol": PROTOCOL, "cases": [
            {"id": case["id"], "events": case["events"]} for case in cases
        ]})
        if len(payload.encode("utf-8")) > MAX_WORKER_BYTES:
            raise _Blocked("worker_input_budget", "The identical worker payload exceeds its byte budget.")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run = root / ("parallel-" + uuid.uuid4().hex)
        run.mkdir(mode=0o700)
        manifest = [{
            key: value for key, value in case.items() if key != "events"
        } | {
            "condition_hashes": [_hash([_certificate(event["body"])[0],
                                       event["route"], event["pipeline"], event["attachments"]])
                                 for event in case["events"]],
        } for case in cases]
        _write(run / "cases.json", manifest)
        results = {policy: _run_candidate(policy, payload, cases, run / policy)
                   for policy in POLICIES}
        scores = {policy: _score(cases, result) for policy, result in results.items()}
        complete = all(result["status"] == "ok" for result in results.values())
        outcome = _adjudicate(scores) if complete else "incomplete"
        report = {
            "protocol": PROTOCOL, "observed_at": context["now"], "source": str(source),
            "source_sha256": source_hash, "input_stats": stats, "acceptance": ACCEPTANCE,
            "budget": {"source_bytes": MAX_SOURCE_BYTES, "records": MAX_RECORDS,
                       "cases": MAX_CASES, "worker_input_bytes": MAX_WORKER_BYTES,
                       "worker_output_bytes": MAX_OUTPUT_BYTES, "wall_seconds_per_policy": WALL_SECONDS,
                       "cpu_seconds_per_policy": CPU_SECONDS, "cooldown_seconds": WINDOW_SECONDS},
            "outcome": outcome, "scores": scores,
            "elapsed_seconds": {policy: result["elapsed_seconds"] for policy, result in results.items()},
            "limitations": [
                "Identity probes compress real message pairs into one cooldown window; they are counterfactuals, not proof that actual reminders were duplicates.",
                "Original-interval probes preserve reminder timing. Sent-ledger presence is not independent proof of delivery.",
                "Labels certify visible text only. Upstream summaries may already be truncated; omitted diagnostics cannot be recovered.",
                "Condition-set identity preserves header, route, pipeline and attachment references, but can hide changes within the same checks.",
                "Held-out mutations are source-derived safety challenges, not claimed historical incidents.",
                "Sequential subprocesses share identical inputs and budgets in separate working directories; this is not an OS security sandbox or a latency benchmark.",
            ],
        }
        _write(run / "report.json", report)
        artifacts = [str(run / name) for name in ("report.json", "cases.json")]
        for policy in POLICIES:
            artifacts.extend(str(run / policy / name) for name in ("protocol.json", "result.json"))
        evidence.append({
            "source": "replay receipts",
            "observation": (f"{len(cases)} cases; {stats['outside_window_repeat_pairs']} source repeat "
                            "pairs outside the 6h cooldown."),
        })
        labels = {"exact_text": "Exact", "semantic_condition": "Check-set"}
        impact = "Replay: " + "; ".join(
            f"{labels[policy]}: {score['suppressed_duplicates']}/{score['duplicate_targets']} repeats suppressed, "
            f"{score['false_suppressions']} unsafe"
            if score["status"] == "ok" else f"{labels[policy]}: {score['status']}"
            for policy, score in scores.items()
        )
        decision = {
            "neither": "Neither passes the experiment's safety/noise criteria.",
            "tie": "Tie: both pass equally; no automatic selection.",
            "incomplete": "No comparison: a bounded worker did not complete.",
        }.get(outcome, f"{labels.get(outcome, outcome)} wins this replay only; deployment needs separate approval.")
        semantic = {
            "outcome": outcome, "acceptance": ACCEPTANCE,
            "conditions": sorted({_hash([_certificate(row["body"])[0], row["pipeline"],
                                        row["route"], row["attachments"]]) for row in selected}),
            "policy_results": {policy: {
                "status": score["status"], "accepted": score["accepted"],
                "failure_kinds": sorted(score.get("false_suppression_kinds", {})),
            } for policy, score in scores.items()},
        }
        return _envelope(
            "ready" if complete else "blocked", "measured_" + outcome, decision,
            evidence, artifacts, semantic,
            "Compared exact-text and check-set dedupe.", impact)
    except _Blocked as exc:
        return _envelope("blocked", exc.code, str(exc), [], [], {"blocked": exc.code})
    except (ValueError, KeyError, TypeError, OverflowError):
        return _envelope("blocked", "invalid_context", "A valid context with explicit paths and an aware ISO8601 now is required.",
                         [], [], {"blocked": "invalid_context"})
    except OSError:
        evidence = ([{"source": str(source), "observation": "Input or artifact access failed; no live action was attempted."}]
                    if source else [])
        return _envelope("blocked", "io_failure", "The read-only input or local receipts could not be accessed safely.",
                         evidence, [], {"blocked": "io_failure"})


if __name__ == "__main__":
    raise SystemExit(_worker())
