"""One fresh-process public execution for the S1 snapshot trial.

This benchmark is intentionally outside the package. The runner supplies the
installed reference-pack site and the selected PyBench source root on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import resource
import sys
import time
from pathlib import Path

import pybench
from pybench.operation_packs import canonical_json


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_files(root: Path) -> tuple[dict[str, str], int]:
    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Trial stage contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _sha_file(path)
            total += path.stat().st_size
        elif not path.is_dir():
            raise RuntimeError("Trial stage contains an unsupported object")
    return files, total


def _live_children() -> str:
    path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    return path.read_text(encoding="ascii").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", choices=("v3", "v4"), required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args()
    expected_source = args.source_root.resolve(strict=True) / "src/pybench"
    imported = Path(pybench.__file__).resolve(strict=True)
    if not imported.is_relative_to(expected_source):
        raise RuntimeError(f"PyBench imported from {imported}, not {expected_source}")
    stage = args.stage.resolve(strict=True)
    request = json.loads((stage / "request.json").read_text(encoding="utf-8"))
    if args.protocol == "v3":
        from pybench.operation_packs_v2 import (
            activate_operation_pack_v2,
            discover_operation_pack_v2,
        )
        from pybench.protocol_v3 import execute_operation_pack_v3

        distribution = metadata.distribution("scwb-v2-python-reference")
        manifest = distribution.locate_file("scwbpyv2reference/operation-pack.json")
        discovery = discover_operation_pack_v2(manifest)
        activate_operation_pack_v2(discovery)
        execute = execute_operation_pack_v3
    else:
        from pybench.operation_packs_v3 import (
            activate_operation_pack_v3,
            discover_operation_pack_v3,
        )
        from pybench.protocol_v4 import execute_operation_pack_v4

        distribution = metadata.distribution("scwb-v3-python-reference")
        manifest = distribution.locate_file("scwbpyv3reference/operation-pack.json")
        discovery = discover_operation_pack_v3(manifest)
        activate_operation_pack_v3(discovery)
        execute = execute_operation_pack_v4
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if before or _live_children():
        raise RuntimeError("Trial setup spawned a child process")
    rss_pre = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    receipt, sidecar = execute(request, discovery, stage)
    elapsed = time.perf_counter() - started
    rss_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss - before
    live_children = _live_children()
    files, total = _stage_files(stage)
    result = {
        "case_id": args.case_id,
        "protocol": args.protocol,
        "status": "ok",
        "execute_seconds": elapsed,
        "rss_pre_kib": rss_pre,
        "rss_peak_kib": rss_peak,
        "child_peak_delta_kib": children,
        "live_children": live_children,
        "receipt_sha256": hashlib.sha256(canonical_json(receipt).encode("utf-8")).hexdigest(),
        "sidecar_sha256": hashlib.sha256(canonical_json(sidecar).encode("utf-8")).hexdigest(),
        "stage_files": files,
        "stage_total_bytes": total,
        "python_version": sys.version,
        "pybench_import_path": str(imported),
    }
    print("PYBENCH_S1_TRIAL_JSON=" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if children == 0 and not live_children else 2


if __name__ == "__main__":
    raise SystemExit(main())
