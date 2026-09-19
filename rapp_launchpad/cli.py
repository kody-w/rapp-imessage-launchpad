from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import __version__
from .config import configure, default_config_path, detect_existing
from .diagnostics import diagnostics
from .errors import ConfigurationError, LaunchpadError
from .protocol import SCENARIOS, validate_proposal
from .service import Launchpad
from .util import read_json, strict_json


def _stdin():
    value = strict_json(sys.stdin.buffer.read(1_048_577))
    if not isinstance(value, dict):
        raise ConfigurationError("stdin request must be a JSON object")
    return value


def parser():
    p = argparse.ArgumentParser(prog="rapp-launchpad", description="One evidence-first canonical iMessage pipeline. Dry-run by default.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, default=default_config_path(), help="private configuration file (never commit it)")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("configure", help="bind the existing service or explicitly initialize a new Mac")
    c.add_argument("--existing-source", type=Path)
    c.add_argument("--home", type=Path)
    c.add_argument("--portable", action="store_true")
    c.add_argument("--recipient-stdin", action="store_true", help="read one recipient line without putting it in process arguments")
    c.add_argument("--allow-send", action="store_true")
    c.add_argument("--scenario-root", type=Path)
    c.add_argument("--allow-scenario", action="append", default=[])
    c.add_argument("--enable", action="append", default=[])
    c.add_argument("--sources-json", type=Path)
    sub.add_parser("setup", help="native onboarding bridge; reads a narrow JSON request").add_argument("--stdin", action="store_true", required=True)
    for name in ("diagnostics", "status", "verify"):
        sub.add_parser(name)
    run = sub.add_parser("run")
    run.add_argument("scenario", nargs="?", default="all")
    run.add_argument("--send", action="store_true")
    run.add_argument("--summary", action="store_true", help="return bounded receipt summaries without proposal bodies")
    submit = sub.add_parser("submit", help="validate a proposal file and apply the shared gate")
    submit.add_argument("proposal", type=Path)
    submit.add_argument("--send", action="store_true")
    valid = sub.add_parser("validate")
    valid.add_argument("proposal", type=Path)
    receipts = sub.add_parser("receipts")
    receipts.add_argument("--limit", type=int, default=50)
    receipts.add_argument("--refresh", action="store_true")
    st = sub.add_parser("self-test", help="one clearly labeled, user-requested onboarding message")
    st.add_argument("--send", action="store_true")
    st.add_argument("--confirm", action="store_true")
    conf = sub.add_parser("confirm")
    conf.add_argument("receipt_id")
    conf.add_argument("--received", action="store_true")
    conf.add_argument("--yes", action="store_true")
    policy = sub.add_parser("policy")
    policy.add_argument("--set-json", type=Path)
    settings = sub.add_parser("settings")
    settings.add_argument("--stdin", action="store_true", required=True)
    sources = sub.add_parser("sources", help="replace explicit private source configuration")
    sources.add_argument("--set-json", type=Path, required=True)
    tick = sub.add_parser("tick")
    tick.add_argument("--send", action="store_true")
    drain = sub.add_parser("drain", help="portable only; existing installations retain their own drainer")
    drain.add_argument("--send", action="store_true")
    drain.add_argument("--limit", type=int, default=1)
    sched = sub.add_parser("schedule")
    sched.add_argument("action", choices=("install", "uninstall", "status"))
    sched.add_argument("--interval", type=int, default=300)
    sched.add_argument("--send", action="store_true")
    return p


def execute(args):
    if args.command == "configure":
        recipient = sys.stdin.readline().strip() if args.recipient_stdin else None
        configure(
            args.config, existing_source=args.existing_source, home=args.home,
            portable=args.portable, recipient=recipient, allow_send=args.allow_send,
            scenario_root=args.scenario_root, allowed=list(SCENARIOS) + args.allow_scenario,
            enabled=args.enable, sources=read_json(args.sources_json) if args.sources_json else None,
        )
        return {"ok": True, "diagnostics": diagnostics(Launchpad(args.config), args.config)}
    if args.command == "setup":
        value = _stdin()
        if set(value) - {"mode", "recipient"} or value.get("mode") not in ("existing", "portable"):
            raise ConfigurationError("invalid onboarding request")
        if value["mode"] == "existing":
            if not detect_existing():
                raise ConfigurationError("no existing canonical installation was detected")
            configure(args.config)
        else:
            configure(args.config, portable=True, recipient=value.get("recipient"))
        return {"ok": True, "diagnostics": diagnostics(Launchpad(args.config), args.config)}
    if args.command == "validate":
        return {"ok": True, "proposal": validate_proposal(read_json(args.proposal))}
    if args.command in ("diagnostics", "status") and not args.config.exists():
        return {"ok": True, "diagnostics": diagnostics(config_path=args.config)}
    service = Launchpad(args.config)
    if args.command in ("diagnostics", "status"):
        from .schedule import status
        return {"ok": True, "diagnostics": diagnostics(service, args.config), "schedule": status(service)}
    if args.command == "run":
        result = service.run(args.scenario, send=args.send)
        if args.summary:
            result["receipts"] = [
                {key: row[key] for key in ("id", "state", "scenario", "at", "reason")}
                for row in result["receipts"]
            ]
        return result
    if args.command == "submit":
        receipt = service.submit(read_json(args.proposal), send=args.send)
        return {"ok": receipt["state"] not in ("error", "unknown"), "receipt": receipt}
    if args.command == "verify":
        return service.ledger.verify()
    if args.command == "receipts":
        return {"ok": True, "receipts": service.receipts(args.limit, refresh=args.refresh)}
    if args.command == "self-test":
        result = service.self_test(send=args.send, confirmed=args.confirm)
        return {"ok": result["receipt"]["state"] not in ("error", "unknown"), **result}
    if args.command == "confirm":
        return {"ok": True, "receipt": service.confirm(args.receipt_id, received=args.received and args.yes)}
    if args.command == "policy":
        return {"ok": True, "policy": service.policy(read_json(args.set_json) if args.set_json else None)}
    if args.command == "settings":
        values = _stdin()
        if set(values) - {"send_enabled", "app_schedule", "enabled_scenarios"}:
            raise ConfigurationError("unsupported settings field")
        return {"ok": True, "settings": service.settings(**values)}
    if args.command == "sources":
        return {"ok": True, "sources": service.sources(read_json(args.set_json))}
    if args.command == "drain":
        if not args.send:
            raise ConfigurationError("drain requires explicit --send")
        return service.transport.drain(limit=args.limit)
    if args.command == "tick":
        from .schedule import tick
        return tick(service, send=args.send)
    if args.command == "schedule":
        from . import schedule
        if args.action == "install":
            return {"ok": True, "schedule": schedule.install(service, interval=args.interval, send=args.send)}
        return {"ok": True, "schedule": getattr(schedule, args.action)(service)}
    raise ConfigurationError("unsupported command")


def main(argv=None):
    from .processes import stop_all
    if hasattr(signal, "SIGTERM"):
        def terminate(_signum, _frame):
            stop_all()
            raise SystemExit(143)
        try:
            signal.signal(signal.SIGTERM, terminate)
        except ValueError:
            pass
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0 if result.get("ok", True) else 1
    except LaunchpadError as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": str(exc)}))
        return 2
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        print(json.dumps({"ok": False, "error": "invalid_state", "message": "Invalid or unreadable input/state; no successful operation is assumed."}))
        return 2
