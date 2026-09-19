"""Verified local receipt frames, adapted from rapp-sentinel alert_ledger.py.

MIT Copyright (c) 2026 Kody Wildfeuer. Unlike the alert path, Launchpad fails
closed on ledger errors, and pins a separate durable head against truncation.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ._vendor import rapp as R
from ._vendor.canonical import filelock
from .errors import BusyError, IntegrityError
from .protocol import RECEIPT_SCHEMA, RECEIPT_STATES
from .util import atomic_json, fsync_dir, private_dir, read_json, strict_json, utc_now

MAX_LEDGER_BYTES = 64 * 1024 * 1024


class Ledger:
    def __init__(self, home: Path):
        self.root = Path(home) / "state" / "launchpad"
        self.path = self.root / "receipts.jsonl"
        self.anchor = self.root / "receipts.head.json"
        self.lock_path = self.root / "service.lock"

    @contextmanager
    def locked(self, timeout=10):
        private_dir(self.root)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + timeout
            while not filelock.lock_nb(handle):
                if time.monotonic() >= deadline:
                    raise BusyError("another Launchpad producer holds the shared runtime lock")
                time.sleep(0.025)
            try:
                yield self
            finally:
                filelock.unlock(handle)

    def _head(self):
        try:
            anchor = read_json(self.anchor)
        except (ValueError, OSError):
            raise IntegrityError("receipt head is unreadable; sending is blocked") from None
        if anchor is None:
            if self.path.exists() and self.path.stat().st_size:
                raise IntegrityError("receipt head is missing; refusing an unanchored ledger")
            anchor = {"stream_id": "local:launchpad:" + uuid.uuid4().hex, "seq": -1, "frame_hash": None}
            atomic_json(self.anchor, anchor)
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"stream_id", "seq", "frame_hash"}
            or not isinstance(anchor["stream_id"], str)
            or not anchor["stream_id"].startswith("local:launchpad:")
            or type(anchor["seq"]) is not int
            or anchor["seq"] < -1
        ):
            raise IntegrityError("invalid receipt head")
        return anchor

    def load_unlocked(self):
        anchor = self._head()
        try:
            if self.path.is_symlink():
                raise IntegrityError("receipt ledger must not be a symbolic link")
            if self.path.exists() and self.path.stat().st_size > MAX_LEDGER_BYTES:
                raise IntegrityError("receipt ledger reached its bounded size; archive with an anchored migration")
            raw = self.path.read_bytes() if self.path.exists() else b""
            if raw and not raw.endswith(b"\n"):
                raise IntegrityError("receipt ledger has a torn final frame")
            frames = []
            for line in raw.splitlines():
                frame = strict_json(line)
                if not isinstance(frame, dict):
                    raise IntegrityError("receipt frame must be an object")
                ok, step, _ = R.verify_frame(
                    frame, head=frames[-1] if frames else None,
                    stream_id_of_record=anchor["stream_id"],
                )
                if not ok:
                    raise IntegrityError(f"receipt chain verification failed at step {step}")
                p = frame["payload"]
                if p.get("schema") != RECEIPT_SCHEMA or p.get("state") not in RECEIPT_STATES:
                    raise IntegrityError("unsupported receipt payload")
                frames.append(frame)
        except (ValueError, OSError, KeyError, TypeError, RecursionError, UnicodeError):
            raise IntegrityError("receipt ledger is malformed or unreadable; sending is blocked") from None
        seq = anchor["seq"]
        if seq >= len(frames):
            raise IntegrityError("receipt ledger was truncated behind its durable head")
        if seq >= 0 and frames[seq]["frame_hash"] != anchor["frame_hash"]:
            raise IntegrityError("receipt ledger disagrees with its durable head")
        if seq == -1 and anchor["frame_hash"] is not None:
            raise IntegrityError("invalid empty receipt head")
        if frames and seq < len(frames) - 1:
            self._pin(frames[-1])
        return frames

    def _pin(self, frame):
        atomic_json(self.anchor, {
            "stream_id": frame["stream_id"], "seq": frame["seq"],
            "frame_hash": frame["frame_hash"],
        })

    def append_unlocked(self, payload, frames=None):
        frames = self.load_unlocked() if frames is None else frames
        if payload.get("schema") != RECEIPT_SCHEMA or payload.get("state") not in RECEIPT_STATES:
            raise IntegrityError("refusing an invalid receipt payload")
        head = frames[-1] if frames else None
        anchor = self._head()
        stamp = max(utc_now(), head["utc"]) if head else utc_now()
        frame = R.build_frame(
            "launchpad.receipt", anchor["stream_id"], len(frames), stamp, payload,
            prev=head["payload_hash"] if head else None,
        )
        ok, _, _ = R.verify_frame(frame, head=head, stream_id_of_record=anchor["stream_id"])
        if not ok:
            raise IntegrityError("new receipt frame failed reference verification")
        encoded = (json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > 262_144:
            raise IntegrityError("receipt frame exceeds 256 KiB")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(self.path), flags, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(self.root)
        self._pin(frame)
        frames.append(frame)
        return payload

    def append(self, payload):
        with self.locked():
            return self.append_unlocked(payload)

    def load(self):
        with self.locked():
            return self.load_unlocked()

    def verify(self):
        frames = self.load()
        return {
            "ok": True, "frames": len(frames),
            "head": frames[-1]["frame_hash"] if frames else None,
            "profile": "RAPP/1-derived unsigned local frames (not full RAPP conformance)",
        }
