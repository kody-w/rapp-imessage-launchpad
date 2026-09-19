# Decision: turn a repeated alert into a ten-second choice

`scenarios.decision.build(context)` returns a JSON-safe decision envelope.
It does **not** send, enqueue, run health checks, invoke repair, change permissions,
reset activity timestamps, or manufacture work. The parent owns delivery and
deduplication through the existing Storykeeper outbox. The RAPP iMessage
Launchpad protocol adapter can call this same API without a second pipeline.

Requires **Python 3.9 or later, standard library only**. Timestamps accept
explicit UTC offsets and JavaScript-style `Z` suffixes, including milliseconds.
`Z` is normalized before `datetime.fromisoformat`, so the installed macOS
Python 3.9 interpreter works without newer date parsing behavior. Naive
timestamps remain invalid.

```python
from pathlib import Path

from scenarios.decision import build

result = build({
    "home": str(Path.home() / ".storykeeper" / "home"),
    "artifact_dir": str(Path.home() / ".storykeeper" / "private-artifacts" / "decision"),
    "now": "2030-04-12T12:00:00+00:00",
    "sources": {},
})
```

`home` is read-only. `artifact_dir` must be separate from `home` and the receipt
directory. Source paths are relative to `home` unless absolute. No environment
variables, network calls, process probes, Messages database, or other home
directories are consulted implicitly. Importing this module does not import
Storykeeper's side-effectful runtime modules.

The caller alone chooses device defaults: `Path.home() / ".storykeeper" / "home"`
is resolved **into context by the adapter**, never inside the builder.
Missing `home` blocks without touching a logged-in user's files. An adapter may
instead supply another local root and explicit source overrides, including
read-only snapshots from another device. Nothing depends on a particular
username, contact, network address, or checkout location.

Permission onboarding, packaging, signing, SDK transport, and outbox access
remain the parent application's responsibility. This scenario needs only its
explicit source files and artifact directory; it does not request Messages,
Automation, Calendar, or Full Disk Access permissions.

## Source contract and bounds

Optional `sources` path overrides:

| Key | Default | Bound |
| --- | --- | --- |
| `verdict` | `state/last_verdict.json` | 256 KiB, at most 200 checks |
| `sent_ledger` | `state/outbox-sent.jsonl` | Last 256 KiB, last 200 complete lines |
| `unknown_ledger` | `state/outbox-unknown.jsonl` | Last 256 KiB, last 200 complete lines |
| `issues` | `state/issues.json` | 256 KiB |
| `escalations` | `state/escalations.json` | 256 KiB, last 200 rows |
| `config` | `config.json` | 256 KiB; output projects only relevant settings |
| `direction` | `direction.json` | 256 KiB; situation and declared interests |
| `logs` | `logs/` | No listing, recursion, or globbing |
| `intent` | Not read unless supplied | 256 KiB, explicit owner intent |
| `runtime` | Not read unless supplied | 256 KiB, external read-only diagnostic snapshot |

At most six exact-issue and two related escalation logs are opened, each at
most 64 KiB. Their exact filenames are derived from escalation receipt
timestamps: `escalation-YYYYMMDD-HHMMSS.log` (UTC). Missing/oversize files are
reported as gaps, not guessed from neighboring files. This keeps the total
source budget below 3 MiB, with no dependency on the total history size.
Regular files only; FIFO/device inputs cannot block the reader.

The current verdict must have a timezone-aware `generated` timestamp within
two hours of `now`, with at most five minutes of future clock tolerance.
Byte limits, truncation, read hashes, parse failures, and missing evidence are
recorded in the private artifact. A corrupt sent tail blocks a decision rather
than treating its readable subset as complete. No history beyond these bounds
is claimed to have been examined.

### Optional target intent

This is an **existing, explicitly supplied owner decision**, not something the
builder creates or infers from inactivity:

```json
{
  "target": "rappterverse",
  "state": "paused",
  "reason": "The owner scheduled a deliberate maintenance pause.",
  "resume_at": "2030-04-12T18:00:00+00:00"
}
```

`state` is `active`, `paused`, or `retired`; `reason` is required. `resume_at` is
optional, copied to `deadline` without resetting it. Within 24 hours of that
deadline (or after it), the decision is time-sensitive; it still does not wake
anything. A disabled `watchers.openrappter.enabled` flag only disables the port
probe, not the independent spin audit, and is never treated as retirement.

### Optional runtime snapshot

A caller may supply a private JSON receipt from a separately authorized,
bounded, read-only diagnostic. The builder never follows embedded paths or
runs embedded commands. For example, a targeted launchd query, bounded stderr
tail, and process/listener reads can supply:

```json
{
  "subject": "openrappter",
  "observed_at": "2030-04-12T12:00:00+00:00",
  "job": {
    "label": "org.example.gateway",
    "state": "spawn scheduled",
    "runs": 40,
    "last_exit": 1
  },
  "error_kind": "runtime_lock_owned",
  "lock_owner_alive": true,
  "listener_alive": true,
  "lock_owner_pid": 1234,
  "listener_pid": 1234
}
```

Classifying a duplicate launcher requires a non-running job, at least three
runs, nonzero last exit, a runtime-lock error, and the **same live PID** owning
the lock and listener. Old snapshots are identified as historical, not used
to assert a current root cause. A listener is not proof of useful work or of
that process being the missing world's producer.

## Decision rules

The private artifact carries an explicit classification:

* **Broken → repair:** owner intent is active and readable checks measure
  stale expected output. Diagnose the actual producer path first; verifying a
  repair requires genuine new activity, not freshened metadata.
* **Asleep → pause:** an explicit pause exists; silence alone is insufficient.
* **Retired → retire:** explicit retirement authority exists; recommend removing
  only the obsolete target's escalation after approval, preserving receipts.
* **Mismeasured → pause:** the repeated “repairs” were actually recorded diagnose
  attempts with complete exact receipts, or an explicitly exclusive
  single-organism scope contradicts the alert target. This corrects the
  **alert interpretation**, not a claim that the public world is healthy.
* **Inconclusive → pause retries:** no sufficient intent/cause evidence.
  Determine whether the target is still required before repair or retirement.

An issue counter must agree with its latest exact escalation timestamp.
Skipped/external-outage rows are not attempts. Related issue combinations
remain labeled separately. Timeout text proves that no terminal result was
captured, **not** that no work ran or no side effects occurred. Even a
`SENTINEL_RESULT: FIXED` is a claim, not a verified repair: this builder does not
read the `repair.verified` chain. Historical diagnoses are not silently
promoted to current root causes.

`ready` means a bounded decision is available, not permission to execute it.
`blocked` covers unreadable/stale/malformed current evidence, missing exact
receipts for a repair claim, and artifact persistence failures. `suppressed`
means both target checks currently pass; old alerts should not be resent.
Missing evidence never becomes a green verdict.

Ready proposals are concise notifications, not embedded diagnostic reports.
`decision` is a complete, scoped human question, such as “Pause only this
escalation while confirming its target?”—never the internal `pause` enum.
`action` reports the receipts already reviewed and the scoped proposal prepared;
it does not hand the user a clerical log-inspection task or claim a repair ran.
Classification and the machine recommendation remain in artifact metadata.
The artifact's `prepared_plan` identifies the exact issue and unexecuted agent
next step; live services and unrelated watchers remain untouched.

Their four answers and every supplied evidence item fit the shared gate's
650-ASCII-character/90-word target; tests include conservative labels, title,
reason, urgency, and any deadline in that budget. Compact source labels refer
to the explicitly read documents above. The private artifact's `details`
preserves the complete explanations and all attributed observations, with
exact paths, read hashes, and transcripts elsewhere in the same artifact.
The renderer need not truncate answers or silently drop evidence.

This scenario does not implement a separate interruption gate, timezone
policy, quiet hours, daily budget, or sent-history cache. The parent's shared
gate owns those decisions. An urgency flag is only a hint: the optional
owner-supplied absolute wake deadline is also attributed in the brief's
evidence, and stale counters alone do not establish urgent material impact.

Public world/chat freshness and local message delivery are separate.
UNKNOWN rows and “sent unverified” records prove neither delivery nor
non-delivery. The builder never opens `chat.db`, grants permissions, or resolves
UNKNOWN records.

## Privacy, dedupe, and verification

The only writes are JSON evidence files under `artifact_dir`, mode 0600 on
POSIX and replaced atomically there. The adapter must choose private,
untracked application storage with appropriate device access controls.
Artifacts include bounded exact transcripts, source hashes, the
decision, and caveats. They are private runtime artifacts: **never commit them,
copy them into docs/fixtures, or publish them with a report.** Config credentials
and notification recipients are not copied. Supplied runtime snapshots should
likewise omit credentials.

The module, this documentation, and its fixtures are public-safe; a returned
envelope is **not** a public sample. Its source paths and observations may
contain private runtime context. Keep real envelopes and artifacts out of
source control, public app bundles, published examples, and analytics.
All committed examples and tests are synthetic.

The fingerprint identifies the target, check states, classification,
recommendation, intent (including an owner's fixed deadline), receipt outcomes,
and runtime diagnosis category.
Advancing ages, `now`, generated/sent times, repeat-message counts, launchd run
counters, PID changes, and artifact paths are excluded. Clock aging can block
delivery without inventing a new incident identity. The builder does not keep
a sent cache; the parent deduplicates this stable fingerprint. Equivalent
offset/`Z` representations of a fixed owner deadline have the same identity.

Run the offline, sanitized suite:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_scenario_decision.py
```

To verify against the macOS system interpreter rather than a shell-managed
Python installation, run the same command with `/usr/bin/python3`.

Tests keep scratch beside the test module and remove it afterward; they never
use a system temporary directory, live config, network, or message delivery.
