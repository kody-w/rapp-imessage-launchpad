# SDK and plugin quickstart

## Stable Python interface

Python 3.9+, no third-party runtime dependencies:

```python
from rapp_launchpad import Launchpad, ProtocolError, validate_proposal

client = Launchpad(config_path="/path/to/private/config.json")
client.run("future")                         # {ok, mode, receipts, reason}
client.run("all", send=False)                # all installed allowlisted modules
client.run("all", send=True)                 # enabled modules only
client.submit(proposal, send=False)          # normalized, gated receipt
client.receipts(limit=50, refresh=True)      # up to 50 latest states, newest first
client.ledger.verify()                       # checks chain and durable head
client.policy()                             # shared runtime policy
client.confirm(receipt_id, received=True)    # explicit human attestation only
```

`send=True` requires macOS and saved explicit send consent.
`submit` accepts only configured scenario names; `self-test` is reserved.
Every SDK instance using the same `home` shares the producer lock, receipt
chain, policy, and canonical outbox. Do not create a home per scenario.

The transport adapter never takes `to` from a proposal. It reads the recipient
inside the isolated worker from canonical `HOME/config.json`
(`notify_handle`, with legacy string `notify` support). `notify: false` refuses
notification. Existing configuration is read-only. Renderer responses contain
only a masked recipient and boolean configuration status, not source settings.
An existing nonempty canonical handle is preserved exactly; the stricter
international-number/email syntax check applies to new-device setup, not to
rewriting a working legacy recipient.

## Write a module

Add `scenarios/<name>.py` with `build(context) -> dict`. The
[proposal schema](../protocol/proposal-1.0.schema.json) is the source of truth.
Follow these rules:

1. Read only explicitly configured source material. No broad home, calendar,
   message, browser, credential, or network discovery.
2. If evidence is missing or ambiguous, return `blocked` or `suppressed` with
   an honest reason. Do not manufacture a plausible finding.
3. Store artifacts in `context["artifact_dir"]`. Return absolute file references
   inside that directory. Never put source data in the code checkout.
4. Use stable semantic fingerprints. Rewording and moving ages are not new
   findings. On an authorized real submission, listed artifacts are copied into
   one private ZIP attachment (at most 8 MiB before compression). Originals are
   retained; dry-runs never stage or send attachments.
5. Never import a sender or invoke Messages. Return a proposal; the SDK owns
   the gate, consent, durable submission, and receipts.
6. Build only trusted local modules. A subprocess is **not** a security
   sandbox: modules run with the user's filesystem/network privileges.

The ten initial names are `future`, `decision`, `connections`, `parallel`,
`meeting`, `intentions`, `win`, `adversary`, `timeline`, and `interrupt`.
Only configured names can execute. Symlink modules and path traversal are
rejected. Additional names require `configure --allow-scenario NAME`;
CLI-only code-root configuration is an explicit trust decision, not a renderer
capability.

`examples/scenarios/future.py` is a **permanently blocked synthetic template**.
It demonstrates the contract without an accidental real message. Its companion
proposal can be validated safely:

```bash
python3 -m rapp_launchpad validate examples/synthetic-proposal.json
```

## Configure sources

Source object meaning belongs to the individual producer. All receive the same
explicit object; use namespaces such as `sources["future"]` to keep them clear.
Do not put API credentials into this public repository.

```bash
python3 -m rapp_launchpad sources --set-json /path/to/private-sources.json
```

The desktop **Import source configuration** action uses a native file picker
and confirmation. It replaces private source settings and returns only an
entry count; there is no renderer filesystem API or arbitrary path IPC.
Prefer local evidence and tokens managed separately by the trusted producer.

## Gate integration

The canonical `scenarios.interrupt.evaluate` is used for every producer.
If a custom plugin root has no gate, the bundled canonical gate is used.
A missing gate fails closed; there is no weaker production fallback. The
configured scenario registry extends the same gate to newly registered names,
without changing their identities. See the
[gate interface](../protocol/README.md#one-shared-gate).

Gate code is bounded like build code. An exception, oversized response,
invalid boolean, or invalid fingerprint refuses sending. The SDK keeps the
proposal's stable incident fingerprint in history. The canonical gate's
`delivery_key(proposal)` identifies the evidenced version for transport
idempotency, permitting corroborated substantive updates without treating
sampling clocks or changed wording as new findings.

The native policy presentation includes `quiet_hours.enabled` and can say
`timezone: "local"`. The adapter translates these to the canonical gate's
`quiet_hours: {start, end} | false` and a real IANA timezone. It does not add
another policy decision layer or guess a local UTC offset.

Optional `interrupt.render(proposal)` is also called in an isolated worker.
Its exact validated text is submitted; the SDK does not replace it with a
summary that omits decision/evidence. The fallback renderer likewise preserves
all four answers and all evidence, refusing an oversized envelope. Keep the
notification concise and put substantial detail in artifacts.

## Process and storage bounds

- Separate `python -I -B` process per build/gate/transport call; never a shell.
- Plugin timeout defaults to 40 seconds, configurable from 1–120.
- Output is capped (1 MiB per plugin stream); malformed/duplicate-key JSON is
  refused. Child process groups are terminated on worker timeout.
- Gate input may contain the complete verified history up to the 64 MiB
  ledger bound, rather than silently dropping old queued decisions at 1 MiB.
  Ordinary build input remains capped at 1 MiB.
- Where supported, plugins have CPU, open-file, and generated-file limits.
  These are availability limits, not a claim of sandbox confinement.
- Queue text: at most 650 ASCII characters and 90 words, without truncating
  decision or attribution.
- Receipt responses are capped at 750 KiB; fewer than the requested count may
  be returned when proposals are large. Native runs use `run --summary`.
- Receipt files: private `0600`, state directories `0700`.
- `context["artifact_dir"]` is under the private configuration directory,
  outside the read-only canonical input home. `context["transport_source"]`
  identifies the adopted canonical source for bounded read-only experiments.
- Detailed worker errors stay in that configuration directory's private
  `diagnostics/`; receipts expose only a diagnostic identifier, never raw
  exception content that could contain credentials.
- Receipt ledger limit: 64 MiB. Fail-closed at the bound; an authenticated,
  anchored archival/migration tool is a future feature, not silent rotation.
- All tests use ignored project-local `.test-data/`, with fake transports or
  canonical **enqueue only** against synthetic private runtimes.

## CLI outcome conventions

Output is JSON. Exit 0 means the requested local command succeeded, **not**
delivery. Exit 1 means a run contains an error/unknown result. Exit 2 means
invalid input, setup, or integrity failure. Pure suppression is a successful
decision, not an operational failure. `verify` concerns receipt-chain integrity,
not verification of iMessage delivery.
