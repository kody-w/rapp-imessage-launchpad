# Onboarding and permissions

## Existing device

The app detects `~/.storykeeper/code/{outbox,paths,filelock}.py` and
`~/.storykeeper/home/config.json`. Detection alone does not change anything.
Choosing **Reuse detected service** binds to that exact runtime.

The adapter imports `outbox.py` in an isolated child with `SENTINEL_HOME` set to
the configured home, then calls:

```python
outbox.enqueue(text, to, attachments=None, dedupe_key=stable_key)
```

The real recipient is read privately by the child. Its results contain no
recipient or raw canonical ledger. Existing `com.rapp.storykeeper.outbox-drain`
is inspected, never booted out, rewritten, disabled, or duplicated. Existing
mode refuses Launchpad `drain`; keep using the canonical installed drainer.
An onboarding test is only queued and follows that drainer's existing cadence.
If it is not loaded, the app says so instead of silently installing one.

## Shared self-chat for AI updates

One iMessage self-chat can serve as an ongoing activity feed across your AIs.
Use short attribution such as `[Copilot / Launchpad]` in producer titles so
progress, results, and requests for decisions have an identifiable source.
Useful updates need not be emergencies; suppress stale repeats rather than
assuming every nonurgent update is unwanted.

**Reusing a sender does not establish the intended destination.** An existing
Storykeeper recipient may point to a different conversation from your
self-chat. Verify the exact iMessage address shown in the intended thread
before accepting a test as successful.

To persist the route on an existing device:

1. Read Launchpad's private app configuration to identify its canonical `home`.
   The detected Storykeeper home is normally `~/.storykeeper/home`.
2. With the operator's approval, set `notify_handle` in that home's private
   `config.json` to the intended self-chat iMessage address. Preserve all other
   settings and existing file permissions; notification must remain enabled.
   Do not put the address in source code, a proposal, or a committed example.
3. This changes the shared default, including other producers that read that
   setting. Launchpad reads it for each enqueue, so a sender reinstall or a
   second drainer is unnecessary. Already queued messages retain their original
   destinations; unrelated AIs with separate configuration are not redirected.
4. Request a labeled self-test and inspect that exact conversation. A matching
   outgoing message and incoming self-chat copy establish the local round trip.
   A conversation-list preview elsewhere or a successful sender exit does not.
   Delivery to other devices remains a separate observation.

For a new Mac, enter the intended self-chat address during portable onboarding
instead. In either mode the destination stays in private runtime configuration,
not this repository. Future operators should resolve that configuration rather
than hardcode an address or reuse an old one-off test override.

Saving the destination does **not** change budgets, quiet hours, consent, or
deduplication. The default daily limit is six, and even explicit self-tests can
be suppressed after it is exhausted. An activity-feed deployment should choose
its policy explicitly; do not silently bypass the gate or claim a suppressed
test was sent.

## New Mac

The portable sender is the same byte-for-byte MIT outbox core recorded in
`_vendor/provenance.json`. New-Mac setup stores:

- app config: `~/Library/Application Support/RAPP iMessage Launchpad/config.json`;
- canonical runtime: `…/RAPP iMessage Launchpad/runtime/`;
- recipient: `runtime/config.json`, private, never committed;
- shared queue: `runtime/state/outbox.jsonl`;
- Launchpad receipts/policy/artifacts: `runtime/state/launchpad/`.

Portable onboarding is disabled when an existing canonical service is detected.
The CLI can deliberately choose a different explicit home, but doing so creates
a different policy/outbox; do not use that as a per-scenario deployment pattern.

Python 3.9+ is a separate prerequisite. The app checks standard Homebrew,
python.org, and system locations and reports the actual interpreter/version.
An administrator can supply an absolute `RAPP_LAUNCHPAD_PYTHON` environment
override. A renderer cannot choose an executable.
The unprovisioned system Python shim is skipped to avoid implicitly triggering
a Command Line Tools installation prompt.

## macOS Automation

1. Open Messages. Sign in and ensure ordinary iMessage works for the intended
   recipient.
2. Request the explicit self-test from the app. On a new Mac this queues the
   labeled test and runs the canonical core for one entry, only if the portable
   queue was empty before the test.
3. Approve the Automation prompt for the actual requesting app/executable.
   The app can open **Privacy & Security → Automation**, but cannot grant
   permission itself.
4. If a background context timed out, use the app's foreground self-test.
   A denied or unshown prompt is not a delivery success.
5. Check the intended recipient device. Record “I received this” only if true.

**No Full Disk Access is needed to SEND.** The canonical core can attempt a
read-only delivery observation; if that evidence is inaccessible, Launchpad
keeps `sent_unverified` or `unknown`. It does not scrape Messages in onboarding,
inspect/reset TCC databases, or silently request broader privacy access.
Unsigned build identity is not stable production permission provisioning;
sign/notarize a release for repeatable deployment.

The self-test is explicitly not a scenario finding. It shares the daily budget
and quiet-hours policy, has one semantic identity per UTC day, and cannot be
used to bypass duplicate suppression. If suppressed, read the receipt instead
of retrying under a different invented fingerprint.

Quiet hours and the send-consent switch gate **new queue submissions**, not
previously accepted messages. Already queued messages can still drain on the
canonical service's cadence; this app does not silently rewrite or pause that
shared service. Uninstalling a producer does not retract its accepted queue.

## Continuous operation

- **App-only schedule:** five minutes; opt-in; stops when the app quits.
  Closing the last macOS window leaves the app running until Quit.
- **Login startup:** separate macOS consent, installed app only.
- **Independent schedule:** explicit install/uninstall of
  `com.rapp.imessage-launchpad.scheduler`, enabled scenarios only.
  No `RunAtLoad`, shared tick lock, bounded workers.
- Existing mode only produces queue entries. New-Mac mode drains at most three
  entries using the canonical core. A persistent schedule disables overlapping
  app scheduling; the shared tick lock is an additional cross-process guard.

After moving the app, changing Python, or installing a new SDK location,
remove/reinstall **only** the scoped Launchpad schedule so its absolute paths
remain valid. No automatic updates or migrations rewrite a running sender.

## Recovery

If queue/receipt evidence is malformed, sending stops. Preserve private
evidence and inspect canonical diagnostics; never delete ledgers to force
retries. Launchpad does not automatically acknowledge quarantines, reset unknown
deliveries, or erase a broken receipt head. Those actions can cause duplicates
or hide failures and belong to the canonical recovery procedure.
