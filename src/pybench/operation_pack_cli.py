from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .operation_packs import (
    OperationPackError,
    discover_operation_pack,
    execute_operation_pack,
    runtime_attestation,
)
from .protocol import error_receipt_from_exception, load_request, write_receipt


def _atomic_json(path: str | os.PathLike[str], value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicit PyBench operation-pack resolver/executor"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--manifest", required=True)
    discover.add_argument("--output")
    execute = commands.add_parser("execute")
    execute.add_argument("--manifest", required=True)
    execute.add_argument("--request", required=True)
    execute.add_argument("--receipt", required=True)
    execute.add_argument("--runtime-attestation", required=True)
    execute.add_argument("--expected-pack-sha256", required=True)
    execute.add_argument("--expected-definition-sha256", required=True)
    execute.add_argument("--expected-distribution-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "discover":
            discovery = discover_operation_pack(args.manifest)
            result = {**discovery.as_dict(), "runtime_attestation": runtime_attestation(discovery)}
            if args.output:
                _atomic_json(args.output, result)
            else:
                print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 0
        request = load_request(args.request)
        receipt, attestation = execute_operation_pack(
            request,
            args.manifest,
            expected_pack_sha256=args.expected_pack_sha256,
            expected_definition_sha256=args.expected_definition_sha256,
            expected_distribution_sha256=args.expected_distribution_sha256,
        )
        _atomic_json(args.runtime_attestation, attestation)
        write_receipt(args.receipt, receipt, request=request)
        return 0
    except Exception as exc:  # noqa: BLE001
        if args.command == "execute" and Path(args.request).is_file():
            request = load_request(args.request)
            receipt = error_receipt_from_exception(
                op_name=request.get("op_name", "<unknown>"),
                run_id=(request.get("run") or {}).get("run_id", "<unknown>"),
                exc=exc,
                request=request,
            )
            write_receipt(args.receipt, receipt)
        payload = (
            exc.as_dict()
            if isinstance(exc, OperationPackError)
            else {"status": "error", "code": exc.__class__.__name__, "message": str(exc)}
        )
        print(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
