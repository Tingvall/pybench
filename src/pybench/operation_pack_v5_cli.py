"""Explicit ABI4/process5 operation-pack subprocess entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from .operation_pack_v4_cli import _atomic_json, _read_request
from .operation_packs_v4 import activate_operation_pack_v4, discover_operation_pack_v4
from .protocol_v5 import error_receipt_v5, execute_operation_pack_v5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit PyBench operation-pack process-v5 executor"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--python-runtime", required=True)
    args = parser.parse_args(argv)
    request = None
    try:
        request = _read_request(args.request)
        discovery = discover_operation_pack_v4(args.manifest)
        activate_operation_pack_v4(discovery)
        receipt, runtime = execute_operation_pack_v5(request, discovery, args.stage_root)
        _atomic_json(Path(args.receipt), receipt)
        _atomic_json(Path(args.python_runtime), runtime)
        return 0
    except BaseException as exc:
        if request is not None and all(
            key in request for key in ("transition", "request_identity", "execution_capability")
        ):
            try:
                _atomic_json(Path(args.receipt), error_receipt_v5(request, exc))
            except BaseException:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
