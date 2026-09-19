"""Permission-scoped capability discovery with a private executable proof."""

from __future__ import annotations

import ast
import copy
import hashlib
import html
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import uuid


MAX_ROOTS = 4
MAX_DEPTH = 2
MAX_DIRECTORIES = 32
MAX_ENTRIES = 256
MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_AST_NODES = 25000
MAX_MATCHES = 32
SKIP_DIRECTORIES = {
    "state", "logs", "node_modules", "venv", "__pycache__", "tests",
    "fixtures", "docs", "build", "dist",
}
CAPABILITY = "last-marked-line-parser/v1"


def _parser_source(name, marker, fallback, coalesce):
    value = '(_input or "")' if coalesce else "_input"
    return (
        f"def {name}(_input):\n"
        f"    for _line in reversed({value}.splitlines()):\n"
        f"        if _line.strip().startswith({marker!r}):\n"
        f"            return _line.strip()[len({marker!r}):].strip()\n"
        f"    return {fallback!r}\n"
    )


_SHARED_SOURCE = '''
def _shared(value, marker, fallback):
    for line in reversed(value.splitlines()):
        if line.strip().startswith(marker):
            return line.strip()[len(marker):].strip()
    return fallback
'''

# This runner and the reconstructed function templates are authored code.
# Original modules, decorators, annotations and function bodies are never run.
_PROOF_RUNNER = '''
import ast
import hashlib
import itertools
import json
from pathlib import Path


def _outcome(function, value):
    try:
        return {"value": function(value)}
    except AttributeError:
        return {"error": "AttributeError"}


def _oracle(value, coalesce):
    if coalesce and not value:
        value = ""
    if not isinstance(value, str):
        return {"error": "AttributeError"}
    answer = FALLBACK
    for line in value.splitlines():
        stripped = line.strip()
        if stripped[:len(MARKER)] == MARKER:
            answer = stripped[len(MARKER):].strip()
    return {"value": answer}


def verify():
    cases = [None, False, 0, 1, [], {}, ["noise"], {"log": "noise"},
             "", "noise", " " * 4096,
             MARKER + " FIXED café 🚀",
             "\\u00a0" + MARKER + " FIXED unicode space\\u00a0",
             MARKER + " FIRST\\r" + MARKER + " LAST",
             MARKER + " FIRST\\x85" + MARKER + " LAST",
             MARKER + " FIRST\\u2029" + MARKER + " LAST"]
    lines = ["noise", MARKER + " FIXED first", "  " + MARKER + " BLOCKED last ",
             "x" + MARKER + " not a marker", MARKER, "\\t",
             MARKER.lower() + " lowercase"]
    for size in range(4):
        for parts in itertools.product(lines, repeat=size):
            for separator in ("\\n", "\\r\\n", "\\u2028"):
                cases.append(separator.join(parts))
    cases = list({json.dumps(value, sort_keys=True): value
                  for value in cases}.values())
    edges = []
    for index, coalesce in enumerate(POLICIES):
        before = globals()["_before_" + str(index)]
        after = globals()["_after_" + str(index)]
        for value in cases:
            expected = _oracle(value, coalesce)
            original = _outcome(before, value)
            connected = _outcome(after, value)
            if original != expected or connected != expected:
                raise AssertionError("fixture equivalence failed")
        edges.append({"source_index": index, "coalesce_falsey": coalesce,
                      "none_before": _outcome(before, None),
                      "none_after": _outcome(after, None)})

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    baseline = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name.startswith("_before_")]
    connected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and (node.name == "_shared" or node.name.startswith("_after_"))]

    def count(functions, kind):
        return sum(isinstance(node, kind) and not isinstance(node, ast.FunctionDef)
                   for function in functions for node in ast.walk(function))

    before_loops = count(baseline, ast.For)
    after_loops = count(connected, ast.For)
    before_statements = count(baseline, ast.stmt)
    after_statements = count(connected, ast.stmt)
    if before_loops != len(POLICIES) or after_loops != 1:
        raise AssertionError("unexpected proof structure")
    if before_statements <= after_statements:
        raise AssertionError("no demonstrated reduction")
    return {
        "schema": "connections-proof/v1", "verified": True,
        "source_implementations": len(POLICIES), "adapters": len(POLICIES),
        "fixtures": len(cases), "equivalence_checks": len(cases) * len(POLICIES),
        "oracle_checks": len(cases) * len(POLICIES) * 2,
        "baseline_parser_loops": before_loops,
        "connected_parser_loops": after_loops,
        "parser_loops_removed": before_loops - after_loops,
        "parser_loop_reduction_percent": round(100 * (1 - after_loops / before_loops), 1),
        "baseline_statements": before_statements,
        "connected_statements": after_statements,
        "statements_removed": before_statements - after_statements,
        "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "edge_cases": edges,
        "measurement_scope": "isolated generated proof; not production runtime or source edits"
    }


if __name__ == "__main__":
    print(json.dumps(verify(), sort_keys=True))
'''


def _fingerprint(identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "connections:" + hashlib.sha256(encoded).hexdigest()


def _result(status, reason, evidence, identity):
    return {
        "scenario": "connections", "status": status,
        "title": "No proven local capability connection",
        "change": "No source repositories were changed.",
        "impact": "No benefit is claimed without a passing isolated proof.",
        "action": "Review the source configuration and bounded discovery evidence.",
        "decision": "Do not integrate or publish anything.",
        "evidence": evidence, "artifacts": [],
        "fingerprint": _fingerprint(identity), "urgency": "routine",
        "reason": reason,
    }


def _absolute_path(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("paths must be nonempty absolute strings")
    path = Path(os.path.abspath(value))
    if path.resolve() != path:
        raise ValueError("symlink paths are not authorized")
    return path


def _roots(context):
    sources = context.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("sources must be a dictionary")
    home = _absolute_path(context["home"])
    raw = sources.get("connections_roots", [])
    if not isinstance(raw, list) or len(raw) > MAX_ROOTS:
        raise ValueError(f"connections_roots must be a list of at most {MAX_ROOTS} paths")
    roots = sorted({_absolute_path(value) for value in raw})
    for root in roots:
        if root == home or root in home.parents:
            raise ValueError("home and its ancestors are not bounded source roots")
    return roots


def _read_python(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("not a bounded regular Python file")
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Python file grew beyond the byte limit")
    tree = ast.parse(content, filename=str(path))
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ValueError("Python AST exceeds the node limit")
    return tree, hashlib.sha256(content).hexdigest()


def _shadows_builtins(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in {"len", "reversed", "__builtins__"}:
                return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in {"len", "reversed"}:
                return True
        if isinstance(node, ast.alias):
            if node.name == "*" or (node.asname or node.name) in {"len", "reversed"}:
                return True
    return False


def _recognize(function):
    args = function.args
    if (function.decorator_list or args.posonlyargs or len(args.args) != 1
            or args.defaults or args.kwonlyargs or args.vararg or args.kwarg):
        return None
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if (len(body) != 2 or not isinstance(body[0], ast.For)
            or not isinstance(body[0].target, ast.Name)
            or not isinstance(body[-1], ast.Return)
            or not isinstance(body[-1].value, ast.Constant)):
        return None
    fallback = body[-1].value.value
    if not isinstance(fallback, str) or len(fallback) > 256:
        return None
    names = (args.args[0].arg, body[0].target.id)
    if len(set(names)) != 2 or set(names) & {"len", "reversed"}:
        return None
    nodes = list(ast.walk(function))
    if len(nodes) > 160:
        return None
    prefixes = [
        node.args[0].value for node in nodes
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith" and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    if len(prefixes) != 1:
        return None
    marker = prefixes[0]
    if (not marker or len(marker) > 128 or marker != marker.strip()
            or len(marker.splitlines()) != 1):
        return None

    class Normalize(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.copy_location(ast.Name(
                id=dict(zip(names, ("_input", "_line"))).get(node.id, node.id),
                ctx=node.ctx), node)

    normalized = copy.deepcopy(function)
    normalized.name = "_parser"
    normalized.body = copy.deepcopy(body)
    normalized.args.args[0].arg = "_input"
    normalized.args.args[0].annotation = None
    normalized.args.args[0].type_comment = None
    normalized.returns = None
    normalized.type_comment = None
    normalized = Normalize().visit(normalized)
    shape = ast.dump(normalized, include_attributes=False)
    for coalesce in (False, True):
        approved = ast.parse(_parser_source("_parser", marker, fallback, coalesce)).body[0]
        if shape == ast.dump(approved, include_attributes=False):
            return {
                "capability": CAPABILITY, "marker": marker, "fallback": fallback,
                "coalesce_falsey": coalesce,
                "shape_sha256": hashlib.sha256(shape.encode()).hexdigest(),
            }
    return None


def _discover(roots):
    matches, issues, seen_files = [], [], set()
    files_read = 0
    for root in roots:
        pending, directories, files = [(root, 0)], 0, 0
        while pending:
            directory, depth = pending.pop(0)
            directories += 1
            if directories > MAX_DIRECTORIES:
                issues.append(f"{root}: directory limit reached")
                break
            try:
                with os.scandir(directory) as entries:
                    entries = list(itertools.islice(entries, MAX_ENTRIES + 1))
                if len(entries) > MAX_ENTRIES:
                    issues.append(f"{directory}: entry limit reached; directory skipped")
                    continue
            except OSError as error:
                issues.append(f"{directory}: {type(error).__name__}")
                continue
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                path = Path(entry.path)
                try:
                    if path.resolve() != path:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < MAX_DEPTH and entry.name not in SKIP_DIRECTORIES:
                            pending.append((path, depth + 1))
                        continue
                    if (not entry.name.endswith(".py")
                            or entry.name.startswith(("test_", "prove_"))
                            or path in seen_files):
                        continue
                    files += 1
                    if files > MAX_FILES:
                        issues.append(f"{root}: Python file limit reached")
                        pending.clear()
                        break
                    seen_files.add(path)
                    tree, digest = _read_python(path)
                    files_read += 1
                    if _shadows_builtins(tree):
                        continue
                    for function in tree.body:
                        if not isinstance(function, ast.FunctionDef):
                            continue
                        match = _recognize(function)
                        if match:
                            calls = [
                                node.lineno for node in ast.walk(tree)
                                if isinstance(node, ast.Call)
                                and isinstance(node.func, ast.Name)
                                and node.func.id == function.name
                            ]
                            match.update(root=str(root), file=str(path),
                                         relative_file=str(path.relative_to(root)),
                                         symbol=function.name, line=function.lineno,
                                         file_sha256=digest,
                                         direct_call_lines=sorted(calls)[:64])
                            matches.append(match)
                            if len(matches) >= MAX_MATCHES:
                                issues.append("capability match limit reached")
                                return matches, issues, files_read
                except (OSError, ValueError, SyntaxError, RecursionError) as error:
                    detail = f": {error}" if isinstance(error, ValueError) else ""
                    issues.append(f"{path}: {type(error).__name__}{detail}")
    return matches, issues, files_read


def _select(matches):
    groups = {}
    for match in matches:
        key = (match["capability"], match["marker"], match["fallback"])
        groups.setdefault(key, []).append(match)
    candidates = [
        group for group in groups.values()
        if len({item["file"] for item in group}) >= 2
    ]
    if not candidates:
        return []
    candidates.sort(key=lambda group: (-len(group), group[0]["marker"], group[0]["fallback"]))
    return sorted(candidates[0], key=lambda item: (item["file"], item["line"]))


def _proof_source(group):
    marker, fallback = group[0]["marker"], group[0]["fallback"]
    parts = ["#!/usr/bin/env python3\n",
             "# Isolated trusted-template reconstruction; never imports the source projects.\n"]
    for index, match in enumerate(group):
        parts.append(_parser_source(f"_before_{index}", marker, fallback, match["coalesce_falsey"]))
    parts.append(_SHARED_SOURCE)
    for index, match in enumerate(group):
        value = '(value or "")' if match["coalesce_falsey"] else "value"
        parts.append(
            f"def _after_{index}(value):\n"
            f"    return _shared({value}, {marker!r}, {fallback!r})\n"
        )
    parts.extend([
        f"\nMARKER = {marker!r}\nFALLBACK = {fallback!r}\n",
        f"POLICIES = {[match['coalesce_falsey'] for match in group]!r}\n",
        _PROOF_RUNNER,
    ])
    return "\n".join(parts)


def _write_private(path, text):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _make_proof(context, roots, group, fingerprint, scan):
    base = _absolute_path(context["artifact_dir"])
    if any(base == root or root in base.parents for root in roots):
        raise ValueError("artifacts must be outside every source root")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    bundle = base / ("connection-" + fingerprint.split(":")[1][:12] + "-" + uuid.uuid4().hex[:12])
    bundle.mkdir(mode=0o700)
    try:
        proof = bundle / "proof.py"
        _write_private(proof, _proof_source(group))
        manifest = {
            "schema": "connections-capabilities/v1", "fingerprint": fingerprint,
            "generated_at": context.get("now"), "roots": [str(root) for root in roots],
            "matches": group, "scan": scan,
            "safety": "AST only; original source code is never executed or modified",
        }
        _write_private(bundle / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        execution = subprocess.run(
            [sys.executable, "-I", "-S", str(proof)],
            cwd=str(bundle), capture_output=True, text=True, timeout=10, check=False,
        )
        if execution.returncode:
            raise ValueError("isolated proof did not pass")
        report = json.loads(execution.stdout)
        if (report.get("verified") is not True
                or report.get("schema") != "connections-proof/v1"
                or report.get("source_implementations") != len(group)
                or report.get("equivalence_checks", 0) < 100
                or report.get("parser_loops_removed") != len(group) - 1):
            raise ValueError("isolated proof returned an invalid measurement")
        _write_private(bundle / "result.json", json.dumps(report, indent=2) + "\n")
        command = shlex.join([sys.executable, "-I", "-S", str(proof)])
        page = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            "<title>Local capability connection proof</title>"
            "<h1>One result parser, preserved caller policies</h1>"
            "<p>Private, isolated proof. No source repository was changed, "
            "imported, merged or published. No messages were queued or sent.</p>"
            "<p>The benefit is fewer duplicated parsing loops and executable "
            "statements in this proof, not a production speedup or saved time.</p>"
            f"<h2>Measured result</h2><pre>{html.escape(json.dumps(report, indent=2))}</pre>"
            f"<h2>Source-backed capabilities</h2><pre>{html.escape(json.dumps(manifest, indent=2))}</pre>"
            f"<h2>Rerun</h2><pre>{html.escape(command)}</pre>"
            '<p><a href="proof.py">Runnable proof</a> · '
            '<a href="result.json">Measurements</a> · '
            '<a href="manifest.json">Evidence</a></p>'
            "<p>Reversal: delete only this generated directory. "
            "These are local artifacts, not a hosted or publicly accessible link.</p></html>"
        )
        _write_private(bundle / "index.html", page)
        return report, [str(bundle / name) for name in
                        ("index.html", "proof.py", "result.json", "manifest.json")]
    except Exception:
        shutil.rmtree(bundle)
        raise


def build(context: dict) -> dict:
    """Discover one supported cross-file capability connection; never send it."""
    try:
        roots = _roots(context)
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
        return _result("blocked", f"Invalid source configuration: {error}",
                       [], {"status": "blocked", "cause": "source-configuration"})
    if not roots:
        result = _result("suppressed", "No source roots authorized; discovery is disabled.",
                         [], {"status": "suppressed", "cause": "disabled"})
        result["action"] = (
            "Authorize repository folders in the app, then pass their absolute paths "
            "through sources.connections_roots."
        )
        return result
    matches, issues, files_read = _discover(roots)
    scan = {
        "python_files_read": files_read, "approved_functions": len(matches),
        "issues": issues, "max_depth": MAX_DEPTH, "max_files_per_root": MAX_FILES,
        "max_bytes_per_file": MAX_FILE_BYTES,
    }
    evidence = [{
        "source": "bounded Python AST scan",
        "observation": (
            f"{files_read} Python files read; {len(matches)} approved parser bodies found. "
            f"Depth <= {MAX_DEPTH}; <= {MAX_FILES} files/root; <= {MAX_FILE_BYTES} bytes/file. "
            "Only the documented parser family is supported; no whole-estate claim."
        ),
    }]
    evidence.extend({"source": "source availability", "observation": issue} for issue in issues)
    group = _select(matches)
    if not group:
        status = "blocked" if issues else "suppressed"
        return _result(
            status,
            "No cross-file approved parser connection was established."
            + (" Some authorized evidence was unavailable or exceeded a bound." if issues else ""),
            evidence, {"status": status, "cause": "no-proven-connection",
                       "roots": [str(root) for root in roots]},
        )
    identity = {
        "capability": CAPABILITY, "marker": group[0]["marker"],
        "fallback": group[0]["fallback"],
        "input_policies": sorted(match["coalesce_falsey"] for match in group),
    }
    fingerprint = _fingerprint(identity)
    for match in group:
        policy = "falsey input becomes empty text" if match["coalesce_falsey"] else "strict input"
        evidence.append({
            "source": f"{match['file']}:{match['line']}",
            "observation": (
                f"{match['symbol']}: complete AST matches {CAPABILITY}; {policy}; "
                f"direct local call lines {match['direct_call_lines']}; "
                f"source SHA-256 {match['file_sha256']}."
            ),
        })
    try:
        report, artifacts = _make_proof(context, roots, group, fingerprint, scan)
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        return _result(
            "blocked", f"Private isolated proof unavailable: {type(error).__name__}.",
            evidence, {"status": "blocked", "cause": "proof-unavailable", "connection": identity},
        )
    result = _result(
        "ready",
        "Fixture equivalence only; no production speedup claimed.",
        [
            {
                "source": "manifest.json",
                "observation": (
                    f"{len(group)} exact AST matches; {len(issues)} bounded-scan issues."
                ),
            },
            {
                "source": "result.json",
                "observation": (
                    f"{report['fixtures']} inputs/parser; "
                    f"{report['oracle_checks']} oracle checks passed."
                ),
            },
        ],
        identity,
    )
    result.update({
        "title": "One shared parser, proven in isolation",
        "change": f"{len(group)} source functions duplicate one result parser.",
        "impact": (
            f"Proof: {report['baseline_parser_loops']}->{report['connected_parser_loops']} loops; "
            f"{report['baseline_statements']}->{report['connected_statements']} statements; "
            f"{report['equivalence_checks']} checks pass."
        ),
        "action": "Review the private proof and adapters.",
        "decision": "Keep isolated; approve integration separately.",
        "artifacts": artifacts,
    })
    return result
