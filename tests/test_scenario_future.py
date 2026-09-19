import copy
import importlib.util
import json
import os
import shutil
import stat
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scenarios import future
from scenarios.future import JSON_LIMIT, build


NOW = datetime(2026, 9, 19, 18, 30, tzinfo=timezone.utc)


def iso(stamp):
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


class FutureTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".scratch-home"
        self.root = parent / ("future-tests-" + uuid.uuid4().hex)
        self.home = self.root / "home"
        self.artifacts = self.root / "artifacts"
        (self.home / "state").mkdir(parents=True)
        (self.home / "logs").mkdir()
        self.addCleanup(self.cleanup)
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.artifacts),
            "now": iso(NOW), "sources": {},
        }
        self.verdict_path = self.home / "state" / "last_verdict.json"
        self.plan_path = self.home / "state" / "future-plan.json"
        self.log_path = self.home / "logs" / "sentinel-2026-09.log"
        self.plan = {
            "schema": "storykeeper.future-plan/v1",
            "updated_at": iso(NOW - timedelta(minutes=30)),
            "projects": [{
                "id": "weekly-export",
                "title": "Weekly export",
                "status": "active",
                "deadline": iso(NOW + timedelta(hours=8)),
                "impact_level": "high",
                "impact": "The scheduled internal report would miss its review window.",
                "dependencies": [{
                    "check_id": "export_valid",
                    "required": True,
                    "recovery_hours": 8,
                    "downstream_hours": 2,
                    "recovery_basis": "Maintainer's remaining-work estimate after reproducing the failure.",
                }],
            }],
        }

    def cleanup(self):
        shutil.rmtree(self.root)
        try:
            self.root.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def native_verdict(self, stamp=None, ok=False, extra=None):
        # Exact shape written by health.py, not a fictional generic failure format.
        checks = [{
            "id": "export_valid", "ok": ok, "severity": "critical",
            "detail": "latest export 3h old", "produced_by": "check_export",
        }, {
            "id": "other_gate", "ok": True, "severity": "warn",
            "detail": "passed", "produced_by": "check_other",
        }]
        checks.extend(extra or [])
        failed = [row["id"] for row in checks if not row["ok"]]
        critical = [row["id"] for row in checks if not row["ok"] and row["severity"] == "critical"]
        return {
            "generated": iso(stamp or NOW - timedelta(minutes=5)),
            "status": "critical" if critical else ("degraded" if failed else "healthy"),
            "checks": checks, "failed": failed, "critical": critical,
            "summary": "synthetic native health fixture",
        }

    def write_log(self, entries=None):
        if entries is None:
            entries = [(NOW - timedelta(minutes=m), ["export_valid"]) for m in (50, 35, 20, 5)]
        lines = ["[2026-09-19T16:00:00+00:00] dashboard refresh complete"]
        for stamp, failed in entries:
            status = "critical" if failed else "healthy"
            # sentinel.log() writes Python's list repr and literal "none", not JSON.
            lines.append(
                f"[{stamp.isoformat(timespec='seconds')}] status={status} "
                f"failing={failed if failed else 'none'}")
        self.log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def fixtures(self):
        self.write_json(self.plan_path, self.plan)
        self.write_json(self.verdict_path, self.native_verdict())
        self.write_log()

    def assert_envelope(self, result):
        expected = {
            "scenario", "status", "title", "change", "impact", "action", "decision",
            "evidence", "artifacts", "fingerprint", "urgency", "reason",
        }
        self.assertTrue(expected <= result.keys())
        self.assertEqual("future", result["scenario"])
        self.assertIn(result["status"], ("ready", "suppressed", "blocked"))
        self.assertIn(result["urgency"], ("routine", "time_sensitive", "urgent"))
        self.assertTrue(result["fingerprint"].startswith("future:"))
        json.dumps(result, allow_nan=False)
        for row in result["evidence"]:
            self.assertEqual({"source", "observation"}, set(row))

    def test_no_evidence_is_blocked_and_has_no_side_effects(self):
        result = build(self.context)
        self.assert_envelope(result)
        self.assertEqual("blocked", result["status"])
        self.assertIn("Missing configured evidence", result["reason"])
        self.assertEqual([], result["artifacts"])
        self.assertEqual("", result["decision"])
        self.assertFalse(self.artifacts.exists())

    def test_native_health_alone_does_not_invent_a_deadline(self):
        self.write_json(self.verdict_path, self.native_verdict())
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("future-plan.json", result["reason"])
        self.assertNotIn("deadline", result)
        self.assertIn("2 checks, 1 nonpassing", result["evidence"][0]["observation"])

    def test_supported_forecast_has_dated_horizon_uncertainty_and_one_decision(self):
        self.fixtures()
        result = build(self.context)
        self.assert_envelope(result)
        self.assertEqual("ready", result["status"], result["reason"])
        self.assertIn(iso(NOW), result["change"])
        self.assertIn(iso(NOW + timedelta(days=7)), result["change"])
        self.assertEqual(self.plan["projects"][0]["deadline"], result["deadline"])
        self.assertIn("if", result["change"])
        self.assertIn("conditional", result["reason"])
        self.assertIn("not a calibrated probability", result["reason"])
        self.assertEqual(1, result["decision"].count("?"))
        self.assertEqual(1, len(result["artifacts"]))
        artifact = Path(result["artifacts"][0])
        self.assertEqual(self.artifacts, artifact.parent)
        if os.name == "posix":
            self.assertEqual(0o600, stat.S_IMODE(artifact.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(self.artifacts.stat().st_mode))
        text = artifact.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(result["decision"]))
        self.assertIn("Deleting it reverses", text)
        self.assertIn("not independently verified", text)
        self.assertIn("missing visibility, not a broken service", text)
        for path in (self.plan_path, self.verdict_path, self.log_path):
            self.assertIn(str(path), text)
            self.assertNotIn(str(path), json.dumps(result["evidence"]))
        self.assertIn(iso(NOW - timedelta(minutes=50)), text)
        self.assertIn("plan:weekly-export", text)
        self.assertIn("verdict/log:export_valid", text)

    def test_single_snapshot_is_not_persistence_evidence(self):
        self.fixtures()
        self.write_log([(NOW - timedelta(minutes=5), ["export_valid"])])
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("Insufficient corroboration", result["reason"])
        self.assertFalse(self.artifacts.exists())

    def test_recovery_estimate_and_basis_are_required(self):
        self.fixtures()
        for key in ("recovery_hours", "downstream_hours", "recovery_basis"):
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan)
                del plan["projects"][0]["dependencies"][0][key]
                self.write_json(self.plan_path, plan)
                self.assertEqual("blocked", build(self.context)["status"])

    def test_horizon_excludes_past_and_more_than_seven_days(self):
        self.fixtures()
        for delta in (timedelta(seconds=-1), timedelta(0), timedelta(days=7, seconds=1)):
            with self.subTest(delta=delta):
                self.plan["projects"][0]["deadline"] = iso(NOW + delta)
                self.write_json(self.plan_path, self.plan)
                result = build(self.context)
                self.assertEqual("suppressed", result["status"])
                self.assertEqual([], result["artifacts"])

    def test_exact_seven_day_boundary_is_included(self):
        self.fixtures()
        self.plan["projects"][0]["deadline"] = iso(NOW + timedelta(days=7))
        self.plan["projects"][0]["dependencies"][0]["recovery_hours"] = 168
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("ready", result["status"])
        self.assertEqual("routine", result["urgency"])

    def test_stale_or_future_dated_verdict_is_blocked(self):
        self.fixtures()
        for delta in (timedelta(hours=-2, seconds=-1), timedelta(seconds=1)):
            with self.subTest(delta=delta):
                self.write_json(self.verdict_path, self.native_verdict(NOW + delta))
                self.assertEqual("blocked", build(self.context)["status"])

    def test_stale_plan_is_not_assumed_current(self):
        self.fixtures()
        self.plan["updated_at"] = iso(NOW - timedelta(days=2))
        self.write_json(self.plan_path, self.plan)
        self.assertEqual("blocked", build(self.context)["status"])

    def test_naive_or_invalid_time_never_gets_a_local_timezone_guess(self):
        self.fixtures()
        for stamp in ("2026-09-19T18:30:00", "tomorrow", None):
            with self.subTest(stamp=stamp):
                result = build({**self.context, "now": stamp})
                self.assertEqual("blocked", result["status"])

    def test_equivalent_timezone_deadlines_have_same_fingerprint(self):
        self.fixtures()
        first = build(self.context)
        self.plan["projects"][0]["deadline"] = "2026-09-19T22:30:00-04:00"
        self.write_json(self.plan_path, self.plan)
        second = build(self.context)
        self.assertEqual("ready", second["status"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_fingerprint_ignores_clock_ages_new_samples_and_artifact_location(self):
        self.fixtures()
        first = build(self.context)
        later = NOW + timedelta(minutes=10)
        verdict = self.native_verdict(later - timedelta(minutes=1))
        verdict["checks"][0]["detail"] = "latest export 4h old"
        self.write_json(self.verdict_path, verdict)
        self.plan["updated_at"] = iso(later)
        self.write_json(self.plan_path, self.plan)
        result = build({
            **self.context, "now": iso(later),
            "artifact_dir": str(self.root / "other-artifacts"),
        })
        self.assertEqual("ready", result["status"])
        self.assertEqual(first["fingerprint"], result["fingerprint"])
        self.assertNotEqual(first["change"], result["change"])
        self.assertEqual(first["evidence"], result["evidence"])

    def test_a_changed_deadline_is_a_new_semantic_event(self):
        self.fixtures()
        first = build(self.context)
        self.plan["projects"][0]["deadline"] = iso(NOW + timedelta(hours=7))
        self.write_json(self.plan_path, self.plan)
        self.assertNotEqual(first["fingerprint"], build(self.context)["fingerprint"])

    def test_most_consequential_supported_project_wins_before_earliest(self):
        self.fixtures()
        lower = copy.deepcopy(self.plan["projects"][0])
        lower.update(id="sooner-low-impact", title="Earlier low-impact task",
                     deadline=iso(NOW + timedelta(hours=1)), impact_level="low")
        self.plan["projects"].insert(0, lower)
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("ready", result["status"])
        self.assertEqual("plan:weekly-export", result["evidence"][0]["source"])
        self.plan["projects"].reverse()
        self.write_json(self.plan_path, self.plan)
        self.assertEqual(result["fingerprint"], build(self.context)["fingerprint"])

    def test_a_recovered_required_gate_suppresses_forecast(self):
        self.fixtures()
        self.write_json(self.verdict_path, self.native_verdict(ok=True))
        result = build(self.context)
        self.assertEqual("suppressed", result["status"])
        self.assertEqual([], result["artifacts"])

    def test_repeated_failure_with_adequate_runway_does_not_invent_a_slip(self):
        self.fixtures()
        self.plan["projects"][0]["deadline"] = iso(NOW + timedelta(days=3))
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("suppressed", result["status"])
        self.assertEqual([], result["artifacts"])

    def test_missing_gate_cannot_be_replaced_with_an_unrelated_failure(self):
        self.fixtures()
        self.plan["projects"][0]["dependencies"][0]["check_id"] = "unobserved_gate"
        self.write_json(self.plan_path, self.plan)
        self.assertEqual("blocked", build(self.context)["status"])

    def test_optional_dependency_does_not_establish_causality(self):
        self.fixtures()
        self.plan["projects"][0]["dependencies"][0]["required"] = False
        self.write_json(self.plan_path, self.plan)
        self.assertEqual("blocked", build(self.context)["status"])

    def test_completed_projects_are_suppressed(self):
        self.fixtures()
        self.plan["projects"][0] = {"id": "weekly-export", "status": "completed"}
        self.write_json(self.plan_path, self.plan)
        self.assertEqual("suppressed", build(self.context)["status"])

    def test_intervening_recovery_breaks_the_failure_streak(self):
        self.fixtures()
        self.write_log([
            (NOW - timedelta(minutes=50), ["export_valid"]),
            (NOW - timedelta(minutes=20), []),
            (NOW - timedelta(minutes=5), ["export_valid"]),
        ])
        self.assertEqual("blocked", build(self.context)["status"])

    def test_sparse_observations_are_not_a_continuous_failure(self):
        self.fixtures()
        self.write_log([
            (NOW - timedelta(hours=3), ["export_valid"]),
            (NOW - timedelta(minutes=5), ["export_valid"]),
        ])
        self.assertEqual("blocked", build(self.context)["status"])

    def test_newer_healthy_log_prevents_forecast_from_older_failed_snapshot(self):
        self.fixtures()
        with self.log_path.open("a") as stream:
            stream.write(f"[{iso(NOW)}] status=healthy failing=none\n")
        self.assertEqual("blocked", build(self.context)["status"])

    def test_contradictory_native_snapshot_is_blocked(self):
        self.fixtures()
        verdict = self.native_verdict()
        verdict["failed"] = []
        self.write_json(self.verdict_path, verdict)
        self.assertEqual("blocked", build(self.context)["status"])

    def test_invalid_json_and_unbounded_input_fail_closed(self):
        self.fixtures()
        for text in (
                "{", "[]", '{"projects": NaN}', '{"projects": [], "projects": []}',
                " " * (JSON_LIMIT + 1)):
            with self.subTest(length=len(text)):
                self.plan_path.write_text(text)
                self.assertEqual("blocked", build(self.context)["status"])

    def test_missing_explicit_history_is_not_silently_ignored(self):
        self.fixtures()
        context = {
            **self.context,
            "sources": {"future_history": [str(self.log_path), str(self.root / "missing.log")]},
        }
        self.assertEqual("blocked", build(context)["status"])

    def test_bounded_log_tail_discards_partial_first_record(self):
        self.fixtures()
        original = self.log_path.read_text()
        self.log_path.write_text("unrelated output " * 20000 + "\n" + original)
        self.assertEqual("ready", build(self.context)["status"])

    def test_month_boundary_reads_only_two_explicit_native_logs(self):
        self.fixtures()
        month_start = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
        self.plan["updated_at"] = iso(month_start - timedelta(minutes=30))
        self.plan["projects"][0]["deadline"] = iso(month_start + timedelta(hours=8))
        self.write_json(self.plan_path, self.plan)
        self.write_json(
            self.verdict_path, self.native_verdict(month_start - timedelta(minutes=1)))
        self.write_log([
            (month_start - timedelta(minutes=30), ["export_valid"]),
            (month_start - timedelta(minutes=15), ["export_valid"]),
        ])
        october = self.home / "logs" / "sentinel-2026-10.log"
        october.write_text(
            f"[{iso(month_start - timedelta(minutes=1))}] "
            "status=critical failing=['export_valid']\n")
        with mock.patch.object(future, "_read", wraps=future._read) as reads:
            result = build({**self.context, "now": iso(month_start)})
        self.assertEqual("ready", result["status"], result["reason"])
        log_sources = {
            str(call.args[0]) for call in reads.call_args_list if call.kwargs.get("tail")
        }
        self.assertEqual({str(self.log_path), str(october)}, log_sources)

    def test_log_parser_does_not_execute_python(self):
        self.fixtures()
        self.log_path.write_text(
            f"[{iso(NOW)}] status=critical failing=__import__('os').getcwd()\n")
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("Malformed failing IDs", result["reason"])

    def test_explicit_sources_work_without_default_files_or_recursive_reads(self):
        self.fixtures()
        sources = {}
        for key, old in (
                ("future_plan", self.plan_path), ("future_verdict", self.verdict_path),
                ("future_history", self.log_path)):
            target = self.root / old.name
            old.rename(target)
            sources[key] = str(target)
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("recursive scan")):
            result = build({**self.context, "sources": sources})
        self.assertEqual("ready", result["status"], result["reason"])

    def test_home_is_supplied_only_by_the_public_adapter_context(self):
        self.fixtures()
        without_home = {key: value for key, value in self.context.items() if key != "home"}
        with mock.patch.object(Path, "home", side_effect=AssertionError("implicit host discovery")):
            self.assertEqual("ready", build(self.context)["status"])
            self.assertEqual("blocked", build(without_home)["status"])

    def test_platform_specific_open_flags_are_optional(self):
        self.fixtures()
        portable_os = SimpleNamespace(**{
            name: getattr(os, name) for name in (
                "O_RDONLY", "O_WRONLY", "O_CREAT", "O_EXCL", "open",
                "fdopen", "fstat", "fsync", "replace",
            )
        })
        with mock.patch.object(future, "os", portable_os):
            result = build(self.context)
        self.assertEqual("ready", result["status"], result["reason"])

    def test_synthetic_unicode_content_round_trips_without_host_locale(self):
        self.fixtures()
        self.plan["projects"][0]["title"] = "Résumé review"
        self.plan["projects"][0]["impact"] = "The résumé review would be delayed."
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("ready", result["status"], result["reason"])
        self.assertIn("Résumé review", result["title"])
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("Résumé review", text)

    def test_complete_notification_has_a_bounded_ascii_shape(self):
        self.fixtures()
        result = build(self.context)
        lines = [result["title"]]
        for label, field in (
                ("Changed", "change"), ("Impact", "impact"),
                ("Action", "action"), ("Need", "decision")):
            lines.append(f"{label}: {result[field]}")
        lines.append("Evidence: " + "; ".join(
            f"{row['source']}: {row['observation']}" for row in result["evidence"]))
        lines.append("Deadline: " + result["deadline"])
        message = "\n".join(lines)
        self.assertTrue(message.isascii())
        self.assertLessEqual(len(message), 650)
        self.assertLessEqual(len(message.split()), 90)
        self.assertIn("if the gate stays unready", message)
        self.assertIn(iso(NOW + timedelta(days=7)), message)
        self.assertEqual(1, message.count("?"))

    def test_long_consequence_is_blocked_instead_of_truncating_evidence(self):
        self.fixtures()
        self.plan["projects"][0]["impact"] = "Dependent review cannot proceed. " * 8
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("notification budget", result["reason"])
        self.assertFalse(self.artifacts.exists())

    def test_word_budget_is_enforced_independently_of_character_budget(self):
        self.fixtures()
        self.plan["projects"][0]["impact"] = "QA " * 32
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("90-word", result["reason"])
        self.assertFalse(self.artifacts.exists())

    def test_ascii_escape_expansion_cannot_overflow_the_notification(self):
        self.fixtures()
        self.plan["projects"][0]["title"] = "\u6c49" * 100
        self.write_json(self.plan_path, self.plan)
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertIn("notification budget", result["reason"])
        self.assertFalse(self.artifacts.exists())

    def shared_gate(self):
        configured = os.environ.get("RAPP_FUTURE_TEST_GATE")
        path = Path(configured) if configured else Path(future.__file__).with_name("interrupt.py")
        if not path.is_file():
            self.skipTest("Shared gate not installed; set RAPP_FUTURE_TEST_GATE to its exact file.")
        spec = importlib.util.spec_from_file_location("future_integration_gate", path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        return gate

    def test_actual_shared_gate_accepts_the_complete_ready_proposal(self):
        gate = self.shared_gate()
        self.fixtures()
        proposal = build(self.context)
        admission = gate.evaluate(proposal, [], self.context["now"])
        self.assertTrue(admission["allow"], admission["reason"])
        message = gate.render(proposal)
        self.assertTrue(message.isascii())
        self.assertLessEqual(len(message), 650)
        self.assertLessEqual(len(message.split()), 90)
        for field in ("change", "impact", "action", "decision"):
            self.assertIn(proposal[field], message)
        for row in proposal["evidence"]:
            self.assertIn(row["source"], message)
            self.assertIn(row["observation"], message)
        self.assertIn(proposal["deadline"], message)

    def test_actual_shared_gate_does_not_resend_after_only_clock_advance(self):
        gate = self.shared_gate()
        self.fixtures()
        first = build(self.context)
        history = [{
            "at": self.context["now"], "decision": "queued", "scenario": "future",
            "fingerprint": first["fingerprint"], "proposal": first,
        }]
        later = {**self.context, "now": iso(NOW + timedelta(minutes=10))}
        verdict = self.native_verdict(NOW + timedelta(minutes=9))
        self.write_json(self.verdict_path, verdict)
        second = build(later)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["evidence"], second["evidence"])
        admission = gate.evaluate(second, history, later["now"])
        self.assertFalse(admission["allow"], admission["reason"])

    def test_actual_shared_gate_accepts_a_distant_deadline_as_routine(self):
        gate = self.shared_gate()
        self.fixtures()
        self.plan["projects"][0]["deadline"] = iso(NOW + timedelta(days=7))
        self.plan["projects"][0]["dependencies"][0]["recovery_hours"] = 168
        self.write_json(self.plan_path, self.plan)
        proposal = build(self.context)
        self.assertEqual("routine", proposal["urgency"])
        admission = gate.evaluate(proposal, [], self.context["now"])
        self.assertTrue(admission["allow"], admission["reason"])

    def test_directories_are_not_discovered_as_source_roots(self):
        self.fixtures()
        result = build({**self.context, "sources": {"future_plan": str(self.home)}})
        self.assertEqual("blocked", result["status"])

    def test_sources_and_artifact_directory_must_not_follow_symlinks(self):
        self.fixtures()
        link = self.root / "linked-plan.json"
        try:
            link.symlink_to(self.plan_path)
        except (OSError, NotImplementedError):
            self.skipTest("The current platform does not permit creating symlinks.")
        result = build({**self.context, "sources": {"future_plan": str(link)}})
        self.assertEqual("blocked", result["status"])
        self.artifacts.symlink_to(self.home, target_is_directory=True)
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], list(self.home.glob("future-*.md")))

    def test_unwritable_artifact_destination_is_not_ready(self):
        self.fixtures()
        self.artifacts.write_text("not a directory")
        result = build(self.context)
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], result["artifacts"])

    def test_build_changes_only_artifacts_and_never_invokes_transport(self):
        self.fixtures()
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with mock.patch("subprocess.run", side_effect=AssertionError("subprocess")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("Messages access")):
            first = build(self.context)
            second = build(self.context)
        self.assertEqual("ready", first["status"])
        self.assertEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        self.assertEqual(1, len(list(self.artifacts.iterdir())))


if __name__ == "__main__":
    unittest.main()
