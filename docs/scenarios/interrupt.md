# Scenario 10: Earn the right to interrupt me

`scenarios.interrupt` is a shared admission gate, not a sender. It has no wall
clock, network, filesystem writes, persistent counters, or runtime changes.
All nine producer scenarios use the same gate. Its own `build()` report stays
private even when a proposed interruption qualifies.

## Public application and SDK integration

The gate's APIs accept and return plain JSON-compatible data, so the public
RAPP iMessage Launchpad protocol adapter can reuse them without a device-specific
implementation. The gate does **not** discover users, recipients, permissions,
calendars, messages, network addresses, or application directories. It imports
the vendored pure RAPP hashing primitive, not runtime configuration.

The application/collector owns any default derived from
`Path.home() / ".storykeeper" / "home"` and supplies that configuration through
context. Explicit source configuration belongs to that caller and takes
precedence over its defaults. This gate has no source-path default: the supplied
proposal's `evidence[].source` and `artifacts[]` are opaque identifiers, preserved
without resolving, expanding, opening, or replacing them. Changing the device's
home directory does not change the assessment or its semantic identity.

Default `timezone="UTC"` uses the standard-library fixed UTC zone, including
systems without an installed IANA timezone database. Configured non-UTC zones
require the adapter environment to provide IANA data; absence fails closed with
an explicit configuration error, never a silent UTC substitution.

All examples and tests here use synthetic, sanitized data. Real proposals,
history, receipts, rendered messages, permission state, and outbox records
remain private, untracked runtime artifacts and must not be committed or
published with the application. The gate writes none of them. `render()` preserves caller
content; it is **not** a privacy scrubber or authorization check. The application
owns source permissions, recipient approval, private storage, redaction for any
public export, and cross-device onboarding. This module neither grants
permissions nor sends or publishes anything.

## Public contract

Minimum runtime: **Python 3.9**, standard library only, plus the vendored
`rapp.py` primitive. The suite is verified with Python 3.9.6. Postponed
annotations and `typing.Optional` also support SDK `get_type_hints()` reflection
on 3.9. `Z` timestamps are normalized to `+00:00` before `fromisoformat()`; the
gate does not depend on newer Python versions accepting `Z` directly.

```python
evaluate(proposal: dict, history: list[dict], now: str,
         policy: Optional[dict] = None) -> dict
render(proposal: dict) -> str
build(context: dict) -> dict
delivery_key(proposal: dict) -> str  # optional outbox idempotency helper
```

`evaluate` always returns `{allow: bool, reason: str, fingerprint: str}`.
The returned fingerprint is the producer's supplied semantic identity, unchanged
(an invalid or oversized identity returns `""` and is rejected). Fingerprints
are limited to **512 characters**, keeping evaluation results below 8 KiB even
when JSON escapes non-ASCII characters, comfortably inside a 1 MiB worker limit.
Use a stable digest rather than embedding unbounded report contents in an identity.
The function
does not mutate its arguments. Rejection reasons distinguish unsupported
proposals, invalid configuration/time/history, duplicates, unsupported updates
or urgency, quiet hours, and exhausted budget.

Only `status="ready"` is eligible. Supported scenarios are `future`, `decision`,
`connections`, `parallel`, `meeting`, `intentions`, `win`, `adversary`, and
`timeline`. Required strings are `title`, `change`, `impact`, `action`, `decision`,
`fingerprint`, and `reason`. Four explicit answers must survive into the text:

1. What changed?
2. Why does it matter?
3. What action was taken or proposed?
4. What is needed from the human?

`decision="No decision needed."` is valid; a blank, unknown, or placeholder
answer is not. `action="No action taken; approval required."` is honest and
acceptable. The gate does not turn a plan into a claim of execution.

`evidence` is a nonempty list of `{source: str, observation: str}`. At least one
observation must describe more than a heartbeat, a moving age, or a poll time.
Observer blindness (“cannot read”), unsupported assumptions, and missing
observations do not count as evidence of a broken watched system.
`artifacts` is a list of path strings (possibly empty). `urgency` is `routine`,
`time_sensitive`, or `urgent`. Optional `deadline`, required `now`, and all
queued-history `at` values must be offset-aware ISO-8601 instants, for example
`2026-09-19T23:30:00Z` or `2026-09-19T19:30:00-04:00`.

Invalid input fails closed. Unknown optional proposal/policy keys do not disable
validation, evidence requirements, or deduplication.

This module is the **single admission-policy implementation**. The SDK may load
it in a bounded worker, but must fail closed with a gate-unavailable error if
loading/evaluation fails. Do not maintain a separate “matching” SDK fallback
policy. No import, `build`, or evaluation automatically runs tests or a sender.

## Identity, evidence, and escalation

Producers must give the same finding a stable `fingerprint`: do not hash `now`,
poll timestamps, changing ages, presentation, or generated artifact names into
its identity. The gate compares **all** prior queued versions, not just the
latest one or a cooldown window. A shared fingerprint identifies the same
finding even across producer scenarios:

- Reworded titles/actions, urgency flags, new artifact paths, source-only churn,
  evidence order, and repeated evidence do not independently earn another page.
- Relative ages such as `stale for 62.1h`, `63 hours old`, and contextual clocks
  such as `checked at <ISO instant>` are normalized. Real magnitudes, such as
  three versus seventeen failed checks, are retained.
- Changed impact must have a substantive observation not already present in
  the finding's queued history. A new measurement or new verified receipt can
  qualify; a changed impact must share terms with new observations and its
  quantities must appear there. A larger number in the impact text alone cannot.
- An evidenced changed deadline can qualify. Merely approaching the same
  deadline or switching an urgency flag cannot. Equivalent timezone offsets
  denote the same deadline.
- Reverting to an already-queued version does not cause threshold-flap spam.
- As a defensive fallback, an unchanged normalized title plus shared source or
  identical claims within a producer associates an accidentally regenerated
  fingerprint with its existing finding. This is not general natural-language
  semantic inference.

The comparison hashes use the existing pure RAPP `H` primitive. This preserves
the existing `cooldown.py` principle of condition identity rather than prose,
without calling its stateful `should_send()`, expiring its deduplication after
six hours, or stripping meaningful quantities as its fallback fingerprint does.
Importing that module would also import runtime-path initialization; this pure
gate deliberately does not.

## Policy

```python
{
    "timezone": "America/New_York",  # IANA name; default "UTC"
    "max_daily": 6,                 # nonnegative integer, global across producers
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "urgent_hours": 2,
    "time_sensitive_hours": 24,
}
```

Quiet hours use the supplied instant in the configured local zone, including
DST. Start is inclusive; end is exclusive. Both overnight and same-day ranges
work. Equal endpoints, bad clocks, unknown zones, naive times, and invalid
budgets/windows are errors rather than silent fallback. Use `False`, `None`,
or `"off"` for `quiet_hours` to explicitly disable quiet hours.

Elevated urgency needs either:

- An absolute deadline confirmed in a sourced observation, with a deadline cue
  such as “deadline”, “due”, “meeting”, or “expires”. It must fall within the
  configured future window for the requested urgency. A poll timestamp alone,
  an expired date, or a tentative/unverified deadline is not enough.
- A sourced concrete active material impact (for example a payment outage
  affecting 42 customers), with matching impact terms and quantities. A
  speculative, negated, or simulated impact does not establish active harm.

Only evidenced `urgent` proposals may override quiet hours. Nothing, including
urgency, overrides deduplication or the daily budget. A demonstration can use
`{"max_daily": 10, "quiet_hours": False}`; it cannot bypass evidence or duplicates.

These are conservative checks on **supplied** observations, not independent
verification of source files or a general entailment/provenance engine. Producers
are responsible for accurate evidence and appropriately stable identities.
The gate never claims that a source was opened, an action ran, or delivery
succeeded.

## History, failures, and concurrency

History records have this shape:

```python
{
    "at": "2026-09-19T16:00:00Z",
    "scenario": proposal["scenario"],
    "fingerprint": proposal["fingerprint"],
    "decision": "queued",  # or "suppressed" / "error"
    "proposal": proposal,
}
```

Only `queued` consumes the global **local-calendar-day** budget and participates
in duplicate detection. Suppressed/error records are ignored after validating
their decision tag; even a failed send of this very proposal can be retried.
Queued records must contain valid proposals with matching identities and
nonfuture timestamps. Corrupt queued history fails closed rather than pretending
there is unused budget. Older queued findings still deduplicate after midnight.

**The adapter/caller owns the lock.** Under one caller-owned lock: read authoritative
history, evaluate, enqueue, then append the correct result. Append `queued` only
after successful queue insertion; an error must not spend the budget. Hold the
lock across the complete transaction, including durable history recording.
Recovery from a queue/history partial write is also the caller's responsibility.
Two pure calls on the same history intentionally return the same result; neither
reserves a slot. No lock or sending path is added here.

`delivery_key(proposal)` is an optional, pure **64-character hexadecimal**
idempotency key for the specific evidenced version admitted by the gate. It
combines the semantic finding fingerprint with the same normalized claim used
by duplicate detection; it does not introduce another policy. Age, poll clock,
presentation, artifacts, and source-only churn leave the key unchanged.
Substantive observations, changed impact, or an evidenced changed deadline can
produce a new delivery key while preserving the finding's semantic fingerprint.
Equivalent timezone offsets do not create a new version.

Use it **only after** `evaluate(...).allow` for the outbox's delivery identity.
Keep `proposal.fingerprint` and the matching history fingerprint unchanged;
store the delivery key separately. Permanently denying every future proposal
with the same raw finding fingerprint would incorrectly block gate-approved
escalations. The key is not authorization: a new key may still be denied by
evidence, history, quiet hours, or budget. Queue/history recovery must reconcile
each delivery key without counting a retry as another successful insertion.
Dry-run/intent receipt types must not be fed as unknown gate-history decision
tags or recorded as `queued`; they do not consume the notification budget.

## Twenty-second rendering and private assessment

`render()` preserves the title, all four answers, every evidence source and
observation, and any deadline. Output is ASCII, at most **650 characters and
90 words**, with whitespace compacted. Common punctuation/diacritics are folded;
otherwise unsupported characters are escaped, not silently discarded.
Artifact lists are not repeated in the text.

There is **no blind truncation**, sentence clipping, or invented summary.
An oversized or incomplete proposal raises `ValueError`; `evaluate` rejects it
before admission. Producers should write a concise, complete notification
envelope and place detailed analysis in their artifacts. In particular, preserve
negative caveats (“not applied”) and the entire decision. Render is not a
substitute for evaluating history and policy.

`build` accepts `{now, history, policy, proposals: [...]}` or a single
`proposal`. It returns a standard `scenario="interrupt"` envelope with
`status="suppressed"`, plus an `assessments` list containing scenario and gate
results. Invalid context is `blocked`; absent proposals are explicitly
unassessed, not declared healthy. Batch assessments are **individual** checks
against the supplied history, not simulated reservations or authorization to
enqueue the whole batch. The caller must evaluate each real enqueue transaction.
The report itself is not an eligible producer and cannot page the human.

## Validation

```sh
python3 -m unittest discover -s tests -p 'test_scenario_interrupt.py' -v
```

The tests cover all producer names, missing answers/evidence, relative-age spam,
source and fingerprint churn, worsened impact, deadline escalation, flapping,
quiet boundaries/local dates/DST, malformed inputs, failed-send retries, global
budgets, caller-owned concurrency, immutable inputs, complete ASCII rendering,
honest private assessments, JSON protocol round trips, opaque source identifiers,
home-directory independence, Python 3.9 SDK type-hint reflection, equivalent
`Z`/numeric-offset timestamps, bounded worker results, per-version outbox
idempotency, and UTC operation without an IANA database.
Tests needing a named local zone skip if its IANA data is unavailable; the
default-UTC and missing-zone fail-closed tests still run.
