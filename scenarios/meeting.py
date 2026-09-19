"""Evidence-backed, local-only meeting briefings. See docs/scenarios/meeting.md."""

import hashlib
import html
import json
import math
import os
import re
import stat
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
MAX_CALENDAR_BYTES = 1024 * 1024
MAX_MATERIAL_BYTES = 256 * 1024
MAX_EVENTS = 256
MAX_MATERIALS = 8
FUTURE_SKEW = timedelta(minutes=5)


class _Invalid(Exception):
    def __init__(self, reason, source, observation):
        super().__init__(observation)
        self.reason = reason
        self.source = source
        self.observation = observation


def _invalid(source, observation, reason="invalid_source"):
    raise _Invalid(reason, source, observation)


def _text(value, source, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _invalid(source, "Expected bounded, nonempty text.")
    if any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127
           or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        _invalid(source, "Control characters are not accepted.")
    return " ".join(value.split())


def _zone(name, source):
    try:
        return ZoneInfo(_text(name, source, 100))
    except (ZoneInfoNotFoundError, ValueError):
        _invalid(source, "An installed IANA timezone is required.")


def _localize(value, zone, source):
    candidates = [value.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    try:
        valid = [candidate for candidate in candidates
                 if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value]
    except (OverflowError, ValueError):
        _invalid(source, "Local time is outside the supported datetime range.")
    if not valid:
        _invalid(source, "Nonexistent local time at a daylight-saving transition.")
    if len({candidate.utcoffset() for candidate in valid}) > 1:
        _invalid(source, "Ambiguous local time; export an explicit UTC offset.")
    return valid[0]


def _instant(raw, source, zone_name=None):
    if not isinstance(raw, str) or not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", raw):
        _invalid(source, "Expected an ISO 8601 datetime, not a date or transcript.")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _invalid(source, "Invalid ISO 8601 datetime.")
    zone = _zone(zone_name, source) if zone_name is not None else None
    if value.tzinfo is None or value.utcoffset() is None:
        if zone is None:
            _invalid(source, "A UTC offset or explicit IANA timezone is required.")
        return _localize(value, zone, source)
    try:
        value.astimezone(UTC)
        return value.astimezone(zone) if zone else value
    except (OverflowError, ValueError):
        _invalid(source, "Time is outside the supported datetime range.")


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fresh(value, now, hours, source, kind):
    if value.astimezone(UTC) - now > FUTURE_SKEW:
        _invalid(source, "Snapshot timestamp is in the future.", "future_" + kind)
    if now - value.astimezone(UTC) > timedelta(hours=hours):
        _invalid(source, "Snapshot exceeds the configured freshness limit.",
                 "stale_" + kind)


def _number(sources, key, default, maximum):
    value = sources.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= maximum or not math.isfinite(value)):
        _invalid("sources." + key, "Expected a positive, bounded number.",
                 "invalid_context")
    return value


def _path(home, raw, source):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or "\0" in raw:
        _invalid(source, "An explicit local path is required.", "invalid_context")
    if raw.startswith(("//", "\\\\")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
        _invalid(source, "URLs and remote calendar access are not supported.")
    path = Path(raw[2:]) if raw.startswith("~/") else Path(raw)
    explicit_absolute = not raw.startswith("~/") and path.is_absolute()
    if not explicit_absolute:
        path = home / path
    try:
        resolved = path.resolve()
        if not explicit_absolute:
            resolved.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        _invalid(source, "Relative paths must resolve inside context.home; "
                 "external paths require an explicit absolute override.", "unsafe_path")
    return resolved


def _read(path, limit):
    source = str(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            _invalid(source, "Expected a regular file within the size limit.")
        flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(os.open(str(path), flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                _invalid(source, "Expected a regular file.")
            data = handle.read(limit + 1)
        if len(data) > limit:
            _invalid(source, "Source exceeds the size limit.")
        return data.decode("utf-8-sig")
    except FileNotFoundError:
        _invalid(source, "Explicitly configured source is missing.", "missing_source")
    except (OSError, UnicodeError):
        _invalid(source, "Source is unreadable or is not UTF-8.", "unreadable_source")


def _json(raw, source):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite number")

    def integer(raw_integer):
        if len(raw_integer) > 20:
            raise ValueError("oversized integer")
        return int(raw_integer)

    def floating(raw_float):
        result = float(raw_float)
        if not math.isfinite(result):
            raise ValueError("nonfinite number")
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite,
                           parse_int=integer, parse_float=floating)
    except (ValueError, RecursionError):
        _invalid(source, "Malformed JSON, duplicate keys, or nonfinite numbers.")
    if not isinstance(value, dict):
        _invalid(source, "Expected a JSON object.")
    return value


def _shape(value, required, optional, source):
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - optional):
        _invalid(source, "Missing or unsupported fields in the intake schema.")


def _list(value, source, limit):
    if not isinstance(value, list) or len(value) > limit:
        _invalid(source, "Expected a bounded list.")
    return value


def _event(raw, source):
    _shape(raw, {"id", "title", "start", "end"},
           {"timezone", "status", "agenda", "all_day"}, source)
    identifier = _text(raw["id"], source + "/id", 256)
    title = _text(raw["title"], source + "/title", 200)
    status = raw.get("status", "confirmed")
    if status not in ("confirmed", "tentative", "cancelled"):
        _invalid(source + "/status", "Unsupported event status.")
    all_day = raw.get("all_day", False)
    if not isinstance(all_day, bool):
        _invalid(source + "/all_day", "Expected a boolean.")
    if "timezone" in raw:
        _zone(raw["timezone"], source + "/timezone")
    if all_day:
        try:
            start, end = date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"])
        except (TypeError, ValueError):
            _invalid(source, "All-day boundaries must be ISO dates.")
    else:
        start = _instant(raw["start"], source + "/start", raw.get("timezone"))
        end = _instant(raw["end"], source + "/end", raw.get("timezone"))
    # Compare instants in UTC, including when both datetimes share a DST zone.
    ordered = end > start if all_day else end.astimezone(UTC) > start.astimezone(UTC)
    if not ordered:
        _invalid(source, "Event end must follow its start.")
    agenda = raw.get("agenda", "")
    if not isinstance(agenda, str) or len(agenda) > 4000:
        _invalid(source + "/agenda", "Expected bounded agenda text.")
    agenda = _text(agenda, source + "/agenda", 4000) if agenda else ""
    return {"id": identifier, "title": title, "start": start, "end": end,
            "status": status, "all_day": all_day, "agenda": agenda, "source": source}


def _calendar_json(raw, source):
    data = _json(raw, source)
    _shape(data, {"schema", "fetched_at", "events"}, set(), source)
    if data["schema"] != "meeting-calendar/v1":
        _invalid(source, "Expected schema meeting-calendar/v1; notes are not a calendar.")
    fetched = _instant(data["fetched_at"], source + "#/fetched_at")
    events = [_event(item, source + "#/events/" + str(index))
              for index, item in enumerate(_list(data["events"], source, MAX_EVENTS))]
    return fetched, events


def _ics_text(raw, source):
    result = []
    characters = iter(raw)
    for char in characters:
        if char == "\\":
            char = next(characters, None)
            if char not in ("n", "N", ",", ";", "\\"):
                _invalid(source, "Unsupported iCalendar text escape.")
            char = "\n" if char in ("n", "N") else char
        result.append(char)
    return "".join(result)


def _ics_time(item, default_zone, source):
    params, raw = item
    if params.keys() - {"TZID", "VALUE"}:
        _invalid(source, "Unsupported iCalendar datetime parameters.")
    kind = params.get("VALUE", "DATE-TIME")
    if kind == "DATE":
        if "TZID" in params or not re.fullmatch(r"\d{8}", raw):
            _invalid(source, "Invalid all-day iCalendar date.")
        try:
            return datetime.strptime(raw, "%Y%m%d").date().isoformat(), True
        except ValueError:
            _invalid(source, "Invalid all-day iCalendar date.")
    if kind != "DATE-TIME" or not re.fullmatch(r"\d{8}T\d{6}Z?", raw):
        _invalid(source, "Expected a concrete iCalendar date-time.")
    if raw.endswith("Z") and "TZID" in params:
        _invalid(source, "UTC iCalendar times cannot also declare TZID.")
    try:
        value = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        _invalid(source, "Invalid iCalendar date-time.")
    if raw.endswith("Z"):
        return value.replace(tzinfo=UTC).isoformat(), False
    zone_name = params.get("TZID", default_zone)
    if not zone_name:
        _invalid(source, "Floating iCalendar times require an explicit IANA timezone.")
    return _localize(value, _zone(zone_name, source), source).isoformat(), False


def _calendar_ics(raw, source, observed_at):
    fetched = _instant(observed_at, "sources.meeting_calendar_fetched_at")
    lines = []
    for line in raw.splitlines():
        if line.startswith((" ", "\t")):
            if not lines:
                _invalid(source, "Orphan iCalendar continuation.")
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
        if len(lines) > 12000 or lines and len(lines[-1]) > 16000:
            _invalid(source, "iCalendar exceeds the line limits.")
    stack, records, current, metadata = [], [], None, {}
    closed = False
    keep = {"UID", "SUMMARY", "DTSTART", "DTEND", "DESCRIPTION", "STATUS",
            "RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID", "DURATION"}
    for line in lines:
        head, separator, value = line.partition(":")
        if not separator or closed:
            _invalid(source, "Malformed iCalendar structure.")
        parts = head.split(";")
        name = parts[0].upper()
        if not re.fullmatch(r"[A-Z0-9-]+", name):
            _invalid(source, "Malformed iCalendar property name.")
        if name not in keep | {"BEGIN", "END", "VERSION", "METHOD", "X-WR-TIMEZONE"}:
            if not stack:
                _invalid(source, "Property outside an iCalendar component.")
            continue
        params = {}
        for part in parts[1:]:
            key, equals, item = part.partition("=")
            if not equals or key.upper() in params:
                _invalid(source, "Malformed iCalendar parameters.")
            params[key.upper()] = item.strip('"')
        if name == "BEGIN":
            value = value.upper()
            if (not stack and value != "VCALENDAR" or len(stack) >= 4
                    or value == "VCALENDAR" and stack
                    or value == "VEVENT" and stack != ["VCALENDAR"]):
                _invalid(source, "Unsupported iCalendar nesting.")
            stack.append(value)
            if value == "VEVENT":
                current = {}
        elif name == "END":
            if not stack or stack.pop() != value.upper():
                _invalid(source, "Unbalanced iCalendar structure.")
            if value.upper() == "VEVENT":
                records.append(current)
                current = None
                if len(records) > MAX_EVENTS:
                    _invalid(source, "Too many calendar events.")
            if not stack:
                closed = True
        elif stack == ["VCALENDAR"] and name in {"VERSION", "METHOD", "X-WR-TIMEZONE"}:
            if name in metadata:
                _invalid(source, "Duplicate calendar metadata.")
            metadata[name] = value
        elif stack == ["VCALENDAR", "VEVENT"] and name in keep:
            if name in current:
                _invalid(source, "Duplicate event property.")
            current[name] = (params, value)
        elif not stack:
            _invalid(source, "Property outside an iCalendar component.")
    if stack or not closed or metadata.get("VERSION") != "2.0":
        _invalid(source, "An entire VERSION:2.0 VCALENDAR is required.")
    events = []
    for index, props in enumerate(records):
        ref = source + "#/VEVENT/" + str(index)
        status = props.get("STATUS", ({}, "CONFIRMED"))[1].lower()
        if metadata.get("METHOD", "").upper() == "CANCEL" or status == "cancelled":
            continue
        if props.keys() & {"RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID"}:
            _invalid(ref, "Export expanded, nonrecurring occurrences with unique IDs.",
                     "unsupported_recurrence")
        if not {"UID", "SUMMARY", "DTSTART", "DTEND"} <= props.keys() or "DURATION" in props:
            _invalid(ref, "Each event needs UID, SUMMARY, DTSTART and DTEND.")
        start, all_day = _ics_time(props["DTSTART"], metadata.get("X-WR-TIMEZONE"), ref)
        end, end_all_day = _ics_time(props["DTEND"], metadata.get("X-WR-TIMEZONE"), ref)
        if all_day != end_all_day:
            _invalid(ref, "Mixed timed and all-day boundaries.")
        event = {"id": _ics_text(props["UID"][1], ref),
                 "title": _ics_text(props["SUMMARY"][1], ref),
                 "start": start, "end": end, "all_day": all_day, "status": status}
        if "DESCRIPTION" in props:
            event["agenda"] = _ics_text(props["DESCRIPTION"][1], ref)
        events.append(_event(event, ref))
    return fetched, events


def _material(raw, source):
    data = _json(raw, source)
    _shape(data, {"schema", "meeting_id", "updated_at", "facts", "decision"},
           {"commitments", "discussion"}, source)
    if data["schema"] != "meeting-material/v1":
        _invalid(source, "Expected schema meeting-material/v1.")
    identifier = _text(data["meeting_id"], source + "#/meeting_id", 256)
    updated = _instant(data["updated_at"], source + "#/updated_at")
    facts = [_text(item, source + "#/facts/" + str(index))
             for index, item in enumerate(_list(data["facts"], source, 16))]
    if len(facts) < 2 or len(set(facts[:2])) != 2:
        _invalid(source, "At least two distinct source-recorded project facts are required.",
                 "insufficient_material")
    decision = data["decision"]
    _shape(decision, {"topic", "options", "criterion", "outcome"}, set(), source)
    decision = dict(decision)
    for name in ("topic", "criterion", "outcome"):
        decision[name] = _text(decision[name], source + "#/decision/" + name, 500)
    options = _list(decision["options"], source + "#/decision/options", 2)
    if len(options) != 2:
        _invalid(source, "A decision needs exactly two documented alternatives.")
    decision["options"] = [_text(item, source + "#/decision/options", 300)
                           for item in options]
    if decision["options"][0] == decision["options"][1]:
        _invalid(source, "Decision alternatives must be distinct.")
    commitments = []
    for index, item in enumerate(_list(data.get("commitments", []), source, 16)):
        ref = source + "#/commitments/" + str(index)
        _shape(item, {"owner", "commitment", "quote"}, {"due"}, ref)
        result = {key: _text(item[key], ref + "/" + key)
                  for key in ("owner", "commitment", "quote")}
        if "due" in item:
            result["due"] = _iso(_instant(item["due"], ref + "/due"))
        commitments.append(result)
    discussion = [_text(item, source + "#/discussion/" + str(index))
                  for index, item in enumerate(_list(data.get("discussion", []), source, 16))]
    return {"meeting_id": identifier, "updated": updated, "facts": facts[:2],
            "decision": decision, "commitments": commitments,
            "discussion": discussion, "source": source}


def _envelope(status, reason, evidence, semantic=None, **changes):
    digest = hashlib.sha256(json.dumps(
        {"scenario": "meeting", "status": status, "reason": reason, "content": semantic},
        sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    result = {
        "scenario": "meeting", "status": status,
        "title": "Meeting briefing unavailable",
        "change": "No evidence-backed meeting briefing was produced.",
        "impact": "Preparing from stale or unrelated notes could target the wrong meeting.",
        "action": "Supply a fresh authorized calendar cache and meeting-linked material.",
        "decision": "Do not infer an upcoming meeting from historical discussion.",
        "evidence": evidence, "artifacts": [], "fingerprint": digest,
        "urgency": "routine", "reason": reason,
    }
    result.update(changes)
    return result


def _md(value):
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", escaped)


def _short_complete(value, chars, words, fallback):
    if value.isascii() and len(value) <= chars and len(value.split()) <= words:
        return value
    return fallback


def _notification(event, material):
    decision = material["decision"]
    identifier = hashlib.sha256(event["id"].encode("utf-8")).hexdigest()[:8]
    question = (f"Which {decision['topic']} option meets "
                f"{decision['criterion'].rstrip('.?')}?")
    question_fallback = "Which of the two documented options meets the acceptance criterion?"
    if question.count("?") != 1:
        question = question_fallback
    evidence = [{
        "source": "calendar",
        "observation": f"{event['status'].capitalize()} meeting starts {_iso(event['start'])}.",
    }]
    for index in (1, 0):
        fact = _short_complete(material["facts"][index], 72, 12, "")
        if fact:
            evidence.append({"source": "project#" + str(index + 1), "observation": fact})
            break
    return {
        "title": _short_complete("Prepare: " + event["title"], 48, 6,
                                 "Meeting brief #" + identifier),
        "change": "A sourced briefing and decision draft are ready.",
        "impact": _short_complete(
            "Goal: " + decision["outcome"], 76, 12,
            "Two documented options need comparison before the meeting."),
        "action": "Review the unsent local draft before the meeting.",
        "decision": _short_complete(
            question, 100, 16, question_fallback),
        "evidence": evidence,
    }


def _draft(event, material, facts, question):
    decision = material["decision"]
    ref = material["source"]
    lines = [
        "# Meeting briefing and decision draft",
        "",
        "LOCAL DRAFT — not sent, approved, or a new commitment.",
        "",
        "## Meeting",
        _md(event["title"]),
        "",
        "## Notification source references",
        f"- calendar: {_md(event['source'])}",
        f"- project#1: {_md(ref + '#/facts/0')}",
        f"- project#2: {_md(ref + '#/facts/1')}",
        "",
        "## Three source-backed facts",
    ]
    for index, fact in enumerate(facts, 1):
        lines.extend([f"{index}. {_md(fact['observation'])}",
                      f"   Source: {_md(fact['source'])}"])
    lines.extend([
        "", "## One outcome-changing question", _md(question),
        f"Source: {_md(ref + '#/decision')}",
        "", "## Prepared decision memo — for review",
        f"Desired outcome: {_md(decision['outcome'])}.",
        f"The decision for {_md(decision['topic'])} remains open.",
        f"The documented alternatives are **{_md(decision['options'][0])}** "
        f"and **{_md(decision['options'][1])}**.",
        f"Evaluate both against **{_md(decision['criterion'])}** before recording a choice.",
        "Draft disposition: no option selected; approval, any new owner, and any new "
        "delivery date remain unassigned. Record a choice only after reviewing the "
        "evidence and the explicit commitments below.",
        f"Source: {_md(ref + '#/decision')}",
        "", "## Recorded explicit commitments",
        "These are source assertions with supplied quotations, not independently "
        "verified promises or new commitments created by this draft.",
    ])
    if not material["commitments"]:
        lines.append("- None recorded. Discussion is not a commitment.")
    for index, item in enumerate(material["commitments"]):
        due = " Due: " + item["due"] + "." if "due" in item else " No due time recorded."
        lines.extend([
            f"- {_md(item['owner'])}: {_md(item['commitment'])}.{_md(due)}",
            f"  Source quotation: {_md(item['quote'])}",
            f"  Source: {_md(ref + '#/commitments/' + str(index))}",
        ])
    lines.extend(["", "## Discussion / inferred possibilities — not commitments"])
    if not material["discussion"] and not event["agenda"]:
        lines.append("- None supplied; no discussion or promises inferred.")
    for index, item in enumerate(material["discussion"]):
        lines.extend([f"- {_md(item)}",
                      f"  Source: {_md(ref + '#/discussion/' + str(index))}"])
    if event["agenda"]:
        lines.extend([f"- Calendar agenda/description only: {_md(event['agenda'])}",
                      f"  Source: {_md(event['source'])}"])
    lines.extend(["", "No attendee contact, calendar mutation, or messaging was performed.", ""])
    return "\n".join(lines)


def _write_artifact(directory, fingerprint, content):
    target = directory / ("meeting-" + fingerprint[:20] + "-decision-draft.md")
    staging = directory / (".meeting-" + uuid.uuid4().hex + ".part")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(str(staging), flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Replacement is atomic and does not follow an existing target symlink.
        os.replace(str(staging), str(target))
    except OSError:
        _invalid(str(directory), "Could not persist the local decision draft.",
                 "artifact_unwritable")
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _invalid(str(directory), "Could not clean up an incomplete local draft.",
                     "artifact_unwritable")
    return str(target)


def build(context: dict) -> dict:
    """Build a JSON-safe envelope; read explicit local files and write only artifact_dir."""
    evidence = []
    selected = None
    try:
        if not isinstance(context, dict):
            _invalid("context", "Expected a context object.", "invalid_context")
        home_raw = context.get("home")
        if not isinstance(home_raw, str) or not Path(home_raw).is_absolute():
            _invalid("context.home", "Expected an absolute local home directory.",
                     "invalid_context")
        try:
            home = Path(home_raw).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            _invalid("context.home", "Home directory is unavailable.", "invalid_context")
        if not home.is_dir():
            _invalid("context.home", "Home must be a directory.", "invalid_context")
        now = _instant(context.get("now"), "context.now").astimezone(UTC)
        directory = _path(home, context.get("artifact_dir"), "context.artifact_dir")
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            _invalid("context.sources", "Expected a sources object.", "invalid_context")
        lookahead = _number(sources, "meeting_lookahead_hours", 24, 168)
        calendar_age = _number(sources, "meeting_calendar_max_age_hours", 24, 72)
        material_age = _number(sources, "meeting_material_max_age_hours", 336, 2160)
        if not sources.get("meeting_calendar"):
            _invalid("sources.meeting_calendar",
                     "No authorized calendar snapshot configured; old notes are not "
                     "upcoming-meeting evidence.", "missing_calendar")
        calendar = _path(home, sources["meeting_calendar"], "sources.meeting_calendar")
        if calendar.suffix.lower() not in (".json", ".ics"):
            _invalid(str(calendar), "Only validated JSON or ICS calendar intake is supported.")
        raw = _read(calendar, MAX_CALENDAR_BYTES)
        if calendar.suffix.lower() == ".json":
            fetched, events = _calendar_json(raw, str(calendar))
        elif calendar.suffix.lower() == ".ics":
            fetched, events = _calendar_ics(
                raw, str(calendar), sources.get("meeting_calendar_fetched_at"))
        _fresh(fetched, now, calendar_age, str(calendar), "calendar")
        identifiers = [item["id"] for item in events]
        if len(set(identifiers)) != len(identifiers):
            _invalid(str(calendar), "Calendar event IDs must be unique.")
        evidence.append({"source": str(calendar),
                         "observation": "Calendar snapshot fetched at " + _iso(fetched)
                         + " passed the configured freshness check."})
        upcoming = sorted(
            (item for item in events
             if not item["all_day"] and item["status"] != "cancelled"
             and timedelta(0) < item["start"].astimezone(UTC) - now
             <= timedelta(hours=lookahead)),
            key=lambda item: (item["start"].astimezone(UTC), item["id"]))
        if not upcoming:
            return _envelope(
                "suppressed", "no_upcoming_meeting", evidence,
                title="No upcoming meeting briefing",
                change="No active timed meeting starts within the configured lookahead.",
                impact="No draft is needed for past, ongoing, cancelled, or all-day entries.",
                action="Keep the authorized calendar snapshot current.",
                decision="Do not send a meeting briefing.")
        selected = upcoming[0]
        material_paths = _list(sources.get("meeting_materials", []),
                               "sources.meeting_materials", MAX_MATERIALS)
        if not material_paths:
            _invalid("sources.meeting_materials",
                     "The upcoming meeting needs explicitly linked project material.",
                     "missing_material")
        matching = []
        seen = set()
        for raw_path in material_paths:
            path = _path(home, raw_path, "sources.meeting_materials")
            if path in seen:
                _invalid(str(path), "Duplicate material path.")
            seen.add(path)
            if path.suffix.lower() != ".json":
                _invalid(str(path), "Meeting material must use the validated JSON schema.")
            material = _material(_read(path, MAX_MATERIAL_BYTES), str(path))
            if material["meeting_id"] == selected["id"]:
                matching.append(material)
        if len(matching) != 1:
            _invalid("sources.meeting_materials",
                     "Exactly one material record must match the selected meeting ID.",
                     "unmatched_material" if not matching else "conflicting_material")
        material = matching[0]
        _fresh(material["updated"], now, material_age, material["source"], "material")
        evidence.append({"source": material["source"],
                         "observation": "Meeting-linked material updated at "
                         + _iso(material["updated"]) + " passed its freshness check."})
        start, end = selected["start"].isoformat(), selected["end"].isoformat()
        facts = [{"source": selected["source"],
                  "observation": f"Calendar records {selected['title']} "
                  f"({selected['status']}), starting {start} and ending {end}."}]
        facts.extend({"source": material["source"] + "#/facts/" + str(index),
                      "observation": fact}
                     for index, fact in enumerate(material["facts"]))
        evidence.extend(facts)
        decision = material["decision"]
        question = (f"Which option should we choose for {decision['topic']} — "
                    f"{decision['options'][0]} or {decision['options'][1]} — "
                    f"to meet {decision['criterion'].rstrip('.?')}?")
        content = _draft(selected, material, facts, question)
        seconds = (selected["start"].astimezone(UTC) - now).total_seconds()
        semantic = {
            "id": selected["id"], "title": selected["title"],
            "start": _iso(selected["start"]), "end": _iso(selected["end"]),
            "status": selected["status"], "agenda": selected["agenda"],
            "facts": material["facts"], "decision": decision,
            "commitments": material["commitments"], "discussion": material["discussion"],
        }
        result = _envelope(
            "ready", "upcoming_meeting", semantic=semantic,
            deadline=_iso(selected["start"]),
            urgency="routine" if selected["status"] != "confirmed" else
                    "urgent" if seconds <= 3600 else
                    "time_sensitive" if seconds <= 14400 else "routine",
            **_notification(selected, material))
        result["artifacts"] = [_write_artifact(directory, result["fingerprint"], content)]
        return result
    except _Invalid as exc:
        evidence.append({"source": exc.source, "observation": exc.observation})
        semantic = {"source": exc.source}
        changes = {"action": exc.observation}
        if selected is not None:
            semantic["meeting_id"] = selected["id"]
            changes["deadline"] = _iso(selected["start"])
        return _envelope("blocked", exc.reason, evidence, semantic, **changes)
