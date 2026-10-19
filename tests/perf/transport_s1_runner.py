"""Owned-stage baseline and paired trials for ordinary v3/v4 snapshots.

No public protocol or runtime implementation is changed by this harness.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


HARNESS = Path(__file__).resolve()
REPO = HARNESS.parents[2]
BASELINE_COMMIT = "0ef3c56f9d300e7813ac5a555e6d2ac7cb64a57e"
MARKER = "PYBENCH_S1_TRIAL_JSON="
FIXTURE_SEED = 20261019


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Guard tree contains symlink: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha_file(path)
        elif not path.is_dir():
            raise RuntimeError(f"Guard tree contains unsupported object: {path}")
    return result


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _source_guard(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if _git(root, "status", "--porcelain"):
        raise RuntimeError(f"Source worktree is dirty: {root}")
    return {"commit": _git(root, "rev-parse", "HEAD"), "files": _tree_hashes(root / "src/pybench")}


def _dependency_guard() -> dict[str, dict[str, str]]:
    return {
        dist.metadata["Name"].lower(): {
            "version": dist.version,
            "metadata_sha256": hashlib.sha256(
                (dist.read_text("METADATA") or "").encode("utf-8")
            ).hexdigest(),
            "record_sha256": hashlib.sha256(
                (dist.read_text("RECORD") or "").encode("utf-8")
            ).hexdigest(),
        }
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    }


def _guard(paths: dict[str, Path], sources: dict[str, Path]) -> dict[str, Any]:
    return {
        "python_executable_sha256": _sha_file(Path(sys.executable).resolve(strict=True)),
        "python_version": sys.version,
        "dependencies": _dependency_guard(),
        "source": {name: _source_guard(root) for name, root in sources.items()},
        "paths": {name: _tree_hashes(path) for name, path in paths.items()},
    }


def _write_json(path: Path, value: Any) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to replace existing evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    if temporary.exists():
        raise RuntimeError(f"Pending evidence already exists: {temporary}")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_jsonl(path: Path, header: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise RuntimeError(f"Refusing to replace existing evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    if temporary.exists():
        raise RuntimeError(f"Pending evidence already exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            for record in [{"kind": "guard", **header}, *({"kind": "trial", **row} for row in rows)]:
                output.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _install_local_fixtures(site: Path) -> None:
    site.mkdir()
    for version in ("v2", "v3"):
        fixture = REPO / "tests/fixtures" / f"operation-pack-{version}-python-reference"
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                "--no-build-isolation", "--no-cache-dir", "--target", str(site), str(fixture),
            ],
            check=True,
            timeout=600,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    sys.path.insert(0, str(site))


def _prepare_template(root: Path, protocol: str, size_mib: int) -> None:
    from pybench.operation_packs import canonical_json
    from v3_helpers import fixture_inputs, make_request

    root.mkdir(parents=True)
    if protocol == "v3":
        from pybench.operation_packs_v2 import discover_operation_pack_v2

        dist = metadata.distribution("scwb-v2-python-reference")
        manifest = dist.locate_file("scwbpyv2reference/operation-pack.json")
        discovery = discover_operation_pack_v2(manifest)
        request = make_request(
            discovery, root, "scwbpyv2reference::all_contracts", fixture_inputs()
        )
    else:
        from pybench.operation_packs_v3 import discover_operation_pack_v3
        from test_operation_pack_v3_successor import _request_v4

        dist = metadata.distribution("scwb-v3-python-reference")
        manifest = dist.locate_file("scwbpyv3reference/operation-pack.json")
        discovery = discover_operation_pack_v3(manifest)
        request = _request_v4(
            discovery, root, "scwbpyv3reference::all_contracts", fixture_inputs()
        )
    (root / "request.json").write_text(canonical_json(request), encoding="utf-8")
    inert = root / "inert/payload.bin"
    inert.parent.mkdir()
    generator = random.Random(FIXTURE_SEED + size_mib)
    with inert.open("xb") as output:
        for _ in range(size_mib):
            output.write(generator.randbytes(1024 * 1024))


def _parse_trial(stdout: str) -> dict[str, Any]:
    records = [line.removeprefix(MARKER) for line in stdout.splitlines() if line.startswith(MARKER)]
    if len(records) != 1:
        raise RuntimeError("Fresh child emitted no unique trial result")
    result = json.loads(records[0])
    if result.get("status") != "ok" or result.get("child_peak_delta_kib") != 0 or result.get("live_children"):
        raise RuntimeError("Trial failed or spawned a child process")
    return result


def _case_id(protocol: str, size_mib: int) -> str:
    return f"{protocol}_{size_mib}mib"


def _trial(
    protocol: str, size_mib: int, label: str, source: Path, site: Path,
    fixture: Path, expected_files: dict[str, str], trials: Path, repetition: int,
) -> dict[str, Any]:
    if shutil.disk_usage(trials).free < 2 * 1024**3:
        raise RuntimeError("Trial has less than 2 GiB free disk")
    if any(trials.iterdir()):
        raise RuntimeError("A prior trial stage remains")
    with tempfile.TemporaryDirectory(prefix="s1-stage-", dir=trials) as temporary:
        stage = Path(temporary) / "stage"
        shutil.copytree(fixture, stage)
        if _tree_hashes(stage) != expected_files:
            raise RuntimeError("Trial stage copy differs from its immutable fixture")
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(source / "tests"), str(site))),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            sys.executable, str(HARNESS.parent / "transport_s1_trial.py"),
            "--protocol", protocol, "--stage", str(stage),
            "--case-id", _case_id(protocol, size_mib),
        ]
        started = time.perf_counter()
        process = subprocess.run(command, capture_output=True, text=True, env=env, timeout=900)
        wall = time.perf_counter() - started
        if process.returncode:
            raise RuntimeError(
                f"Trial {label} {_case_id(protocol, size_mib)} #{repetition} exited "
                f"{process.returncode}: {process.stderr[-4000:]}"
            )
        result = _parse_trial(process.stdout)
        if result["stage_files"].get("inert/payload.bin") != expected_files["inert/payload.bin"]:
            raise RuntimeError("Inert baseline payload changed during execution")
        result.update({"label": label, "repetition": repetition, "wall_seconds": wall})
        return result


def _cases(protocols: str, sizes: str) -> list[tuple[str, int]]:
    names = protocols.split(",")
    values = [int(value) for value in sizes.split(",")]
    if names != ["v3", "v4"] or values != [64, 256]:
        raise RuntimeError("This frozen harness requires v3,v4 and 64,256 MiB")
    return [(protocol, size) for protocol in names for size in values]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("baseline", "paired"), required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--protocols", required=True)
    parser.add_argument("--sizes-mib", required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--order")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--baseline-ledger", type=Path)
    parser.add_argument("--raw", type=Path, required=True)
    args = parser.parse_args()
    if args.repetitions != 3:
        raise RuntimeError("Frozen repetition count is three")
    cases = _cases(args.protocols, args.sizes_mib)
    baseline = args.baseline_root.resolve(strict=True)
    if _source_guard(baseline)["commit"] != BASELINE_COMMIT:
        raise RuntimeError("Baseline source is not the frozen 0ef3c56 peer")
    if args.work_root.is_symlink():
        raise RuntimeError("Owned work root cannot be a symlink")
    work = args.work_root.resolve(strict=False)
    if args.phase == "baseline":
        if work.exists() or args.ledger is None or args.candidate_root or args.baseline_ledger:
            raise RuntimeError("Baseline needs a fresh owned work root and --ledger")
        work.mkdir(parents=True)
        _install_local_fixtures(work / "site")
        fixtures = work / "fixtures"
        fixtures.mkdir()
        for protocol, size in cases:
            _prepare_template(fixtures / _case_id(protocol, size), protocol, size)
        sources = {"baseline": baseline}
        guard_paths = {
            "site": work / "site", "fixtures": fixtures, "harness": HARNESS.parent,
            "reference_v2": REPO / "tests/fixtures/operation-pack-v2-python-reference",
            "reference_v3": REPO / "tests/fixtures/operation-pack-v3-python-reference",
        }
    else:
        if not work.is_dir() or args.baseline_ledger is None or args.candidate_root is None:
            raise RuntimeError("Paired mode needs existing owned baseline evidence and candidate root")
        if args.baseline_ledger.resolve(strict=True) != work / "baseline.json":
            raise RuntimeError("Paired mode must use this work root's baseline ledger")
        if args.order != "baseline,candidate/candidate,baseline/baseline,candidate":
            raise RuntimeError("Paired order differs from frozen trial order")
        sources = {"baseline": baseline, "candidate": args.candidate_root.resolve(strict=True)}
        sys.path.insert(0, str(work / "site"))
        guard_paths = {
            "site": work / "site", "fixtures": work / "fixtures", "harness": HARNESS.parent,
            "reference_v2": REPO / "tests/fixtures/operation-pack-v2-python-reference",
            "reference_v3": REPO / "tests/fixtures/operation-pack-v3-python-reference",
        }
    if work.is_symlink() or args.raw.resolve(strict=False) != work / (
        "baseline.jsonl" if args.phase == "baseline" else "paired.jsonl"
    ):
        raise RuntimeError("Evidence path or work root is not the owned trial location")
    if args.phase == "baseline" and args.ledger.resolve(strict=False) != work / "baseline.json":
        raise RuntimeError("Baseline ledger must be inside the owned work root")
    trials = work / "trials"
    trials.mkdir(exist_ok=True)
    if trials.is_symlink() or any(trials.iterdir()):
        raise RuntimeError("Trial scratch is not empty; no broad cleanup is allowed")
    before = _guard(guard_paths, sources)
    if args.phase == "paired":
        ledger = json.loads(args.baseline_ledger.read_text(encoding="utf-8"))
        if ledger["guard"]["source"]["baseline"] != before["source"]["baseline"]:
            raise RuntimeError("Baseline source changed since its frozen ledger")
        if ledger["guard"]["paths"] != before["paths"]:
            raise RuntimeError("Fixtures, site, or harness differ from frozen baseline")
        if ledger["guard"]["dependencies"] != before["dependencies"] or ledger["guard"]["python_executable_sha256"] != before["python_executable_sha256"]:
            raise RuntimeError("Interpreter or dependencies changed since baseline")
    rows: list[dict[str, Any]] = []
    frozen_ledger: dict[str, Any] | None = None
    try:
        for protocol, size in cases:
            fixture = work / "fixtures" / _case_id(protocol, size)
            expected_files = _tree_hashes(fixture)
            for repetition in range(1, 4):
                labels = ("baseline",) if args.phase == "baseline" else (
                    ("baseline", "candidate") if repetition != 2 else ("candidate", "baseline")
                )
                for label in labels:
                    rows.append(_trial(
                        protocol, size, label, sources[label], work / "site", fixture,
                        expected_files, trials, repetition,
                    ))
        if args.phase == "baseline":
            ceilings: dict[str, dict[str, float]] = {}
            for protocol, size in cases:
                group = [row for row in rows if row["case_id"] == _case_id(protocol, size)]
                if len({json.dumps(row["stage_files"], sort_keys=True) for row in group}) != 1:
                    raise RuntimeError("Baseline stage bytes differ across fresh trials")
                b1 = group[0]["stage_total_bytes"]
                rss_ceiling = statistics.median(row["rss_peak_kib"] for row in group) - b1 // 2048
                if rss_ceiling <= max(row["rss_pre_kib"] for row in group):
                    raise RuntimeError("Frozen RSS ceiling is below measured interpreter floor")
                ceilings[_case_id(protocol, size)] = {
                    "rss_kib": rss_ceiling,
                    "execute_seconds": 1.10 * statistics.median(row["execute_seconds"] for row in group),
                }
            frozen_ledger = {"guard": before, "ceilings": ceilings, "cases": [
                _case_id(protocol, size) for protocol, size in cases
            ]}
        else:
            for protocol, size in cases:
                name = _case_id(protocol, size)
                group = [row for row in rows if row["case_id"] == name]
                base = [row for row in group if row["label"] == "baseline"]
                candidate = [row for row in group if row["label"] == "candidate"]
                for left, right in zip(base, candidate, strict=True):
                    for key in ("stage_files", "receipt_sha256", "sidecar_sha256"):
                        if left[key] != right[key]:
                            raise RuntimeError(f"Candidate {name} differs in {key}")
                ceiling = ledger["ceilings"][name]
                if max(row["rss_peak_kib"] for row in candidate) > ceiling["rss_kib"] or statistics.median(row["execute_seconds"] for row in candidate) > ceiling["execute_seconds"]:
                    raise RuntimeError(f"Candidate {name} misses frozen RSS or elapsed ceiling")
    finally:
        after = _guard(guard_paths, sources)
        if before != after or any(trials.iterdir()):
            raise RuntimeError("Source, fixture, installed site, harness, or trial scratch changed")
        _write_jsonl(args.raw, {"phase": args.phase, "guard": before}, rows)
    if frozen_ledger is not None:
        _write_json(args.ledger, frozen_ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
