"""Read-only, bounded producer of a portable Storykeeper incident brief."""

import hashlib
import html
import io
import ipaddress
import json
import os
import re
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


MAX_VERDICT_BYTES = 256 * 1024
MAX_SENT_BYTES = 64 * 1024
MAX_CHECKS = 128
MAX_MESSAGES = 128
MAX_DETAIL_CHARS = 8192
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_AGE = timedelta(hours=6)
FUTURE_TOLERANCE = timedelta(minutes=5)
TEXT_NAME = "incident-brief.txt"
HTML_NAME = "incident-brief.html"
ZIP_NAME = HTML_NAME + ".zip"
RECEIPT_NAME = "acceptance.json"
FORMAT = "storykeeper-win/1"

EXPLANATIONS = {
    "rv_world_merging": (
        "Rappterverse merge freshness needs attention",
        "A serving page does not establish that its underlying state is advancing.",
        "Inspect the last successful merge and workflow logs before choosing a repair.",
    ),
    "rv_meaningful_activity": (
        "Rappterverse activity needs review",
        "The activity check distinguishes fresh work from a page that merely loads.",
        "Review the latest chat and agent-state timestamps and their producing jobs.",
    ),
    "rv_validation": (
        "The action gate needs attention",
        "Stopped validation and rejected actions are different problems; use the recorded detail.",
        "Inspect the latest gate run before deciding whether a restart or a code change is appropriate.",
    ),
    "alert_delivery": (
        "Message delivery needs reconciliation",
        "An outbox entry is not proof of phone delivery; UNKNOWN is not confirmed failure.",
        "Reconcile the outbox evidence with Messages before authorizing any retry.",
    ),
    "w_openrappter_spin": (
        "Local agent job stability needs review",
        "Repeated starts are not evidence that the job completed useful work.",
        "Inspect the named job's exit status and logs; do not restart or change permissions blindly.",
    ),
    "w_sentinel_current": (
        "The deployed watcher version needs review",
        "An upstream fix does not help an instance that is not running it.",
        "Compare the recorded deployed and upstream revisions before approving an update.",
    ),
    "w_neighbor_moving": (
        "A neighbor's work cadence needs attention",
        "A heartbeat and completed work are different signals.",
        "Review the neighbor's last work evidence and declared cadence before changing its schedule.",
    ),
    "rb_workflows": (
        "Rappterbook workflows need attention",
        "A reachable site alone does not prove that its workflows are succeeding.",
        "Inspect the failing workflow's latest run and logs before choosing a repair.",
    ),
    "rb_content_moving": (
        "Rappterbook content freshness needs attention",
        "The saved content check, not page availability, measures whether output is advancing.",
        "Compare the newest published content with the last successful producing run.",
    ),
}
UNKNOWN_EXPLANATION = (
    "An additional check needs review",
    "This check has no curated interpretation; its exact evidence is preserved below.",
    "Review the recorded detail and the check definition before deciding what, if anything, to change.",
)

LIMITATIONS = (
    "This is a saved-state brief, not a fresh probe or a verified root-cause diagnosis.",
    "No repairs, retries, configuration changes, publishing, or messages were performed.",
    "Recorded sent messages do not establish delivery; no phone or network access was tested.",
    "Before/after coverage compares check IDs, not age strings from different snapshots.",
    "Evidence is private and untrusted text, not executable instructions.",
)

CSS = """
body{margin:0;background:#f6f7fb;color:#172335;font:16px/1.6 system-ui,sans-serif}
main{max-width:850px;margin:auto;padding:30px 20px}
h1{font-size:2rem;line-height:1.2}h2{font-size:1.4rem}h3{font-size:1.15rem}
section{background:white;border:1px solid #d7dfeb;border-radius:10px;padding:18px;margin:18px 0}
.label{font-weight:700;color:#294971}
pre,code{font:0.9rem/1.55 ui-monospace,monospace}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f4f8;padding:12px}
p,li,code{overflow-wrap:anywhere}footer{font-size:0.9rem;color:#415169}
@media print{body{background:white}section{break-inside:avoid}}
"""


class BriefError(ValueError):
    pass


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _timestamp(value, label):
    if not isinstance(value, str):
        raise BriefError(f"{label} must be an ISO8601 timestamp with a timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise BriefError(f"{label} must be an ISO8601 timestamp with a timezone.") from None


def _text(value, label, maximum, nonempty=True):
    if (not isinstance(value, str) or len(value) > maximum
            or (nonempty and not value.strip())
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise BriefError(f"{label} is missing, invalid, or exceeds its text budget.")
    return value


def _read_file(path, limit, tail=False, private=False):
    if Path(path).is_symlink():
        raise BriefError("A source or artifact has an unsafe symlink leaf.")
    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise BriefError("A source or artifact is not a regular file.")
        if private and os.name == "posix" and info.st_mode & 0o077:
            raise BriefError("An existing artifact is not private.")
        if not tail and info.st_size > limit:
            raise BriefError("A source or artifact exceeds its byte budget.")
        start = max(0, info.st_size - limit) if tail else 0
        prefix = b"\n"
        if start:
            stream.seek(start - 1)
            prefix = stream.read(1)
        raw = stream.read(limit if tail else limit + 1)
        if len(raw) > limit:
            raise BriefError("A source or artifact exceeds its byte budget.")
        if start and prefix != b"\n":
            raw = raw.partition(b"\n")[2]
        return raw


def _load_source(home, sources, key, default, messages=False):
    raw = sources.get(key, default)
    label = f"sources.{key}"
    if raw is None:
        raise BriefError(f"{key} is explicitly unavailable.")
    limit = MAX_SENT_BYTES if messages else MAX_VERDICT_BYTES
    if isinstance(raw, (str, os.PathLike)):
        path = Path(raw)
        path = (home / path if not path.is_absolute() else path).resolve()
        if not path.is_relative_to(home):
            raise BriefError(f"{key} source is outside the approved home.")
        label = str(path.relative_to(home))
        encoded = _read_file(path, limit, tail=messages)
        if messages:
            data = []
            for line in encoded.splitlines()[-MAX_MESSAGES:]:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict):
                    data.append(record)
        else:
            data = json.loads(encoded)
    else:
        data = raw[-MAX_MESSAGES:] if messages and isinstance(raw, list) else raw
        encoded = _json_bytes(data)
        if len(encoded) > limit:
            raise BriefError(f"{key} exceeds its byte budget.")
    expected = list if messages else dict
    if not isinstance(data, expected):
        raise BriefError(f"{key} has an unsupported top-level shape.")
    return data, label


def _validate_verdict(verdict, now):
    generated = _timestamp(verdict.get("generated"), "last_verdict.generated")
    age = now - generated
    if age > MAX_AGE:
        raise BriefError("The saved verdict is over six hours old; no current incident is established.")
    if age < -FUTURE_TOLERANCE:
        raise BriefError("The saved verdict is more than five minutes in the future.")
    checks = verdict.get("checks")
    if not isinstance(checks, list) or not 0 < len(checks) <= MAX_CHECKS:
        raise BriefError("last_verdict.checks must contain 1 to 128 check records.")
    ids = set()
    failed = []
    for check in checks:
        if not isinstance(check, dict):
            raise BriefError("Every verdict check must be an object.")
        check_id = _text(check.get("id"), "check.id", 200)
        if check_id in ids or any(c.isspace() for c in check_id):
            raise BriefError("Check IDs must be unique and contain no whitespace.")
        ids.add(check_id)
        if type(check.get("ok")) is not bool or check.get("severity") not in ("warn", "critical"):
            raise BriefError("Every check needs boolean ok and warn/critical severity.")
        detail = _text(check.get("detail"), "check.detail", MAX_DETAIL_CHARS,
                       nonempty=not check["ok"])
        if not check["ok"]:
            label, impact, action = EXPLANATIONS.get(check_id, UNKNOWN_EXPLANATION)
            failed.append({
                "id": check_id, "severity": check["severity"], "detail": detail,
                "label": label, "impact": impact, "action": action,
            })
    critical = {item["id"] for item in failed if item["severity"] == "critical"}
    for key, actual in (("failed", {item["id"] for item in failed}), ("critical", critical)):
        declared = verdict.get(key)
        if (not isinstance(declared, list)
                or not all(isinstance(item, str) for item in declared)
                or len(declared) != len(set(declared)) or set(declared) != actual):
            raise BriefError(f"last_verdict.{key} disagrees with its check records.")
    status = "critical" if critical else ("degraded" if failed else "healthy")
    if verdict.get("status") != status:
        raise BriefError("last_verdict.status disagrees with its check records.")
    return generated.isoformat(), sorted(failed, key=lambda item: (item["severity"] != "critical", item["id"]))


def _private_url_count(text):
    count = 0
    for value in re.findall(r"https?://[^\s<>\"']+", text):
        try:
            host = urlsplit(value).hostname
            if not host:
                continue
            if host.lower() == "localhost" or host.lower().endswith(".local"):
                count += 1
                continue
            address = ipaddress.ip_address(host)
            if not address.is_global:
                count += 1
        except ValueError:
            continue
    return count


def _baseline(records, findings, now):
    for row in reversed(records):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            continue
        text = row["text"]
        covered = [
            item["id"] for item in findings
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(item["id"])
                         + r"(?![A-Za-z0-9_])", text)
        ]
        if not covered:
            continue
        try:
            at = _timestamp(row.get("sent_at") or row.get("at"), "message timestamp")
        except BriefError:
            continue
        if at - now > FUTURE_TOLERANCE:
            continue
        attachments = row.get("attachments", [])
        return {
            "available": True, "recorded_at": at.isoformat(),
            "message_characters": len(text), "covered_check_ids": sorted(covered),
            "attachment_count": len(attachments) if isinstance(attachments, list) else None,
            "private_network_urls": _private_url_count(text),
            "explicitly_unverified": bool(row.get("unverified")),
        }
    return {"available": False, "reason": "No usable matching record in the bounded sent-ledger tail."}


def _baseline_text(baseline, total):
    if not baseline["available"]:
        return "Message baseline unavailable: " + baseline["reason"]
    attachments = baseline["attachment_count"]
    note = (
        f"Newest usable matching record ({baseline['recorded_at']}): "
        f"{baseline['message_characters']} characters; "
        f"{len(baseline['covered_check_ids'])}/{total} current check IDs; "
        f"{attachments if attachments is not None else 'unknown'} attachments; "
        f"{baseline['private_network_urls']} private-network URLs."
    )
    if baseline["explicitly_unverified"]:
        note += " The ledger explicitly marks delivery unverified."
    return note


def _baseline_notice(baseline, total):
    if not baseline["available"]:
        return "No usable baseline; delivery untested."
    files = baseline["attachment_count"]
    return (
        f"{len(baseline['covered_check_ids'])}/{total} IDs, "
        f"{baseline['private_network_urls']} private links, "
        f"{files if files is not None else 'unknown'} attachments; delivery untested."
    )


def _render(generated, findings, baseline, source):
    count = len(findings)
    critical = sum(item["severity"] == "critical" for item in findings)
    summary = f"{count} recorded findings ({critical} critical), each with full evidence and a read-only next step."
    before = _baseline_text(baseline, count)
    text = [
        "STORYKEEPER — OFFLINE INCIDENT BRIEF", f"Saved snapshot: {generated}",
        f"Evidence source: {source}", summary, "", "WHAT YOU CAN USE NOW",
        "Read this file anywhere; the companion ZIP contains the same evidence in a standalone HTML page.",
        "No decision is needed to use the brief. Any repair or retry is a separate decision.",
        "", "BEFORE / AFTER", before,
        f"Now: {count}/{count} findings have their complete recorded detail in both offline formats.",
        "A private-network URL requires that network; its reachability was not tested.",
        "", "FINDINGS (CRITICAL FIRST)",
    ]
    sections = []
    for index, item in enumerate(findings, 1):
        heading = f"{index}. {item['label']} [{item['severity']}]"
        text.extend([
            "", heading, f"Check: {item['id']}", f"Why it matters: {item['impact']}",
            "Recorded evidence:", item["detail"], f"Next step: {item['action']}",
        ])
        sections.append(
            "<section><h3>" + html.escape(heading) + "</h3><p><code>"
            + html.escape(item["id"]) + "</code></p><p>"
            + html.escape(item["impact"]) + "</p><p class=\"label\">Recorded evidence</p><pre>"
            + html.escape(item["detail"]) + "</pre><p><strong>Read-only next step: </strong>"
            + html.escape(item["action"]) + "</p></section>"
        )
    text.extend(["", "LIMITS AND SAFETY", *LIMITATIONS, ""])
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<title>Storykeeper offline incident brief</title><style>" + CSS
        + "</style></head><body><main><header><h1>Storykeeper incident brief</h1><p>"
        + html.escape(summary) + "</p><p>Saved snapshot: " + html.escape(generated)
        + "</p><p>Evidence source: " + html.escape(source)
        + "</p></header><section><h2>Usable offline, without a decision</h2>"
        "<p>All evidence is embedded. Any repair or retry is a separate decision.</p>"
        "<h2>Before / after</h2><p>" + html.escape(before)
        + f"</p><p>Now: {count}/{count} complete findings in both offline formats.</p>"
        "<p>A private-network URL requires that network; its reachability was not tested.</p></section>"
        "<h2>Findings — critical first</h2>" + "".join(sections)
        + "<footer><h2>Limits and safety</h2><ul>"
        + "".join("<li>" + html.escape(item) + "</li>" for item in LIMITATIONS)
        + "</ul></footer></main></body></html>\n"
    )
    return "\n".join(text).encode("utf-8"), page.encode("utf-8")


class _HTMLAudit(HTMLParser):
    TAGS = {"html", "head", "meta", "title", "style", "body", "main", "header",
            "h1", "h2", "h3", "p", "section", "pre", "code", "strong", "footer", "ul", "li"}
    ATTRS = {"lang", "charset", "name", "content", "http-equiv", "class"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.passive = True
        self.data = []
        self.style = False

    def handle_starttag(self, tag, attrs):
        self.passive &= tag in self.TAGS and all(key in self.ATTRS for key, _ in attrs)
        values = dict(attrs)
        if "http-equiv" in values:
            self.passive &= (values["http-equiv"] or "").lower() == "content-security-policy"
        self.style = tag == "style"

    def handle_endtag(self, tag):
        self.passive &= tag in self.TAGS
        if tag == "style":
            self.style = False

    def handle_data(self, data):
        if self.style:
            self.passive &= re.search(r"@import|url\s*\(|expression\s*\(", data, re.I) is None
        else:
            self.data.append(data)


def _zip(page):
    buffer = io.BytesIO()
    info = zipfile.ZipInfo(HTML_NAME, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, page)
    return buffer.getvalue()


def _validate_written(bundle, expected_text, expected_zip, findings):
    text_bytes = _read_file(bundle / TEXT_NAME, MAX_ARTIFACT_BYTES, private=True)
    zip_bytes = _read_file(bundle / ZIP_NAME, MAX_ARTIFACT_BYTES, private=True)
    if text_bytes != expected_text or zip_bytes != expected_zip:
        raise BriefError("Artifact read-back differs from the generated content.")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        if archive.namelist() != [HTML_NAME] or archive.testzip() is not None:
            raise BriefError("The HTML ZIP failed its integrity check.")
        if archive.getinfo(HTML_NAME).file_size > MAX_ARTIFACT_BYTES:
            raise BriefError("The HTML ZIP exceeds its expanded byte budget.")
        page = archive.read(HTML_NAME).decode("utf-8")
    text = text_bytes.decode("utf-8")
    parser = _HTMLAudit()
    parser.feed(page)
    parser.close()
    visible = "".join(parser.data)
    text_count = html_count = 0
    escaped = True
    for item in findings:
        values = [item[key] for key in ("id", "detail", "label", "impact", "action")]
        text_count += all(value in text for value in values)
        html_count += all(value in visible for value in values)
        escaped &= all(html.escape(value) in page for value in values)
    if not (text_count == html_count == len(findings) and escaped and parser.passive):
        raise BriefError("The offline brief failed completeness or passive-HTML acceptance.")
    return {
        "validated_from_disk": True, "failed_checks": len(findings),
        "text_complete_findings": text_count, "html_complete_findings": html_count,
        "escaped_evidence": escaped, "html_passive": parser.passive, "zip_integrity": True,
    }


def _write_private(path, payload):
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_bundle(root, text, page, findings, generated, baseline, provenance, fingerprint):
    zipped = _zip(page)
    if max(len(text), len(page), len(zipped)) > MAX_ARTIFACT_BYTES:
        raise BriefError("The generated brief exceeds its output budget.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = {
        "schema": FORMAT + "/acceptance", "fingerprint": fingerprint,
        "snapshot_at": generated, "provenance": provenance, "before": baseline,
        "artifact_permissions": (
            "POSIX directory 0700 and files 0600" if os.name == "posix"
            else "Inherited OS ACL; the caller must protect artifact_dir"),
        "limitations": list(LIMITATIONS),
    }
    identity = text + zipped + _json_bytes(metadata)
    bundle = root / ("win-" + _digest(identity)[:24])
    created = False
    owned = []
    try:
        try:
            bundle.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            if (bundle.is_symlink() or not bundle.is_dir()
                    or (os.name == "posix" and bundle.stat().st_mode & 0o077)):
                raise BriefError("An existing artifact directory is unsafe.")
        if bundle.resolve().parent != root:
            raise BriefError("The artifact directory escapes its approved root.")
        if created:
            for name, payload in ((TEXT_NAME, text), (ZIP_NAME, zipped)):
                _write_private(bundle / name, payload)
                owned.append(bundle / name)
        acceptance = _validate_written(bundle, text, zipped, findings)
        receipt = {
            **metadata, "acceptance": acceptance,
            "files": {name: {"bytes": len(payload), "sha256": _digest(payload)}
                      for name, payload in ((TEXT_NAME, text), (ZIP_NAME, zipped))},
        }
        encoded = _json_bytes(receipt)
        if created:
            _write_private(bundle / RECEIPT_NAME, encoded)
            owned.append(bundle / RECEIPT_NAME)
        if _read_file(bundle / RECEIPT_NAME, MAX_ARTIFACT_BYTES, private=True) != encoded:
            raise BriefError("The acceptance receipt failed read-back.")
        return bundle
    except Exception:
        if created:
            for path in reversed(owned):
                path.unlink(missing_ok=True)
            bundle.rmdir()
        raise


def _suppressed(reason):
    return {
        "scenario": "win", "status": "suppressed",
        "title": "No validated Storykeeper incident brief",
        "change": "No usable artifact was produced.", "impact": "No notification is warranted.",
        "action": "Do not enqueue this result.", "decision": "No user decision requested.",
        "evidence": [{"source": "win input/acceptance gate", "observation": reason}],
        "artifacts": [], "fingerprint": "win:suppressed:" + _digest(reason.encode("utf-8")),
        "urgency": "routine", "reason": reason,
    }


def build(context: dict) -> dict:
    """Build verified private artifacts; never enqueue, send, probe, or modify inputs."""
    try:
        if not isinstance(context, dict):
            raise BriefError("The build context must be an object.")
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            raise BriefError("context.sources must be an object.")
        now = _timestamp(context.get("now"), "context.now")
        for name in ("home", "artifact_dir"):
            if not isinstance(context.get(name), (str, os.PathLike)) or not str(context[name]).strip():
                raise BriefError(f"context.{name} must be a local path.")
        home = Path(context["home"]).expanduser().resolve()
        root = Path(context["artifact_dir"]).expanduser().resolve()
        verdict, verdict_source = _load_source(
            home, sources, "last_verdict", "state/last_verdict.json")
        generated, findings = _validate_verdict(verdict, now)
        if not findings:
            return _suppressed("The saved verdict has no failed checks; no incident brief is needed.")
        sent_source = "sources.outbox_sent" if "outbox_sent" in sources else "state/outbox-sent.jsonl"
        try:
            records, sent_source = _load_source(
                home, sources, "outbox_sent", "state/outbox-sent.jsonl", messages=True)
            baseline = _baseline(records, findings, now)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            baseline = {"available": False, "reason": "The optional sent-message source is unavailable or invalid."}
        fingerprint = "win:offline-brief:" + _digest(_json_bytes({
            "format": FORMAT,
            "findings": [(item["id"], item["severity"]) for item in findings],
        }))
        provenance = {
            "last_verdict": verdict_source, "outbox_sent": sent_source,
            "normalized_verdict_sha256": _digest(_json_bytes(verdict)),
        }
        text, page = _render(generated, findings, baseline, verdict_source)
        bundle = _write_bundle(root, text, page, findings, generated, baseline, provenance, fingerprint)
        count = len(findings)
        return {
            "scenario": "win", "status": "ready",
            "title": "Offline Storykeeper brief",
            "change": "Built text and offline HTML briefs.",
            "impact": f"{count}/{count} findings retain full evidence.",
            "action": "Open the text attachment or HTML ZIP.",
            "decision": "None to read; repairs need separate approval.",
            "evidence": [
                {"source": "last_verdict",
                 "observation": f"{count} findings; sampled at {generated}."},
                {"source": "outbox_sent", "observation": _baseline_notice(baseline, count)},
                {"source": RECEIPT_NAME,
                 "observation": f"Text/HTML {count}/{count} complete; ZIP verified."},
            ],
            "artifacts": [str(bundle / TEXT_NAME), str(bundle / ZIP_NAME)],
            "fingerprint": fingerprint, "urgency": "routine",
            "reason": "Offline artifacts verified; sources untouched.",
        }
    except BriefError as exc:
        return _suppressed(str(exc))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, zipfile.BadZipFile):
        return _suppressed("Local input or artifact validation failed; nothing was approved for delivery.")
