import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from test_launchpad import PrivateCase, NOW, proposal
from rapp_launchpad import Launchpad
from rapp_launchpad.config import canonical_source, package_scenarios
from rapp_launchpad.transport import CanonicalTransport
from rapp_launchpad.util import atomic_json


class RegisteredScenarioIntegration(PrivateCase):
    def test_all_ten_real_modules_build_without_dependency_or_protocol_errors(self):
        config = json.loads(self.path.read_text())
        config["scenario_root"] = str(package_scenarios())
        atomic_json(self.path, config)
        service = Launchpad(self.path, transport=self.transport, clock=lambda: NOW)
        result = service.run("all")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["receipts"]), 10)
        self.assertTrue(all(row["state"] == "suppressed" for row in result["receipts"]))
        self.assertEqual(self.transport.calls, [])

    def test_custom_registration_uses_same_canonical_gate(self):
        config = json.loads(self.path.read_text())
        config["allowed_scenarios"].append("custom-proof")
        atomic_json(self.path, config)
        self.plugin("custom-proof")
        result = self.service.run("custom-proof", send=True)
        self.assertEqual(result["receipts"][0]["state"], "queued", result)
        self.assertEqual(self.service.run("custom-proof", send=True)["receipts"][0]["state"], "suppressed")
        self.assertEqual(len(self.transport.calls), 1)

    def test_substantive_update_uses_distinct_version_key_not_second_policy(self):
        original = proposal()
        first = self.service.submit(original, send=True)
        updated = copy.deepcopy(original)
        updated["change"] = "The synthetic fixture now has 2 blocked checks."
        updated["impact"] = "2 checks are blocked."
        updated["evidence"] = [{"source": "synthetic:test", "observation": "Observed 2 blocked checks."}]
        second = self.service.submit(updated, send=True)
        self.assertEqual((first["state"], second["state"]), ("queued", "queued"))
        self.assertNotEqual(first["dedupe_key"], second["dedupe_key"])
        self.assertEqual(self.service.submit(updated, send=True)["state"], "suppressed")

    def test_self_test_history_does_not_poison_regular_gate(self):
        self.service.self_test(send=True, confirmed=True)
        result = self.service.submit(proposal(), send=True)
        self.assertEqual(result["state"], "queued", result)

    def test_private_worker_diagnostic_is_available_but_not_exposed(self):
        self.plugin(body="def build(context):\n    raise RuntimeError('SYNTHETIC_PRIVATE_DETAIL')\n")
        result = self.service.run("future")
        self.assertFalse(result["ok"])
        self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL", json.dumps(result))
        diagnostics = list((self.path.parent / "diagnostics").glob("plugin-*.json"))
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("SYNTHETIC_PRIVATE_DETAIL", diagnostics[0].read_text())

    def test_real_canonical_queue_stages_archive_and_preserves_original(self):
        config = self.service.config
        directory = Path(config["artifact_root"]) / "future"
        directory.mkdir(parents=True)
        original = directory / "proof.txt"
        original.write_text("Synthetic artifact; not a real finding.")
        value = dict(proposal(), artifacts=[str(original)])
        service = Launchpad(self.path, transport=CanonicalTransport(config), clock=lambda: NOW)
        receipt = service.submit(value, send=True)
        self.assertEqual(receipt["state"], "queued")
        queued = json.loads((self.home / "state/outbox.jsonl").read_text().splitlines()[0])
        staged = Path(queued["attachments"][0])
        self.assertTrue(staged.is_file())
        with zipfile.ZipFile(staged) as archive:
            self.assertEqual(archive.read("proof.txt"), original.read_bytes())
        script = "import outbox; outbox._send=lambda *args:(True,'synthetic unverified'); assert outbox.drain()[0]==1"
        subprocess.run(
            [sys.executable, "-c", script], cwd=canonical_source(),
            env=dict(os.environ, SENTINEL_HOME=str(self.home)), check=True,
            capture_output=True, timeout=30,
        )
        self.assertFalse(staged.exists())
        self.assertTrue(original.is_file())
