# RAPP iMessage Launchpad

**Many evidence producers. One accountable iMessage pipeline.**

Use one self-chat as a shared activity feed for your AIs, with clearly
attributed progress, results, and decisions rather than only emergency alerts.
See [shared self-chat setup](docs/ONBOARDING.md#shared-self-chat-for-ai-updates)
to persist the destination without committing private account details.

A local Electron mission control, a versioned proposal/receipt protocol, and a
standard-library Python SDK. Scenarios produce evidence-backed proposals;
a shared gate decides whether they deserve an interruption; the **existing
canonical outbox** is the only sender. Dry-run is the default.

## What is real—and what is not assumed

- macOS sends through Messages using the audited MIT `rapp-sentinel` outbox.
- An existing Storykeeper installation is detected and reused. Launchpad does
  **not** replace its source, recipient, LaunchAgents, or drainer.
- New Macs receive a portable copy of that same core—not a different transport.
- Onboarding checks the actual runtime, recipient presence, queue, receipt
  chain, and job state. A self-test requires an explicit native confirmation.
- `queued`, `sent_unverified`, `unknown`, `delivered`, and `user_confirmed`
  mean different things. Process exit 0 is **never** delivery evidence.
- Scenarios without evidence are blocked/suppressed. Missing modules are shown
  as **not installed**; there are no canned findings masquerading as live data.
- Authorized submissions include a bounded ZIP of the scenario's declared
  artifacts when present, so results do not depend on a private LAN link.
  Private originals remain available in the app's data directory.
- No HTTP listener, cloud service, telemetry, account token, or background
  permission reset. Sources and evidence stay in your private runtime.

## Install on another Mac

1. Use the unsigned universal DMG or ZIP from your trusted release source. Move
   **RAPP iMessage Launchpad.app** into Applications.
2. Install **Python 3.9+** if the app reports it missing. Python is not bundled;
   [python.org](https://www.python.org/downloads/macos/) provides an installer.
   The SDK itself has no third-party runtime dependencies.
3. Open Messages and sign in to iMessage. Complete the app's onboarding.
4. Reuse a detected pipeline, or on a new Mac enter the intended recipient.
   Existing recipients are never returned unmasked to the renderer.
5. Choose **Request one self-test**. Allow Automation for the actual requesting
   app/executable when macOS prompts. Check the recipient device before choosing
   **I received this**.
6. Import your own private source configuration. Preview scenarios, inspect
   evidence, then explicitly enable real queue submissions if desired.

**Unsigned limitation:** this development build has no Developer ID signature
or notarization. Gatekeeper may block it, and Automation identity may change
between builds. Verify the source/checksum and follow macOS's documented
individual-app approval flow only if you trust the build. Do not disable
Gatekeeper, reset TCC, or edit privacy databases. Signed/notarized production
distribution remains a release-owner responsibility.

**Full Disk Access is not required to send.** Machine delivery verification
needs additional evidence/access; Launchpad does not request that access.
Without evidence the result stays unknown/unverified, or separately
user-confirmed. Windows/Linux support SDK validation and dry-runs, **not
iMessage sending**.

## SDK and CLI

From this checkout, the CLI works immediately without installing dependencies:

```bash
python3 -m rapp_launchpad --help
python3 -m rapp_launchpad diagnostics
```

Optional editable installation:

```bash
python3 -m pip install -e .
rapp-launchpad --version
```

Bind an existing installation. This writes only Launchpad's private device
configuration and its own runtime subdirectory:

```bash
python3 -m rapp_launchpad configure \
  --existing-source "$HOME/.storykeeper/code" \
  --home "$HOME/.storykeeper/home"
python3 -m rapp_launchpad run all             # dry-run
python3 -m rapp_launchpad receipts --refresh
python3 -m rapp_launchpad verify
```

To use a separate private configuration, put **`--config PATH` before the
subcommand**. Configure `--scenario-root PATH`, additional
`--allow-scenario NAME`, `--enable NAME`, and `--sources-json PRIVATE.json` as
needed. Source JSON is intentionally not committed. Ten standard scenario names
are allowlisted by default; installed modules are discovered only within that
configured root.

```bash
# An explicit stdin object changes consent, not recipient settings.
printf '%s\n' '{"send_enabled":true}' | python3 -m rapp_launchpad settings --stdin
python3 -m rapp_launchpad sources --set-json /path/to/your/private-sources.json
python3 -m rapp_launchpad run future --send   # real enqueue, if the gate permits
python3 -m rapp_launchpad self-test --send --confirm
python3 -m rapp_launchpad confirm RECEIPT_ID --received --yes
```

`--send` enqueues; existing installations retain their own draining cadence.
New-Mac self-test and explicitly scheduled portable ticks use the same canonical
core to drain a bounded number of messages.

```python
from rapp_launchpad import Launchpad, validate_proposal

launchpad = Launchpad(config_path="/path/to/private/config.json")
result = launchpad.run("future")                   # recorded dry-run
receipt = launchpad.submit(validate_proposal(proposal), send=False)
print(receipt["state"])
# send=True additionally requires saved send consent and macOS.
```

See [protocol](protocol/README.md), [SDK/plugin guide](docs/SDK.md),
[onboarding](docs/ONBOARDING.md), and [security/limits](docs/SECURITY.md).

## Optional scheduling

Nothing is installed automatically. In the app, enable selected scenarios,
then choose app-only scheduling and optionally macOS login startup.
For a producer that survives app exit:

```bash
printf '%s\n' '{"send_enabled":true,"enabled_scenarios":["future"]}' \
  | python3 -m rapp_launchpad settings --stdin
python3 -m rapp_launchpad schedule install --interval 300 --send
python3 -m rapp_launchpad schedule status
python3 -m rapp_launchpad schedule uninstall
```

Only `com.rapp.imessage-launchpad.scheduler` is managed. It uses a tick lock,
bounded workers, no run-at-load burst, and a 300–86400 second interval. Existing
mode is a **producer only**, never a duplicate outbox drainer. Portable mode
drains at most three canonical queue entries per tick. The configured code,
Python executable, and private configuration must remain at their installed
paths. Reinstall this scoped schedule after moving/upgrading them.

## Build and verify

Node.js 22+ and npm, Python 3.9+, and macOS are the development requirements.
Direct Node dependencies are exact-pinned; use the committed lockfile.

```bash
mkdir -p .build-cache
export TMPDIR="$PWD/.build-cache"
export ELECTRON_CACHE="$PWD/.build-cache/electron"
export ELECTRON_BUILDER_CACHE="$PWD/.build-cache/electron-builder"
npm ci
python3 -m unittest discover -s tests/python -v
python3 -m unittest discover -s tests -p 'test_scenario_*.py' -v
npm test
npm run lint
npm run smoke                         # isolated unconfigured profile; no sends
npm run dist:mac                      # unsigned Apple Silicon + Intel universal DMG/ZIP
node scripts/smoke.mjs --packaged
```

Artifacts go to `release/`; test evidence stays in ignored `.test-data/`.
The package excludes configurations, caches, logs, Python bytecode, private
artifacts, and tests. Public examples are synthetic and never auto-enabled.
No publication or auto-update is performed by the build.

The shared gate governs SDK producers. Adopting an existing outbox does not
silently pause or rewrite legacy Storykeeper notification jobs; those callers
can be migrated to the SDK separately.

## License

MIT. Canonical MIT source attribution, provenance hashes, and notices are in
[NOTICE.md](NOTICE.md). Local receipt hashing reuses the RAPP reference
implementation; this project **does not claim full RAPP conformance**.
