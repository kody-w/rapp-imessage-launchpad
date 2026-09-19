"""Evidence-first interruption decisions for every scenario and protocol adapter.

Inputs are caller-supplied JSON values. Source and artifact identifiers remain
opaque: this module never resolves a home directory or opens device-local data.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from rapp_launchpad._vendor.rapp import H
except ModuleNotFoundError as error:
    if error.name != "rapp_launchpad":
        raise
    from rapp import H


SCENARIOS = frozenset({
    "future", "decision", "connections", "parallel", "meeting",
    "intentions", "win", "adversary", "timeline",
})
MAX_CHARS = 650
MAX_WORDS = 90
MAX_FINGERPRINT_CHARS = 512
_ANSWERS = ("change", "impact", "action", "decision")
_EMPTY = frozenset({
    "", "-", "?", "n/a", "na", "none", "null", "unknown", "unverified",
    "tbd", "todo", "pending", "not available", "needs attention",
    "important", "urgent", "something changed",
})
_ISO = (
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d{1,6})?)?(?:[Zz]|[+-]\d{2}:\d{2})"
)
_STAMP = re.compile(_ISO)
_CLOCK = re.compile(
    r"\b(?:as of|checked at|observed at|evaluated at|updated at|sampled at|"
    r"generated at|"
    r"current time|now(?: is)?)\s*[:=]?\s*" + _ISO, re.I,
)
_DURATION = (
    r"\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|"
    r"hours?|hrs?|h|days?|d|weeks?|w)"
)
_AGE_SUFFIX = re.compile(
    r"\b" + _DURATION + r"\s*(?:old|ago|late|overdue|remaining|left)\b", re.I,
)
_AGE_PREFIX = re.compile(
    r"\b(age[sd]?|elapsed|waiting|waited|stale|overdue|behind)"
    r"(?:\s+(?:for|by))?\s*[:=]?\s*" + _DURATION + r"\b", re.I,
)
_COUNTDOWN = re.compile(
    r"\b(due|deadline|expires?|starts?)\s+(?:in|after)\s*" + _DURATION + r"\b", re.I,
)
_HEARTBEAT = re.compile(
    r"^(?:heartbeat\b|routine (?:status|check)\b|"
    r"monitoring continues\b|nothing new\b|"
    r"no (?:new )?(?:changes?|findings?|updates?)\b|"
    r"no (?:issues?|problems?|alerts?) (?:detected|found)\b|"
    r"still (?:healthy|running|green|monitoring)\b|"
    r"all (?:systems|checks) (?:are )?(?:healthy|green|ok|operational)\b|"
    r"status (?:unchanged|check)\b)", re.I,
)
_UNSUPPORTED_OBSERVATION = re.compile(
    r"^(?:unverified\b|unknown\b|assumed\b|guessed\b|"
    r"not (?:checked|verified|observed)\b|"
    r"no (?:evidence|data|observations?)\b|"
    r"(?:cannot|could not|unable to) (?:read|observe|verify|audit|access)\b)", re.I,
)
_DEADLINE_CUE = re.compile(
    r"\b(?:deadline|due|starts?|expires?|closes?|cutoff|"
    r"release|meeting|handoff|by)\b", re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:no|not|unconfirmed|unverified|tentative|hypothetical|"
    r"possible|could|might|may|would|simulation|simulated|drill)\b", re.I,
)
_ACTIVE_IMPACT = re.compile(
    r"\b(?:outage|data loss|data corruption|security breach|"
    r"production (?:is )?(?:down|unavailable)|"
    r"(?:payments?|customers?|transactions?|requests?)\b"
    r"[^.!?;]{0,60}\b(?:blocked|failing|failed|unavailable))\b", re.I,
)
_STOP_WORDS = frozenset(
    "a an the is are was were be been to of in on at for from by with and or "
    "it this that now still has have had will can could would may might".split()
)
_ASCII = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2192": "->",
    "\u2190": "<-", "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u00a0": " ",
})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    value = " ".join(value.split())
    if value.casefold().rstrip(".!") in _EMPTY:
        raise ValueError(f"{field} must contain an explicit answer, not a placeholder")
    if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError(f"{field} contains control characters or invalid Unicode")
    return value


def _identity(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_FINGERPRINT_CHARS:
        raise ValueError("fingerprint must be a string of at most 512 characters")
    _text(value, "fingerprint")
    return value


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(_ISO, value.strip()):
        raise ValueError(f"{field} must be offset-aware ISO-8601")
    try:
        stamp = datetime.fromisoformat(value.strip().upper().replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{field} is not a valid instant") from error


def _semantic(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _CLOCK.sub("", text)
    text = _AGE_PREFIX.sub(lambda match: match[1], text)
    text = _AGE_SUFFIX.sub("", text)
    text = _COUNTDOWN.sub(lambda match: match[1], text)

    def stamp(match: re.Match) -> str:
        try:
            return _instant(match[0], "observation").isoformat()
        except ValueError:
            return match[0]

    text = _STAMP.sub(stamp, text)
    text = re.sub(r"\b(?:now|still)\s+", "", text)
    text = re.sub(r"(?<!\d)[.,;:!?]+|[.,;:!?]+(?!\d)", " ", text)
    return " ".join(text.split())


def _material(observation: str) -> bool:
    text = _semantic(observation)
    return bool(
        text and text not in _EMPTY and len(_words(text)) >= 2
        and not _HEARTBEAT.match(observation.strip()) and not _HEARTBEAT.match(text)
        and not _UNSUPPORTED_OBSERVATION.match(text)
    )


def _observations(proposal: dict) -> set[str]:
    return {
        _semantic(item["observation"]) for item in proposal["evidence"]
        if _material(item["observation"])
    }


def _validate(proposal: dict) -> None:
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be an object")
    if not isinstance(proposal.get("scenario"), str) or proposal["scenario"] not in SCENARIOS:
        raise ValueError("unsupported scenario")
    if proposal.get("status") != "ready":
        raise ValueError("only status=ready is eligible")
    _identity(proposal.get("fingerprint"))
    for field in ("title", *_ANSWERS, "reason"):
        _text(proposal.get(field), field)
    if proposal.get("urgency") not in ("routine", "time_sensitive", "urgent"):
        raise ValueError("unsupported urgency")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("evidence must contain attributed observations")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("each evidence item must be an object")
        _text(item.get("source"), "evidence.source")
        _text(item.get("observation"), "evidence.observation")
    if not _observations(proposal):
        raise ValueError("evidence contains no substantive observation")
    artifacts = proposal.get("artifacts")
    if not isinstance(artifacts, list) or any(
        not isinstance(item, str) or not item.strip() for item in artifacts
    ):
        raise ValueError("artifacts must be a list of nonempty paths")
    if "deadline" in proposal:
        _instant(proposal["deadline"], "deadline")


def _policy(policy: Optional[dict]) -> dict:
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object")
    zone = policy.get("timezone", "UTC")
    if not isinstance(zone, str) or not zone:
        raise ValueError("timezone must be an IANA zone name")
    if zone == "UTC":
        zone = timezone.utc
    else:
        try:
            zone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("unknown timezone or unavailable IANA timezone data") from error
    limit = policy.get("max_daily", 6)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("max_daily must be a nonnegative integer")
    quiet = policy.get("quiet_hours", {"start": "22:00", "end": "08:00"})
    if quiet is None or quiet is False or quiet == "off":
        quiet = None
    else:
        if not isinstance(quiet, dict):
            raise ValueError("quiet_hours must be {start, end}, False, None, or 'off'")
        minutes = []
        for name in ("start", "end"):
            value = quiet.get(name)
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", value,
            ):
                raise ValueError(f"quiet_hours.{name} must be HH:MM")
            hour, minute = map(int, value.split(":"))
            minutes.append(hour * 60 + minute)
        if minutes[0] == minutes[1]:
            raise ValueError("quiet hours must have distinct start and end")
        quiet = tuple(minutes)
    windows = {}
    for name, default in (("urgent_hours", 2), ("time_sensitive_hours", 24)):
        value = policy.get(name, default)
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
        windows[name] = value
    if windows["urgent_hours"] > windows["time_sensitive_hours"]:
        raise ValueError("urgent_hours must not exceed time_sensitive_hours")
    return {"zone": zone, "max_daily": limit, "quiet": quiet, **windows}


def _queued(history: list[dict], now: datetime) -> list[tuple[datetime, dict]]:
    if not isinstance(history, list):
        raise ValueError("history must be a list")
    queued = []
    for record in history:
        if not isinstance(record, dict) or record.get("decision") not in (
            "queued", "suppressed", "error",
        ):
            raise ValueError("history contains an invalid decision record")
        if record["decision"] != "queued":
            continue
        at = _instant(record.get("at"), "history.at")
        if at > now:
            raise ValueError("queued history is in the future")
        prior = record.get("proposal")
        _validate(prior)
        if (
            record.get("scenario") != prior["scenario"]
            or record.get("fingerprint") != prior["fingerprint"]
        ):
            raise ValueError("queued history identity does not match its proposal")
        queued.append((at, prior))
    return sorted(queued, key=lambda item: item[0])


def _claim(proposal: dict) -> str:
    deadline = (
        _instant(proposal["deadline"], "deadline").isoformat()
        if "deadline" in proposal else None
    )
    return H("scenario-interrupt/1:claim", {
        "change": _semantic(proposal["change"]),
        "impact": _semantic(proposal["impact"]),
        "observations": sorted(_observations(proposal)),
        "deadline": deadline,
    })


def _related(proposal: dict, prior: dict) -> bool:
    if proposal["fingerprint"] == prior["fingerprint"]:
        return True
    if proposal["scenario"] != prior["scenario"]:
        return False
    if _semantic(proposal["title"]) != _semantic(prior["title"]):
        return False
    sources = {item["source"].strip() for item in proposal["evidence"]}
    old_sources = {item["source"].strip() for item in prior["evidence"]}
    return bool(sources & old_sources) or _claim(proposal) == _claim(prior)


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?;])\s+", text)


def _deadline_evidenced(proposal: dict) -> bool:
    if "deadline" not in proposal:
        return False
    deadline = _instant(proposal["deadline"], "deadline")
    for item in proposal["evidence"]:
        for sentence in _sentences(item["observation"]):
            sentence = _CLOCK.sub("", sentence)
            if not _DEADLINE_CUE.search(sentence) or _UNCERTAIN.search(sentence):
                continue
            for match in _STAMP.finditer(sentence):
                try:
                    if _instant(match[0], "observed deadline") == deadline:
                        return True
                except ValueError:
                    continue
    return False


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]*|\d+(?:\.\d+)?", text.casefold())) - _STOP_WORDS


def _impact_evidenced(proposal: dict) -> bool:
    impact = _semantic(proposal["impact"])
    if _UNCERTAIN.search(impact):
        return False
    quantities = set(re.findall(r"\b\d+(?:\.\d+)?\b", _STAMP.sub("", impact)))
    for item in proposal["evidence"]:
        for sentence in _sentences(item["observation"]):
            if not _ACTIVE_IMPACT.search(sentence) or _UNCERTAIN.search(sentence):
                continue
            observed = _semantic(sentence)
            if (
                len(_words(impact) & _words(observed)) >= 2
                and quantities <= set(re.findall(r"\b\d+(?:\.\d+)?\b", observed))
            ):
                return True
    return False


def _urgent_enough(proposal: dict, now: datetime, settings: dict) -> bool:
    if _impact_evidenced(proposal):
        return True
    if not _deadline_evidenced(proposal):
        return False
    remaining = (_instant(proposal["deadline"], "deadline") - now).total_seconds()
    window = settings[proposal["urgency"] + "_hours"] * 3600
    return 0 <= remaining <= window


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", " ".join(text.split()).translate(_ASCII))
    accents = "\u0300\u0301\u0302\u0303\u0304\u0306\u0307\u0308\u030a\u030b\u030c\u0327\u0328"
    text = "".join(char for char in text if char not in accents)
    return text.encode("ascii", "backslashreplace").decode("ascii")


def render(proposal: dict) -> str:
    """Return a complete <=650-character, <=90-word ASCII text, or raise ValueError.

    Nothing is silently truncated, including caveats, decisions, or evidence.
    This formats supplied claims; it does not attest to their truth or send them.
    """
    _validate(proposal)
    if (
        _HEARTBEAT.match(proposal["change"].strip())
        or _HEARTBEAT.match(_semantic(proposal["change"]))
    ):
        raise ValueError("routine heartbeat is not an interruption")
    lines = [_ascii(proposal["title"])]
    for label, field in (
        ("Changed", "change"), ("Impact", "impact"),
        ("Action", "action"), ("Need", "decision"),
    ):
        lines.append(f"{label}: {_ascii(proposal[field])}")
    lines.append("Evidence: " + "; ".join(
        f"{_ascii(item['source'])}: {_ascii(item['observation'])}"
        for item in proposal["evidence"]
    ))
    if "deadline" in proposal:
        lines.append("Deadline: " + _ascii(proposal["deadline"]))
    text = "\n".join(lines)
    if len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        raise ValueError(
            "message_too_long: provide a concise complete proposal; "
            "decision and evidence cannot be truncated"
        )
    return text


def delivery_key(proposal: dict) -> str:
    """Identify a finding's evidenced version for outbox retries, not admission.

    Keep the semantic fingerprint in proposal/history records. A delivery key
    changes with substantive claims, not moving ages, clocks, or presentation.
    """
    _validate(proposal)
    return H("scenario-interrupt/1:delivery", {
        "fingerprint": proposal["fingerprint"],
        "claim": _claim(proposal),
    })


def evaluate(proposal: dict, history: list[dict], now: str,
             policy: Optional[dict] = None) -> dict:
    """Assess admission, never reserve budget, record, enqueue, or send.

    The caller must lock across reading history, evaluating, enqueueing, and
    recording the outcome. Only a successful ``queued`` record spends budget.
    """
    fingerprint = (
        proposal.get("fingerprint", "") if isinstance(proposal, dict) else ""
    )
    try:
        fingerprint = _identity(fingerprint)
    except ValueError:
        fingerprint = ""

    def result(allow: bool, reason: str) -> dict:
        return {"allow": allow, "reason": reason, "fingerprint": fingerprint}

    try:
        _validate(proposal)
        render(proposal)
    except (ValueError, TypeError) as error:
        return result(False, f"unsupported_proposal: {error}")
    try:
        current = _instant(now, "now")
        settings = _policy(policy)
        local = current.astimezone(settings["zone"])
    except (ValueError, TypeError, OverflowError) as error:
        return result(False, f"invalid_policy_or_time: {error}")
    try:
        queued = _queued(history, current)
    except (ValueError, TypeError, OverflowError) as error:
        return result(False, f"invalid_history: {error}")

    related = [prior for _, prior in queued if _related(proposal, prior)]
    if any(_claim(proposal) == _claim(prior) for prior in related):
        return result(False, "duplicate: this unchanged finding was already queued")
    if related:
        observed = set().union(*(_observations(prior) for prior in related))
        new_observations = _observations(proposal) - observed
        if not new_observations:
            return result(False, "unsupported_update: no substantive new evidence")
        impact = _semantic(proposal["impact"])
        if impact != _semantic(related[-1]["impact"]):
            quantities = set(re.findall(r"\b\d+(?:\.\d+)?\b", _STAMP.sub("", impact)))
            new_evidence = " ".join(new_observations)
            if (
                not _words(impact) & _words(new_evidence)
                or not quantities <= set(re.findall(r"\b\d+(?:\.\d+)?\b", new_evidence))
            ):
                return result(False, "unsupported_update: changed impact lacks corroborating evidence")
        if (
            proposal.get("deadline") != related[-1].get("deadline")
            and "deadline" in proposal and not _deadline_evidenced(proposal)
        ):
            return result(False, "unsupported_update: changed deadline lacks evidence")

    urgency = proposal["urgency"]
    if urgency != "routine" and not _urgent_enough(proposal, current, settings):
        return result(False, "unsupported_urgency: no evidenced imminent deadline or active material impact")
    if settings["quiet"]:
        start, end = settings["quiet"]
        minute = local.hour * 60 + local.minute
        quiet = (
            start <= minute < end if start < end else minute >= start or minute < end
        )
        if quiet and urgency != "urgent":
            return result(False, "quiet_hours: only evidenced urgent findings may interrupt")
    try:
        spent = sum(at.astimezone(settings["zone"]).date() == local.date() for at, _ in queued)
    except (ValueError, OverflowError):
        return result(False, "invalid_history: queued instant cannot be represented in the local zone")
    if spent >= settings["max_daily"]:
        return result(False, "daily_budget: global queued-notification limit reached")
    reason = "evidenced_update" if related else "supported_new_finding"
    return result(True, reason)


def build(context: dict) -> dict:
    """Privately assess context.proposals (or context.proposal); never request a page."""
    assessments = []
    status = "suppressed"
    reason = "Private gate assessment only; no notifications were queued or sent."
    try:
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        current = _instant(context.get("now"), "now")
        _policy(context.get("policy"))
        history = context.get("history", [])
        _queued(history, current)
        proposals = context.get("proposals")
        if proposals is None:
            proposals = [context["proposal"]] if "proposal" in context else []
        if not isinstance(proposals, list):
            raise ValueError("context.proposals must be a list")
        for proposal in proposals:
            assessment = evaluate(proposal, history, context["now"], context.get("policy"))
            scenario = proposal.get("scenario") if isinstance(proposal, dict) else None
            assessments.append({
                "scenario": scenario if isinstance(scenario, str) and scenario in SCENARIOS else None,
                **assessment,
            })
    except (ValueError, TypeError, OverflowError) as error:
        status = "blocked"
        reason = f"Cannot assess the gate: {error}. No notifications were queued or sent."
    allowed = sum(item["allow"] for item in assessments)
    identity = sorted(
        ((item["scenario"] or "", item["fingerprint"]) for item in assessments),
    )
    return {
        "scenario": "interrupt",
        "status": status,
        "title": "Earn the right to interrupt me",
        "change": (
            f"Assessed {len(assessments)} proposals; {allowed} individually eligible."
            if assessments else "No proposals assessed."
        ),
        "impact": "Unsupported or repeated findings do not earn a notification.",
        "action": "Returned a private assessment; did not reserve budget, enqueue, or send.",
        "decision": "No decision needed.",
        "evidence": [{"source": "interrupt.evaluate", "observation": reason}],
        "artifacts": [],
        "fingerprint": "interrupt:" + H("scenario-interrupt/1:assessment", [list(item) for item in identity]),
        "urgency": "routine",
        "reason": reason,
        "assessments": assessments,
    }
