from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .errors import ConfigurationError
from .protocol import SCENARIOS, SLUG
from .util import atomic_json, private_dir, read_json, utc_now

CONFIG_SCHEMA = "rapp-imessage-launchpad/config/1.0"
APP_NAME = "RAPP iMessage Launchpad"


def default_config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME / "config.json"


def package_scenarios() -> Path:
    return Path(__file__).resolve().parent.parent / "scenarios"


def canonical_source() -> Path:
    return Path(__file__).resolve().parent / "_vendor" / "canonical"


def detect_existing():
    source = Path.home() / ".storykeeper" / "code"
    home = Path.home() / ".storykeeper" / "home"
    if all((source / name).is_file() for name in ("outbox.py", "paths.py", "filelock.py")) and (home / "config.json").is_file():
        return {"source": str(source.resolve()), "home": str(home.resolve())}
    return None


def validate_source(path: Path):
    if not path.is_dir() or not all((path / n).is_file() for n in ("outbox.py", "paths.py", "filelock.py")):
        raise ConfigurationError("canonical source must contain outbox.py, paths.py, and filelock.py")


def load_config(path=None):
    path = Path(path or default_config_path()).expanduser().resolve()
    try:
        value = read_json(path)
    except (ValueError, OSError, UnicodeError):
        raise ConfigurationError("configuration is unreadable or malformed") from None
    if not isinstance(value, dict) or value.get("schema") != CONFIG_SCHEMA:
        raise ConfigurationError("Launchpad is not configured; complete onboarding or run configure")
    if value.get("transport_mode") not in ("existing", "portable"):
        raise ConfigurationError("unsupported transport mode")
    for key in ("home", "transport_source", "scenario_root"):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise ConfigurationError(f"{key} must be an absolute configured path")
    value.setdefault("artifact_root", str(path.parent / "artifacts"))
    artifacts = Path(value["artifact_root"]).resolve()
    home = Path(value["home"]).resolve()
    if not Path(value["artifact_root"]).is_absolute() or artifacts == home or home in artifacts.parents:
        raise ConfigurationError("artifact_root must be absolute and outside the read-only input home")
    allowed = value.get("allowed_scenarios")
    enabled = value.get("enabled_scenarios")
    if (
        not isinstance(allowed, list) or len(allowed) > 50
        or not all(isinstance(n, str) and SLUG.fullmatch(n) for n in allowed)
        or len(set(allowed)) != len(allowed)
        or not isinstance(enabled, list) or not set(enabled) <= set(allowed)
    ):
        raise ConfigurationError("invalid scenario allowlist")
    if type(value.get("send_enabled")) is not bool or type(value.get("app_schedule")) is not bool:
        raise ConfigurationError("send and app-schedule flags must be booleans")
    if not isinstance(value.get("sources"), dict):
        raise ConfigurationError("sources must be an explicitly configured object")
    if type(value.get("plugin_timeout")) is not int or not 1 <= value["plugin_timeout"] <= 120:
        raise ConfigurationError("plugin_timeout must be 1–120 seconds")
    return value


def validate_recipient(value):
    import re
    if not isinstance(value, str) or len(value) > 254 or not (
        re.fullmatch(r"\+[1-9][0-9]{6,14}", value)
        or re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value)
    ):
        raise ConfigurationError("recipient must be an international +number or an iMessage email address")
    return value


def configure(
    path=None, *, existing_source=None, home=None, recipient=None, portable=False,
    allow_send=False, scenario_root=None, allowed=None, enabled=None, sources=None,
):
    path = Path(path or default_config_path()).expanduser().resolve()
    if path.exists():
        raise ConfigurationError("configuration already exists; use settings, policy, or a distinct --config")
    if portable:
        if existing_source:
            raise ConfigurationError("portable and existing source are mutually exclusive")
        if detect_existing() and home is None:
            raise ConfigurationError("an existing canonical pipeline was detected; reuse it instead of creating a second home")
        home_path = Path(home).expanduser().resolve() if home else path.parent / "runtime"
        source = canonical_source()
        if (home_path / "config.json").exists():
            raise ConfigurationError("portable setup refuses to replace an existing runtime configuration")
        recipient = validate_recipient(recipient)
        private_dir(home_path)
        private_dir(home_path / "state")
        atomic_json(home_path / "config.json", {"notify": True, "notify_handle": recipient})
    else:
        found = detect_existing() if not existing_source and not home else None
        source = Path(existing_source or (found or {}).get("source", "")).expanduser().resolve()
        raw_home = home or (found or {}).get("home")
        if not raw_home:
            raise ConfigurationError("no existing pipeline detected; specify --existing-source and --home, or --portable")
        home_path = Path(raw_home).expanduser().resolve()
        if recipient is not None:
            raise ConfigurationError("existing recipient configuration is read-only; change it in the canonical service")
        if not (home_path / "config.json").is_file():
            raise ConfigurationError("existing runtime must already have config.json")
    validate_source(source)
    root = Path(scenario_root).expanduser().resolve() if scenario_root else package_scenarios()
    if not root.is_dir():
        raise ConfigurationError("scenario root does not exist")
    names = list(dict.fromkeys(allowed if allowed is not None else SCENARIOS))
    active = list(dict.fromkeys(enabled or []))
    if not all(isinstance(n, str) and SLUG.fullmatch(n) for n in names) or not set(active) <= set(names):
        raise ConfigurationError("enabled scenarios must be in the configured allowlist")
    value = {
        "schema": CONFIG_SCHEMA, "home": str(home_path), "transport_source": str(source),
        "transport_mode": "portable" if portable else "existing",
        "scenario_root": str(root), "allowed_scenarios": names, "enabled_scenarios": active,
        "sources": sources or {}, "send_enabled": bool(allow_send),
        "artifact_root": str(path.parent / "artifacts"),
        "app_schedule": False, "plugin_timeout": 40, "configured_at": utc_now(),
    }
    json.dumps(value, allow_nan=False)
    atomic_json(path, value)
    return value
