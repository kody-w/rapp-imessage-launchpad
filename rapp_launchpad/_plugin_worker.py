from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapp_launchpad.protocol import SLUG
from rapp_launchpad.util import strict_json, within


def execute(request):
    name = request["name"]
    root = Path(request["root"]).resolve()
    if not isinstance(name, str) or not SLUG.fullmatch(name):
        raise ValueError("invalid plugin name")
    path = root / (name + ".py")
    if not path.is_file() or path.is_symlink() or not within(path, root):
        raise ValueError("plugin is not a regular allowlisted module")
    os.umask(0o077)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    except (ImportError, ValueError, OSError):
        pass
    sys.path.insert(0, str(root.parent))
    spec = importlib.util.spec_from_file_location("scenarios." + name, path)
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
        if name == "interrupt" and request["action"] != "build" and hasattr(module, "SCENARIOS"):
            registered = request.get("allowed_scenarios", [])
            if not isinstance(registered, list) or not all(
                isinstance(item, str) and SLUG.fullmatch(item) for item in registered
            ):
                raise ValueError("invalid configured scenario registry")
            prior = [
                row.get("scenario") for row in request.get("history", [])
                if isinstance(row, dict)
            ]
            prior = [item for item in prior if isinstance(item, str) and SLUG.fullmatch(item)]
            module.SCENARIOS = frozenset(module.SCENARIOS) | set(registered) | set(prior) | {"self-test"}
        if request["action"] == "build":
            result = module.build(request["context"])
        elif request["action"] == "gate" and name == "interrupt":
            result = module.evaluate(
                request["proposal"], request["history"], request["now"], request["policy"],
            )
        elif request["action"] == "render" and name == "interrupt":
            renderer = getattr(module, "render", None)
            result = renderer(request["proposal"]) if callable(renderer) else None
        elif request["action"] == "delivery-key" and name == "interrupt":
            result = module.delivery_key(request["proposal"])
        else:
            raise ValueError("unsupported plugin action")
    return {"ok": True, "result": result}


def main():
    try:
        raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        request = strict_json(raw, max_bytes=64 * 1024 * 1024)
        if request.get("action") != "gate" and len(raw) > 1_048_576:
            raise ValueError("build input exceeds 1 MiB")
        result = execute(request)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        encoded = json.dumps({
            "ok": False, "error": type(exc).__name__,
            "message": "plugin failed; no message was submitted",
            "detail": str(exc)[:2000],
        })
    print(encoded)


if __name__ == "__main__":
    main()
