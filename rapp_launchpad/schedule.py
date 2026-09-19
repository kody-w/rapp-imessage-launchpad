from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from ._vendor.canonical import filelock
from .diagnostics import launchd_status
from .errors import ConfigurationError
from .util import atomic_bytes, atomic_json, private_dir, utc_now

LABEL = "com.rapp.imessage-launchpad.scheduler"


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def _owned(path, config_path):
    if path.is_symlink():
        return False
    try:
        value = plistlib.loads(path.read_bytes())
        args = value["ProgramArguments"]
        config = args[args.index("--config") + 1]
        return value.get("Label") == LABEL and Path(config).resolve() == config_path.resolve()
    except (OSError, ValueError, KeyError, TypeError, IndexError, plistlib.InvalidFileException):
        return False


def status(service):
    path = plist_path()
    return {
        **launchd_status(LABEL), "installed": path.is_file(),
        "owned_by_config": _owned(path, service.config_path) if path.exists() else False,
        "plist": str(path), "drains_outbox": service.config["transport_mode"] == "portable",
    }


def _launchctl(*args, allow_missing=False):
    try:
        result = subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigurationError("launchctl is unavailable or timed out") from None
    if result.returncode and not allow_missing:
        raise ConfigurationError("launchctl refused the scoped job operation; inspect schedule status")


def install(service, *, interval=300, send=False):
    if sys.platform != "darwin":
        raise ConfigurationError("persistent LaunchAgent scheduling is macOS-only")
    if send is not True or not service.config["send_enabled"]:
        raise ConfigurationError("schedule installation requires --send and explicit send-enabled consent")
    if not service.config["enabled_scenarios"]:
        raise ConfigurationError("enable at least one installed scenario before scheduling")
    if type(interval) is not int or not 300 <= interval <= 86400:
        raise ConfigurationError("schedule interval must be 300–86400 seconds")
    if not all(item["installed"] for item in service.plugins.catalog() if item["enabled"]):
        raise ConfigurationError("an enabled scenario is not installed")
    path = plist_path()
    if path.exists() and not _owned(path, service.config_path):
        raise ConfigurationError("refusing to overwrite a job belonging to a different configuration")
    root = private_dir(service.ledger.root)
    value = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable, "-I", "-B", str(Path(__file__).with_name("_cli_entry.py")),
            "--config", str(service.config_path), "tick", "--send",
        ],
        "WorkingDirectory": str(root), "StartInterval": interval, "RunAtLoad": False,
        "ProcessType": "Background", "ThrottleInterval": 60,
        "StandardOutPath": str(root / "scheduler.stdout.log"),
        "StandardErrorPath": str(root / "scheduler.stderr.log"),
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
    }
    # Only our fixed job is replaced. Storykeeper's source, config, and jobs
    # remain untouched; its already-installed outbox drainer is never duplicated.
    if path.exists():
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(path, plistlib.dumps(value))
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    service.settings(app_schedule=False)
    result = status(service)
    if not result["loaded"]:
        raise ConfigurationError("schedule file was written, but the job is not loaded")
    return result


def uninstall(service):
    if sys.platform != "darwin":
        raise ConfigurationError("LaunchAgent scheduling is macOS-only")
    path = plist_path()
    if not path.exists():
        return status(service)
    if not _owned(path, service.config_path):
        raise ConfigurationError("refusing to remove a job belonging to a different configuration")
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", allow_missing=True)
    path.unlink()
    return status(service)


def tick(service, *, send=False):
    if send is not True:
        raise ConfigurationError("tick requires explicit --send; use run all for a dry-run")
    if not service.config["send_enabled"]:
        raise ConfigurationError("sending is disabled")
    private_dir(service.ledger.root)
    fd = os.open(str(service.ledger.root / "tick.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        if not filelock.lock_nb(handle):
            return {"ok": True, "skipped": "another bounded tick is already running"}
        try:
            start = utc_now()
            result = service.run("all", send=True)
            drain = None
            if service.config["transport_mode"] == "portable":
                drain = service.transport.drain(limit=3)
            marker = {
                "started_at": start, "finished_at": utc_now(), "ok": result["ok"],
                "considered": len(result["receipts"]),
                "queued": sum(row["state"] == "queued" for row in result["receipts"]),
                "drainer": "canonical portable core" if drain else "existing service owns drain",
            }
            atomic_json(service.ledger.root / "tick.json", marker)
            return {"ok": result["ok"], "tick": marker, "drain": drain}
        finally:
            filelock.unlock(handle)
