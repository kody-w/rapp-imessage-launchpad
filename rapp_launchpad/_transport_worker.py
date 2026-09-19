"""Isolated adapter to the ONE canonical outbox. Never a second sender."""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapp_launchpad.config import validate_source
from rapp_launchpad.errors import LaunchpadError
from rapp_launchpad.util import parse_time, read_json, strict_json, within

LIMIT = 64 * 1024 * 1024


def _rows(path):
    path = Path(path)
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > LIMIT:
        raise ValueError("unsafe or oversized transport ledger")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("torn transport ledger")
    rows = [strict_json(line) for line in raw.splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid transport ledger")
    return rows


def _recipient(home):
    value = read_json(home / "config.json")
    if not isinstance(value, dict) or value.get("notify") is False:
        raise ValueError("recipient notification is not configured")
    candidate = value.get("notify_handle") or value.get("notify")
    if (
        not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 254
        or any(ord(char) < 32 for char in candidate)
    ):
        raise ValueError("canonical recipient is missing or malformed")
    return candidate


def _mask(recipient):
    if "@" in recipient:
        return recipient[0] + "•••@•••"
    return "•••• " + recipient[-4:]


def _reason(raw):
    value = str(raw or "").lower()
    if "permission" in value or "automation" in value or "-1743" in value:
        return "automation_permission_required"
    if "unverified" in value or "unreadable" in value:
        return "sent_without_delivery_evidence"
    if "timed out" in value or "timeout" in value:
        return "sender_timed_out"
    if "did not record" in value:
        return "no_complete_send_evidence"
    return "canonical_sender_reported_issue" if value else None


def _classify(row, kind):
    base = {
        "entry_id": row.get("entry_id"), "at": row.get("at"),
        "dedupe_key": row.get("dedupe_key"),
    }
    if kind == "queued":
        return dict(base, state="queued", reason="durably accepted by the canonical outbox")
    if kind == "unknown":
        return dict(base, state="unknown", reason="canonical outbox reports an uncertain outcome; do not retry")
    if kind in ("dead_letter", "expired"):
        return dict(base, state="error", reason="canonical outbox terminalized this item as " + kind)
    evidence = row.get("delivery_evidence")
    if (
        kind == "sent" and isinstance(evidence, dict)
        and evidence.get("source") == "Messages/chat.db"
        and type(evidence.get("message_rowid")) is int and evidence["message_rowid"] > 0
        and isinstance(row.get("verified_at"), str)
    ):
        parse_time(row["verified_at"])
        return dict(base, state="delivered", reason="canonical verifier recorded a matching delivered Messages row")
    return dict(
        base, state="sent_unverified",
        reason="canonical sender recorded a send; no matching delivered-message evidence is available",
    )


def _snapshot(outbox, keys):
    found = {}
    counts = {}
    specs = (
        ("queued", outbox.QUEUE), ("sent", outbox.SENT),
        ("sent_unverified", outbox.UNVERIFIED), ("dead_letter", outbox.DEAD_LETTER),
        ("expired", outbox.EXPIRED), ("unknown", outbox.UNKNOWN),
    )
    with outbox._locked(outbox.LOCK):
        for kind, path in specs:
            rows = _rows(path)
            counts[kind] = len(rows)
            for row in rows:
                if row.get("dedupe_key") in keys:
                    found[row["dedupe_key"]] = _classify(row, kind)
        inflight = read_json(outbox.INFLIGHT)
        if inflight:
            raw = inflight.get("raw_line") or inflight.get("queue_line")
            if isinstance(raw, str):
                item = strict_json(raw)
                if item.get("dedupe_key") in keys and found.get(item["dedupe_key"], {}).get("state") in (None, "queued"):
                    found[item["dedupe_key"]] = _classify(item, "unknown")
        quarantine = _rows(outbox.QUARANTINE)
        resolutions = _rows(outbox.QUARANTINE_RESOLVED)
        resolved = {row.get("incident_id") for row in resolutions}
        unresolved = sum(row.get("id") not in resolved for row in quarantine)
        recovery = outbox._strict_recovery_records_unlocked()
        recovery_present = any(not record["acknowledged_at"] for record in recovery)
        last = read_json(outbox.LAST_DRAIN)
    return {
        "counts": counts, "matches": found, "quarantine_records": unresolved,
        "strict_recovery_marker": recovery_present,
        "inflight": bool(inflight),
        "last_drain": {
            "at": last.get("at"), "processed": last.get("sent"), "remaining": last.get("kept"),
            "reason_code": _reason(last.get("why")),
        } if isinstance(last, dict) else None,
    }


def execute(request):
    if request.get("action") not in {"enqueue", "snapshot", "drain"}:
        raise ValueError("unsupported transport action")
    source = Path(request["source"]).resolve()
    home = Path(request["home"]).resolve()
    validate_source(source)
    if not (home / "config.json").is_file():
        raise ValueError("canonical home is not configured")
    os.umask(0o077)
    os.environ["SENTINEL_HOME"] = str(home)
    sys.path.insert(0, str(source))
    with contextlib.redirect_stdout(sys.stderr):
        outbox = importlib.import_module("outbox")
        if Path(outbox.__file__).resolve() != source / "outbox.py" or Path(outbox.HOME).resolve() != home:
            raise ValueError("canonical module or runtime binding mismatch")
        action = request["action"]
        if action == "enqueue":
            text = request["text"]
            key = request["dedupe_key"]
            if not isinstance(text, str) or not 1 <= len(text) <= 650:
                raise ValueError("message exceeds the bounded text limit")
            if not isinstance(key, str) or not key.startswith("launchpad/1/") or len(key) > 240:
                raise ValueError("invalid transport dedupe key")
            attachments = request.get("attachments", [])
            if not isinstance(attachments, list) or len(attachments) > 1:
                raise ValueError("at most one staged artifact bundle is permitted")
            if attachments:
                staging = Path(request["staging_root"]).resolve()
                for raw in attachments:
                    path = Path(raw)
                    if not path.is_absolute() or not within(path, staging) or path.is_symlink() or not path.is_file():
                        raise ValueError("attachment must be a private staged file")
                    if path.suffix != ".zip" or path.stat().st_size > 9 * 1024 * 1024:
                        raise ValueError("artifact bundle exceeds the bounded ZIP limit")
            accepted = outbox.enqueue(text, _recipient(home), attachments=attachments, dedupe_key=key)
            snapshot = _snapshot(outbox, {key})
            return {
                "ok": True, "accepted": bool(accepted),
                "receipt": snapshot["matches"].get(key),
            }
        if action == "drain":
            if request.get("portable") is not True:
                raise ValueError("existing pipeline owns its drainer; Launchpad will not start another")
            limit = request.get("limit", 1)
            if type(limit) is not int or not 1 <= limit <= 3 or sys.platform != "darwin":
                raise ValueError("bounded macOS-only drain required")
            processed, remaining, why = outbox.drain(limit=limit)
            return {
                "ok": True, "processed": processed, "remaining": remaining,
                "reason_code": _reason(why),
                "delivery": "not inferred from the sender exit status",
            }
        keys = request.get("keys", [])
        if not isinstance(keys, list) or len(keys) > 500 or not all(isinstance(k, str) and len(k) <= 240 for k in keys):
            raise ValueError("invalid lookup keys")
        result = _snapshot(outbox, set(keys))
        try:
            recipient = _recipient(home)
            result.update(recipient_configured=True, recipient_masked=_mask(recipient))
        except (ValueError, TypeError, LaunchpadError):
            result.update(recipient_configured=False, recipient_masked=None)
        return dict(result, ok=True)


def main():
    try:
        request = strict_json(sys.stdin.buffer.read(1_048_577))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        result = execute(request)
    except Exception as exc:
        result = {
            "ok": False, "error": type(exc).__name__,
            "message": "canonical outbox operation failed; inspect private runtime diagnostics",
        }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
