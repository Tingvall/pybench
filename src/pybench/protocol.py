from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import PATH_CONTRACT_VERSION, BundleHandle, open_bundle, resolve_bundle_path
from .capability import build_write_manifest, install_sqlite_authorizer, validate_stage_marker

PROTOCOL_VERSION = "pybench-op-v2"
PATH_CONTRACT = {
    "version": PATH_CONTRACT_VERSION,
    "durable_paths": "bundle_relative_contained",
    "external_inputs": "explicit_import_only",
    "serialized_models": "trusted_provenance_runtime_or_explicit_opt_in",
}
_CAPABILITY_FIELDS = {
    "capability_id",
    "mode",
    "session_id",
    "run_id",
    "source_attestation",
    "staging_identity",
    "write_allowlist",
}


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrustedModelPath:
    """Contained model path authorized by an exact validated operation request."""

    relative: str
    absolute: Path
    contract_version: str


def _require_nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{name} must be a non-empty string")
    return value


def _sha256(value: Any, name: str) -> None:
    text = _require_nonempty_str(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProtocolError(f"{name} must be a lowercase SHA-256 identity")


def _validate_allowance(item: Any, where: str, *, receipt: bool = False) -> None:
    if not isinstance(item, dict):
        raise ProtocolError(f"{where} must be an object")
    allowed = {
        "output_name",
        "output_id",
        "output_kind",
        "mutation_class",
        "table",
        "path",
        "key_scope",
        "row_count",
        "content_sha256",
    }
    extras = set(item) - allowed
    if extras:
        raise ProtocolError(f"{where} contains unknown fields: {sorted(extras)}")
    for key in ("output_name", "output_id", "output_kind", "mutation_class"):
        _require_nonempty_str(item.get(key), f"{where}.{key}")
    mutation_class = item["mutation_class"]
    if mutation_class == "sqlite_rows":
        _require_nonempty_str(item.get("table"), f"{where}.table")
        scope = item.get("key_scope")
        if not isinstance(scope, dict) or set(scope) != {"column", "values"}:
            raise ProtocolError(f"{where}.key_scope must contain exactly column and values")
        _require_nonempty_str(scope.get("column"), f"{where}.key_scope.column")
        values = scope.get("values")
        if not isinstance(values, list) or not values or len(values) != len(set(values)):
            raise ProtocolError(f"{where}.key_scope.values must be non-empty and unique")
        for index, value in enumerate(values):
            _require_nonempty_str(value, f"{where}.key_scope.values[{index}]")
        if "path" in item:
            raise ProtocolError(f"{where}: SQLite allowance cannot contain path")
    elif mutation_class == "payload_tree":
        from .bundle import normalize_bundle_relative_path

        normalize_bundle_relative_path(item.get("path"), purpose="capability payload path")
        if "table" in item or "key_scope" in item:
            raise ProtocolError(f"{where}: payload allowance cannot contain table/key_scope")
    else:
        raise ProtocolError(f"{where}.mutation_class is unsupported")
    if receipt:
        count = item.get("row_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ProtocolError(f"{where}.row_count must be a non-negative integer")
        _sha256(item.get("content_sha256"), f"{where}.content_sha256")


def _validate_capability(capability: Any, where: str) -> dict[str, Any]:
    if not isinstance(capability, dict):
        raise ProtocolError(f"{where} must be an object")
    extras = set(capability) - _CAPABILITY_FIELDS
    missing = _CAPABILITY_FIELDS - set(capability)
    if extras or missing:
        raise ProtocolError(
            f"{where} fields mismatch (missing={sorted(missing)}, unknown={sorted(extras)})"
        )
    for key in ("capability_id", "mode", "session_id", "run_id"):
        _require_nonempty_str(capability.get(key), f"{where}.{key}")
    mode = capability["mode"]
    if mode not in {"read_only", "staging_write"}:
        raise ProtocolError(f"{where}.mode is unsupported")
    source = capability.get("source_attestation")
    if not isinstance(source, dict) or set(source) != {"scheme", "content_sha256"}:
        raise ProtocolError(f"{where}.source_attestation fields mismatch")
    if source["scheme"] != "scworkbench-scientific-state-v1":
        raise ProtocolError(f"{where}.source_attestation.scheme is unsupported")
    _sha256(source["content_sha256"], f"{where}.source_attestation.content_sha256")
    stage = capability.get("staging_identity")
    if not isinstance(stage, dict) or set(stage) != {"root_sha256", "checkpoint_generation"}:
        raise ProtocolError(f"{where}.staging_identity fields mismatch")
    _sha256(stage["root_sha256"], f"{where}.staging_identity.root_sha256")
    _require_nonempty_str(
        stage["checkpoint_generation"], f"{where}.staging_identity.checkpoint_generation"
    )
    allowlist = capability.get("write_allowlist")
    if not isinstance(allowlist, list):
        raise ProtocolError(f"{where}.write_allowlist must be an array")
    if mode == "read_only" and allowlist:
        raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: read_only requires an empty allowlist")
    if mode == "staging_write" and not allowlist:
        raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: staging_write requires an allowlist")
    for index, item in enumerate(allowlist):
        _validate_allowance(item, f"{where}.write_allowlist[{index}]")
    return capability


def _validate_output_specs(outputs: Any, where: str) -> None:
    if not isinstance(outputs, dict):
        raise ProtocolError(f"{where} must be an object")
    for name, spec in outputs.items():
        _require_nonempty_str(name, f"{where} key")
        if not isinstance(spec, dict):
            raise ProtocolError(f"{where}.{name} must be an object")
        _require_nonempty_str(spec.get("artifact_id"), f"{where}.{name}.artifact_id")
        _require_nonempty_str(spec.get("type"), f"{where}.{name}.type")


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ProtocolError("request must be a JSON object")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol_version: {request.get('protocol_version')!r}; writable v1 is disabled"
        )
    required = {
        "message_type",
        "protocol_version",
        "bundle_dir",
        "path_contract",
        "op_name",
        "run",
        "execution_capability",
        "inputs",
        "params",
        "outputs",
    }
    if set(request) != required:
        raise ProtocolError(
            f"request fields mismatch (missing={sorted(required - set(request))}, "
            f"unknown={sorted(set(request) - required)})"
        )
    if request["message_type"] != "request":
        raise ProtocolError("request.message_type must be 'request'")
    if request.get("path_contract") != PATH_CONTRACT:
        raise ProtocolError("request.path_contract does not match the engine containment contract")
    _require_nonempty_str(request.get("bundle_dir"), "request.bundle_dir")
    _require_nonempty_str(request.get("op_name"), "request.op_name")
    run = request.get("run")
    if not isinstance(run, dict) or set(run) != {"session_id", "run_id"}:
        raise ProtocolError("request.run must contain exactly session_id and run_id")
    _require_nonempty_str(run["session_id"], "request.run.session_id")
    _require_nonempty_str(run["run_id"], "request.run.run_id")
    capability = _validate_capability(request.get("execution_capability"), "request.execution_capability")
    if run["session_id"] != capability["session_id"] or run["run_id"] != capability["run_id"]:
        raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: run identity differs from capability")
    if not isinstance(request.get("inputs"), dict) or not isinstance(request.get("params"), dict):
        raise ProtocolError("request.inputs and request.params must be objects")
    _validate_output_specs(request.get("outputs"), "request.outputs")
    for allowance in capability["write_allowlist"]:
        name = allowance["output_name"]
        if name not in request["outputs"]:
            raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: allowance names unknown output")
        spec = request["outputs"][name]
        if allowance["output_id"] != spec["artifact_id"] or allowance["output_kind"] != spec["type"]:
            raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: allowance output binding differs")
        if allowance["mutation_class"] == "sqlite_rows" and allowance["key_scope"]["values"] != [allowance["output_id"]]:
            raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: key scope must bind exact output")
    return request


def open_bundle_for_request(request: dict[str, Any]) -> BundleHandle:
    validate_request(request)
    capability = request["execution_capability"]
    if capability["mode"] == "staging_write":
        validate_stage_marker(request["bundle_dir"], capability)
    bundle = open_bundle(
        request["bundle_dir"],
        access_mode=capability["mode"],
        capability=capability,
    )
    install_sqlite_authorizer(bundle)
    return bundle


def trusted_model_path(
    request: dict[str, Any],
    bundle: BundleHandle,
    model_path: str,
    *,
    expected_type: str = "either",
) -> TrustedModelPath:
    validate_request(request)
    from .bundle import normalize_bundle_relative_path

    relative = normalize_bundle_relative_path(model_path, purpose="serialized model payload")
    absolute = resolve_bundle_path(
        bundle,
        relative,
        expected_type=expected_type,  # type: ignore[arg-type]
        inspect_tree=True,
        purpose="serialized model payload",
    )
    return TrustedModelPath(relative, absolute, PATH_CONTRACT_VERSION)


def validate_receipt(
    receipt: dict[str, Any], request: dict[str, Any] | None = None
) -> dict[str, Any]:
    if request is not None:
        validate_request(request)
    if not isinstance(receipt, dict):
        raise ProtocolError("receipt must be a JSON object")
    if receipt.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported receipt protocol_version: {receipt.get('protocol_version')!r}")
    status = _require_nonempty_str(receipt.get("status"), "receipt.status")
    required = {"message_type", "protocol_version", "status", "op_name", "run_id", "capability_echo"}
    required |= {"outputs", "write_manifest"} if status == "ok" else {"error_class", "message"}
    if set(receipt) != required:
        raise ProtocolError("receipt fields do not match canonical v2 schema")
    if receipt["message_type"] != "receipt" or status not in {"ok", "error"}:
        raise ProtocolError("receipt message_type/status is invalid")
    _require_nonempty_str(receipt.get("op_name"), "receipt.op_name")
    _require_nonempty_str(receipt.get("run_id"), "receipt.run_id")
    echo = _validate_capability(receipt.get("capability_echo"), "receipt.capability_echo")
    if request is not None:
        if echo != request["execution_capability"]:
            raise ProtocolError("PYBENCH_CAPABILITY_MISMATCH: capability echo differs")
        if receipt["op_name"] != request["op_name"] or receipt["run_id"] != request["run"]["run_id"]:
            raise ProtocolError("PYBENCH_RECEIPT_MISMATCH: operation/run differs")
    if status == "ok":
        _validate_output_specs(receipt.get("outputs"), "receipt.outputs")
        manifest = receipt.get("write_manifest")
        if not isinstance(manifest, list):
            raise ProtocolError("receipt.write_manifest must be an array")
        for index, item in enumerate(manifest):
            _validate_allowance(item, f"receipt.write_manifest[{index}]", receipt=True)
        if request is not None:
            for name, output in receipt["outputs"].items():
                expected = request["outputs"].get(name)
                if expected is None:
                    raise ProtocolError(
                        "PYBENCH_RECEIPT_MISMATCH: receipt names unknown output"
                    )
                if output.get("artifact_id") != expected["artifact_id"] or output.get(
                    "type"
                ) != expected["type"]:
                    raise ProtocolError(
                        "PYBENCH_RECEIPT_MISMATCH: receipt output binding differs"
                    )
            for descriptor in manifest:
                matches = []
                for allowed in request["execution_capability"]["write_allowlist"]:
                    same = all(
                        descriptor[key] == allowed[key]
                        for key in (
                            "output_name",
                            "output_id",
                            "output_kind",
                            "mutation_class",
                        )
                    )
                    if not same:
                        continue
                    if descriptor["mutation_class"] == "sqlite_rows":
                        same = descriptor["table"] == allowed["table"] and descriptor[
                            "key_scope"
                        ] == allowed["key_scope"]
                    else:
                        relative = descriptor["path"].rstrip("/")
                        prefix = allowed["path"].rstrip("/")
                        same = relative == prefix or relative.startswith(prefix)
                    if same:
                        matches.append(allowed)
                if len(matches) != 1:
                    raise ProtocolError(
                        "PYBENCH_RECEIPT_MISMATCH: manifest descriptor is unrequested or ambiguous"
                    )
    else:
        _require_nonempty_str(receipt.get("error_class"), "receipt.error_class")
        _require_nonempty_str(receipt.get("message"), "receipt.message")
    return receipt


def load_request(path: str | Path) -> dict[str, Any]:
    return validate_request(json.loads(Path(path).read_text()))


def write_receipt(
    path: str | Path,
    receipt: dict[str, Any],
    request: dict[str, Any] | None = None,
) -> None:
    validate_receipt(receipt, request=request)
    Path(path).write_text(json.dumps(receipt, indent=2, allow_nan=False))


def ok_receipt(
    *,
    op_name: str,
    run_id: str,
    outputs: dict[str, Any],
    request: dict[str, Any],
    bundle: BundleHandle,
    **_ignored: Any,
) -> dict[str, Any]:
    return {
        "message_type": "receipt",
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok",
        "op_name": op_name,
        "run_id": run_id,
        "capability_echo": request["execution_capability"],
        "outputs": outputs,
        "write_manifest": build_write_manifest(bundle),
    }


def error_receipt_from_exception(
    *, op_name: str, run_id: str, exc: Exception, request: dict[str, Any]
) -> dict[str, Any]:
    message = str(exc).replace(str(request.get("bundle_dir", "")), "<bundle>")
    return {
        "message_type": "receipt",
        "protocol_version": PROTOCOL_VERSION,
        "status": "error",
        "op_name": str(op_name),
        "run_id": str(run_id),
        "capability_echo": request["execution_capability"],
        "error_class": exc.__class__.__name__,
        "message": message or exc.__class__.__name__,
    }
