from __future__ import annotations

import sys
import uuid
from pathlib import Path

from .errors import PluginError
from .config import package_scenarios
from .gate import canonical_policy
from .processes import run_json
from .protocol import SLUG, validate_proposal
from .util import atomic_json, private_dir


class Plugins:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["scenario_root"])

    def catalog(self):
        return [
            {
                "name": name, "installed": (self.root / (name + ".py")).is_file(),
                "enabled": name in self.config["enabled_scenarios"],
            }
            for name in self.config["allowed_scenarios"]
        ]

    def _call(self, name, action, **fields):
        gate_action = name == "interrupt" and action in ("gate", "render", "delivery-key")
        if (name not in self.config["allowed_scenarios"] and not gate_action) or not SLUG.fullmatch(name):
            raise PluginError("scenario is not in the explicit configured allowlist")
        root = self.root
        if gate_action and not (root / "interrupt.py").is_file():
            root = package_scenarios()
        result = run_json(
            [sys.executable, "-I", "-B", Path(__file__).with_name("_plugin_worker.py")],
            {"name": name, "root": str(root), "action": action,
             "allowed_scenarios": self.config["allowed_scenarios"], **fields},
            timeout=self.config["plugin_timeout"],
            max_input=64 * 1024 * 1024 if action == "gate" else 1_048_576,
        )
        if result.get("ok") is not True:
            diagnostic = "plugin-" + uuid.uuid4().hex + ".json"
            directory = private_dir(Path(self.config["artifact_root"]).parent / "diagnostics")
            atomic_json(directory / diagnostic, {
                "scenario": name, "action": action, "error": result,
            })
            raise PluginError(
                "scenario failed; no message was submitted. Private diagnostic: " + diagnostic
            )
        return result["result"]

    def build(self, name, now):
        if name not in self.config["allowed_scenarios"]:
            raise PluginError("scenario is not allowlisted")
        artifacts = private_dir(Path(self.config["artifact_root"]) / name)
        context = {
            "home": self.config["home"], "artifact_dir": str(artifacts),
            "now": now, "sources": self.config["sources"],
            "transport_source": self.config["transport_source"],
        }
        proposal = validate_proposal(self._call(name, "build", context=context), artifact_dir=artifacts)
        if proposal["scenario"] != name:
            raise PluginError("plugin returned a proposal for a different scenario")
        return proposal

    def has_gate(self):
        return (self.root / "interrupt.py").is_file() or (package_scenarios() / "interrupt.py").is_file()

    def evaluate(self, proposal, history, now, policy):
        return self._call(
            "interrupt", "gate", proposal=proposal, history=history, now=now,
            policy=canonical_policy(policy),
        )

    def render(self, proposal):
        return self._call("interrupt", "render", proposal=proposal)

    def delivery_key(self, proposal):
        return self._call("interrupt", "delivery-key", proposal=proposal)
