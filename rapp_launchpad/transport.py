from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import PluginError, TransportError
from .processes import run_json


class CanonicalTransport:
    def __init__(self, config):
        self.config = config

    def _call(self, action, **fields):
        request = {
            "action": action, "source": self.config["transport_source"],
            "home": self.config["home"], **fields,
        }
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if sys.platform == "darwin":
            env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        try:
            result = run_json(
                [sys.executable, "-I", "-B", Path(__file__).with_name("_transport_worker.py")],
                request, timeout=115 if action == "drain" else 45, env=env,
            )
        except (PluginError, OSError):
            raise TransportError("canonical outbox worker failed or timed out; outcome may be unknown") from None
        if result.get("ok") is not True:
            raise TransportError("canonical outbox refused the operation; no success or delivery is assumed")
        return result

    def enqueue(self, text, dedupe_key, attachments=None):
        return self._call(
            "enqueue", text=text, dedupe_key=dedupe_key,
            attachments=list(attachments or []),
            staging_root=str(Path(self.config["home"]) / "state" / "reports" / "launchpad"),
        )

    def snapshot(self, keys=()):
        return self._call("snapshot", keys=list(keys))

    def drain(self, limit=1):
        if self.config["transport_mode"] != "portable":
            raise TransportError("the existing pipeline owns its drainer; use its existing service")
        if sys.platform != "darwin":
            raise TransportError("iMessage sending is only supported on macOS")
        return self._call("drain", portable=True, limit=limit)
