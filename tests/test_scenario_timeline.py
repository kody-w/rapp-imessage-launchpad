"""Synthetic protocol fixtures; no live user evidence or device access."""

import json
import os
import plistlib
import shutil
import stat
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scenarios import timeline


NOW = "2026-09-19T18:30:00Z"
ROLES = ("logs", "verdict", "last_run", "repair_receipts",
         "deployments", "launch_agents", "device_receipts")


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(".timeline-test-data") / uuid.uuid4().hex
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.context = {
            "home": str(self.home.resolve()), "artifact_dir": str((self.root / "artifacts").resolve()),
            "now": NOW, "sources": {role: [] for role in ROLES},
        }
        self.addCleanup(self.clean)

    def clean(self):
        shutil.rmtree(self.root)
        try:
            self.root.parent.rmdir()
        except OSError:
            pass

    def write(self, name, value, role=None):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        if role:
            self.context["sources"][role].append(name)
        return path

    def logs(self, samples, name="logs/sentinel-2026-09.log"):
        lines = []
        for at, failed in samples:
            lines.append(f"[{at}] status={'degraded' if failed else 'healthy'} "
                         f"failing={failed if failed else 'none'}")
        return self.write(name, "\n".join(lines) + "\n", "logs")

    def repeated(self, check="w_neighbor_moving"):
        return self.logs([
            ("2026-09-19T17:00:00Z", []),
            ("2026-09-19T17:15:00Z", [check]),
            ("2026-09-19T17:30:00Z", [check]),
        ])

    def test_release_gate_ignores_resampled_failure_but_admits_new_episode(self):
        from scenarios.interrupt import delivery_key, evaluate

        path = self.repeated()
        first, _ = self.build()
        history = [{
            "at": NOW, "scenario": "timeline", "fingerprint": first["fingerprint"],
            "decision": "queued", "proposal": first,
        }]
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:45:00Z] status=degraded failing=['w_neighbor_moving']\n")
        sampled, _ = self.build()
        policy = {"timezone": "UTC", "quiet_hours": False, "max_daily": 10}
        self.assertEqual(delivery_key(first), delivery_key(sampled))
        self.assertFalse(evaluate(sampled, history, NOW, policy)["allow"])
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:50:00Z] status=healthy failing=none\n")
            handle.write("[2026-09-19T18:00:00Z] status=degraded failing=['w_neighbor_moving']\n")
            handle.write("[2026-09-19T18:15:00Z] status=degraded failing=['w_neighbor_moving']\n")
        changed, _ = self.build()
        self.assertNotEqual(delivery_key(first), delivery_key(changed))
        self.assertTrue(evaluate(changed, history, NOW, policy)["allow"])

    def build(self):
        result = timeline.build(self.context)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["scenario"], "timeline")
        self.assertIn(result["status"], ("ready", "suppressed", "blocked"))
        self.assertIn(result["urgency"], ("routine", "time_sensitive", "urgent"))
        for field in ("title", "change", "impact", "action", "decision", "fingerprint", "reason"):
            self.assertIsInstance(result[field], str)
        for item in result["evidence"]:
            self.assertEqual(set(item), {"source", "observation"})
        for path in result["artifacts"]:
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).is_relative_to(Path(self.context["artifact_dir"])))
        self.assertNotIn("deadline", result)
        if result["status"] == "ready":
            self.assert_notification_budget(result)
        report = json.loads(Path(result["artifacts"][0]).read_text()) if result["artifacts"] else {}
        return result, report

    def assert_notification_budget(self, result):
        text = "\n".join(
            [f"{key}: {result[key]}" for key in
             ("title", "change", "impact", "action", "decision", "reason", "urgency")]
            + [f"{item['source']}: {item['observation']}" for item in result["evidence"]]
        )
        self.assertLessEqual(len(text.encode("ascii")), 600)
        self.assertLessEqual(len(text.split()), 80)
        return text

    def test_latest_recurring_episode_not_alphabetically_first_long_running_failure(self):
        self.logs([
            ("2026-09-19T17:00:00Z", ["rv_world_merging"]),
            ("2026-09-19T17:15:00Z", ["rv_world_merging", "w_neighbor_moving"]),
            ("2026-09-19T17:30:00Z", ["rv_world_merging", "w_neighbor_moving"]),
        ])
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["selected"]["check"], "w_neighbor_moving")
        self.assertEqual(report["selected"]["first_failure"], "2026-09-19T17:15:00Z")
        self.assertEqual(report["root_cause"], "not established")
        self.assertIn("read-only", result["action"])

    def test_sorting_uses_instants_offsets_fractional_seconds_not_file_order(self):
        self.logs([
            ("2026-09-19T14:00:00.100-04:00", ["w_neighbor_moving"]),
            ("2026-09-19T19:45:00+02:00", ["w_neighbor_moving"]),
            ("2026-09-19T17:30:00Z", []),
        ])
        _, report = self.build()
        self.assertEqual(report["selected"]["first_failure"], "2026-09-19T17:45:00Z")
        self.assertEqual(report["selected"]["last_failure"], "2026-09-19T18:00:00.100000Z")
        times = [timeline._instant(event["at"]) for event in report["observations"]]
        self.assertEqual(times, sorted(times))

    def test_z_suffix_is_normalized_before_python39_fromisoformat(self):
        real_datetime = timeline.datetime
        with mock.patch.object(timeline, "datetime", wraps=real_datetime) as parser:
            value = timeline._instant("2026-09-19T17:30:00.125Z")
        parser.fromisoformat.assert_called_once_with("2026-09-19T17:30:00.125+00:00")
        self.assertEqual(timeline._stamp(value), "2026-09-19T17:30:00.125000Z")

    def test_recurrence_across_a_recovery_has_new_earliest_actionable_event(self):
        self.logs([
            ("2026-09-19T17:00:00Z", ["rv_world_merging"]),
            ("2026-09-19T17:15:00Z", []),
            ("2026-09-19T17:30:00Z", ["rv_world_merging"]),
        ])
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["selected"]["episodes"], 2)
        self.assertEqual(report["selected"]["first_failure"], "2026-09-19T17:30:00Z")

    def test_mirrored_chain_and_last_run_do_not_turn_one_tick_into_recurrence(self):
        self.logs([("2026-09-19T17:30:00Z", ["rv_world_merging"])])
        self.write("state/last_run.json", {
            "at": "2026-09-19T17:30:11Z", "failed": ["rv_world_merging"],
        }, "last_run")
        self.write("neighborhood/copilot/chain.jsonl", json.dumps({
            "kind": "sentinel.tick", "utc": "2026-09-19T17:30:11.250Z",
            "payload": {"failed": ["rv_world_merging"], "critical": ["rv_world_merging"]},
        }) + "\n", "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(report["selected"])

    def test_same_instant_duplicate_lines_are_one_observation(self):
        self.logs([
            ("2026-09-19T17:30:00Z", ["rv_world_merging"]),
            ("2026-09-19T13:30:00-04:00", ["rv_world_merging"]),
        ])
        result, _ = self.build()
        self.assertEqual(result["status"], "blocked")

    def test_chain_is_a_fallback_not_a_requirement(self):
        self.write("neighborhood/copilot/chain.jsonl", "\n".join(json.dumps({
            "kind": "sentinel.tick", "utc": at,
            "payload": {"failed": ["rv_world_merging"], "critical": ["rv_world_merging"]},
        }) for at in ("2026-09-19T17:00:00Z", "2026-09-19T17:15:00Z")), "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["urgency"], "routine")
        self.assertTrue(report["selected"]["critical"])
        self.assertEqual(report["selected"]["primary_channel"], "chain")

    def test_single_retained_log_can_fall_back_to_two_actual_chain_ticks(self):
        self.logs([("2026-09-19T17:15:00Z", ["rv_world_merging"])])
        self.write("neighborhood/copilot/chain.jsonl", "\n".join(json.dumps({
            "kind": "sentinel.tick", "utc": at, "payload": {"failed": ["rv_world_merging"]},
        }) for at in ("2026-09-19T17:00:11Z", "2026-09-19T17:15:11Z")), "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["selected"]["primary_channel"], "chain")
        self.assertEqual(report["selected"]["total_samples"], 2)

    def test_later_last_run_without_severity_does_not_erase_known_criticality(self):
        self.repeated("rv_world_merging")
        self.write("state/last_verdict.json", {
            "generated": "2026-09-19T17:30:00Z", "failed": ["rv_world_merging"],
            "critical": ["rv_world_merging"],
        }, "verdict")
        self.write("state/last_run.json", {
            "at": "2026-09-19T17:30:11Z", "failed": ["rv_world_merging"],
        }, "last_run")
        result, report = self.build()
        self.assertEqual(result["urgency"], "routine")
        self.assertTrue(report["selected"]["critical"])

    def test_coincident_recurring_checks_do_not_imply_causal_priority(self):
        self.logs([
            ("2026-09-19T17:00:00Z", ["rv_world_merging", "rv_meaningful_activity"]),
            ("2026-09-19T17:15:00Z", ["rv_meaningful_activity", "rv_world_merging"]),
        ])
        result, report = self.build()
        self.assertEqual(len(report["equally_recent_checks"]), 2)
        self.assertIn("not causal priority", result["reason"])
        self.assertEqual(report["selected"]["check"], "rv_meaningful_activity")

    def test_conflicting_latest_samples_block_instead_of_imposing_order(self):
        path = self.repeated()
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:45:00Z] status=degraded failing=['w_neighbor_moving']\n")
            handle.write("[2026-09-19T17:45:00Z] status=healthy failing=none\n")
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("ambiguous", result["reason"])
        self.assertTrue(any(gap["code"] == "conflicting_samples" for gap in report["evidence_gaps"]))

    def test_naive_future_invalid_timestamps_not_repaired_using_mtime(self):
        self.logs([
            ("2026-09-19T17:00:00", ["rv_world_merging"]),
            ("not-a-date", ["rv_world_merging"]),
            ("2026-09-20T17:00:00Z", ["rv_world_merging"]),
            ("2026-09-19T17:30:00Z", ["rv_world_merging"]),
        ])
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        codes = {gap["code"] for gap in report["evidence_gaps"]}
        self.assertTrue({"invalid_timestamp", "future_timestamp"}.issubset(codes))

    def test_corrupt_jsonl_rows_reported_while_valid_rows_survive(self):
        self.write("state/events.jsonl", "\n".join([
            '{"at":"2026-09-19T17:00:00Z","failed":["rv_world_merging"]}',
            "{truncated",
            '{"at":"2026-09-19T17:15:00Z","failed":["rv_world_merging"]}',
        ]), "logs")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any(gap["code"] == "corrupt_row" for gap in report["evidence_gaps"]))
        self.assertIn("Evidence gaps", result["reason"])

    def test_bad_utf8_line_does_not_destroy_valid_jsonl_history(self):
        self.write("state/events.jsonl", (
            b'{"at":"2026-09-19T17:00:00Z","failed":["rv_world_merging"]}\n'
            b'\xff\xfe\n'
            b'{"at":"2026-09-19T17:15:00Z","failed":["rv_world_merging"]}\n'
        ), "logs")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any(gap["code"] == "corrupt_row" for gap in report["evidence_gaps"]))

    def test_malformed_xml_plist_is_reported_not_raised(self):
        self.repeated()
        self.write("schedules/bad.plist", b"<?xml version='1.0'?><plist><dict>", "launch_agents")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(any(gap["code"] == "unreadable" for gap in report["evidence_gaps"]))

    def test_unknown_repair_receipt_shape_is_reported(self):
        self.write("state/repairs.json", {"unexpected": True}, "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(gap["code"] == "corrupt_row" for gap in report["evidence_gaps"]))

    def test_missing_corrupt_and_undated_sources_are_explicit(self):
        self.context["sources"]["logs"] = ["logs/missing.log", "logs/launchd.err.log"]
        self.write("logs/launchd.err.log", "run.sh: logs/run.log: No such file or directory\n")
        self.write("state/bad.json", "{broken", "verdict")
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        codes = {gap["code"] for gap in report["evidence_gaps"]}
        self.assertTrue({"missing", "unreadable", "undated_log"}.issubset(codes))

    def test_missing_home_is_not_created_or_claimed_healthy(self):
        self.context["home"] = str((self.root / "absent").resolve())
        self.context["sources"] = {"launch_agents": []}
        result, _ = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(Path(self.context["home"]).exists())

    def test_healthy_samples_suppress_without_claiming_complete_health(self):
        self.logs([("2026-09-19T17:00:00Z", []), ("2026-09-19T17:15:00Z", [])])
        result, _ = self.build()
        self.assertEqual(result["status"], "suppressed")
        self.assertIn("does not prove", result["reason"])

    def test_no_longer_listed_is_not_permanent_recovery(self):
        path = self.repeated()
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:45:00Z] status=healthy failing=none\n")
        result, report = self.build()
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(report["selected"]["clearance"], "not_listed_as_failing")
        self.assertIn("not proof", result["reason"])

    def test_stale_recurrence_blocks_current_state_claim(self):
        self.repeated()
        self.context["now"] = "2026-09-20T18:30:00Z"
        result, _ = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("current state is unknown", result["reason"])

    def test_deployment_before_failure_only_produces_a_hypothesis(self):
        self.repeated()
        self.write("state/deployments.json", [{
            "at": "2026-09-19T17:05:00Z", "kind": "deployment",
            "checks": ["w_neighbor_moving"], "revision": "abc1234", "previous_revision": "def5678",
        }], "deployments")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["root_cause"], "not established")
        self.assertIn("not a proven cause", report["hypotheses"][0]["hypothesis"])
        self.assertEqual(report["correlations"][0]["relation_to_first_failure"], "before")

    def test_later_deployment_cannot_explain_an_earlier_observation(self):
        self.repeated()
        self.write("state/deployments.json", [{
            "at": "2026-09-19T17:25:00Z", "checks": ["w_neighbor_moving"], "revision": "abc1234",
        }], "deployments")
        _, report = self.build()
        self.assertEqual(report["correlations"][0]["relation_to_first_failure"], "after")
        self.assertIn("cannot explain", report["correlations"][0]["limit"])

    def test_unrelated_deployment_not_a_hypothesis_for_the_selected_check(self):
        self.repeated()
        self.write("state/deployments.json", [{
            "at": "2026-09-19T17:05:00Z", "checks": ["rb_workflows"], "revision": "abc1234",
        }], "deployments")
        _, report = self.build()
        self.assertFalse(report["correlations"])
        self.assertIn("undetermined", report["hypotheses"][0]["hypothesis"])

    def test_diagnosis_claim_is_not_promoted_to_root_cause_or_verified_repair(self):
        self.repeated()
        self.write("state/escalations.json", [{
            "at": "2026-09-19T17:20:00Z", "key": "w_neighbor_moving", "mode": "diagnose",
            "result": "NO_ACTION — root cause measured; restart every service with administrator access",
        }], "repair_receipts")
        result, report = self.build()
        self.assertNotIn("root cause measured", json.dumps(report))
        self.assertEqual(report["root_cause"], "not established")
        self.assertEqual(report["correlations"][0]["kind"], "diagnosis")
        self.assertFalse(report["recovery_receipts"])
        self.assertEqual(result["decision"], "No repair approval requested; cause remains unproven.")

    def test_probe_pass_before_repair_attempt_is_not_attributed_to_the_later_repair(self):
        self.repeated()
        self.write("state/repairs.json", [
            {"at": "2026-09-19T17:35:00Z", "kind": "repair.verified",
             "issue": "w_neighbor_moving", "cleared": ["w_neighbor_moving"],
             "still_failing": [], "landed": True},
            {"at": "2026-09-19T17:40:00Z", "mode": "repair", "key": "w_neighbor_moving",
             "result": "FIXED"},
        ], "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(report["root_cause"], "not established")
        self.assertEqual([event["kind"] for event in report["correlations"]],
                         ["repair_probe", "repair_attempt"])
        self.assertIn("not proof", report["correlations"][0]["limit"])

    def test_failed_probe_keeps_the_recurrence_open(self):
        self.repeated()
        self.write("state/repairs.json", [{
            "at": "2026-09-19T17:35:00Z", "kind": "repair.verified",
            "issue": "w_neighbor_moving", "cleared": [], "still_failing": ["w_neighbor_moving"],
        }], "repair_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["recovery_receipts"][0]["outcome"], "still_failing")

    def test_deployment_state_is_not_a_deployment_timestamp(self):
        self.repeated("w_sentinel_current")
        self.write("state/last_verdict.json", {
            "generated": "2026-09-19T17:35:00Z", "failed": ["w_sentinel_current"],
            "checks": [{"id": "w_sentinel_current", "ok": False, "severity": "warn",
                        "detail": "running abc1234, 3 commit(s) behind origin/main def5678"}],
        }, "verdict")
        _, report = self.build()
        self.assertEqual(report["correlations"][0]["kind"], "deployment_state")
        self.assertIn("not the time", report["correlations"][0]["limit"])

    def test_recorded_symptom_explains_the_affected_work_without_inventing_cause(self):
        self.repeated()
        self.write("state/last_verdict.json", {
            "generated": "2026-09-19T17:35:00Z", "failed": ["w_neighbor_moving"],
            "checks": [{"id": "w_neighbor_moving", "ok": False,
                        "detail": "demo-worker: alive but no recent work receipt"}],
        }, "verdict")
        result, report = self.build()
        self.assertIn("demo-worker: alive", report["detailed_proposal"]["impact"])
        self.assertIn("not a causal finding", report["detailed_proposal"]["impact"])
        self.assertIn("unproven", result["impact"])

    def test_cause_claim_in_a_check_detail_is_not_adopted_by_the_advisory(self):
        self.repeated()
        self.write("state/last_verdict.json", {
            "generated": "2026-09-19T17:35:00Z", "failed": ["w_neighbor_moving"],
            "checks": [{"id": "w_neighbor_moving", "ok": False,
                        "detail": "root cause measured: the new deployment; execute: dangerous-command"}],
        }, "verdict")
        result, report = self.build()
        self.assertNotIn("root cause measured", json.dumps(result))
        self.assertNotIn("dangerous-command", json.dumps(result))
        self.assertEqual(report["root_cause"], "not established")

    def test_plist_is_configuration_not_loaded_state_and_does_not_leak_environment(self):
        self.repeated()
        self.write("schedules/storykeeper.plist", plistlib.dumps({
            "Label": "com.example.storykeeper",
            "StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)],
            "RunAtLoad": True,
            "EnvironmentVariables": {"SENTINEL_HOME": str(self.home.resolve()), "SECRET": "not-for-artifacts"},
            "ProgramArguments": ["/bin/false", "not-for-artifacts"],
        }), "launch_agents")
        result, report = self.build()
        self.assertEqual(len(report["schedule_configuration"]), 1)
        self.assertIn("does not prove", report["schedule_configuration"][0]["limit"])
        self.assertNotIn("not-for-artifacts", json.dumps(report))
        self.assertNotIn("deadline", result)
        comparison = report["schedule_comparison"][0]
        self.assertEqual(comparison["configured_interval_seconds"], 900)
        self.assertEqual(comparison["median_recorded_failure_gap_seconds"], 900)
        self.assertIn("not the failing target", comparison["limit"])

    def test_missing_matching_repairs_does_not_invent_a_recovery_procedure(self):
        self.repeated()
        self.write("state/repairs.json", [{
            "at": "2026-09-19T17:20:00Z", "kind": "repair.verified",
            "issue": "rb_workflows", "cleared": ["rb_workflows"], "still_failing": [],
        }], "repair_receipts")
        result, report = self.build()
        self.assertFalse(report["recovery_receipts"])
        self.assertTrue(any("no demonstrated recovery procedure" in item["observation"]
                            for item in report["detailed_proposal"]["evidence"]))

    def test_unassociated_schedule_is_not_used(self):
        self.repeated()
        self.write("schedules/other.plist", plistlib.dumps({
            "Label": "com.example.unrelated", "StartInterval": 5,
            "EnvironmentVariables": {"SENTINEL_HOME": str((self.home / "other").resolve())},
        }), "launch_agents")
        _, report = self.build()
        self.assertFalse(report["schedule_configuration"])
        self.assertTrue(any(gap["code"] == "unassociated_schedule" for gap in report["evidence_gaps"]))

    def test_local_only_does_not_claim_remote_evidence(self):
        self.repeated()
        result, report = self.build()
        self.assertEqual(report["other_device_receipts"], [])
        self.assertTrue(any("Local evidence only" in item["observation"] for item in result["evidence"]))

    def test_other_devices_only_come_from_explicit_existing_receipts(self):
        self.write("state/receipts/lab-mini.json", [
            {"at": "2026-09-19T17:15:00Z", "device": "lab-mini", "failed": ["queue_stalled"]},
            {"at": "2026-09-19T17:30:00Z", "device": "lab-mini", "failed": ["queue_stalled"]},
        ], "device_receipts")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(report["other_device_receipts"], ["lab-mini"])
        self.assertTrue(any("Receipt clocks/claims unverified" in item["observation"]
                            for item in result["evidence"]))

    def test_path_escape_and_symlinks_outside_home_are_refused(self):
        outside = self.root / "outside.json"
        outside.write_text('{"failed":[],"at":"2026-09-19T17:00:00Z"}')
        (self.home / "link.json").symlink_to(outside.resolve())
        self.context["sources"]["verdict"] = ["../outside.json", "link.json"]
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual({gap["code"] for gap in report["evidence_gaps"]}, {"outside_scope"})
        self.assertFalse(report["sources"])

    def test_symlink_loop_is_reported_instead_of_crashing(self):
        (self.home / "loop.json").symlink_to("loop.json")
        self.context["sources"]["verdict"] = ["loop.json"]
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(gap["code"] == "unreadable" for gap in report["evidence_gaps"]))

    def test_bounded_log_tail_retains_complete_lines_and_reports_truncation(self):
        self.write("logs/large.log", "x" * 600 + "\n" + "\n".join([
            "[2026-09-19T17:15:00Z] status=degraded failing=['w_neighbor_moving']",
            "[2026-09-19T17:30:00Z] status=degraded failing=['w_neighbor_moving']",
        ]), "logs")
        with mock.patch.object(timeline, "MAX_BYTES", 300):
            result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertIn("actual onset is unknown", result["reason"])
        self.assertTrue(any(gap["code"] == "truncated" for gap in report["evidence_gaps"]))

    def test_fingerprint_stable_when_now_ages_sample_counts_and_artifact_paths_change(self):
        path = self.repeated()
        result, _ = self.build()
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:45:00Z] status=degraded failing=['w_neighbor_moving']\n")
        self.context["now"] = "2026-09-19T19:00:00+00:00"
        self.context["artifact_dir"] = str((self.root / "second-artifacts").resolve())
        other, _ = self.build()
        self.assertNotEqual(result["evidence"], other["evidence"])
        self.assertEqual(result["fingerprint"], other["fingerprint"])

    def test_ready_notification_is_concise_and_retains_full_evidence_in_artifacts(self):
        self.repeated()
        self.context["sources"]["deployments"] = ["state/missing-deployments.json"]
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assert_notification_budget(result)
        self.assertGreater(len(report["detailed_proposal"]["evidence"]), len(result["evidence"]))
        for item in result["evidence"]:
            self.assertIn(item["source"], report["notification_sources"])
            self.assertTrue(Path(report["notification_sources"][item["source"]]).is_file())
        self.assertIn("Evidence gaps: 1", result["reason"])
        self.assertEqual(report["envelope"], result)

    def test_human_decision_has_a_prepared_recovery_plan_without_changing_incident_identity(self):
        self.repeated()
        result, report = self.build()
        self.assertEqual(result["decision"], "No repair approval requested; cause remains unproven.")
        self.assertNotIn("review_local_timeline", json.dumps(result))
        self.assertIn("I prepared the timeline and recovery plan", result["action"])
        self.assertIn("next saved check read-only; no restart", result["action"])
        plan = report["recovery_plan"]
        self.assertFalse(plan["repair_approval_requested"])
        self.assertEqual(plan["earliest_actionable_event"]["at"], report["selected"]["first_failure"])
        self.assertEqual(plan["earliest_actionable_event"]["source"], report["selected"]["first_source"])
        self.assertEqual(plan["prior_not_failing_at"], "2026-09-19T17:00:00Z")
        self.assertIn("Keep services unchanged", plan["safest_recovery_option"])
        self.assertIn("same authorized sources", plan["agent_next_step"])
        self.assertIn(result["decision"], Path(result["artifacts"][1]).read_text())
        self.assertEqual(result["fingerprint"], timeline._digest({
            "version": 1, "status": "ready", "decision": "review_local_timeline",
            "check": "w_neighbor_moving", "device": "local",
            "episode_after": "2026-09-19T17:00:00Z", "urgency": "routine",
            "ties": [{"device": "local", "check": "w_neighbor_moving"}],
            "repair_outcomes": [], "revisions": [], "cause": "not_established", "other_devices": [],
        }))

    def test_long_identifiers_ties_and_gaps_still_fit_the_ready_notification_budget(self):
        check = "demo_check_" + "x" * 140
        other = "demo_check_" + "y" * 140
        device = "demo_device_" + "z" * 140
        source = "state/receipts/" + "demo-source-" + "q" * 140 + ".json"
        self.write(source, [
            {"at": "2026-09-19T17:00:00.123456Z", "device": device, "failed": [check, other]},
            {"at": "2026-09-19T17:15:00.123456Z", "device": device, "failed": [check, other]},
        ], "device_receipts")
        self.context["sources"]["deployments"] = ["state/missing-deployments.json"]
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assert_notification_budget(result)
        self.assertIn(check, report["detailed_proposal"]["change"])
        self.assertIn(device, report["detailed_proposal"]["change"])
        self.assertIn("actual onset is unknown", result["reason"])
        self.assertIn("not causal priority", result["reason"])

    def test_fingerprint_ignores_ages_in_check_details(self):
        self.repeated()
        path = self.write("state/last_verdict.json", {
            "generated": "2026-09-19T17:35:00Z", "failed": ["w_neighbor_moving"],
            "checks": [{"id": "w_neighbor_moving", "ok": False, "detail": "last WORKED 481m ago"}],
        }, "verdict")
        result, _ = self.build()
        row = json.loads(path.read_text())
        row["generated"] = "2026-09-19T17:50:00Z"
        row["checks"][0]["detail"] = "last WORKED 496m ago"
        path.write_text(json.dumps(row))
        other, _ = self.build()
        self.assertEqual(result["fingerprint"], other["fingerprint"])

    def test_fingerprint_changes_for_a_new_episode_after_observed_clearance(self):
        path = self.repeated()
        result, _ = self.build()
        with path.open("a") as handle:
            handle.write("[2026-09-19T17:45:00Z] status=healthy failing=none\n")
            handle.write("[2026-09-19T18:00:00Z] status=degraded failing=['w_neighbor_moving']\n")
        other, _ = self.build()
        self.assertEqual(other["status"], "ready")
        self.assertNotEqual(result["fingerprint"], other["fingerprint"])

    def test_build_has_no_process_network_or_source_write_side_effects(self):
        self.repeated()
        before = {str(path): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        with mock.patch("subprocess.run", side_effect=AssertionError("no processes")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("no network")):
            result, _ = self.build()
        self.assertEqual(result["status"], "ready")
        after = {str(path): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_namespaced_source_config(self):
        self.repeated()
        self.context["sources"] = {"timeline": self.context["sources"], "unrelated": {"unused": True}}
        result, _ = self.build()
        self.assertEqual(result["status"], "ready")

    def test_default_evidence_paths_follow_only_the_explicit_context_home(self):
        self.home = self.root / "portable profile" / ".storykeeper" / "home"
        self.home.mkdir(parents=True)
        self.context["home"] = str(self.home.resolve())
        self.repeated()
        self.context["sources"] = {"timeline": {"launch_agents": []}}
        with mock.patch.object(timeline.Path, "home", side_effect=AssertionError("no implicit profile")):
            result, report = self.build()
        self.assertEqual(result["status"], "ready")
        for item in report["sources"] + report["evidence_gaps"]:
            self.assertTrue(Path(item["source"]).is_relative_to(self.home.resolve()))

    def test_explicit_sources_replace_not_augment_default_evidence_paths(self):
        self.write("logs/sentinel-2026-09.log", "default source must not be read\n")
        selected = self.logs([
            ("2026-09-19T17:00:00Z", ["demo_queue_stalled"]),
            ("2026-09-19T17:15:00Z", ["demo_queue_stalled"]),
        ], name="imported-receipts/demo.log")
        result, report = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual([row["source"] for row in report["sources"]], [str(selected.resolve())])
        self.assertFalse(report["evidence_gaps"])

    def test_missing_context_home_does_not_fall_back_to_a_live_profile(self):
        self.context.pop("home")
        with mock.patch.object(timeline.Path, "home", side_effect=AssertionError("no implicit profile")):
            result = timeline.build(self.context)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["artifacts"])

    @unittest.skipUnless(os.name == "posix", "POSIX owner permissions")
    def test_runtime_artifacts_stay_owner_only_including_preexisting_files(self):
        self.repeated()
        result, _ = self.build()
        folder = Path(self.context["artifact_dir"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode) & 0o077, 0)
        for artifact in result["artifacts"]:
            Path(artifact).chmod(0o644)
        result, _ = self.build()
        for artifact in result["artifacts"]:
            self.assertEqual(stat.S_IMODE(Path(artifact).stat().st_mode), 0o600)

    def test_owned_public_files_do_not_embed_profile_paths_ips_or_phone_numbers(self):
        root = Path(__file__).resolve().parents[1]
        patterns = (
            r"/(?:Users|home)/[A-Za-z0-9_.-]+/",
            r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+[\\/]",
            r"(?<![A-Za-z0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])",
            r"(?<![0-9])\+[1-9][0-9]{10,14}\b",
        )
        for relative in ("scenarios/timeline.py", "tests/test_scenario_timeline.py",
                         "docs/scenarios/timeline.md"):
            text = (root / relative).read_text(encoding="utf-8")
            for pattern in patterns:
                with self.subTest(file=relative, pattern=pattern):
                    self.assertNotRegex(text, pattern)

    def test_invalid_context_is_json_safe_and_does_not_raise(self):
        for value in (None, {}, {"now": "bad"}, {**self.context, "sources": []},
                      {**self.context, "now": "2026-09-19T18:30:00"}):
            with self.subTest(value=value):
                result = timeline.build(value)
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["artifacts"])
                json.dumps(result, allow_nan=False)

    def test_unwritable_artifact_target_blocks_readiness(self):
        self.repeated()
        target = self.root / "not-a-directory"
        target.write_text("keep")
        self.context["artifact_dir"] = str(target.resolve())
        result, _ = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["artifacts"])
        self.assertEqual(target.read_text(), "keep")

    def test_inconsistent_boolean_results_are_not_treated_as_healthy(self):
        self.write("state/verdict.json", {
            "generated": "2026-09-19T17:00:00Z", "failed": [],
            "checks": [{"id": "rv_world_merging", "ok": "false"}],
        }, "verdict")
        result, report = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(report["evidence_gaps"])


if __name__ == "__main__":
    unittest.main()
