from __future__ import annotations

import json
import os
import signal
import subprocess
import threading

from .errors import PluginError
from .util import strict_json

_ACTIVE = set()


def _stop(process):
    try:
        if os.name == "nt":
            if process.poll() is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_all():
    for process in list(_ACTIVE):
        _stop(process)


def run_json(argv, payload, *, timeout=40, max_output=1_048_576, max_input=1_048_576, env=None):
    """No shell; bounded wall time and pipes; terminate our own child group."""
    process = subprocess.Popen(
        [str(a) for a in argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=os.name != "nt", env=env,
    )
    _ACTIVE.add(process)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def collect(pipe, name):
        try:
            while True:
                chunk = pipe.read(16_384)
                if not chunk:
                    break
                if len(buffers[name]) + len(chunk) > max_output:
                    overflow.set()
                    _stop(process)
                    break
                buffers[name].extend(chunk)
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=collect, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=collect, args=(process.stderr, "stderr"), daemon=True),
    ]
    writer = None
    for thread in threads:
        thread.start()
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > max_input:
            raise PluginError("subprocess input exceeds its bounded byte limit")
        def write_input():
            try:
                process.stdin.write(raw)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        process.wait(timeout=timeout)
        writer.join(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            _stop(process)
            raise PluginError("subprocess left an inherited output pipe open")
        if overflow.is_set():
            raise PluginError("subprocess exceeded the bounded output limit")
        if process.returncode != 0:
            raise PluginError("subprocess failed; private stdout/stderr is not exposed")
        try:
            result = strict_json(bytes(buffers["stdout"]))
        except (ValueError, UnicodeError, RecursionError):
            raise PluginError("subprocess returned invalid JSON") from None
        if not isinstance(result, dict):
            raise PluginError("subprocess result must be an object")
        return result
    except subprocess.TimeoutExpired:
        raise PluginError("subprocess exceeded its time limit; its process group was stopped") from None
    except BrokenPipeError:
        raise PluginError("subprocess exited before accepting input") from None
    finally:
        _stop(process)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        if writer is not None:
            writer.join(timeout=1)
        if writer is None or not writer.is_alive():
            if not process.stdin.closed:
                process.stdin.close()
        _ACTIVE.discard(process)
