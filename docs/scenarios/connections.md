# Scenario 3: executable capability connections

`scenarios.connections.build(context)` returns the shared scenario contract.
It never imports source projects, edits them, merges, publishes, enqueues, or
sends messages. Transport and any genuinely accessible link belong to the
shared runner and the public RAPP iMessage Launchpad protocol adapter.
This module has no separate Messages integration or publication mechanism.

## Authorized inputs

```python
from pathlib import Path

def connection_context(application_home, authorized_folders, now):
    home = Path(application_home)
    return {
        "home": str(home),
        "artifact_dir": str(home / "artifacts" / "connections"),
        "now": now,
        "sources": {
            "connections_roots": [str(path) for path in authorized_folders],
        },
    }
```

The caller supplies the application home through `context["home"]`. The
Launchpad/runner default is `Path.home() / ".storykeeper" / "home"`; the
scenario never independently looks up a user's home, account, or device.
The example accepts values already selected and authorized by the parent
app's permission onboarding; it does not grant permissions itself.

Roots must be absolute directories, with no symlink components. At most four
are accepted. The application home and its ancestors are refused as scan
roots. **There are no implicit project roots.** An absent
`sources.connections_roots` key or an empty list produces `suppressed`
without scanning. Explicit roots are the full read allowlist, including
authorized folders outside the application home. Reauthorize the actual
local folders on another device instead of assuming an account name,
checkout layout, or shared machine path. There is no home-wide or network scan.

The scan reads Python source, not README claims or runtime data. It skips
hidden entries, symlinks and redirected paths, `test_*`, `prove_*`, and
runtime/dependency/test directories. Per root it is limited to depth 2,
32 directories, 256 entries
per directory, and 128 Python files. Files are bounded to 256 KiB and 25,000
AST nodes; at most 32 matching functions are retained. Unavailable, invalid,
or oversized evidence is reported. The capability inventory is deliberately
narrow, not an assertion that other capabilities or duplicates do not exist.

## What is actually proved

The current supported family is a **last-marked-line parser**: split text
into lines, inspect them in reverse, strip surrounding whitespace, recognize
a literal prefix, return the final matching payload, or a literal fallback.
Complete function ASTs must match one of two audited templates. Function,
argument, local-variable and repository names are not matching criteria.
Docstrings, comments and annotations do not define the capability. Decorated
functions, extra statements, changed prefix slicing, and nonmatching control
flow are rejected. Obvious shadowing of `len`/`reversed` is also rejected.

At least two distinct source files must implement the same marker/fallback
contract. Matching files may be in different repositories or one authorized
workspace. One variant calls `input.splitlines()` directly; the other calls
`(input or "").splitlines()`. **That difference is retained**, not assumed away:
strict callers still raise `AttributeError` on `None`, while falsey-tolerant
callers return their fallback.

The private proof reconstructs only those approved bodies from authored
templates, using source literals as escaped data. It does not copy executable
source or run arbitrary ASTs. Python runs this generated, self-contained file
with `-I -S`, no shell, no project imports, and a ten-second timeout. A separate
forward-scanning oracle checks both baseline implementations and the shared
implementation plus caller adapters across over a thousand distinct synthetic
inputs per implementation, including multiple markers, whitespace, Unicode
line separators, empty text, and falsey/non-string inputs.

The measured benefit is structural: actual parsing-loop and executable
AST-statement counts in the generated baseline versus connected code. For
two implementations, two loops become one; eight body statements become six
including the two adapters. This **does not claim production latency savings,
hours saved, or a deployed improvement**. Original modules are not imported,
so import-time wiring, dynamically replaced builtins, and real caller types
remain integration-review questions. The evidence records source locations,
content hashes, normalized shapes, policies, up to 64 direct local call-site
lines per function, and coverage limits. Call-site evidence is static, not a
claim that an original caller was executed.

## Artifacts, status and repetition

Each successful build creates one unique directory under `artifact_dir`,
containing `proof.py`, `result.json`, `manifest.json`, and `index.html`.
Directories request mode 0700 and files 0600 on POSIX systems. On systems
without POSIX permissions, the caller must supply a private application-data
directory protected by the operating system's access controls. The scenario
refuses artifacts inside any source root or through a symlink.
Runtime evidence can contain local paths and source literals: it is private,
untracked state, not public SDK example data, and must not be committed. The HTML
report escapes source-derived data, has no network dependencies, and links
to local files only; it is **not** a claim of a hosted or phone-accessible URL.

Rerun with `python3 -I -S <returned-proof-path>`. It prints deterministic
JSON without reading or writing the original projects. Reversal is deletion
of that generated directory only. Existing proof directories are never
overwritten. Retention belongs to the caller.

* `ready`: a source-backed cross-file connection and its measured proof pass.
  Missing other roots remain counted in the concise evidence, with full
  diagnostics in the manifest, without invalidating the proven pair.
* `suppressed`: disabled, or the bounded readable sources contain no supported
  cross-file connection.
* `blocked`: invalid/unavailable evidence prevents a connection, or the private
  proof cannot safely be created or verified. No benefit is asserted.

Ready fingerprints hash the capability version, marker, fallback, and sorted
multiset of input policies. They exclude time, artifact names, paths, source
symbol names, formatting, and source order. Semantic changes and additional
matched implementations change the fingerprint; a repeated proof does not.
All events have routine urgency and no deadline.

Ready notification envelopes keep all four answers and two attributed summary
observations concise and ASCII-only. Tests include title, reason, every answer,
and every evidence item within 550 characters and 80 words, leaving room under
the shared gate's 650-character/90-word target. Source locations, hashes,
caller policies, and full scan warnings remain in `manifest.json`; detailed
measurements remain in `result.json`. Those names identify actual files in
the returned `artifacts` list. The private report links everything together.
The parent supplies any honest accessible artifact link and owns rendering,
quiet hours, daily limits, and queued-history deduplication. This scenario
does not implement or bypass those policies.

## Validation

The module and generated proof use only the Python **3.9+ standard library**;
both have been verified with Python 3.9.6. No newer interpreter or dependency
is required. The proof uses `sys.executable`, so the adapter's chosen runtime
is also the proof's runtime. `context["now"]` is preserved as string metadata,
including timestamps ending in `Z`; no version-specific ISO timestamp parser
is invoked.

From the repository root, using the same interpreter as the installed runner:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_scenario_connections.py -v
```

For the macOS system Python, substitute `/usr/bin/python3` for `python3`.

Fixtures and proof artifacts stay under the test directory and are removed
after each test. All committed fixtures are synthetic, without account
identifiers, contacts, private conversations, personal project paths, or
network addresses. Tests use native path handling; filesystem-specific FIFO,
symlink, and POSIX-mode checks are conditional on platform support.
No system temporary directory or live transport is used.
