from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import shutil
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from rapp_launchpad import Launchpad, ProtocolError, validate_proposal
from rapp_launchpad._transport_worker import _classify
from rapp_launchpad._vendor import rapp as R
from rapp_launchpad.config import canonical_source, configure, load_config
from rapp_launchpad.errors import ConfigurationError, IntegrityError, PluginError, TransportError
from rapp_launchpad.gate import DEFAULT_POLICY, canonical_policy, evaluate, render, validate_policy
from rapp_launchpad.processes import run_json
from rapp_launchpad.transport import CanonicalTransport
from rapp_launchpad.util import atomic_json, strict_json

NOW = "2030-05-14T16:00:00.000Z"


def proposal(fingerprint="synthetic-release-blocker-v1", scenario="future"):
    return {
        "scenario": scenario, "status": "ready", "title": "Synthetic contract example — not a real finding",
        "change": "The synthetic fixture changed its release checkpoint.",
        "impact": "The contract test should identify this bounded change.",
        "action": "Inspect the synthetic test artifact; do not send this as a real finding.",
        "decision": "Contract-test fixture only.",
        "evidence": [{"source": "synthetic:test", "observation": "Explicit synthetic unit-test evidence."}],
        "artifacts": [], "fingerprint": fingerprint, "urgency": "routine",
        "reason": "Synthetic evidence exercises the contract.",
    }


class FakeTransport:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.drains = []
        self.fail_after_enqueue = False
        self.fail_snapshot = False

    def snapshot(self, keys=()):
        if self.fail_snapshot:
            raise TransportError("synthetic transport read failure")
        return {
            "ok": True, "matches": {key: self.rows[key] for key in keys if key in self.rows},
            "counts": {"queued": sum(row["state"] == "queued" for row in self.rows.values())},
            "recipient_configured": True, "recipient_masked": "r•••@•••",
        }

    def enqueue(self, text, dedupe_key, attachments=None):
        self.calls.append((text, dedupe_key))
        accepted = dedupe_key not in self.rows
        self.rows.setdefault(dedupe_key, {
            "state": "queued", "entry_id": uuid.uuid4().hex, "at": NOW,
            "dedupe_key": dedupe_key, "reason": "synthetic durable queue",
        })
        if self.fail_after_enqueue:
            self.fail_after_enqueue = False
            raise TransportError("synthetic crash after durable enqueue")
        return {"accepted": accepted, "receipt": self.rows[dedupe_key]}

    def drain(self, limit=1):
        self.drains.append(limit)
        return {"ok": True, "processed": 0, "remaining": 0}


class PrivateCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(".test-data/python") / uuid.uuid4().hex
        self.root.mkdir(parents=True, mode=0o700)
        self.root = self.root.resolve()
        self.home = self.root / "canonical-runtime"
        self.home.mkdir(mode=0o700)
        (self.home / "state").mkdir(mode=0o700)
        atomic_json(self.home / "config.json", {"notify": True, "notify_handle": "reader@example.invalid"})
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.path = self.root / "device" / "config.json"
        configure(
            self.path, existing_source=canonical_source(), home=self.home,
            scenario_root=self.plugins, allow_send=True,
        )
        self.transport = FakeTransport()
        self.service = Launchpad(self.path, transport=self.transport, clock=lambda: NOW)
        policy = copy.deepcopy(DEFAULT_POLICY)
        policy["quiet_hours"]["enabled"] = False
        policy["timezone"] = "UTC"
        self.service.policy(policy)

    def tearDown(self):
        shutil.rmtree(self.root)

    def plugin(self, name="future", body=None):
        if body is None:
            body = "def build(context):\n    return " + repr(proposal(scenario=name)) + "\n"
        (self.plugins / (name + ".py")).write_text(body)


class ProtocolTests(unittest.TestCase):
    def test_normalizes_version_without_mutating_input(self):
        original = proposal()
        result = validate_proposal(original)
        self.assertEqual(result["schema"], "rapp-imessage-launchpad/proposal/1.0")
        self.assertNotIn("schema", original)

    def test_rejects_missing_unsupported_and_ambiguous_inputs(self):
        bad = []
        value = proposal()
        del value["evidence"]
        bad.append(value)
        bad.append(dict(proposal(), schema="rapp-imessage-launchpad/proposal/2.0"))
        bad.append(dict(proposal(), additional="unsupported"))
        bad.append(dict(proposal(), scenario="../../outbox"))
        bad.append(dict(proposal(), evidence=[]))
        bad.append(dict(proposal(), action=""))
        bad.append(dict(proposal(), fingerprint=""))
        bad.append(dict(proposal(), deadline="2030-01-01T12:00:00"))
        bad.append(dict(proposal(), title="\ud800"))
        bad.append(dict(proposal(), evidence=[{"source": "synthetic", "observation": 1.2}]))
        for value in bad:
            with self.subTest(value=list(value.keys())):
                with self.assertRaises(ProtocolError):
                    validate_proposal(value)

    def test_suppression_needs_no_fabricated_evidence(self):
        value = dict(proposal(), status="blocked", evidence=[], change="", action="", impact="", decision="")
        self.assertEqual(validate_proposal(value)["status"], "blocked")

    def test_duplicate_json_keys_and_nonfinite_values_refused(self):
        for raw in ('{"status":"ready","status":"blocked"}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                strict_json(raw)

    def test_reference_frame_hash_and_chain_roundtrip(self):
        payload = {"evidence": ["synthetic"], "count": 1, "ready": True}
        self.assertEqual(R.H("rapp/1:particle", payload), hashlib.sha256(
            b'rapp/1:particle\n{"count":1,"evidence":["synthetic"],"ready":true}'
        ).hexdigest())
        frame = R.build_frame("launchpad.receipt", "local:test", 0, NOW, payload, None)
        self.assertEqual(R.verify_frame(frame, stream_id_of_record="local:test"), (True, None, "ok"))
        self.assertFalse(R.verify_frame(frame, stream_id_of_record="other")[0])

    def test_render_refuses_truncation_of_oversized_envelope(self):
        value = proposal()
        for key in ("title", "change", "impact", "action"):
            value[key] = "synthetic " * 100
        with self.assertRaises(ProtocolError):
            render(value)

    def test_render_preserves_decision_and_all_evidence(self):
        value = proposal()
        value["evidence"].append({"source": "synthetic:second", "observation": "Second attributed observation."})
        text = render(value)
        for key in ("change", "impact", "action", "decision"):
            self.assertIn(value[key], text)
        for evidence in value["evidence"]:
            self.assertIn(evidence["source"], text)
            self.assertIn(evidence["observation"], text)
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text), 650)
        self.assertLessEqual(len(text.split()), 90)


class GateTests(unittest.TestCase):
    def policy(self, **extra):
        return {**copy.deepcopy(DEFAULT_POLICY), "timezone": "UTC", **extra}

    def test_only_queued_consumes_daily_budget(self):
        history = [
            {"at": NOW, "scenario": "decision", "fingerprint": str(i), "decision": "suppressed", "proposal": proposal()}
            for i in range(20)
        ]
        self.assertTrue(evaluate(proposal(), history, NOW, self.policy())["allow"])
        for row in history[:6]:
            row["decision"] = "queued"
        self.assertFalse(evaluate(proposal(), history, NOW, self.policy())["allow"])

    def test_quiet_hours_cross_midnight_with_zone(self):
        policy = self.policy()
        for now, allow in [
            ("2030-05-14T21:59:00Z", True), ("2030-05-14T22:00:00Z", False),
            ("2030-05-15T07:59:00Z", False), ("2030-05-15T08:00:00Z", True),
        ]:
            self.assertEqual(evaluate(proposal(), [], now, policy)["allow"], allow)

    def test_dedupe_is_semantic_not_wording_or_current_age(self):
        p = proposal()
        history = [{"at": "2029-05-14T16:00:00Z", "scenario": p["scenario"], "fingerprint": p["fingerprint"], "decision": "queued", "proposal": p}]
        changed_prose = dict(p, change="Same finding, rewritten prose.")
        self.assertFalse(evaluate(changed_prose, history, NOW, self.policy())["allow"])

    def test_invalid_policy_and_expired_deadline_fail_closed(self):
        with self.assertRaises(ConfigurationError):
            validate_policy(self.policy(max_daily=11))
        with self.assertRaises(ConfigurationError):
            validate_policy(self.policy(timezone="not/a-zone"))
        with self.assertRaises(ConfigurationError):
            validate_policy(self.policy(quiet_hours={"enabled": True, "start": "99:00", "end": "08:00"}))
        self.assertFalse(evaluate(dict(proposal(), deadline="2029-01-01T00:00:00Z"), [], NOW, self.policy())["allow"])

    def test_canonical_policy_shape_maps_without_new_policy_decisions(self):
        value = self.policy(timezone="local")
        with mock.patch("rapp_launchpad.gate._local_zone_name", return_value="America/New_York"):
            result = canonical_policy(value)
        self.assertEqual(result, {
            "max_daily": 6, "timezone": "America/New_York",
            "quiet_hours": {"start": "22:00", "end": "08:00"},
        })
        self.assertEqual(value["timezone"], "local")
        for disabled in (False, None, {"enabled": False, "start": "22:00", "end": "08:00"}):
            value = self.policy(quiet_hours=disabled)
            self.assertIs(canonical_policy(value)["quiet_hours"], False)
            self.assertFalse(validate_policy(value)["quiet_hours"]["enabled"])
        self.assertTrue(validate_policy(self.policy(quiet_hours={"start": "22:00", "end": "08:00"}))["quiet_hours"]["enabled"])

    def test_oversized_ready_proposal_is_suppressed_without_dropping_evidence(self):
        value = dict(proposal(), evidence=[{"source": "synthetic", "observation": "detail " * 100}])
        verdict = evaluate(value, [], NOW, self.policy())
        self.assertFalse(verdict["allow"])
        self.assertIn("650", verdict["reason"])


class ServiceTests(PrivateCase):
    def test_dry_run_never_calls_enqueue_or_drain(self):
        row = self.service.submit(proposal())
        self.assertEqual(row["state"], "dry_run")
        self.assertTrue(row["would_queue"])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.drains, [])
        self.assertTrue(self.service.ledger.verify()["ok"])

    def test_send_records_intent_queue_and_permanent_dedupe(self):
        row = self.service.submit(proposal(), send=True)
        self.assertEqual(row["state"], "queued")
        again = self.service.submit(dict(proposal(), change="Reworded but identical semantic identity."), send=True)
        self.assertEqual(again["state"], "suppressed")
        self.assertEqual(len(self.transport.calls), 1)
        states = [f["payload"]["state"] for f in self.service.ledger.load()]
        self.assertEqual(states[:2], ["intent", "queued"])

    def test_shared_budget_survives_second_config_and_restart(self):
        for i in range(6):
            value = proposal(str(i))
            value["change"] = f"Synthetic independent finding {i} was observed."
            value["evidence"][0]["observation"] = f"Synthetic source confirms independent finding {i}."
            self.assertEqual(self.service.submit(value, send=True)["state"], "queued")
        second_path = self.root / "second-device" / "config.json"
        configure(second_path, existing_source=canonical_source(), home=self.home, scenario_root=self.plugins, allow_send=True)
        other = Launchpad(second_path, transport=self.transport, clock=lambda: NOW)
        self.assertEqual(other.submit(proposal("seventh"), send=True)["state"], "suppressed")
        self.assertEqual(other.policy()["max_daily"], 6)
        self.assertEqual(len(self.transport.calls), 6)

    def test_multiple_producers_serialize_same_fingerprint(self):
        other = Launchpad(self.path, transport=self.transport, clock=lambda: NOW)
        results, errors = [], []
        def run(service):
            try:
                results.append(service.submit(proposal(), send=True)["state"])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(service,)) for service in (self.service, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ["queued", "suppressed"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_crash_after_enqueue_reconciles_without_resend(self):
        self.transport.fail_after_enqueue = True
        row = self.service.submit(proposal(), send=True)
        self.assertEqual(row["state"], "unknown")
        restored = Launchpad(self.path, transport=self.transport, clock=lambda: NOW)
        self.assertEqual(restored.submit(proposal(), send=True)["state"], "suppressed")
        self.assertEqual(len(self.transport.calls), 1)
        queued = [f for f in restored.ledger.load() if f["payload"].get("decision") == "queued"]
        self.assertEqual(len(queued), 1)

    def test_unreadable_transport_blocks_further_send(self):
        self.service.submit(proposal(), send=True)
        self.transport.fail_snapshot = True
        with self.assertRaises(TransportError):
            self.service.submit(proposal("new"), send=True)
        self.assertEqual(len(self.transport.calls), 1)

    def test_confirmation_is_distinct_and_does_not_reconsume_budget(self):
        row = self.service.submit(proposal(), send=True)
        with self.assertRaises(ConfigurationError):
            self.service.confirm(row["id"])
        confirmed = self.service.confirm(row["id"], received=True)
        self.assertEqual(confirmed["state"], "user_confirmed")
        self.assertIsNone(confirmed["decision"])
        self.assertEqual(self.service.receipts(refresh=True)[0]["state"], "user_confirmed")

    def test_send_requires_explicit_consent_and_macos(self):
        self.service.settings(send_enabled=False)
        with self.assertRaises(ConfigurationError):
            self.service.submit(proposal(), send=True)
        self.assertEqual(self.service.submit(proposal())["state"], "dry_run")
        self.service.settings(send_enabled=True)
        with mock.patch("sys.platform", "linux"):
            with self.assertRaises(ConfigurationError):
                self.service.submit(proposal(), send=True)
        self.assertEqual(self.transport.calls, [])

    def test_long_lived_sdk_respects_consent_revocation(self):
        other = Launchpad(self.path, transport=self.transport, clock=lambda: NOW)
        self.service.settings(send_enabled=False)
        with self.assertRaises(ConfigurationError):
            other.submit(proposal(), send=True)
        self.assertEqual(self.transport.calls, [])

    def test_truthy_strings_cannot_authorize_sends(self):
        for send in ("true", "false", 1, None):
            with self.assertRaises(ConfigurationError):
                self.service.submit(proposal(), send=send)
        with self.assertRaises(ConfigurationError):
            self.service.self_test(send="true", confirmed="true")
        self.assertEqual(self.transport.calls, [])

    def test_latest_transition_moves_receipt_to_front(self):
        first = self.service.submit(proposal("first"), send=True)
        second = self.service.submit(proposal("second"), send=True)
        self.assertEqual(self.service.receipts()[0]["id"], second["id"])
        self.service.confirm(first["id"], received=True)
        self.assertEqual(self.service.receipts()[0]["id"], first["id"])

    def test_self_test_requires_action_and_existing_mode_never_drains(self):
        with self.assertRaises(ConfigurationError):
            self.service.self_test(send=True)
        result = self.service.self_test(send=True, confirmed=True)
        self.assertEqual(result["receipt"]["state"], "queued")
        self.assertIn("self-test", self.transport.calls[0][0])
        self.assertEqual(self.transport.drains, [])
        self.assertEqual(self.service.self_test(send=True, confirmed=True)["receipt"]["state"], "suppressed")

    def test_sdk_prevents_reserved_self_test_spoofing(self):
        with self.assertRaises(ConfigurationError):
            self.service.submit(proposal(scenario="self-test"), send=True)

    def test_receipt_hash_tampering_and_truncation_block_send(self):
        self.service.submit(proposal())
        self.service.submit(proposal("second"))
        path = self.service.ledger.path
        original = path.read_bytes()
        frames = original.splitlines(keepends=True)
        path.write_bytes(frames[0])
        with self.assertRaises(IntegrityError):
            self.service.submit(proposal("new"), send=True)
        path.write_bytes(original)
        frame = json.loads(frames[0])
        frame["payload"]["reason"] = "edited"
        path.write_text(json.dumps(frame) + "\n" + frames[1].decode())
        with self.assertRaises(IntegrityError):
            self.service.ledger.verify()
        self.assertEqual(self.transport.calls, [])

    def test_missing_anchor_and_torn_tail_not_silently_repaired(self):
        self.service.submit(proposal())
        anchor = self.service.ledger.anchor.read_bytes()
        self.service.ledger.anchor.unlink()
        with self.assertRaises(IntegrityError):
            self.service.ledger.verify()
        self.service.ledger.anchor.write_bytes(anchor)
        with self.service.ledger.path.open("ab") as fh:
            fh.write(b'{"partial":')
        with self.assertRaises(IntegrityError):
            self.service.ledger.verify()

    def test_source_import_returns_count_not_private_values(self):
        result = self.service.sources({"future": {"private_setting": "SYNTHETIC_SECRET"}})
        self.assertEqual(result["entries"], 1)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertEqual(load_config(self.path)["sources"]["future"]["private_setting"], "SYNTHETIC_SECRET")


class PluginTests(PrivateCase):
    def test_runs_only_configured_module_in_child(self):
        self.plugin()
        result = self.service.run("future")
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipts"][0]["state"], "dry_run")
        self.assertEqual(self.transport.calls, [])
        with self.assertRaises(PluginError):
            self.service.plugins.build("../outside", NOW)

    def test_missing_and_broken_plugins_record_errors(self):
        self.assertFalse(self.service.run("future")["ok"])
        self.plugin(body="def build(context):\n    raise RuntimeError('SYNTHETIC_SECRET')\n")
        result = self.service.run("future")
        self.assertFalse(result["ok"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertEqual(self.transport.calls, [])

    def test_plugin_output_and_timeout_bounded(self):
        self.plugin(body="def build(context):\n    import time\n    time.sleep(30)\n    return {}\n")
        self.service.config["plugin_timeout"] = 1
        start = time.monotonic()
        with self.assertRaises(PluginError):
            self.service.plugins.build("future", NOW)
        self.assertLess(time.monotonic() - start, 6)
        self.plugin(body="def build(context):\n    print('x' * 2000000)\n    return {}\n")
        with self.assertRaises(PluginError):
            self.service.plugins.build("future", NOW)

    def test_artifacts_cannot_escape_private_plugin_root(self):
        outside = self.root / "not-a-plugin-artifact.txt"
        outside.write_text("synthetic")
        self.plugin(body="def build(context):\n    return " + repr(dict(proposal(), artifacts=[str(outside)])) + "\n")
        result = self.service.run("future")
        self.assertFalse(result["ok"])
        self.assertEqual(self.transport.calls, [])

    def test_integrated_interrupt_gate_is_used_and_validated(self):
        self.plugin()
        self.plugin("interrupt", "def evaluate(proposal, history, now, policy=None):\n    return {'allow':False,'reason':'canonical synthetic gate refused','fingerprint':proposal['fingerprint']}\n")
        result = self.service.run("future", send=True)
        self.assertEqual(result["receipts"][0]["reason"], "canonical synthetic gate refused")
        self.assertEqual(self.transport.calls, [])
        self.plugin("interrupt", "def evaluate(*args):\n    return {'allow':'yes','reason':'invalid','fingerprint':'x'}\n")
        self.assertFalse(self.service.run("future", send=True)["ok"])

    def test_integrated_gate_receives_its_canonical_policy_and_renderer_is_used(self):
        self.plugin("interrupt", """
def evaluate(proposal, history, now, policy=None):
    assert policy == {'max_daily':6,'timezone':'UTC','quiet_hours':False}
    return {'allow':True,'reason':'canonical gate permitted','fingerprint':proposal['fingerprint']}
def render(proposal):
    lines = ['CANONICAL'] + [key + ': ' + proposal[key] for key in ('change','impact','action','decision')]
    lines += [item['source'] + ': ' + item['observation'] for item in proposal['evidence']]
    return '\\n'.join(lines)
def delivery_key(proposal):
    return 'a' * 64
""")
        row = self.service.submit(proposal(), send=True)
        self.assertEqual(row["state"], "queued")
        text = self.transport.calls[0][0]
        self.assertTrue(text.startswith("CANONICAL\n"))
        self.assertIn(proposal()["decision"], text)
        self.assertIn(proposal()["evidence"][0]["source"], text)

    def test_bad_canonical_render_records_error_before_enqueue_intent(self):
        self.plugin("interrupt", """
def evaluate(proposal, history, now, policy=None):
    return {'allow':True,'reason':'canonical gate permitted','fingerprint':proposal['fingerprint']}
def render(proposal):
    return 'x' * 651
""")
        row = self.service.submit(proposal(), send=True)
        self.assertEqual(row["state"], "error")
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(any(frame["payload"]["state"] == "intent" for frame in self.service.ledger.load()))

    def test_optional_renderer_absence_uses_complete_fallback(self):
        self.plugin("interrupt", """
def evaluate(proposal, history, now, policy=None):
    return {'allow':True,'reason':'canonical gate permitted','fingerprint':proposal['fingerprint']}
def delivery_key(proposal):
    return 'a' * 64
""")
        row = self.service.submit(proposal(), send=True)
        self.assertEqual(row["state"], "queued")
        self.assertIn(proposal()["decision"], self.transport.calls[0][0])
        self.assertIn(proposal()["evidence"][0]["observation"], self.transport.calls[0][0])

    def test_gate_receives_complete_history_larger_than_one_mebibyte(self):
        self.plugin("interrupt", "def evaluate(proposal, history, now, policy=None):\n    return {'allow':False,'reason':str(len(history)),'fingerprint':proposal['fingerprint']}\n")
        item = dict(proposal(), change="synthetic " * 300)
        history = [
            {"at": NOW, "scenario": "future", "fingerprint": str(i), "decision": "suppressed", "proposal": item}
            for i in range(500)
        ]
        self.assertGreater(len(json.dumps(history)), 1_048_576)
        result = self.service.plugins.evaluate(proposal(), history, NOW, self.service.policy())
        self.assertEqual(result["reason"], "500")


class CanonicalAdapterTests(PrivateCase):
    def test_actual_canonical_enqueue_and_dedupe_without_any_sender(self):
        real = CanonicalTransport(self.service.config)
        key = "launchpad/1/" + "a" * 64
        result = real.enqueue("Synthetic adapter contract fixture. Never drained.", key)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["receipt"]["state"], "queued")
        self.assertFalse(real.enqueue("Same fixture.", key)["accepted"])
        snapshot = real.snapshot([key])
        self.assertEqual(snapshot["counts"]["queued"], 1)
        self.assertNotIn("reader@example.invalid", json.dumps(snapshot))
        queued = json.loads((self.home / "state" / "outbox.jsonl").read_text())
        self.assertEqual(queued["attachments"], [])
        self.assertEqual(queued["dedupe_key"], key)
        with self.assertRaises(TransportError):
            real.drain()
        self.assertFalse((self.home / "state" / "outbox-sent.jsonl").exists())

    def test_canonical_malformed_terminal_ledger_is_not_rewritten_by_diagnostics(self):
        bad = self.home / "state" / "outbox-sent.jsonl"
        bad.write_text('{"torn":')
        with self.assertRaises(TransportError):
            CanonicalTransport(self.service.config).snapshot()
        self.assertEqual(bad.read_text(), '{"torn":')

    def test_existing_recipient_is_preserved_not_reformatted(self):
        atomic_json(self.home / "config.json", {"notify": True, "notify_handle": "legacy-synthetic-handle"})
        real = CanonicalTransport(self.service.config)
        result = real.enqueue("Synthetic adapter fixture. Never drained.", "launchpad/1/" + "b" * 64)
        self.assertTrue(result["accepted"])
        row = json.loads((self.home / "state" / "outbox.jsonl").read_text())
        self.assertEqual(row["to"], "legacy-synthetic-handle")
        self.assertFalse((self.home / "state" / "outbox-sent.jsonl").exists())

    def test_sent_ledger_and_exit_success_are_not_delivery(self):
        row = {"entry_id": "a" * 32, "at": NOW, "sent_at": NOW}
        self.assertEqual(_classify(row, "sent")["state"], "sent_unverified")
        self.assertEqual(_classify(dict(row, unverified="exit 0"), "sent")["state"], "sent_unverified")
        verified = dict(row, verified_at=NOW, delivery_evidence={"source": "Messages/chat.db", "message_rowid": 9})
        self.assertEqual(_classify(verified, "sent")["state"], "delivered")
        self.assertEqual(_classify(verified, "unknown")["state"], "unknown")

    def test_vendored_files_match_audited_provenance(self):
        root = canonical_source().parent
        metadata = json.loads((root / "provenance.json").read_text())
        for name, digest in metadata["sha256"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest, name)
        self.assertIn("Kody Wildfeuer", (root / "LICENSE").read_text())


class ScheduleTests(PrivateCase):
    def test_existing_pipeline_schedule_never_replaces_or_duplicates_drainer(self):
        from rapp_launchpad import schedule
        self.plugin()
        self.service.settings(enabled_scenarios=["future"])
        path = self.root / "LaunchAgents" / (schedule.LABEL + ".plist")
        with mock.patch.object(schedule, "plist_path", return_value=path), \
             mock.patch.object(schedule, "_launchctl") as launchctl, \
             mock.patch.object(schedule, "launchd_status", return_value={"loaded": True, "state": "waiting"}):
            result = schedule.install(self.service, send=True)
            self.assertFalse(result["drains_outbox"])
            plist = plistlib.loads(path.read_bytes())
            self.assertEqual(plist["Label"], "com.rapp.imessage-launchpad.scheduler")
            self.assertIn("tick", plist["ProgramArguments"])
            self.assertNotIn("outbox.py", " ".join(plist["ProgramArguments"]))
            self.assertFalse(plist["RunAtLoad"])
            calls = repr(launchctl.call_args_list)
            self.assertNotIn("storykeeper.outbox-drain", calls)
            schedule.uninstall(self.service)
            self.assertFalse(path.exists())

    def test_schedule_requires_consent_and_refuses_other_config_job(self):
        from rapp_launchpad import schedule
        with self.assertRaises(ConfigurationError):
            schedule.install(self.service)
        self.plugin()
        self.service.settings(enabled_scenarios=["future"])
        path = self.root / "foreign.plist"
        path.write_bytes(plistlib.dumps({"Label": schedule.LABEL, "ProgramArguments": ["python", "--config", "/synthetic/other-config"]}))
        original = path.read_bytes()
        with mock.patch.object(schedule, "plist_path", return_value=path):
            with self.assertRaises(ConfigurationError):
                schedule.install(self.service, send=True)
            with self.assertRaises(ConfigurationError):
                schedule.uninstall(self.service)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
