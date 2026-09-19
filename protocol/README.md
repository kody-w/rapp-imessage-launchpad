# Launchpad protocol 1.0

## Compatibility

Application/SDK release: **1.0.0**. Wire schemas are explicitly versioned:

- `rapp-imessage-launchpad/proposal/1.0`
- `rapp-imessage-launchpad/receipt/1.0`
- `rapp-imessage-launchpad/config/1.0` (private device configuration)

The module boundary may omit `schema`; `validate_proposal` inserts the exact
1.0 identifier. Wire proposals include it. Unknown proposal fields and schema
versions fail closed. A future minor protocol version needs an explicit reader
update; no silent coercion or “best effort” sends. SDK patch releases can fix
implementation bugs without changing 1.0 field meaning.

Canonical JSON must have unique object member names, valid Unicode strings,
finite JSON values, and no control characters except normal text whitespace.
The proposal field profile is strings/arrays/objects only, preserving the
reference RAPP primitive's no-float domain. Maximum encoded proposal: 64 KiB.
The SDK additionally validates nonblank ready fields and timezone-aware dates.

## Proposal contract

`scenarios/<name>.py` exports:

```python
def build(context: dict) -> dict:
    ...
```

Context:

| Field | Meaning |
|---|---|
| `home` | Absolute canonical runtime, not the code checkout |
| `artifact_dir` | Private directory for this scenario's generated artifacts |
| `now` | Timezone-aware ISO 8601 evaluation time |
| `sources` | Explicitly configured source object; no implicit broad discovery |
| `transport_source` | Optional SDK-supplied canonical code root for read-only experiments |

Required proposal fields:

| Field | Meaning |
|---|---|
| `scenario` | Configured lower-case slug; default ten names in the SDK |
| `status` | `ready`, `suppressed`, or `blocked` |
| `title` | Short human-facing title, up to 200 characters |
| `change`, `impact`, `action`, `decision` | What changed, why it matters, what was done/prepared, and the exact human decision or explicit absence of one |
| `evidence` | Up to 64 `{source, observation}` records; at least one for `ready` |
| `artifacts` | Up to 32 existing absolute paths **inside** the private artifact directory |
| `fingerprint` | Stable semantic identity, up to 512 characters |
| `urgency` | `routine`, `time_sensitive`, or `urgent` |
| `reason` | Explicit readiness/suppression/block reason |
| `deadline` | Optional timezone-aware ISO 8601 deadline |

Each free-text field is bounded at 4096 characters unless stated otherwise.
On an explicitly authorized real submission, referenced artifacts are copied
into one ZIP attachment, bounded to 8 MiB before compression. Paths must remain
inside the scenario's private artifact directory. Original files are never
passed to the canonical cleanup/HTML transformation code. Staged copies use
the existing outbox's report directory and cleanup rules. Dry-runs never stage
or send attachments. The `interrupt` diagnostic producer alone may include a
bounded optional `assessments` array; it never requests a notification itself.

**Fingerprint discipline:** use stable issue/commit/event identities and the
semantic condition. Do not include “minutes ago,” evaluation time, unrelated
log offsets, generated filenames, or wording changes. A materially different
finding needs a genuinely different identity, not a dedupe override.

## One shared gate

The installed or bundled canonical `scenarios/interrupt.py` is evaluated in a
bounded worker with the configured scenario registry:

```python
evaluate(proposal, history, now, policy=None)
# -> {"allow": bool, "reason": str, "fingerprint": str}
```

History rows are `{at, scenario, fingerprint, decision, proposal}`. Only
`decision == "queued"` consumes the budget. Dry-runs, intents, suppression,
errors, and receipt-state transitions do not.

The initial built-in policy is:

```json
{
  "max_daily": 6,
  "quiet_hours": {"enabled": true, "start": "22:00", "end": "08:00"},
  "timezone": "local"
}
```

The SDK translates that stored/display form when calling the canonical gate:
`quiet_hours.enabled: false` becomes `quiet_hours: false`; enabled hours become
exactly `{start, end}`. `timezone: "local"` resolves to the operating system's
actual IANA zone, never a guessed fixed offset or silent UTC fallback.
Canonical `evaluate` therefore receives `{max_daily, timezone: IANA,
quiet_hours: {start, end} | false}`. Failure to resolve a local IANA zone is
explicit and fail-closed; configure an available named zone such as UTC.
The SDK also accepts the canonical hours object or `false`/`null` on policy
input and normalizes it to the native display form when saved.
The `"off"` alias also disables quiet hours. Optional positive finite
`urgent_hours` and `time_sensitive_hours` pass through unchanged, preserving
the canonical ordering constraint and defaults of 2 and 24 hours.

It lives once in `HOME/state/launchpad/policy.json`, under the same producer
lock and receipt chain used by every Launchpad configuration bound to that
runtime. An explicit demonstration may use `max_daily: 10` and disabled quiet
hours. Evidence checks and dedupe remain in force. The canonical gate permits
quiet-hours interruption only for evidenced urgent impact/deadlines, not a
severity flag alone. Budgets cover producers that participate in this SDK;
legacy producers outside it are not silently modified.

The SDK independently refuses non-ready proposals. Its outbox key is
`launchpad/1/` plus canonical `interrupt.delivery_key(proposal)`: a hash of
incident identity and its evidenced substantive version. The same gate
controls deduplication, without a second policy blocking valid updates.
It never accepts an arbitrary message destination from a plugin. A missing
canonical gate fails closed rather than selecting a weaker production gate.

When present, canonical `interrupt.render(proposal)` supplies the actual queue
text. It must preserve change, impact, action, decision, and **all** supplied
evidence attribution within 650 ASCII characters and 90 words. The SDK checks
those output bounds independently. The fallback renderer preserves every
answer and evidence record too, escaping non-ASCII characters instead of
dropping them. Oversized envelopes are refused, not silently truncated; put
longer supporting material in private artifacts.

## Receipt states and transitions

| State | What is actually known |
|---|---|
| `dry_run` | A ready proposal would queue; no transport mutation occurred |
| `intent` | Durable enqueue intent exists; queue acceptance not yet known |
| `queued` | Matching durable canonical queue or terminal evidence exists |
| `sent_unverified` | Canonical sender recorded a send; delivery not proved |
| `unknown` | An outcome is uncertain; reconciliation required, no blind retry |
| `delivered` | Canonical verifier provided a matching delivered Messages row |
| `user_confirmed` | User explicitly attested receipt; not machine delivery proof |
| `suppressed` | Shared policy or producer decided not to interrupt |
| `error` | Validation, plugin, transport, or setup failed |

An enqueue is preceded by an fsynced intent. Reconciliation looks up its stable
key under the canonical outbox lock after a crash, restores one queued decision
if necessary, and does not send it again. Actual sending remains the core's
serialized responsibility. An unknown enqueue does not mean “not sent.”

Canonical `outbox-sent.jsonl` alone is mapped to **sent_unverified** even if its
record lacks an `unverified` flag: the original count-based sender evidence
does not prove an exact delivered-message match. `delivered` requires the
canonical verifier's `verified_at` and `delivery_evidence` with source
`Messages/chat.db` and a positive matching `message_rowid`. Human confirmation
never edits the canonical sent/unknown ledgers.

## RAPP-derived local integrity

Every receipt event uses the original RAPP reference `build_frame`,
`verify_frame`, `H("rapp/1:particle", ...)`, and `H("rapp/1:wave", ...)`.
Frames have the original eleven-key shape; `prev` binds the prior payload
hash. The chain is checked before appending, locked, flushed, and fsynced.
A separately atomically pinned head detects suffix truncation.

Stream identifiers are intentionally **local** (`local:launchpad:<random>`).
No authenticated GitHub identity, registry, signed network stream, full JCS
number implementation, or complete RAPP conformance suite is claimed.
This is a tested **RAPP/1-derived unsigned local profile**, not blanket
“RAPP-compliant.” A hostile account owner can replace both chain and head;
independent external anchoring/signatures are outside v1.
