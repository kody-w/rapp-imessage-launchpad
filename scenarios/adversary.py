"""Falsify success-as-delivery without importing or invoking live transport."""

import ast
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


HYPOTHESIS = (
    "For ten scenario producers sharing the outbox, a successful handoff or "
    "sent entry is sufficient evidence of this message's verified delivery."
)
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_LEDGER_LINES = 2000
MAX_CODE_BYTES = 1024 * 1024
SOURCE_NAMES = {
    "sent": "outbox-sent.jsonl",
    "unverified": "outbox-unverified.jsonl",
    "unknown": "outbox-unknown.jsonl",
    "unknown_resolved": "outbox-unknown-resolved.jsonl",
}
PRODUCTION_CODE = Path(__file__).resolve().parents[1] / "outbox.py"
if not PRODUCTION_CODE.is_file():
    PRODUCTION_CODE = Path(__file__).resolve().parents[1] / "rapp_launchpad" / "_vendor" / "canonical" / "outbox.py"
FIXTURE_TEXT = "adversary synthetic message"
FIXTURE_TO = "adversary-fixture.invalid"
CASES = (
    ("delivered_control", True, "delivered"),
    ("no_receipt_control", True, "absent"),
    ("sent_not_delivered", True, "sent_only"),
    ("unrelated_delivery", True, "unrelated"),
    ("unreadable_dropped", False, "absent"),
    ("unreadable_delivered", False, "delivered"),
)
ACTION = (
    "Have the shared runner distinguish accepted, attempted, verified and unknown. "
    "Require a unique recipient/content/time-matched is_delivered=1, error=0 "
    "receipt before claiming delivery. Keep uncertain attempts terminal; "
    "reconcile them read-only, never blindly resend. This producer changes no transport."
)
USER_NEEDS = {
    "refuted": "No decision needed; unverified delivery must stay unknown.",
    "survived": "No decision needed; these trials do not prove real delivery.",
    "inconclusive": "No decision requested; the evidence is insufficient to judge delivery.",
}
LIMITATIONS = [
    "Fixture counterexamples do not estimate the live non-delivery rate.",
    "Local JSONL records are recorded claims, not an independent query of Messages "
    "or proof that a recipient read a message.",
    "Missing, unverified and unknown evidence does not prove non-delivery and "
    "must not trigger a duplicate send.",
    "The experiment is text-only; attachments, concurrent writers, delayed "
    "delivery and crash recovery are outside its scope.",
    "The probe executes this checkout's implementation; the active worker's "
    "code version is not independently observed.",
    "Ten producers implying ten useful messages is untested: these receipts "
    "contain no user-value labels.",
]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("now must be a timezone-aware ISO8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(verdict):
    semantic = {"scenario": "adversary", "hypothesis": HYPOTHESIS,
                "verdict": verdict, "recommendation": "separate-delivery-proof-v1"}
    return "adversary:" + _sha(json.dumps(semantic, sort_keys=True).encode())


def _reject_constant(value):
    raise ValueError("non-finite JSON value")


def _read_ledger(path):
    provenance = {"path": str(path), "status": "missing", "complete": False,
                  "records": 0, "invalid_lines": 0}
    records = []
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            provenance["status"] = "not_regular"
            return provenance, records
        # Nonblocking open also prevents a misconfigured FIFO from hanging a build.
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_BINARY", 0))
        fd = os.open(str(path), flags)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                provenance["status"] = "not_regular"
                return provenance, records
            data = handle.read(MAX_LEDGER_BYTES + 1)
            after = os.fstat(handle.fileno())
    except FileNotFoundError:
        return provenance, records
    except OSError as exc:
        provenance.update(status="unreadable", error=type(exc).__name__)
        return provenance, records

    limited = len(data) > MAX_LEDGER_BYTES
    data = data[:MAX_LEDGER_BYTES]
    changed = (before.st_size, before.st_mtime_ns) != (
        after.st_size, after.st_mtime_ns)
    lines = data.splitlines()
    if limited and data and not data.endswith(b"\n"):
        lines = lines[:-1]
    provenance.update(
        sha256=_sha(data), sha256_scope="bounded_prefix" if limited else "snapshot",
        bytes_read=len(data), file_bytes=before.st_size,
        changed_during_read=changed,
    )
    limited = limited or len(lines) > MAX_LEDGER_LINES
    for number, raw in enumerate(lines[:MAX_LEDGER_LINES], 1):
        try:
            record = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(record, dict):
                raise ValueError("record is not an object")
        except (ValueError, UnicodeError, RecursionError):
            provenance["invalid_lines"] += 1
            continue
        records.append((number, _sha(raw), record))
    provenance["records"] = len(records)
    provenance["complete"] = not (
        limited or changed or provenance["invalid_lines"])
    provenance["status"] = (
        "partial" if limited else "changed" if changed
        else "invalid" if provenance["invalid_lines"] else "ok")
    return provenance, records


def _hex_id(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _recorded_verification(row):
    proof = row.get("delivery_evidence")
    if not isinstance(proof, dict):
        return False
    rowid, delta = proof.get("message_rowid"), proof.get("delta_seconds")
    try:
        _stamp(row.get("verified_at"))
    except (ValueError, TypeError, OverflowError):
        return False
    return (
        proof.get("source") == "Messages/chat.db"
        and type(rowid) is int and rowid > 0
        and type(delta) in (float, int) and 0 <= delta <= 15
        and math.isfinite(delta)
    )


def _receipt_summary(sources):
    summary = {"usable_receipts": 0}
    for role in ("sent", "unverified"):
        provenance, records = sources[role]
        counts = {"receipt_rows": 0, "recorded_verified": 0,
                  "explicitly_unverified": 0, "without_recorded_proof": 0,
                  "unmarked_without_proof": 0, "invalid_receipt_shapes": 0}
        samples = []
        sampled = set()
        for line, digest, row in records:
            if (not isinstance(row.get("to"), str)
                    or not isinstance(row.get("text"), str)
                    or not any(isinstance(row.get(k), str) for k in (
                        "at", "sent_at", "attempted_at"))):
                counts["invalid_receipt_shapes"] += 1
                continue
            counts["receipt_rows"] += 1
            if _recorded_verification(row):
                category = "recorded_verified"
            elif role == "unverified" or row.get("unverified"):
                category = "explicitly_unverified"
                counts["without_recorded_proof"] += 1
            else:
                category = "unmarked_without_proof"
                counts["without_recorded_proof"] += 1
            counts[category] += 1
            if category not in sampled:
                samples.append({"line": line, "sha256": digest, "category": category})
                sampled.add(category)
        summary[role] = counts
        summary["usable_receipts"] += counts["receipt_rows"]
        provenance["samples"] = samples

    resolutions = {}
    invalid_resolutions = 0
    for _, _, row in sources["unknown_resolved"][1]:
        if (row.get("schema") != "rapp-outbox-unknown-resolution/1.0"
                or not _hex_id(row.get("unknown_id"))
                or row.get("resolution") not in ("delivered", "not-delivered")
                or not isinstance(row.get("resolved_at"), str)
                or not isinstance(row.get("reason"), str)):
            invalid_resolutions += 1
            continue
        resolutions.setdefault(row["unknown_id"], set()).add(row["resolution"])
    unknown = {"receipt_rows": 0, "with_recorded_resolution": 0,
               "without_matching_resolution_evidence": 0,
               "invalid_receipt_shapes": 0,
               "invalid_resolution_shapes": invalid_resolutions}
    occurrences = {}
    for _, digest, row in sources["unknown"][1]:
        identity = row.get("unknown_id")
        if (not isinstance(row.get("reason"), str)
                or (identity is not None and not _hex_id(identity))
                or (identity is None and not _hex_id(row.get("queue_sha256")))):
            unknown["invalid_receipt_shapes"] += 1
            continue
        if identity is None:
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            identity = _sha(f"legacy-unknown:{digest}:{occurrence}".encode("ascii"))
        unknown["receipt_rows"] += 1
        key = ("with_recorded_resolution" if len(resolutions.get(identity, ())) == 1
               else "without_matching_resolution_evidence")
        unknown[key] += 1
    summary["unknown"] = unknown
    summary["usable_receipts"] += unknown["receipt_rows"]
    return summary


def _load_probe(source_path=None):
    source_path = Path(source_path) if source_path is not None else PRODUCTION_CODE
    with source_path.open("rb") as handle:
        source = handle.read(MAX_CODE_BYTES + 1)
    if len(source) > MAX_CODE_BYTES:
        raise ValueError("production source exceeds probe limit")
    tree = ast.parse(source, filename=str(source_path))
    names = ("_delivered_count", "_send")
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names) or {node.name for node in nodes} != set(names):
        raise ValueError("production probe functions unavailable")
    allowed_attributes = {
        "exists", "connect", "execute", "close", "fetchone", "name", "is_file",
        "run", "strip", "TimeoutExpired", "__name__", "returncode", "stderr",
        "values", "items", "sleep", "join",
    }
    for node in nodes:
        if node.decorator_list or any(
            isinstance(child, (ast.Import, ast.ImportFrom))
            or (isinstance(child, ast.Attribute) and child.attr not in allowed_attributes)
            for child in ast.walk(node)
        ):
            raise ValueError("production probe no longer fits isolated harness")
    code = compile(ast.Module(body=nodes, type_ignores=[]),
                   str(source_path), "exec")
    provenance = {
        "path": str(source_path), "sha256": _sha(source),
        "functions": [{"name": node.name, "first_line": node.lineno,
                       "last_line": node.end_lineno} for node in nodes],
        "mode": "AST-extracted production functions, not an import of outbox",
    }
    return code, provenance


class _Fixture:
    """Only SELECTs see this in-memory database; 'osascript' is an inert method."""

    def __init__(self, readable, receipt):
        self.readable, self.receipt = readable, receipt
        self.calls = self.queries = self.sleeps = 0
        self.returncodes = []
        self.virtual_seconds = 0.0
        self.violation = False
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE handle (id TEXT);
            INSERT INTO handle (id) VALUES ('adversary-fixture.invalid');
            CREATE TABLE message (
                handle_id INTEGER, text TEXT, is_from_me INTEGER,
                is_sent INTEGER, is_delivered INTEGER, error INTEGER);
        """)

    def __str__(self):
        return "adversary-memory-only"

    def exists(self):
        return True

    def attachment_path(self, *args, **kwargs):
        self.violation = True
        raise ValueError("attachments are outside the text-only harness")

    def connect(self, database, uri=False, timeout=5):
        if database != "file:adversary-memory-only?mode=ro" or not uri:
            self.violation = True
            raise ValueError("only the in-memory fixture may be queried")
        if not self.readable:
            raise sqlite3.OperationalError("fixture ledger deliberately unreadable")
        return self

    def execute(self, query, parameters=()):
        self.queries += 1
        if self.queries > 64 or not query.lstrip().upper().startswith("SELECT"):
            self.violation = True
            raise ValueError("fixture read budget exceeded or write attempted")
        return self.db.execute(query, parameters)

    def close(self):
        # Production closes its read handle; the harness owns the fixture lifetime.
        pass

    def dispatch(self, argv, **kwargs):
        self.calls += 1
        if self.calls != 1 or argv != ["osascript", "-", FIXTURE_TEXT, FIXTURE_TO]:
            self.violation = True
            raise ValueError("only one synthetic text dispatch is allowed")
        if self.receipt != "absent":
            text = "different synthetic message" if self.receipt == "unrelated" else FIXTURE_TEXT
            delivered = int(self.receipt != "sent_only")
            self.db.execute("INSERT INTO message VALUES (1, ?, 1, 1, ?, 0)",
                            (text, delivered))
        self.returncodes.append(0)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def sleep(self, seconds):
        self.sleeps += 1
        self.virtual_seconds += seconds
        if self.sleeps > 40 or self.virtual_seconds > 10.001:
            self.violation = True
            raise ValueError("virtual polling budget exceeded")

    def matching_delivered(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM message m JOIN handle h ON m.handle_id=h.ROWID "
            "WHERE h.id=? AND m.text=? AND m.is_from_me=1 AND m.is_sent=1 "
            "AND m.is_delivered=1 AND m.error=0", (FIXTURE_TO, FIXTURE_TEXT)
        ).fetchone()[0]


def _assess(cases):
    by_name = {case["name"]: case for case in cases}
    positive = by_name.get("delivered_control", {})
    negative = by_name.get("no_receipt_control", {})
    controls = {
        "delivered": positive.get("returned_success") is True
        and positive.get("matching_delivered_rows") == 1 and not positive.get("warning"),
        "no_receipt": negative.get("returned_success") is False
        and negative.get("matching_delivered_rows") == 0,
    }
    counterexamples = [case["name"] for case in cases
                       if case.get("returned_success") is True
                       and case.get("matching_delivered_rows") == 0]
    valid = (set(by_name) == {case[0] for case in CASES}
             and all(controls.values())
             and all(not case.get("error") and case.get("mock_dispatches") == 1
                     and case.get("mock_returncodes") == [0]
                     for case in cases))
    verdict = ("refuted" if counterexamples else "survived") if valid else "inconclusive"
    return verdict, controls, counterexamples


def _run_experiment(source_path=None):
    start = time.monotonic()
    result = {"verdict": "inconclusive", "cases": [], "real_transport_calls": 0,
              "bounds": {"cases": len(CASES), "dispatches_per_case": 1,
                         "selects_per_case": 64, "virtual_sleeps_per_case": 40}}
    try:
        code, result["production_source"] = _load_probe(source_path)
    except (OSError, ValueError, SyntaxError, TypeError) as exc:
        result["error"] = type(exc).__name__
        return result
    for name, readable, receipt in CASES:
        fixture = _Fixture(readable, receipt)
        case = {"name": name, "ledger_readable": readable, "injected_receipt": receipt}
        namespace = {
            "__builtins__": {
                "Exception": Exception, "int": int, "str": str, "type": type,
                "any": any, "all": all, "range": range,
            },
            "Path": fixture.attachment_path, "CHAT_DB": fixture,
            "sqlite3": fixture, "time": fixture,
            "subprocess": SimpleNamespace(run=fixture.dispatch, TimeoutExpired=TimeoutError),
            "APPLESCRIPT": "disabled: this value is never interpreted",
            "SEND_TIMEOUT": 25,
        }
        try:
            exec(code, namespace)
            case["production_count_before"] = namespace["_delivered_count"](FIXTURE_TO)
            ok, warning = namespace["_send"](FIXTURE_TEXT, FIXTURE_TO)
            if type(ok) is not bool or not isinstance(warning, str):
                raise ValueError("production result no longer fits harness")
            case.update(returned_success=ok, warning=warning,
                        production_count_after=namespace["_delivered_count"](FIXTURE_TO),
                        matching_delivered_rows=fixture.matching_delivered())
        except Exception as exc:
            case["error"] = type(exc).__name__
        finally:
            case.update(mock_dispatches=fixture.calls, select_calls=fixture.queries,
                        mock_returncodes=fixture.returncodes,
                        virtual_sleeps=fixture.sleeps,
                        virtual_seconds=round(fixture.virtual_seconds, 3))
            if fixture.violation:
                case["error"] = "HarnessBoundaryViolation"
            fixture.db.close()
        result["cases"].append(case)
    verdict, controls, counterexamples = _assess(result["cases"])
    result.update(
        verdict=verdict, controls=controls, counterexamples=counterexamples,
        mock_transport_calls=sum(case["mock_dispatches"] for case in result["cases"]),
        elapsed_ms=round((time.monotonic() - start) * 1000, 3),
    )
    return result


def _write_report(directory, report):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "adversary-experiment.json"
    staging = directory / (".adversary-" + uuid.uuid4().hex + ".json")
    try:
        fd = os.open(str(staging), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return str(path)


def _notification_evidence(receipts, sources, experiment):
    trials = len(experiment["cases"])
    counterexamples = len(experiment.get("counterexamples", []))
    controls = sum(experiment.get("controls", {}).values())
    evidence = [{
        "source": "mock outbox",
        "observation": (
            f"{counterexamples}/{trials} counterexamples; controls {controls}/2; "
            "0 real sends." if trials else "Probe unavailable; see private report."),
    }]
    priorities = [
        ("sent", receipts["sent"]["explicitly_unverified"]),
        ("unverified", receipts["unverified"]["without_recorded_proof"]),
        ("unknown", receipts["unknown"]["without_matching_resolution_evidence"]),
    ]
    priorities += [(role, receipts[role]["receipt_rows"])
                   for role in ("sent", "unverified", "unknown")]
    role = next((role for role, count in priorities if count), None)
    if role is None:
        evidence.append({"source": "local ledgers",
                         "observation": "No usable receipts; see private report."})
        return evidence
    counts = receipts[role]
    label = "Sample" if sources[role][0]["complete"] else "Incomplete sample"
    if role == "unknown":
        observation = (
            f"{label}: unresolved outcomes "
            f"{'present' if counts['without_matching_resolution_evidence'] else 'absent'}; "
            f"recorded resolutions {'present' if counts['with_recorded_resolution'] else 'absent'}.")
    else:
        observation = (
            f"{label}: unverified outcomes "
            f"{'present' if counts['explicitly_unverified'] else 'absent'}; "
            f"recorded verification {'present' if counts['recorded_verified'] else 'absent'}.")
    evidence.append({"source": role + " ledger", "observation": observation})
    return evidence


def _base(verdict, reason):
    return {
        "scenario": "adversary",
        "status": {"refuted": "ready", "survived": "suppressed",
                   "inconclusive": "blocked"}[verdict],
        "title": "Adversary: delivery proof",
        "change": "",
        "impact": "All 10 producers can overclaim delivery; real outcomes remain unknown.",
        "action": "No new experiment report saved; delivery handling was not changed.",
        "decision": USER_NEEDS[verdict],
        "evidence": [],
        "artifacts": [],
        "fingerprint": _fingerprint(verdict),
        "urgency": "routine",
        "reason": reason,
    }


def build(context: dict) -> dict:
    """Use adapter-supplied paths; read receipts and persist a private mock experiment."""
    try:
        stamp = _stamp(context["now"])
        if any(not isinstance(context[key], (str, os.PathLike)) or not str(context[key])
               for key in ("home", "artifact_dir")):
            raise ValueError("home and artifact_dir must be nonempty paths")
        home = Path(context["home"]).expanduser().resolve()
        directory = Path(context["artifact_dir"]).expanduser().resolve()
        source_config = context.get("sources", {})
        if not isinstance(source_config, dict):
            raise ValueError("sources must be an object")
        overrides = source_config.get("adversary", {})
        if not isinstance(overrides, dict) or set(overrides) - set(SOURCE_NAMES):
            raise ValueError("invalid sources.adversary keys")
        paths = {}
        for role, name in SOURCE_NAMES.items():
            raw = overrides.get(role, str(Path("state") / name))
            if not isinstance(raw, (str, os.PathLike)) or not str(raw):
                raise ValueError("receipt sources must be nonempty local paths")
            path = Path(raw).expanduser()
            paths[role] = (path if path.is_absolute() else home / path).resolve()
        if (directory / "adversary-experiment.json") in paths.values():
            raise ValueError("artifact would overwrite a receipt source")
    except (KeyError, ValueError, TypeError, OSError, OverflowError, RuntimeError) as exc:
        return _base("inconclusive", f"Invalid adversary context ({type(exc).__name__}); "
                     "no live transport or state was touched.")

    sources = {role: _read_ledger(path) for role, path in paths.items()}
    receipts = _receipt_summary(sources)
    code_root = context.get("transport_source")
    experiment = _run_experiment(Path(code_root) / "outbox.py" if code_root else None)
    verdict = experiment["verdict"] if receipts["usable_receipts"] else "inconclusive"
    count = len(experiment.get("counterexamples", []))
    trials = len(experiment["cases"])
    if not receipts["usable_receipts"]:
        reason = ("No usable local receipt evidence; applicability to the current "
                  f"initiative is inconclusive. Mock-only verdict: {experiment['verdict']}.")
    elif verdict == "refuted":
        reason = "Mocks refute success-as-proof; real delivery is unknown."
    elif verdict == "survived":
        reason = ("The assumption survived these bounded fixtures only, not a proof "
                  "of real delivery or message usefulness.")
    else:
        reason = "The production probe or its controls failed; no delivery conclusion is justified."
    envelope = _base(verdict, reason)
    envelope["change"] = (
        f"{count}/{trials} mock trials returned success without delivery proof."
        if trials else "The production probe could not run."
    )
    envelope["evidence"] = _notification_evidence(receipts, sources, experiment)
    report = {
        "schema": "storykeeper-adversary/1", "generated_at": stamp,
        "hypothesis": HYPOTHESIS, "verdict": verdict, "reason": reason,
        "selection": "Delivery confidence is a shared dependency of all ten producers. "
                     "The sent/unverified/unknown receipt evidence can test this "
                     "assumption; it cannot measure whether ten messages are useful.",
        "receipts": receipts,
        "sources": {role: provenance for role, (provenance, _) in sources.items()},
        "notification_source_map": {
            "mock outbox": "experiment.production_source",
            "sent ledger": "sources.sent",
            "unverified ledger": "sources.unverified",
            "unknown ledger": "sources.unknown",
            "local ledgers": "sources",
        },
        "experiment": experiment, "recommended_change": ACTION,
        "limitations": LIMITATIONS,
        "fingerprint": envelope["fingerprint"],
    }
    try:
        artifact = _write_report(directory, report)
    except (OSError, ValueError) as exc:
        envelope.update(status="blocked", decision=USER_NEEDS["inconclusive"],
                        fingerprint=_fingerprint("inconclusive"),
                        reason=f"Cannot persist experiment evidence ({type(exc).__name__}); "
                               "no live transport or state was touched.")
        return envelope
    envelope["artifacts"] = [artifact]
    envelope["action"] = (
        f"Ran {trials} mock trials; saved evidence. "
        "Recommend reconciling unknown sends before retry."
        if trials else "Saved the probe limitation; no transport change was deployed."
    )
    return envelope
