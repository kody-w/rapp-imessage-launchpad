import copy
import importlib
import importlib.util
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scenarios import meeting


try:
    ZoneInfo("America/New_York")
    HAS_NAMED_ZONE = True
except ZoneInfoNotFoundError:
    HAS_NAMED_ZONE = False


class MeetingScenarioTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(".meeting-test-fixtures")
        self.root = self.base / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.now = "2026-09-19T18:00:00Z"
        self.calendar = {
            "schema": "meeting-calendar/v1",
            "fetched_at": "2026-09-19T17:45:00Z",
            "events": [{
                "id": "synthetic-review-1",
                "title": "Synthetic release review",
                "start": "2026-09-19T15:00:00-04:00",
                "end": "2026-09-19T15:30:00-04:00",
                "agenda": "We could ask the release lead to approve a pilot.",
            }],
        }
        self.material = {
            "schema": "meeting-material/v1",
            "meeting_id": "synthetic-review-1",
            "updated_at": "2026-09-19T16:00:00Z",
            "facts": [
                "The synthetic pilot is currently restricted to staging.",
                "The synthetic rollback drill has not yet been recorded as passing.",
            ],
            "decision": {
                "topic": "pilot scope",
                "options": ["keep the pilot in staging", "approve a limited production pilot"],
                "criterion": "a demonstrated rollback path",
                "outcome": "Record a release scope that does not bypass rollback evidence",
            },
            "commitments": [{
                "owner": "Fixture release lead",
                "commitment": "publish the rollback checklist",
                "quote": "I will publish the rollback checklist.",
                "due": "2026-09-19T18:45:00Z",
            }],
            "discussion": ["A production pilot was proposed, not approved."],
        }
        self.context = {
            "home": str(self.root.resolve()),
            "artifact_dir": "artifacts",
            "now": self.now,
            "sources": {"meeting_calendar": "calendar.json",
                        "meeting_materials": ["material.json"]},
        }
        self.save()

    @classmethod
    def tearDownClass(cls):
        base = Path(".meeting-test-fixtures")
        if base.is_dir() and not any(base.iterdir()):
            base.rmdir()

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data) if isinstance(data, dict) else data, encoding="utf-8")
        return path

    def save(self):
        self.write("calendar.json", self.calendar)
        self.write("material.json", self.material)

    def symlink(self, link, target):
        try:
            link.symlink_to(target)
        except (NotImplementedError, OSError):
            self.skipTest("This environment does not permit creating symbolic links.")

    def build(self):
        result = meeting.build(self.context)
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        self.assertEqual(result["scenario"], "meeting")
        self.assertEqual(set(result) - {"deadline"}, {
            "scenario", "status", "title", "change", "impact", "action", "decision",
            "evidence", "artifacts", "fingerprint", "urgency", "reason",
        })
        for key in ("title", "change", "impact", "action", "decision", "fingerprint",
                    "urgency", "reason"):
            self.assertIsInstance(result[key], str)
        for item in result["evidence"]:
            self.assertEqual(set(item), {"source", "observation"})
        if result["status"] == "ready":
            text = self.notification_text(result)
            self.assertTrue(text.isascii(), text)
            self.assertLessEqual(len(text), 650, text)
            self.assertLessEqual(len(text.split()), 90, text)
        return result

    @staticmethod
    def notification_text(result):
        lines = [result["title"]]
        for label, field in (("Changed", "change"), ("Impact", "impact"),
                             ("Action", "action"), ("Need", "decision")):
            lines.append(label + ": " + result[field])
        lines.append("Evidence: " + "; ".join(
            item["source"] + ": " + item["observation"] for item in result["evidence"]))
        lines.append("Deadline: " + result["deadline"])
        return "\n".join(lines)

    def assert_blocked(self, reason=None):
        result = self.build()
        self.assertEqual(result["status"], "blocked", result)
        if reason:
            self.assertEqual(result["reason"], reason)
        self.assertEqual(result["artifacts"], [])
        self.assertFalse((self.root / "artifacts").exists())
        return result

    def use_ics(self, event_lines=None, calendar_lines=None):
        if event_lines is None:
            event_lines = [
                "UID:synthetic-review-1", "SUMMARY:Synthetic release review",
                "DTSTART:20260919T190000Z",
                "DTEND:20260919T193000Z",
                "DESCRIPTION:A production pilot was discussed\\, not approved.\\n",
                " It remains a proposal.",
            ]
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Synthetic fixture//EN"]
        lines.extend(calendar_lines or [])
        lines.extend(["BEGIN:VEVENT", *event_lines, "END:VEVENT", "END:VCALENDAR", ""])
        self.write("calendar.ics", "\r\n".join(lines))
        self.context["sources"]["meeting_calendar"] = "calendar.ics"
        self.context["sources"]["meeting_calendar_fetched_at"] = self.calendar["fetched_at"]

    def test_upcoming_build_persists_three_facts_question_and_actual_draft(self):
        with mock.patch("socket.socket", side_effect=AssertionError("no network")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            result = self.build()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["deadline"], "2026-09-19T19:00:00Z")
        self.assertEqual(result["urgency"], "urgent")
        self.assertEqual(len(result["artifacts"]), 1)
        artifact = Path(result["artifacts"][0])
        self.assertEqual(artifact.parent, (self.root / "artifacts").resolve())
        if os.name == "posix":
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        text = artifact.read_text(encoding="utf-8")
        facts = text.split("## Three source-backed facts\n")[1].split("\n## ")[0]
        self.assertEqual(sum(line.startswith(("1. ", "2. ", "3. ")) for line in facts.splitlines()), 3)
        self.assertIn("#/events/0", text)
        self.assertIn("#/facts/0", text)
        self.assertIn("#/facts/1", text)
        self.assertIn("#/decision", text)
        self.assertIn("## Prepared decision memo", text)
        self.assertIn(self.material["decision"]["outcome"], text)
        self.assertIn("no option selected", text)
        self.assertEqual(result["decision"].count("?"), 1)
        self.assertIn("a demonstrated rollback path", result["decision"])
        self.assertIn("unsent local draft", result["action"])
        self.assertIn(result["deadline"], result["evidence"][0]["observation"])
        self.assertIn("## Notification source references", text)
        self.assertNotIn(str(self.root.resolve()), self.notification_text(result))

    def test_maximum_notification_fields_and_fractional_deadline_fit_wire_budget(self):
        self.calendar["events"][0].update(title="T" * 39,
                                           start="2026-09-19T19:00:00.123456Z",
                                           end="2026-09-19T19:30:00.123456Z")
        self.material["facts"][1] = "F" * 72
        self.material["decision"].update(topic="Q" * 24, criterion="C" * 55,
                                          outcome="O" * 70)
        self.save()
        result = self.build()
        self.assertEqual(len(result["title"]), 48)
        self.assertEqual(len(result["impact"]), 76)
        self.assertEqual(len(result["decision"]), 100)
        self.assertEqual(len(result["deadline"]), 27)
        self.assertEqual(len(result["evidence"]), 2)

    def test_long_unicode_material_stays_complete_in_artifact_not_notification(self):
        self.calendar["events"][0]["title"] = "架空" * 100
        self.material["facts"] = ["Synthetic first " + "a" * 950,
                                  "Synthetic second " + "b" * 950]
        self.material["decision"] = {
            "topic": "t" * 500, "criterion": "c" * 500, "outcome": "架" * 500,
            "options": ["option-a" + "x" * 292, "option-b" + "y" * 292],
        }
        self.material["commitments"] *= 16
        self.material["discussion"] = ["Synthetic discussion " + "d" * 950] * 16
        self.save()
        result = self.build()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(
            result["decision"],
            "Which of the two documented options meets the acceptance criterion?")
        self.assertIn("unsent local draft", result["action"])
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn(self.calendar["events"][0]["title"], text)
        for fact in self.material["facts"]:
            self.assertIn(fact, text)
        for value in self.material["decision"]["options"]:
            self.assertIn(value, text)
        self.assertIn(self.material["decision"]["outcome"], text)
        self.assertIn(self.material["decision"]["criterion"], text)
        self.assertIn(self.material["discussion"][-1], text)
        self.assertIn("#/commitments/15", text)
        self.assertIn("#/discussion/15", text)

    def test_notification_does_not_truncate_a_long_fact_or_lose_its_caveat(self):
        self.material["facts"][1] = (
            "Synthetic release approval was discussed with several prerequisites, "
            "but it was explicitly NOT granted.")
        self.save()
        result = self.build()
        self.assertEqual(result["evidence"][1]["source"], "project#1")
        self.assertEqual(result["evidence"][1]["observation"], self.material["facts"][0])
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn(self.material["facts"][1], text)

    def test_tentative_calendar_keeps_qualification_without_elevated_urgency(self):
        self.calendar["events"][0]["status"] = "tentative"
        self.save()
        result = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["urgency"], "routine")
        self.assertTrue(result["evidence"][0]["observation"].startswith("Tentative meeting"))

    def test_invalid_unicode_and_control_text_are_blocked_before_drafting(self):
        for title in ["Synthetic \ud800 invalid", "Synthetic \x7f invalid"]:
            with self.subTest(title=repr(title)):
                self.calendar["events"][0]["title"] = title
                self.save()
                self.assert_blocked("invalid_source")

    def test_ready_through_actual_shared_gate_when_available(self):
        if importlib.util.find_spec("scenarios.interrupt") is None:
            self.skipTest("Shared gate is tested when integrated or on PYTHONPATH.")
        gate = importlib.import_module("scenarios.interrupt")
        proposal = self.build()
        rendered = gate.render(proposal)
        self.assertEqual(rendered, self.notification_text(proposal))
        admitted = gate.evaluate(proposal, [], self.now, {"quiet_hours": False})
        self.assertTrue(admitted["allow"], admitted)
        quiet = {"quiet_hours": {"start": "17:00", "end": "20:00"}}
        self.assertTrue(gate.evaluate(proposal, [], self.now, quiet)["allow"])
        history = [{"at": self.now, "decision": "queued", "scenario": "meeting",
                    "fingerprint": proposal["fingerprint"], "proposal": proposal}]
        again = gate.evaluate(proposal, history, "2026-09-19T18:05:00Z",
                              {"quiet_hours": False})
        self.assertFalse(again["allow"], again)
        self.assertTrue(again["reason"].startswith("duplicate:"), again)
        self.assertTrue(gate.evaluate(proposal, [{"decision": "suppressed"}],
                                      self.now, {"quiet_hours": False})["allow"])

    def test_explicit_commitments_are_separate_from_discussion_and_agenda(self):
        result = self.build()
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        committed = text.split("## Recorded explicit commitments")[1].split("## Discussion")[0]
        discussed = text.split("## Discussion")[1]
        self.assertIn("I will publish the rollback checklist.", committed)
        self.assertIn("#/commitments/0", committed)
        self.assertNotIn("production pilot was proposed", committed)
        self.assertIn("production pilot was proposed", discussed)
        self.assertIn("We could ask the release lead", discussed)
        self.assertIn("not commitments", discussed)
        self.assertIn("No attendee contact", text)

    def test_no_commitments_are_inferred(self):
        del self.material["commitments"]
        self.save()
        result = self.build()
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("None recorded. Discussion is not a commitment.", text)
        self.assertFalse(any("explicit commitment:" in e["observation"] for e in result["evidence"]))

    def test_fingerprint_and_artifact_do_not_move_with_now_or_cache_refresh(self):
        first = self.build()
        first_text = Path(first["artifacts"][0]).read_text(encoding="utf-8")
        self.context["now"] = "2026-09-19T18:10:00Z"
        self.calendar["fetched_at"] = "2026-09-19T18:09:00Z"
        self.material["updated_at"] = "2026-09-19T18:08:00Z"
        self.save()
        second = self.build()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(first_text, Path(second["artifacts"][0]).read_text(encoding="utf-8"))
        self.assertNotIn(self.context["now"], first_text)
        self.assertEqual(len(list((self.root / "artifacts").iterdir())), 1)

    def test_semantic_change_changes_fingerprint(self):
        first = self.build()
        self.material["facts"][1] = "The synthetic rollback drill now passes."
        self.save()
        second = self.build()
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["status"], "ready")

    def test_artifact_directory_is_not_fingerprint_input(self):
        first = self.build()
        self.context["artifact_dir"] = "other-artifacts"
        second = self.build()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["artifacts"], second["artifacts"])

    def test_context_home_and_explicit_source_overrides_do_not_use_account_home(self):
        app_home = self.root / ".storykeeper" / "home"
        app_home.mkdir(parents=True)
        self.context.update(home=str(app_home.resolve()),
                            artifact_dir=str((self.root / "private-output").resolve()))
        self.context["sources"].update(
            meeting_calendar=str((self.root / "calendar.json").resolve()),
            meeting_materials=[str((self.root / "material.json").resolve())])
        with mock.patch.object(Path, "home", side_effect=AssertionError("no account lookup")):
            result = self.build()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(Path(result["artifacts"][0]).parent,
                         (self.root / "private-output").resolve())
        self.assertEqual(list(app_home.iterdir()), [])

    def test_default_launchpad_context_never_discovers_sources_or_creates_output(self):
        app_home = self.root / ".storykeeper" / "home"
        app_home.mkdir(parents=True)
        self.context.update(home=str(app_home.resolve()), sources={})
        with mock.patch.object(Path, "home", side_effect=AssertionError("no account lookup")), \
                mock.patch.object(meeting, "_read", side_effect=AssertionError("no implicit reads")):
            result = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "missing_calendar")
        self.assertEqual(list(app_home.iterdir()), [])

    def test_relocation_to_another_device_preserves_semantic_fingerprint(self):
        first = self.build()
        relocated = self.root / "another-device"
        relocated.mkdir()
        for name in ("calendar.json", "material.json"):
            shutil.copyfile(self.root / name, relocated / name)
        self.context["home"] = str(relocated.resolve())
        second = self.build()
        self.assertEqual(second["status"], "ready")
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["artifacts"], second["artifacts"])
        self.assertIn(str(relocated.resolve()),
                      Path(second["artifacts"][0]).read_text(encoding="utf-8"))

    def test_optional_platform_open_flags_are_not_required(self):
        names = ("O_RDONLY", "O_WRONLY", "O_CREAT", "O_EXCL",
                 "open", "fdopen", "fstat", "fsync", "replace")
        portable_os = SimpleNamespace(**{name: getattr(os, name) for name in names})
        with mock.patch.object(meeting, "os", portable_os):
            self.assertEqual(self.build()["status"], "ready")

    def test_offset_and_utc_sources_work_without_an_iana_database(self):
        with mock.patch.object(meeting, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assertEqual(self.build()["status"], "ready")
            self.use_ics()
            self.assertEqual(self.build()["status"], "ready")
            self.use_ics(["UID:synthetic-review-1", "SUMMARY:Synthetic review",
                          "DTSTART;TZID=America/New_York:20260919T150000",
                          "DTEND;TZID=America/New_York:20260919T153000"])
            blocked = self.build()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "invalid_source")
        self.assertIn("installed IANA timezone", blocked["action"])

    def test_equivalent_time_offsets_and_reordered_calendar_do_not_change_fingerprint(self):
        first = self.build()
        self.calendar["events"][0].update(start="2026-09-19T19:00:00Z",
                                           end="2026-09-19T19:30:00Z")
        self.calendar["events"].insert(0, dict(
            self.calendar["events"][0], id="cancelled", status="cancelled"))
        self.save()
        second = self.build()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertIn("#/events/1", Path(second["artifacts"][0]).read_text(encoding="utf-8"))

    def test_past_ongoing_cancelled_and_outside_window_are_suppressed(self):
        event = copy.deepcopy(self.calendar["events"][0])
        cases = [
            {"start": "2026-09-18T19:00:00Z", "end": "2026-09-18T19:30:00Z"},
            {"start": self.now, "end": "2026-09-19T19:30:00Z"},
            {"start": "2026-09-19T17:50:00Z", "end": "2026-09-19T18:30:00Z"},
            {"start": "2026-09-21T19:00:00Z", "end": "2026-09-21T19:30:00Z"},
            {"status": "cancelled"},
        ]
        for update in cases:
            with self.subTest(update=update):
                self.calendar["events"] = [dict(event, **update)]
                self.save()
                result = self.build()
                self.assertEqual(result["status"], "suppressed", result)
                self.assertEqual(result["artifacts"], [])
        self.assertFalse((self.root / "artifacts").exists())

    def test_empty_fresh_calendar_is_suppressed_not_missing(self):
        self.calendar["events"] = []
        self.save()
        self.assertEqual(self.build()["status"], "suppressed")

    def test_old_transcripts_never_create_upcoming_meetings(self):
        self.calendar["events"][0]["start"] = "2025-09-19T19:00:00Z"
        self.calendar["events"][0]["end"] = "2025-09-19T19:30:00Z"
        self.material["discussion"] = ["Next meeting tomorrow; all attendees will join."]
        self.save()
        self.assertEqual(self.build()["status"], "suppressed")
        self.context["sources"].pop("meeting_calendar")
        self.assert_blocked("missing_calendar")

    def test_earliest_active_meeting_selected_independent_of_input_order(self):
        later = dict(self.calendar["events"][0], id="another-meeting",
                     start="2026-09-20T01:00:00Z", end="2026-09-20T02:00:00Z")
        cancelled = dict(later, id="cancelled", status="cancelled",
                         start="2026-09-19T18:15:00Z", end="2026-09-19T18:45:00Z")
        self.calendar["events"] = [later, cancelled, self.calendar["events"][0]]
        self.save()
        self.assertEqual(self.build()["deadline"], "2026-09-19T19:00:00Z")

    def test_timezones_use_instants_not_lexicographic_or_machine_local_time(self):
        cases = [
            ("2026-09-20T03:00:00+09:00", "2026-09-20T03:30:00+09:00", "suppressed"),
            ("2026-09-20T04:00:00+09:00", "2026-09-20T04:30:00+09:00", "ready"),
            ("2026-09-19T11:30:00-07:00", "2026-09-19T12:00:00-07:00", "ready"),
        ]
        for start, end, status in cases:
            with self.subTest(start=start):
                self.calendar["events"][0].update(start=start, end=end)
                self.save()
                self.assertEqual(self.build()["status"], status)

    @unittest.skipUnless(HAS_NAMED_ZONE, "IANA timezone database is unavailable")
    def test_json_explicit_zone_resolves_unambiguous_local_time(self):
        self.calendar["events"][0].update(start="2026-09-19T15:00:00",
                                           end="2026-09-19T15:30:00",
                                           timezone="America/New_York")
        self.save()
        self.assertEqual(self.build()["deadline"], "2026-09-19T19:00:00Z")

    @unittest.skipUnless(HAS_NAMED_ZONE, "IANA timezone database is unavailable")
    def test_dst_gap_and_fold_local_times_fail_closed(self):
        for start, end in [
            ("2026-03-08T02:15:00", "2026-03-08T03:30:00"),
            ("2026-11-01T01:15:00", "2026-11-01T02:00:00"),
        ]:
            with self.subTest(start=start):
                self.calendar["events"][0].update(start=start, end=end,
                                                   timezone="America/New_York")
                self.save()
                self.assert_blocked("invalid_source")

    @unittest.skipUnless(HAS_NAMED_ZONE, "IANA timezone database is unavailable")
    def test_dst_fold_with_explicit_offsets_and_utc_order_is_valid(self):
        self.context["now"] = "2026-11-01T04:30:00Z"
        self.calendar["fetched_at"] = "2026-11-01T04:29:00Z"
        self.material["updated_at"] = "2026-11-01T04:00:00Z"
        self.calendar["events"][0].update(start="2026-11-01T01:45:00-04:00",
                                           end="2026-11-01T01:15:00-05:00",
                                           timezone="America/New_York")
        self.save()
        result = self.build()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["deadline"], "2026-11-01T05:45:00Z")

    def test_naive_time_unknown_timezone_and_bad_end_are_blocked(self):
        event = copy.deepcopy(self.calendar["events"][0])
        for update in [
            {"start": "2026-09-19T19:00:00"},
            {"timezone": "Not/A_Zone"},
            {"start": "2026-09-19"},
            {"end": "2026-09-19T18:00:00Z"},
        ]:
            with self.subTest(update=update):
                self.calendar["events"] = [dict(event, **update)]
                self.save()
                self.assert_blocked("invalid_source")

    def test_freshness_uses_snapshot_timestamp_not_file_mtime(self):
        self.calendar["fetched_at"] = "2026-09-17T17:45:00Z"
        self.save()
        os.utime(self.root / "calendar.json", None)
        self.assert_blocked("stale_calendar")

    def test_future_calendar_and_stale_or_future_material_are_blocked(self):
        for kind, value, reason in [
            ("calendar", "2026-09-19T19:00:00Z", "future_calendar"),
            ("material", "2026-08-01T12:00:00Z", "stale_material"),
            ("material", "2026-09-19T19:00:00Z", "future_material"),
        ]:
            with self.subTest(reason=reason):
                self.calendar["fetched_at"] = "2026-09-19T17:45:00Z"
                self.material["updated_at"] = "2026-09-19T16:00:00Z"
                if kind == "calendar":
                    self.calendar["fetched_at"] = value
                else:
                    self.material["updated_at"] = value
                self.save()
                self.assert_blocked(reason)

    def test_freshness_and_lookahead_controls(self):
        self.context["sources"]["meeting_lookahead_hours"] = 0.5
        self.assertEqual(self.build()["status"], "suppressed")
        self.context["sources"]["meeting_lookahead_hours"] = 1
        self.context["sources"]["meeting_calendar_max_age_hours"] = 0.1
        self.assert_blocked("stale_calendar")
        self.context["sources"]["meeting_calendar_max_age_hours"] = 1
        self.context["sources"]["meeting_material_max_age_hours"] = 1
        self.assert_blocked("stale_material")
        self.context["sources"]["meeting_material_max_age_hours"] = 3
        self.assertEqual(self.build()["status"], "ready")

    def test_invalid_controls_are_blocked(self):
        for key, value in [
            ("meeting_lookahead_hours", 0), ("meeting_lookahead_hours", 169),
            ("meeting_calendar_max_age_hours", 73),
            ("meeting_material_max_age_hours", 2161),
            ("meeting_calendar_max_age_hours", True),
            ("meeting_lookahead_hours", float("nan")),
            ("meeting_lookahead_hours", 10 ** 1000),
            ("meeting_lookahead_hours", "24"),
        ]:
            with self.subTest(key=key, value=value):
                self.context["sources"] = {"meeting_calendar": "calendar.json", key: value}
                self.assert_blocked("invalid_context")

    def test_urgency_changes_do_not_change_semantic_fingerprint(self):
        self.calendar["events"][0].update(start="2026-09-19T23:00:00Z",
                                           end="2026-09-19T23:30:00Z")
        self.save()
        fingerprints = []
        for now, urgency in [("18:00", "routine"), ("20:00", "time_sensitive"),
                             ("22:00", "urgent")]:
            self.context["now"] = "2026-09-19T" + now + ":00Z"
            result = self.build()
            self.assertEqual(result["urgency"], urgency)
            fingerprints.append(result["fingerprint"])
        self.assertEqual(len(set(fingerprints)), 1)

    def test_missing_calendar_and_missing_file_are_distinct(self):
        self.context["sources"] = {}
        self.assert_blocked("missing_calendar")
        self.context["sources"]["meeting_calendar"] = "absent.json"
        self.assert_blocked("missing_source")

    def test_missing_unmatched_and_conflicting_material_are_blocked(self):
        self.context["sources"]["meeting_materials"] = []
        self.assert_blocked("missing_material")
        self.context["sources"]["meeting_materials"] = ["material.json"]
        self.material["meeting_id"] = "different-meeting"
        self.save()
        self.assert_blocked("unmatched_material")
        self.material["meeting_id"] = "synthetic-review-1"
        self.save()
        self.write("duplicate.json", self.material)
        self.context["sources"]["meeting_materials"].append("duplicate.json")
        self.assert_blocked("conflicting_material")

    def test_inadequate_facts_and_unquoted_commitment_are_blocked(self):
        material = copy.deepcopy(self.material)
        for update in [
            {"facts": ["Only one fact."]},
            {"facts": ["Duplicate.", "Duplicate."]},
            {"commitments": [{"owner": "Fixture lead", "commitment": "Might review it."}]},
        ]:
            with self.subTest(update=update):
                self.material = dict(material, **update)
                self.save()
                self.assert_blocked()

    def test_corruption_duplicate_keys_and_nonfinite_json_are_blocked(self):
        for value in ["{", "[]", '{"schema":"a","schema":"b"}', '{"value": NaN}',
                      '{"value": 1e1000}', '{"value": ' + "9" * 1000 + "}",
                      '{"nested":' + "[" * 1100 + "0" + "]" * 1100 + "}",
                      "a" * (meeting.MAX_CALENDAR_BYTES + 1)]:
            with self.subTest(size=len(value)):
                self.write("calendar.json", value)
                self.assert_blocked("invalid_source")

    def test_invalid_utf8_and_nonregular_sources_are_blocked(self):
        (self.root / "calendar.json").write_bytes(b"\xff\xfe")
        self.assert_blocked("unreadable_source")
        (self.root / "calendar.json").unlink()
        (self.root / "calendar.json").mkdir()
        self.assert_blocked("invalid_source")

    def test_schema_bounds_and_ambiguous_ids_are_blocked(self):
        original = copy.deepcopy(self.calendar)
        cases = [
            dict(original, schema="old-transcript/v1"),
            dict(original, events=[original["events"][0]] * (meeting.MAX_EVENTS + 1)),
            dict(original, events=[original["events"][0], original["events"][0]]),
            dict(original, events=[dict(original["events"][0], rrule="FREQ=WEEKLY")]),
            dict(original, events=[dict(original["events"][0], title="x" * 201)]),
            dict(original, events=[dict(original["events"][0], agenda=False)]),
            dict(original, events=[dict(original["events"][0], timezone=None)]),
        ]
        for calendar in cases:
            with self.subTest(keys=list(calendar)):
                self.write("calendar.json", calendar)
                self.assert_blocked("invalid_source")

    def test_invalid_context_is_json_safe_and_has_no_side_effects(self):
        for context in [None, {}, {"home": "."},
                        dict(self.context, now="2026-09-19T18:00:00"),
                        dict(self.context, sources=[]),
                        dict(self.context, artifact_dir=None)]:
            with self.subTest(context=context):
                result = meeting.build(context)
                json.dumps(result, allow_nan=False)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["artifacts"], [])
        self.assertFalse((self.root / "artifacts").exists())

    def test_datetime_range_extremes_do_not_crash(self):
        self.context["now"] = "9999-12-31T23:59:59Z"
        self.calendar["fetched_at"] = self.context["now"]
        self.calendar["events"] = []
        self.save()
        self.assertEqual(self.build()["status"], "suppressed")
        self.context["now"] = "0001-01-01T00:00:00+01:00"
        self.assert_blocked("invalid_source")

    def test_wrong_extension_is_rejected_before_reading(self):
        self.context["sources"]["meeting_calendar"] = "not-a-calendar.txt"
        with mock.patch.object(meeting, "_read", side_effect=AssertionError("must not read")):
            self.assert_blocked("invalid_source")

    def test_relative_paths_cannot_escape_home_and_remote_sources_are_rejected(self):
        for key, value in [("meeting_calendar", "../elsewhere.json"),
                           ("meeting_calendar", "~/" + str(self.root.resolve().parent / "outside.json")),
                           ("meeting_calendar", "https://example.invalid/calendar.ics"),
                           ("meeting_calendar", "//example.invalid/calendar.ics"),
                           ("meeting_calendar", "\\\\example.invalid\\calendar.ics")]:
            with self.subTest(value=value):
                self.context["sources"][key] = value
                self.assert_blocked()
        self.context["sources"]["meeting_calendar"] = "calendar.json"
        self.context["artifact_dir"] = "../elsewhere-artifacts"
        self.assert_blocked("unsafe_path")

    def test_source_symlink_outside_home_is_not_read(self):
        self.symlink(self.root / "outside.json", self.root.resolve().parent / "not-read.json")
        self.context["sources"]["meeting_calendar"] = "outside.json"
        self.assert_blocked("unsafe_path")

    def test_artifact_failure_is_blocked_not_ready(self):
        self.write("output-file", "not a directory")
        self.context["artifact_dir"] = "output-file"
        result = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "artifact_unwritable")
        self.assertEqual(result["artifacts"], [])

    def test_atomic_write_failure_removes_partial_artifact(self):
        with mock.patch.object(meeting.os, "replace", side_effect=OSError("write failure")):
            result = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "artifact_unwritable")
        self.assertEqual(list((self.root / "artifacts").iterdir()), [])

    def test_existing_artifact_symlink_is_replaced_without_touching_its_target(self):
        first = self.build()
        artifact = Path(first["artifacts"][0])
        target = self.write("unrelated.txt", "preserve me")
        artifact.unlink()
        self.symlink(artifact, target.resolve())
        second = self.build()
        self.assertEqual(second["status"], "ready")
        self.assertFalse(artifact.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve me")

    def test_ics_end_to_end_with_utc_folding_and_source_refs(self):
        self.use_ics()
        result = self.build()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["deadline"], "2026-09-19T19:00:00Z")
        text = Path(result["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("#/VEVENT/0", text)
        self.assertIn("discussed, not approved. It remains a proposal.", text)
        self.assertIn("#/facts/0", text)

    @unittest.skipUnless(HAS_NAMED_ZONE, "IANA timezone database is unavailable")
    def test_ics_utc_and_explicit_calendar_timezone(self):
        for date_lines, metadata in [
            (["DTSTART:20260919T190000Z", "DTEND:20260919T193000Z"], []),
            (["DTSTART:20260919T150000", "DTEND:20260919T153000"],
             ["X-WR-TIMEZONE:America/New_York"]),
        ]:
            with self.subTest(date_lines=date_lines):
                self.use_ics(["UID:synthetic-review-1", "SUMMARY:Synthetic review", *date_lines],
                             metadata)
                self.assertEqual(self.build()["deadline"], "2026-09-19T19:00:00Z")

    def test_ics_dtstamp_cannot_replace_snapshot_freshness(self):
        self.use_ics()
        self.context["sources"].pop("meeting_calendar_fetched_at")
        self.assert_blocked("invalid_source")
        self.context["sources"]["meeting_calendar_fetched_at"] = "2026-09-16T18:00:00Z"
        self.assert_blocked("stale_calendar")

    def test_ics_ignores_attendee_parameters_and_rejects_invalid_text_escapes(self):
        event = ["UID:synthetic-review-1", "SUMMARY:Synthetic review",
                 "DTSTART:20260919T190000Z", "DTEND:20260919T193000Z"]
        self.use_ics(event + ['ATTENDEE;CN="Fixture; Lead":mailto:fixture@example.invalid'])
        self.assertEqual(self.build()["status"], "ready")
        self.use_ics(event + ["DESCRIPTION:Invalid trailing escape" + "\\" * 3])
        result = self.build()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "invalid_source")

    def test_ics_recurrence_floating_time_and_malformed_structure_are_blocked(self):
        prefix = ["UID:synthetic-review-1", "SUMMARY:Synthetic review"]
        time_lines = ["DTSTART:20260919T190000Z", "DTEND:20260919T193000Z"]
        for extra, expected in [
            (["RRULE:FREQ=WEEKLY"], "unsupported_recurrence"),
            (["RDATE:20260920T190000Z"], "unsupported_recurrence"),
            (["RECURRENCE-ID:20260919T190000Z"], "unsupported_recurrence"),
            (["SUMMARY:Duplicate summary"], "invalid_source"),
        ]:
            with self.subTest(extra=extra):
                self.use_ics(prefix + time_lines + extra)
                self.assert_blocked(expected)
        self.use_ics(prefix + ["DTSTART:20260919T190000", "DTEND:20260919T193000"])
        self.assert_blocked("invalid_source")
        self.write("calendar.ics", "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n")
        self.assert_blocked("invalid_source")

    def test_all_day_entries_are_not_invented_as_timed_meetings(self):
        self.calendar["events"][0].update(start="2026-09-20", end="2026-09-21", all_day=True)
        self.save()
        self.assertEqual(self.build()["status"], "suppressed")
        self.use_ics(["UID:synthetic-review-1", "SUMMARY:All day fixture",
                      "DTSTART;VALUE=DATE:20260920", "DTEND;VALUE=DATE:20260921"])
        self.assertEqual(self.build()["status"], "suppressed")

    def test_cancelled_ics_and_alarm_do_not_contact_anyone(self):
        self.use_ics(calendar_lines=["METHOD:CANCEL"])
        self.assertEqual(self.build()["status"], "suppressed")
        self.use_ics(["UID:synthetic-review-1", "SUMMARY:Synthetic review",
                      "DTSTART:20260919T190000Z", "DTEND:20260919T193000Z",
                      "ATTENDEE:mailto:fixture@example.invalid",
                      "BEGIN:VALARM", "ACTION:EMAIL", "TRIGGER:-PT15M",
                      "ATTENDEE:mailto:fixture@example.invalid", "END:VALARM"])
        with mock.patch("socket.socket", side_effect=AssertionError("no network")):
            result = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("fixture@example.invalid",
                         Path(result["artifacts"][0]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
