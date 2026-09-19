from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from .config import default_config_path, detect_existing
from .errors import LaunchpadError
from .util import read_json

EXISTING_DRAINER = "com.rapp.storykeeper.outbox-drain"


def launchd_status(label):
    if sys.platform != "darwin":
        return {"label": label, "loaded": False, "state": "unsupported", "pid": None}
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=5,
        )
        state = re.search(r"^\s*state = ([a-z ]+)$", result.stdout, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE)
        exit_status = re.search(r"^\s*last exit code = (-?\d+)$", result.stdout, re.MULTILINE)
        return {
            "label": label, "loaded": result.returncode == 0,
            "state": state.group(1) if state else "not-loaded",
            "pid": int(pid.group(1)) if pid else None,
            "last_exit_code": int(exit_status.group(1)) if exit_status else None,
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"label": label, "loaded": None, "state": "unavailable", "pid": None}


def diagnostics(service=None, config_path=None):
    checks = [
        {
            "id": "platform", "status": "pass" if sys.platform == "darwin" else "blocked",
            "title": "macOS sender" if sys.platform == "darwin" else "Preview-only platform",
            "detail": "iMessage sending is macOS-only. Windows/Linux can validate proposals and inspect receipts.",
        },
        {
            "id": "python", "status": "pass" if sys.version_info >= (3, 9) else "blocked",
            "title": "Python " + platform.python_version(),
            "detail": "A local Python 3.9+ runtime is required; it is not bundled.",
        },
    ]
    result = {
        "configured": service is not None, "platform": sys.platform,
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "config_path": str(config_path or default_config_path()),
        "existing_available": detect_existing() is not None,
        "checks": checks, "automation": "unknown-until-explicit-self-test",
        "delivery_verification": "No Full Disk Access is required to SEND. Without additional evidence, delivery stays unverified.",
    }
    if service is None:
        checks.append({"id": "setup", "status": "blocked", "title": "Not configured", "detail": "Choose the detected service or configure a new Mac."})
        result["ready_to_queue"] = False
        return result
    config = service.config
    result.update({
        "home": config["home"], "state_location": str(service.ledger.root),
        "transport_mode": config["transport_mode"],
        "send_enabled": config["send_enabled"], "app_schedule": config["app_schedule"],
        "scenarios": service.plugins.catalog(),
        "source_entries": len(config["sources"]),
        "gate": "scenarios.interrupt.evaluate" if service.plugins.has_gate() else "built-in conservative gate",
    })
    source = Path(config["transport_source"]) / "outbox.py"
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result["transport_source"] = {"path": str(source.parent), "sha256": digest}
        checks.append({"id": "transport", "status": "pass", "title": "Canonical outbox located", "detail": "The existing MIT core is used in an isolated subprocess; no alternate Messages sender."})
    except OSError:
        checks.append({"id": "transport", "status": "blocked", "title": "Canonical source unavailable", "detail": "Restore the configured source location."})
    try:
        result["outbox"] = service.transport.snapshot()
        recipient = result["outbox"]["recipient_configured"]
        checks.append({
            "id": "recipient", "status": "pass" if recipient else "blocked",
            "title": "Recipient configured" if recipient else "Recipient missing or disabled",
            "detail": result["outbox"]["recipient_masked"] or "Set the recipient in the canonical runtime configuration.",
        })
        if result["outbox"]["quarantine_records"] or result["outbox"]["strict_recovery_marker"]:
            checks.append({
                "id": "canonical-integrity", "status": "blocked", "title": "Canonical recovery requires attention",
                "detail": "Unresolved canonical terminal/recovery evidence blocks safe dedupe-keyed sends. Launchpad will not acknowledge or rewrite it.",
            })
    except LaunchpadError:
        result["outbox"] = None
        checks.append({"id": "outbox", "status": "blocked", "title": "Canonical outbox unreadable", "detail": "No health or delivery claim can be made. Inspect the private canonical state before sending."})
    try:
        result["ledger"] = service.ledger.verify()
        result["policy"] = service.policy()
        checks.append({"id": "ledger", "status": "pass", "title": "Receipt chain verified", "detail": f'{result["ledger"]["frames"]} frames checked against the durable head. Unsigned local integrity, not third-party delivery proof.'})
    except LaunchpadError:
        result["ledger"] = {"ok": False}
        checks.append({"id": "ledger", "status": "blocked", "title": "Receipt integrity failure", "detail": "Sending is fail-closed. Do not delete or rewrite the ledger to hide the failure."})
    checks.append({
        "id": "automation", "status": "unknown", "title": "Messages permission not assumed",
        "detail": "Sign in to Messages. Use the explicit self-test to request Automation from the actual sending context. A background job cannot reliably display a permission prompt.",
    })
    checks.append({
        "id": "delivery", "status": "unknown", "title": "Delivery is a separate receipt",
        "detail": "Queued ≠ sent ≠ delivered. Exit code 0 is never delivery. Confirm receipt yourself or use additional canonical verifier evidence.",
    })
    result["drainer"] = launchd_status(EXISTING_DRAINER) if config["transport_mode"] == "existing" else {
        "label": None, "loaded": None, "state": "app-or-explicit-launchpad-schedule", "pid": None,
    }
    result["last_tick"] = read_json(service.ledger.root / "tick.json")
    result["ready_to_queue"] = not any(check["status"] == "blocked" for check in checks)
    return result
