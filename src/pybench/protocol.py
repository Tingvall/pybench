from __future__ import annotations

import json
import traceback as _traceback
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "pybench-op-v1"


class ProtocolError(RuntimeError):
    pass


def _require_nonempty_str(x: Any, name: str) -> str:
    if not isinstance(x, str) or not x:
        raise ProtocolError(f"{name} must be a non-empty string")
    return x


def _validate_output_specs(outputs: Any, where: str) -> None:
    if isinstance(outputs, list) and len(outputs) == 0:
        return
    if not isinstance(outputs, dict):
        raise ProtocolError(f"{where} must be an object")
    for name, spec in outputs.items():
        _require_nonempty_str(name, f"{where} key")
        if not isinstance(spec, dict):
            raise ProtocolError(f"{where}.{name} must be an object")
        _require_nonempty_str(spec.get("artifact_id"), f"{where}.{name}.artifact_id")
        _require_nonempty_str(spec.get("type"), f"{where}.{name}.type")


def validate_request(req: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(req, dict):
        raise ProtocolError("request must be a JSON object")
    if req.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol_version: {req.get(protocol_version)!r}")
    _require_nonempty_str(req.get("bundle_dir"), "request.bundle_dir")
    _require_nonempty_str(req.get("op_name"), "request.op_name")

    run = req.get("run")
    if not isinstance(run, dict):
        raise ProtocolError("request.run must be an object")
    _require_nonempty_str(run.get("run_id"), "request.run.run_id")

    if not isinstance(req.get("inputs"), dict):
        raise ProtocolError("request.inputs must be an object")
    if not isinstance(req.get("params"), dict):
        raise ProtocolError("request.params must be an object")
    _validate_output_specs(req.get("outputs"), "request.outputs")
    return req


def validate_receipt(receipt: dict[str, Any], request: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ProtocolError("receipt must be a JSON object")
    if receipt.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported receipt protocol_version: {receipt.get(protocol_version)!r}")
    status = _require_nonempty_str(receipt.get("status"), "receipt.status")
    if status not in {"ok", "error"}:
        raise ProtocolError("receipt.status must be ok or error")
    _require_nonempty_str(receipt.get("op_name"), "receipt.op_name")
    _require_nonempty_str(receipt.get("run_id"), "receipt.run_id")

    if status == "ok":
        outs = receipt.get("outputs")
        if not isinstance(outs, dict):
            raise ProtocolError("receipt.outputs must be an object when status=ok")
        if request is not None:
            req_outs = request.get("outputs", {})
            for k, v in outs.items():
                if k not in req_outs:
                    raise ProtocolError(f"receipt.outputs contains unknown output: {k}")
                if not isinstance(v, dict):
                    raise ProtocolError(f"receipt.outputs.{k} must be an object")
                if v.get("artifact_id") != req_outs[k].get("artifact_id"):
                    raise ProtocolError(f"artifact_id mismatch for output {k}")
                if v.get("type") != req_outs[k].get("type"):
                    raise ProtocolError(f"type mismatch for output {k}")
    else:
        _require_nonempty_str(receipt.get("error_class"), "receipt.error_class")
        _require_nonempty_str(receipt.get("message"), "receipt.message")
    return receipt


def load_request(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    req = json.loads(p.read_text())
    return validate_request(req)


def write_receipt(path: str | Path, receipt: dict[str, Any], request: dict[str, Any] | None = None) -> None:
    validate_receipt(receipt, request=request)
    Path(path).write_text(json.dumps(receipt, indent=2, allow_nan=False))


def ok_receipt(*, op_name: str, run_id: str, outputs: dict[str, Any], warnings: list[str] | None = None,
               timings: dict[str, float] | None = None, pybench_version: str | None = None) -> dict[str, Any]:
    rec = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok",
        "op_name": op_name,
        "run_id": run_id,
        "outputs": outputs,
        "warnings": warnings or [],
        "timings": timings or {},
    }
    if pybench_version:
        rec["pybench"] = {"version": pybench_version}
    return rec


def error_receipt_from_exception(*, op_name: str, run_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": "error",
        "op_name": str(op_name),
        "run_id": str(run_id),
        "error_class": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
