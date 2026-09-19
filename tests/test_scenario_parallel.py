"""Standard-library fixtures; scratch data stays beside the project, never in /tmp."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch
import uuid

from scenarios import parallel


PROJECT = Path(__file__).resolve().parents[1]


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.scratch = PROJECT / ".scratch-home" / ("parallel-tests-" + uuid.uuid4().hex)
        self.home = self.scratch / "home"
        self.artifacts = self.scratch / "artifacts"
        self.source = self.home / "state" / "outbox-sent.jsonl"
        self.source.parent.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.scratch)
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.artifacts),
            "now": "2026-09-19T18:30:00+00:00", "sources": {},
        }

    def rows(self, count=18, *, exact=False, plain=False):
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(count):
            age = 10 if exact else 10 + index
            amount = 3 if exact else 3 + index // 6
            body = (
                "🔴 Example Sentinel needs you.\n"
                "'rv_world_merging,rv_validation' survived 2 automated repairs.\n"
                f"rv_world_merging: last merge {age}.0h ago; "
                f"rv_validation: gate has not run in {age}.0h - stopped, not rejecting; "
                f"alert_delivery: {amount} UNKNOWN delivery evidence record(s); "
                "w_openrappter_spin: spinning launchd job(s): example.worker (runs=10, last exit 1); "
                "rv_pr_queue: oldest PR #42 has waited 10h; "
                "w_sentinel_current: running abcdef0, 3 commit(s) behind origin/main; "
                "w_neighbor_moving: worker: alive but last WORKED 15m ago (bar 480m) - ran is not worked"
            )
            if plain:
                body = "Example manual alert\nCondition remains offline."
            rows.append({
                "at": (base + timedelta(hours=index)).isoformat(),
                "to": "private-fixture-recipient", "text": body + parallel.REPORT_SEPARATOR
                + f"https://reports.invalid/share/report-{index}.html", "attachments": [],
            })
        return rows

    def write(self, rows=None):
        self.source.write_text("".join(json.dumps(row) + "\n" for row in (
            self.rows() if rows is None else rows)), encoding="utf-8")

    def report(self, result):
        path = next(Path(item) for item in result["artifacts"] if Path(item).name == "report.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_unrelated_outgoing_messages_do_not_create_an_experiment_update(self):
        from scenarios.interrupt import delivery_key, evaluate
        rows = self.rows()
        self.write(rows)
        first = parallel.build(self.context)
        rows.append({"at": self.context["now"], "to": "fixture",
                     "text": "Unrelated SDK finding.", "attachments": []})
        self.write(rows)
        second = parallel.build(self.context)
        self.assertEqual(delivery_key(first), delivery_key(second))
        history = [{"at": self.context["now"], "scenario": "parallel",
                    "fingerprint": first["fingerprint"], "decision": "queued", "proposal": first}]
        self.assertFalse(evaluate(second, history, self.context["now"],
                                  {"quiet_hours": False, "timezone": "UTC"})["allow"])

    def test_realistic_both_fail_is_a_ready_measured_neither(self):
        self.write()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = parallel.build(self.context)
        self.assertEqual("ready", result["status"])
        self.assertEqual("parallel", result["scenario"])
        self.assertEqual("measured_neither", result["reason"])
        report = self.report(result)
        exact = report["scores"]["exact_text"]
        semantic = report["scores"]["semantic_condition"]
        self.assertEqual(0, exact["false_suppressions"])
        self.assertLess(exact["duplicate_recall"], .8)
        self.assertEqual(1, semantic["duplicate_recall"])
        self.assertGreater(semantic["false_suppressions"], 0)
        self.assertGreater(semantic["partitions"]["held_out"]["false_suppressions"], 0)
        self.assertEqual(exact["events"], semantic["events"])
        self.assertEqual(exact["duplicate_targets"], semantic["duplicate_targets"])
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual({"age_reset", "exit_code", "delivery_count", "pr_identity",
                          "commit_distance", "threshold", "predicate_change"},
                         set(report["input_stats"]["numeric_guards_exercised"]))
        self.assertEqual("routine", result["urgency"])

    def test_exact_can_win_when_actual_duplicates_are_exact(self):
        self.write(self.rows(exact=True))
        result = parallel.build(self.context)
        report = self.report(result)
        self.assertEqual("exact_text", report["outcome"])
        self.assertTrue(report["scores"]["exact_text"]["accepted"])
        self.assertFalse(report["scores"]["semantic_condition"]["accepted"])

    def test_tie_does_not_select_by_policy_name_or_timing(self):
        self.write(self.rows(plain=True))
        result = parallel.build(self.context)
        report = self.report(result)
        self.assertEqual("ready", result["status"])
        self.assertEqual("tie", report["outcome"])
        self.assertTrue(all(score["accepted"] for score in report["scores"].values()))
        self.assertIn("no automatic selection", result["decision"])

    def test_ready_answers_and_all_evidence_fit_shared_render_budget(self):
        for rows in (self.rows(), self.rows(exact=True), self.rows(plain=True)):
            with self.subTest(first=rows[0]["text"].splitlines()[0]):
                self.write(rows)
                result = parallel.build(self.context)
                self.assertEqual("ready", result["status"])
                parts = [result["title"]]
                for field in ("change", "impact", "action", "decision"):
                    self.assertTrue(result[field].strip(), field)
                    parts.append(f"{field}: {result[field]}")
                for evidence in result["evidence"]:
                    self.assertTrue(evidence["source"].strip())
                    self.assertTrue(evidence["observation"].strip())
                    parts.append(f"{evidence['source']}: {evidence['observation']}")
                visible = "\n".join(parts)
                self.assertTrue(visible.isascii())
                self.assertLessEqual(len(visible), 550)
                self.assertLessEqual(len(visible.split()), 75)
                self.assertNotIn(str(self.home), visible)
                self.assertNotIn(str(self.artifacts), visible)
                self.assertEqual(str(self.source), self.report(result)["source"])

    def test_adjudication_is_measured_and_name_independent(self):
        cases = {
            "exact_text": {"accepted": False, "suppressed_duplicates": 0},
            "semantic_condition": {"accepted": True, "suppressed_duplicates": 10},
        }
        self.assertEqual("semantic_condition", parallel._adjudicate(cases))
        cases["exact_text"] = {"accepted": True, "suppressed_duplicates": 11}
        self.assertEqual("exact_text", parallel._adjudicate(cases))
        cases["exact_text"]["suppressed_duplicates"] = 10
        self.assertEqual("tie", parallel._adjudicate(cases))
        for item in cases.values():
            item["accepted"] = False
        self.assertEqual("neither", parallel._adjudicate(cases))

    def test_worker_inputs_budgets_and_isolation_are_identical(self):
        self.write()
        result = parallel.build(self.context)
        protocols = [json.loads(Path(item).read_text()) for item in result["artifacts"]
                     if Path(item).name == "protocol.json"]
        self.assertEqual(2, len(protocols))
        self.assertEqual(protocols[0]["input_sha256"], protocols[1]["input_sha256"])
        self.assertEqual(protocols[0]["cases"], protocols[1]["cases"])
        self.assertEqual(protocols[0]["acceptance"], protocols[1]["acceptance"])
        self.assertEqual(protocols[0]["wall_seconds"], protocols[1]["wall_seconds"])
        folders = {Path(item).parent for item in result["artifacts"] if Path(item).name == "result.json"}
        self.assertEqual(2, len(folders))
        for item in result["artifacts"]:
            path = Path(item)
            self.assertTrue(path.is_relative_to(self.artifacts))
            self.assertTrue(path.is_file())
            content = path.read_text()
            self.assertNotIn("private-fixture-recipient", content)
            self.assertNotIn("Example Sentinel", content)
            self.assertNotIn("reports.invalid", content)
            if os.name == "posix":
                self.assertEqual(0, path.stat().st_mode & 0o077)

    def test_public_sources_have_no_device_specific_identity_literals(self):
        private_location = re.compile(
            r"/(?:Users|home)/[A-Za-z0-9_.-]+|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+")
        phone_or_ip = re.compile(r"(?<!\w)\+\d{10,15}\b|\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for relative in ("scenarios/parallel.py", "tests/test_scenario_parallel.py",
                         "docs/scenarios/parallel.md"):
            text = (PROJECT / relative).read_text(encoding="utf-8")
            self.assertIsNone(private_location.search(text), relative)
            self.assertIsNone(phone_or_ip.search(text), relative)
        module = ast.parse(Path(parallel.__file__).read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                             and node.func.attr == "home" for node in ast.walk(module)))

    def test_context_is_the_only_home_and_source_authority(self):
        self.write()
        with patch.dict(os.environ, {"HOME": str(self.scratch / "not-authorized"),
                                     "USERPROFILE": str(self.scratch / "not-authorized")}), \
                patch.object(Path, "home", side_effect=AssertionError("No implicit home lookup")):
            result = parallel.build(self.context)
            self.assertEqual("ready", result["status"])
            self.assertEqual(str(self.source), self.report(result)["source"])
            context = {key: value for key, value in self.context.items() if key != "home"}
            with patch.object(parallel, "_read_source") as read:
                self.assertEqual("invalid_context", parallel.build(context)["reason"])
                read.assert_not_called()

    def test_worker_environment_keeps_only_system_requirements(self):
        with patch.dict(os.environ, {"SystemRoot": "synthetic-system-root",
                                     "WINDIR": "synthetic-system-root",
                                     "HOME": "unapproved-home", "TOKEN": "fixture-secret"}, clear=True):
            environment = parallel._worker_environment(self.artifacts)
        self.assertEqual({
            "SystemRoot": "synthetic-system-root", "WINDIR": "synthetic-system-root",
            "HOME": str(self.artifacts), "USERPROFILE": str(self.artifacts), "PATH": os.defpath,
        }, environment)

    def test_fingerprint_ignores_now_timestamps_ages_urls_and_run_paths(self):
        rows = self.rows()
        self.write(rows)
        first = parallel.build(self.context)
        self.context["now"] = "2026-10-01T12:00:00Z"
        second = parallel.build(self.context)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["artifacts"], second["artifacts"])
        for row in rows:
            row["at"] = (datetime.fromisoformat(row["at"]) + timedelta(days=1)).isoformat()
            row["text"] = row["text"].replace("reports.invalid", "other.invalid")
            for pattern in parallel.AGE_FIELDS:
                row["text"] = pattern.sub(
                    lambda match: match[1] + str(float(match[2]) + 100) + match[3], row["text"])
        self.write(rows)
        third = parallel.build(self.context)
        self.assertEqual(first["fingerprint"], third["fingerprint"])
        for row in rows:
            row["text"] = row["text"].replace("UNKNOWN delivery", "FAILED delivery")
        self.write(rows)
        fourth = parallel.build(self.context)
        self.assertNotEqual(first["fingerprint"], fourth["fingerprint"])

    def test_absent_empty_and_unrepeated_histories_are_honest(self):
        result = parallel.build(self.context)
        self.assertEqual(("blocked", "missing_input"), (result["status"], result["reason"]))
        self.write([])
        self.assertEqual("suppressed", parallel.build(self.context)["status"])
        rows = self.rows(8)
        for index, row in enumerate(rows):
            row["text"] += f"\nMeaningful change {index}"
        self.write(rows)
        self.assertEqual("suppressed", parallel.build(self.context)["status"])
        self.assertFalse(self.artifacts.exists())

    def test_corrupt_record_is_not_silently_ignored(self):
        for bad in ("{broken", "[]", '{"text":"hello"}', '{"text":NaN,"to":"x","at":"bad"}'):
            with self.subTest(bad=bad):
                self.write()
                with self.source.open("a", encoding="utf-8") as handle:
                    handle.write(bad + "\n")
                result = parallel.build(self.context)
                self.assertEqual(("blocked", "corrupt_input"), (result["status"], result["reason"]))
                self.assertEqual([], result["artifacts"])
        self.source.write_bytes(b"\xff")
        self.assertEqual("corrupt_input", parallel.build(self.context)["reason"])
        rows = self.rows()
        rows[0]["unused_metadata"] = float("nan")
        self.write(rows)
        self.assertEqual("corrupt_input", parallel.build(self.context)["reason"])

    def test_invalid_context_and_aware_timestamps_required(self):
        self.write()
        for change in ({"now": "not-a-date"}, {"now": "2026-09-19T12:00:00"},
                       {"sources": []}, {"artifact_dir": None}, {"home": None}):
            with self.subTest(change=change):
                self.assertEqual("blocked", parallel.build({**self.context, **change})["status"])
        self.assertEqual("blocked", parallel.build({})["status"])
        self.assertEqual("blocked", parallel.build(None)["status"])
        rows = self.rows()
        rows[2]["at"] = "2026-09-01T00:00:00"
        self.write(rows)
        self.assertEqual("corrupt_input", parallel.build(self.context)["reason"])

    def test_explicit_source_paths_override_defaults(self):
        override = self.scratch / "explicit.jsonl"
        self.write()
        self.source.rename(override)
        for key in ("parallel_alerts", "outbox_sent"):
            result = parallel.build({**self.context, "sources": {key: str(override)}})
            self.assertEqual("ready", result["status"])
            self.assertEqual(str(override), self.report(result)["source"])
        result = parallel.build({**self.context, "sources": {
            "parallel_alerts": str(override), "outbox_sent": str(self.scratch / "absent.jsonl"),
        }})
        self.assertEqual("ready", result["status"])
        self.assertEqual(str(override), self.report(result)["source"])

    def test_artifacts_cannot_touch_read_only_home_or_the_source_tree(self):
        self.write()
        before = self.source.read_bytes()
        inside = self.home / "state" / "receipts"
        result = parallel.build({**self.context, "artifact_dir": str(inside)})
        self.assertEqual("unsafe_artifacts", result["reason"])
        self.assertFalse(inside.exists())
        result = parallel.build({**self.context, "artifact_dir": str(self.scratch)})
        self.assertEqual("unsafe_artifacts", result["reason"])
        self.assertEqual(before, self.source.read_bytes())

    def test_artifact_symlink_cannot_enter_read_only_home(self):
        self.write()
        link = self.scratch / "linked-output"
        try:
            link.symlink_to(self.home, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {type(exc).__name__}")
        result = parallel.build({**self.context, "artifact_dir": str(link / "receipts")})
        self.assertEqual("unsafe_artifacts", result["reason"])
        self.assertFalse((self.home / "receipts").exists())

    def test_source_record_text_and_case_budgets(self):
        self.write()
        with patch.object(parallel, "MAX_SOURCE_BYTES", 20):
            self.assertEqual("source_budget", parallel.build(self.context)["reason"])
        with patch.object(parallel, "MAX_RECORDS", 2):
            self.assertEqual("record_budget", parallel.build(self.context)["reason"])
        rows = self.rows()
        rows[0]["text"] = "x" * (parallel.MAX_TEXT_CHARS + 1)
        self.write(rows)
        self.assertEqual("corrupt_input", parallel.build(self.context)["reason"])
        self.write(self.rows(500))
        records, _ = parallel._read_source(self.source)
        cases, stats, _ = parallel._corpus(records)
        self.assertGreater(stats["repeat_pairs"], 48)
        self.assertLessEqual(len(cases), parallel.MAX_CASES)
        self.assertTrue(all(len(case["events"]) <= parallel.MAX_EVENTS_PER_CASE for case in cases))
        with patch.object(parallel, "MAX_CASES", 1):
            self.assertEqual("case_budget", parallel.build(self.context)["reason"])
        with patch.object(parallel, "MAX_WORKER_BYTES", 20):
            self.assertEqual("worker_input_budget", parallel.build(self.context)["reason"])
        self.assertFalse(self.artifacts.exists())

    def test_non_regular_input_is_blocked_without_hanging(self):
        result = parallel.build({**self.context, "sources": {"parallel_alerts": str(self.home)}})
        self.assertEqual("invalid_input", result["reason"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "This platform does not support FIFOs")
    def test_fifo_input_is_blocked_without_hanging(self):
        fifo = self.scratch / "input-fifo"
        os.mkfifo(fifo)
        result = parallel.build({**self.context, "sources": {"parallel_alerts": str(fifo)}})
        self.assertEqual("invalid_input", result["reason"])

    def test_worker_rejects_oversized_corpus_and_output(self):
        self.write()
        records, _ = parallel._read_source(self.source)
        cases, _, _ = parallel._corpus(records)
        event = parallel._event(records[0], 0)
        payload = json.dumps({
            "protocol": parallel.PROTOCOL,
            "cases": [{"id": "overflow", "events": [event]}] * (parallel.MAX_CASES + 1),
        })
        self.artifacts.mkdir()
        result = parallel._run_candidate("exact_text", payload, cases, self.artifacts / "oversized")
        self.assertEqual("worker_failed", result["status"])
        process = subprocess.CompletedProcess(
            ["worker"], 0, stdout=" " * (parallel.MAX_OUTPUT_BYTES + 1), stderr="")
        with patch.object(parallel.subprocess, "run", return_value=process):
            result = parallel.build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertEqual({"output_budget"},
                         {score["status"] for score in self.report(result)["scores"].values()})

    def test_both_workers_attempted_with_equal_bounds_on_timeout(self):
        self.write()
        with patch.object(parallel.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("worker", parallel.WALL_SECONDS)) as run:
            result = parallel.build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertEqual("measured_incomplete", result["reason"])
        self.assertEqual(2, run.call_count)
        left, right = run.call_args_list
        self.assertEqual(left.kwargs["input"], right.kwargs["input"])
        self.assertEqual(left.kwargs["timeout"], right.kwargs["timeout"])
        self.assertNotEqual(left.kwargs["cwd"], right.kwargs["cwd"])
        self.assertTrue(left.kwargs["start_new_session"])
        payload = json.loads(left.kwargs["input"])
        self.assertNotIn("expected", json.dumps(payload))
        self.assertNotIn("source_lines", json.dumps(payload))
        self.assertNotIn("observed_repeat", json.dumps(payload))
        self.assertNotIn("held_out", json.dumps(payload))
        self.assertEqual({"timeout"}, {score["status"] for score in self.report(result)["scores"].values()})

    def test_malformed_incomplete_or_failed_worker_never_wins(self):
        self.write()
        for output, code in (("not json", 0), ('{"predictions":[]}', 0), ("", 1)):
            with self.subTest(output=output, code=code):
                process = subprocess.CompletedProcess(["worker"], code, stdout=output, stderr="private stderr")
                with patch.object(parallel.subprocess, "run", return_value=process):
                    result = parallel.build(self.context)
                self.assertEqual("blocked", result["status"])
                self.assertEqual("incomplete", self.report(result)["outcome"])
                self.assertNotIn("private stderr", "".join(Path(path).read_text() for path in result["artifacts"]))

    def test_non_boolean_predictions_are_rejected(self):
        self.write()

        def pretend(command, **kwargs):
            payload = json.loads(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                "protocol": parallel.PROTOCOL, "policy": command[-1],
                "predictions": [{"id": case["id"], "emit": [1] * len(case["events"])}
                                for case in payload["cases"]],
            }), stderr="")

        with patch.object(parallel.subprocess, "run", side_effect=pretend):
            result = parallel.build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertEqual({"invalid_output"},
                         {score["status"] for score in self.report(result)["scores"].values()})

    def test_number_changes_and_age_resets_are_not_duplicate_labels(self):
        self.write()
        records, _ = parallel._read_source(self.source)
        left, right = records[:2]
        self.assertTrue(parallel._same_visible_condition(left, right))
        for old, new in (("last exit 1", "last exit 2"), ("3 UNKNOWN", "4 UNKNOWN"),
                         ("PR #42", "PR #43"), ("bar 480m", "bar 481m"),
                         ("last merge 10.0h", "last merge 0.0h"),
                         ("stopped, not rejecting", "running, rejecting work")):
            with self.subTest(old=old):
                changed = {**left, "body": left["body"].replace(old, new)}
                self.assertNotEqual(left["body"], changed["body"])
                self.assertFalse(parallel._same_visible_condition(left, changed))

    def test_original_intervals_are_not_rewritten_as_duplicate_pages(self):
        rows = self.rows(exact=True)
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        for index, row in enumerate(rows):
            row["at"] = (base + timedelta(hours=7 * index)).isoformat()
        self.write(rows)
        records, _ = parallel._read_source(self.source)
        cases, stats, _ = parallel._corpus(records)
        self.assertEqual(0, stats["within_window_repeat_pairs"])
        self.assertEqual(len(rows) - 1, stats["outside_window_repeat_pairs"])
        self.assertTrue(all(case["expected"] == [True, True] for case in cases
                            if case["kind"] == "original_timing"))
        self.assertTrue(any(case["expected"] == [True, False] for case in cases
                            if case["kind"] == "observed_repeat"))

    def test_cooldown_resets_scope_and_does_not_slide(self):
        self.write(self.rows(plain=True))
        records, _ = parallel._read_source(self.source)
        cases, _, _ = parallel._corpus(records)
        guards = [case for case in cases if case["partition"] == "held_out"]
        for policy in parallel.POLICIES:
            predicted = parallel._predict(policy, guards)
            for case, result in zip(guards, predicted):
                self.assertEqual(case["expected"], result["emit"], (policy, case["kind"]))

    def test_family_and_recipient_isolation(self):
        rows = self.rows()
        outsiders = self.rows(5)
        for row in outsiders:
            row["to"] = "different-private-recipient"
        self.write(rows + outsiders)
        records, _ = parallel._read_source(self.source)
        cases, stats, selected = parallel._corpus(records)
        self.assertEqual(23, stats["records"])
        self.assertEqual(18, stats["selected_records"])
        self.assertEqual(1, len({(row["route"], row["pipeline"]) for row in selected}))
        self.assertTrue(all(line <= 18 for case in cases for line in case["source_lines"]))

    def test_report_footer_parser_does_not_strip_arbitrary_content(self):
        self.assertEqual("alert", parallel._body(
            "alert" + parallel.REPORT_SEPARATOR + "https://example.invalid/share/token.html"))
        for suffix in ("https://example.invalid/important-change", "file:///share/token.html",
                       "https://example.invalid/share/token.html\nnew failure"):
            text = "alert" + parallel.REPORT_SEPARATOR + suffix
            self.assertEqual(text, parallel._body(text))

    def test_first_emissions_count_toward_safety_gate(self):
        case = {"id": "case", "partition": "held_out", "kind": "required_first",
                "expected": [True, False]}
        score = parallel._score([case], {"status": "ok",
                                        "predictions": [{"id": "case", "emit": [False, False]}]})
        self.assertEqual(1, score["false_suppressions"])
        self.assertEqual(1, score["duplicate_recall"])
        self.assertFalse(score["accepted"])


if __name__ == "__main__":
    unittest.main()
