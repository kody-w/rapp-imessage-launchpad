# Scenario 5: meeting briefing

`scenarios.meeting.build(context: dict) -> dict` is a bounded, deterministic,
stdlib-only builder. It never contacts attendees, accesses a remote connector,
requests permissions, executes calendar alarms, mutates calendars, sends iMessage,
or enqueues a message. The RAPP iMessage Launchpad host/protocol adapter owns
scheduling, deduplication, device onboarding, permissions, and transport.

## Public package and private evidence boundary

The public module, examples, and tests contain only synthetic data and no account,
device, contact, or personal-workspace defaults. **An unconfigured live build is
blocked**, not a demonstration of calendar connectivity. The builder never mines
historical notes, browser profiles, contacts, or credentials for a meeting.
Local discovery findings belong in private runtime receipts, not public docs.

Use an already authorized export/cache through the explicit intake below. Keep
real snapshots, materials, envelopes, and drafts outside the source checkout and
untracked: they can contain private calendar details and absolute source paths.
Source authorization and the authority of original project records still apply.
A material cache is a source assertion, not independent verification of a promise.

## Context and exact source keys

Required context fields:

- `home`: absolute existing private application home supplied by the host.
- `artifact_dir`: explicit private output directory, absolute or relative to `home`.
- `now`: ISO 8601 datetime with an explicit UTC offset (`Z` is accepted).
- `sources`: dictionary, with these meeting-specific keys:

| Key | Type | Meaning/default |
| --- | --- | --- |
| `meeting_calendar` | string | Explicit `.json` or `.ics` calendar path; required. |
| `meeting_calendar_fetched_at` | string | Required for ICS: actual snapshot acquisition time, ISO 8601 with offset. JSON uses its own `fetched_at`; this key cannot override it. |
| `meeting_materials` | list of strings | Up to eight explicit JSON material paths; exactly one must match the selected meeting ID. |
| `meeting_lookahead_hours` | number | Future-start window; default 24, greater than 0 and at most 168. |
| `meeting_calendar_max_age_hours` | number | Snapshot freshness; default 24, greater than 0 and at most 72. |
| `meeting_material_max_age_hours` | number | Selected material freshness; default 336 (14 days), at most 2160 (90 days). |

The host supplies its default `home` from `Path.home() / ".storykeeper" / "home"`
through context. This module never consults the current account's home or falls
back to a developer workspace. Relative paths (and the compatibility `~/` prefix)
resolve inside `context.home`; relative traversal and escaping relative symlinks
are rejected. Explicit absolute source paths override that base, for example an
already-authorized calendar selected using the host's local file picker.
An absolute `artifact_dir` can independently select a private session directory.
The host must authorize those explicit paths; context is not a permission grant
and the module does not attempt to acquire permissions.

URLs and UNC/network-share paths are rejected. There is no directory crawl or
implicit source discovery. Other scenarios' source keys are ignored. Inputs are
UTF-8 regular files: calendars at most 1 MiB/256 events and each material at most
256 KiB. JSON rejects duplicate keys, nonfinite numbers, and unsupported schema
fields. Lists and text fields have additional bounds in the module.

### Onboarding another device

1. The host creates a private application home and obtains normal user approval
   to read a chosen local calendar export and meeting-material file.
2. Pass those selected paths through the source keys above, rather than copying
   another device's usernames, paths, contacts, or credentials into configuration.
3. Stamp actual acquisition time and use current, meeting-linked material.
4. Call `build` with the new device's context and inspect its envelope. Missing,
   denied, or stale inputs remain blocked; they never trigger permission changes,
   attendee contact, or messaging from this module.

Python 3.9+ is required. UTC/offset inputs do not need a timezone database.
Named IANA timezones require an installed database; if one is unavailable, supply
a concrete UTC/offset export without a named timezone instead of guessing the
device's zone. Optional POSIX open flags are not required on other platforms.
The host remains responsible for platform-specific private directory ACLs and
any separately approved iMessage transport.

Calendar freshness uses `fetched_at`, **never file mtime, event DTSTART, DTSTAMP,
or an old transcript date**. Material freshness uses `updated_at`. A timestamp
more than five minutes ahead of `now` is rejected. Copying a stale file does not
refresh it. Stamp an acquisition time only when an authorized snapshot was actually
acquired; the builder cannot independently prove a caller's acquisition claim.

## Explicit JSON calendar intake

This is synthetic fixture data, not a real meeting:

```json
{
  "schema": "meeting-calendar/v1",
  "fetched_at": "2026-09-19T17:45:00Z",
  "events": [{
    "id": "synthetic-review-1",
    "title": "Synthetic release review",
    "start": "2026-09-19T15:00:00-04:00",
    "end": "2026-09-19T15:30:00-04:00",
    "status": "confirmed",
    "agenda": "Discuss pilot scope; approval has not been granted."
  }]
}
```

`id`, `title`, `start`, and `end` are required for each event. IDs must be unique.
Optional fields are `status` (`confirmed`, `tentative`, `cancelled`; default
`confirmed`), `timezone` (IANA name), `agenda`, and `all_day` (boolean).
All-day boundaries are ISO dates with an exclusive end date; those entries never
become timed briefings. Timed boundaries require offsets or an explicit IANA
timezone. The builder rejects nonexistent or ambiguous DST wall times unless an
offset disambiguates them. Ordering and freshness compare UTC instants, never
machine-local time or timestamp strings. End must follow start.

The earliest active timed event satisfying `now < start <= now + lookahead` is
selected; ties break by ID. Past, ongoing, cancelled, all-day, and out-of-window
entries are suppressed. Tentative meetings retain that label. A fresh empty
calendar is also suppressed, whereas missing/stale/invalid calendar evidence is
blocked. A missing briefing for the earliest meeting does not cause a silent jump
to a later meeting.

## ICS intake subset

Supply an explicit `.ics` path and `meeting_calendar_fetched_at`. The parser
accepts a complete `VERSION:2.0` VCALENDAR, folded lines, escaped text, and concrete
VEVENTs with `UID`, `SUMMARY`, `DTSTART`, and `DTEND`. Datetimes can be UTC (`Z`),
have an IANA `TZID`, or use an explicit `X-WR-TIMEZONE` for floating values.
`DESCRIPTION` is discussion context only. All-day `VALUE=DATE` entries are ignored
for briefing purposes; `STATUS:CANCELLED` and `METHOD:CANCEL` do not trigger a
briefing. Attendee/contact properties and nested alarms are not acted on or copied.

Recurrence rules, exclusions, recurrence IDs, and duration-only events are not
expanded or guessed. An active recurrence makes intake blocked
(`unsupported_recurrence`); export already-expanded occurrences with distinct
IDs instead. Unknown/custom timezones require a concrete offset/UTC export.
Unzoned floating times, malformed components, duplicate singleton properties,
and missing ends block intake. ICS parsing is bounded to 12,000 unfolded lines,
16,000 characters per unfolded line, and 256 events.

## Meeting-linked project material

Only one validated material file may match the selected event's ID (ICS `UID`).
All supplied material files are schema-validated; freshness applies to the matching
one. No fuzzy title/project-name matching or transcript mining occurs.

```json
{
  "schema": "meeting-material/v1",
  "meeting_id": "synthetic-review-1",
  "updated_at": "2026-09-19T16:00:00Z",
  "facts": [
    "The synthetic pilot is restricted to staging.",
    "The synthetic rollback drill has not been recorded as passing."
  ],
  "decision": {
    "topic": "pilot scope",
    "options": ["keep the pilot in staging", "approve a limited production pilot"],
    "criterion": "a demonstrated rollback path",
    "outcome": "Record a release scope that does not bypass rollback evidence"
  },
  "commitments": [{
    "owner": "Fixture release lead",
    "commitment": "publish the rollback checklist",
    "quote": "I will publish the rollback checklist.",
    "due": "2026-09-19T18:45:00Z"
  }],
  "discussion": ["A production pilot was proposed, not approved."]
}
```

`schema`, `meeting_id`, `updated_at`, `facts`, and `decision` are required.
The three briefing facts are the selected calendar slot plus the first two
distinct project facts, in supplied priority order (up to 16 accepted).
The artifact's one outcome-changing question is derived from the two distinct
documented alternatives, topic, and acceptance criterion. The full desired
outcome is also preserved there. Missing facts or decision inputs block rather
than fabricate.

`commitments` and `discussion` are optional, each at most 16 records.
Only structured commitments with an owner, commitment, and original quotation
are labeled **recorded explicit commitments**. Optional `due` must be an
offset-bearing datetime. The draft explicitly marks these as source assertions,
not independently verified promises. Calendar prose and the separate discussion
list stay **discussion / inferred possibilities**, never assigned as commitments.
The builder does not invent approvals, owners, deadlines, or attendee statements.
Each claim cites its intake file and JSON pointer (or ICS VEVENT index).

## Result, draft, and verification

Every result is JSON-safe and includes `scenario="meeting"`, `status`
(`ready`, `suppressed`, `blocked`), `title`, `change`, `impact`, `action`, `decision`,
`evidence` (`source`/`observation` objects), `artifacts` (path strings),
`fingerprint`, `urgency`, and `reason`. A selected meeting adds its UTC `deadline`.

### Compact notification contract

Ready notification text is bounded to **650 ASCII characters and 90 words**
with the shared gate's complete `title`, `Changed`, `Impact`, `Action`, `Need`,
`Evidence`, and `Deadline` layout. Source-file and artifact paths are not
automatically embedded in notification fields. Artifact paths remain separately in the
private envelope; the gate does not render them as message text.

The four answers identify a completed briefing, the documented goal (or the
need to compare two documented options), the review action, and a short
outcome-changing question. Whole ASCII values are used only when they fit their
field's character and word budget. Otherwise the need still asks which of the
two documented options meets the acceptance criterion, rather than just saying
"see the draft"; the action still requires review of the unsent draft. No excerpt
can silently drop a negation or a qualifier.
Long or non-ASCII titles use a stable, meeting-ID-derived label. All original
Unicode text, alternatives, stakes, facts, quotations, and references remain
complete in the artifact, and the semantic fingerprint still covers them.

Ready evidence includes `calendar: Confirmed meeting starts <absolute UTC time>`
(or the explicitly qualified tentative equivalent) and at most one complete
short project fact. Source aliases `calendar`, `project#1`, and `project#2`
resolve to the exact file and event/fact pointer in the artifact's
**Notification source references** section. Larger project observations stay
in the artifact rather than being clipped for transport.

Tentative meetings always have routine urgency, preserving their uncertainty.
Confirmed elevated urgency is based on the observed absolute meeting deadline.
This module owns no quiet-hour, budget, or resend policy: the shared gate
evaluates those, counts/deduplicates only queued history, and does not permit
elapsed-time resends.

A ready build atomically persists a Markdown **briefing and prepared
decision memo** inside `artifact_dir`. It contains three referenced facts, one
question, tailored alternatives/criterion/outcome, explicit commitments with
quotations, and separately labeled discussion. It is a real reviewable draft,
not a claim that a decision was approved or a message sent. Nothing is written
on suppressed or evidence-blocked builds. An output error returns blocked.
The output path is hash-based, not derived from an untrusted meeting title.
New files use mode 0600 and new directories use 0700 on POSIX; other platforms
inherit the host-managed private directory ACLs. The builder does not publish,
register runtime artifacts with git, or change repository ignore rules.

The SHA-256 fingerprint reflects selected meeting identity, UTC start/end instants,
title/status/agenda, the two selected project facts, decision inputs, commitments,
and discussion. It excludes moving `now`, urgency, acquisition/material freshness
timestamps, file locations, calendar ordering, and the output directory. Repeated
ready builds of unchanged evidence reuse the artifact name; source references in
the artifact are refreshed if a cache moves or reorders. Urgency is `urgent` at one hour
or less before start, `time_sensitive` at four hours or less, otherwise `routine`.
These elevated levels apply only to confirmed meetings. Tentative, blocked, and
suppressed envelopes have routine urgency; delivery decisions remain the parent's
responsibility.

```python
from pathlib import Path

from scenarios.meeting import build

# The host supplies and provisions this private location, not the scenario.
app_home = Path.home() / ".storykeeper" / "home"
result = build({
    "home": str(app_home),
    "artifact_dir": "artifacts/meeting",
    "now": "2026-09-19T18:00:00Z",
    "sources": {
        "meeting_calendar": "authorized-cache/calendar.json",
        "meeting_materials": ["authorized-cache/review-material.json"]
    }
})
```

Run from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_scenario_meeting.py' -v
```

Fixtures are synthetic, created under the repository during testing and cleaned
afterward. Tests do not use system temporary directories, real calendars, network
access, AI, contacts, or messaging. Both JSON and ICS intake are exercised through
`build`, including persisted drafts, source references, freshness, timezones/DST,
future/past boundaries, corruption, missing evidence, safety limits, and stable
fingerprints. Portability cases exercise context-only home selection, explicit
source/output overrides, device relocation, missing optional OS flags, and
UTC fallback without an IANA database. Named-zone and symlink-specific tests
skip only when the host lacks those capabilities; all remaining tests still run.
Every ready fixture checks the wire budget. An additional integration test calls
the actual `scenarios.interrupt.evaluate` and `render` when that module is
available in the integrated package or on `PYTHONPATH`; standalone runs explicitly
skip that integration test rather than substituting a fake gate.
