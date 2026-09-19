# Trust boundaries and limits

## Desktop boundary

The renderer is sandboxed, `contextIsolation: true`, `nodeIntegration: false`,
with spellcheck disabled. It receives a frozen, narrow preload API, never raw
IPC, filesystem handles, process execution, environment variables, source
configuration, or an unmasked stored recipient.

A secure custom `launchpad://app/` protocol serves four allowlisted local
assets. Other paths get 404. Navigation, windows, webviews, browser permissions,
and network requests are denied. CSP disallows remote scripts, remote content,
inline script, eval, frames, forms, and network connections.

Main-process IPC validates exact shapes, enums, slugs, booleans, receipt IDs,
and the **exact top-level application frame**. It never accepts a shell
command, executable, arbitrary URL, or filesystem path from the renderer.
Native file selection is the only desktop source-config import. Native dialogs
confirm setup, self-test, queueing, persistent scheduling, login, and receipt
attestation. Text from plugins is rendered with `textContent`, not HTML.

The recipient typed during new-device onboarding is a transient user input. It
is sent to the child over stdin, never in process arguments, cleared from the
input after setup, and never returned unmasked. There are no app account tokens
or cloud secrets.

## Python and plugin boundary

Configured Python code is **trusted code**, not untrusted data. `-I`, output
limits, timeouts, process groups, and resource limits improve determinism and
availability; they do not prevent a malicious installed plugin from accessing
the user's data or network. Audit a plugin before explicitly allowing it.
No package/plugin marketplace or automatic code download is included.

One private canonical runtime owns the recipient. Every participating producer
shares its gate, lock, semantic dedupe, budget, durable queue, and receipts.
Legacy producers outside this SDK retain their behavior and are not silently
subjected to a new policy. They still share the existing canonical queue.
For a truly universal producer policy, migrate those producers to this SDK.

Subprocesses use fixed entrypoints, argument arrays, and stdin JSON—never a
shell. Raw child stderr is not sent to the renderer because it may contain
private source material or recipient details.

## Receipt integrity is not external attestation

The original RAPP reference primitives are reused with MIT attribution.
An append verifies the entire chain, then fsyncs the event and atomically
updates an independent head. A crash between append and head pin can advance
the verified head safely; a torn tail or truncation behind the head is refused.

These are unsigned local frames. An attacker controlling both ledger and head
can replace them. The canonical terminal ledgers are local evidence, not
cryptographic statements from Apple. A human-confirmed receipt is an explicit
user assertion; it is not upgraded to machine-verified `delivered`.
No full RAPP conformance, identity ownership, network signing, or remote
attestation is claimed.

## Distribution

The development package is unsigned/not notarized and bundles no Python
runtime. It is not an Apple-supported deployment system or iMessage API.
Messages Automation and sign-in must work on each individual Mac.

No telemetry, crash upload, HTTP listener, remote app content, auto-update,
credential harvesting, private screenshots, or background TCC manipulation is
present. The builder publishes nothing. Dependencies are pinned in
`package-lock.json`; build-tool notices and audit results should be considered
before signing a production release.

## Reporting

For public bug reports, include a synthetic reproducer, SDK/app versions,
platform, and redacted diagnostics. Do not attach recipient configuration,
private source JSON, full outbox/receipt ledgers, evidence artifacts, or
screenshots that expose personal data. Report sensitive issues privately to
the repository owner instead of posting private evidence in public issues.
