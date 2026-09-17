from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .operation_packs import OperationPackError, canonical_json
from .operation_packs_v3 import activate_operation_pack_v3, discover_operation_pack_v3
from .protocol_v4 import error_receipt_v4, execute_operation_pack_v4


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise OperationPackError(
                "PYBENCH_CAPABILITY_MISMATCH",
                f"Request repeats raw object key {key!r}",
                phase="staging",
            )
        value[key] = child
    return value


def _read_request(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except OperationPackError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperationPackError(
            "PYBENCH_CAPABILITY_MISMATCH",
            f"Request is not strict UTF-8 JSON: {exc}",
            phase="staging",
        ) from exc
    if not isinstance(value, dict):
        raise OperationPackError(
            "PYBENCH_CAPABILITY_MISMATCH", "Request root must be an object", phase="staging"
        )
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(canonical_json(value), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit PyBench operation-pack v4 executor")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--python-runtime", required=True)
    args = parser.parse_args(argv)
    request: dict[str, Any] | None = None
    try:
        request = _read_request(args.request)
        discovery = discover_operation_pack_v3(args.manifest)
        activate_operation_pack_v3(discovery)
        receipt, runtime = execute_operation_pack_v4(request, discovery, args.stage_root)
        _atomic_json(Path(args.receipt), receipt)
        _atomic_json(Path(args.python_runtime), runtime)
        return 0
    except BaseException as exc:
        if request is not None and all(
            key in request for key in ("transition", "request_identity", "execution_capability")
        ):
            try:
                _atomic_json(Path(args.receipt), error_receipt_v4(request, exc))
            except BaseException:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
