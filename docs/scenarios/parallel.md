# Scenario 4: parallel-universe alert-identity experiment

`scenarios.parallel.build(context) -> dict` compares two real alternatives to a
blocked dedupe-policy decision: should repeat alert identity use exact operational
text, or the set of failing conditions? It does **not** change that policy, call
the transport, send a message, import live sentinel modules, publish, or merge.

## Read-only inputs and envelope

```python
from datetime import datetime, timezone
from pathlib import Path

from scenarios.parallel import build

# The caller/SDK resolves device-local defaults, not the scenario module.
home = Path.home() / ".storykeeper" / "home"
private_artifacts = home.parent / "private-artifacts" / "parallel"
result = build({
    "home": str(home),
    "artifact_dir": str(private_artifacts),
    "now": datetime.now(timezone.utc).isoformat(),
    "sources": {
        # Optional; default: home/state/outbox-sent.jsonl.
        "parallel_alerts": str(home / "state" / "outbox-sent.jsonl"),
    },
})
```

`sources.outbox_sent` is also supported, with `parallel_alerts` taking precedence.
Sources are explicit JSONL file paths, not commands or URLs. Each nonempty record
requires `text`, `to`, and timezone-aware ISO8601 `at` (or `sent_at`). Optional
`attachments` is a list of reference strings; optional `pipeline` explicitly
names the message family. Otherwise the first operational-text line names it.
The largest family/recipient group is selected deterministically; unrelated
pipelines are never pooled to manufacture repetition.

The envelope includes `scenario="parallel"`, `status`, `title`, `change`,
`impact`, `action`, `decision`, `evidence`, `artifacts`, `fingerprint`, `urgency`,
and `reason`. Urgency is routine; no deadline is invented.

Ready notification text keeps the four answers and **all** attributed
observations concise. Tests budget at most 550 ASCII characters / 75 words for
the title, labeled answers, and observations, leaving room for the shared
renderer within its 650-character / 90-word ceiling. Nothing is truncated to
make the message fit. Here, `unsafe` counts required emissions wrongly hidden.
`parallel_alerts` identifies the logical input source; its exact authorized path
and content hash remain in private `report.json`, alongside detailed scores and
case evidence. `replay receipts` attributes the measured experiment observations.

* **ready:** both experiments completed, including a measured tie or **neither**.
* **suppressed:** valid history lacks six same-pipeline records and two certified
  visible-condition repeat pairs. No invented fixture replaces missing evidence.
* **blocked:** absent/corrupt/oversized input, invalid context, unsafe output
  location, inaccessible receipts, timeout, or invalid/incomplete worker output.
  Corrupt rows are never silently skipped.

Artifacts must be outside the read-only input home and cannot contain the input
source. All writes stay under the supplied artifact directory. Explicit sources
outside `home` are allowed; callers must authorize them.

### Public Launchpad / SDK adapter boundary

The API is platform-neutral and requires Python 3.9 or later. The app/SDK supplies
the authorized host's `home`, a private untracked `artifact_dir`, `now`, and
explicit source overrides through context. The module never calls `Path.home()`,
scans another device, or falls back to a developer's checkout or personal paths.
Use the SDK's per-device private artifact directory in the app; the example above
is a standalone caller configuration, not an additional scenario default.

Permission onboarding, peer-device trust, native messaging access, transport,
and any eventual send are owned by the app/SDK. This scenario only reads an
already-authorized sent-ledger file. It does not request OS permissions, read
calendar/contact/message databases, or enable a live dedupe policy. Missing or
unreadable evidence produces a blocked envelope, not a broader search.

Committed tests and examples are synthetic and sanitized. Runtime evidence,
receipts, and device paths remain private; do not bundle them in the Electron app,
commit them to a public checkout, or publish them with SDK examples.

## Why these alternatives

The existing `cooldown.py` describes age drift making unchanged alerts look new
and uses check-set identity. The sent ledger contains the actual messages used
here, including age changes and changes within the same checks. This experiment
tests the tradeoff rather than assuming either exactness or semantic grouping is
safe.

| Universe | Identity |
|---|---|
| `exact_text` | Exact operational body plus attachment references |
| `semantic_condition` | Sorted named checks plus exact header and attachment references; exact-body fallback when checks are absent |

Both retain recipient/pipeline boundaries, the same non-sliding six-hour
cooldown, clock-reversal fail-open behavior, and current-condition state.
This cooldown is **only an experiment parameter**, never a request for the
shared transport to resend after six hours. The transport's interruption gate
owns queued-history dedupe, quiet hours, daily caps, and timezone policy.
Semantic identity extracts explicit `check_name:` labels, not arbitrary
underscore-containing prose. It is a production-style check-set candidate,
**not** a claim to understand all semantics and not a byte-for-byte replacement
for the live gate. It deliberately remains capable of failing the safety tests.

A recognized trailing `Static HTML report` link block is removed for **both**
policies: the production gate runs before these freshly generated report URLs
are appended. Arbitrary URLs, malformed footers, numbers, and report-generation
failure messages are not discarded.

## Same corpus, criteria, and budget

The parent builds labels independently of either policy:

1. **Observed identity probes:** real same-pipeline message pairs whose visible
   text is identical except for allowlisted, nondecreasing elapsed ages. These
   cover merge age, stale chat/state, stopped validation age, waiting-PR age, and
   last-work age. PR identities, exit codes, delivery counts, thresholds, commit
   distances, and all other text remain significant.
2. **Observed change probes:** adjacent actual messages with any other visible
   change, conservatively requiring emission rather than guessing it is safe
   to suppress.
3. **Original-interval probes:** preserve actual elapsed intervals, including
   legitimate reminders at or beyond six hours.
4. **Held-out source-derived challenges:** numeric counts versus ages, PR
   identity, exit code, threshold, commit distance, stopped-versus-rejecting
   predicate, age reset, severity, new check, changed recipient/pipeline/
   attachment, recurrence, first emission, exact duplicate, cooldown boundary,
   non-sliding expiry, and reversed clock. Field-specific challenges run only
   when an actual source message contains that field; unavailable guards are
   disclosed. Mutations are labeled as mutations, never historical incidents.

Identity/change probes deliberately replay actual messages 60 seconds apart in
isolated episodes. **This is a counterfactual identity test**, not a claim that
all repeated sent-ledger entries were duplicate pages. Outside-window repeats
are counted separately and original-timing probes must still emit reminders.
Labels certify visible text, not complete underlying system state.

Both workers receive the **same byte-identical payload**, with labels and
provenance withheld. Implementations and acceptance criteria are fixed before
execution; there is no fitting or candidate-specific tuning.

Both must:

* suppress at least **80%** of known duplicate targets;
* suppress **zero** required emissions, including all safety challenges;
* complete the identical corpus within the same limits.

If just one passes, it wins this replay. If both pass, more duplicate suppression
wins; equal counts produce a tie. If neither passes, the result says **neither**.
Wall-clock noise is reported, never used as a tiebreaker. A failed worker blocks
the comparison instead of handing victory to the other worker.

| Bound | Per build / per worker |
|---|---|
| Source | One regular UTF-8 JSONL file, at most 4 MiB / 2,048 records |
| Record | At most 8,192 text characters, 8 attachment references |
| Corpus | At most 160 episodes, 4 events each |
| Sampling | Up to 48 observed repeats, 48 changes, 24 original intervals; disclosed in report |
| Worker input/output | 4 MiB / 128 KiB |
| Wall time | 3 seconds for each policy; subprocess timeout kills and waits for that worker |
| CPU / file output | 2 CPU seconds and 128 KiB file-size rlimits where supported |

Workers run sequentially under isolated Python (`-I -B`) in separate private
working directories with a minimal environment. Sequential execution avoids
cross-worker contention; “parallel universes” refers to separate policy/state
experiments, not a latency race. Directory/process isolation is **not** an OS
security sandbox. There is no network or shell execution. On Windows, only the
system runtime paths needed by Python are carried into the minimal worker
environment; caller tokens and personal home directories are not inherited.

## Receipts, privacy, and repeat behavior

Each build creates a unique private run directory below `artifact_dir`:

* `report.json`: measured scores, shared acceptance/bounds, input aggregate
  counts/hash, outcome, limitations;
* `cases.json`: expected decisions, source line numbers, case kinds,
  counterfactual/original timing, condition hashes;
* `exact_text/{protocol,result}.json`;
* `semantic_condition/{protocol,result}.json`.

Raw alert text, recipients, and report URLs are **not** persisted in receipts.
Workers receive text in memory through stdin; results contain decisions only.
On POSIX, receipt files are created with mode `0600`, run/worker directories with
`0700`. Other platforms inherit the caller-provided private directory's access
controls; the app/SDK must provision that directory with user-only access.
Keep the authorized source private. Do not commit logs, fixtures copied from
live state, or experiment output.

The fingerprint includes protocol, semantic conditions, measured outcome, and
safety-failure categories. It excludes current/source timestamps, recognized
elapsed ages, report URLs, runtime durations, receipt paths, and repetition
counts. Identical conclusions about the same conditions stay stable; a meaningful
condition change, changed gate result, or incomplete worker changes identity.
The scenario itself does not persist delivery/dedupe state; the shared transport
owns sending and repeated-fingerprint suppression.

## Validation and limitations

```sh
python3 -B -m unittest discover -s tests -p test_scenario_parallel.py -v
```

Use the installed Python 3.9+ executable (`py -3` on Windows if applicable).

Tests use generated stdlib fixtures in unique project-local
`.scratch-home/parallel-tests-*` directories and remove only their own data.
They cover bounded/corrupt/absent inputs, equal worker inputs and limits, isolation,
read-only source preservation, fingerprint stability, ties, both-fail outcomes,
selection by measured score, reminder boundaries, and held-out safety failures.
Public-safety tests check for user-home, E.164-phone, and IPv4 literals in
the owned sources. Context-only source selection and the Windows runtime
environment allowlist are also tested. POSIX permission assertions and
FIFO/symlink probes are capability-gated; no native platform support is inferred
from those skipped probes.

This is a bounded review aid, not approval to deploy either policy. The ledger
may already contain upstream-truncated summaries, and presence in a sent ledger
does not independently verify delivery. Unknown changes are conservatively
significant; counts therefore measure this explicit safety contract, not an
unlabeled model of human alert usefulness. Before deployment, review any omitted
diagnostics, unavailable guards, sampling, and longer-lived incident state.
