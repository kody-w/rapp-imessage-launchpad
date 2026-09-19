import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

from scenarios import connections


def parser_fixture(name="consume", argument="text", line="entry",
                   marker="RESULT:", fallback="not reported", coalesce=False):
    value = f'({argument} or "")' if coalesce else argument
    return (
        f"def {name}({argument}):\n"
        '    """A fixture capability, independent of its project or symbol name."""\n'
        f"    for {line} in reversed({value}.splitlines()):\n"
        f"        if {line}.strip().startswith({marker!r}):\n"
        f"            return {line}.strip()[len({marker!r}):].strip()\n"
        f"    return {fallback!r}\n"
    )


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.scratch_parent = Path(__file__).resolve().parent / ".connections-fixtures"
        self.workspace = self.scratch_parent / uuid.uuid4().hex
        self.workspace.mkdir(mode=0o700, parents=True)
        self.addCleanup(shutil.rmtree, self.workspace)
        self.home = self.workspace / "home"
        self.home.mkdir()
        self.alpha = self.home / "unrelated-project"
        self.beta = self.home / "different-workspace"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.first = self.alpha / "worker.py"
        self.second = self.beta / "decode.py"
        self.first.write_text(parser_fixture(), encoding="utf-8")
        self.second.write_text(
            parser_fixture("summarize", "payload", "row", coalesce=True), encoding="utf-8")
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.workspace / "artifacts"),
            "now": "2026-09-19T18:30:00+00:00",
            "sources": {"connections_roots": [str(self.alpha), str(self.beta)]},
        }

    @classmethod
    def tearDownClass(cls):
        scratch = Path(__file__).resolve().parent / ".connections-fixtures"
        if scratch.exists() and not any(scratch.iterdir()):
            scratch.rmdir()

    def report(self, event):
        path = next(Path(item) for item in event["artifacts"]
                    if Path(item).name == "result.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def manifest(self, event):
        path = next(Path(item) for item in event["artifacts"]
                    if Path(item).name == "manifest.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (NotImplementedError, OSError):
            self.skipTest("the current filesystem does not allow symlink fixtures")

    def snapshot(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for root in (self.alpha, self.beta) for path in root.rglob("*")
                if path.is_file()}

    def test_actual_proof_shape_and_source_reversibility(self):
        before = self.snapshot()
        event = connections.build(self.context)
        self.assertEqual("ready", event["status"], event)
        self.assertEqual("connections", event["scenario"])
        self.assertEqual("routine", event["urgency"])
        self.assertTrue(all(key in event for key in (
            "title", "change", "impact", "action", "decision", "evidence",
            "artifacts", "fingerprint", "reason",
        )))
        json.dumps(event, allow_nan=False)
        self.assertEqual(4, len(event["artifacts"]))
        for item in event["artifacts"]:
            path = Path(item)
            self.assertTrue(path.is_file())
            self.assertIn(Path(self.context["artifact_dir"]), path.parents)
            if os.name == "posix":
                self.assertEqual(0, path.stat().st_mode & 0o077)
        report = self.report(event)
        self.assertTrue(report["verified"])
        self.assertEqual(2, report["baseline_parser_loops"])
        self.assertEqual(1, report["connected_parser_loops"])
        self.assertEqual(50, report["parser_loop_reduction_percent"])
        self.assertEqual(8, report["baseline_statements"])
        self.assertEqual(6, report["connected_statements"])
        self.assertGreater(report["fixtures"], 1000)
        self.assertEqual(2 * report["fixtures"], report["equivalence_checks"])
        self.assertEqual(4 * report["fixtures"], report["oracle_checks"])
        self.assertEqual(before, self.snapshot())
        shutil.rmtree(Path(event["artifacts"][0]).parent)
        self.assertEqual(before, self.snapshot())

    def test_private_proof_is_standalone_and_replayable(self):
        event = connections.build(self.context)
        proof = next(item for item in event["artifacts"] if Path(item).name == "proof.py")
        replay = subprocess.run(
            [sys.executable, "-I", "-S", proof], cwd=str(self.home),
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertEqual(self.report(event), json.loads(replay.stdout))
        tree = ast.parse(Path(proof).read_text(encoding="utf-8"))
        modules = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        ]
        self.assertNotIn("worker", modules)
        self.assertNotIn("decode", modules)
        self.assertNotIn("subprocess", modules)

    def test_adapters_preserve_the_real_falsey_input_difference(self):
        report = self.report(connections.build(self.context))
        edges = {edge["coalesce_falsey"]: edge for edge in report["edge_cases"]}
        self.assertEqual({"error": "AttributeError"}, edges[False]["none_before"])
        self.assertEqual({"value": "not reported"}, edges[True]["none_before"])
        for edge in edges.values():
            self.assertEqual(edge["none_before"], edge["none_after"])

    def test_ready_envelope_fits_gate_budget_without_dropping_evidence(self):
        for count in (2, connections.MAX_MATCHES):
            with self.subTest(count=count):
                for index in range(count - 2):
                    (self.alpha / f"parser_{index:02}.py").write_text(
                        parser_fixture(name=f"consume_{index}"), encoding="utf-8")
                event = connections.build(self.context)
                self.assertEqual("ready", event["status"], event)
                parts = [
                    f"{key}: {event[key]}"
                    for key in ("title", "change", "impact", "action", "decision", "reason")
                ]
                parts.extend(f"{item['source']}: {item['observation']}"
                             for item in event["evidence"])
                envelope = "\n".join(parts)
                self.assertLessEqual(len(envelope.encode("ascii")), 550)
                self.assertLessEqual(len(envelope.split()), 80)
                self.assertNotIn(str(self.home), envelope)
                self.assertEqual("routine", event["urgency"])
                self.assertNotIn("deadline", event)
                self.assertEqual(count, len(self.manifest(event)["matches"]))
                artifact_names = {Path(path).name for path in event["artifacts"]}
                for item in event["evidence"]:
                    self.assertIn(item["source"], artifact_names)

    def test_semantic_fingerprint_ignores_names_paths_time_and_order(self):
        original = connections.build(self.context)
        self.first.write_text(
            "# relocated implementation\n\n" + parser_fixture("new_name", "message", "piece"),
            encoding="utf-8",
        )
        self.first.rename(self.alpha / "moved.py")
        self.context["now"] = "2030-01-01T00:00:00Z"
        self.context["artifact_dir"] = str(self.workspace / "other-artifacts")
        self.context["sources"]["connections_roots"].reverse()
        again = connections.build(self.context)
        self.assertEqual("ready", again["status"], again)
        self.assertEqual(original["fingerprint"], again["fingerprint"])
        self.assertNotEqual(original["artifacts"], again["artifacts"])

    def test_context_timestamps_are_preserved_without_version_specific_parsing(self):
        fingerprints = set()
        for now in ("2030-01-01T00:00:00Z", "2030-01-01T00:00:00+00:00"):
            with self.subTest(now=now):
                self.context["now"] = now
                event = connections.build(self.context)
                self.assertEqual("ready", event["status"], event)
                path = next(Path(item) for item in event["artifacts"]
                            if Path(item).name == "manifest.json")
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(now, manifest["generated_at"])
                fingerprints.add(event["fingerprint"])
        self.assertEqual(1, len(fingerprints))

    def test_semantic_policy_and_marker_changes_change_fingerprint(self):
        original = connections.build(self.context)
        self.second.write_text(parser_fixture("other"), encoding="utf-8")
        strict = connections.build(self.context)
        self.assertEqual("ready", strict["status"])
        self.assertNotEqual(original["fingerprint"], strict["fingerprint"])
        for path in (self.first, self.second):
            path.write_text(parser_fixture(marker="OUTCOME:"), encoding="utf-8")
        changed_marker = connections.build(self.context)
        self.assertNotEqual(strict["fingerprint"], changed_marker["fingerprint"])

    def test_original_modules_are_never_imported_or_executed(self):
        trap = self.workspace / "should-not-exist"
        self.first.write_text(
            f"from pathlib import Path\nPath({str(trap)!r}).write_text('bad')\n"
            "raise RuntimeError('module must not run')\n" + parser_fixture(),
            encoding="utf-8",
        )
        event = connections.build(self.context)
        self.assertEqual("ready", event["status"], event)
        self.assertFalse(trap.exists())

    def test_capability_map_records_static_caller_evidence(self):
        self.first.write_text(
            parser_fixture() + "\ndef use_output(log):\n    return consume(log)\n",
            encoding="utf-8",
        )
        event = connections.build(self.context)
        manifest_path = next(Path(path) for path in event["artifacts"]
                             if Path(path).name == "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        match = next(item for item in manifest["matches"] if item["file"] == str(self.first))
        self.assertEqual([9], match["direct_call_lines"])

    def test_readme_claims_and_function_names_are_not_evidence(self):
        self.first.write_text("def result_line(value):\n    return value\n", encoding="utf-8")
        (self.alpha / "README.md").write_text(parser_fixture(), encoding="utf-8")
        event = connections.build(self.context)
        self.assertEqual("suppressed", event["status"], event)
        self.assertEqual([], event["artifacts"])

    def test_similar_but_unapproved_bodies_are_not_reconstructed(self):
        fixture = parser_fixture()
        alternatives = [
            fixture.replace("reversed(text.splitlines())", "text.splitlines()"),
            fixture.replace("len('RESULT:')", "len('OTHER:')"),
            fixture.replace("    for entry", "    print(text)\n    for entry"),
            "@unknown_decorator\n" + fixture,
            "reversed = custom_reverse\n" + fixture,
            "from unknown_module import *\n" + fixture,
        ]
        for alternative in alternatives:
            with self.subTest(source=alternative):
                self.first.write_text(alternative, encoding="utf-8")
                self.assertEqual("suppressed", connections.build(self.context)["status"])

    def test_different_prefix_or_fallback_is_not_the_same_connection(self):
        for kwargs in ({"marker": "OTHER:"}, {"fallback": "different fallback"}):
            with self.subTest(kwargs=kwargs):
                self.second.write_text(parser_fixture(**kwargs), encoding="utf-8")
                self.assertEqual("suppressed", connections.build(self.context)["status"])

    def test_missing_sources_block_without_creating_artifacts(self):
        self.context["sources"]["connections_roots"] = [str(self.workspace / "missing")]
        event = connections.build(self.context)
        self.assertEqual("blocked", event["status"])
        self.assertEqual([], event["artifacts"])
        self.assertIn("FileNotFoundError", json.dumps(event["evidence"]))
        self.assertFalse(Path(self.context["artifact_dir"]).exists())

    def test_partial_missing_evidence_is_not_silently_ignored(self):
        self.context["sources"]["connections_roots"].append(str(self.workspace / "missing"))
        event = connections.build(self.context)
        self.assertEqual("ready", event["status"], event)
        self.assertIn("1 bounded-scan issues", json.dumps(event["evidence"]))
        self.assertIn("FileNotFoundError", json.dumps(self.manifest(event)["scan"]["issues"]))

    def test_explicit_empty_roots_disable_instead_of_using_defaults(self):
        self.context["sources"] = {"connections_roots": []}
        event = connections.build(self.context)
        self.assertEqual("suppressed", event["status"])
        self.assertIn("disabled", event["reason"])

    def test_missing_root_permission_never_scans_or_uses_machine_home(self):
        self.context["sources"] = {}
        with mock.patch.object(connections, "_discover") as discover:
            with mock.patch.object(Path, "home", side_effect=AssertionError("context only")):
                event = connections.build(self.context)
            discover.assert_not_called()
        self.assertEqual("suppressed", event["status"], event)
        self.assertIn("sources.connections_roots", event["action"])
        self.assertEqual([], event["artifacts"])
        self.assertFalse(Path(self.context["artifact_dir"]).exists())

    def test_omitted_sources_cannot_enable_discovery(self):
        del self.context["sources"]
        with mock.patch.object(connections, "_discover") as discover:
            event = connections.build(self.context)
            discover.assert_not_called()
        self.assertEqual("suppressed", event["status"], event)

    def test_caller_app_home_preserves_explicit_external_folder_permissions(self):
        app_home = self.workspace / "application-data" / "home"
        app_home.mkdir(parents=True)
        self.context["home"] = str(app_home)
        self.context["artifact_dir"] = str(app_home / "artifacts" / "connections")
        with mock.patch.object(Path, "home", side_effect=AssertionError("context only")):
            event = connections.build(self.context)
        self.assertEqual("ready", event["status"], event)
        for path in event["artifacts"]:
            self.assertIn(app_home, Path(path).parents)
        source_locations = [item["file"] for item in self.manifest(event)["matches"]]
        self.assertIn(str(self.first), source_locations)
        self.assertIn(str(self.second), source_locations)

    def test_duplicate_and_overlapping_roots_do_not_duplicate_a_source(self):
        self.context["sources"]["connections_roots"] = [
            str(self.alpha), str(self.alpha), str(self.alpha / "child"),
        ]
        (self.alpha / "child").mkdir()
        self.first.rename(self.alpha / "child" / "worker.py")
        event = connections.build(self.context)
        self.assertEqual("suppressed", event["status"], event)

    def test_home_and_ancestor_scans_are_refused(self):
        for root in (str(self.home), str(self.workspace), self.home.anchor):
            with self.subTest(root=root):
                self.context["sources"]["connections_roots"] = [root]
                with mock.patch.object(connections, "_discover") as discover:
                    event = connections.build(self.context)
                    discover.assert_not_called()
                self.assertEqual("blocked", event["status"])

    def test_invalid_configuration_blocks(self):
        values = [None, "not a list", ["relative/path"], [4], [str(self.alpha)] * 5]
        for value in values:
            with self.subTest(value=value):
                self.context["sources"]["connections_roots"] = value
                self.assertEqual("blocked", connections.build(self.context)["status"])

    def test_source_symlinks_are_not_followed(self):
        self.first.unlink()
        self.symlink(self.first, self.second)
        self.assertEqual("suppressed", connections.build(self.context)["status"])
        alias = self.home / "alias"
        self.symlink(alias, self.beta)
        self.context["sources"]["connections_roots"] = [str(alias)]
        self.assertEqual("blocked", connections.build(self.context)["status"])

    def test_private_runtime_and_test_directories_are_not_read(self):
        self.first.unlink()
        for directory in ("state", "logs", "tests", ".private"):
            nested = self.alpha / directory
            nested.mkdir()
            (nested / "copy.py").write_text(parser_fixture(), encoding="utf-8")
        event = connections.build(self.context)
        self.assertEqual("suppressed", event["status"])

    def test_redirected_child_paths_are_not_followed(self):
        self.first.unlink()
        child = self.alpha / "redirected"
        child.mkdir()
        (child / "copy.py").write_text(parser_fixture(), encoding="utf-8")
        resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            if path == child:
                return self.workspace / "outside"
            return resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", redirected):
            event = connections.build(self.context)
        self.assertEqual("suppressed", event["status"], event)

    def test_large_and_invalid_sources_are_bounded(self):
        self.first.write_text(" " * (connections.MAX_FILE_BYTES + 1), encoding="utf-8")
        self.assertEqual("blocked", connections.build(self.context)["status"])
        self.first.write_text("def invalid(:", encoding="utf-8")
        self.assertEqual("blocked", connections.build(self.context)["status"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixtures need operating-system support")
    def test_nonregular_sources_are_bounded(self):
        self.first.unlink()
        os.mkfifo(self.first)
        self.assertEqual("blocked", connections.build(self.context)["status"])

    def test_directory_limits_do_not_silently_report_complete_coverage(self):
        with mock.patch.object(connections, "MAX_ENTRIES", 0):
            event = connections.build(self.context)
        self.assertEqual("blocked", event["status"])
        self.assertIn("entry limit", json.dumps(event["evidence"]))

    def test_artifacts_cannot_modify_a_source_or_follow_a_symlink(self):
        for directory in (self.alpha, self.alpha / "proof-output"):
            with self.subTest(directory=directory):
                before = self.snapshot()
                self.context["artifact_dir"] = str(directory)
                self.assertEqual("blocked", connections.build(self.context)["status"])
                self.assertEqual(before, self.snapshot())
        link = self.workspace / "artifact-link"
        self.symlink(link, self.beta)
        self.context["artifact_dir"] = str(link)
        self.assertEqual("blocked", connections.build(self.context)["status"])

    def test_failed_proof_never_becomes_ready(self):
        with mock.patch.object(connections.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("proof", 10)):
            event = connections.build(self.context)
        self.assertEqual("blocked", event["status"])
        self.assertIn("TimeoutExpired", event["reason"])
        self.assertEqual([], event["artifacts"])
        self.assertEqual([], list(Path(self.context["artifact_dir"]).iterdir()))

    def test_marker_data_cannot_inject_executable_code_or_html(self):
        marker = "RESULT:<script>\"';__import__('os').system('bad')"
        for path in (self.first, self.second):
            path.write_text(parser_fixture(marker=marker), encoding="utf-8")
        event = connections.build(self.context)
        self.assertEqual("ready", event["status"], event)
        page = Path(event["artifacts"][0]).read_text(encoding="utf-8")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
