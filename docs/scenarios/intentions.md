# Scenario 6: recover dropped intentions

`scenarios.intentions.build(context) -> dict` reviews **only explicitly configured
local JSON files**. It ranks up to three current, quiet, explicit user promises
and prepares one concrete next-step artifact for the highest-ranked promise.
It never sends, enqueues, executes a suggested action, edits a source, contacts
GitHub, or discovers files. Delivery belongs to the shared scenario/outbox service.
Implementation and fixtures use only the Python 3.9+ standard library.

## Context and authorization

The Launchpad/protocol adapter supplies the complete context. A portable caller
may choose the following default home; this module never calls `Path.home()`,
reads environment defaults, requests OS permissions, or probes another device.
An explicit caller-provided home/output directory overrides these illustrative
defaults. The adapter provisions private directories and obtains source access
before invoking `build`.

```python
from datetime import datetime, timezone
from pathlib import Path

home = Path.home() / ".storykeeper" / "home"
context = {
    "home": str(home),
    "artifact_dir": str(home / "state" / "scenario-artifacts" / "intentions"),
    "now": datetime.now(timezone.utc).isoformat(),
    "sources": {},  # No source access until the user explicitly selects inputs.
}
```

Configure `context["sources"]["intentions"]` with this synthetic example:

```json
{
  "owner_id": "self",
  "files": ["notes/promises.json"],
  "quiet_days": 7,
  "max_idle_days": 90,
  "snapshot_max_age_days": 7,
  "relevance_max_age_days": 30
}
```

`home`, `artifact_dir`, `now`, and `sources` are required. `home` must be an existing
absolute directory; `artifact_dir` must be an absolute, private output directory
separate from `home` and its ancestors. `now` is supplied by the caller, never
read from the wall clock. All dates require ISO8601 timestamps with a timezone;
date-only or naive timestamps are invalid. Equivalent offsets are normalized
to UTC.

`sources.intentions.owner_id` and `files` are required, not inferred from the OS,
assignees, an agent identity, or a message account. Other scenario keys in `sources`
are ignored. A path is either absolute (explicit authorization for that exact
file) or relative to `home`, without `..`. URLs, `~`, symlinks and filesystem
reparse points (including ancestor directories), devices and directory inputs
are rejected. No glob expansion or recursive scan occurs.

The four day settings are optional, with the defaults shown above. Each must be
an integer in `1..365`, and `quiet_days <= max_idle_days`. Unknown configuration
keys are errors. The review is bounded to eight files, 512 KiB per file, and 500
records **total**; exceeding a bound blocks rather than silently truncating
possibly important completion/ownership records.

## Validated source format

A UTF-8 JSON snapshot has exactly `schema`, `as_of`, and `items`. This is a
**synthetic illustration, not a claim about the user's intentions**:

```json
{
  "schema": "intentions/v1",
  "as_of": "2026-09-19T18:30:00Z",
  "items": [
    {
      "id": "release-outline",
      "kind": "promise",
      "owner_id": "self",
      "status": "open",
      "title": "Prepare the release review",
      "created_at": "2026-08-20T12:00:00Z",
      "updated_at": "2026-09-01T12:00:00Z",
      "original": {
        "kind": "note",
        "author_id": "self",
        "ref": "project-journal#release-review",
        "quote": "I will prepare the release review.",
        "context": "Next: Draft a release outline. Sections: Audience; Verified changes; Caveats."
      },
      "consequence": {
        "level": "high",
        "reason": "The release review needs an agreed scope."
      },
      "relevance": {
        "state": "current",
        "checked_at": "2026-09-18T12:00:00Z",
        "reason": "The release review remains on the current milestone."
      },
      "next_step": {
        "format": "outline",
        "title": "Release review outline",
        "action": "Draft a release outline",
        "points": ["Audience", "Verified changes", "Caveats"]
      },
      "deadline": "2026-09-20T12:00:00Z"
    }
  ]
}
```

All illustrated record/nested fields are required except `deadline`, which may
also be `null`. Unknown fields and duplicate JSON keys are rejected, including
unrecognized lifecycle fields that could otherwise conceal completion. Empty
`items` is valid. Non-finite JSON numbers, invalid UTF-8 and malformed structures
are not valid snapshots.

| Field | Accepted values / meaning |
| --- | --- |
| `id` | Stable canonical ID across files, matching `[A-Za-z0-9][A-Za-z0-9._:/#-]*`; never a generated row number. |
| `kind` | `promise`, `idea`, `suggestion`, `repair`; only `promise` qualifies. |
| `owner_id` | Explicit current owner, exactly matching configured `owner_id`. |
| `status` | `open`, `in_progress`, `completed`, `done`, `cancelled`, `canceled`, `closed`. |
| `created_at` | When the promise was made, not when it was exported. |
| `updated_at` | Last meaningful work/lifecycle activity or explicit reaffirmation, not an export/scan timestamp. |
| `original.kind` | `note`, `issue`, `unfinished_work`, `alert`, `repair`; the latter two never qualify. |
| `original.author_id` | Author of the **quoted promise**, also exactly matching the selected owner. Assignment alone is not a promise. |
| `original.ref` | Original note/issue/work reference, retained verbatim but never fetched or executed. |
| `original.quote` | Exact, unadorned first-person affirmative promise, not an assistant paraphrase. |
| `original.context` | Bounded surrounding context, including known resolutions and specific next-step details. |
| `consequence.level` | `high`, `medium`, `low`, explicitly assessed in the authorized export. |
| `consequence.reason` | Source-supported consequence of leaving this unfinished, not an invented obligation. |
| `relevance.state` | `current`, `stale`, `unknown`; only `current` qualifies. |
| `relevance.checked_at` | When continuing relevance was actually checked. |
| `relevance.reason` | Why the promise still matters now, with its original contextual basis. |
| `next_step.format` | `outline`, `message`, `checklist`. |
| `next_step.title` | Title of the private artifact to prepare. |
| `next_step.action` | Concrete first action; must occur in the original quote/context. |
| `next_step.points` | 1–10 specific sections/items, each occurring in that same original context. |
| `deadline` | Optional recorded deadline, never a guessed due date. |

Text must be nonempty without non-whitespace control characters. IDs, identities
and titles are limited to 200 characters; reasons to 1,000; original references
and quotes to 2,000; original context to 8,000; action and each point to 500.
Grounding matches ignore whitespace, case and straight/curly apostrophe
differences, not wording. The resulting outline/checklist/message is assembled
from these grounded details and cites both the original reference/context and
the actual `FILE#/items/INDEX` snapshot locations. It is a **draft**, not completed
work, and asserts no new delivery date. Source strings are data, never commands.
Markdown/HTML from source strings is escaped in the draft to prevent embedded
images or active markup; the private JSON report preserves the original text.

Build a snapshot only from authorized notes, local issue exports or unfinished
work records. Preserve the actual author's words; verify ownership, lifecycle,
consequence and current relevance. Do not automatically fill `as_of` or
`relevance.checked_at` with the scan time for old, unchecked data. The module
validates this contract, but cannot authenticate a manually fabricated export.
No local note/issue adapter is implied.

## Conservative exclusions and freshness

- Only supported first-person English forms qualify: `I will …`, `I'll …`,
  `I promise/promised to …`, `I commit/committed to …`, `I agree/agreed to …`.
  This is a conservative deterministic guard, **not a general language model**.
  Negation, questions, conditional/tentative phrasing, reported speech and ideas
  do not become promises. Ambiguous wording is intentionally missed, not guessed.
- Completed/cancelled/closed records always exclude that canonical ID, even if
  another configured file still contains an open copy. Other conflicting copies
  also exclude the ID. Identical copies merge their citations. A reopened
  commitment needs reconciled sources rather than overriding a terminal copy.
- Other-owned or agent-authored records, operational alert/repair sources,
  ideas and suggestions are excluded even if their text looks like a promise.
  Clear completion/cancellation or withdrawal in the surrounding context or
  current-relevance reason also vetoes `open`.
- `created_at <= updated_at <= as_of` and
  `created_at <= relevance.checked_at <= as_of <= now` must hold.
  A deadline may not precede creation.
- Every configured snapshot must be current (default at most seven days old).
  **Any** missing, stale, future, malformed or unreadable source blocks the whole
  review: a missing source might contain a cancellation or ownership change.
- Default inactivity is 7–90 days inclusive, measured from recorded activity,
  not mtime. An old promise with genuine newer activity may qualify; very old
  untouched work does not. Relevance must be explicitly current and checked
  within 30 days. A deadline over seven days overdue additionally needs
  relevance reconfirmed **after** that deadline.

## Ranking, urgency, envelope and private outputs

Consequence contributes `high=30`, `medium=20`, `low=10`; relevance checked within
7/14/30 days contributes `6/4/2` (also `2` for older relevance if explicitly
configured). A recorded deadline due/overdue within one day contributes `9`,
within seven days `6`, within 30 days `3`, otherwise `0`. Sort descending by the
sum, then by consequence, earlier deadline, and canonical ID. Return only the
best three and draft only the first; report both scores and original evidence.

Urgency comes only from the selected promise's recorded deadline: `urgent` for
high consequence due within 24 hours or overdue; otherwise `time_sensitive`
within seven days or overdue; otherwise `routine`. Age alone is never urgency.

The envelope has `scenario="intentions"`, `status`, `title`, `change`, `impact`,
`action`, `decision`, `evidence` (`source`/`observation` pairs), `artifacts`,
`fingerprint`, `urgency`, `reason`, and optional recorded `deadline`.

- `ready`: qualifying records and a private cited next-step draft exist.
- `suppressed`: reliable snapshots were reviewed, but nothing qualified.
- `blocked`: reliable inputs or safe private output are unavailable. No intention
  or draft is fabricated and the shared service must not treat this as ready.

Ready notification content is deliberately small: title, four answers, reason
and evidence together stay within 500 ASCII characters / 70 words, leaving room
for the shared gate's labels under its 650-character / 90-word rendering target.
The notification cites only the selected promise. `artifacts[0]#/ranked/0`
means the first ranked record in the private review artifact listed first in
the envelope; that record preserves the full original context and exact source
citations. Its concise observation explicitly includes the recorded, absolute
UTC deadline when present, rather than expecting an urgency flag to prove it.
Otherwise it records the relevance-check date and urgency remains routine.
Long, non-ASCII or overly wordy titles/consequences/actions use truthful
review-oriented fallback text, never a truncated statement or lossy translation.
Full details remain in the artifacts and continue to determine the fingerprint.

For valid contexts, a private `intentions-review-HASH.json` records the envelope,
top three, full `source_evidence`, aggregate exclusion counts, policy and reviewed
count; ready reviews also produce `intentions-next-step-HASH.md`. Only these
artifacts are written, inside `artifact_dir`. On POSIX systems, directory mode is `0700` and file mode
is `0600`. On Windows, the adapter must provision a user-restricted directory
ACL before calling; POSIX mode bits are not an ACL guarantee. Files inherit that
private ACL and are replaced through same-directory staging files. Source files
are untouched, and artifact encoding is UTF-8 with LF newlines on every platform.
Do not point the output directory at a repository/public tree. Do not commit
reports, quotes or real input exports. Prior artifacts are not pruned: consumers
must use this build's returned envelope/artifact list, not old drafts discovered
in the output directory.

The semantic SHA-256 fingerprint excludes evaluation/export/relevance-check
timestamps, source positions/paths, artifact paths, score drift and input order.
It includes ranked canonical IDs, promise words, actionable details, stated
consequence/relevance, deadline and selected urgency. Equivalent whitespace
and timezone offsets do not create a new intention; materially changed work or
urgency does. Freshness/eligibility and rank changes can change the result.

## Public protocol integration and device onboarding

The API and `intentions/v1` source contract are unchanged for the public RAPP
iMessage Launchpad adapter. The public package contains only code, documentation
and synthetic fixtures: no account handles, destinations, local source paths,
calendar/text excerpts or real promises. Per-device context, selected source
files, envelopes, evidence and artifacts are private runtime data, not shared
configuration defaults, SDK examples, application bundles or public telemetry.

On each device, the adapter obtains explicit local-file access, selects the
current owner's identity, provisions its private instance/artifact directories,
and passes that device's paths through context. Do not copy another device's
absolute paths or infer permissions from its configuration. Permission denial
or unavailable input remains `blocked`; never bypass it or promote repair advice
to a user promise. The adapter alone owns protocol transport, consent and the
shared outbox; this module neither publishes nor delivers messages.
The shared gate alone applies notification timezone, quiet hours, daily caps
and queued-history deduplication. This module's four day settings concern source
eligibility, not delivery policy, and elapsed time alone is not a resend request.

## Local verification and live limitation

```sh
python3 -m unittest discover -s tests -p test_scenario_intentions.py
```

Fixtures are synthetic and use a per-test directory under the checkout's ignored
`.scratch-home/`, cleaned after each test. They never use `/tmp`, credentials,
Messages, a network, or the outbox. Symlink-dependent tests explicitly skip when
the device cannot create symlinks; POSIX mode assertions run only on POSIX.
Context-only path selection, explicit source overrides, device-file rejection,
reparse-point rejection and portable artifact encoding have fixture coverage.

A newly onboarded device without explicit `sources.intentions` configuration
returns `blocked`, with a private report and the missing-input explanation.
Operational issue attempts and agent repair advice are **not** evidence that
the user promised to repair anything. Supplying a real, authorized
`intentions/v1` snapshot is required before a recovery can be ready.
