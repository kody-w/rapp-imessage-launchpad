from __future__ import annotations

import hashlib
import json
import secrets
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

from .config import default_config_path, load_config
from .errors import ConfigurationError, LaunchpadError, PluginError, TransportError
from .gate import DEFAULT_POLICY, evaluate, render, validate_message, validate_policy
from .ledger import Ledger
from .plugins import Plugins
from .protocol import RECEIPT_SCHEMA, validate_proposal
from .transport import CanonicalTransport
from .util import atomic_json, parse_time, private_dir, read_json, utc_now


class Launchpad:
    def __init__(self, config_path=None, *, transport=None, clock=None):
        self.config_path = Path(config_path or default_config_path()).expanduser().resolve()
        self.config = load_config(self.config_path)
        self.ledger = Ledger(Path(self.config["home"]))
        self.transport = transport or CanonicalTransport(self.config)
        self.plugins = Plugins(self.config)
        self.clock = clock or utc_now

    def _refresh_config(self):
        current = load_config(self.config_path)
        if any(current[key] != self.config[key] for key in ("home", "transport_source", "transport_mode")):
            raise ConfigurationError("transport binding changed; create a new Launchpad client before continuing")
        self.config = current
        self.plugins = Plugins(current)

    def _policy_unlocked(self):
        path = self.ledger.root / "policy.json"
        value = read_json(path)
        if value is None:
            value = validate_policy(DEFAULT_POLICY)
            atomic_json(path, value)
        return validate_policy(value)

    def policy(self, value=None):
        with self.ledger.locked():
            self.ledger.load_unlocked()
            if value is not None:
                atomic_json(self.ledger.root / "policy.json", validate_policy(value))
            return self._policy_unlocked()

    @staticmethod
    def _latest(frames):
        latest = {}
        for frame in frames:
            row = frame["payload"]
            latest.pop(row["id"], None)
            latest[row["id"]] = row
        return latest

    @staticmethod
    def _history(frames):
        return [
            {
                "at": row["at"], "scenario": row["scenario"], "fingerprint": row["fingerprint"],
                "decision": row["decision"], "proposal": row.get("proposal") or {},
            }
            for row in (frame["payload"] for frame in frames)
            if row.get("decision") in ("queued", "suppressed", "error")
        ]

    def _record(self, frames, *, state, scenario, fingerprint, reason, proposal=None, **fields):
        row = {
            "schema": RECEIPT_SCHEMA, "id": uuid.uuid4().hex,
            "event_id": uuid.uuid4().hex, "at": self.clock(), "observed_at": self.clock(),
            "state": state, "scenario": scenario, "fingerprint": fingerprint,
            "decision": state if state in ("queued", "suppressed", "error") else None,
            "reason": reason, "proposal": proposal, **fields,
        }
        self.ledger.append_unlocked(row, frames)
        return row

    def _reconcile_unlocked(self, frames):
        rows = [
            row for row in self._latest(frames).values()
            if row.get("dedupe_key") and row["state"] in ("intent", "queued", "sent_unverified", "unknown")
        ]
        if not rows:
            return
        matches = {}
        for offset in range(0, len(rows), 500):
            snapshot = self.transport.snapshot([row["dedupe_key"] for row in rows[offset:offset + 500]])
            matches.update(snapshot["matches"])
        previously_queued = {f["payload"]["id"] for f in frames if f["payload"].get("decision") == "queued"}
        for row in rows:
            match = matches.get(row["dedupe_key"])
            if not match:
                if row["state"] in ("intent", "unknown"):
                    self._record(
                        frames, id=row["id"], state="error", scenario=row["scenario"],
                        fingerprint=row["fingerprint"], proposal=row["proposal"],
                        reason="enqueue was not found in the canonical queue or terminal ledgers",
                        dedupe_key=row["dedupe_key"],
                    )
                continue
            if row["id"] not in previously_queued and match["state"] != "error":
                at = match.get("at") or row["at"]
                parse_time(at)
                self._record(
                    frames, id=row["id"], at=at, state="queued", scenario=row["scenario"],
                    fingerprint=row["fingerprint"], proposal=row["proposal"],
                    reason="recovered durable canonical enqueue after an interrupted producer",
                    dedupe_key=row["dedupe_key"], transport=match,
                )
                previously_queued.add(row["id"])
                if match["state"] == "queued":
                    continue
            if match["state"] != row["state"]:
                self._record(
                    frames, id=row["id"], state=match["state"], decision=None,
                    scenario=row["scenario"], fingerprint=row["fingerprint"],
                    proposal=row["proposal"], reason=match["reason"], transport=match,
                    dedupe_key=row["dedupe_key"],
                )

    def _gate(self, proposal, history, now, policy):
        if proposal["status"] != "ready":
            return {"allow": False, "reason": proposal["reason"], "fingerprint": proposal["fingerprint"]}
        if proposal["scenario"] != "self-test":
            if not self.plugins.has_gate():
                raise PluginError("the canonical interruption gate is missing; submission is disabled")
            result = self.plugins.evaluate(proposal, history, now, policy)
        else:
            result = evaluate(proposal, history, now, policy)
        if (
            not isinstance(result, dict) or type(result.get("allow")) is not bool
            or not isinstance(result.get("reason"), str) or not 1 <= len(result["reason"]) <= 4096
            or not isinstance(result.get("fingerprint"), str)
            or not 1 <= len(result["fingerprint"]) <= 512
        ):
            raise PluginError("shared interruption gate returned an invalid result")
        return result

    def _render(self, proposal):
        text = None
        if self.plugins.has_gate() and proposal["scenario"] != "self-test":
            text = self.plugins.render(proposal)
        return validate_message(render(proposal) if text is None else text)

    def _stage_artifacts(self, proposal, token):
        if not proposal["artifacts"]:
            return []
        root = Path(self.config["artifact_root"]) / proposal["scenario"]
        validate_proposal(proposal, artifact_dir=root)
        staging = private_dir(Path(self.config["home"]) / "state" / "reports" / "launchpad")
        directory = Path(tempfile.mkdtemp(prefix="send-", dir=staging))
        bundle = directory / ("rapp-" + proposal["scenario"] + "-" + token[:12] + ".zip")
        remaining = 8 * 1024 * 1024
        try:
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for raw in proposal["artifacts"]:
                    path = Path(raw).resolve()
                    with path.open("rb") as handle:
                        data = handle.read(remaining + 1)
                    remaining -= len(data)
                    if remaining < 0:
                        raise ConfigurationError("artifact bundle exceeds the 8 MiB input limit")
                    archive.writestr(str(path.relative_to(root.resolve())), data)
            bundle.chmod(0o600)
        except Exception:
            if bundle.exists():
                bundle.unlink()
            directory.rmdir()
            raise
        return [str(bundle)]

    def submit(self, proposal, *, send=False, _self_test=False):
        if type(send) is not bool or type(_self_test) is not bool:
            raise ConfigurationError("send flags must be explicit booleans")
        proposal = validate_proposal(proposal)
        self._refresh_config()
        if proposal["scenario"] not in self.config["allowed_scenarios"] and not (
            _self_test and proposal["scenario"] == "self-test"
        ):
            raise ConfigurationError("proposal scenario is not explicitly allowlisted")
        if proposal["scenario"] == "self-test" and not _self_test:
            raise ConfigurationError("self-test is reserved for the explicit onboarding action")
        if send and sys.platform != "darwin":
            raise ConfigurationError("iMessage sends require macOS; dry-run works on this platform")
        if send and not (self.config["send_enabled"] or _self_test):
            raise ConfigurationError("sending is disabled; enable it explicitly in settings or configure --allow-send")
        now = self.clock()
        parse_time(now)
        with self.ledger.locked(timeout=60):
            self._refresh_config()
            if send and not (self.config["send_enabled"] or _self_test):
                raise ConfigurationError("send consent was revoked before submission")
            if proposal["scenario"] not in self.config["allowed_scenarios"] and not _self_test:
                raise ConfigurationError("scenario was removed from the configured allowlist")
            frames = self.ledger.load_unlocked()
            self._reconcile_unlocked(frames)
            history = self._history(frames)
            policy = self._policy_unlocked()
            result = self._gate(proposal, history, now, policy)
            fingerprint = proposal["fingerprint"]
            allow = result["allow"] and proposal["status"] == "ready"
            reason = result["reason"]
            if not allow:
                return self._record(
                    frames, state="suppressed", scenario=proposal["scenario"],
                    fingerprint=fingerprint, reason=reason, proposal=proposal, dry_run=not send,
                )
            try:
                text = self._render(proposal)
            except LaunchpadError as exc:
                return self._record(
                    frames, state="error", scenario=proposal["scenario"],
                    fingerprint=fingerprint, reason=str(exc), proposal=proposal,
                    error_code=exc.code,
                )
            if not send:
                return self._record(
                    frames, state="dry_run", scenario=proposal["scenario"], fingerprint=fingerprint,
                    reason="would queue; dry-run has no transport side effect", proposal=proposal,
                    would_queue=True, message_preview=text,
                )
            if proposal["scenario"] == "self-test":
                token = hashlib.sha256((proposal["scenario"] + "\0" + fingerprint).encode()).hexdigest()
            else:
                token = self.plugins.delivery_key(proposal)
                if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                    raise PluginError("canonical gate returned an invalid delivery-version key")
            attachments = self._stage_artifacts(proposal, token)
            key = "launchpad/1/" + token
            intent = self._record(
                frames, state="intent", scenario=proposal["scenario"], fingerprint=fingerprint,
                reason="durable enqueue intent, not a send or delivery", proposal=proposal, dedupe_key=key,
            )
            try:
                response = self.transport.enqueue(text, key, attachments=attachments)
                match = response.get("receipt")
                if not match:
                    raise TransportError("canonical enqueue returned no durable matching queue or terminal evidence")
                if match["state"] == "error":
                    return self._record(
                        frames, id=intent["id"], state="error", scenario=proposal["scenario"],
                        fingerprint=fingerprint, reason=match["reason"], proposal=proposal,
                        dedupe_key=key, transport=match,
                    )
                receipt = self._record(
                    frames, id=intent["id"], state="queued", scenario=proposal["scenario"],
                    fingerprint=fingerprint, reason="durably accepted by the one canonical outbox",
                    proposal=proposal, dedupe_key=key, transport=match,
                )
                if match["state"] != "queued":
                    receipt = self._record(
                        frames, id=intent["id"], state=match["state"], decision=None,
                        scenario=proposal["scenario"], fingerprint=fingerprint, proposal=proposal,
                        reason=match["reason"], dedupe_key=key, transport=match,
                    )
                return receipt
            except TransportError:
                return self._record(
                    frames, id=intent["id"], state="unknown", decision="error",
                    scenario=proposal["scenario"], fingerprint=fingerprint, proposal=proposal,
                    dedupe_key=key, reason="enqueue outcome is uncertain; reconciliation is required before another send",
                )

    def run(self, name="all", *, send=False):
        if type(send) is not bool or not isinstance(name, str):
            raise ConfigurationError("run requires a scenario name and an explicit boolean send flag")
        self._refresh_config()
        if name == "all":
            names = self.config["enabled_scenarios"] if send else [
                item["name"] for item in self.plugins.catalog() if item["installed"]
            ]
        else:
            names = [name]
        receipts = []
        for scenario in names:
            try:
                proposal = self.plugins.build(scenario, self.clock())
                receipts.append(self.submit(proposal, send=send))
            except LaunchpadError as exc:
                with self.ledger.locked():
                    frames = self.ledger.load_unlocked()
                    receipts.append(self._record(
                        frames, state="error", scenario=scenario, fingerprint="plugin-error:" + scenario,
                        reason=str(exc), error_code=exc.code,
                    ))
        return {
            "ok": all(row["state"] not in ("error", "unknown") for row in receipts),
            "mode": "send" if send else "dry-run", "receipts": receipts,
            "reason": None if names else "No installed/enabled scenarios; silence is not a finding.",
        }

    def receipts(self, limit=50, *, refresh=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ConfigurationError("receipt limit must be 1–200")
        with self.ledger.locked(timeout=60):
            frames = self.ledger.load_unlocked()
            if refresh:
                self._reconcile_unlocked(frames)
            selected, size = [], 0
            for row in list(self._latest(frames).values())[::-1]:
                encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
                if len(selected) == limit or size + len(encoded) > 750_000:
                    break
                selected.append(row)
                size += len(encoded)
            return selected

    def confirm(self, receipt_id, *, received=False):
        if received is not True:
            raise ConfigurationError("confirmation requires an explicit human statement of receipt")
        with self.ledger.locked():
            frames = self.ledger.load_unlocked()
            row = self._latest(frames).get(receipt_id)
            if not row or row["state"] not in ("queued", "sent_unverified", "unknown", "user_confirmed"):
                raise ConfigurationError("only an existing queued/uncertain receipt can be human-confirmed")
            if row["state"] == "user_confirmed":
                return row
            return self._record(
                frames, id=row["id"], state="user_confirmed", scenario=row["scenario"],
                fingerprint=row["fingerprint"], proposal=row["proposal"],
                dedupe_key=row.get("dedupe_key"), confirmed_by="explicit-local-user",
                reason="the user explicitly confirmed receipt; this is not machine-verified delivery",
            )

    def self_test(self, *, send=False, confirmed=False):
        if send is not True or confirmed is not True:
            raise ConfigurationError("self-test sends only with both --send and --confirm")
        snapshot = self.transport.snapshot()
        if not snapshot.get("recipient_configured"):
            raise ConfigurationError("configure the canonical recipient first")
        if self.config["transport_mode"] == "portable" and snapshot["counts"]["queued"]:
            raise ConfigurationError("portable self-test requires an empty queue; inspect pending items first")
        token = secrets.token_hex(3).upper()
        proposal = {
            "scenario": "self-test", "status": "ready", "title": "Launchpad self-test " + token,
            "change": "You explicitly requested this onboarding message.",
            "impact": "This checks the configured sender path, not automated delivery proof.",
            "action": "If received, confirm this code in Launchpad: " + token,
            "decision": "One user-requested onboarding test, not a scenario finding.",
            "evidence": [{"source": "local user action", "observation": "Explicitly confirmed self-test."}],
            "artifacts": [], "fingerprint": "self-test:" + self.clock()[:10],
            "urgency": "routine", "reason": "explicit onboarding consent",
        }
        receipt = self.submit(proposal, send=True, _self_test=True)
        result = {"receipt": receipt, "code": token, "drain": None}
        if receipt["state"] == "queued" and self.config["transport_mode"] == "portable":
            result["drain"] = self.transport.drain(limit=1)
            self.receipts(refresh=True)
        return result

    def settings(self, *, send_enabled=None, app_schedule=None, enabled_scenarios=None):
        with self.ledger.locked():
            value = load_config(self.config_path)
            for key, setting in (("send_enabled", send_enabled), ("app_schedule", app_schedule)):
                if setting is not None:
                    if type(setting) is not bool:
                        raise ConfigurationError("setting flags must be booleans")
                    if setting and sys.platform != "darwin":
                        raise ConfigurationError("automatic iMessage sending is macOS-only")
                    value[key] = setting
            if enabled_scenarios is not None:
                if not isinstance(enabled_scenarios, list) or not set(enabled_scenarios) <= set(value["allowed_scenarios"]):
                    raise ConfigurationError("enabled scenarios must be allowlisted")
                value["enabled_scenarios"] = list(dict.fromkeys(enabled_scenarios))
            if value["app_schedule"] and not value["send_enabled"]:
                raise ConfigurationError("app scheduling requires explicit send consent")
            atomic_json(self.config_path, value)
            self.config = value
            self.plugins = Plugins(value)
        return {"send_enabled": value["send_enabled"], "app_schedule": value["app_schedule"], "enabled_scenarios": value["enabled_scenarios"]}

    def sources(self, value):
        if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 262_144:
            raise ConfigurationError("source configuration must be a JSON object no larger than 256 KiB")
        with self.ledger.locked():
            config = load_config(self.config_path)
            config["sources"] = value
            atomic_json(self.config_path, config)
            self.config = config
            self.plugins = Plugins(config)
        return {"entries": len(value), "detail": "Private source configuration replaced; contents are not returned to the renderer."}
