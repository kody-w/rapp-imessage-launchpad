# Scenario 7: a usable Storykeeper incident brief

## Chosen improvement and success criteria

The approved scope is message usefulness, not repairing watched platforms.
Inspection of the existing `sentinel.notify` path found a private-network report
URL appended to messages. Its human-escalation path slices the summary at 400
characters. Neither is a portable, complete explanation. A sent-ledger entry can
also contain `unverified`; it is not proof of delivery to a phone.

Before building, success is defined as:

1. Read only bounded, local Storykeeper inputs; run no checks, commands, models,
   network requests, configuration changes, publishing, or messaging.
2. Produce a plain-text brief and a self-contained HTML ZIP from a fresh,
   internally consistent `last_verdict.json`. Every failed check must have its
   exact ID, complete detail, plain-language interpretation, and read-only next
   step in both formats. Unknown check IDs get an explicit generic interpretation,
   never an invented diagnosis. Known check titles name the area needing review;
   an unreadable observation is not relabelled as a confirmed platform outage.
3. Compare check-ID coverage and portability against the latest matching
   recorded message when available. Do not compare changing age strings as if
   they proved truncation, or call a private address unreachable without a probe.
4. Read the written text and ZIP back, parse the HTML, and verify evidence
   completeness, escaping, absence of active/external resources, ZIP integrity,
   and a measured acceptance receipt before returning `ready`.
5. Keep source state untouched, all generated files private under `artifact_dir`,
   and delivery under the existing shared outbox. Deleting the generated bundle
   reverses the change.

## Contract

```python
from datetime import datetime, timezone
from pathlib import Path
from scenarios.win import build

home = Path.home() / ".storykeeper" / "home"
result = build({
    "home": home,
    "artifact_dir": home / "state" / "scenario-artifacts" / "win",
    "now": datetime.now(timezone.utc).isoformat(),
    "sources": {},
})
```

The host app/SDK constructs this context. The producer itself has no implicit
profile, phone, device, repository, or environment-derived home default.
An alternate `home` and explicit `sources` override the caller's defaults.
Missing required context is suppressed, not replaced with a developer's path.

`build(context: dict) -> dict` returns `scenario`, `status`, `title`, `change`,
`impact`, `action`, `decision`, `evidence`, `artifacts`, `fingerprint`, `urgency`,
and `reason`. This producer uses `routine` urgency: it delivered a communication
improvement, not a fresh independent diagnosis. Source severity stays in the
brief; it does not manufacture a deadline.

The optional `sources` dictionary supports:

| Key | In-memory fixture or override | Default |
| --- | --- | --- |
| `last_verdict` | Parsed verdict dictionary or local JSON path | `home/state/last_verdict.json` |
| `outbox_sent` | List of message dictionaries or local JSONL path | `home/state/outbox-sent.jsonl` |

Paths are relative to `home` unless absolute, must remain inside that home after
symlink resolution, and must identify regular files. An explicit `None` disables
that source. Unrelated source keys are ignored. Omitted `sources` is equivalent
to `{}`. Inline fixtures obey the same size and shape limits as file inputs.

Required verdict fields follow the actual health producer: `generated`, `status`,
`checks`, `failed`, and `critical`. Each check has `id`, boolean `ok`, `severity`
(`warn` or `critical`), and string `detail`. Failed details must be nonempty.
Declared failed/critical lists and status must agree with the check records.
An empty, stale, future-dated, malformed, or inconsistent verdict is not an
incident brief.

Bounds: 256 KiB verdict, 128 check records, 8,192 characters per check detail,
64 KiB newest sent-ledger tail, and 128 candidate messages. The verdict may be
at most six hours old or five minutes ahead of `now`; timestamps must include
their timezone. No recursive discovery or transcript loading is performed.
The baseline uses the newest usable matching record in that bounded tail;
malformed, unrelated, timestamp-free, and future-dated records are skipped.

## Results, deduplication, and safe delivery

`ready` means usable artifacts passed their read-back acceptance checks. It does
**not** mean the underlying incidents were repaired, the current checks were
rerun, or a phone received anything. A missing/unreadable message baseline is
labelled unavailable; it cannot become a made-up before/after result.

`suppressed` means no usable current incident or no safely validated artifact.
These are producer/data issues, not reasons to ask the user for a decision.
This implementation does not invent `blocked` decisions to report progress.
A healthy verdict produces no files.

The semantic fingerprint depends on the brief format and the sorted failed
check IDs/severities, not timestamps, paths, check order, changing ages/counters,
or message share tokens. It changes when a finding enters/leaves the set or its
severity changes. This deliberately identifies the communication improvement
for that incident set, not every fresh measurement of the same incident.

Artifacts live in a content-addressed child directory of `artifact_dir`:

* `incident-brief.txt`: complete, immediately readable without extracting a ZIP.
* `incident-brief.html.zip`: `incident-brief.html` only; inline styling, no scripts,
  external resources, links, refresh, or form submission.
* `acceptance.json`: sizes, SHA-256 receipts, actual measured acceptance, input
  provenance, baseline coverage, and limits.

The return value's `artifacts` lists the text and ZIP, which can be passed to the
existing outbox as attachments. The acceptance receipt is named in `evidence`
and remains alongside them for local verification. The producer neither imports
nor calls `outbox`, `sentinel`, or `standup`. In particular, it never invokes the
report-publishing path or reads a contact/configuration file. Supplying a ZIP
avoids the existing outbox's HTML wrapping/deletion behavior.

Use the shared renderer for the result's concise message. Do not truncate the
brief or append another private report URL. The complete check evidence belongs
in the attachments, not another wall of opaque check IDs in the message.
The notification retains all four answers and three attributed observations
within 650 ASCII characters and 90 words, including conservative field/source
labels. Tests also bound the serialized visible envelope, 128 findings, long
Unicode source paths, and a missing message baseline. `last_verdict` and
`outbox_sent` are source aliases resolved by `acceptance.json`'s `provenance`;
the receipt itself is beside the first returned artifact. Full paths and full
measurements remain in the private artifacts, not the short message.

Win adds no gate policy: the shared gate owns timezone, quiet hours, daily limits,
and queued-history deduplication. Urgency remains `routine`; no deadline or
urgency flag is fabricated to bypass quiet hours. Rebuilding a snapshot does
not create a new fingerprint merely because time elapsed.
The verdict's `generated` timestamp is explicitly labelled `sampled at` in the
notification so the canonical gate also ignores this sampling clock when
comparing claims and delivery keys. Actual incident dates remain intact in the
complete evidence; new failed findings still produce an eligible update.

Output files contain private observations and must not be committed or published.
Delivery verification, deduplication, retention, and approved recipients remain
the shared pipeline's responsibility. Do not delete artifacts while queued.

## Public app / protocol adapter boundary

Only the producer, documentation, and synthetic tests belong in the public app.
Neither actual input snapshots nor generated results/briefs are public assets.
Use a runtime artifact directory outside the checkout, derived from the app's
private per-user data directory or the explicit context, and exclude it from any
publication/export manifest. Do not package local fixture-run directories.

On POSIX hosts the producer creates directories as `0700` and files as `0600`
and checks those permissions when reusing them. On other hosts the adapter must
provide an artifact directory protected by the user's OS ACL; the receipt states
this inherited-ACL assumption instead of claiming POSIX permission enforcement.
Binary file flags are selected portably. Symlink fixtures are skipped when the
test host cannot create them; the producer still rejects symlink artifacts.

This module requires only read access to the selected state inputs and write
access to `artifact_dir`. It never requests Contacts, Calendar, Messages,
Automation, or Full Disk Access permissions. Device permission onboarding,
recipient authorization, and the standardized send protocol belong to the host
app/shared pipeline, not to scenario `win`. The same `build(context)` contract
can be called by that adapter without giving this producer messaging authority.

## Validation and rollback

Requires **Python 3.9 or newer, standard library only**. The producer and its
tests run on the installed macOS sender's Python 3.9.6; no interpreter upgrade
or package installation is needed. ISO8601 `Z` timestamps are normalized to
`+00:00` before `datetime.fromisoformat`, and the API uses no PEP 604 union-type
annotations.

Run the sanitized tests from the checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_scenario_win.py -v
```

For a macOS sender that uses the system interpreter, verify that exact runtime:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -p test_scenario_win.py -v
```

The tests create and remove owned fixture directories under
`scenarios/win_assets/`; they do not use system temporary directories.
They exercise the actual health/outbox shapes, read-back acceptance,
malicious HTML, missing/inconsistent/stale inputs, file boundaries,
semantic deduplication, and source immutability.
The shared-gate regression imports the real `scenarios.interrupt` when integrated.
For an isolated producer checkout, set `RAPP_WIN_TEST_GATE` to the canonical gate
module's path before running the suite; without either, that integration test
explicitly skips. Publication validation must run it without a skip. It models
queued history in memory, verifies timestamp-only suppression and stable delivery
keys, and admits an added failure without writing to a real queue.

A safe live invocation uses the same `build` API with the real home and a private
session artifact directory. Read the resulting text and extract the ZIP
locally to verify use; no server or browser connection is required.

Rollback: stop selecting `win` in the parent runner and remove only its generated
content-addressed directory after pending delivery has ended. No daemon, source
repository, configuration, or outbox state needs reverting.
