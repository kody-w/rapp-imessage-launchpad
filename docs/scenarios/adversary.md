# Scenario 8 — adversary of the favorite plan

`scenarios.adversary.build(context) -> dict` challenges one concrete dependency
of the current initiative: **ten scenario producers share a transport, but a
successful handoff / sent entry is not necessarily verified delivery of that
message**. Delivery confidence affects every producer. The alternative
assumption, “ten producers imply ten useful messages,” cannot be tested from
delivery receipts; this producer does not pretend to have user-value labels.

Runtime: **Python 3.9+ with the standard library only**, verified using Python
3.9.6. UTC `Z` timestamps are normalized to `+00:00` before `fromisoformat`;
no Python 3.10+ annotation syntax or third-party dependencies are required.

## Configuration

The RAPP iMessage Launchpad protocol adapter supplies normalized context.
Construct device-specific defaults at that boundary, not in this producer:

```python
from datetime import datetime, timezone
from pathlib import Path

home = Path.home() / ".storykeeper" / "home"
context = {
  "home": str(home),
  "artifact_dir": str(home / "state" / "scenario-artifacts" / "adversary"),
  "now": datetime.now(timezone.utc).isoformat(),
  "sources": {
    "adversary": {
      "sent": "state/outbox-sent.jsonl",
      "unverified": "state/outbox-unverified.jsonl",
      "unknown": "state/outbox-unknown.jsonl",
      "unknown_resolved": "state/outbox-unknown-resolved.jsonl"
    }
  }
}
```

All four source overrides are optional; the displayed relative paths are their
defaults, resolved against `home`. Absolute local paths also work. Other
scenarios' `sources` keys are ignored; unsupported keys inside `adversary` block
the build. `now` must be timezone-aware ISO8601. The producer does not use wall
time to invent urgency or a deadline. No additional dependencies or config
changes are required.

`build` requires `home` and `artifact_dir` from context; it never discovers an
ambient user's home or silently falls back to another device's private state.
The adapter can instead supply a configured home and any private artifact
directory outside the public checkout. The context and returned envelope are
ordinary JSON objects with path strings; no SDK-specific types are required.
No real usernames, recipient identifiers, network addresses, personal project
paths, calendar excerpts, or message text are bundled; fixtures are synthetic.

### Onboarding and packaging boundary

Device permission onboarding, transport configuration, authentication and
delivery remain the app/shared runner's responsibility. This producer never
requests Automation or Full Disk Access, accesses the real Messages database,
installs jobs, or sends a permission-test message. An unreadable source stays
explicitly unreadable; completing onboarding never authorizes a duplicate send.
The same scenario API can run as a local read-only producer in the protocol
adapter, including on hosts without Messages or `osascript`.

Bundle the pipeline's `outbox.py` alongside the `scenarios` package so the probe
can inspect the corresponding implementation. Missing or incompatible source
returns `blocked/inconclusive`; a packaged app must not replace that result with
a fabricated success. No separate transport, SDK fork, or publishing step is
introduced here.

## Actual, bounded experiment

Each build reads the checkout's `outbox.py` and AST-extracts its actual `_send`
and `_delivered_count` definitions, recording the source SHA-256 and line
ranges. It does **not** import `outbox`, `paths`, `verify_outbox`, or the shared
runner; their import-time/live state effects are therefore avoided.

The extracted functions run against six independent in-memory SQLite fixtures:

| Trial | Injected receipt | What it tests |
| --- | --- | --- |
| Positive control | Matching outgoing sent **and delivered** row | Harness can recognize a genuine fixture delivery |
| Negative control | No row; mock osascript returns zero | Harness can distinguish transport acceptance from no receipt |
| Sent, not delivered | Matching `is_sent=1`, `is_delivered=0` row | A sent count is not a delivered count |
| Unrelated delivery | Delivered row for another message to the same handle | A recipient-wide count increment does not identify this message |
| Unreadable, dropped | Unreadable query, no row | Success with an “unverified” warning is not delivery proof |
| Unreadable, delivered | Same unreadable query, matching delivered row | The same observation also cannot prove non-delivery |

The independent fixture oracle requires the synthetic recipient, exact text,
outgoing/sent/delivered flags, and zero error. It measures what was injected,
not a real recipient's state. No fixture has a real phone number or address.
The subprocess global is replaced **only in the extracted namespace** by an
inert method; no process is launched and no global transport is monkeypatched.
SQLite lives solely in memory. Each trial allows one synthetic dispatch, at
most 64 SELECTs, and 40 virtual sleeps (10 virtual seconds, no real sleeping).
The six-trial total, controls, outcomes, query/poll counts, source hash, and
elapsed milliseconds are persisted. Source changes that cannot run safely in
the restricted harness produce an inconclusive result, not a guessed verdict.

This is a text-only function-level experiment, **not** a full drain/runner
integration test or a measured live delivery-failure rate. It does not test
attachments, races, recovery, delivery latency, recipient reading, or usefulness.
The checked-out implementation is identified by hash; the active worker's
deployed code version is not independently observed.

## Evidence, verdict, and useful change

The JSONL readers never call outbox status, recovery, reconciliation or drain.
Each source is bounded to 2 MiB / 2,000 physical lines. Provenance includes its
path, snapshot/prefix SHA-256, coverage state, invalid-line count, and selected
record line numbers / hashes. Counts are explicitly a bounded sample, not
necessarily lifetime totals. Missing, unreadable, changing, corrupt and
truncated inputs stay visible rather than becoming silent zeros.

Coverage is limited to the legacy flat outbox JSONL schemas described here.
This producer does not interpret the Launchpad SDK's chain-wrapped receipt
events or verify its append-chain hashes. Source overrides relocate inputs;
they do not convert schemas. SDK receipt support requires an explicit, tested
mapping from its documented schema. A successful plugin/renderer smoke check
does not expand this coverage or authorize sending against real shared history.

An explicit `unverified` marker differs from an unmarked row lacking proof.
`recorded_verified` requires the existing verifier's structured
`Messages/chat.db` evidence (`message_rowid`, 0–15-second delta, `verified_at`);
even that is a **recorded verification claim**, not independent confirmation.
A later recorded verification supersedes an old unverified marker. Unknown
resolution records are matched by identity and conflicting resolutions remain
uncertain; an operator resolution is not counted as a verified-delivery row.
Missing resolution evidence does not establish non-delivery.

* **Refuted / ready:** controls pass, at least one actual probe success lacks a
  matching delivered fixture row, and usable local receipts establish relevance.
* **Survived / suppressed:** controls pass with no counterexample in these
  fixtures; this is not universal proof.
* **Inconclusive / blocked:** controls/source compatibility fail, there are no
  usable local receipts, context is invalid, or evidence cannot be persisted.
  A mock-only result is retained separately when live evidence is absent.

The returned JSON-safe envelope has `scenario="adversary"`, status, title,
change, impact, action, decision, evidence, artifacts, fingerprint, urgency and
reason. `decision` is a complete user-facing need, not a verdict enum;
`refuted` / `survived` / `inconclusive` remain in the private artifact's `verdict`.
`action` describes the mock trials and saved evidence, then recommends a practical
safeguard; it does not claim a transport change was deployed. Failed persistence
never claims evidence was saved. `urgency` is routine. Its semantic fingerprint
depends on the hypothesis, verdict and recommendation version, **not** clock,
artifact/source paths, source hash, or changing receipt counts. The shared
runner owns transport and deduplication; this producer does neither.

Ready notification fields are concise ASCII: the title, four answers, reason
and selected attributed observations fit a 550-character / 75-word producer
budget, leaving room for the shared renderer's labels within its 650-character /
90-word target. This is a formatting regression check, not a separate policy
gate. Complete counts, source paths, hashes, coverage and uncertainty remain in
the private artifact. Its `notification_source_map` resolves short notification
source labels to the full provenance objects. Artifact paths are not notification
body text. Routine urgency does not bypass quiet hours; there is no fabricated
deadline, elapsed-time resend, or scenario-local delivery history.

The actionable change is to separate accepted / attempted / verified / unknown
in shared-runner reporting and require unique recipient/content/time-matched
delivered evidence. **Keep uncertain sends terminal and reconcile read-only;
never resend merely because proof is missing.** This module deliberately does
not modify transport, permissions, configuration, or live ledgers.

Only `artifact_dir/adversary-experiment.json` is written, via an atomic
same-directory replacement requesting mode `0600` (on POSIX). Other platforms
must provide a private app-data directory protected by their native ACLs.
The report contains redacted aggregates,
provenance and synthetic measurements, not message bodies or recipients. Keep
it private and outside git. Source paths and snapshot hashes are still private
runtime evidence: do not publish reports, generated envelopes, logs or tested
device contexts as public app fixtures. No runtime artifacts are written beside
the code or in system temporary directories.

## Validation

From the checkout root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s tests -p 'test_scenario_adversary.py' -v
```

On macOS, use `/usr/bin/python3` in that command to verify the installed sender's
interpreter rather than a different Python on `PATH`. The timestamp regression
test covers UTC `Z`, explicit UTC, and positive/negative offsets on Python 3.9.

Tests use synthetic files in individually owned, cleaned-up directories under
the checkout working directory, never system temporary directories. They cover
actual falsification, positive/negative controls, honest survived/inconclusive
classification, missing/malformed/bounded sources, private provenance,
no-real-transport/read-only behavior, source overrides, and stable fingerprints.
Protocol roundtrips, context-only home selection, relocated Unicode paths,
CRLF receipt hashes and absence of POSIX-only flags are covered with sanitized
fixtures. FIFO and POSIX permission assertions are platform-conditional.
