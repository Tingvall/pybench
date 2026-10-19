"""Detached parent for one owned S1 baseline or paired runner invocation."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from transport_s1_runner import _best_effort_guard


def _atomic_json(path: Path, value: dict) -> None:
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("x", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")
    pending.replace(path)


def _stop_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.log_root.exists() or args.log_root.is_symlink():
        raise RuntimeError("Supervisor needs a command and a fresh owned log root")
    log_root = args.log_root.resolve(strict=False)
    log_root.mkdir(parents=True)
    work = args.work_root.resolve(strict=False)
    baseline = args.baseline_root.resolve(strict=True)
    candidate = args.candidate_root.resolve(strict=True) if args.candidate_root else None
    before = _best_effort_guard(work, baseline, candidate)
    _atomic_json(log_root / "pre_guard.json", before)
    process: subprocess.Popen | None = None
    code = 1
    timed_out = False
    error = ""
    started = time.perf_counter()
    try:
        with (log_root / "runner.stdout").open("xb") as stdout, (
            log_root / "runner.stderr"
        ).open("xb") as stderr:
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=4 * 60 * 60)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_group(process)
                code = 124
    except BaseException:
        error = traceback.format_exc()
        if process is not None:
            _stop_group(process)
    finally:
        after = _best_effort_guard(work, baseline, candidate)
        _atomic_json(log_root / "post_guard.json", after)
        (log_root / "supervisor_error.log").write_text(error, encoding="utf-8")
        _atomic_json(log_root / "status.json", {
            "exit_code": code, "timed_out": timed_out,
            "elapsed_seconds": time.perf_counter() - started,
            "command": command, "pre_post_guard_equal": before == after,
            "supervisor_error": bool(error),
        })
    if error:
        sys.stderr.write(error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
