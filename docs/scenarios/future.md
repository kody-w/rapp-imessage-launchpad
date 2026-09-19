# 1. Send me a message from the future

`scenarios.future.build(context)` prepares **one conditional seven-day forecast**
and one private, reversible recovery-first checklist. It returns the shared
JSON-safe envelope; the shared runner alone decides whether/how to deliver it
through the existing iMessage pipeline. This module does not import transport,
read Messages, enqueue, run checks/commands, contact anyone, or alter live state.

Runtime: **Python 3.9+ standard library only**, verified with Python 3.9.6.
UTC `Z` suffixes are normalized to `+00:00` before `datetime.fromisoformat`;
the module does not require newer annotation syntax or Python dependencies.

## Authorized inputs

`context` supplies absolute `home`, private `artifact_dir`, a timezone-aware
ISO8601 `now`, and optional `sources`.

The public Launchpad/SDK adapter owns device onboarding and constructs this
context. It may derive its default home from
`Path.home() / ".storykeeper" / "home"`; the scenario itself never discovers
the host's home or substitutes a personal path. An absent `context["home"]`
blocks the build. Context paths and explicit sources make the same module
usable on another device without modifying the code or adding Messages access.
All examples and committed fixtures are synthetic; private runtime receipts
and artifacts must stay in the adapter's private, untracked output directory.

Only these exact files are opened:

| Source key | Default / format |
| --- | --- |
| `future_plan` | `<home>/state/future-plan.json`, explicit local plan below |
| `future` | Alias for `future_plan`; the latter takes precedence |
| `future_verdict` | `<home>/state/last_verdict.json`, native `health.py` verdict |
| `future_history` | An absolute native sentinel log path, or 1–4 exact paths |

Without `future_history`, only `<home>/logs/sentinel-YYYY-MM.log` for months
intersecting the preceding 48 hours is considered (at most two files). No
directory discovery, recursive scans, configuration/contact reads, or paths
embedded in source content are followed. Symlink traversal is refused. Explicit
source files may be outside `home`; supplying a path is the authorization, and
does not authorize reading its siblings. Input content is data, never commands.

The native verdict parser uses `generated`, `status`, `checks[]` with
`id`/`ok`/`severity`, and the corroborating `failed[]` list. This is the shape
written by `health.py` and persisted by `sentinel.py`. It does not mistake
`last_run.json.at` for the verdict timestamp. Native status logs are parsed as:

```text
[2026-09-19T18:25:00+00:00] status=critical failing=['export_valid']
[2026-09-19T18:30:00+00:00] status=healthy failing=none
```

JSON files are limited to 1 MiB; each history read is limited to its last
256 KiB. Other log lines are ignored, not mined for instructions or forecasts.
Missing/corrupt/stale evidence is not interpreted as a healthy system.

## Explicit project/dependency plan

This synthetic example is not runtime evidence. The scenario **does not create
or populate this plan in live state**. An authorized owner supplies an existing
plan or configures `future_plan` to point to one:

```json
{
  "schema": "storykeeper.future-plan/v1",
  "updated_at": "2026-09-19T18:00:00Z",
  "projects": [
    {
      "id": "weekly-export",
      "title": "Weekly export",
      "status": "active",
      "deadline": "2026-09-20T02:30:00Z",
      "impact_level": "high",
      "impact": "The scheduled internal report would miss its review window.",
      "dependencies": [
        {
          "check_id": "export_valid",
          "required": true,
          "recovery_hours": 8,
          "downstream_hours": 2,
          "recovery_basis": "Maintainer's remaining-work estimate after reproducing the failure."
        }
      ]
    }
  ]
}
```

- Up to 50 projects and 16 dependencies per project.
- Project statuses: `active`, `completed`, `cancelled`. Inactive projects need
  only their unique `id` and `status`.
- Impact levels: `critical`, `high`, `medium`, `low`. Impact descriptions and
  estimates are assertions from this explicit plan, not independently verified
  facts. All active deadlines and the plan timestamp must carry timezones.
- `required: true` means **a positive result of this exact check is a delivery
  prerequisite**. It does not claim that a nonpassing check proves a service
  outage. Optional dependencies cannot establish this mechanism.
- `recovery_hours` is the estimated **remaining** recovery work if started now,
  not total historical repair time; it must be positive.
  `downstream_hours` is remaining dependent work after recovery, possibly zero.
  Both are finite and at most 8,760 hours. `recovery_basis` is required.
- A plan older than 24 hours, a verdict older than two hours, and future-dated
  observations block forecasting. No date, causal link, consequence, or
  estimate is inferred from a failure's age.

## Forecast and uncertainty gates

An event is eligible only when its deadline lies in `(now, now + 7 days]`.
Its explicitly required check must currently be nonpassing. At least two
distinct observations must corroborate that result across 15 minutes, with no
intervening missing/passing result and no observation gap greater than two
hours. Repeating the same timestamp is not corroboration. A more recent
healthy log invalidates an older failed snapshot's persistence claim.

The remaining recovery plus downstream-work estimate must meet or exceed the
deadline runway. This is the causal mechanism: an unfulfilled required gate
occupies the delivery's critical path with insufficient estimated time.
The forecast says **likely if unchanged**, with moderate conditional
confidence, **not** a numerical probability or guarantee. Visibility failure,
faster recovery, unrecorded progress, or a changed plan can invalidate it.

Supported candidates are ranked by declared consequence, then earliest
deadline, project ID, and check ID. Exactly one wins. Missing required-gate
evidence for an in-horizon project blocks the ranking rather than presenting a
partial view as complete. A candidate without persistent corroboration is not
called likely. The scenario does not attempt to discover unknown projects.

`ready` includes the actual event `deadline`, an explicitly dated seven-day
horizon in `change`, traceable evidence, one `decision`, and one artifact.
Notification evidence uses stable `plan:<project-id>` and
`verdict/log:<check-id>` source labels. Their exact file mappings, dated
observations, recovery-estimate basis, and full caveats are retained in the
private checklist. The concise plan observation preserves the absolute
deadline and consequence; the gate observation attests only to repeated
nonpassing results, never a proved service outage.

The complete notification (title, four answers, all notification evidence,
and deadline) must fit **650 ASCII characters and 90 words**. A conservative
ASCII-expansion preflight blocks oversized titles/consequences rather than
truncating an answer or evidence. Nothing is automatically sent. Notifications
are `time_sensitive` only when the observed deadline is within 24 hours,
otherwise `routine`; the shared gate still owns policy, admission, and quiet
hours. This scenario never marks a forecast `urgent`.

`suppressed` means no supported in-horizon risk was found in valid inputs;
it is not a guarantee of safety. `blocked` means evidence, configuration, or
artifact persistence is inadequate. Non-ready envelopes have no decision,
deadline, or artifacts, so the delivery owner has nothing speculative to send.

## Reversibility, privacy, and deduplication

The only write is `future-<semantic-digest>.md` in `artifact_dir`. On POSIX the
file is mode 0600 and a newly created artifact directory is mode 0700. Platforms
without POSIX access modes must receive an ACL-protected per-user directory
from the adapter; mode arguments alone do not establish Windows ACL privacy.
Platform-specific file-open flags are optional, and source/artifact contents
are UTF-8. A same-directory staging file is atomically replaced and cleaned up.
The checklist proposes reviewing
the gate/estimate and prioritizing dependency recovery; it changes no project,
calendar, configuration, or system. Deleting the draft reverses all preparation.
The one decision is whether to prioritize this recovery-first checklist.

The fingerprint covers the event kind, project identity, exact UTC deadline,
required check, remaining-work estimates, and declared consequence. It
excludes current time, advancing ages, observation counts/timestamps, prose
health details, source paths, and artifact paths. A fresh tick of the same
semantic risk is not a new message. Changed deadlines or causal estimates are
new events. No private native evidence or contact information belongs in git.
Compact observation text also excludes advancing clocks and sample counts.
An updated evaluation window or another identical nonpassing sample alone
must not become substantive new evidence for a queued-history resend.

## Verification

From the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_scenario_future.py' -v
```

On macOS, verify with the installed sender's interpreter rather than a newer
development Python:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -p 'test_scenario_future.py' -v
```

When `scenarios/interrupt.py` is installed, the suite also runs ready fixtures
through the actual shared `evaluate` and `render`, including queued-history
dedupe after clock advancement. In an isolated scenario worktree, point
`RAPP_FUTURE_TEST_GATE` to that exact gate file to run these integration cases.
Only these integration cases skip when no gate is supplied; tests are never
run at plugin import or automatically during application startup.

Fixtures preserve the real native verdict/log shapes and exercise missing
evidence, uncertainty, dated horizons, priority, recovery, stable identity,
source bounds, symlink refusal, artifact privacy, and transport-free operation.
Portability fixtures also cover explicit-context-only home selection, absent
platform-specific open flags, and synthetic Unicode content. POSIX permission
assertions run only on POSIX; symlink tests skip if the host cannot create them.
Test scratch data is created only under the repository's ignored
`.scratch-home/` and removed after each test, never in system scratch locations.

A read-only live build may use the canonical Storykeeper home and a
session-owned `artifact_dir`. A live verdict with failures but no explicit
project/dependency/deadline plan must remain `blocked`; it is not permission
to fabricate a seven-day forecast or to send a message.
