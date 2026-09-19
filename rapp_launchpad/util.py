from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

MAX_JSON_BYTES = 1_048_576


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp must be an ISO 8601 string with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def strict_json(raw: str | bytes, *, max_bytes=MAX_JSON_BYTES) -> Any:
    if len(raw) > max_bytes:
        raise ValueError("JSON exceeds the configured byte limit")
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
    )


def read_json(path: Path, default=None):
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("JSON file exceeds the size limit")
        return strict_json(path.read_bytes())
    except FileNotFoundError:
        return default


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError("private state directory must be a real directory")
    path.chmod(0o700)
    return path


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value) -> None:
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    atomic_bytes(path, raw)


def atomic_bytes(path: Path, raw: bytes) -> None:
    if path.is_symlink():
        raise ConfigurationError("refusing to replace a symbolic-link configuration")
    if path.parent.is_symlink():
        raise ConfigurationError("configuration directory must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".next")
    try:
        fd = os.open(str(staging), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        fsync_dir(path.parent)
    finally:
        if staging.exists():
            staging.unlink()


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
