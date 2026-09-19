# Third-party notices and provenance

The canonical outbox and RAPP reference primitives are from
[kody-w/rapp-sentinel](https://github.com/kody-w/rapp-sentinel),
revision `a69a4af55f6e50a1f22da5b08e7e848b0148ca94`.

**Copyright (c) 2026 Kody Wildfeuer. MIT License.**
The complete original license is preserved at
`rapp_launchpad/_vendor/LICENSE`. Its copyright and permission notice apply to
the copied files and substantial adaptations.

The five vendored files are byte-for-byte copies, recorded by SHA-256 in
`rapp_launchpad/_vendor/provenance.json`:

- `canonical/outbox.py`: the existing serialized, durable iMessage outbox;
- `canonical/paths.py`: separates code from `SENTINEL_HOME`;
- `canonical/filelock.py`: canonical cross-process lock primitive;
- `rapp.py`: reference canonicalization and frame build/verify primitives;
- `LICENSE`: original license text.

`rapp_launchpad/ledger.py` adapts the original `alert_ledger.py` pattern:
reference-built and reference-verified decision frames before every append.
Launchpad adds shared locking, fsync, fail-closed errors, and a durable head pin.
It does **not** copy private identity stamps, instance identifiers, runtime
configuration, recipients, evidence, or logs.

Electron, Chromium, Node.js, and packaging dependencies carry their own licenses
in their distributions. `package-lock.json` pins the resolved dependency tree.
No Apple-owned assets or code are included. iMessage and macOS are Apple
trademarks; this project is not affiliated with or endorsed by Apple.
