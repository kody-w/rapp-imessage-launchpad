# Scenario 9: make machines explain themselves

`scenarios.timeline.build(context)` is a stdlib-only, **read-only** timeline builder.
It supports **Python 3.9 or newer**, including the macOS system Python 3.9.6 used
by the installed sender. No third-party dependencies or newer annotation syntax
are required. UTC `Z` timestamps are explicitly converted to `+00:00` before
`datetime.fromisoformat`, without relying on newer interpreter permissiveness.
It returns the shared JSON-safe advisory envelope; the parent owns any sender.
It never calls health checks, subprocesses, SSH, launchctl, network APIs, permission
probes, or enqueue/send functions. No repair, deployment, service change, or
publication is performed.

```python
from datetime import datetime, timezone
from pathlib import Path

from scenarios.timeline import build

result = build({
    "home": str(Path.home() / ".storykeeper" / "home"),
    "artifact_dir": str(Path.home() / ".storykeeper" / "private-artifacts" / "timeline"),
    "now": datetime.now(timezone.utc).isoformat(),
    "sources": {"timeline": {}},
})
```

The Launchpad protocol adapter supplies this context. The conventional home is
chosen **by the caller**, not inferred by the scenario: an absent `context.home`
blocks instead of falling back to another user's profile or the checkout.
Every default log/receipt path is relative to the supplied home; explicit
`sources` replace the corresponding defaults. Do not ship a user's resolved
paths in a public configuration or fixture.

## Source schema and boundaries

`sources.timeline` (or a directly supplied `sources` object) accepts these keys.
Each accepts a path string or a list of path strings; `[]` explicitly disables
that source role. Relative paths are relative to `home`.

| Key | Default | Recognized evidence |
| --- | --- | --- |
| `logs` | Current and previous UTC month `logs/sentinel-YYYY-MM.log`, plus `logs/launchd.err.log` | Native `[ISO8601] status=… failing=['check_id']` / `failing=none`; `[ISO8601] verified fixed: […]`; JSON/JSONL snapshots |
| `verdict` | `state/last_verdict.json` | `generated`, `failed`, optional `checks: [{id, ok: bool, severity, detail}]`, `critical` |
| `last_run` | `state/last_run.json` | `at`, `failed`, optional `status` |
| `repair_receipts` | `state/escalations.json`, `neighborhood/copilot/chain.jsonl` | Native escalation arrays and `rapp/1` `sentinel.tick`, `neighbor.acted`, `repair.verified` frames |
| `deployments` | `state/deployments.jsonl` | Explicit saved deployment records described below; missing records are reported |
| `launch_agents` | `~/Library/LaunchAgents/com.rapp.storykeeper.plist` | Saved plist schedule with `EnvironmentVariables.SENTINEL_HOME` matching `home` |
| `device_receipts` | None | Explicit **already-existing local** JSON/JSONL receipts identifying a non-`local` `device`; no device discovery or contact |

Only regular files contained in `home` are opened, except selected plist files
under the local `~/Library/LaunchAgents`. Traversal and symlink escapes are
rejected. The supplied `home` can be an isolated fixture directory for tests.
No recursive search, config/credential dump, external checkout inspection, or
following paths/commands embedded in receipts takes place.

For permission onboarding, the app/SDK must supply only authorized source paths;
set ungranted optional roles to `[]`. This module does not grant permissions,
open onboarding dialogs, discover another device, or contact a sender. A denied
read remains an explicit evidence gap rather than prompting a permission probe.
Another device contributes only previously saved receipts selected by the caller.

Reads are bounded to eight files per role, one MiB per file, and 6,000 rows per
file. Log/JSONL files use complete retained tail lines; oversized whole JSON/plist
files are skipped. Missing, malformed, undated, future-dated, truncated, and
unassociated evidence appears in `evidence_gaps` and envelope evidence. A missing
file does not mean a successful check. Filesystem mtimes never become event times.

### Normalized saved records

JSON files hold one object or an array; JSONL holds one object per line.
Timestamp fields are `at`, `utc`, `generated`, or `timestamp`: offset-aware ISO8601
only, normalized to UTC. Multiple timestamp fields must agree. `Z`, offsets,
fractional seconds, and out-of-order records are handled by instant, not lexical
or file order. Free-text log timestamps without a timezone are not guessed.

```json
[
  {"at":"2026-09-19T17:00:00Z","failed":["queue_stalled"],"critical":[]},
  {"at":"2026-09-19T17:05:00Z","kind":"deployment","checks":["queue_stalled"],"revision":"abc1234","previous_revision":"def5678"},
  {"at":"2026-09-19T17:15:00Z","mode":"diagnose","key":"queue_stalled","result":"UNKNOWN"},
  {"at":"2026-09-19T17:20:00Z","kind":"repair.attempt","checks":["queue_stalled"]},
  {"at":"2026-09-19T17:25:00Z","kind":"repair.verified","issue":"queue_stalled","cleared":[],"still_failing":["queue_stalled"],"landed":false}
]
```

Use snapshots in `logs`, deployments in `deployments`, and attempts/probes in
`repair_receipts`. Native frames carry `utc`, `kind`, and the corresponding
`payload`. Check association is an explicit `checks` list or comma-separated
`issue`/`key`; nearby unrelated events are not silently assigned to a failure.
Other-device receipts use the same shapes plus, for example, `"device":"lab-mini"`,
and must be selected through `device_receipts`. Their clocks and assertions are
not independently verified. Watcher/agent names in a local chain are not devices.

## Interpretation and causal limits

One primary observation stream per device establishes recurrence: native logs
first, local `sentinel.tick` history second, explicitly saved device receipts
third. Prefer a stream with at least two distinct retained timestamps over a
single retained snapshot. A repeated timestamp counts once. Chain/verdict/last-run mirrors of the
same run do not manufacture a second failure. Verdicts and saved repair probes
can update an existing episode's observed state but do not inflate its count.

At least two distinct primary failure samples are required for a check to
recur; those samples may span an observed clearance. Select its latest observed
episode. Rank checks by their latest failure, then newest episode onset. Exact
ties are displayed deterministically and disclosed, not called causal priority.
Same-instant contradictory observations are reported; a contradiction at or
after the selected latest failure blocks readiness.

The earliest actionable event is the first **retained failure observation in
the latest episode**, not a guessed outage start or a preceding deployment.
Absent prior clear evidence, actual onset is unknown. Absence from a later
failure list is labeled `not_listed_as_failing`, not a proven permanent repair.

Deployment precedence is a hypothesis only. A later deployment cannot explain
an earlier observation merely by proximity. `w_sentinel_current` revision
metadata describes code state when checked, not when deployment occurred.
Diagnostic claims (including “root cause measured” or “FIXED”) are not copied
into causal conclusions. `repair.verified` is a recorded probe result, not proof
that an earlier repair caused it. The builder intentionally reports
`root_cause: "not established"`; proving causation requires evidence beyond
these observational sources. Schedule plists prove configuration only, not
that a job was loaded, fired, or succeeded. No next-run deadline is invented.
Ordinary check detail is labeled as a saved symptom, not an independently
verified explanation; explicit cause/fix claims are withheld from advisory text.
For simple interval/minute schedules, the artifact compares configured reporter
cadence with median spacing of failure samples in the latest episode. The
reporter's schedule is not assumed to be the failing target's schedule.

The safest recovery recommendation is read-only comparison with prior good
records and repair probes, followed by an owner-reviewed reversible proposal.
It never recommends replaying a diagnosis, deploying a merely newer revision,
or restarting unrelated services as a demonstrated fix.

## Envelope, artifacts, and verification

The envelope includes `scenario`, `status`, `title`, `change`, `impact`, `action`,
`decision`, `evidence: [{source, observation}]`, `artifacts`, `fingerprint`,
`urgency`, and `reason`. An active, fresh recurrence is `ready`; a historical
episode no longer observed failing is `suppressed`; missing recurrence evidence,
recent contradictions, stale active evidence (over six hours), invalid context,
or artifact-write failures are `blocked`. `ready` means a review is supported,
not that a cause or fix is known. Source criticality remains in the artifact,
but notifications are `routine`: a severity flag alone is not an observed
deadline or concrete active material impact. Age never becomes urgency.

Ready proposals use compact ASCII text and two attributed observations so the
shared renderer can preserve all four answers and every supplied observation
within its 650-character / 90-word budget. Regression fixtures reserve room for
renderer labels (at most 600 characters / 80 words with additional labels).
Short source IDs resolve to full paths in the artifact's `notification_sources`;
the complete explanation and evidence are retained in `detailed_proposal` and
the Markdown report. Missing evidence and ambiguity are still disclosed in the
compact reason. Long display identifiers are shortened only for the notification,
never in the analysis or semantic fingerprint.

The human-facing decision requests no repair approval while cause remains
unproven; internal decision keys are not notification text. The agent prepares
a `recovery_plan` in the private artifact, anchoring the earliest actionable
event to its timestamp and source, retaining any prior not-failing observation,
and stating the safest evidenced option: leave services unchanged and compare
the next saved check from the already-authorized sources. This is a prepared
read-only follow-up plan, not a new probe, scheduled action, or repair claim.
Wording-only changes do not change the incident fingerprint or create a resend.

Interruption policy stays in the shared gate. This scenario implements no daily
quota, quiet-hour timezone policy, queued-history dedupe, or elapsed-time resend.

`timeline-evidence.json` and `timeline.md` are written **only to `artifact_dir`**.
They include source inventory, UTC-ordered observations, hypotheses, correlations,
earliest action, repair outcomes, scope limits, and evidence gaps. No live data
belongs in source control. Artifacts may contain local operational details; they
are not a publication package. The adapter must choose a private, untracked
runtime directory outside the application source/package and never bundle
these files with the public app, protocol examples, or SDK fixtures. On POSIX,
new artifact directories are owner-only and output files are enforced as mode
`0600`, including when replacing an existing file; existing parent-directory
permissions are not modified. The host app owns platform-specific storage ACLs.

The SHA-256 semantic fingerprint excludes age prose, sample counts, latest poll
timestamps, generated time, and filesystem/artifact paths. It changes for a new
episode after an observed clear sample, selected check/device, assessment state,
material recorded revisions/repair outcomes, or explicit scope change. Crossing
the freshness boundary changes the assessment, not merely its age wording.

Run the sanitized realistic fixtures without dependencies:

```sh
python3 -m unittest discover -s tests -p 'test_scenario_timeline.py' -v
```

On macOS, also validate with the sender's actual interpreter rather than assuming
the shell's `python3` resolves to the same installation:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -p 'test_scenario_timeline.py' -q
```

Tests create and clean isolated project-local `.timeline-test-data` fixtures;
all observations, identities, revisions, and schedules are synthetic. Tests
never use system temporary directories or inspect live devices. Portability
regressions cover caller-selected homes, source overrides, lack of implicit
profile fallback, private artifact modes, and embedded personal-path/IP/phone
patterns in the three owned public files.
