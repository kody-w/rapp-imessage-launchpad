"""Synthetic, deterministic fixtures; no live notes, network, outbox, or /tmp."""

import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import stat
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from scenarios import intentions


NOW = "2026-09-19T18:30:00Z"
ROOT = Path(__file__).resolve().parents[1]


def promise(identifier="release-outline", **overrides):
    record = {
        "id": identifier,
        "kind": "promise",
        "owner_id": "self",
        "status": "open",
        "title": "Prepare the release review",
        "created_at": "2026-08-20T12:00:00Z",
        "updated_at": "2026-09-01T12:00:00Z",
        "original": {
            "kind": "note",
            "author_id": "self",
            "ref": "synthetic-project-journal#release-review",
            "quote": "I will prepare the release review.",
            "context": "Next: Draft a release outline. Sections: Audience; Verified changes; Caveats.",
        },
        "consequence": {"level": "high", "reason": "The release review needs an agreed scope."},
        "relevance": {
            "state": "current",
            "checked_at": "2026-09-18T12:00:00Z",
            "reason": "The release review remains on the current milestone.",
        },
        "next_step": {
            "format": "outline",
            "title": "Release review outline",
            "action": "Draft a release outline",
            "points": ["Audience", "Verified changes", "Caveats"],
        },
        "deadline": "2026-09-20T12:00:00Z",
    }
    record.update(overrides)
    return record


class IntentionsTests(unittest.TestCase):
    def setUp(self):
        self.workspace = ROOT / ".scratch-home" / f"intentions-{os.getpid()}-{uuid.uuid4().hex}"
        self.workspace.mkdir(mode=0o700, parents=True)
        self.addCleanup(shutil.rmtree, self.workspace)
        self.home = self.workspace / "home"
        self.home.mkdir(mode=0o700)
        self.artifacts = self.workspace / "artifacts"
        self.input = self.home / "promises.json"
        self.context = {
            "home": str(self.home),
            "artifact_dir": str(self.artifacts),
            "now": NOW,
            "sources": {"intentions": {"owner_id": "self", "files": ["promises.json"]}},
        }

    def write(self, records, path=None, as_of=NOW):
        path = path or self.input
        path.write_text(json.dumps({"schema": intentions.SCHEMA, "as_of": as_of, "items": records}),
                        encoding="utf-8")
        return path

    def build(self, records=None):
        if records is not None:
            self.write(records)
        return intentions.build(self.context)

    def report(self, result):
        return json.loads(Path(result["artifacts"][0]).read_text(encoding="utf-8"))

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not supported or authorized on this device")

    def assert_envelope(self, result, status):
        self.assertEqual(status, result["status"])
        self.assertEqual("intentions", result["scenario"])
        for field in ("title", "change", "impact", "action", "decision", "fingerprint", "reason"):
            self.assertIsInstance(result[field], str, field)
            self.assertTrue(result[field], field)
        self.assertIn(result["urgency"], {"routine", "time_sensitive", "urgent"})
        self.assertRegex(result["fingerprint"], r"^intentions:[a-f0-9]{64}$")
        for entry in result["evidence"]:
            self.assertEqual({"source", "observation"}, set(entry))
        for path in result["artifacts"]:
            self.assertTrue(Path(path).is_file())
            self.assertTrue(Path(path).is_relative_to(self.artifacts))

    def assert_notice_budget(self, result):
        fields = [result[key] for key in ("title", "change", "impact", "action", "decision", "reason")]
        for evidence in result["evidence"]:
            fields.extend((evidence["source"], evidence["observation"]))
        content = "\n".join(fields)
        self.assertTrue(content.isascii())
        self.assertLessEqual(len(content), 500)
        self.assertLessEqual(len(content.split()), 70)
        rendered = "\n".join((
            f"Title: {result['title']}",
            f"What changed: {result['change']}",
            f"Why it matters: {result['impact']}",
            f"What is ready: {result['action']}",
            f"What do you decide: {result['decision']}",
            f"Reason: {result['reason']}",
            *[f"Evidence [{item['source']}]: {item['observation']}" for item in result["evidence"]],
        ))
        self.assertLessEqual(len(rendered), 650)
        self.assertLessEqual(len(rendered.split()), 90)

    def test_ready_drafts_concrete_outline_with_original_context_citations(self):
        result = self.build([promise()])
        self.assert_envelope(result, "ready")
        self.assertEqual("urgent", result["urgency"])
        self.assertEqual("2026-09-20T12:00:00Z", result["deadline"])
        self.assertEqual(2, len(result["artifacts"]))
        draft = Path(result["artifacts"][1]).read_text(encoding="utf-8")
        for expected in ("Draft a release outline [1]", "### Audience [1]",
                         "### Verified changes [1]", "### Caveats [1]",
                         "I will prepare the release review.", "synthetic-project-journal",
                         intentions._markdown(f"{self.input}#/items/0"),
                         "not sent, enqueued, or executed"):
            self.assertIn(expected, draft)
        report = self.report(result)
        self.assertEqual(result, report["result"])
        self.assertEqual("release-outline", report["ranked"][0]["id"])
        self.assertEqual(45, report["ranked"][0]["score"])
        self.assertEqual([f"{self.input}#/items/0"], report["ranked"][0]["citations"])
        self.assert_notice_budget(result)
        self.assertEqual("artifacts[0]#/ranked/0", result["evidence"][0]["source"])
        self.assertIn(f"Recorded deadline: {result['deadline']}", result["evidence"][0]["observation"])
        self.assertTrue(any(row["source"] == f"{self.input}#/items/0"
                            for row in report["source_evidence"]))

    def test_long_three_item_review_keeps_full_detail_out_of_notification(self):
        items = []
        for number in range(3):
            item = promise(str(number) + "x" * 190, title="Synthetic detailed title " * 7)
            item["consequence"]["reason"] = "The synthetic review needs a carefully specified scope. " * 12
            item["next_step"]["action"] = "Draft the synthetic release outline with grounded details. " * 6
            item["original"]["context"] += " " + item["next_step"]["action"]
            items.append(item)
        result = self.build(items)
        self.assert_envelope(result, "ready")
        self.assert_notice_budget(result)
        self.assertEqual(1, len(result["evidence"]))
        report = self.report(result)
        self.assertEqual(3, len(report["ranked"]))
        self.assertEqual(4, len(report["source_evidence"]))
        self.assertEqual(items[0]["title"].strip(), report["ranked"][0]["title"])
        self.assertEqual(items[0]["consequence"]["reason"].strip(),
                         report["ranked"][0]["consequence"]["reason"])
        self.assertEqual(items[0]["next_step"]["action"].strip(),
                         report["ranked"][0]["next_step"]["action"])
        self.assertIn("see the cited review", result["impact"])
        self.assertIn("Prepared draft", result["action"])

    def test_notification_preserves_meaning_by_not_truncating_or_transliterating(self):
        item = promise(title="Synthetic review \u2264 target", deadline=None)
        item["consequence"]["reason"] = "The synthetic tolerance is \u2264 the agreed limit."
        item["next_step"]["action"] = "Draft a synthetic r\u00e9sum\u00e9 outline"
        item["original"]["context"] += " " + item["next_step"]["action"]
        result = self.build([item])
        self.assert_envelope(result, "ready")
        self.assert_notice_budget(result)
        self.assertEqual("Recover a recorded promise", result["title"])
        self.assertEqual(item["consequence"]["reason"], self.report(result)["ranked"][0]["consequence"]["reason"])
        self.assertEqual("routine", result["urgency"])
        self.assertNotIn("deadline", result)
        self.assertIn("Relevance checked:", result["evidence"][0]["observation"])

    def test_notification_limits_apply_to_short_character_heavy_word_counts(self):
        item = promise(title="a " * 20)
        item["consequence"]["reason"] = "a " * 30
        item["next_step"]["action"] = "a " * 35
        item["original"]["context"] += " " + item["next_step"]["action"]
        result = self.build([item])
        self.assert_envelope(result, "ready")
        self.assert_notice_budget(result)
        self.assertEqual("Recover a recorded promise", result["title"])
        self.assertIn("see the cited review", result["impact"])

    def test_notification_summarization_does_not_hide_semantic_fingerprint_changes(self):
        item = promise()
        item["consequence"]["reason"] = "Synthetic detailed consequence. " * 10
        first = self.build([item])
        item["consequence"]["reason"] += "Different material consequence."
        second = self.build([item])
        self.assertEqual(first["impact"], second["impact"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_notification_budget_at_field_limits_and_fractional_deadline(self):
        item = promise(title="X" * 43, deadline="2026-09-20T12:00:00.123456Z")
        item["consequence"]["reason"] = "X" * 90
        item["next_step"]["action"] = "X" * 72
        item["original"]["context"] += " " + item["next_step"]["action"]
        result = self.build([item])
        self.assert_envelope(result, "ready")
        self.assert_notice_budget(result)
        self.assertEqual(52, len(result["title"]))
        self.assertEqual(90, len(result["impact"]))
        self.assertEqual(85, len(result["action"]))
        self.assertIn(result["deadline"], result["evidence"][0]["observation"])

    def test_documented_context_and_snapshot_are_valid(self):
        documentation = (ROOT / "docs" / "scenarios" / "intentions.md").read_text(encoding="utf-8")
        config, snapshot = [json.loads(block) for block in
                            re.findall(r"```json\n(.*?)\n```", documentation, re.DOTALL)]
        self.context["sources"] = {"intentions": config}
        path = self.home / config["files"][0]
        path.parent.mkdir(mode=0o700)
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assert_envelope(self.build(), "ready")

    def test_all_supported_artifact_formats(self):
        for form, text in (("outline", "### Audience [1]"),
                           ("message", "Following up on my promise:"),
                           ("checklist", "- [ ] Audience [1]")):
            with self.subTest(form=form):
                item = promise()
                item["next_step"]["format"] = form
                result = self.build([item])
                self.assert_envelope(result, "ready")
                self.assertIn(text, Path(result["artifacts"][1]).read_text(encoding="utf-8"))

    def test_rank_top_three_by_consequence_relevance_and_deadline_not_input_order(self):
        items = []
        for identifier, level, deadline in (
            ("low-urgent", "low", "2026-09-19T19:00:00Z"),
            ("high-routine", "high", None),
            ("medium-urgent", "medium", "2026-09-19T20:00:00Z"),
            ("high-urgent", "high", "2026-09-19T21:00:00Z"),
            ("high-less-current", "high", None),
        ):
            item = promise(identifier, deadline=deadline)
            item["consequence"]["level"] = level
            if identifier == "high-less-current":
                item["relevance"]["checked_at"] = "2026-08-25T12:00:00Z"
            items.append(item)
        result = self.build(items)
        ranked = self.report(result)["ranked"]
        self.assertEqual(["high-urgent", "high-routine", "medium-urgent"],
                         [item["id"] for item in ranked])
        self.assertEqual([45, 36, 35], [item["score"] for item in ranked])
        self.assertEqual(5, self.report(result)["eligible_records"])
        reversed_result = self.build(list(reversed(items)))
        self.assertEqual(result["fingerprint"], reversed_result["fingerprint"])

    def test_ties_use_stable_ids(self):
        result = self.build([promise("b"), promise("a")])
        self.assertEqual(["a", "b"], [item["id"] for item in self.report(result)["ranked"]])

    def test_ties_use_actual_deadline_order_including_fractional_seconds(self):
        result = self.build([
            promise("a-fractional", deadline="2026-09-20T12:00:00.500000Z"),
            promise("z-whole", deadline="2026-09-20T12:00:00Z"),
        ])
        self.assertEqual(["z-whole", "a-fractional"],
                         [item["id"] for item in self.report(result)["ranked"]])

    def test_negation_tentative_ideas_reported_speech_and_questions_are_not_promises(self):
        quotes = [
            "I will not prepare the release review.",
            "I won't prepare the release review.",
            "I never promised to prepare the release review.",
            "I did not promise to prepare the release review.",
            "I don't think I will prepare the release review.",
            "I will maybe prepare the release review.",
            "I will prepare the release review if the project resumes.",
            "I will prepare the release review when the project resumes.",
            "I will prepare the release review (cancelled).",
            "I should prepare the release review.",
            "I would prepare the release review.",
            "I will consider preparing the release review.",
            "I will try to prepare the release review.",
            "I will need to prepare the release review.",
            "Someone said I will prepare the release review.",
            "The suggested fix is: I will prepare the release review.",
            "I will prepare the release review?",
        ]
        for quote in quotes:
            with self.subTest(quote=quote):
                item = promise()
                item["original"]["quote"] = quote
                result = self.build([item])
                self.assert_envelope(result, "suppressed")
                self.assertEqual([], self.report(result)["ranked"])
                self.assertEqual(1, len(result["artifacts"]))

    def test_explicit_affirmative_forms(self):
        for quote in ("I'll prepare the release review.",
                      "I’ll prepare the release review.",
                      "I promised to prepare the release review.",
                      "I commit to prepare the release review.",
                      "I agreed to prepare the release review."):
            with self.subTest(quote=quote):
                item = promise()
                item["original"]["quote"] = quote
                self.assertEqual("ready", self.build([item])["status"])

    def test_ideas_suggestions_repairs_and_operational_sources_excluded(self):
        for field, value in (("kind", "idea"), ("kind", "suggestion"), ("kind", "repair"),
                             ("original.kind", "alert"), ("original.kind", "repair")):
            with self.subTest(field=field, value=value):
                item = promise()
                if field == "kind":
                    item[field] = value
                else:
                    item["original"]["kind"] = value
                self.assertEqual("suppressed", self.build([item])["status"])

    def test_other_owned_or_agent_authored_excluded_even_with_first_person_promise(self):
        for field, value in (("owner_id", "teammate"), ("author_id", "teammate"),
                             ("author_id", "repair-agent")):
            with self.subTest(field=field):
                item = promise()
                if field == "owner_id":
                    item[field] = value
                else:
                    item["original"][field] = value
                result = self.build([item])
                self.assert_envelope(result, "suppressed")
                self.assertEqual({"not_owned_and_authored_by_user": 1},
                                 self.report(result)["excluded"])

    def test_completed_and_cancelled_states_excluded(self):
        for status in ("completed", "done", "cancelled", "canceled", "closed"):
            with self.subTest(status=status):
                result = self.build([promise(status=status)])
                self.assert_envelope(result, "suppressed")
                self.assertEqual({"completed_or_cancelled": 1}, self.report(result)["excluded"])

    def test_completion_and_cancellation_in_context_override_open_status(self):
        for text in ("Already done.", "The commitment has been cancelled.",
                     "I completed the release review.", "This task is closed.",
                     "No longer my responsibility.", "I have finished this.",
                     "It was cancelled.", "Status: done.",
                     "I won't do this after all.", "Done."):
            with self.subTest(text=text):
                item = promise()
                item["original"]["context"] += " " + text
                result = self.build([item])
                self.assertEqual("suppressed", result["status"])
                self.assertEqual({"resolved_in_original_context": 1}, self.report(result)["excluded"])

    def test_relevance_text_cannot_contradict_current_flag(self):
        item = promise()
        item["relevance"]["reason"] = "No longer relevant."
        self.assertEqual("suppressed", self.build([item])["status"])

    def test_terminal_copy_vetoes_open_copy_across_files(self):
        completed = promise(status="completed", updated_at="2026-09-18T12:00:00Z")
        self.write([promise()])
        second = self.write([completed], self.home / "completed.json")
        self.context["sources"]["intentions"]["files"].append(str(second))
        result = self.build()
        self.assert_envelope(result, "suppressed")
        self.assertEqual(2, self.report(result)["reviewed_records"])
        self.assertEqual({"completed_or_cancelled": 1}, self.report(result)["excluded"])

    def test_conflicting_ownership_or_content_copies_are_not_ranked(self):
        for changed in (promise(owner_id="teammate"), promise(title="Conflicting title")):
            with self.subTest(changed=changed["title"]):
                result = self.build([promise(), changed])
                self.assertEqual("suppressed", result["status"])
                self.assertEqual({"conflicting_copies": 1}, self.report(result)["excluded"])

    def test_identical_copies_are_deduplicated_and_both_cited(self):
        self.write([promise()])
        second = self.write([promise()], self.home / "second.json")
        baseline = self.build()
        self.context["sources"]["intentions"]["files"].extend([str(second), "promises.json"])
        result = self.build()
        self.assertEqual("ready", result["status"])
        self.assertEqual(1, len(self.report(result)["ranked"]))
        self.assertEqual(2, len(self.report(result)["ranked"][0]["citations"]))
        self.assertEqual(baseline["fingerprint"], result["fingerprint"])

    def test_recent_activity_stale_activity_and_uncertain_relevance_excluded(self):
        records = [
            promise("recent", updated_at="2026-09-18T12:00:00Z"),
            promise("old", created_at="2026-01-01T12:00:00Z", updated_at="2026-02-01T12:00:00Z"),
            promise("unknown"),
            promise("stale"),
            promise("unchecked", created_at="2026-07-01T12:00:00Z"),
        ]
        records[2]["relevance"]["state"] = "unknown"
        records[3]["relevance"]["state"] = "stale"
        records[4]["relevance"]["checked_at"] = "2026-08-01T12:00:00Z"
        result = self.build(records)
        self.assert_envelope(result, "suppressed")
        self.assertEqual({"recent_activity": 1, "stale_activity": 1,
                          "not_current": 2, "stale_relevance": 1}, self.report(result)["excluded"])

    def test_old_promise_with_actual_recent_activity_can_be_recovered(self):
        result = self.build([promise(created_at="2025-01-01T12:00:00Z")])
        self.assertEqual("ready", result["status"])

    def test_quiet_window_boundaries_and_configuration(self):
        now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        item = promise(deadline=None)
        item["updated_at"] = (now - timedelta(days=7)).isoformat()
        self.assertEqual("ready", self.build([item])["status"])
        item["updated_at"] = (now - timedelta(days=7) + timedelta(seconds=1)).isoformat()
        self.assertEqual("suppressed", self.build([item])["status"])
        self.context["sources"]["intentions"]["quiet_days"] = 3
        self.assertEqual("ready", self.build([item])["status"])
        self.context["sources"]["intentions"]["quiet_days"] = True
        self.assertEqual("blocked", self.build([item])["status"])

    def test_long_overdue_requires_reconfirmation_after_deadline(self):
        item = promise(deadline="2026-09-05T12:00:00Z")
        item["relevance"]["checked_at"] = "2026-09-04T12:00:00Z"
        result = self.build([item])
        self.assertEqual("suppressed", result["status"])
        item["relevance"]["checked_at"] = "2026-09-18T12:00:00Z"
        self.assertEqual("ready", self.build([item])["status"])

    def test_urgency_uses_only_recorded_deadline_and_consequence(self):
        for level, deadline, urgency in (
            ("high", None, "routine"),
            ("high", "2026-10-20T12:00:00Z", "routine"),
            ("high", "2026-09-23T12:00:00Z", "time_sensitive"),
            ("medium", "2026-09-19T19:00:00Z", "time_sensitive"),
            ("high", "2026-09-19T19:00:00Z", "urgent"),
        ):
            with self.subTest(level=level, deadline=deadline):
                item = promise(deadline=deadline)
                item["consequence"]["level"] = level
                result = self.build([item])
                self.assertEqual(urgency, result["urgency"])
                self.assertEqual(deadline, result.get("deadline"))

    def test_fingerprint_ignores_clock_snapshot_formatting_paths_and_excluded_records(self):
        item = promise()
        first = self.build([item])
        self.context["now"] = "2026-09-19T19:30:00Z"
        self.write([promise("excluded", status="done"), copy.deepcopy(item)], as_of=self.context["now"])
        self.input.write_text(json.dumps(json.loads(self.input.read_text()), indent=4, sort_keys=True),
                              encoding="utf-8")
        renamed = self.home / "renamed.json"
        self.input.rename(renamed)
        self.context["sources"]["intentions"]["files"] = [str(renamed)]
        later = self.build()
        self.assertEqual(first["fingerprint"], later["fingerprint"])
        self.context["artifact_dir"] = str(self.workspace / "other-artifacts")
        moved = self.build()
        self.assertEqual(first["fingerprint"], moved["fingerprint"])

    def test_semantic_changes_and_deadline_urgency_boundary_change_fingerprint(self):
        item = promise(deadline="2026-09-21T19:00:00Z")
        first = self.build([item])
        item["consequence"]["reason"] = "A different verified consequence."
        changed = self.build([item])
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
        self.context["now"] = "2026-09-21T18:30:00Z"
        boundary = self.build([item])
        self.assertEqual("urgent", boundary["urgency"])
        self.assertNotEqual(changed["fingerprint"], boundary["fingerprint"])

    def test_timezone_equivalent_dates_and_whitespace_have_stable_fingerprints(self):
        item = promise()
        first = self.build([item])
        item["deadline"] = "2026-09-20T08:00:00-04:00"
        item["original"]["quote"] = "I will  prepare the release review."
        second = self.build([item])
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_missing_configuration_does_not_discover_or_read_other_files(self):
        self.write([promise()])
        self.context["sources"] = {}
        with mock.patch.object(intentions, "_read_source", side_effect=AssertionError("must not scan")):
            result = self.build()
        self.assert_envelope(result, "blocked")
        self.assertEqual([], self.report(result)["ranked"])
        self.assertIn("owner_id", result["evidence"][-1]["observation"])

    def test_home_defaults_belong_to_adapter_not_module(self):
        self.write([promise()])
        with mock.patch.object(Path, "home", side_effect=AssertionError("must use supplied context")):
            self.assert_envelope(self.build(), "ready")
            missing_home = {key: value for key, value in self.context.items() if key != "home"}
            self.assertEqual("blocked", intentions.build(missing_home)["status"])

    def test_explicit_source_outside_home_overrides_without_discovery(self):
        explicit = self.write([promise()], self.workspace / "authorized-export.json")
        self.context["sources"]["intentions"]["files"] = [str(explicit)]
        self.context["sources"]["unrelated"] = {"files": ["must-not-read.json"]}
        with mock.patch.object(intentions, "_read_source", wraps=intentions._read_source) as reader:
            self.assert_envelope(self.build(), "ready")
        reader.assert_called_once_with(explicit)
        self.assertFalse(self.input.exists())

    def test_missing_unreadable_or_non_json_source_blocks(self):
        self.assert_envelope(self.build(), "blocked")
        self.input.write_text("# I will fix the alert\n", encoding="utf-8")
        self.assert_envelope(self.build(), "blocked")
        with mock.patch.object(intentions, "_read_source", side_effect=PermissionError("private")):
            self.assert_envelope(self.build(), "blocked")

    def test_real_operational_issue_and_repair_shapes_are_not_promises(self):
        for raw in (
            {"watcher_check": {"attempts": 2, "last_result": "NO_ACTION; repair suggested"}},
            {"note": {"one_change": "Repair the watcher"}, "apply": False},
        ):
            with self.subTest(shape=raw):
                self.input.write_text(json.dumps(raw), encoding="utf-8")
                self.assert_envelope(self.build(), "blocked")

    def test_one_bad_configured_source_blocks_partial_recovery(self):
        self.write([promise()])
        self.context["sources"]["intentions"]["files"].append("missing-completions.json")
        result = self.build()
        self.assert_envelope(result, "blocked")
        self.assertEqual([], self.report(result)["ranked"])
        self.assertEqual(1, len(result["artifacts"]))

    def test_empty_valid_snapshot_suppresses(self):
        result = self.build([])
        self.assert_envelope(result, "suppressed")
        self.assertEqual(0, self.report(result)["reviewed_records"])

    def test_missing_record_fields_and_unknown_fields_block(self):
        for field in ("owner_id", "original", "status", "updated_at", "relevance", "next_step"):
            with self.subTest(field=field):
                item = promise()
                del item[field]
                self.assertEqual("blocked", self.build([item])["status"])
        item = promise(completed_at="2026-09-18T12:00:00Z")
        self.assertEqual("blocked", self.build([item])["status"])
        self.assertEqual("blocked", self.build([promise("ambiguous id")])["status"])

    def test_stale_future_and_timezone_missing_dates_block(self):
        for as_of in ("2026-08-01T12:00:00Z", "2026-09-20T12:00:00Z", "2026-09-19"):
            with self.subTest(as_of=as_of):
                self.write([promise()], as_of=as_of)
                self.assertEqual("blocked", self.build()["status"])
        for field, value in (("created_at", "2026-09-20T12:00:00Z"),
                             ("updated_at", "2026-09-20T12:00:00Z"),
                             ("updated_at", "2026-09-01T12:00:00"),
                             ("deadline", "2026-01-01T00:00:00Z")):
            with self.subTest(field=field):
                self.assertEqual("blocked", self.build([promise(**{field: value})])["status"])

    def test_invalid_context_always_returns_blocked_envelope(self):
        for context in (None, [], {}, {**self.context, "now": "yesterday"},
                        {**self.context, "home": "relative/home"}):
            with self.subTest(context=context):
                result = intentions.build(context)
                self.assertEqual("blocked", result["status"])
                self.assertEqual("intentions", result["scenario"])
                self.assertEqual([], result["artifacts"])

    def test_draft_content_must_be_grounded_in_original_context(self):
        for field in ("action", "points"):
            with self.subTest(field=field):
                item = promise()
                item["next_step"][field] = "Invented work" if field == "action" else ["Invented work"]
                result = self.build([item])
                self.assert_envelope(result, "blocked")
                self.assertIn("must occur", result["evidence"][-1]["observation"])
                self.assertEqual(1, len(result["artifacts"]))

    def test_duplicate_json_keys_and_nonfinite_numbers_block(self):
        for raw in ('{"schema":"intentions/v1","schema":"other","as_of":"' + NOW + '","items":[]}',
                    '{"schema":"intentions/v1","as_of":"' + NOW + '","items":NaN}'):
            self.input.write_text(raw, encoding="utf-8")
            self.assertEqual("blocked", self.build()["status"])

    def test_bounded_sources_and_records(self):
        self.input.write_bytes(b" " * (intentions.MAX_FILE_BYTES + 1))
        self.assertEqual("blocked", self.build()["status"])
        self.write([])
        self.context["sources"]["intentions"]["files"] *= intentions.MAX_FILES + 1
        self.assertEqual("blocked", self.build()["status"])
        self.context["sources"]["intentions"]["files"] = ["promises.json"]
        with mock.patch.object(intentions, "MAX_RECORDS", 1):
            self.assertEqual("blocked", self.build([promise("one"), promise("two")])["status"])

    def test_source_directories_urls_and_relative_escape_block(self):
        self.write([promise()])
        for path in (str(self.home), "https://example.invalid/notes.json", "../outside.json"):
            with self.subTest(path=path):
                self.context["sources"]["intentions"]["files"] = [path]
                self.assertEqual("blocked", self.build()["status"])

    def test_source_symlinks_and_ancestor_symlinks_block(self):
        real = self.write([promise()])
        alias = self.home / "alias.json"
        self.symlink(alias, real)
        directory_alias = self.home / "directory-alias"
        self.symlink(directory_alias, self.home, directory=True)
        for path in ("alias.json", "directory-alias/promises.json"):
            with self.subTest(path=path):
                self.context["sources"]["intentions"]["files"] = [path]
                self.assertEqual("blocked", self.build()["status"])

    def test_filesystem_reparse_points_are_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with mock.patch.object(Path, "lstat", return_value=info), \
                mock.patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
            self.assertTrue(intentions._has_symlink(self.input))

    def test_nonregular_sources_rejected_before_opening(self):
        info = SimpleNamespace(st_mode=stat.S_IFCHR, st_size=0)
        with mock.patch.object(Path, "lstat", return_value=info), \
                mock.patch.object(os, "open", side_effect=AssertionError("must not open a device")):
            with self.assertRaises(intentions.InvalidInput):
                intentions._read_source(self.input)

    def test_artifacts_remain_in_context_and_source_is_unchanged(self):
        self.write([promise()])
        before = self.input.read_bytes()
        self.artifacts.mkdir(mode=0o755)
        result = self.build()
        self.assert_envelope(result, "ready")
        self.assertEqual(before, self.input.read_bytes())
        for path in result["artifacts"]:
            self.assertNotIn(b"\r\n", Path(path).read_bytes())
        self.assertEqual([], list(self.artifacts.glob("*.partial")))

    @unittest.skipUnless(os.name == "posix", "POSIX modes do not validate Windows ACLs")
    def test_posix_artifact_permissions_are_private(self):
        self.artifacts.mkdir(mode=0o755)
        result = self.build([promise()])
        self.assert_envelope(result, "ready")
        self.assertEqual(0o700, stat.S_IMODE(self.artifacts.stat().st_mode))
        for path in result["artifacts"]:
            self.assertEqual(0o600, stat.S_IMODE(Path(path).stat().st_mode))
        self.assertEqual([], list(self.artifacts.glob("*.partial")))

    def test_draft_escapes_source_markup_but_report_preserves_original(self):
        item = promise()
        markup = "![external image](https://example.invalid/track) <img src='https://example.invalid/track'>"
        item["original"]["context"] += " " + markup
        item["next_step"]["points"].append(markup)
        result = self.build([item])
        self.assertEqual("ready", result["status"])
        draft = Path(result["artifacts"][1]).read_text(encoding="utf-8")
        self.assertNotIn("![external image]", draft)
        self.assertNotIn("<img", draft)
        self.assertIn("&lt;img", draft)
        self.assertIn(markup, self.report(result)["ranked"][0]["original"]["context"])

    def test_atomic_output_replaces_file_symlinks_without_touching_target(self):
        result = self.build([promise()])
        outside = self.workspace / "untouched.txt"
        outside.write_text("unchanged", encoding="utf-8")
        draft = Path(result["artifacts"][1])
        draft.unlink()
        self.symlink(draft, outside)
        self.assertEqual("ready", self.build()["status"])
        self.assertEqual("unchanged", outside.read_text(encoding="utf-8"))
        self.assertFalse(draft.is_symlink())

    def test_artifact_symlink_destination_refused(self):
        target = self.workspace / "target"
        target.mkdir()
        self.symlink(self.artifacts, target, directory=True)
        result = self.build([promise()])
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], list(target.iterdir()))

    def test_artifact_home_destination_refused(self):
        self.context["artifact_dir"] = str(self.home)
        self.assertEqual("blocked", self.build([promise()])["status"])

    def test_artifact_ancestor_symlinks_refused(self):
        target = self.workspace / "target"
        target.mkdir()
        alias = self.workspace / "alias"
        self.symlink(alias, target, directory=True)
        self.context["artifact_dir"] = str(alias / "artifacts")
        self.assertEqual("blocked", self.build([promise()])["status"])
        self.assertEqual([], list(target.iterdir()))

    def test_artifact_home_ancestors_refused_before_any_permission_change(self):
        self.context["artifact_dir"] = str(self.workspace)
        with mock.patch.object(Path, "chmod", side_effect=AssertionError("must not chmod home ancestor")):
            self.assertEqual("blocked", self.build([promise()])["status"])

    def test_artifact_write_failure_is_not_ready(self):
        with mock.patch.object(intentions, "_write_private", side_effect=OSError("no space")):
            result = self.build([promise()])
        self.assertEqual("blocked", result["status"])
        self.assertEqual([], result["artifacts"])
        self.assertIn("output is unavailable", result["reason"])
        self.assertFalse(any(row["source"].startswith("artifacts[0]") for row in result["evidence"]))
        self.assertTrue(any(row["source"] == f"{self.input}#/items/0" for row in result["evidence"]))

    def test_no_network_process_or_outbox_effects(self):
        self.write([promise()])
        with mock.patch("socket.socket", side_effect=AssertionError("network")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process")):
            self.assertEqual("ready", self.build()["status"])
        self.assertFalse((self.home / "state").exists())
        self.assertEqual(["promises.json"], sorted(path.name for path in self.home.iterdir()))


if __name__ == "__main__":
    unittest.main()
