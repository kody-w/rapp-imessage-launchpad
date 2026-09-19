from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import LaunchpadError
from .util import parse_time, within

PROTOCOL_VERSION = "1.0"
PROPOSAL_SCHEMA = "rapp-imessage-launchpad/proposal/1.0"
RECEIPT_SCHEMA = "rapp-imessage-launchpad/receipt/1.0"
SCENARIOS = (
    "future", "decision", "connections", "parallel", "meeting",
    "intentions", "win", "adversary", "timeline", "interrupt",
)
SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
RECEIPT_STATES = frozenset({
    "dry_run", "intent", "queued", "sent_unverified", "unknown",
    "delivered", "user_confirmed", "suppressed", "error",
})
REQUIRED = frozenset({
    "scenario", "status", "title", "change", "impact", "action", "decision",
    "evidence", "artifacts", "fingerprint", "urgency", "reason",
})


class ProtocolError(LaunchpadError):
    code = "protocol_error"


def _text(value, field, maximum=4096, nonempty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ProtocolError(f"{field} must be a string of at most {maximum} characters")
    if nonempty and not value.strip():
        raise ProtocolError(f"{field} must not be empty")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ProtocolError(f"{field} contains an unpaired surrogate")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise ProtocolError(f"{field} contains a control character")
    return value


def validate_proposal(value: dict, *, artifact_dir: Path | None = None) -> dict:
    """Return a detached, normalized 1.0 proposal; reject ambiguous inputs."""
    if not isinstance(value, dict) or not REQUIRED <= value.keys():
        raise ProtocolError("proposal is missing required 1.0 fields")
    if set(value) - REQUIRED - {"schema", "deadline", "assessments"}:
        raise ProtocolError("proposal contains unsupported 1.0 fields")
    if "assessments" in value and (
        value.get("scenario") != "interrupt" or not isinstance(value["assessments"], list)
        or len(value["assessments"]) > 50
        or not all(isinstance(item, dict) for item in value["assessments"])
    ):
        raise ProtocolError("assessments is reserved for bounded private interruption assessments")
    if value.get("schema", PROPOSAL_SCHEMA) != PROPOSAL_SCHEMA:
        raise ProtocolError("unsupported proposal schema/version")
    if not isinstance(value["scenario"], str) or not SLUG.fullmatch(value["scenario"]):
        raise ProtocolError("scenario must be a lower-case slug")
    if value["status"] not in ("ready", "suppressed", "blocked"):
        raise ProtocolError("status must be ready, suppressed, or blocked")
    if value["urgency"] not in ("routine", "time_sensitive", "urgent"):
        raise ProtocolError("unsupported urgency")
    ready = value["status"] == "ready"
    for field in ("title", "change", "impact", "action", "decision", "reason"):
        _text(value[field], field, 4096, nonempty=ready or field == "reason")
    _text(value["fingerprint"], "fingerprint", 512, nonempty=True)
    if len(value["title"]) > 200:
        raise ProtocolError("title exceeds 200 characters")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 64 or (ready and not evidence):
        raise ProtocolError("ready proposals require 1–64 evidence records")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"source", "observation"}:
            raise ProtocolError("evidence requires exactly source and observation")
        _text(item["source"], "evidence.source", nonempty=True)
        _text(item["observation"], "evidence.observation", nonempty=True)
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 32:
        raise ProtocolError("artifacts must contain at most 32 paths")
    for raw in artifacts:
        _text(raw, "artifact", nonempty=True)
        if artifact_dir is not None:
            path = Path(raw)
            if not path.is_absolute() or not within(path, artifact_dir):
                raise ProtocolError("plugin artifacts must stay inside its private artifact directory")
            if not path.is_file() or path.is_symlink():
                raise ProtocolError("artifact must be an existing, non-symlink file")
    if "deadline" in value:
        try:
            parse_time(value["deadline"])
        except (ValueError, TypeError):
            raise ProtocolError("deadline must include an ISO 8601 timezone") from None
    result = dict(value, schema=PROPOSAL_SCHEMA)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 65_536:
        raise ProtocolError("proposal exceeds 64 KiB")
    return json.loads(encoded)
