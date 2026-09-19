"""Synthetic, sanitized fixtures; no device-local calendar, message, or state reads."""

import copy
import json
import unittest
from typing import Optional, get_type_hints
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scenarios.interrupt import build, delivery_key, evaluate, render


NOW = "2026-09-19T16:00:00Z"
OPEN = {"quiet_hours": False}
try:
    ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    HAS_LOCAL_ZONE = False
else:
    HAS_LOCAL_ZONE = True


def proposal(**updates):
    result = {
        "scenario": "future",
        "status": "ready",
        "title": "Release risk",
        "change": "Release validation fails.",
        "impact": "Release delivery is blocked.",
        "action": "Prepared a repair plan; no fix applied.",
        "decision": "Approve the repair?",
        "evidence": [{
            "source": "checks/release.json",
            "observation": "Release validation failed: 3 required checks.",
        }],
        "artifacts": ["repair-plan.md"],
        "fingerprint": "release:required-checks",
        "urgency": "routine",
        "reason": "New failing checks have a repair path.",
    }
    result.update(updates)
    return result


def record(item=None, at="2026-09-19T15:00:00Z", decision="queued"):
    item = proposal() if item is None else copy.deepcopy(item)
    return {
        "at": at, "scenario": item["scenario"], "fingerprint": item["fingerprint"],
        "decision": decision, "proposal": item,
    }


def deadline_proposal(deadline="2026-09-19T23:30:00Z", urgency="urgent"):
    return proposal(
        deadline=deadline, urgency=urgency,
        evidence=[{
            "source": "release-calendar",
            "observation": f"Release deadline: {deadline}; validation failed.",
        }],
    )


class EvidenceTests(unittest.TestCase):
    def test_complete_evidenced_proposal_is_admitted(self):
        result = evaluate(proposal(), [], NOW)
        self.assertEqual(result, {
            "allow": True, "reason": "supported_new_finding",
            "fingerprint": "release:required-checks",
        })

    def test_all_nine_producers_share_the_gate(self):
        for scenario in (
            "future", "decision", "connections", "parallel", "meeting",
            "intentions", "win", "adversary", "timeline",
        ):
            with self.subTest(scenario=scenario):
                self.assertTrue(evaluate(proposal(scenario=scenario), [], NOW)["allow"])

    def test_unsupported_results_and_types_fail_closed(self):
        for update in (
            {"scenario": "interrupt"}, {"scenario": "other"}, {"scenario": []},
            {"status": "suppressed"}, {"status": "blocked"}, {"status": "success"},
            {"evidence": []}, {"evidence": "trust me"}, {"evidence": [None]},
            {"evidence": [{"source": "", "observation": "Passed."}]},
            {"evidence": [{"source": "report", "observation": "unknown"}]},
            {"artifacts": "report.md"}, {"artifacts": [False]},
            {"fingerprint": 23}, {"urgency": "critical"},
            {"fingerprint": "\ud800"}, {"change": "Release \udfff failed."},
        ):
            with self.subTest(update=update):
                result = evaluate(proposal(**update), [], NOW)
                self.assertFalse(result["allow"])
                self.assertIn("unsupported_proposal", result["reason"])
        for invalid in (None, [], "ready"):
            self.assertFalse(evaluate(invalid, [], NOW)["allow"])

    def test_four_explicit_answers_are_required(self):
        for field in ("change", "impact", "action", "decision"):
            for value in ("", " ", "N/A", "unknown", None):
                with self.subTest(field=field, value=value):
                    self.assertFalse(evaluate(proposal(**{field: value}), [], NOW)["allow"])
        self.assertTrue(evaluate(proposal(decision="No decision needed."), [], NOW)["allow"])
        self.assertTrue(evaluate(proposal(action="No action taken; approval required."), [], NOW)["allow"])

    def test_json_shaped_wrong_types_return_structured_rejections(self):
        for field in (
            "scenario", "status", "title", "change", "impact", "action", "decision",
            "fingerprint", "reason", "urgency", "evidence", "deadline",
        ):
            for value in (None, False, 0, [], {}, ["x"], {"x": 1}):
                with self.subTest(field=field, value=value):
                    item = proposal(**{field: value})
                    result = evaluate(item, [], NOW)
                    self.assertEqual(set(result), {"allow", "reason", "fingerprint"})
                    self.assertFalse(result["allow"])
                    self.assertIsInstance(result["fingerprint"], str)
                    report = build({"now": NOW, "proposal": item})
                    self.assertFalse(report["assessments"][0]["allow"])

    def test_routine_heartbeats_never_become_news(self):
        for change in (
            "Heartbeat: everything is running.",
            "No new findings; monitoring continues.",
            "All systems are green.",
            "No issues detected.",
            "Still monitoring.",
        ):
            with self.subTest(change=change):
                self.assertFalse(evaluate(proposal(change=change), [], NOW)["allow"])
        self.assertFalse(evaluate(proposal(evidence=[{
            "source": "poll", "observation": "Observed at 2026-09-19T16:00:00Z",
        }]), [], NOW)["allow"])

    def test_observer_blindness_or_an_assumption_is_not_supported_evidence(self):
        for observation in (
            "Cannot read the release report.",
            "No evidence was collected.",
            "Unverified release failure.",
            "Assumed the release failed.",
        ):
            with self.subTest(observation=observation):
                item = proposal(evidence=[{"source": "reader", "observation": observation}])
                self.assertFalse(evaluate(item, [], NOW)["allow"])

    def test_arbitrary_urgency_flags_are_not_evidence(self):
        for urgency in ("time_sensitive", "urgent"):
            result = evaluate(proposal(urgency=urgency), [], NOW)
            self.assertFalse(result["allow"])
            self.assertIn("unsupported_urgency", result["reason"])

    def test_real_imminent_deadline_can_justify_quiet_hours_exception(self):
        item = deadline_proposal()
        self.assertTrue(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])
        self.assertFalse(evaluate(item, [], "2026-09-19T16:00:00Z")["allow"])
        self.assertFalse(evaluate(item, [], "2026-09-20T00:00:00Z")["allow"])
        item["urgency"] = "time_sensitive"
        self.assertTrue(evaluate(item, [], NOW)["allow"])
        self.assertFalse(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])

    def test_deadline_requires_attributed_absolute_confirmation(self):
        item = deadline_proposal()
        for observation in (
            "A release is coming soon.",
            "Checked at 2026-09-19T23:30:00Z; validation failed.",
            "Checked at 2026-09-19T23:30:00Z by the release monitor.",
            "Tentative release deadline: 2026-09-19T23:30:00Z.",
            "No confirmed deadline; now is 2026-09-19T23:30:00Z.",
            "Release deadline: 2026-09-20T23:30:00Z.",
        ):
            with self.subTest(observation=observation):
                item["evidence"][0]["observation"] = observation
                self.assertFalse(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])
        item["evidence"][0]["observation"] = "Release deadline: 2026-09-19T19:30:00-04:00."
        self.assertTrue(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])

    def test_observed_active_impact_can_justify_urgency_without_deadline(self):
        item = proposal(
            urgency="urgent", impact="Payment outage blocks 42 customers.",
            evidence=[{
                "source": "payments/probe",
                "observation": "Payment outage: 42 customers blocked.",
            }],
        )
        self.assertTrue(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])
        for observation in (
            "Possible payment outage: 42 customers blocked.",
            "No payment outage: 42 customers passed.",
            "Simulated payment outage: 42 customers blocked.",
            "Payment outage: 2 customers blocked.",
        ):
            with self.subTest(observation=observation):
                item["evidence"][0]["observation"] = observation
                self.assertFalse(evaluate(item, [], "2026-09-19T23:00:00Z")["allow"])


class DuplicateTests(unittest.TestCase):
    def test_no_resend_when_cooldown_or_day_has_elapsed(self):
        history = [record()]
        for now in (NOW, "2026-09-20T16:00:00Z", "2027-09-19T16:00:00Z"):
            with self.subTest(now=now):
                result = evaluate(proposal(), history, now, OPEN)
                self.assertFalse(result["allow"])
                self.assertIn("duplicate", result["reason"])

    def test_moving_age_and_clock_do_not_create_a_new_finding(self):
        old = proposal(
            change="Release report stale for 62.1h.",
            impact="Report is 62.1 hours old.",
            evidence=[{
                "source": "report",
                "observation": "Report stale for 62.1h; checked at 2026-09-19T15:00:00Z.",
            }],
        )
        new = proposal(
            change="Release report stale for 63.4h.",
            impact="Report is 63.4 hours old.",
            evidence=[{
                "source": "report",
                "observation": "Report stale for 63.4h; checked at 2026-09-19T16:00:00Z.",
            }],
            now=NOW, age_hours=63.4,
        )
        result = evaluate(new, [record(old)], NOW)
        self.assertFalse(result["allow"])
        self.assertEqual(result["fingerprint"], old["fingerprint"])
        self.assertIn("duplicate", result["reason"])

    def test_fingerprint_churn_alone_does_not_bypass_semantic_dedupe(self):
        result = evaluate(proposal(fingerprint="new-clock-hash"), [record()], NOW)
        self.assertFalse(result["allow"])
        self.assertIn("duplicate", result["reason"])

    def test_shared_semantic_identity_deduplicates_across_producers(self):
        result = evaluate(proposal(scenario="timeline"), [record()], NOW)
        self.assertFalse(result["allow"])
        self.assertIn("duplicate", result["reason"])
        self.assertTrue(evaluate(proposal(
            scenario="timeline", fingerprint="timeline:distinct-finding",
        ), [record()], NOW)["allow"])

    def test_presentational_changes_and_new_source_are_not_new_evidence(self):
        for update in (
            {"title": "LOUDER RELEASE WARNING", "urgency": "urgent"},
            {"action": "Prepared the same plan again.", "decision": "Please approve?"},
            {"artifacts": ["regenerated-plan.md"], "reason": "New poll."},
            {"change": "  RELEASE validation fails.  "},
            {"evidence": [{
                "source": "new-report", "observation": "Release validation failed: 3 required checks.",
            }]},
        ):
            with self.subTest(update=update):
                self.assertFalse(evaluate(proposal(**update), [record()], NOW)["allow"])

    def test_appending_a_fresh_poll_time_or_countdown_does_not_create_evidence(self):
        item = proposal()
        item["evidence"][0]["observation"] += " Checked at 2026-09-19T16:00:00Z."
        self.assertFalse(evaluate(item, [record()], NOW)["allow"])
        old = proposal(change="Release starts in 90 minutes.")
        new = proposal(change="Release starts in 30 minutes.")
        self.assertFalse(evaluate(new, [record(old)], NOW)["allow"])

    def test_evidence_order_and_duplicate_rows_are_irrelevant(self):
        old = proposal()
        old["evidence"].append({"source": "plan", "observation": "Plan includes rollback."})
        new = copy.deepcopy(old)
        new["evidence"].reverse()
        new["evidence"].append(new["evidence"][0].copy())
        self.assertFalse(evaluate(new, [record(old)], NOW)["allow"])

    def test_worsening_impact_requires_new_evidence_and_preserves_real_numbers(self):
        item = proposal(impact="Release validation now blocks 17 checks.")
        result = evaluate(item, [record()], NOW)
        self.assertFalse(result["allow"])
        self.assertIn("unsupported_update", result["reason"])
        item["evidence"][0]["observation"] = "Release validation failed: 17 required checks."
        self.assertTrue(evaluate(item, [record()], NOW)["allow"])
        item["evidence"][0]["observation"] = "Release validation still failed: 3 checks."
        self.assertFalse(evaluate(item, [record()], NOW)["allow"])

    def test_substantive_new_receipt_is_eligible(self):
        item = proposal(evidence=[{
            "source": "repair-test",
            "observation": "Repair dry run passes 3 previously failing checks.",
        }])
        self.assertTrue(evaluate(item, [record()], NOW)["allow"])

    def test_flapping_back_to_an_already_notified_version_stays_quiet(self):
        worse = proposal(
            impact="Release validation blocks 17 checks.",
            evidence=[{"source": "checks", "observation": "17 required release checks failed."}],
        )
        history = [record(worse, "2026-09-19T15:30:00Z"), record()]
        self.assertFalse(evaluate(proposal(), history, NOW)["allow"])

    def test_deadline_escalation_requires_more_than_passing_time_or_flag(self):
        old = deadline_proposal(urgency="routine")
        new = copy.deepcopy(old)
        new["urgency"] = "urgent"
        self.assertFalse(evaluate(new, [record(old)], "2026-09-19T23:00:00Z")["allow"])
        new = deadline_proposal("2026-09-19T23:15:00Z")
        self.assertTrue(evaluate(new, [record(old)], "2026-09-19T23:00:00Z")["allow"])
        new["evidence"] = copy.deepcopy(old["evidence"])
        self.assertFalse(evaluate(new, [record(old)], "2026-09-19T23:00:00Z")["allow"])

    def test_equivalent_deadline_offsets_are_not_new_deadlines(self):
        old = deadline_proposal(urgency="routine")
        new = copy.deepcopy(old)
        new["deadline"] = "2026-09-19T19:30:00-04:00"
        self.assertFalse(evaluate(new, [record(old)], NOW, OPEN)["allow"])
        new["evidence"][0]["observation"] = (
            "Release deadline: 2026-09-19T19:30:00-04:00; validation failed."
        )
        self.assertFalse(evaluate(new, [record(old)], NOW, OPEN)["allow"])


class PolicyAndHistoryTests(unittest.TestCase):
    def test_quiet_boundaries_and_explicit_disable(self):
        for stamp, allow in (
            ("2026-09-19T21:59:59Z", True), ("2026-09-19T22:00:00Z", False),
            ("2026-09-20T07:59:59Z", False), ("2026-09-20T08:00:00Z", True),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(evaluate(proposal(), [], stamp)["allow"], allow)
        for off in (False, None, "off"):
            self.assertTrue(evaluate(proposal(), [], "2026-09-19T23:00:00Z", {
                "quiet_hours": off,
            })["allow"])
        policy = {"quiet_hours": {"start": "09:00", "end": "10:00"}}
        self.assertFalse(evaluate(proposal(), [], "2026-09-19T09:30:00Z", policy)["allow"])
        self.assertTrue(evaluate(proposal(), [], "2026-09-19T10:00:00Z", policy)["allow"])

    @unittest.skipUnless(HAS_LOCAL_ZONE, "IANA timezone data unavailable")
    def test_local_zone_and_dst_fold_use_the_supplied_instant(self):
        policy = {"timezone": "America/New_York"}
        self.assertFalse(evaluate(proposal(), [], "2026-09-20T03:00:00Z", policy)["allow"])
        self.assertTrue(evaluate(proposal(), [], "2026-09-20T12:00:00Z", policy)["allow"])
        for stamp in ("2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"):
            self.assertFalse(evaluate(proposal(), [], stamp, policy)["allow"])

    def test_bad_times_zones_and_policies_fail_closed(self):
        for stamp in (
            "", "yesterday", "2026-09-19", "2026-09-19T16:00:00",
            "2026-02-30T16:00:00Z", "2026-09-19T16:00:00+25:00", None,
        ):
            with self.subTest(stamp=stamp):
                self.assertFalse(evaluate(proposal(), [], stamp)["allow"])
        for policy in (
            [], {"timezone": "Not/AZone"}, {"timezone": ""}, {"timezone": 4},
            {"max_daily": -1}, {"max_daily": True}, {"max_daily": "6"},
            {"quiet_hours": True}, {"quiet_hours": {}},
            {"quiet_hours": {"start": "25:00", "end": "08:00"}},
            {"quiet_hours": {"start": "22:7", "end": "08:00"}},
            {"quiet_hours": {"start": "08:00", "end": "08:00"}},
            {"urgent_hours": float("nan")}, {"urgent_hours": float("inf")},
            {"urgent_hours": 0}, {"urgent_hours": 30}, {"time_sensitive_hours": False},
        ):
            with self.subTest(policy=policy):
                result = evaluate(proposal(), [], NOW, policy)
                self.assertFalse(result["allow"])
                self.assertIn("invalid_policy_or_time", result["reason"])
        self.assertFalse(evaluate(proposal(deadline="tomorrow"), [], NOW)["allow"])

    def budget_history(self, count):
        return [record(proposal(
            scenario="timeline", fingerprint=f"other-{index}",
            title=f"Other finding {index}",
        )) for index in range(count)]

    def test_budget_is_global_six_per_local_day_with_no_urgent_bypass(self):
        self.assertTrue(evaluate(proposal(), self.budget_history(5), NOW)["allow"])
        result = evaluate(proposal(), self.budget_history(6), NOW)
        self.assertFalse(result["allow"])
        self.assertIn("daily_budget", result["reason"])
        self.assertFalse(evaluate(deadline_proposal(), self.budget_history(6), "2026-09-19T23:00:00Z")["allow"])
        self.assertFalse(evaluate(proposal(), [], NOW, {"max_daily": 0})["allow"])
        self.assertTrue(evaluate(proposal(), self.budget_history(9), NOW, {
            "max_daily": 10, "quiet_hours": False,
        })["allow"])
        self.assertFalse(evaluate(proposal(), [record()], NOW, {
            "max_daily": 10, "quiet_hours": False, "allow_duplicates": True,
        })["allow"])

    @unittest.skipUnless(HAS_LOCAL_ZONE, "IANA timezone data unavailable")
    def test_budget_uses_calendar_day_not_rolling_age_or_utc_date(self):
        history = self.budget_history(6)
        for item in history:
            item["at"] = "2026-09-19T23:00:00Z"
        policy = {"timezone": "America/New_York", "quiet_hours": False}
        self.assertFalse(evaluate(proposal(), history, "2026-09-20T03:00:00Z", policy)["allow"])
        self.assertTrue(evaluate(proposal(), history, "2026-09-20T04:00:00Z", policy)["allow"])

    def test_only_queued_consumes_budget_or_duplicate_effect(self):
        history = [record(decision=kind) for kind in ("suppressed", "error")] * 10
        self.assertTrue(evaluate(proposal(), history, NOW)["allow"])
        self.assertTrue(evaluate(proposal(), [{"decision": "error", "at": "broken"}], NOW)["allow"])
        history.append(record())
        self.assertFalse(evaluate(proposal(), history, NOW)["allow"])

    def test_corrupt_queued_history_and_unknown_decisions_fail_closed(self):
        for history in (
            None, {}, [None], [{"decision": "sent"}],
            [record(at="broken")], [record(at="2026-09-20T16:00:00Z")],
            [{**record(), "proposal": {}}], [{**record(), "fingerprint": "mismatch"}],
        ):
            with self.subTest(history=history):
                result = evaluate(proposal(), history, NOW)
                self.assertFalse(result["allow"])
                self.assertIn("invalid_history", result["reason"])

    @unittest.skipUnless(HAS_LOCAL_ZONE, "IANA timezone data unavailable")
    def test_unrepresentable_local_history_fails_closed(self):
        history = [record(
            proposal(scenario="timeline", fingerprint="timeline:other"),
            at="0001-01-01T00:00:00Z",
        )]
        result = evaluate(proposal(), history, NOW, {
            "timezone": "America/New_York", "quiet_hours": False,
        })
        self.assertFalse(result["allow"])
        self.assertIn("invalid_history", result["reason"])

    def test_pure_gate_leaves_lock_and_successful_reservation_to_caller(self):
        item = proposal()
        history = []
        policy = {"max_daily": 1, "quiet_hours": False}
        before = copy.deepcopy((item, history, policy))
        with mock.patch("builtins.open", side_effect=AssertionError("gate performed IO")):
            first = evaluate(item, history, NOW, policy)
            second = evaluate(item, history, NOW, policy)
        self.assertEqual(first, second)
        self.assertTrue(first["allow"])
        self.assertEqual((item, history, policy), before)
        history.append(record(decision="error"))
        self.assertTrue(evaluate(item, history, NOW, policy)["allow"])
        history.append(record())
        self.assertFalse(evaluate(item, history, NOW, policy)["allow"])


class RenderingAndBuildTests(unittest.TestCase):
    def test_render_is_self_contained_and_does_not_claim_execution(self):
        item = proposal(decision="Do not deploy. Approve only after review.")
        text = render(item)
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text), 650)
        self.assertLessEqual(len(text.split()), 90)
        for field in ("title", "change", "impact", "action", "decision"):
            self.assertIn(item[field], text)
        for evidence in item["evidence"]:
            self.assertIn(evidence["source"], text)
            self.assertIn(evidence["observation"], text)
        self.assertIn("no fix applied", text)
        self.assertNotIn("Notification sent", text)

    def test_ascii_conversion_retains_unknown_characters_as_escapes(self):
        text = render(proposal(title="Caf\u00e9 \u2014 release \u2192 \u672a"))
        self.assertTrue(text.isascii())
        self.assertIn("Cafe - release -> \\u672a", text)
        text = render(proposal(title="Approved\u0338"))
        self.assertIn("\\u0338", text)

    def test_long_text_is_rejected_not_blindly_truncated(self):
        for field, value in (
            ("decision", "Approve " * 100 + "only after rollback review."),
            ("impact", "x" * 700),
            ("change", "a " * 100),
        ):
            with self.subTest(field=field):
                item = proposal(**{field: value})
                with self.assertRaisesRegex(ValueError, "message_too_long"):
                    render(item)
                self.assertFalse(evaluate(item, [], NOW)["allow"])
        with self.assertRaises(ValueError):
            render(proposal(status="blocked"))

    def test_render_budget_boundaries_are_inclusive(self):
        item = proposal()
        item["change"] += "x" * (650 - len(render(item)))
        self.assertEqual(len(render(item)), 650)
        item["change"] += "x"
        with self.assertRaisesRegex(ValueError, "message_too_long"):
            render(item)
        item = proposal()
        item["change"] += " x" * (90 - len(render(item).split()))
        self.assertEqual(len(render(item).split()), 90)
        item["change"] += " x"
        with self.assertRaisesRegex(ValueError, "message_too_long"):
            render(item)

    def test_private_builder_reports_assessment_not_a_send(self):
        item = proposal()
        result = build({"proposal": item, "history": [], "now": NOW, "policy": OPEN})
        self.assertEqual(result["scenario"], "interrupt")
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(len(result["assessments"]), 1)
        self.assertTrue(result["assessments"][0]["allow"])
        self.assertIn("individually eligible", result["change"])
        self.assertIn("no notifications were queued or sent", result["reason"])
        self.assertEqual(result["decision"], "No decision needed.")
        self.assertFalse(evaluate(result, [], NOW)["allow"])
        later = build({"proposal": item, "history": [], "now": "2026-09-20T16:00:00Z"})
        self.assertEqual(result["fingerprint"], later["fingerprint"])

    def test_builder_does_not_simulate_reservations_for_a_batch(self):
        result = build({
            "proposals": [proposal(), proposal()], "history": [], "now": NOW,
            "policy": {"max_daily": 1},
        })
        self.assertEqual([item["allow"] for item in result["assessments"]], [True, True])
        self.assertIn("individually", result["change"])

    def test_builder_reports_missing_inputs_honestly(self):
        result = build({"now": NOW})
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(result["change"], "No proposals assessed.")
        for context in ({}, None, {"now": NOW, "proposals": "bad"}):
            with self.subTest(context=context):
                result = build(context)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["change"], "No proposals assessed.")
                self.assertIn("Cannot assess", result["reason"])
        result = build({
            "now": NOW,
            "proposals": [
                proposal(scenario={"invalid": "type"}), proposal(scenario="\ud800"), proposal(),
            ],
        })
        self.assertEqual([item["allow"] for item in result["assessments"]], [False, False, True])


class ProtocolPortabilityTests(unittest.TestCase):
    def test_sdk_type_hint_reflection_works_on_python_39(self):
        self.assertEqual(get_type_hints(evaluate), {
            "proposal": dict, "history": list[dict], "now": str,
            "policy": Optional[dict], "return": dict,
        })
        self.assertEqual(get_type_hints(render), {"proposal": dict, "return": str})
        self.assertEqual(get_type_hints(build), {"context": dict, "return": dict})
        self.assertEqual(get_type_hints(delivery_key), {"proposal": dict, "return": str})

    def test_evaluation_output_is_bounded_even_for_oversized_fingerprints(self):
        for fingerprint in ("f" * 512, "\U0001f9e9" * 512):
            with self.subTest(size=len(fingerprint)):
                result = evaluate(proposal(fingerprint=fingerprint), [], NOW)
                self.assertTrue(result["allow"])
                self.assertEqual(result["fingerprint"], fingerprint)
                self.assertLess(len(json.dumps(result).encode("utf-8")), 8192)
        for fingerprint in ("f" * 513, '"' * (1024 * 1024 + 1)):
            with self.subTest(size=len(fingerprint)):
                result = evaluate(proposal(fingerprint=fingerprint), [], NOW)
                self.assertFalse(result["allow"])
                self.assertEqual(result["fingerprint"], "")
                self.assertLess(len(json.dumps(result).encode("utf-8")), 8192)

    def test_z_timestamps_match_explicit_utc_offsets(self):
        item = proposal()
        self.assertEqual(
            evaluate(item, [], NOW),
            evaluate(item, [], NOW.replace("Z", "+00:00")),
        )
        self.assertEqual(
            build({"now": NOW, "proposal": item}),
            build({"now": NOW.replace("Z", "+00:00"), "proposal": item}),
        )

    def test_default_utc_does_not_require_an_installed_iana_database(self):
        with mock.patch(
            "scenarios.interrupt.ZoneInfo", side_effect=ZoneInfoNotFoundError("unavailable"),
        ) as zone:
            self.assertTrue(evaluate(proposal(), [], NOW)["allow"])
            self.assertTrue(evaluate(proposal(), [], NOW, {"timezone": "UTC"})["allow"])
            self.assertTrue(build({"now": NOW, "proposal": proposal()})["assessments"][0]["allow"])
            zone.assert_not_called()
            result = evaluate(proposal(), [], NOW, {"timezone": "America/New_York"})
            self.assertFalse(result["allow"])
            self.assertIn("unavailable IANA timezone data", result["reason"])

    def test_explicit_source_identifiers_are_opaque_and_do_not_touch_device_home(self):
        for source in (
            "fixture:release-check",
            "receipts/release.json",
            r"receipts\release.json",
            "https://example.invalid/receipts/release",
        ):
            with self.subTest(source=source):
                item = proposal(evidence=[{
                    "source": source, "observation": "Release validation failed: 3 required checks.",
                }])
                with mock.patch("pathlib.Path.home", side_effect=AssertionError("resolved home")), \
                     mock.patch("pathlib.Path.open", side_effect=AssertionError("opened source")), \
                     mock.patch("builtins.open", side_effect=AssertionError("performed IO")):
                    self.assertTrue(evaluate(item, [], NOW)["allow"])
                    self.assertIn(source, render(item))
                    report = build({"now": NOW, "proposal": item})
                self.assertTrue(report["assessments"][0]["allow"])
                self.assertEqual(report["artifacts"], [])

    def test_protocol_json_round_trip_preserves_results_without_runtime_defaults(self):
        context = {
            "now": NOW, "proposal": proposal(), "history": [],
            "policy": {"quiet_hours": False, "max_daily": 10},
        }
        wire = json.loads(json.dumps(context, allow_nan=False))
        result = evaluate(wire["proposal"], wire["history"], wire["now"], wire["policy"])
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        report = build(wire)
        self.assertEqual(report, json.loads(json.dumps(report, allow_nan=False)))
        first = build({**wire, "home": "fixtures/device-a"})
        second = build({**wire, "home": "fixtures/device-b"})
        self.assertEqual(first, second)
        self.assertEqual(report, first)
        self.assertEqual(context, wire)


class DeliveryKeyTests(unittest.TestCase):
    def test_retry_identity_ignores_aging_clocks_and_presentation(self):
        old = proposal(
            change="Release report stale for 62.1h.",
            impact="Report is 62.1 hours old.",
            evidence=[{
                "source": "report",
                "observation": "Report stale for 62.1h; checked at 2026-09-19T15:00:00Z.",
            }],
        )
        new = copy.deepcopy(old)
        for field in ("change", "impact"):
            new[field] = new[field].replace("62.1", "63.4")
        new["evidence"][0]["observation"] = (
            "Report stale for 63.4h; checked at 2026-09-19T16:00:00Z."
        )
        new.update(
            title="Another release heading", action="Plan remains unapplied.",
            decision="Review the same plan?", artifacts=["new-report.md"],
            now=NOW, age_hours=63.4,
        )
        before = copy.deepcopy((old, new))
        with mock.patch("builtins.open", side_effect=AssertionError("performed IO")):
            self.assertEqual(delivery_key(old), delivery_key(new))
        self.assertEqual((old, new), before)
        self.assertRegex(delivery_key(old), r"^[a-f0-9]{64}$")

    def test_admitted_worsening_gets_a_new_delivery_key_with_the_same_finding_identity(self):
        old = proposal()
        new = proposal(
            impact="Release validation blocks 17 checks.",
            evidence=[{"source": "checks", "observation": "17 required release checks failed."}],
        )
        self.assertEqual(old["fingerprint"], new["fingerprint"])
        self.assertTrue(evaluate(new, [record(old)], NOW)["allow"])
        self.assertNotEqual(delivery_key(old), delivery_key(new))

    def test_evidenced_deadline_change_has_a_distinct_delivery_version(self):
        old = deadline_proposal(urgency="routine")
        equivalent = copy.deepcopy(old)
        equivalent["deadline"] = "2026-09-19T19:30:00-04:00"
        equivalent["evidence"][0]["observation"] = (
            "Release deadline: 2026-09-19T19:30:00-04:00; validation failed."
        )
        self.assertEqual(delivery_key(old), delivery_key(equivalent))
        new = deadline_proposal("2026-09-19T23:15:00Z")
        self.assertTrue(evaluate(new, [record(old)], "2026-09-19T23:00:00Z")["allow"])
        self.assertNotEqual(delivery_key(old), delivery_key(new))

    def test_delivery_identity_is_global_but_not_a_second_policy_gate(self):
        old = proposal()
        self.assertEqual(delivery_key(old), delivery_key(proposal(scenario="timeline")))
        self.assertNotEqual(
            delivery_key(old), delivery_key(proposal(fingerprint="release:other-finding")),
        )
        self.assertEqual(delivery_key(old), delivery_key(proposal(urgency="urgent")))
        self.assertFalse(evaluate(proposal(urgency="urgent"), [], NOW)["allow"])
        self.assertFalse(evaluate(old, [record(old)], NOW)["allow"])


if __name__ == "__main__":
    unittest.main()
