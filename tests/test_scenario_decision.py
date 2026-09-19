"""Sanitized, offline decision tests; scratch lives beside these tests."""

import hashlib
import json
import os
import shutil
import stat
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scenarios import decision


NOW = "2030-04-12T12:00:00+00:00"
ISSUE = "rv_meaningful_activity,rv_world_merging"
ATTEMPT_TIMES = ("2030-04-10T08:00:00+00:00", "2030-04-10T12:00:00+00:00")


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("decision-fixture-" + uuid.uuid4().hex)
        self.home = self.root / "home"
        self.artifacts = self.root / "artifacts"
        (self.home / "state").mkdir(parents=True)
        (self.home / "logs").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.artifacts),
            "now": NOW, "sources": {},
        }
        self.verdict = {
            "generated": NOW, "status": "critical",
            "checks": [
                {"id": "rv_world_merging", "ok": False, "severity": "critical",
                 "detail": "last merge 101.0h ago"},
                {"id": "rv_meaningful_activity", "ok": False, "severity": "critical",
                 "detail": "chat stale 201.0h; agent state stale 101.0h"},
                {"id": "alert_delivery", "ok": False, "severity": "warn",
                 "detail": "3 UNKNOWN delivery evidence record(s)"},
                {"id": "w_openrappter_spin", "ok": False, "severity": "warn",
                 "detail": "spinning launchd job(s): org.example.gateway (runs=40, last exit 1)"},
            ],
        }
        self.history = [
            {"at": at, "key": ISSUE, "mode": "diagnose",
             "result": "UNKNOWN (no SENTINEL_RESULT line)"} for at in ATTEMPT_TIMES
        ]
        self.put("state/last_verdict.json", self.verdict)
        self.put("state/escalations.json", self.history)
        self.put("state/issues.json", {
            ISSUE: {"attempts": 2, "last_attempt": ATTEMPT_TIMES[-1],
                    "last_result": "UNKNOWN (no SENTINEL_RESULT line)"},
        })
        self.put("config.json", {
            "instance_name": "Example Observer", "level": 1,
            "watchers": {"openrappter": {"enabled": False}},
            "notify_handle": "PRIVATE-RECIPIENT-NOT-AN-INPUT",
            "token": "PRIVATE-TOKEN-NOT-AN-INPUT",
        })
        self.put("direction.json", {"situation": "Observe world freshness.",
                                    "cares_about": ["rappterverse"]})
        self.sent = {
            "at": NOW, "sent_at": NOW, "entry_id": "example-entry",
            "text": (f"Example Observer needs you.\n'{ISSUE}' survived 2 automated repairs.\n"
                     "rv_world_merging: last merge 101.0h ago; "
                     "rv_meaningful_activity: chat stale 201.0h"),
            "unverified": "delivery evidence unavailable",
        }
        self.jsonl("state/outbox-sent.jsonl", [self.sent, self.sent])
        self.jsonl("state/outbox-unknown.jsonl", [
            {"entry_id": "example-unknown", "reason": "send started before terminal outcome"},
        ])
        for at in ATTEMPT_TIMES:
            self.log(at, "copilot timed out after 900s")

    def put(self, relative, value):
        path = self.home / relative
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def jsonl(self, relative, rows):
        path = self.home / relative
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def log(self, at, text):
        stamp = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc)
        path = self.home / "logs" / ("escalation-" + stamp.strftime("%Y%m%d-%H%M%S") + ".log")
        path.write_text(text, encoding="utf-8")
        return path

    def intent(self, state, **extra):
        path = self.put("intent.json", {
            "target": "rappterverse", "state": state,
            "reason": "The owner explicitly chose this lifecycle.", **extra,
        })
        self.context["sources"]["intent"] = str(path)

    def runtime(self, **extra):
        value = {
            "subject": "openrappter", "observed_at": NOW,
            "job": {"label": "org.example.gateway", "state": "spawn scheduled",
                    "runs": 40, "last_exit": 1},
            "error_kind": "runtime_lock_owned",
            "lock_owner_alive": True, "listener_alive": True,
            "lock_owner_pid": 1234, "listener_pid": 1234,
            **extra,
        }
        path = self.put("runtime.json", value)
        self.context["sources"]["runtime"] = str(path)

    def build(self):
        result = decision.build(self.context)
        json.dumps(result, allow_nan=False)
        self.assertEqual("decision", result["scenario"])
        self.assertIn(result["status"], ("ready", "blocked", "suppressed"))
        self.assertIn(result["urgency"], ("routine", "time_sensitive", "urgent"))
        return result

    def receipt(self, result):
        self.assertEqual(1, len(result["artifacts"]))
        return json.loads(Path(result["artifacts"][0]).read_text())

    def details(self, result):
        return self.receipt(result)["details"]

    def notification_text(self, result):
        labels = (
            ("title", "Title"), ("change", "What changed"),
            ("impact", "Why it matters"), ("action", "What to do"),
            ("decision", "Decision needed"), ("reason", "Reason"),
            ("urgency", "Urgency"),
        )
        lines = [label + ": " + result[key] for key, label in labels]
        lines += ["Evidence [" + row["source"] + "]: " + row["observation"]
                  for row in result["evidence"]]
        if result.get("deadline"):
            lines.append("Deadline: " + result["deadline"])
        return "\n".join(lines)

    def test_exact_timeout_receipts_correct_the_repair_claim(self):
        result = self.build()
        self.assertEqual("ready", result["status"])
        self.assertEqual("Pause only this escalation while confirming its target?", result["decision"])
        self.assertTrue(result["reason"].startswith("mismeasured:"))
        self.assertIn("diagnose runs, not verified repairs", result["change"])
        receipt = self.receipt(result)
        self.assertEqual("pause", receipt["recommendation"])
        self.assertEqual(["timeout", "timeout"], [a["outcome"] for a in receipt["attempts"]])
        self.assertEqual(["copilot timed out after 900s"] * 2,
                         [a["receipt"] for a in receipt["attempts"]])
        self.assertTrue(any("not that no work ran" in c for c in receipt["caveats"]))
        self.assertNotIn("no attempt ever ran", json.dumps(result))

    def test_config_secrets_are_not_copied(self):
        result = self.build()
        data = Path(result["artifacts"][0]).read_text() + json.dumps(result)
        self.assertNotIn("PRIVATE-TOKEN-NOT-AN-INPUT", data)
        self.assertNotIn("PRIVATE-RECIPIENT-NOT-AN-INPUT", data)

    def test_exact_issue_not_unrelated_timeout_or_skipped_spend(self):
        other_at = "2030-04-11T11:00:00+00:00"
        skipped = {"at": other_at, "key": ISSUE, "skipped": True, "result": "SKIPPED"}
        other = {"at": other_at, "key": "rv_meaningful_activity", "mode": "diagnose",
                 "result": "NO_ACTION — earlier opinion"}
        self.put("state/escalations.json", self.history + [skipped, other])
        self.log(other_at, "SENTINEL_RESULT: NO_ACTION — earlier opinion\n")
        receipt = self.receipt(self.build())
        self.assertEqual(2, sum(row["relation"] == "exact" for row in receipt["attempts"]))
        related = [row for row in receipt["attempts"] if row["relation"] == "related"]
        self.assertEqual(1, len(related))
        self.assertEqual("no_action", related[0]["outcome"])

    def test_missing_exact_log_blocks_instead_of_repeating_claim(self):
        self.log(ATTEMPT_TIMES[0], "").unlink()
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertIn("complete exact receipts", result["reason"])
        self.assertNotIn("diagnose runs, not verified repairs", result["change"])

    def test_diagnose_counter_without_actual_logs_is_not_repair_evidence(self):
        for at in ATTEMPT_TIMES:
            self.log(at, "").unlink()
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("inconclusive", self.receipt(result)["classification"])

    def test_missing_or_corrupt_documents_are_not_healthy(self):
        for name in ("last_verdict", "escalations", "issues"):
            for value in (None, "{broken", "[]"):
                with self.subTest(source=name, value=value):
                    path = self.home / "state" / (name + ".json")
                    original = path.read_bytes()
                    if value is None:
                        path.unlink()
                    else:
                        path.write_text(value)
                    result = self.build()
                    self.assertEqual("blocked", result["status"])
                    path.write_bytes(original)

    def test_all_sources_missing_are_blocked_with_an_artifact(self):
        shutil.rmtree(self.home)
        self.home.mkdir()
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertTrue(self.receipt(result)["source_reads"])

    def test_current_attempt_record_must_match_latest_receipt(self):
        self.put("state/issues.json", {
            ISSUE: {"attempts": 2, "last_attempt": "2030-04-10T13:00:00+00:00"},
        })
        self.assertEqual("blocked", self.build()["status"])

    def test_one_log_cannot_be_counted_as_two_attempts(self):
        self.put("state/escalations.json", [self.history[-1], self.history[-1]])
        self.assertEqual("blocked", self.build()["status"])

    def test_empty_receipt_is_incomplete_evidence(self):
        self.log(ATTEMPT_TIMES[-1], "")
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("empty", self.receipt(result)["attempts"][-1]["receipt_status"])

    def test_alert_count_must_agree_with_current_issue_counter(self):
        self.put("state/issues.json", {
            ISSUE: {"attempts": 1, "last_attempt": ATTEMPT_TIMES[-1]},
        })
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertEqual(1, self.receipt(result)["issue_record"]["attempts"])
        self.assertEqual(2, self.receipt(result)["issue_record"]["claimed_repairs"])

    def test_old_episode_does_not_inflate_current_attempt_count(self):
        old = {"at": "2030-03-01T00:00:00+00:00", "key": ISSUE,
               "mode": "repair", "result": "FIXED"}
        self.put("state/escalations.json", [old] + self.history)
        result = self.build()
        self.assertEqual("ready", result["status"])
        self.assertEqual(2, len(self.receipt(result)["attempts"]))

    def test_timeout_from_repair_mode_does_not_prove_no_side_effects(self):
        for row in self.history:
            row["mode"] = "repair"
        self.put("state/escalations.json", self.history)
        result = self.build()
        self.assertTrue(result["reason"].startswith("inconclusive:"))
        self.assertNotIn("not repairs", result["change"])
        self.assertIn("not proof", self.details(result)["change"])

    def test_terminal_fixed_is_only_a_claim_not_verification(self):
        for row in self.history:
            row["mode"], row["result"] = "repair", "FIXED — claimed fix"
            self.log(row["at"], "SENTINEL_RESULT: FIXED — claimed fix\n")
        self.put("state/escalations.json", self.history)
        result = self.build()
        self.assertEqual("ready", result["status"])
        self.assertTrue(result["reason"].startswith("inconclusive:"))
        self.assertIn("not proof of verified repairs", self.details(result)["change"])
        self.assertEqual("fixed", self.receipt(result)["attempts"][0]["outcome"])

    def test_active_intent_and_measured_stall_recommend_repair(self):
        self.intent("active")
        result = self.build()
        self.assertIn("Approve scoped repair triage", result["decision"])
        self.assertEqual("repair", self.receipt(result)["recommendation"])
        self.assertTrue(result["reason"].startswith("broken:"))
        self.assertIn("never reset timestamps", self.receipt(result)["prepared_plan"]["next_step"])
        self.assertIn("not verified repairs", self.details(result)["change"])

    def test_unreadable_checks_do_not_prove_broken_even_when_active(self):
        self.intent("active")
        for row in self.verdict["checks"][:2]:
            row["detail"] = "cannot read public state"
        self.put("state/last_verdict.json", self.verdict)
        self.jsonl("state/outbox-sent.jsonl", [])
        result = self.build()
        self.assertNotEqual("repair", self.receipt(result)["recommendation"])
        self.assertTrue(result["reason"].startswith("inconclusive:"))

    def test_explicit_pause_is_asleep_not_broken(self):
        self.intent("paused", resume_at="2030-04-12T18:00:00+00:00")
        result = self.build()
        self.assertTrue(result["reason"].startswith("asleep:"))
        self.assertIn("Keep only this escalation paused", result["decision"])
        self.assertEqual("pause", self.receipt(result)["recommendation"])
        self.assertEqual("time_sensitive", result["urgency"])
        self.assertEqual("2030-04-12T18:00:00+00:00", result["deadline"])

    def test_explicit_retirement_recommends_only_scoped_retirement(self):
        self.intent("retired")
        result = self.build()
        self.assertIn("Retire only this escalation", result["decision"])
        self.assertEqual("retire", self.receipt(result)["recommendation"])
        self.assertTrue(result["reason"].startswith("retired:"))
        self.assertIn("after approval", self.receipt(result)["prepared_plan"]["next_step"])

    def test_disabled_port_probe_never_means_retired_or_asleep(self):
        self.jsonl("state/outbox-sent.jsonl", [])
        result = self.build()
        self.assertTrue(result["reason"].startswith("inconclusive:"))
        self.assertIn("disabled probe is not retirement", json.dumps(self.details(result)))

    def test_wrong_target_or_reasonless_intent_is_not_authority(self):
        for extra in ({"target": "another-world"}, {"reason": ""}):
            with self.subTest(extra=extra):
                self.intent("retired", **extra)
                self.assertNotEqual("retire", self.receipt(self.build())["recommendation"])

    def test_exclusive_declared_scope_mismatch_is_visible(self):
        self.jsonl("state/outbox-sent.jsonl", [])
        self.put("direction.json", {
            "situation": "Example Observer watches one organism: the document pipeline.",
            "cares_about": ["the pipeline", "its output"],
        })
        result = self.build()
        self.assertTrue(result["reason"].startswith("mismeasured:"))
        self.assertIn("declared scope and alert target differ", self.details(result)["reason"])

    def test_scope_omission_without_exclusivity_is_not_retirement(self):
        self.jsonl("state/outbox-sent.jsonl", [])
        self.put("direction.json", {
            "situation": "Observe several useful tools.", "cares_about": ["their output"],
        })
        self.assertTrue(self.build()["reason"].startswith("inconclusive:"))

    def test_duplicate_launcher_does_not_mean_dead_serving_gateway(self):
        self.runtime()
        result = self.build()
        self.assertIn("duplicate launcher is not a dead serving gateway", self.details(result)["reason"])
        self.assertIn("Live services untouched", result["action"])
        self.assertIn("failures are unproven", result["impact"])
        self.assertIn("neither useful world activity", json.dumps(self.details(result)))

    def test_mismatched_lock_and_listener_do_not_establish_duplicate(self):
        self.runtime(listener_pid=5678)
        receipt = self.receipt(self.build())
        self.assertEqual("crash_loop", receipt["semantic_identity"]["runtime_kind"])

    def test_invalid_pid_does_not_establish_a_live_lock_owner(self):
        self.runtime(lock_owner_pid=0, listener_pid=0)
        receipt = self.receipt(self.build())
        self.assertEqual("crash_loop", receipt["semantic_identity"]["runtime_kind"])

    def test_stale_runtime_is_historical_not_a_current_root_cause(self):
        self.runtime(observed_at="2020-01-01T00:00:00+00:00")
        result = self.build()
        self.assertNotIn("duplicate launcher is not a dead serving gateway", self.details(result)["reason"])
        self.assertIn("Historical/unverified-age", json.dumps(self.details(result)))

    def test_unknown_delivery_is_not_non_delivery_or_stale_public_chat(self):
        result = self.build()
        rendered = json.dumps(self.details(result))
        self.assertIn("Neither proves delivery or non-delivery", rendered)
        self.assertIn("unrelated to the macOS Messages database", rendered)
        self.assertNotIn("grant Full Disk Access", json.dumps(result))

    def test_all_ready_classifications_fit_gate_without_dropping_evidence(self):
        for lifecycle in (None, "active", "paused", "retired"):
            with self.subTest(lifecycle=lifecycle):
                self.context["sources"].pop("intent", None)
                if lifecycle:
                    self.intent(lifecycle, resume_at="2030-04-12T18:00:00.123456Z")
                result = self.build()
                self.assertEqual("ready", result["status"])
                self.assertTrue(result["decision"].endswith("?"))
                self.assertGreaterEqual(len(result["decision"].split()), 8)
                self.assertIn("prepared", result["action"].lower())
                self.assertNotRegex(result["action"], r"^(Inspect|Confirm|Read|Retire|Keep)\b")
                text = self.notification_text(result)
                self.assertTrue(text.isascii())
                self.assertLessEqual(len(text), 650, text)
                self.assertLessEqual(len(text.split()), 90, text)
                for row in result["evidence"]:
                    self.assertIn(row["source"], text)
                    self.assertIn(row["observation"], text)
                self.assertNotIn(str(self.home), text)

    def test_inconclusive_and_scope_only_briefs_fit_gate_limits(self):
        self.jsonl("state/outbox-sent.jsonl", [])
        cases = (
            {"situation": "Observe world freshness.", "cares_about": ["rappterverse"]},
            {"situation": "Example Observer watches one organism: a document pipeline."},
        )
        for direction in cases:
            with self.subTest(direction=direction):
                self.put("direction.json", direction)
                result = self.build()
                self.assertEqual("ready", result["status"])
                self.assertTrue(result["decision"].endswith("?"))
                self.assertIn("prepared", result["action"].lower())
                text = self.notification_text(result)
                self.assertTrue(text.isascii())
                self.assertLessEqual(len(text), 650, text)
                self.assertLessEqual(len(text.split()), 90, text)

    def test_brief_preserves_full_attributed_evidence_in_private_artifact(self):
        self.runtime()
        result = self.build()
        details = self.details(result)
        self.assertEqual(1, len(result["evidence"]))
        self.assertGreater(len(details["evidence"]), len(result["evidence"]))
        for at in ATTEMPT_TIMES:
            filename = "escalation-" + decision._stamp(at).strftime("%Y%m%d-%H%M%S") + ".log"
            self.assertTrue(any(row["source"].endswith(filename) for row in details["evidence"]))
        self.assertIn("duplicate launcher", details["reason"])
        self.assertIn("UNKNOWN", json.dumps(details["evidence"]))

    def test_action_reports_prepared_work_not_user_clerical_tasks_or_execution(self):
        result = self.build()
        receipt = self.receipt(result)
        self.assertIn("Checked exact receipts", result["action"])
        self.assertIn("prepared an escalation-only pause proposal", result["action"])
        self.assertNotIn("stderr", result["action"])
        self.assertNotIn("inspect", result["action"].lower())
        self.assertFalse(receipt["prepared_plan"]["executed"])
        self.assertEqual(list(decision.CHECKS), receipt["prepared_plan"]["issue"])
        self.assertIn("stderr", receipt["prepared_plan"]["next_step"])
        self.assertEqual("pause", receipt["semantic_identity"]["decision"])
        self.assertEqual(result["decision"], receipt["details"]["decision"])

    def test_elevated_deadline_is_attributed_not_just_an_urgency_flag(self):
        self.intent("paused", resume_at="2030-04-12T18:00:00.000Z")
        result = self.build()
        self.assertEqual("time_sensitive", result["urgency"])
        row = result["evidence"][0]
        self.assertIn("owner intent", row["source"])
        self.assertIn("Declared wake deadline: 2030-04-12T18:00:00+00:00", row["observation"])

    def test_recovered_checks_suppress_old_alert(self):
        for row in self.verdict["checks"][:2]:
            row["ok"], row["detail"] = True, "current output measured"
        self.put("state/last_verdict.json", self.verdict)
        result = self.build()
        self.assertEqual("suppressed", result["status"])
        self.assertIn("historical alerts are not a new incident", result["reason"])

    def test_stale_future_or_naive_verdict_blocks(self):
        for stamp in ("2030-04-12T08:00:00+00:00", "2030-04-13T12:00:00+00:00",
                      "2030-04-12T12:00:00", "nonsense", None):
            with self.subTest(stamp=stamp):
                self.verdict["generated"] = stamp
                self.put("state/last_verdict.json", self.verdict)
                self.assertEqual("blocked", self.build()["status"])

    def test_javascript_z_timestamps_match_offset_receipts_on_python39(self):
        before = self.build()
        self.context["now"] = "2030-04-12T12:00:00.000Z"
        self.verdict["generated"] = self.context["now"]
        self.put("state/last_verdict.json", self.verdict)
        for row in self.history:
            row["at"] = row["at"].replace("+00:00", ".000Z")
            self.log(row["at"], "copilot timed out after 900s")
        self.put("state/escalations.json", self.history)
        self.put("state/issues.json", {
            ISSUE: {"attempts": 2, "last_attempt": self.history[-1]["at"]},
        })
        self.sent.update(at=self.context["now"], sent_at=self.context["now"])
        self.jsonl("state/outbox-sent.jsonl", [self.sent, self.sent])
        after = self.build()
        self.assertEqual("ready", after["status"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertTrue(self.receipt(after)["issue_record"]["receipts_complete"])

    def test_equivalent_z_owner_deadline_preserves_semantic_identity(self):
        self.intent("paused", resume_at="2030-04-12T18:00:00+00:00")
        before = self.build()
        self.intent("paused", resume_at="2030-04-12T18:00:00.000Z")
        after = self.build()
        self.assertEqual("ready", after["status"])
        self.assertEqual("2030-04-12T18:00:00.000Z", after["deadline"])
        self.assertEqual("time_sensitive", after["urgency"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_missing_or_malformed_check_coverage_blocks(self):
        for checks in ([], [{"id": "rv_world_merging", "ok": "false"}], "bad",
                       self.verdict["checks"] + [self.verdict["checks"][0]]):
            with self.subTest(checks=checks):
                self.put("state/last_verdict.json", {**self.verdict, "checks": checks})
                self.assertEqual("blocked", self.build()["status"])

    def test_corrupt_tail_blocks_and_is_reported(self):
        path = self.home / "state/outbox-sent.jsonl"
        with path.open("a") as stream:
            stream.write("{broken\n")
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertIn("corrupt sent-ledger", result["reason"])
        read = next(r for r in self.receipt(result)["source_reads"] if r["name"] == "sent_ledger")
        self.assertEqual(1, read["corrupt_rows"])

    def test_nonfinite_json_is_not_accepted(self):
        path = self.home / "state/last_verdict.json"
        path.write_text('{"generated": "2030-04-12T12:00:00Z", "checks": [], "n": NaN}')
        self.assertEqual("blocked", self.build()["status"])

    def test_document_and_receipt_size_caps(self):
        for path, limit in (
                (self.home / "state/last_verdict.json", decision.SOURCE_BOUNDS["document_bytes"]),
                (self.log(ATTEMPT_TIMES[0], ""), decision.SOURCE_BOUNDS["receipt_bytes"])):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(b"x" * (limit + 1))
                result = self.build()
                self.assertEqual("blocked", result["status"])
                self.assertTrue(any(r["status"] == "over_bound"
                                    for r in self.receipt(result)["source_reads"]))
                path.write_bytes(original)

    def test_ledger_window_is_bounded_without_scanning_history(self):
        path = self.home / "state/outbox-sent.jsonl"
        row = json.dumps(self.sent) + "\n"
        path.write_text(row * 1200)
        result = self.build()
        read = next(r for r in self.receipt(result)["source_reads"] if r["name"] == "sent_ledger")
        self.assertLessEqual(read["bytes_read"], decision.SOURCE_BOUNDS["ledger_tail_bytes"])
        self.assertEqual(decision.SOURCE_BOUNDS["ledger_rows"], read["rows_read"])
        self.assertTrue(read["truncated"])
        self.assertEqual("ready", result["status"])

    def test_history_and_attempt_limits_are_explicit(self):
        self.put("state/issues.json", {ISSUE: {"attempts": 99, "last_attempt": ATTEMPT_TIMES[-1]}})
        self.put("state/escalations.json", self.history * 120)
        result = self.build()
        receipt = self.receipt(result)
        self.assertEqual("blocked", result["status"])
        self.assertLessEqual(len(receipt["attempts"]), decision.SOURCE_BOUNDS["exact_attempts"])
        read = next(row for row in receipt["source_reads"] if row["name"] == "escalations")
        self.assertTrue(read["truncated"])

    def test_semantic_fingerprint_ignores_ages_times_repeat_counts_and_runs(self):
        self.runtime()
        original = self.build()
        self.context["now"] = "2030-04-13T12:00:00+00:00"
        self.verdict["generated"] = self.context["now"]
        self.verdict["checks"][0]["detail"] = "last merge 125.0h ago"
        self.verdict["checks"][1]["detail"] = "chat stale 225.0h; agent state stale 125.0h"
        self.verdict["checks"][3]["detail"] = (
            "spinning launchd job(s): org.example.gateway (runs=900, last exit 1)")
        self.put("state/last_verdict.json", self.verdict)
        self.sent.update(at=self.context["now"], sent_at=self.context["now"], entry_id="another-entry")
        self.sent["text"] = self.sent["text"].replace("101.0", "125.0").replace("201.0", "225.0")
        self.jsonl("state/outbox-sent.jsonl", [self.sent] * 10)
        self.runtime(observed_at=self.context["now"],
                     job={"label": "org.example.gateway", "state": "spawn scheduled",
                          "runs": 900, "last_exit": 1})
        updated = self.build()
        self.assertEqual("ready", updated["status"])
        self.assertEqual(original["fingerprint"], updated["fingerprint"])
        self.assertEqual(original["artifacts"], updated["artifacts"])

    def test_clock_aging_alone_does_not_change_incident_identity(self):
        before = self.build()
        self.context["now"] = "2030-04-20T12:00:00+00:00"
        after = self.build()
        self.assertEqual("blocked", after["status"])
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_new_actual_attempt_outcome_changes_identity(self):
        before = self.build()
        self.log(ATTEMPT_TIMES[-1], "SENTINEL_RESULT: BLOCKED — missing producer receipt\n")
        after = self.build()
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_new_operator_decision_changes_identity(self):
        before = self.build()
        self.intent("retired")
        self.assertNotEqual(before["fingerprint"], self.build()["fingerprint"])

    def test_changed_owner_deadline_is_a_new_decision_not_clock_aging(self):
        self.intent("paused", resume_at="2030-04-12T18:00:00+00:00")
        before = self.build()
        self.intent("paused", resume_at="2030-04-15T18:00:00+00:00")
        self.assertNotEqual(before["fingerprint"], self.build()["fingerprint"])

    def test_sources_can_be_overridden_without_touching_defaults(self):
        alternate = self.put("alternate.json", {
            **self.verdict,
            "checks": [{"id": key, "ok": True, "detail": "measured current"}
                       for key in decision.CHECKS],
        })
        self.context["sources"]["verdict"] = alternate.name
        self.assertEqual("suppressed", self.build()["status"])

    def test_adapter_supplies_device_home_without_module_fallback(self):
        with mock.patch.object(Path, "home", return_value=self.root):
            device_home = Path.home() / ".storykeeper" / "home"
        device_home.parent.mkdir()
        shutil.move(self.home, device_home)
        self.home = device_home
        self.context["home"] = str(device_home)
        with mock.patch.object(Path, "home", side_effect=AssertionError("caller owns defaults")):
            result = self.build()
        self.assertEqual("ready", result["status"])
        for read in self.receipt(result)["source_reads"]:
            self.assertIn(device_home, Path(read["source"]).parents)

    def test_missing_context_home_never_reads_a_logged_in_users_files(self):
        del self.context["home"]
        with mock.patch.object(Path, "home", side_effect=AssertionError("no implicit home")), \
                mock.patch.object(decision._Sources, "read",
                                  side_effect=AssertionError("no source access")):
            result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], result["artifacts"])

    def test_all_sources_can_be_overridden_for_another_device_snapshot(self):
        self.context["sources"] = {
            key: str(self.home / relative) for key, relative in decision.DEFAULT_SOURCES.items()
        }
        other_home = self.root / "another-device" / ".storykeeper" / "home"
        self.context["home"] = str(other_home)
        result = self.build()
        self.assertEqual("ready", result["status"])
        self.assertFalse(other_home.exists())
        for read in self.receipt(result)["source_reads"]:
            self.assertIn(self.home, Path(read["source"]).parents)

    def test_regular_sources_work_without_platform_nonblock_flag(self):
        with mock.patch.object(decision.os, "O_NONBLOCK", 0, create=True):
            self.assertEqual("ready", self.build()["status"])

    def test_public_sources_contain_no_device_identity_or_contact_literals(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("scenarios/decision.py", "tests/test_scenario_decision.py",
                         "docs/scenarios/decision.md"):
            text = (root / relative).read_text(encoding="utf-8")
            for pattern in (
                    r"/(?:Users|home)/[A-Za-z0-9_.-]+",
                    r"[A-Za-z]:\\Users\\[^\\\s]+",
                    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                    r"(?<!\w)\+\d{7,15}\b"):
                with self.subTest(source=relative, pattern=pattern):
                    self.assertNotRegex(text, pattern)

    def test_build_is_read_only_except_private_artifacts(self):
        def snapshot():
            return {
                str(path.relative_to(self.home)):
                (hashlib.sha256(path.read_bytes()).hexdigest(),
                 path.stat().st_mtime_ns, path.stat().st_mode)
                for path in self.home.rglob("*") if path.is_file()
            }
        before = snapshot()
        with mock.patch("subprocess.run", side_effect=AssertionError("no process calls")):
            result = self.build()
        self.assertEqual(before, snapshot())
        artifact = Path(result["artifacts"][0])
        self.assertEqual(0o600, stat.S_IMODE(artifact.stat().st_mode))
        self.assertEqual([artifact], list(self.artifacts.iterdir()))

    def test_artifact_hardlink_cannot_truncate_live_source(self):
        result = self.build()
        artifact = Path(result["artifacts"][0])
        source = self.home / "config.json"
        original = source.read_bytes()
        artifact.unlink()
        os.link(source, artifact)
        self.assertEqual("ready", self.build()["status"])
        self.assertEqual(original, source.read_bytes())

    def test_artifacts_inside_home_or_logs_are_rejected(self):
        for path in (self.home, self.home / "artifacts", self.home / "logs"):
            with self.subTest(path=path):
                self.context["artifact_dir"] = str(path)
                result = self.build()
                self.assertEqual("blocked", result["status"])
                self.assertEqual([], result["artifacts"])

    def test_artifact_failure_blocks_instead_of_claiming_persistence(self):
        self.artifacts.write_text("not a directory")
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], result["artifacts"])
        self.assertIn("could not be persisted", result["reason"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_non_regular_source_is_rejected_without_blocking(self):
        path = self.home / "state/last_verdict.json"
        path.unlink()
        os.mkfifo(path)
        result = self.build()
        self.assertEqual("blocked", result["status"])
        self.assertTrue(any(r["status"] == "not_regular"
                            for r in self.receipt(result)["source_reads"]))

    def test_invalid_context_is_json_safe_and_blocked(self):
        for context in (None, [], {}, {**self.context, "now": "naive"},
                        {**self.context, "sources": []},
                        {**self.context, "sources": {"verdict": 123}}):
            with self.subTest(context=context):
                result = decision.build(context)
                self.assertEqual("blocked", result["status"])
                json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
