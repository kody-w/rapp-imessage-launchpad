import copy
import hashlib
import html
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import unittest
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scenarios import win


NOW = "2026-09-19T18:30:00+00:00"


def verdict_fixture():
    rows = [
        ("rv_world_merging", False, "critical", "last merge 30.2h ago"),
        ("rv_meaningful_activity", False, "critical", "chat stale 44.1h; agent state stale 30.2h"),
        ("rv_validation", False, "warn", "gate has not run in 43.6h - stopped, not rejecting"),
        ("alert_delivery", False, "warn", "3 UNKNOWN delivery evidence record(s)"),
        ("w_openrappter_spin", False, "warn", "spinning launchd job(s): com.example.agent (runs=120, last exit 1)"),
        ("w_sentinel_current", False, "warn", "running abc1234, 2 commits behind origin/main def5678; review deployment"),
        ("w_neighbor_moving", False, "warn", "sample-neighbor: alive but last WORKED 500m ago (bar 480m) - ran is not worked"),
        ("rb_workflows", True, "warn", "all green"),
        ("rb_public_surface", True, "warn", "public state readable, 12 agents"),
    ]
    verdict = {
        "generated": "2026-09-19T18:18:00+00:00",
        "checks": [
            {"id": key, "ok": ok, "severity": severity, "detail": detail,
             "produced_by": "sanitized_fixture"}
            for key, ok, severity, detail in rows
        ],
    }
    return synchronize(verdict)


def synchronize(verdict):
    failed = [item for item in verdict["checks"] if not item["ok"]]
    verdict["failed"] = [item["id"] for item in failed]
    verdict["critical"] = [item["id"] for item in failed if item["severity"] == "critical"]
    verdict["status"] = "critical" if verdict["critical"] else ("degraded" if failed else "healthy")
    verdict["summary"] = "; ".join(f"{item['id']}: {item['detail']}" for item in failed)
    return verdict


def sent_fixture():
    return [{
        "entry_id": "a" * 32,
        "at": "2026-09-19T17:45:00+00:00",
        "to": "SYNTHETIC_RECIPIENT_DO_NOT_COPY",
        "text": (
            "Storykeeper Demo needs you.\n"
            "rv_world_merging: last merge 29.6h ago; "
            "rv_meaningful_activity: chat stale 43.5h\n\n"
            "Static HTML report:\n"
            "http://fixture-report.local/share/do-not-copy-private-token.html"
        ),
        "attachments": [],
        "unverified": "sent unverified: unavailable receipt",
        "sent_at": "2026-09-19T17:45:01+00:00",
        "queue_sha256": "b" * 64,
    }]


class PageReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


class WinScenarioTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("scenarios/win_assets") / ("fixture-" + uuid.uuid4().hex)
        self.home = self.root / "home"
        self.artifacts = self.root / "artifacts"
        (self.home / "state").mkdir(parents=True, mode=0o700)
        self.verdict = verdict_fixture()
        self.messages = sent_fixture()
        self.context = {
            "home": str(self.home), "artifact_dir": str(self.artifacts),
            "now": NOW, "sources": {},
        }
        self.write_sources()

    def tearDown(self):
        shutil.rmtree(self.root)
        try:
            self.root.parent.rmdir()
        except OSError:
            pass

    def write_sources(self):
        (self.home / "state/last_verdict.json").write_text(
            json.dumps(self.verdict), encoding="utf-8")
        (self.home / "state/outbox-sent.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in self.messages), encoding="utf-8")

    def inline_context(self, verdict=None, messages=None):
        return {
            **self.context,
            "sources": {
                "last_verdict": copy.deepcopy(self.verdict if verdict is None else verdict),
                "outbox_sent": copy.deepcopy(self.messages if messages is None else messages),
            },
        }

    def read_ready(self, result):
        self.assertEqual("ready", result["status"], result)
        self.assertEqual(2, len(result["artifacts"]))
        text_path, zip_path = map(Path, result["artifacts"])
        with zipfile.ZipFile(zip_path) as archive:
            self.assertEqual([win.HTML_NAME], archive.namelist())
            self.assertIsNone(archive.testzip())
            page = archive.read(win.HTML_NAME).decode("utf-8")
        receipt_path = text_path.parent / win.RECEIPT_NAME
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return text_path.read_text(encoding="utf-8"), page, receipt

    def assert_notification_budget(self, result):
        self.assertEqual("ready", result["status"], result)
        fields = ("title", "change", "impact", "action", "decision")
        envelope = "\n".join(
            [key.title() + ": " + result[key] for key in fields]
            + ["Evidence [" + row["source"] + "]: " + row["observation"]
               for row in result["evidence"]]
        )
        self.assertLessEqual(len(envelope.encode("ascii")), 650)
        self.assertLessEqual(len(envelope.split()), 90)
        visible = {key: result[key] for key in fields}
        visible["evidence"] = result["evidence"]
        self.assertLessEqual(
            len(json.dumps(visible, ensure_ascii=False, separators=(",", ":")).encode("ascii")), 650)

    def assert_suppressed(self, result):
        self.assertEqual("suppressed", result["status"], result)
        self.assertEqual([], result["artifacts"])
        self.assertEqual("No user decision requested.", result["decision"])

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Host does not permit symlink fixtures: {type(exc).__name__}")

    def test_actual_health_and_outbox_shapes_produce_a_measured_usable_bundle(self):
        result = win.build(self.context)
        text, page, receipt = self.read_ready(result)
        self.assertEqual("win", result["scenario"])
        self.assertEqual("routine", result["urgency"])
        for field in ("title", "change", "impact", "action", "decision", "fingerprint", "reason"):
            self.assertIsInstance(result[field], str)
            self.assertTrue(result[field])
        self.assertTrue(all(set(row) == {"source", "observation"} for row in result["evidence"]))
        acceptance = receipt["acceptance"]
        self.assertEqual(7, acceptance["failed_checks"])
        self.assertEqual(7, acceptance["text_complete_findings"])
        self.assertEqual(7, acceptance["html_complete_findings"])
        for field in ("validated_from_disk", "escaped_evidence", "html_passive", "zip_integrity"):
            self.assertIs(acceptance[field], True)
        self.assertEqual(2, len(receipt["before"]["covered_check_ids"]))
        self.assertEqual(len(self.messages[0]["text"]), receipt["before"]["message_characters"])
        self.assertEqual(1, receipt["before"]["private_network_urls"])
        self.assertEqual(0, receipt["before"]["attachment_count"])
        self.assertIs(receipt["before"]["explicitly_unverified"], True)
        self.assertIn("2/7 current check IDs", text)
        self.assertIn("7/7 findings have their complete recorded detail", text)
        self.assertIn("delivery unverified", text)
        self.assertIn("No repairs, retries", text)
        self.assertIn("Evidence source: state/last_verdict.json", text)
        self.assertNotIn("all green", text)
        parsed = PageReader()
        parsed.feed(page)
        visible = "".join(parsed.text)
        for check in self.verdict["checks"]:
            if not check["ok"]:
                self.assertIn(check["id"], text)
                self.assertIn(check["detail"], text)
                self.assertIn(check["detail"], visible)
        self.assertNotIn("script", parsed.tags)
        self.assertFalse(any(key in ("src", "href", "action") or key.startswith("on")
                             for key, _ in parsed.attrs))
        for path_text in result["artifacts"]:
            path = Path(path_text)
            self.assertTrue(path.is_relative_to(self.artifacts.resolve()))
            if os.name == "posix":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             receipt["files"][path.name]["sha256"])
            self.assertEqual(path.stat().st_size, receipt["files"][path.name]["bytes"])
        for payload in (text, page, json.dumps(receipt), json.dumps(result)):
            self.assertNotIn("SYNTHETIC_RECIPIENT_DO_NOT_COPY", payload)
            self.assertNotIn("do-not-copy-private-token", payload)
            self.assertNotIn("fixture-report.local", payload)

    def test_long_evidence_is_not_truncated_and_html_is_inert(self):
        detail = (
            '</pre><script>alert("x")</script><img src="https://invalid.test/leak" '
            'onerror="alert(1)"> & \'quotes\'\n'
            + "complete evidence • " * 300 + "END OF EVIDENCE"
        )
        self.verdict["checks"][0]["detail"] = detail
        result = win.build(self.inline_context(synchronize(self.verdict)))
        text, page, receipt = self.read_ready(result)
        self.assertIn(detail, text)
        self.assertIn(html.escape(detail), page)
        parsed = PageReader()
        parsed.feed(page)
        self.assertIn(detail, "".join(parsed.text))
        self.assertNotIn("script", parsed.tags)
        self.assertNotIn("img", parsed.tags)
        self.assertNotIn("END OF EVIDENCE", result["impact"])
        self.assertTrue(receipt["acceptance"]["escaped_evidence"])
        self.assert_notification_budget(result)

    def test_ready_notification_preserves_answers_and_sources_within_gate_budget(self):
        for messages in (self.messages, []):
            with self.subTest(baseline=bool(messages)):
                result = win.build(self.inline_context(messages=messages))
                self.read_ready(result)
                self.assert_notification_budget(result)
                self.assertEqual(
                    ["last_verdict", "outbox_sent", "acceptance.json"],
                    [row["source"] for row in result["evidence"]],
                )

    def test_largest_supported_check_set_has_a_compact_notification(self):
        self.verdict["generated"] = "2026-09-19T18:18:00.123456+00:00"
        self.verdict["checks"] = [
            {"id": f"sample_issue_{index:03d}", "ok": False,
             "severity": "critical" if index == 0 else "warn",
             "detail": f"Synthetic recorded observation {index}."}
            for index in range(win.MAX_CHECKS)
        ]
        synchronize(self.verdict)
        self.messages[0]["text"] = "; ".join(self.verdict["failed"]) + "\nhttp://fixture-report.local/"
        self.messages[0]["attachments"] = ["synthetic-attachment"] * 1000
        result = win.build(self.inline_context())
        self.read_ready(result)
        self.assert_notification_budget(result)

    def test_long_unicode_source_paths_stay_in_artifacts_not_the_notification(self):
        relative = Path("state") / ("synthetic-" * 6 + "観測.json")
        (self.home / relative).write_text(json.dumps(self.verdict), encoding="utf-8")
        result = win.build({**self.context, "sources": {"last_verdict": str(relative)}})
        text, _, receipt = self.read_ready(result)
        self.assertIn(str(relative), text)
        self.assertEqual(str(relative), receipt["provenance"]["last_verdict"])
        self.assert_notification_budget(result)

    def test_unknown_check_identifier_is_escaped_without_an_invented_diagnosis(self):
        self.verdict["checks"][0]["id"] = "<svg/onload=alert(1)>"
        synchronize(self.verdict)
        text, page, _ = self.read_ready(win.build(self.inline_context()))
        self.assertIn("no curated interpretation", text)
        self.assertIn("<svg/onload=alert(1)>", text)
        self.assertIn("&lt;svg/onload=alert(1)&gt;", page)
        parsed = PageReader()
        parsed.feed(page)
        self.assertNotIn("svg", parsed.tags)

    def test_unreadable_check_evidence_is_not_relabelled_as_a_confirmed_outage(self):
        self.verdict["checks"][0]["detail"] = "unreadable: permission denied while listing commits"
        text, page, _ = self.read_ready(win.build(self.inline_context(synchronize(self.verdict))))
        self.assertIn("permission denied while listing commits", text)
        self.assertIn("merge freshness needs attention", text)
        self.assertNotIn("updates are not merging", text)
        self.assertNotIn("updates are not merging", page)

    def test_build_is_read_only_and_never_contacts_network_or_runs_commands(self):
        before = {str(path): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        context = self.inline_context()
        unchanged = copy.deepcopy(context)
        with mock.patch("subprocess.run", side_effect=AssertionError("no commands")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("no network")), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("no requests")):
            self.read_ready(win.build(context))
        after = {str(path): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(unchanged, context)

    def test_profile_defaults_are_caller_owned_and_explicit_sources_override_disk(self):
        (self.home / "state/last_verdict.json").write_text("invalid", encoding="utf-8")
        (self.home / "state/outbox-sent.jsonl").write_text("invalid", encoding="utf-8")
        with mock.patch.object(Path, "home", side_effect=AssertionError("no implicit profile lookup")):
            _, _, receipt = self.read_ready(win.build(self.inline_context()))
        self.assertEqual("sources.last_verdict", receipt["provenance"]["last_verdict"])
        self.assertEqual("sources.outbox_sent", receipt["provenance"]["outbox_sent"])
        self.assertTrue(receipt["before"]["available"])

    def test_non_posix_host_uses_binary_safe_flags_and_caller_directory_acl(self):
        host = SimpleNamespace(
            name="nt", O_BINARY=0,
            **{key: getattr(os, key) for key in (
                "O_RDONLY", "O_WRONLY", "O_CREAT", "O_EXCL",
                "PathLike", "open", "fdopen", "fstat", "fsync",
            )},
        )
        with mock.patch.object(win, "os", host):
            _, _, receipt = self.read_ready(win.build(self.context))
        self.assertEqual("Inherited OS ACL; the caller must protect artifact_dir",
                         receipt["artifact_permissions"])

    def test_public_files_have_no_personal_home_phone_or_address_literals(self):
        checkout = Path(__file__).resolve().parents[1]
        patterns = (
            r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])",
            r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/",
            r"[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\",
            r"\+[1-9][0-9]{7,14}\b",
        )
        for relative in ("scenarios/win.py", "tests/test_scenario_win.py", "docs/scenarios/win.md"):
            source = (checkout / relative).read_text(encoding="utf-8")
            for pattern in patterns:
                with self.subTest(file=relative, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source))

    def test_repeated_identical_build_reuses_immutable_validated_files(self):
        first = win.build(self.context)
        self.read_ready(first)
        paths = [*map(Path, first["artifacts"]), Path(first["artifacts"][0]).parent / win.RECEIPT_NAME]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        second = win.build(self.context)
        self.assertEqual(first, second)
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])

    def test_semantic_fingerprint_ignores_times_order_counters_and_artifact_location(self):
        first = win.build(self.inline_context())
        self.read_ready(first)
        self.verdict["generated"] = "2026-09-19T20:01:00+00:00"
        self.verdict["checks"].reverse()
        for item in self.verdict["checks"]:
            item["detail"] = item["detail"].replace("30.2", "32.0").replace("runs=120", "runs=920")
        synchronize(self.verdict)
        context = self.inline_context()
        context["artifact_dir"] = str(self.root / "other-artifacts")
        context["now"] = "2026-09-19T20:10:00+00:00"
        second = win.build(context)
        self.read_ready(second)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["artifacts"], second["artifacts"])

    def test_shared_gate_suppresses_sample_clock_changes_but_admits_new_findings(self):
        gate_path = os.environ.get("RAPP_WIN_TEST_GATE")
        if gate_path:
            spec = importlib.util.spec_from_file_location("win_shared_gate_regression", gate_path)
            gate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gate)
        else:
            try:
                gate = importlib.import_module("scenarios.interrupt")
            except ModuleNotFoundError as exc:
                if exc.name != "scenarios.interrupt":
                    raise
                self.skipTest("Run with the shared gate installed or set RAPP_WIN_TEST_GATE.")

        first_context = self.inline_context()
        incident = "Last merge failed on 2026-09-18T12:00:00Z; merged state has not advanced."
        first_context["sources"]["last_verdict"]["checks"][0]["detail"] = incident
        synchronize(first_context["sources"]["last_verdict"])
        first = win.build(first_context)
        self.read_ready(first)
        self.assertTrue(gate.evaluate(first, [], NOW)["allow"])
        history = [{
            "at": NOW, "scenario": first["scenario"], "fingerprint": first["fingerprint"],
            "decision": "queued", "proposal": copy.deepcopy(first),
        }]

        later_context = copy.deepcopy(first_context)
        later_context["now"] = "2026-09-19T18:45:00Z"
        later_context["sources"]["last_verdict"]["generated"] = "2026-09-19T18:33:00+00:00"
        later = win.build(later_context)
        text, page, _ = self.read_ready(later)
        self.assertEqual(first["fingerprint"], later["fingerprint"])
        self.assertNotEqual(first["artifacts"], later["artifacts"])
        duplicate = gate.evaluate(later, history, later_context["now"])
        self.assertFalse(duplicate["allow"], duplicate)
        self.assertIn("duplicate", duplicate["reason"])
        self.assertEqual(gate.delivery_key(first), gate.delivery_key(later))
        self.assertRegex(gate.delivery_key(first), r"^[a-f0-9]{64}$")
        self.assertIn(incident, text)
        self.assertIn(incident, page)

        changed_context = copy.deepcopy(later_context)
        for check in changed_context["sources"]["last_verdict"]["checks"]:
            if check["id"] == "rb_workflows":
                check.update(ok=False, detail="Workflow failed on 2026-09-19T18:32:00Z: permission denied.")
        synchronize(changed_context["sources"]["last_verdict"])
        changed = win.build(changed_context)
        self.read_ready(changed)
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
        admitted = gate.evaluate(changed, history, changed_context["now"])
        self.assertTrue(admitted["allow"], admitted)
        self.assertNotEqual(gate.delivery_key(later), gate.delivery_key(changed))
        self.assertLessEqual(len(gate.render(later)), 650)
        self.assertLessEqual(len(gate.render(later).split()), 90)

    def test_membership_and_severity_change_the_fingerprint(self):
        first = win.build(self.inline_context())
        self.verdict["checks"][0]["severity"] = "warn"
        second = win.build(self.inline_context(synchronize(self.verdict)))
        self.verdict["checks"][0]["ok"] = True
        third = win.build(self.inline_context(synchronize(self.verdict)))
        for result in (first, second, third):
            self.read_ready(result)
        self.assertEqual(3, len({row["fingerprint"] for row in (first, second, third)}))

    def test_provenance_changes_do_not_collide_with_existing_artifacts(self):
        first = win.build(self.inline_context())
        self.verdict["checks"][-1]["detail"] = "public state readable, 13 agents"
        second = win.build(self.inline_context())
        self.read_ready(first)
        self.read_ready(second)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["artifacts"], second["artifacts"])

    def test_healthy_state_is_suppressed_without_creating_artifacts(self):
        for check in self.verdict["checks"]:
            check["ok"] = True
        self.assert_suppressed(win.build(self.inline_context(synchronize(self.verdict))))
        self.assertFalse(self.artifacts.exists())

    def test_missing_required_verdict_fields_are_not_invented(self):
        for key in ("generated", "status", "checks", "failed", "critical"):
            with self.subTest(key=key):
                verdict = copy.deepcopy(self.verdict)
                del verdict[key]
                self.assert_suppressed(win.build(self.inline_context(verdict)))
        self.assertFalse(self.artifacts.exists())

    def test_bad_or_inconsistent_check_records_fail_closed(self):
        mutations = (
            lambda value: value["checks"][0].pop("detail"),
            lambda value: value["checks"][0].update(detail=""),
            lambda value: value["checks"][0].update(detail="unsafe\x00text"),
            lambda value: value["checks"][0].update(detail=42),
            lambda value: value["checks"][0].update(ok="false"),
            lambda value: value["checks"][0].update(ok=0),
            lambda value: value["checks"][0].update(severity="urgent"),
            lambda value: value["checks"][0].update(id=value["checks"][1]["id"]),
            lambda value: value["checks"][0].update(id="two ids"),
            lambda value: value["checks"].append("not a check"),
            lambda value: value.update(checks=[]),
            lambda value: value.update(checks={"one": "check"}),
            lambda value: value.update(failed=[]),
            lambda value: value["failed"].append(value["failed"][0]),
            lambda value: value.update(critical=[]),
            lambda value: value.update(status="healthy"),
            lambda value: value.update(failed=[{}]),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                verdict = copy.deepcopy(self.verdict)
                mutation(verdict)
                self.assert_suppressed(win.build(self.inline_context(verdict)))
        self.assertFalse(self.artifacts.exists())

    def test_stale_future_and_timezone_free_dates_are_suppressed(self):
        for value in ("2026-09-19T12:29:59+00:00", "2026-09-19T18:35:01+00:00",
                      "2026-09-19T18:20:00", "not a date", None):
            with self.subTest(value=value):
                verdict = copy.deepcopy(self.verdict)
                verdict["generated"] = value
                self.assert_suppressed(win.build(self.inline_context(verdict)))
        self.assertFalse(self.artifacts.exists())

    def test_freshness_boundaries_and_iso_z_timestamps_work(self):
        for value in ("2026-09-19T12:30:00Z", "2026-09-19T18:35:00Z",
                      "2026-09-19T20:18:00+02:00"):
            with self.subTest(value=value):
                verdict = copy.deepcopy(self.verdict)
                verdict["generated"] = value
                self.read_ready(win.build(self.inline_context(verdict)))

    def test_invalid_context_never_writes(self):
        contexts = [None, [], {}, {**self.context, "sources": []},
                    {**self.context, "now": "2026-09-19T18:30:00"},
                    {**self.context, "home": ""}, {**self.context, "artifact_dir": None}]
        for context in contexts:
            with self.subTest(context=context):
                self.assert_suppressed(win.build(context))
        self.assertFalse(self.artifacts.exists())

    def test_missing_empty_malformed_or_nonobject_verdict_suppresses(self):
        path = self.home / "state/last_verdict.json"
        path.unlink()
        self.assert_suppressed(win.build(self.context))
        for payload in ("", "{", "[]", "null"):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                self.assert_suppressed(win.build(self.context))
        path.write_bytes(b"\xff\xfe\x00")
        self.assert_suppressed(win.build(self.context))
        self.assertFalse(self.artifacts.exists())

    def test_absent_optional_baseline_is_explicit_not_fake_before_after(self):
        (self.home / "state/outbox-sent.jsonl").unlink()
        contexts = [
            self.context,
            {**self.context, "sources": {"outbox_sent": None}},
            {**self.context, "sources": {"outbox_sent": {}}},
        ]
        for context in contexts:
            with self.subTest(context=context):
                text, _, receipt = self.read_ready(win.build(context))
                self.assertFalse(receipt["before"]["available"])
                self.assertIn("Message baseline unavailable", text)
                self.assertNotIn("0/7 current check IDs", text)
                self.assertNotIn("0 attachments", text)

    def test_latest_usable_matching_record_is_selected_not_unrelated_or_future_record(self):
        unrelated = {**self.messages[0], "text": "Other scenario: calendar reminder"}
        future = {**self.messages[0], "sent_at": "2026-09-19T23:00:00Z"}
        malformed = {"text": self.messages[0]["text"], "at": "invalid"}
        records = self.messages + [unrelated, future, malformed, "invalid"]
        _, _, receipt = self.read_ready(win.build(self.inline_context(messages=records)))
        self.assertEqual("2026-09-19T17:45:01+00:00", receipt["before"]["recorded_at"])
        self.assertEqual(2, len(receipt["before"]["covered_check_ids"]))

    def test_oversized_ledger_reads_only_newest_tail_and_handles_torn_final_record(self):
        path = self.home / "state/outbox-sent.jsonl"
        newest = json.dumps(self.messages[0]).encode("utf-8") + b"\n"
        path.write_bytes(b"x" * (win.MAX_SENT_BYTES * 3) + b"\n" + newest + b'{"text":')
        with mock.patch.object(win, "_read_file", wraps=win._read_file) as reader:
            _, _, receipt = self.read_ready(win.build(self.context))
        self.assertEqual(2, len(receipt["before"]["covered_check_ids"]))
        reads = [call for call in reader.call_args_list if call.kwargs.get("tail")]
        self.assertEqual(1, len(reads))
        self.assertEqual(win.MAX_SENT_BYTES, reads[0].args[1])

    def test_input_size_and_check_count_budgets(self):
        path = self.home / "state/last_verdict.json"
        path.write_bytes(b" " * (win.MAX_VERDICT_BYTES + 1))
        self.assert_suppressed(win.build(self.context))
        verdict = copy.deepcopy(self.verdict)
        verdict["checks"] = verdict["checks"] * (win.MAX_CHECKS + 1)
        self.assert_suppressed(win.build(self.inline_context(verdict)))
        verdict = copy.deepcopy(self.verdict)
        verdict["checks"][0]["detail"] = "x" * (win.MAX_DETAIL_CHARS + 1)
        self.assert_suppressed(win.build(self.inline_context(verdict)))
        verdict = copy.deepcopy(self.verdict)
        verdict["extra"] = "x" * (win.MAX_VERDICT_BYTES + 1)
        self.assert_suppressed(win.build(self.inline_context(verdict)))
        self.assertFalse(self.artifacts.exists())

    def test_explicit_source_paths_are_confined_to_home(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.verdict), encoding="utf-8")
        for raw in ("../outside.json", str(outside.resolve())):
            with self.subTest(raw=raw):
                context = {**self.context, "sources": {"last_verdict": raw}}
                with mock.patch.object(win, "_read_file", side_effect=AssertionError("outside read")):
                    self.assert_suppressed(win.build(context))
        self.assertFalse(self.artifacts.exists())
        self.read_ready(win.build({
            **self.context, "sources": {"last_verdict": "state/last_verdict.json"},
        }))

    def test_symlink_source_cannot_escape_home(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.verdict), encoding="utf-8")
        source = self.home / "state/last_verdict.json"
        source.unlink()
        self.symlink(source, outside.resolve())
        self.assert_suppressed(win.build(self.context))
        self.assertFalse(self.artifacts.exists())

    def test_nonregular_source_does_not_get_read(self):
        source = self.home / "state/last_verdict.json"
        source.unlink()
        source.mkdir()
        self.assert_suppressed(win.build(self.context))
        source.rmdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(source)
            self.assert_suppressed(win.build(self.context))
        self.assertFalse(self.artifacts.exists())

    def test_tampered_artifact_suppresses_without_overwriting_it(self):
        first = win.build(self.context)
        self.read_ready(first)
        path = Path(first["artifacts"][0])
        path.write_text("tampered", encoding="utf-8")
        self.assert_suppressed(win.build(self.context))
        self.assertEqual("tampered", path.read_text(encoding="utf-8"))

    def test_artifact_symlink_cannot_write_outside_artifact_root(self):
        first = win.build(self.context)
        bundle = Path(first["artifacts"][0]).parent
        shutil.rmtree(bundle)
        outside = self.root / "outside"
        outside.mkdir()
        self.symlink(bundle, outside.resolve(), directory=True)
        self.assert_suppressed(win.build(self.context))
        self.assertEqual([], list(outside.iterdir()))
        self.assertTrue(bundle.is_symlink())

    def test_artifact_leaf_symlink_is_refused_before_reading(self):
        first = win.build(self.inline_context())
        path = Path(first["artifacts"][0])
        original = path.read_bytes()
        path.unlink()
        outside = self.root / "outside.txt"
        outside.write_bytes(original)
        self.symlink(path, outside.resolve())
        with mock.patch.object(win.os, "open", side_effect=AssertionError("must not follow leaf")):
            self.assert_suppressed(win.build(self.inline_context()))
        self.assertEqual(original, outside.read_bytes())

    def test_failed_readback_cannot_claim_ready_and_partial_bundle_is_removed(self):
        original = win._write_private

        def corrupt(path, payload):
            original(path, b"incomplete text" if path.name == win.TEXT_NAME else payload)

        with mock.patch.object(win, "_write_private", side_effect=corrupt):
            self.assert_suppressed(win.build(self.context))
        self.assertEqual([], list(self.artifacts.iterdir()))

    def test_write_failure_is_suppressed_not_a_fake_user_decision(self):
        with mock.patch.object(win, "_write_private", side_effect=PermissionError("denied")):
            self.assert_suppressed(win.build(self.context))
        self.assertEqual([], list(self.artifacts.iterdir()))


if __name__ == "__main__":
    unittest.main()
