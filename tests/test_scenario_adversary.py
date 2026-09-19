import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scenarios import adversary


class AdversaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".adversary-test-" + uuid.uuid4().hex)
        self.home = self.root / "home"
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.root / "artifacts"),
            "now": "2026-09-19T18:30:00Z", "sources": {},
        }
        self.receipt = {
            "text": "private-synthetic-text", "to": "private-synthetic-recipient",
            "at": "2026-09-19T18:00:00+00:00",
            "sent_at": "2026-09-19T18:00:02+00:00",
            "unverified": "synthetic ledger unreadable",
        }
        self.write("outbox-sent.jsonl", [self.receipt])

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, name, rows):
        path = self.state / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def build_report(self, context=None):
        envelope = adversary.build(context or self.context)
        self.assertEqual(1, len(envelope["artifacts"]))
        report = json.loads(Path(envelope["artifacts"][0]).read_text(encoding="utf-8"))
        json.dumps(envelope, allow_nan=False)
        return envelope, report

    def test_more_unverified_rows_do_not_repeat_the_same_experiment_conclusion(self):
        from scenarios.interrupt import delivery_key, evaluate
        first, _ = self.build_report()
        self.write("outbox-sent.jsonl", [self.receipt, dict(self.receipt)])
        second, _ = self.build_report()
        self.assertEqual(delivery_key(first), delivery_key(second))
        history = [{"at": self.context["now"], "scenario": "adversary",
                    "fingerprint": first["fingerprint"], "decision": "queued", "proposal": first}]
        self.assertFalse(evaluate(second, history, self.context["now"],
                                  {"quiet_hours": False, "timezone": "UTC"})["allow"])

    def test_utc_z_and_offsets_normalize_on_python39(self):
        for stamp in ("2026-09-19T18:30:00Z", "2026-09-19T18:30:00+00:00",
                      "2026-09-19T14:30:00-04:00", "2026-09-20T00:00:00+05:30"):
            with self.subTest(stamp=stamp):
                context = dict(self.context, now=stamp)
                envelope, report = self.build_report(context)
                self.assertEqual("ready", envelope["status"])
                self.assertEqual("2026-09-19T18:30:00+00:00", report["generated_at"])

    def test_falsification_uses_production_functions_and_both_controls(self):
        envelope, report = self.build_report()
        experiment = report["experiment"]
        cases = {case["name"]: case for case in experiment["cases"]}
        self.assertEqual("ready", envelope["status"])
        self.assertEqual("refuted", report["verdict"])
        self.assertEqual({"delivered": True, "no_receipt": True}, experiment["controls"])
        self.assertEqual(
            {"sent_not_delivered", "unrelated_delivery", "unreadable_dropped"},
            set(experiment["counterexamples"]),
        )
        self.assertEqual(6, experiment["mock_transport_calls"])
        self.assertEqual(0, experiment["real_transport_calls"])
        self.assertTrue(all(case["mock_returncodes"] == [0] for case in cases.values()))
        self.assertEqual(1, cases["delivered_control"]["matching_delivered_rows"])
        self.assertFalse(cases["no_receipt_control"]["returned_success"])
        self.assertEqual(40, cases["no_receipt_control"]["virtual_sleeps"])
        self.assertEqual(10.0, cases["no_receipt_control"]["virtual_seconds"])
        for name in ("sent_not_delivered", "unrelated_delivery"):
            self.assertTrue(cases[name]["returned_success"])
            self.assertEqual("", cases[name]["warning"])
            self.assertEqual(1, cases[name]["production_count_after"])
            self.assertEqual(0, cases[name]["matching_delivered_rows"])
        source = report["experiment"]["production_source"]
        self.assertEqual(hashlib.sha256(adversary.PRODUCTION_CODE.read_bytes()).hexdigest(),
                         source["sha256"])
        self.assertEqual({"_send", "_delivered_count"},
                         {item["name"] for item in source["functions"]})

    def test_unreadable_success_cannot_distinguish_delivery_from_drop(self):
        _, report = self.build_report()
        cases = {case["name"]: case for case in report["experiment"]["cases"]}
        dropped, delivered = cases["unreadable_dropped"], cases["unreadable_delivered"]
        for case in (dropped, delivered):
            self.assertTrue(case["returned_success"])
            self.assertIsNone(case["production_count_before"])
            self.assertIsNone(case["production_count_after"])
            self.assertIn("unverified", case["warning"])
        self.assertEqual(dropped["warning"], delivered["warning"])
        self.assertEqual(0, dropped["matching_delivered_rows"])
        self.assertEqual(1, delivered["matching_delivered_rows"])
        self.assertIn("does not prove non-delivery", " ".join(report["limitations"]))

    def test_survived_is_computed_not_a_canned_refutation(self):
        experiment = adversary._run_experiment()
        repaired = copy.deepcopy(experiment["cases"])
        for case in repaired:
            if case["matching_delivered_rows"] == 0:
                case["returned_success"] = False
        verdict, controls, counterexamples = adversary._assess(repaired)
        self.assertEqual("survived", verdict)
        self.assertTrue(all(controls.values()))
        self.assertEqual([], counterexamples)
        experiment.update(verdict=verdict, cases=repaired, controls=controls,
                          counterexamples=counterexamples)
        with mock.patch.object(adversary, "_run_experiment", return_value=experiment):
            envelope, _ = self.build_report()
        self.assertEqual("suppressed", envelope["status"])
        self.assertIn("bounded fixtures only", envelope["reason"])

    def test_failed_control_is_inconclusive_even_with_counterexamples(self):
        experiment = adversary._run_experiment()
        cases = experiment["cases"]
        cases[0]["returned_success"] = False
        verdict, _, counterexamples = adversary._assess(cases)
        self.assertEqual("inconclusive", verdict)
        self.assertTrue(counterexamples)
        experiment["verdict"] = verdict
        with mock.patch.object(adversary, "_run_experiment", return_value=experiment):
            envelope, _ = self.build_report()
        self.assertEqual("blocked", envelope["status"])

    def test_missing_receipts_are_not_silently_treated_as_delivery(self):
        (self.state / "outbox-sent.jsonl").unlink()
        envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual("inconclusive", report["verdict"])
        self.assertEqual(0, report["receipts"]["usable_receipts"])
        self.assertEqual("refuted", report["experiment"]["verdict"])
        self.assertEqual("missing", report["sources"]["sent"]["status"])
        self.assertIn("Mock-only verdict", envelope["reason"])

    def test_missing_production_source_fails_closed(self):
        with mock.patch.object(adversary, "PRODUCTION_CODE", self.root / "missing.py"):
            envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual("inconclusive", report["experiment"]["verdict"])
        self.assertEqual("FileNotFoundError", report["experiment"]["error"])

    def test_semantic_fingerprint_ignores_clock_paths_and_receipt_counts(self):
        first, first_report = self.build_report()
        self.write("outbox-sent.jsonl", [self.receipt, self.receipt])
        context = dict(self.context, now="2026-09-20T01:30:00-04:00",
                       artifact_dir=str(self.root / "different-artifacts"))
        second, second_report = self.build_report(context)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["evidence"], second["evidence"])
        self.assertNotEqual(first_report["receipts"]["sent"]["receipt_rows"],
                            second_report["receipts"]["sent"]["receipt_rows"])
        self.assertNotEqual(first["fingerprint"], adversary._fingerprint("survived"))

    def test_ready_notification_budget_retains_attributed_evidence(self):
        sample_size = adversary.MAX_LEDGER_LINES + 1
        self.write("outbox-sent.jsonl", [self.receipt] * sample_size)
        for role in ("sent", "unknown"):
            with self.subTest(role=role):
                if role == "unknown":
                    (self.state / "outbox-sent.jsonl").unlink()
                    self.write("outbox-unknown.jsonl", [
                        {"unknown_id": "a" * 64, "reason": "synthetic uncertainty"}
                    ] * sample_size)
                envelope, report = self.build_report()
                fields = [envelope[key] for key in (
                    "title", "change", "impact", "action", "decision", "reason")]
                for item in envelope["evidence"]:
                    fields.extend((item["source"], item["observation"]))
                    self.assertIn(item["source"], report["notification_source_map"])
                text = " ".join(fields)
                self.assertEqual("ready", envelope["status"])
                self.assertLessEqual(len(text.encode("ascii")), 550)
                self.assertLessEqual(len(text.split()), 75)
                self.assertNotIn(str(self.root), text)
                self.assertTrue(any(item["source"] == role + " ledger"
                                    for item in envelope["evidence"]))
                self.assertEqual(str(self.state / adversary.SOURCE_NAMES[role]),
                                 report["sources"][role]["path"])
                self.assertEqual("partial", report["sources"][role]["status"])
                self.assertEqual("routine", envelope["urgency"])
                self.assertNotIn("deadline", envelope)

    def test_ready_need_is_a_sentence_and_action_describes_completed_work(self):
        envelope, report = self.build_report()
        self.assertEqual(
            "No decision needed; unverified delivery must stay unknown.",
            envelope["decision"])
        self.assertEqual("refuted", report["verdict"])
        self.assertEqual(
            "Ran 6 mock trials; saved evidence. "
            "Recommend reconciling unknown sends before retry.",
            envelope["action"])
        self.assertTrue(Path(envelope["artifacts"][0]).is_file())
        self.assertFalse(any(item["observation"].endswith(("; ok.", "; partial."))
                             for item in envelope["evidence"]))
        for verdict in ("refuted", "survived", "inconclusive"):
            need = adversary._base(verdict, "synthetic reason")["decision"]
            self.assertNotEqual(verdict, need)
            self.assertTrue(need.endswith("."))
            self.assertGreater(len(need.split()), 4)

    def test_no_live_transport_import_side_effects_or_private_content_in_artifacts(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.state.iterdir()}
        modules = {name: sys.modules.get(name)
                   for name in ("outbox", "paths", "verify_outbox", "watcher_outbox")}
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("real process")), \
                mock.patch.object(adversary.time, "sleep",
                                  side_effect=AssertionError("real sleep")):
            envelope, report = self.build_report()
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in self.state.iterdir()})
        self.assertEqual(modules, {name: sys.modules.get(name) for name in modules})
        serialized = json.dumps([envelope, report])
        self.assertNotIn(self.receipt["text"], serialized)
        self.assertNotIn(self.receipt["to"], serialized)
        artifact = Path(envelope["artifacts"][0])
        self.assertEqual(Path(self.context["artifact_dir"]), artifact.parent)
        if os.name == "posix":
            self.assertEqual(0o600, artifact.stat().st_mode & 0o777)
        self.assertEqual([artifact], list(artifact.parent.iterdir()))

    def test_receipt_provenance_and_recorded_verification_are_not_delivery_claims(self):
        verified = dict(self.receipt, verified_at="2026-09-19T18:01:00Z",
                        delivery_evidence={"source": "Messages/chat.db",
                                           "message_rowid": 42, "delta_seconds": 0.5})
        unmarked = {key: value for key, value in self.receipt.items() if key != "unverified"}
        misleading = dict(unmarked, delivery_evidence={"source": "osascript", "returncode": 0})
        path = self.write("outbox-sent.jsonl", [self.receipt, verified, unmarked, misleading])
        _, report = self.build_report()
        counts = report["receipts"]["sent"]
        self.assertEqual(4, counts["receipt_rows"])
        self.assertEqual(1, counts["recorded_verified"])
        self.assertEqual(1, counts["explicitly_unverified"])
        self.assertEqual(2, counts["unmarked_without_proof"])
        self.assertEqual(3, counts["without_recorded_proof"])
        provenance = report["sources"]["sent"]
        self.assertTrue(provenance["complete"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), provenance["sha256"])
        self.assertEqual([1, 2, 3], [sample["line"] for sample in provenance["samples"]])
        self.assertIn("recorded claims", " ".join(report["limitations"]))

    def test_unknown_resolution_is_not_counted_as_verified_delivery(self):
        identity = "a" * 64
        unknown = {"unknown_id": identity, "reason": "synthetic interrupted handoff"}
        self.write("outbox-unknown.jsonl", [unknown])
        _, report = self.build_report()
        self.assertEqual(1, report["receipts"]["unknown"]["without_matching_resolution_evidence"])
        self.assertEqual("missing", report["sources"]["unknown_resolved"]["status"])
        resolution = {
            "schema": "rapp-outbox-unknown-resolution/1.0", "unknown_id": identity,
            "resolution": "delivered", "resolved_at": self.context["now"],
            "reason": "synthetic operator attestation",
        }
        self.write("outbox-unknown-resolved.jsonl", [resolution])
        _, report = self.build_report()
        self.assertEqual(1, report["receipts"]["unknown"]["with_recorded_resolution"])
        self.assertEqual(0, report["receipts"]["sent"]["recorded_verified"])
        self.write("outbox-unknown-resolved.jsonl",
                   [resolution, dict(resolution, resolution="not-delivered")])
        _, report = self.build_report()
        self.assertEqual(1, report["receipts"]["unknown"]["without_matching_resolution_evidence"])

    def test_corrupt_and_bounded_sources_report_incomplete_coverage(self):
        path = self.state / "outbox-sent.jsonl"
        path.write_bytes(json.dumps(self.receipt).encode() + b"\n{broken\n")
        _, report = self.build_report()
        self.assertEqual("invalid", report["sources"]["sent"]["status"])
        self.assertEqual(1, report["sources"]["sent"]["invalid_lines"])
        self.assertFalse(report["sources"]["sent"]["complete"])
        self.write("outbox-sent.jsonl", [self.receipt] * 3)
        with mock.patch.object(adversary, "MAX_LEDGER_LINES", 1):
            _, report = self.build_report()
        self.assertEqual(1, report["receipts"]["sent"]["receipt_rows"])
        self.assertEqual("partial", report["sources"]["sent"]["status"])
        with mock.patch.object(adversary, "MAX_LEDGER_BYTES", 10):
            envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual("bounded_prefix", report["sources"]["sent"]["sha256_scope"])

    def test_malformed_proof_and_json_never_become_verified(self):
        malformed = dict(
            self.receipt, verified_at="2026-09-19T18:01:00Z",
            delivery_evidence={"source": "Messages/chat.db",
                               "message_rowid": 42, "delta_seconds": 10 ** 500})
        path = self.write("outbox-sent.jsonl", [malformed])
        with path.open("ab") as handle:
            handle.write(b'{"value": NaN}\n')
        _, report = self.build_report()
        self.assertEqual(0, report["receipts"]["sent"]["recorded_verified"])
        self.assertEqual(1, report["sources"]["sent"]["invalid_lines"])

    def test_unreadable_source_retains_uncertainty(self):
        real_open = adversary.os.open

        def selective_open(path, flags, *args):
            if str(path).endswith("outbox-sent.jsonl"):
                raise PermissionError("synthetic permission refusal")
            return real_open(path, flags, *args)

        with mock.patch.object(adversary.os, "open", side_effect=selective_open):
            envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual("unreadable", report["sources"]["sent"]["status"])
        self.assertFalse(report["sources"]["sent"]["complete"])

    def test_probe_drift_cannot_gain_filesystem_access(self):
        source = self.root / "unsafe-probe.py"
        source.write_text(
            "def _delivered_count(to):\n    return 0\n"
            "def _send(text, to):\n    Path('outside').write_text('bad')\n"
            "    return True, ''\n", encoding="utf-8")
        with mock.patch.object(adversary, "PRODUCTION_CODE", source):
            envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual([], report["experiment"]["cases"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixtures require POSIX")
    def test_non_regular_source_does_not_block_or_mutate(self):
        fifo = self.state / "receipt-fifo"
        os.mkfifo(fifo)
        self.context["sources"] = {"adversary": {"sent": "state/receipt-fifo"}}
        envelope, report = self.build_report()
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual("not_regular", report["sources"]["sent"]["status"])

    def test_source_overrides_resolve_from_home(self):
        original = self.state / "outbox-sent.jsonl"
        alternative = self.state / "selected-receipts.jsonl"
        original.rename(alternative)
        self.context["sources"] = {"adversary": {"sent": "state/selected-receipts.jsonl"}}
        envelope, report = self.build_report()
        self.assertEqual("ready", envelope["status"])
        self.assertEqual(str(alternative), report["sources"]["sent"]["path"])
        self.assertFalse(original.exists())

    def test_protocol_context_roundtrips_without_ambient_home_discovery(self):
        context = json.loads(json.dumps(self.context))
        with mock.patch.object(Path, "home", side_effect=AssertionError("ambient home lookup")):
            envelope, _ = self.build_report(context)
        expected = {
            "scenario", "status", "title", "change", "impact", "action", "decision",
            "evidence", "artifacts", "fingerprint", "urgency", "reason",
        }
        self.assertEqual(expected, set(envelope))
        self.assertEqual("adversary", envelope["scenario"])
        self.assertEqual("ready", envelope["status"])
        self.assertEqual(envelope, json.loads(json.dumps(envelope, allow_nan=False)))

    def test_missing_home_does_not_fall_back_to_this_devices_private_state(self):
        context = dict(self.context)
        del context["home"]
        with mock.patch.object(Path, "home", side_effect=AssertionError("ambient home lookup")):
            envelope = adversary.build(context)
        self.assertEqual("blocked", envelope["status"])
        self.assertFalse(Path(context["artifact_dir"]).exists())

    def test_absolute_source_override_supports_relocated_unicode_paths(self):
        selected = self.root / "synthetic device receipts" / "message-évidence.jsonl"
        selected.parent.mkdir()
        selected.write_text(json.dumps(self.receipt) + "\n", encoding="utf-8")
        self.context["sources"] = {"adversary": {"sent": str(selected)}}
        envelope, report = self.build_report()
        self.assertEqual("ready", envelope["status"])
        self.assertEqual(str(selected), report["sources"]["sent"]["path"])

    def test_receipt_reader_handles_platforms_without_nonblocking_flag(self):
        portable_os = SimpleNamespace(
            O_RDONLY=os.O_RDONLY, open=os.open, fdopen=os.fdopen, fstat=os.fstat)
        path = self.state / "outbox-sent.jsonl"
        raw = json.dumps(self.receipt).encode("utf-8") + b"\r\n"
        path.write_bytes(raw)
        with mock.patch.object(adversary, "os", portable_os):
            provenance, records = adversary._read_ledger(path)
        self.assertEqual("ok", provenance["status"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), provenance["sha256"])
        self.assertEqual(1, len(records))

    def test_invalid_context_and_artifact_failures_are_blocked(self):
        for patch in ({"now": "not-a-date"}, {"now": "2026-09-19"},
                      {"artifact_dir": ""}, {"home": ""},
                      {"sources": []}, {"sources": {"adversary": {"transport": "live"}}}):
            with self.subTest(patch=patch):
                envelope = adversary.build(dict(self.context, **patch))
                self.assertEqual("blocked", envelope["status"])
                self.assertEqual([], envelope["artifacts"])
                json.dumps(envelope, allow_nan=False)
        with mock.patch.object(adversary, "_write_report", side_effect=PermissionError):
            envelope = adversary.build(self.context)
        self.assertEqual("blocked", envelope["status"])
        self.assertIn("Cannot persist", envelope["reason"])
        self.assertEqual(
            "No new experiment report saved; delivery handling was not changed.",
            envelope["action"])
        self.assertEqual(adversary.USER_NEEDS["inconclusive"], envelope["decision"])

    def test_artifact_must_not_overwrite_a_configured_source(self):
        destination = Path(self.context["artifact_dir"]) / "adversary-experiment.json"
        self.context["sources"] = {"adversary": {"sent": str(destination)}}
        envelope = adversary.build(self.context)
        self.assertEqual("blocked", envelope["status"])
        self.assertEqual([], envelope["artifacts"])
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
