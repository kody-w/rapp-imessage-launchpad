"""Recover explicit, current, user-owned promises from opt-in local snapshots.

Only build(context) is public. No source discovery, network, execution, or outbox
access is performed. See docs/scenarios/intentions.md for the input contract.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import uuid


SCHEMA = "intentions/v1"
MAX_FILES = 8
MAX_FILE_BYTES = 524288
MAX_RECORDS = 500
POLICY = {
    "quiet_days": 7,
    "max_idle_days": 90,
    "snapshot_max_age_days": 7,
    "relevance_max_age_days": 30,
}
TERMINAL = {"completed", "done", "cancelled", "canceled", "closed"}
PROMISE = re.compile(
    r"^i (?:will|(?:promise|promised|commit|committed|agree|agreed) to) .+\S$"
    r"|^i'll .+\S$", re.IGNORECASE
)
AMBIGUOUS = re.compile(
    r"\b(?:not|no|never|won't|can't|cannot|don't|didn't|wouldn't|couldn't|"
    r"shouldn't|isn't|wasn't|unable|maybe|perhaps|possibly|probably|might|may|"
    r"should|would|could|if|unless|when|once|until|provided|assuming|depending|"
    r"someday|sometime|eventually|consider|considering|think|thinking|try|trying|"
    r"hope|hoping|wish|planning|cancelled|canceled|withdrawn|rescinded|abandoned)\b"
    r"|\b(?:need|want|plan|intend) to\b",
    re.IGNORECASE,
)
RESOLVED_CONTEXT = re.compile(
    r"\balready (?:done|completed|finished|sent|delivered)\b"
    r"|\b(?:this|the) (?:promise|task|commitment|work) "
    r"(?:is|was|has been) (?:done|completed|finished|cancelled|canceled|closed)\b"
    r"|\b(?:i|we) (?:(?:have|had) )?(?:already )?"
    r"(?:cancelled|canceled|completed|finished|withdrawn|rescinded)\b"
    r"|\b(?:this|it|that) (?:is|was|has been) "
    r"(?:done|completed|finished|cancelled|canceled|closed)\b"
    r"|\b(?:promise|task|commitment) (?:cancelled|canceled|completed)\b"
    r"|\b(?:status|resolution):\s*(?:done|complete|completed|cancelled|canceled|closed|resolved)\b"
    r"|(?:^|[.;(])\s*(?:done|completed|finished|cancelled|canceled)\s*(?:[.!;)]|$)"
    r"|\b(?:i|we) (?:will not|won't|am not going to|are not going to)\b"
    r"|\bno longer (?:needed|required|relevant|my responsibility)\b",
    re.IGNORECASE,
)


class InvalidInput(ValueError):
    pass


def _text(value, field, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidInput(f"{field} must be nonempty text, at most {limit} characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise InvalidInput(f"{field} contains control characters")
    return value.strip()


def _shape(value, required, optional=(), field="record"):
    if not isinstance(value, dict):
        raise InvalidInput(f"{field} must be an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise InvalidInput(f"{field} has missing or unsupported fields")
    return value


def _choice(value, choices, field):
    if not isinstance(value, str) or value not in choices:
        raise InvalidInput(f"{field} has an unsupported value")
    return value


def _date(value, field):
    value = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidInput(f"{field} must be a timezone-aware ISO8601 timestamp") from None


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _normal(value):
    return " ".join(value.replace("\u2019", "'").split())


def _markdown(value):
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(value, quote=False))


def _has_symlink(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            return True
    return False


def _canonical(value):
    if isinstance(value, str):
        return _normal(value)
    if isinstance(value, dict):
        return {key: _canonical(part) for key, part in value.items()}
    if isinstance(value, list):
        return [_canonical(part) for part in value]
    return value


def _digest(value):
    raw = json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("JSON contains duplicate keys")
        result[key] = value
    return result


def _invalid_number(_value):
    raise InvalidInput("JSON contains a non-finite number")


def _read_source(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise InvalidInput("source must be a regular file of at most 524288 bytes")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise InvalidInput("source must be a regular file of at most 524288 bytes")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise InvalidInput("source exceeds 524288 bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys,
                          parse_constant=_invalid_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise InvalidInput("source must be bounded UTF-8 JSON") from None


def _source_path(raw, home):
    raw = _text(raw, "source path", 4096)
    if "://" in raw or raw.startswith("~") or "\n" in raw or "\r" in raw:
        raise InvalidInput("source paths must be explicit local paths")
    path = Path(raw)
    if not path.is_absolute():
        if ".." in path.parts:
            raise InvalidInput("relative source paths must remain under home")
        path = home / path
    path = Path(os.path.abspath(path))
    if _has_symlink(path):
        raise InvalidInput("source symlinks and filesystem reparse points are not supported")
    return path


def _config(context):
    sources = context.get("sources")
    if not isinstance(sources, dict):
        raise InvalidInput("context.sources must be an object")
    config = sources.get("intentions")
    if config is None:
        raise InvalidInput("configure sources.intentions with owner_id and explicit files")
    _shape(config, {"owner_id", "files"}, POLICY, "sources.intentions")
    owner = _text(config["owner_id"], "owner_id", 200)
    files = config["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise InvalidInput("sources.intentions.files must list 1 to 8 explicit JSON files")
    policy = dict(POLICY)
    for key, default in POLICY.items():
        value = config.get(key, default)
        if type(value) is not int or not 1 <= value <= 365:
            raise InvalidInput(f"{key} must be an integer from 1 to 365")
        policy[key] = value
    if policy["quiet_days"] > policy["max_idle_days"]:
        raise InvalidInput("quiet_days must not exceed max_idle_days")
    return owner, files, policy


def _record(raw, as_of):
    required = {
        "id", "kind", "owner_id", "status", "title", "created_at", "updated_at",
        "original", "consequence", "relevance", "next_step",
    }
    _shape(raw, required, {"deadline"})
    item = {key: _text(raw[key], key, 200) for key in ("id", "owner_id", "title")}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/#-]*", item["id"]):
        raise InvalidInput("id must be a canonical identifier without whitespace")
    item["kind"] = _choice(raw["kind"], {"promise", "idea", "suggestion", "repair"}, "kind")
    item["status"] = _choice(raw["status"], TERMINAL | {"open", "in_progress"}, "status")
    created = _date(raw["created_at"], "created_at")
    updated = _date(raw["updated_at"], "updated_at")
    if not created <= updated <= as_of:
        raise InvalidInput("created_at <= updated_at <= as_of is required")
    item.update(created_at=_iso(created), updated_at=_iso(updated))
    original = _shape(raw["original"], {"kind", "author_id", "ref", "quote", "context"},
                      field="original")
    item["original"] = {
        "kind": _choice(original["kind"], {"note", "issue", "unfinished_work", "alert", "repair"},
                        "original.kind"),
        "author_id": _text(original["author_id"], "original.author_id", 200),
        "ref": _text(original["ref"], "original.ref", 2000),
        "quote": _text(original["quote"], "original.quote", 2000),
        "context": _text(original["context"], "original.context", 8000),
    }
    consequence = _shape(raw["consequence"], {"level", "reason"}, field="consequence")
    item["consequence"] = {
        "level": _choice(consequence["level"], {"low", "medium", "high"}, "consequence.level"),
        "reason": _text(consequence["reason"], "consequence.reason"),
    }
    relevance = _shape(raw["relevance"], {"state", "checked_at", "reason"}, field="relevance")
    checked = _date(relevance["checked_at"], "relevance.checked_at")
    if not created <= checked <= as_of:
        raise InvalidInput("created_at <= relevance.checked_at <= as_of is required")
    item["relevance"] = {
        "state": _choice(relevance["state"], {"current", "stale", "unknown"}, "relevance.state"),
        "checked_at": _iso(checked),
        "reason": _text(relevance["reason"], "relevance.reason"),
    }
    step = _shape(raw["next_step"], {"format", "title", "action", "points"}, field="next_step")
    points = step["points"]
    if not isinstance(points, list) or not 1 <= len(points) <= 10:
        raise InvalidInput("next_step.points must contain 1 to 10 concrete points")
    item["next_step"] = {
        "format": _choice(step["format"], {"outline", "message", "checklist"}, "next_step.format"),
        "title": _text(step["title"], "next_step.title", 200),
        "action": _text(step["action"], "next_step.action", 500),
        "points": [_text(point, "next_step.points", 500) for point in points],
    }
    if raw.get("deadline") is not None:
        deadline = _date(raw["deadline"], "deadline")
        if deadline < created:
            raise InvalidInput("deadline must not precede created_at")
        item["deadline"] = _iso(deadline)
    return item


def _exclusion(item, owner, now, policy):
    if item["status"] in TERMINAL:
        return "completed_or_cancelled"
    if item["owner_id"] != owner or item["original"]["author_id"] != owner:
        return "not_owned_and_authored_by_user"
    if item["kind"] != "promise":
        return "not_a_promise"
    if item["original"]["kind"] not in {"note", "issue", "unfinished_work"}:
        return "operational_advice_not_a_promise"
    quote = _normal(item["original"]["quote"])
    if not PROMISE.fullmatch(quote) or AMBIGUOUS.search(quote) or "?" in quote:
        return "not_an_explicit_affirmative_promise"
    surrounding = " ".join((quote, item["original"]["context"], item["relevance"]["reason"]))
    if RESOLVED_CONTEXT.search(_normal(surrounding)):
        return "resolved_in_original_context"
    idle = now - _date(item["updated_at"], "updated_at")
    if idle < timedelta(days=policy["quiet_days"]):
        return "recent_activity"
    if idle > timedelta(days=policy["max_idle_days"]):
        return "stale_activity"
    relevance = item["relevance"]
    if relevance["state"] != "current":
        return "not_current"
    checked = _date(relevance["checked_at"], "relevance.checked_at")
    if now - checked > timedelta(days=policy["relevance_max_age_days"]):
        return "stale_relevance"
    deadline = _date(item["deadline"], "deadline") if "deadline" in item else None
    if deadline and now - deadline > timedelta(days=7) and checked <= deadline:
        return "expired_without_reconfirmation"
    return None


def _grounded(item):
    context = _normal(item["original"]["quote"] + " " + item["original"]["context"]).casefold()
    step = item["next_step"]
    for text in [step["action"], *step["points"]]:
        if _normal(text).casefold() not in context:
            raise InvalidInput("next_step.action and points must occur in original quote/context")


def _rank(item, now):
    consequence = {"high": 30, "medium": 20, "low": 10}[item["consequence"]["level"]]
    age = now - _date(item["relevance"]["checked_at"], "relevance.checked_at")
    relevance = 6 if age <= timedelta(days=7) else 4 if age <= timedelta(days=14) else 2
    deadline = _date(item["deadline"], "deadline") if "deadline" in item else None
    until = deadline - now if deadline else None
    deadline_score = 0
    if until is not None:
        deadline_score = (9 if until <= timedelta(days=1) else
                          6 if until <= timedelta(days=7) else
                          3 if until <= timedelta(days=30) else 0)
    urgency = "routine"
    if until is not None and timedelta(0) <= until <= timedelta(hours=24):
        urgency = ("urgent" if consequence == 30 and until <= timedelta(hours=2)
                   else "time_sensitive")
    return {
        "score": consequence + relevance + deadline_score,
        "score_components": {"consequence": consequence, "relevance": relevance,
                             "deadline": deadline_score},
        "urgency": urgency,
    }


def _semantic(item):
    return {
        key: item[key] for key in
        ("id", "owner_id", "title", "consequence", "next_step", "deadline") if key in item
    } | {"promise": item["original"]["quote"], "relevance": item["relevance"]["reason"]}


def _notice_text(value, fallback, max_chars, max_words):
    value = _normal(value)
    if value.isascii() and len(value) <= max_chars and len(value.split()) <= max_words:
        return value
    return fallback


def _envelope(status, reason, evidence, ranked=()):
    result = {
        "scenario": "intentions",
        "status": status,
        "title": "Dropped intentions review",
        "change": "No recoverable promise was established.",
        "impact": "No commitment or completion is inferred from missing evidence.",
        "action": "Review the private report and the documented intentions/v1 input contract.",
        "decision": "Do not deliver an intention reminder.",
        "evidence": evidence,
        "artifacts": [],
        "urgency": "routine",
        "reason": reason,
    }
    semantics = {"scenario": "intentions", "version": 1, "status": status}
    if ranked:
        best = ranked[0]
        observation = "Explicit user promise; open; current."
        if "deadline" in best:
            observation += f" Recorded deadline: {best['deadline']}."
        else:
            observation += f" Relevance checked: {best['relevance']['checked_at'][:10]}."
        result.update(
            title=_notice_text(f"Recover: {best['title']}", "Recover a recorded promise", 52, 8),
            change="An owned promise is recorded open and quiet.",
            impact=_notice_text(
                best["consequence"]["reason"],
                f"Recorded {best['consequence']['level']} consequence; see the cited review.",
                90, 14,
            ),
            action=_notice_text(
                f"Draft ready: {best['next_step']['action']}",
                f"Prepared draft ({best['next_step']['format']}); see the review.",
                85, 14,
            ),
            decision="Review the draft; resume, revise, or dismiss.",
            evidence=[{"source": "artifacts[0]#/ranked/0", "observation": observation}],
            urgency=best["urgency"],
            reason="Highest-ranked current, quiet promise.",
        )
        if "deadline" in best:
            result["deadline"] = best["deadline"]
        semantics.update(ranked=[_semantic(item) for item in ranked])
    else:
        semantics["reason"] = reason
    result["fingerprint"] = "intentions:" + _digest(semantics)
    return result


def _draft(best):
    step = best["next_step"]
    lines = [
        f"# {_markdown(step['title'])}", "",
        "PRIVATE DRAFT — review required; not sent, enqueued, or executed.", "",
        f"## Next action\n\n{_markdown(step['action'])} [1]", "",
    ]
    if step["format"] == "message":
        lines += [
            "## Prepared message", "",
            f"Following up on my promise: {_markdown(best['original']['quote'])} [1]", "",
            f"Proposed next step: {_markdown(step['action'])} [1]", "",
            *[f"- {_markdown(point)} [1]" for point in step["points"]],
            "", "Draft for review; no new completion or delivery date is asserted.", "",
        ]
    elif step["format"] == "outline":
        lines += ["## Working outline", ""]
        for point in step["points"]:
            lines += [f"### {_markdown(point)} [1]", "", "- [ ] Add verified material for this section.", ""]
    else:
        lines += ["## Working checklist", "",
                  *[f"- [ ] {_markdown(point)} [1]" for point in step["points"]], ""]
    lines += [
        "## Why this comes first", "",
        f"Consequence: {_markdown(best['consequence']['reason'])}",
        f"Current relevance: {_markdown(best['relevance']['reason'])}",
        f"Last recorded activity: {best['updated_at']}.",
    ]
    if "deadline" in best:
        lines += [f"Recorded deadline (not a new promise): {best['deadline']}."]
    lines += [
        "", "## Original context [1]", "",
        f"Reference: {_markdown(best['original']['ref'])}", "",
        *[f"> {_markdown(line)}" for line in best["original"]["quote"].splitlines()], "",
        *[f"> {_markdown(line)}" for line in best["original"]["context"].splitlines()], "",
        "Local snapshot citations:", "",
        *[f"- {_markdown(citation)}" for citation in best["citations"]], "",
        "Check the original context and current status before using this draft.", "",
    ]
    return "\n".join(lines)


def _write_private(root, name, content):
    target = root / name
    staging = root / f".{name}.{uuid.uuid4().hex}.partial"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(staging, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    return str(target)


def _artifacts(context, envelope, ranked, audit):
    try:
        raw = _text(context.get("artifact_dir"), "context.artifact_dir", 4096)
        root = Path(raw)
        if not root.is_absolute() or _has_symlink(root):
            raise InvalidInput("artifact_dir must be an absolute, non-symlink private directory")
        resolved = root.resolve()
        home = Path(context["home"]).resolve()
        if resolved == home or resolved in home.parents:
            raise InvalidInput("artifact_dir must be separate from home and its ancestors")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
        suffix = envelope["fingerprint"].split(":")[1][:24]
        report_path = root / f"intentions-review-{suffix}.json"
        paths = [str(report_path)]
        if ranked:
            paths.append(_write_private(root, f"intentions-next-step-{suffix}.md", _draft(ranked[0])))
        envelope["artifacts"] = paths
        report = {"result": envelope, "ranked": ranked, **audit}
        _write_private(root, report_path.name, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return envelope
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return _envelope(
            "blocked", "Private artifact output is unavailable; no draft is ready for delivery.",
            audit.get("source_evidence", envelope["evidence"]) + [
                {"source": "context.artifact_dir",
                 "observation": "Use a writable private directory separate from home and its ancestors."},
            ],
        )


def build(context):
    """Return a delivery-neutral scenario envelope; write only private artifacts."""
    if not isinstance(context, dict):
        return _envelope("blocked", "A context object with home, artifact_dir, now and sources is required.", [])
    try:
        now = _date(context.get("now"), "context.now")
        home = Path(_text(context.get("home"), "context.home", 4096))
        if not home.is_absolute() or not home.is_dir():
            raise InvalidInput("context.home must be an existing absolute directory")
    except (InvalidInput, OSError, ValueError):
        return _envelope("blocked", "Valid home and timezone-aware now inputs are required.", [
            {"source": "context", "observation": "No sources were inspected."},
        ])

    evidence = []
    audit = {"schema": "intentions-review/v1", "evaluated_at": _iso(now),
             "reviewed_records": 0, "excluded": {}, "policy": dict(POLICY)}
    try:
        owner, files, policy = _config(context)
        audit["policy"] = policy
        paths = sorted({_source_path(raw, home) for raw in files}, key=str)
        grouped = defaultdict(list)
        for path in paths:
            source = str(path)
            try:
                snapshot = _shape(_read_source(path), {"schema", "as_of", "items"}, field="snapshot")
                if snapshot["schema"] != SCHEMA:
                    raise InvalidInput("snapshot.schema must be intentions/v1")
                as_of = _date(snapshot["as_of"], "snapshot.as_of")
                if not timedelta(0) <= now - as_of <= timedelta(days=policy["snapshot_max_age_days"]):
                    raise InvalidInput("snapshot is stale or future-dated; reconfirm status before export")
                records = snapshot["items"]
                if not isinstance(records, list) or audit["reviewed_records"] + len(records) > MAX_RECORDS:
                    raise InvalidInput("at most 500 records across all sources are supported")
                for index, raw in enumerate(records):
                    item = _record(raw, as_of)
                    grouped[item["id"]].append((item, f"{source}#/items/{index}"))
                audit["reviewed_records"] += len(records)
                evidence.append({"source": source, "observation": (
                    f"Validated {len(records)} records in an authorized snapshot as of {_iso(as_of)}."
                )})
            except (OSError, InvalidInput, ValueError, RecursionError) as error:
                detail = str(error) if isinstance(error, InvalidInput) else type(error).__name__
                evidence.append({"source": source, "observation": f"Unreliable source: {detail}."})
                raise InvalidInput("All configured sources must be readable, current intentions/v1 snapshots.") from None

        excluded = Counter()
        candidates = []
        for copies in grouped.values():
            if any(item["status"] in TERMINAL for item, _ in copies):
                excluded["completed_or_cancelled"] += 1
                continue
            # A conflicting copy may be an ownership change or a newer resolution.
            if len({_digest(item) for item, _ in copies}) != 1:
                excluded["conflicting_copies"] += 1
                continue
            item = copies[0][0]
            rejection = _exclusion(item, owner, now, policy)
            if rejection:
                excluded[rejection] += 1
                continue
            _grounded(item)
            candidates.append({**item, **_rank(item, now),
                               "citations": sorted(citation for _, citation in copies)})
        ranked = sorted(candidates, key=lambda item: (
            -item["score"], -item["score_components"]["consequence"],
            "deadline" not in item,
            _date(item["deadline"], "deadline") if "deadline" in item
            else datetime.max.replace(tzinfo=timezone.utc),
            item["id"],
        ))[:3]
        for index, item in enumerate(ranked, 1):
            item["rank"] = index
            evidence.append({"source": item["citations"][0], "observation": (
                f"Rank {index}; score {item['score']}; explicit promise: "
                f"{item['original']['quote']} Status: {item['status']}. "
                f"Last recorded activity: {item['updated_at']}. "
                f"Consequence: {item['consequence']['reason']} "
                f"Current relevance: {item['relevance']['reason']}"
            )})
        audit.update(excluded=dict(sorted(excluded.items())), eligible_records=len(candidates),
                     source_evidence=evidence)
        if ranked:
            result = _envelope(
                "ready", "Current, explicitly user-owned promises are quiet; ranked by consequence and relevance.",
                evidence, ranked,
            )
        else:
            evidence.append({"source": "validated snapshots", "observation": (
                "No eligible dropped promise. Exclusions: " + json.dumps(audit["excluded"], sort_keys=True)
            )})
            result = _envelope("suppressed", "No current, quiet, explicit user promise qualifies.", evidence)
        return _artifacts(context, result, ranked, audit)
    except (InvalidInput, OSError, ValueError, RuntimeError) as error:
        detail = str(error) if isinstance(error, InvalidInput) else type(error).__name__
        evidence.append({"source": "sources.intentions", "observation": detail})
        result = _envelope(
            "blocked", "Reliable explicit-promise inputs are missing or invalid; no intention is inferred.",
            evidence,
        )
        return _artifacts(context, result, [], audit)
