"""Conservative built-in gate; integrated interrupt.evaluate is authoritative."""
from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError
from .protocol import ProtocolError
from .util import parse_time

DEFAULT_POLICY = {
    "max_daily": 6,
    "quiet_hours": {"enabled": True, "start": "22:00", "end": "08:00"},
    "timezone": "local",
}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise ConfigurationError("policy requires exactly max_daily, quiet_hours, and timezone")
    if type(value["max_daily"]) is not int or not 1 <= value["max_daily"] <= 10:
        raise ConfigurationError("max_daily must be 1–10 (default 6)")
    quiet = value["quiet_hours"]
    if quiet is False or quiet is None:
        quiet = {"enabled": False, "start": "22:00", "end": "08:00"}
    elif isinstance(quiet, dict) and set(quiet) == {"start", "end"}:
        quiet = dict(quiet, enabled=True)
    if not isinstance(quiet, dict) or set(quiet) != {"enabled", "start", "end"} or type(quiet["enabled"]) is not bool:
        raise ConfigurationError("quiet_hours must be false/null or {start, end}, with optional boolean enabled")
    for field in ("start", "end"):
        raw = quiet[field]
        if not isinstance(raw, str) or len(raw) != 5 or raw[2] != ":":
            raise ConfigurationError("quiet hours must use HH:MM")
        try:
            hour, minute = (int(part) for part in raw.split(":"))
        except ValueError:
            raise ConfigurationError("quiet hours must use HH:MM") from None
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ConfigurationError("invalid quiet-hours time")
    if quiet["enabled"] and quiet["start"] == quiet["end"]:
        raise ConfigurationError("equal quiet-hour endpoints are ambiguous; disable quiet hours explicitly")
    zone = value["timezone"]
    if not isinstance(zone, str) or len(zone) > 128:
        raise ConfigurationError("timezone must be local or an IANA zone name")
    if zone != "local":
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError("timezone is unavailable on this Python installation") from None
    return {
        "max_daily": value["max_daily"], "quiet_hours": dict(quiet), "timezone": zone,
    }


def _local_zone_name():
    candidate = os.environ.get("TZ", "").lstrip(":")
    if not candidate or candidate.startswith("/"):
        parts = Path(candidate or "/etc/localtime").resolve().parts
        candidate = ""
        positions = [index for index, part in enumerate(parts) if part == "zoneinfo"]
        if positions:
            candidate = "/".join(parts[positions[-1] + 1:])
    try:
        if not candidate:
            raise ValueError("no IANA timezone available")
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigurationError(
            "the local IANA timezone could not be resolved; set an explicit policy timezone such as UTC"
        ) from None
    return candidate


def canonical_policy(value):
    """Translate presentation settings, not policy decisions, at the gate boundary."""
    value = validate_policy(value)
    quiet = value["quiet_hours"]
    return {
        "max_daily": value["max_daily"],
        "timezone": _local_zone_name() if value["timezone"] == "local" else value["timezone"],
        "quiet_hours": {"start": quiet["start"], "end": quiet["end"]} if quiet["enabled"] else False,
    }


def evaluate(proposal, history, now, policy=None):
    policy = validate_policy(policy or DEFAULT_POLICY)
    clock = parse_time(now)
    local = clock.astimezone() if policy["timezone"] == "local" else clock.astimezone(ZoneInfo(policy["timezone"]))
    fingerprint = proposal["fingerprint"]

    def verdict(allow, reason):
        return {"allow": allow, "reason": reason, "fingerprint": fingerprint}

    if proposal["status"] != "ready":
        return verdict(False, proposal["reason"] or "scenario is not ready")
    if not proposal["evidence"] or not proposal["action"].strip():
        return verdict(False, "no evidence-backed actionable finding")
    queued = [row for row in history if row.get("decision") == "queued"]
    if any(row.get("fingerprint") == fingerprint and row.get("scenario") == proposal["scenario"] for row in queued):
        return verdict(False, "semantic finding was already queued")
    daily = 0
    for row in queued:
        when = parse_time(row["at"])
        when = when.astimezone() if policy["timezone"] == "local" else when.astimezone(ZoneInfo(policy["timezone"]))
        if when.date() == local.date():
            daily += 1
    if daily >= policy["max_daily"]:
        return verdict(False, "shared daily interruption budget is exhausted")
    quiet = policy["quiet_hours"]
    minute = local.strftime("%H:%M")
    inside = (
        quiet["start"] <= minute < quiet["end"]
        if quiet["start"] < quiet["end"]
        else minute >= quiet["start"] or minute < quiet["end"]
    )
    if quiet["enabled"] and inside:
        return verdict(False, "shared quiet hours are active")
    if proposal.get("deadline") and parse_time(proposal["deadline"]).astimezone(timezone.utc) <= clock.astimezone(timezone.utc):
        return verdict(False, "action deadline has passed")
    try:
        render(proposal)
    except ProtocolError as exc:
        return verdict(False, str(exc))
    return verdict(True, "evidence, dedupe, budget, and quiet-hours checks passed")


def validate_message(text):
    if (
        not isinstance(text, str) or not text.strip() or len(text) > 650
        or not text.isascii() or len(text.split()) > 90
        or any(ord(char) < 32 and char not in "\n\r\t" for char in text)
    ):
        raise ProtocolError(
            "notification must preserve all four answers and attributed evidence within 650 ASCII characters and 90 words; move detail to artifacts"
        )
    return text


def render(proposal):
    """Preserve the complete envelope; never truncate a decision or attribution."""
    def ascii_text(value):
        return value.strip().encode("ascii", "backslashreplace").decode("ascii")

    pieces = [
        ascii_text(proposal["title"]),
        "Change: " + ascii_text(proposal["change"]),
        "Impact: " + ascii_text(proposal["impact"]),
        "Action: " + ascii_text(proposal["action"]),
        "Decision: " + ascii_text(proposal["decision"]),
        "Evidence:",
    ]
    pieces.extend(
        ascii_text(item["source"]) + ": " + ascii_text(item["observation"])
        for item in proposal["evidence"]
    )
    return validate_message("\n".join(pieces))
