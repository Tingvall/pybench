from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .authenticated_core_reads import validate_authenticated_core_read
from .cell_set_family import normalize_cell_set_family
from .feature_set_registry import validate_registry_read
from .operation_packs import OperationPackError, canonical_json, canonical_sha256, protocol_asset
from .operation_packs_v2 import (
    PROCESS_PROTOCOL_V3,
    PackDiscoveryV2,
    _identity,
    active_operation_packs_v2,
    distribution_identity_v2,
)

REQUEST_SCHEMA = "scworkbench.operation_pack.transition_request.v2"
RECEIPT_SCHEMA = "scworkbench.operation_pack.transition_receipt.v2"
_REQUEST_ASSET = "operation-pack-transition-request-v2.schema.json"
_RECEIPT_ASSET = "operation-pack-transition-receipt-v2.schema.json"
_WRITER_METHODS = {
    "write_matrix",
    "write_space",
    "write_space_2d",
    "write_model_ref",
    "write_terminal_primitive",
    "write_path_2d_family",
    "write_space_path_family",
    "request_cell_set_admission",
    "request_cell_set_family_admission",
}
_CELL_KINDS = {
    "matrix",
    "space",
    "space_2d",
    "cell_vector",
    "cell_vector_family",
    "relation_family",
}
_CONSUMED_CAPABILITIES: set[str] = set()


def _fail(code: str, message: str, phase: str, **details: Any) -> None:
    raise OperationPackError(code, message, phase=phase, **details)


def _validate_schema(value: Any, asset: str, code: str, phase: str) -> None:
    schema = json.loads(protocol_asset(asset).read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        first = errors[0]
        _fail(
            code,
            f"Value failed Draft 2020-12 validation: {first.message}",
            phase,
            instance_path=list(first.absolute_path),
        )


def _assert_self_identity(value: Mapping[str, Any], field: str, code: str, phase: str) -> None:
    claimed = value.get(field)
    payload = {key: item for key, item in value.items() if key != field}
    if not isinstance(claimed, Mapping) or claimed.get("sha256") != canonical_sha256(payload):
        _fail(code, f"Self identity mismatch at {field}", phase, identity_field=field)


def _no_absolute_paths(
    value: Any,
    *,
    code: str = "PYBENCH_CAPABILITY_MISMATCH",
    phase: str = "staging",
) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _no_absolute_paths(child, code=code, phase=phase)
    elif isinstance(value, list):
        for child in value:
            _no_absolute_paths(child, code=code, phase=phase)
    elif isinstance(value, str):
        path = Path(value)
        if (
            value.startswith("file:")
            or path.is_absolute()
            or (len(value) > 2 and value[1:3] == ":\\")
        ):
            _fail(code, "V3 protocol values may not expose absolute host paths", phase)


def validate_request_v3(request: Mapping[str, Any], discovery: PackDiscoveryV2) -> dict[str, Any]:
    value = copy.deepcopy(dict(request))
    if value.get("process_protocol_version") != PROCESS_PROTOCOL_V3:
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "V3 execution rejects non-v3 process transport",
            "staging",
        )
    if isinstance(value.get("outputs"), list) and any(
        isinstance(item, Mapping)
        and isinstance(item.get("descriptor"), Mapping)
        and "contained_matrix_model_sibling_write_grant" in item["descriptor"]
        for item in value["outputs"]
    ):
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "Python execution does not implement matrix-trained contained-model authority",
            "staging",
        )
    _validate_schema(value, _REQUEST_ASSET, "PYBENCH_CAPABILITY_MISMATCH", "staging")
    _no_absolute_paths(value)
    _assert_self_identity(
        value["transition"], "transition_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging"
    )
    for item in value["inputs"]:
        _assert_self_identity(
            item["descriptor"],
            "descriptor_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
    for item in value.get("registry_reads", []):
        _assert_self_identity(
            item["descriptor"],
            "descriptor_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
    for item in value["outputs"]:
        _assert_self_identity(
            item["descriptor"], "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging"
        )
    _assert_self_identity(
        value["execution_capability"],
        "capability_identity",
        "PYBENCH_CAPABILITY_MISMATCH",
        "staging",
    )
    _assert_self_identity(value, "request_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
    identity = discovery.identity
    name = value["operation_authentication"]["name"]
    expected_authentication = {
        "name": name,
        "pack_identity": identity["pack_identity"],
        "manifest_identity": identity["manifest_identity"],
        "manifest_source_identity": discovery.manifest_source_identity,
        "distribution_identity": discovery.distribution_identity,
        "operation_definition_identity": identity["operation_definition_identities"].get(name),
        "entry_point": {
            "value": discovery.manifest["entry_point"],
            "identity": discovery.entry_point_identity,
        },
    }
    if canonical_json(value["operation_authentication"]) != canonical_json(expected_authentication):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Request operation authentication differs from installed pack",
            "staging",
        )
    operation = next(
        (item for item in discovery.manifest["operations"] if item["qualified_name"] == name), None
    )
    if operation is None:
        _fail("OPERATION_PACK_REQUIRED", f"Operation {name!r} is absent", "resolution")
    input_names = [item["name"] for item in value["inputs"]]
    required = [item["name"] for item in operation["inputs"] if item["required"]]
    allowed = [item["name"] for item in operation["inputs"]]
    if (
        not set(required) <= set(input_names)
        or not set(input_names) <= set(allowed)
        or len(input_names) != len(set(input_names))
    ):
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Request input closure differs", "resolution")
    declarations = {item["name"]: item for item in operation["inputs"]}
    for item in value["inputs"]:
        declaration = declarations[item["name"]]
        descriptor = item["descriptor"]
        if (
            descriptor["kind"] != declaration["kind"]
            or descriptor["alignment"] != declaration["alignment"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Input {item['name']!r} type or alignment differs from the manifest",
                "resolution",
            )
    read_declarations = {item["name"]: item for item in operation.get("registry_reads", [])}
    read_grants = value.get("registry_reads", [])
    if [item["name"] for item in read_grants] != sorted(read_declarations):
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Registry read closure differs", "resolution")
    input_descriptors = {item["name"]: item["descriptor"] for item in value["inputs"]}
    for item in read_grants:
        declaration = read_declarations[item["name"]]
        descriptor = item["descriptor"]
        binding = {
            key: declaration.get(key)
            for key in (
                "namespace_parameter",
                "namespaces_parameter",
                "feature_sets_parameter",
                "no_selector_policy",
                "empty_match_policy",
            )
        }
        if (
            descriptor["matrix_input"] != declaration["matrix_input"]
            or descriptor["selector_binding"] != binding
            or descriptor["matrix_content_identity"]
            != input_descriptors[declaration["matrix_input"]]["content_identity"]
            or descriptor["feature_order_identity"]
            != input_descriptors[declaration["matrix_input"]]["feature_order_identity"]
        ):
            _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Registry read binding differs", "resolution")
    output_names = [item["name"] for item in value["outputs"]]
    expected_outputs = [item["name"] for item in operation["outputs"]]
    if output_names != expected_outputs or len(output_names) != len(set(output_names)):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Request output closure differs", "staging")
    outputs_by_name = {item["name"]: item for item in operation["outputs"]}
    dynamic_outputs = {item["output"] for item in operation.get("dimension_policies", [])}
    for item in value["outputs"]:
        declaration = outputs_by_name[item["name"]]
        descriptor = item["descriptor"]
        declared_tuple = (
            declaration["category"],
            declaration["kind"],
            declaration["storage_profile"],
        )
        granted_tuple = (
            descriptor["category"],
            descriptor["kind"],
            descriptor["storage_profile"],
        )
        if declared_tuple != granted_tuple:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                f"Output {item['name']!r} type tuple differs from the manifest",
                "staging",
            )
        dynamic = (
            item["name"] in dynamic_outputs or descriptor["kind"] == "cell_set_family_admission"
        )
        if dynamic != (descriptor["expected_dimensions"] == [0, 0]):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                f"Output {item['name']!r} dimension policy differs from the manifest",
                "staging",
            )
    cap = value["execution_capability"]
    readers = [
        {
            "input_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in value["inputs"]
    ] + [
        {
            "registry_read_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in read_grants
    ]
    writers = [
        {
            "output_name": item["name"],
            "writer": item["descriptor"]["writer"],
            "grant_identity": item["descriptor"]["grant_identity"],
        }
        for item in value["outputs"]
    ]
    bindings = (
        cap["session_id"] == value["transition"]["session_id"],
        cap["run_id"] == value["transition"]["run_id"],
        cap["checkpoint_generation"] == value["transition"]["checkpoint_generation"],
        cap["transition_identity"] == value["transition"]["transition_identity"],
        cap["source_state_identity"] == value["transition"]["source_state_identity"],
        cap["pack_identity"] == identity["pack_identity"],
        cap["distribution_identity"] == discovery.distribution_identity,
        cap["manifest_source_identity"] == discovery.manifest_source_identity,
        cap["operation_definition_identity"] == identity["operation_definition_identities"][name],
        cap["entry_point_identity"] == discovery.entry_point_identity,
        cap["reader_grants"] == readers,
        cap["writer_grants"] == writers,
        cap["selected_requirement_set_identity"]
        == value["dependency_resolution"]["selected_requirement_set_identity"],
        cap["preflight_report_identity"]
        == value["dependency_resolution"]["preflight_report_identity"],
    )
    if not all(bindings) or cap["mode"] != "private_staging_write" or cap["single_use"] is not True:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Capability bindings differ from request", "staging")
    errors = sorted(
        Draft202012Validator(operation["params_schema"]).iter_errors(value["params"]),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        _fail("OPERATION_PACK_PARAM_SCHEMA_INVALID", errors[0].message, "resolution")
    return value


def validate_receipt_v3(
    receipt: Mapping[str, Any],
    request: Mapping[str, Any],
    stage_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    value = copy.deepcopy(dict(receipt))
    if value.get("process_protocol_version") != PROCESS_PROTOCOL_V3:
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "V3 receipt rejects non-v3 process transport",
            "receipt",
        )
    _validate_schema(value, _RECEIPT_ASSET, "PYBENCH_RECEIPT_MISMATCH", "receipt")
    _no_absolute_paths(value)
    _assert_self_identity(value, "receipt_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt")
    expected = (
        value["transition_id"] == request["transition"]["transition_id"],
        value["session_id"] == request["transition"]["session_id"],
        value["run_id"] == request["transition"]["run_id"],
        value["transition_identity"] == request["transition"]["transition_identity"],
        value["request_identity"] == request["request_identity"],
        value["capability_identity"] == request["execution_capability"]["capability_identity"],
    )
    if not all(expected):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Receipt binding differs from request", "receipt")
    if value["status"] == "ok":
        observed = value["outputs"] + value["registry_requests"]
        grants = request["outputs"]
        observed_keys = [
            (
                item["name"],
                item["allocated_id"],
                item["descriptor"]["writer"],
                item["descriptor"]["grant_identity"]["sha256"],
            )
            for item in observed
        ]
        grant_keys = [
            (
                item["name"],
                item["allocated_id"],
                item["descriptor"]["writer"],
                item["descriptor"]["grant_identity"]["sha256"],
            )
            for item in grants
        ]
        if sorted(observed_keys) != sorted(grant_keys) or len(observed_keys) != len(
            set(observed_keys)
        ):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Receipt is not a bijection with grants", "receipt")
        for item in observed:
            _assert_self_identity(
                item["descriptor"],
                "observed_descriptor_identity",
                "PYBENCH_RECEIPT_MISMATCH",
                "receipt",
            )
            if item["descriptor"]["kind"] == "cell_set_family_admission" and stage_root is not None:
                root = _safe_root(Path(stage_root))
                relative = f"registry_requests/{item['name']}/payload.json"
                path = _safe_relative(root, relative, must_exist=True)
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    first_input = request["inputs"][0]["name"]
                    projected = json.loads(
                        _safe_relative(
                            root, f"inputs/{first_input}/payload.json", must_exist=True
                        ).read_text(encoding="utf-8")
                    )
                    grant = next(grant for grant in grants if grant["name"] == item["name"])
                    expected = _observed_descriptor(
                        grant,
                        _Write(payload, path, {}, item["descriptor"]["writer"]),
                        projected.get("cell_id", []),
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, StopIteration) as exc:
                    _fail(
                        "PYBENCH_RECEIPT_MISMATCH",
                        f"Family evidence is unreadable: {exc}",
                        "receipt",
                    )
                if canonical_json(item["descriptor"]) != canonical_json(expected):
                    _fail(
                        "PYBENCH_RECEIPT_MISMATCH",
                        "Family receipt differs from staged payload",
                        "receipt",
                    )
        read_grants = request.get("registry_reads", [])
        read_observed = value.get("registry_reads", [])
        if [item["name"] for item in read_observed] != [item["name"] for item in read_grants]:
            _fail("PYBENCH_RECEIPT_MISMATCH", "Registry read receipt closure differs", "receipt")
        for grant, item in zip(read_grants, read_observed, strict=True):
            source = grant["descriptor"]
            expected_read = {
                key: source[key]
                for key in (
                    "category",
                    "kind",
                    "reader",
                    "descriptor_identity",
                    "content_identity",
                    "source_identity",
                    "ancestry_identity",
                    "payload_manifest_identity",
                )
            }
            expected_read["use_count"] = 1
            expected_read["observed_descriptor_identity"] = _identity(
                "scworkbench.operation_pack.observed_registry_read.v2",
                expected_read,
                "descriptor excluding observed_descriptor_identity",
            )
            if item["descriptor"] != expected_read:
                _fail("PYBENCH_RECEIPT_MISMATCH", "Registry read observation differs", "receipt")
            if stage_root is not None:
                root = _safe_root(Path(stage_root))
                path = _safe_relative(
                    root, f"registry_reads/{grant['name']}/payload.json", must_exist=True
                )
                binding = source["selector_binding"]
                declaration = {"matrix_input": source["matrix_input"], **binding}
                matrix = next(
                    input_item["descriptor"]
                    for input_item in request["inputs"]
                    if input_item["name"] == source["matrix_input"]
                )
                validate_registry_read(
                    path.read_bytes(), grant["name"], source, declaration, request["params"], matrix
                )
        _assert_self_identity(
            value["mutation_manifest"],
            "mutation_identity",
            "PYBENCH_RECEIPT_MISMATCH",
            "receipt",
        )
    return value


def _selected(selector: Mapping[str, Any] | None, params: Mapping[str, Any]) -> bool:
    if selector is None:
        return True
    return canonical_json(params.get(selector["parameter"])) == canonical_json(selector["equals"])


def _distribution_evidence(
    name: str, specifier: str, approved: str | None = None
) -> dict[str, Any]:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        _fail("OPERATION_PACK_DEPENDENCY_MISSING", f"Distribution {name!r} is absent", "preflight")
    try:
        compatible = Version(distribution.version) in SpecifierSet(specifier)
    except (InvalidVersion, InvalidSpecifier):
        compatible = False
    if not compatible:
        _fail(
            "OPERATION_PACK_DEPENDENCY_INCOMPATIBLE",
            f"Distribution {name!r} has incompatible version",
            "preflight",
        )
    identity = distribution_identity_v2(distribution)
    if approved and identity["sha256"] != approved:
        _fail(
            "OPERATION_PACK_DEPENDENCY_INCOMPATIBLE",
            f"Distribution {name!r} content identity differs",
            "preflight",
        )
    return {
        "name": distribution.metadata.get("Name", name),
        "normalized_name": canonicalize_name(name),
        "version": distribution.version,
        "artifact_identity": identity,
    }


def _dynamic_value(payload: Mapping[str, Any], field: str) -> str:
    value: Any = payload.get("meta", {})
    for part in field.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value if isinstance(value, str) else ""


def preflight_v3(
    operation: Mapping[str, Any],
    input_payloads: Mapping[str, Mapping[str, Any]],
    params: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    declared = operation.get("dependencies", [])
    selected: list[Mapping[str, Any]] = []
    dispositions: dict[str, Any] = {}
    dynamic: list[dict[str, Any]] = []
    for dependency in declared:
        requirement_id = dependency["requirement_id"]
        selected_now = dependency["disposition"] == "required" or _selected(
            dependency.get("selected_when"), params
        )
        if not selected_now:
            dispositions[requirement_id] = {
                "requirement_id": requirement_id,
                "status": "not_selected",
            }
            continue
        selected.append(dependency)
        kind = dependency["kind"]
        if kind == "python_distribution":
            evidence = _distribution_evidence(
                dependency["name"],
                dependency["version"]["specifier"],
                dependency.get("approved_content_sha256"),
            )
            if importlib.util.find_spec(dependency["import_probe"]) is None:
                _fail(
                    "OPERATION_PACK_DEPENDENCY_MISSING",
                    f"Import probe {dependency['import_probe']!r} is unavailable",
                    "preflight",
                )
            dispositions[requirement_id] = {
                "requirement_id": requirement_id,
                "status": "available",
                **evidence,
            }
        elif kind == "dynamic_input_metadata_package":
            payload = input_payloads.get(dependency["input_name"], {})
            provider = _dynamic_value(payload, dependency["metadata_field"])
            if (
                not provider
                or dependency["ecosystem"] != "Python"
                or not all(
                    part and part.replace("-", "").replace("_", "").isalnum()
                    for part in provider.split(".")
                )
            ):
                _fail(
                    "OPERATION_PACK_DYNAMIC_PROVIDER_INVALID",
                    "Dynamic provider metadata is absent or malformed",
                    "preflight",
                )
            evidence = _distribution_evidence(provider, ">=0")
            if importlib.util.find_spec(provider.replace("-", "_")) is None:
                _fail(
                    "OPERATION_PACK_DEPENDENCY_MISSING",
                    f"Dynamic provider {provider!r} import is unavailable",
                    "preflight",
                )
            input_record = next(
                item for item in request["inputs"] if item["name"] == dependency["input_name"]
            )
            resolution = {
                "requirement_id": requirement_id,
                "selector_identity": _identity(
                    "scworkbench.operation_pack.dynamic_dependency_selector.v2", dependency
                ),
                "input_name": dependency["input_name"],
                "input_identity": input_record["descriptor"]["content_identity"],
                "metadata_field": dependency["metadata_field"],
                "ecosystem": "Python",
                "provider_name": provider,
                "package_name_scheme": "python_distribution_name_v1",
                "probe": "python_import_spec_v1",
                "disposition": "required",
                "resolved_package": {
                    "name": evidence["name"],
                    "version": evidence["version"],
                    "artifact_identity": evidence["artifact_identity"],
                    "probe_evidence_identity": _identity(
                        "scworkbench.python_import_spec.v1", {"import_name": provider}
                    ),
                },
                "status": "available",
            }
            resolution["resolution_identity"] = _identity(
                "scworkbench.operation_pack.dynamic_dependency_resolution.v2",
                resolution,
                "resolution excluding resolution_identity",
            )
            dynamic.append(resolution)
            dispositions[requirement_id] = resolution
        elif kind == "device_runtime":
            available = dependency["name"] == "cpu"
            if not available:
                _fail(
                    "OPERATION_PACK_PREFLIGHT_FAILED",
                    f"Device {dependency['name']!r} is unavailable",
                    "preflight",
                )
            dispositions[requirement_id] = {
                "requirement_id": requirement_id,
                "status": "available",
                "name": dependency["name"],
            }
        elif kind == "system_library":
            probe = dependency["probe"]
            available = (
                bool(shutil.which(probe["target"]))
                if probe["kind"]
                in {
                    "executable_version",
                    "pkg_config_version",
                }
                else False
            )
            if not available:
                _fail(
                    "OPERATION_PACK_PREFLIGHT_FAILED",
                    f"System dependency {dependency['name']!r} is unavailable",
                    "preflight",
                )
            dispositions[requirement_id] = {
                "requirement_id": requirement_id,
                "status": "available",
                "probe": probe,
            }
        else:
            _fail(
                "OPERATION_PACK_PREFLIGHT_FAILED",
                f"Dependency kind {kind!r} is unavailable to Python",
                "preflight",
            )
    declared_identity = _identity("scworkbench.operation_pack.dependencies.v2", declared)
    selected_identity = _identity("scworkbench.operation_pack.dependencies.v2", selected)
    report_value = {
        "declared": declared,
        "selected": selected,
        "dispositions": dispositions,
        "dynamic": dynamic,
        "network_policy": "deny",
    }
    report_identity = _identity("scworkbench.operation_pack.preflight.v2", report_value)
    declared_request = request["dependency_resolution"]
    if (
        declared_request["declared_requirement_set_identity"] != declared_identity
        or declared_request["selected_requirement_set_identity"] != selected_identity
        or declared_request["dynamic_resolutions"] != dynamic
        or declared_request["preflight_report_identity"] != report_identity
        or declared_request["network_policy"] != "deny"
    ):
        _fail(
            "OPERATION_PACK_DYNAMIC_PROVIDER_CHANGED"
            if dynamic
            else "OPERATION_PACK_PREFLIGHT_FAILED",
            "Core-resolved dependency evidence differs from Python preflight",
            "dispatch" if dynamic else "preflight",
        )
    return {
        "declared_identity": declared_identity,
        "selected_identity": selected_identity,
        "preflight_identity": report_identity,
        "dynamic": dynamic,
        "dispositions": dispositions,
    }


def _safe_root(root: Path) -> Path:
    root = root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Private stage root is invalid", "staging")
    return root


def _safe_relative(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Path is outside private stage", "execution")
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Symlink in private staged path", "execution")
    if must_exist and not current.is_file():
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Projected input payload is absent", "resolution")
    return current


def _snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Private stage contains a symlink", "audit")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _changed_snapshot(root: Path, before: Mapping[str, bytes]) -> list[str]:
    changed: set[str] = set()
    observed: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Private stage contains a symlink", "audit")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        observed.add(relative)
        prior = before.get(relative)
        if prior is None:
            changed.add(relative)
        else:
            prior_view = memoryview(prior)
        offset = 0
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                if prior is not None and block != bytes(prior_view[offset : offset + len(block)]):
                    changed.add(relative)
                offset += len(block)
        if prior is not None and offset != len(prior):
            changed.add(relative)
    return sorted(changed | (before.keys() - observed))


def _tree(root: Path) -> set[str]:
    result: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Private stage contains a symlink", "audit")
        result.add(path.relative_to(root).as_posix())
    return result


def _restore(root: Path, before: Mapping[str, bytes]) -> None:
    for path in sorted(root.iterdir()):
        shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink(
            missing_ok=True
        )
    for relative, payload in before.items():
        path = _safe_relative(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _dimensions(value: Mapping[str, Any]) -> list[int]:
    if value.get("kind") == "cell_set_family_admission":
        sets = value.get("sets", [])
        return [len(sets), sum(len(item["cells"]) for item in sets)]
    dimensions = value.get("dimensions")
    if (
        isinstance(dimensions, list)
        and dimensions
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and float(item).is_integer()
            and item >= 0
            for item in dimensions
        )
    ):
        return [int(item) for item in dimensions]
    _fail("PYBENCH_OUTPUT_INVALID", "Primitive dimensions are absent or invalid", "audit")


def _ordered(value: Mapping[str, Any], key: str) -> list[str]:
    result = value.get(key, [])
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item for item in result
    ):
        _fail("PYBENCH_OUTPUT_INVALID", f"Primitive {key} is invalid", "audit")
    return result


@dataclass
class _Write:
    value: dict[str, Any]
    path: Path
    semantic_metadata: dict[str, Any]
    writer: str
    paths: list[Path] = field(default_factory=list)


class V3Readers(Mapping[str, Callable[..., dict[str, Any]]]):
    def __init__(
        self,
        values: Mapping[str, dict[str, Any]],
        registry_values: Mapping[str, dict[str, Any]] | None = None,
    ):
        self._values = dict(values)
        self._registry_values = dict(registry_values or {})
        self._registry_calls: list[str] = []
        self._calls: list[dict[str, Any]] = []

    @property
    def calls(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._calls)

    @property
    def registry_calls(self) -> list[str]:
        return list(self._registry_calls)

    def __getitem__(self, key: str) -> Callable[..., dict[str, Any]]:
        if key in self._registry_values:

            def read_registry(*args, **kwargs):
                if args or kwargs or key in self._registry_calls:
                    _fail(
                        "PYBENCH_CAPABILITY_MISMATCH",
                        "Registry reader accepts no arguments and is single use",
                        "execution",
                    )
                self._registry_calls.append(key)
                return copy.deepcopy(self._registry_values[key])

            return read_registry
        if key not in self._values:
            raise KeyError(key)
        value = self._values[key]
        if value["kind"] != "matrix":

            def read_primitive(*args, **kwargs):
                if args or kwargs:
                    _fail(
                        "PYBENCH_CAPABILITY_MISMATCH",
                        "Only read_matrix accepts a typed selection",
                        "execution",
                    )
                return copy.deepcopy(value)

            return read_primitive

        def read_matrix(*, features=None, cells=None):
            allowed_features = list(value.get("feature_id", []))
            allowed_cells = list(value.get("cell_id", []))
            selected_features = allowed_features if features is None else list(features)
            selected_cells = allowed_cells if cells is None else list(cells)
            if (
                any(not isinstance(item, str) for item in selected_features + selected_cells)
                or not set(selected_features) <= set(allowed_features)
                or not set(selected_cells) <= set(allowed_cells)
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    "Matrix reader selection exceeds the authenticated input descriptor",
                    "execution",
                )
            feature_index = [allowed_features.index(item) for item in selected_features]
            cell_index = [allowed_cells.index(item) for item in selected_cells]
            result = copy.deepcopy(value)
            source_values = value.get("values")
            if not isinstance(source_values, list):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    "Projected Python matrix has no readable values",
                    "execution",
                )
            result["values"] = [
                [source_values[row][column] for column in cell_index] for row in feature_index
            ]
            result["feature_id"] = selected_features
            result["cell_id"] = selected_cells
            result["dimensions"] = [len(selected_features), len(selected_cells)]
            self._calls.append(
                {"input": key, "features": selected_features, "cells": selected_cells}
            )
            return result

        return read_matrix

    def __iter__(self):
        return iter((*self._values, *self._registry_values))

    def __len__(self) -> int:
        return len(self._values) + len(self._registry_values)


class V3Writers:
    def __init__(self, request: Mapping[str, Any], stage_root: Path, cell_order: list[str]):
        self._grants = {item["name"]: item for item in request["outputs"]}
        self._stage_root = stage_root
        self._cell_order = cell_order
        self._writes: dict[str, _Write] = {}
        self._consumed = False

    @property
    def writes(self) -> Mapping[str, _Write]:
        return dict(self._writes)

    def consume(self) -> None:
        self._consumed = True

    def _write(
        self,
        writer: str,
        name: str,
        value: Mapping[str, Any],
        semantic_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._consumed:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Capability is already consumed", "execution")
        grant = self._grants.get(name)
        if (
            grant is None
            or grant["descriptor"]["writer"] != writer
            or writer not in _WRITER_METHODS
        ):
            _fail("PYBENCH_CAPABILITY_MISMATCH", f"Writer is not granted for {name!r}", "execution")
        if name in self._writes:
            _fail("PYBENCH_RECEIPT_MISMATCH", f"Output {name!r} was written twice", "receipt")
        payload = copy.deepcopy(dict(value))
        expected_kind = grant["descriptor"]["kind"]
        if payload.get("kind") != expected_kind:
            _fail("PYBENCH_OUTPUT_INVALID", f"Output {name!r} has the wrong kind", "execution")
        if expected_kind == "cell_set_family_admission":
            payload, _ = normalize_cell_set_family(payload, self._cell_order)
        relative_root = grant["descriptor"].get("relative_root", f"registry_requests/{name}")
        output_root = _safe_relative(self._stage_root, relative_root)
        if output_root.exists():
            _fail(
                "PYBENCH_OUT_OF_SCOPE_MUTATION", "Granted output root already exists", "execution"
            )
        output_root.mkdir(parents=True)
        path = output_root / "payload.json"
        path.write_text(canonical_json(payload), encoding="utf-8")
        self._writes[name] = _Write(payload, path, dict(semantic_metadata or {}), writer)
        return {
            "name": name,
            "allocated_id": grant["allocated_id"],
            "grant_identity": grant["descriptor"]["grant_identity"],
        }

    def write_matrix(self, name, value, semantic_metadata=None):
        return self._write("write_matrix", name, value, semantic_metadata)

    def write_space(self, name, value, semantic_metadata=None):
        return self._write("write_space", name, value, semantic_metadata)

    def write_space_2d(self, name, value, semantic_metadata=None):
        return self._write("write_space_2d", name, value, semantic_metadata)

    def write_model_ref(self, name, value, semantic_metadata=None):
        return self._write("write_model_ref", name, value, semantic_metadata)

    def write_terminal_primitive(self, name, value, semantic_metadata=None):
        return self._write("write_terminal_primitive", name, value, semantic_metadata)

    def write_path_2d_family(self, name, value, semantic_metadata=None):
        return self._write("write_path_2d_family", name, value, semantic_metadata)

    def write_space_path_family(self, name, value, semantic_metadata=None):
        return self._write("write_space_path_family", name, value, semantic_metadata)

    def request_cell_set_admission(self, name, value, semantic_metadata=None):
        return self._write("request_cell_set_admission", name, value, semantic_metadata)

    def request_cell_set_family_admission(self, name, value, semantic_metadata=None):
        return self._write("request_cell_set_family_admission", name, value, semantic_metadata)


def _audit_chunk_calls(
    operation: Mapping[str, Any],
    params: Mapping[str, Any],
    readers: V3Readers,
    inputs: Mapping[str, Mapping[str, Any]],
    request: Mapping[str, Any],
) -> dict[str, Any] | None:
    chunk = operation.get("chunking")
    if chunk is None:
        return None
    sequence = params.get(chunk["sequence_parameter"])
    if sequence is None:
        sequence = inputs[chunk["input"]].get("feature_id", [])
    if not isinstance(sequence, list) or any(not isinstance(item, str) for item in sequence):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Chunk sequence is not an ordered feature list",
            "execution",
        )
    size = params.get(chunk["size_parameter"])
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk size is invalid", "execution")
    expected = [sequence[index : index + size] for index in range(0, len(sequence), size)]
    observed = [call["features"] for call in readers.calls if call["input"] == chunk["input"]]
    if observed != expected or any(not batch or len(batch) > size for batch in observed):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Observed matrix-reader calls do not implement the authenticated chunk declaration",
            "execution",
            expected_batches=expected,
            observed_batches=observed,
        )
    input_record = next(item for item in request["inputs"] if item["name"] == chunk["input"])
    observation = {
        "request_identity": request["request_identity"],
        "capability_identity": request["execution_capability"]["capability_identity"],
        "operation_definition_identity": request["operation_authentication"][
            "operation_definition_identity"
        ],
        "input_name": chunk["input"],
        "input_descriptor_identity": input_record["descriptor"]["descriptor_identity"],
        "axis": chunk["axis"],
        "sequence_parameter": chunk["sequence_parameter"],
        "size_parameter": chunk["size_parameter"],
        "requested_chunk_size": size,
        "observed_call_count": len(observed),
        "observed_batches": observed,
        "observed_batch_sizes": [len(batch) for batch in observed],
    }
    observation["observation_identity"] = _identity(
        "pybench.operation_pack.chunk_observation.v1",
        observation,
        "chunk observation excluding observation_identity",
    )
    return observation


def _read_inputs(request: Mapping[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for item in request["inputs"]:
        name, descriptor = item["name"], item["descriptor"]
        path = _safe_relative(root, f"inputs/{name}/payload.json", must_exist=True)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Input {name!r} is unreadable: {exc}",
                "resolution",
            )
        if not isinstance(value, dict) or value.get("kind") != descriptor["kind"]:
            _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", f"Input {name!r} type differs", "resolution")
        if (
            _identity(descriptor["content_identity"]["scheme"], value)
            != descriptor["content_identity"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH", f"Input {name!r} content differs", "resolution"
            )
        if _dimensions(value) != descriptor["dimensions"]:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH", f"Input {name!r} dimensions differ", "resolution"
            )
        if value.get("storage_profile") != descriptor["storage_profile"]:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Input {name!r} storage profile differs",
                "resolution",
            )
        for key, identity_key, scheme in (
            ("cell_id", "cell_order_identity", "scworkbench.cell_order.v1"),
            ("feature_id", "feature_order_identity", "scworkbench.feature_order.v1"),
        ):
            if (
                identity_key in descriptor
                and _identity(scheme, _ordered(value, key)) != descriptor[identity_key]
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH", f"Input {name!r} {key} differs", "resolution"
                )
        if (
            "semantic_metadata_identity" in descriptor
            and _identity("scworkbench.operation_pack.semantic_metadata.v2", value.get("meta", {}))
            != descriptor["semantic_metadata_identity"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Input {name!r} semantic metadata differs",
                "resolution",
            )
        if (
            descriptor["kind"] == "path_2d_family"
            and _identity(
                "scworkbench.row_order.v1", [value.get("key", []), value.get("point", [])]
            )
            != descriptor["row_order_identity"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH", f"Input {name!r} row order differs", "resolution"
            )
        if descriptor["kind"] == "space_path_family":
            row_identity = _identity(
                "scworkbench.row_order.v1",
                [value.get(key, []) for key in ("space_id", "path_id", "point", "dim")],
            )
            linked_identity = _identity(
                "scworkbench.space.content.v1", sorted(set(value.get("space_id", [])))
            )
            if (
                row_identity != descriptor["row_order_identity"]
                or linked_identity != descriptor["linked_space_identity"]
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    f"Input {name!r} row order or linked space differs",
                    "resolution",
                )
        if descriptor["kind"] == "model_ref":
            tree = value.get("payload_tree")
            contained = isinstance(tree, dict) and not any(
                Path(child).is_absolute() or ".." in Path(child).parts for child in tree
            )
            if (
                not contained
                or _identity("scworkbench.model_linkage.v1", value)
                != descriptor["model_linkage_identity"]
                or _identity("scworkbench.operation_pack.contained_payload.v2", value)
                != descriptor["contained_payload_identity"]
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    f"Input {name!r} model linkage or containment differs",
                    "resolution",
                )
        values[name] = value
    return values


def _read_registry_reads(
    request: Mapping[str, Any], operation: Mapping[str, Any], root: Path
) -> dict[str, dict[str, Any]]:
    declarations = {item["name"]: item for item in operation.get("registry_reads", [])}
    matrices = {item["name"]: item["descriptor"] for item in request["inputs"]}
    values: dict[str, dict[str, Any]] = {}
    for item in request.get("registry_reads", []):
        name = item["name"]
        path = _safe_relative(root, f"registry_reads/{name}/payload.json", must_exist=True)
        declaration = declarations[name]
        raw = path.read_bytes()
        if declaration["kind"] == "feature_set_registry":
            values[name] = validate_registry_read(
                raw,
                name,
                item["descriptor"],
                declaration,
                request["params"],
                matrices[declaration["matrix_input"]],
            )
        else:
            values[name] = validate_authenticated_core_read(
                raw, name, item["descriptor"], declaration, request
            )
    return values


def _observed_descriptor(
    grant: Mapping[str, Any], write: _Write, cell_order: list[str]
) -> dict[str, Any]:
    expected = grant["descriptor"]
    value = write.value
    kind = expected["kind"]
    dimensions = _dimensions(value)
    dynamic_table = kind in {
        "feature_table",
        "obs_table",
        "cell_set_family_admission",
    } and expected["expected_dimensions"] == [
        0,
        0,
    ]
    if not dynamic_table and dimensions != expected["expected_dimensions"]:
        _fail("PYBENCH_OUTPUT_INVALID", f"Output {grant['name']!r} dimensions differ", "audit")
    observed: dict[str, Any] = {
        "category": expected["category"],
        "kind": kind,
        "storage_profile": expected["storage_profile"],
        "writer": expected["writer"],
        "grant_identity": expected["grant_identity"],
        "dimensions": dimensions,
        "alignment": expected["expected_alignment"],
        "ancestry_identity": expected["expected_ancestry_identity"],
    }
    if kind == "cell_set_family_admission":
        normalized, identities = normalize_cell_set_family(value, cell_order)
        if (
            normalized != value
            or _identity("scworkbench.cell_order.v1", cell_order)
            != expected["expected_cell_order_identity"]
        ):
            _fail(
                "PYBENCH_OUTPUT_INVALID",
                "Family payload or cell order differs from its grant",
                "audit",
            )
        observed.update(
            cell_order_identity=expected["expected_cell_order_identity"],
            family_identity=identities["family_identity"],
            member_identities=identities["member_identities"],
            source_identity=identities["source_identity"],
            semantic_validation={
                "constraints_identity": _identity(
                    "scworkbench.operation_pack.semantic_constraints.v2",
                    expected["semantic_constraints"],
                    "cell-set-family admission constraints",
                ),
                "status": "passed",
            },
        )
        observed["ancestry_identity"] = _identity(
            "scworkbench.provenance.ancestry.v1",
            expected["expected_ancestry_identity"],
            "registry request ancestry",
        )
        observed["observed_descriptor_identity"] = _identity(
            "scworkbench.operation_pack.observed_registry_request.v2",
            observed,
            "descriptor excluding observed_descriptor_identity",
        )
        return observed
    if kind == "cell_set_admission":
        cells = _ordered(value, "cells")
        if len(cells) != len(set(cells)):
            _fail("PYBENCH_OUTPUT_INVALID", "Cell-set membership is not unique", "audit")
        observed.update(
            cell_order_identity=_identity(
                "scworkbench.cell_order.v1", cells, "ordered admitted cell identifiers"
            ),
            membership_identity=_identity(
                "scworkbench.cell_set.membership.v1", cells, "ordered exact membership"
            ),
            semantic_validation={
                "constraints_identity": _identity(
                    "scworkbench.operation_pack.semantic_constraints.v2",
                    expected["semantic_constraints"],
                    "cell-set admission constraints",
                ),
                "status": "passed",
            },
        )
        observed["ancestry_identity"] = _identity(
            "scworkbench.provenance.ancestry.v1",
            expected["expected_ancestry_identity"],
            "registry request ancestry",
        )
        observed["observed_descriptor_identity"] = _identity(
            "scworkbench.operation_pack.observed_registry_request.v2",
            observed,
            "descriptor excluding observed_descriptor_identity",
        )
        return observed
    observed.update(
        model_linkage=expected["expected_model_linkage"],
        content_identity=_identity(f"scworkbench.{kind}.content.v1", value),
        payload_manifest_identity=_identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            {
                "path": write.path.relative_to(write.path.parents[2]).as_posix(),
                "size": write.path.stat().st_size,
                "sha256": hashlib.sha256(write.path.read_bytes()).hexdigest(),
            },
        ),
        semantic_metadata_identity=_identity(
            "scworkbench.operation_pack.semantic_metadata.v2", write.semantic_metadata
        ),
        semantic_validation={
            "constraints_identity": _identity(
                "scworkbench.operation_pack.semantic_constraints.v2",
                expected["semantic_constraints"],
            ),
            "status": "passed",
        },
    )
    if kind in _CELL_KINDS:
        observed["cell_order_identity"] = _identity(
            "scworkbench.cell_order.v1", _ordered(value, "cell_id")
        )
        if observed["cell_order_identity"] != expected["expected_cell_order_identity"]:
            _fail("PYBENCH_OUTPUT_INVALID", "Output cell order differs", "audit")
    if kind in {"matrix", "feature_table"}:
        observed["feature_order_identity"] = _identity(
            "scworkbench.feature_order.v1", _ordered(value, "feature_id")
        )
        if observed["feature_order_identity"] != expected["expected_feature_order_identity"]:
            _fail("PYBENCH_OUTPUT_INVALID", "Output feature order differs", "audit")
    if kind == "path_2d_family":
        observed["row_order_identity"] = _identity(
            "scworkbench.row_order.v1", [value.get("key", []), value.get("point", [])]
        )
        if observed["row_order_identity"] != expected["expected_row_order_identity"]:
            _fail("PYBENCH_OUTPUT_INVALID", "Output row order differs", "audit")
    if kind == "space_path_family":
        observed["row_order_identity"] = _identity(
            "scworkbench.row_order.v1",
            [value.get(key, []) for key in ("space_id", "path_id", "point", "dim")],
        )
        observed["linked_space_identity"] = expected["expected_linked_space_identity"]
        if observed["row_order_identity"] != expected["expected_row_order_identity"]:
            _fail("PYBENCH_OUTPUT_INVALID", "Output row order differs", "audit")
        actual_link = _identity(
            "scworkbench.space.content.v1", sorted(set(value.get("space_id", [])))
        )
        if actual_link != expected["expected_linked_space_identity"]:
            _fail("PYBENCH_OUTPUT_INVALID", "Output linked space differs", "audit")
    if kind == "model_ref":
        payload_tree = value.get("payload_tree")
        if not isinstance(payload_tree, dict) or any(
            Path(key).is_absolute() or ".." in Path(key).parts for key in payload_tree
        ):
            _fail("PYBENCH_OUTPUT_INVALID", "Model payload is not contained", "audit")
        observed["model_linkage_identity"] = _identity("scworkbench.model_linkage.v1", value)
        observed["contained_payload"] = {
            "contained": True,
            "no_symlinks": True,
            "payload_tree_identity": _identity(
                "scworkbench.operation_pack.contained_payload.v2", value
            ),
        }
    observed["observed_descriptor_identity"] = _identity(
        "scworkbench.operation_pack.observed_output.v2",
        observed,
        "descriptor excluding observed_descriptor_identity",
    )
    return observed


def _python_runtime_sidecar(
    discovery: PackDiscoveryV2,
    request: Mapping[str, Any],
    chunk_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    seed = request["params"].get("seed")
    value = {
        "schema": "pybench.operation_pack.python_runtime.v3",
        "backend": "Python",
        "distribution_identity": discovery.distribution_identity,
        "interpreter": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "abi": sysconfig.get_config_var("SOABI") or "unknown",
            "platform": sysconfig.get_platform(),
            "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        },
        "seed": {
            "status": "declared" if "seed" in request["params"] else "not_declared",
            "value": seed,
        },
        "device": {"type": "cpu", "descriptor": platform.machine() or "CPU"},
    }
    if chunk_observation is not None:
        value["chunk_observation"] = copy.deepcopy(dict(chunk_observation))
    value["runtime_identity"] = _identity(
        "pybench.operation_pack.python_runtime.v3",
        value,
        "python runtime sidecar excluding runtime_identity",
    )
    return value


def validate_python_runtime_sidecar_v3(
    sidecar: Mapping[str, Any],
    request: Mapping[str, Any],
    discovery: PackDiscoveryV2,
    inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    request = validate_request_v3(request, discovery)
    value = copy.deepcopy(dict(sidecar))
    code = "PYBENCH_RUNTIME_ATTESTATION_MISMATCH"
    phase = "runtime_attestation"
    _validate_schema(value, "pybench-op-v3-python-runtime.schema.json", code, phase)
    _no_absolute_paths(value, code=code, phase=phase)
    _assert_self_identity(value, "runtime_identity", code, phase)
    if value["distribution_identity"] != discovery.distribution_identity:
        _fail(code, "Runtime sidecar distribution differs from installed discovery", phase)
    operation_name = request["operation_authentication"]["name"]
    operation = next(
        (
            item
            for item in discovery.manifest["operations"]
            if item["qualified_name"] == operation_name
        ),
        None,
    )
    if operation is None:
        _fail(code, "Runtime sidecar names an unavailable operation", phase)
    chunk = operation.get("chunking")
    observed = value.get("chunk_observation")
    if chunk is None:
        if observed is not None:
            _fail(code, "Nonchunked operation carries a chunk observation", phase)
        return value
    if observed is None:
        _fail(code, "Chunked operation is missing its chunk observation", phase)
    _assert_self_identity(observed, "observation_identity", code, phase)
    source_input = inputs.get(chunk["input"])
    input_record = next(item for item in request["inputs"] if item["name"] == chunk["input"])
    descriptor = input_record["descriptor"]
    if (
        not isinstance(source_input, Mapping)
        or source_input.get("kind") != descriptor["kind"]
        or _identity(descriptor["content_identity"]["scheme"], source_input)
        != descriptor["content_identity"]
    ):
        _fail(code, "Chunk observation input differs from its authenticated descriptor", phase)
    sequence = request["params"].get(chunk["sequence_parameter"])
    if sequence is None:
        sequence = source_input.get("feature_id", [])
    size = request["params"].get(chunk["size_parameter"])
    if not isinstance(sequence, list) or not isinstance(size, int) or isinstance(size, bool):
        _fail(code, "Chunk observation cannot be resolved from authenticated inputs", phase)
    expected_batches = [sequence[index : index + size] for index in range(0, len(sequence), size)]
    expected = {
        "request_identity": request["request_identity"],
        "capability_identity": request["execution_capability"]["capability_identity"],
        "operation_definition_identity": request["operation_authentication"][
            "operation_definition_identity"
        ],
        "input_name": chunk["input"],
        "input_descriptor_identity": input_record["descriptor"]["descriptor_identity"],
        "axis": chunk["axis"],
        "sequence_parameter": chunk["sequence_parameter"],
        "size_parameter": chunk["size_parameter"],
        "requested_chunk_size": size,
        "observed_call_count": len(expected_batches),
        "observed_batches": expected_batches,
        "observed_batch_sizes": [len(batch) for batch in expected_batches],
    }
    expected["observation_identity"] = _identity(
        "pybench.operation_pack.chunk_observation.v1",
        expected,
        "chunk observation excluding observation_identity",
    )
    if canonical_json(observed) != canonical_json(expected):
        _fail(code, "Chunk observation differs from authenticated execution inputs", phase)
    return value


def _shared_runtime(
    discovery: PackDiscoveryV2,
    operation: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schema": "scworkbench.operation_pack.runtime.v2",
        "backend": "Python",
        "pack_identity": discovery.identity["pack_identity"],
        "manifest_source_identity": discovery.manifest_source_identity,
        "distribution_identity": discovery.distribution_identity,
        "operation_definition_identity": discovery.identity["operation_definition_identities"][
            operation["qualified_name"]
        ],
        "entry_point": {
            "value": discovery.manifest["entry_point"],
            "identity": discovery.entry_point_identity,
        },
        "declared_requirement_set_identity": preflight["declared_identity"],
        "selected_requirement_set_identity": preflight["selected_identity"],
        "dynamic_resolutions": preflight["dynamic"],
        "preflight_report_identity": preflight["preflight_identity"],
        "path_policy": "no_absolute_paths",
    }
    value["runtime_identity"] = _identity(
        "scworkbench.operation_pack.runtime.v2",
        value,
        "runtime attestation excluding runtime_identity",
    )
    return value


def execute_operation_pack_v3(
    request: Mapping[str, Any],
    discovery: PackDiscoveryV2,
    stage_root: str | os.PathLike[str],
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = validate_request_v3(request, discovery)
    root = _safe_root(Path(stage_root))
    before = _snapshot(root)
    tree_before = _tree(root)
    operation_name = request["operation_authentication"]["name"]
    operation = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == operation_name
    )
    active = active_operation_packs_v2().get(discovery.manifest["namespace"])
    if active is None or canonical_json(active[0].identity) != canonical_json(discovery.identity):
        _fail("OPERATION_PACK_REQUIRED", "Exact operation pack is not active", "resolution")
    capability_id = request["execution_capability"]["capability_identity"]["sha256"]
    if capability_id in _CONSUMED_CAPABILITIES:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Capability is already consumed", "staging")
    _CONSUMED_CAPABILITIES.add(capability_id)
    writers: V3Writers | None = None
    try:
        if fault_hook:
            fault_hook("before_input_read")
        inputs = _read_inputs(request, root)
        registry_values = _read_registry_reads(request, operation, root)
        preflight = preflight_v3(operation, inputs, request["params"], request)
        if fault_hook:
            fault_hook("before_dispatch")
        cell_order = list(next(iter(inputs.values())).get("cell_id", [])) if inputs else []
        writers = V3Writers(request, root, cell_order)
        readers = V3Readers(inputs, registry_values)
        active[1].handlers[operation_name](
            readers,
            writers,
            copy.deepcopy(request["params"]),
            {
                "transition_id": request["transition"]["transition_id"],
                "capability_id": request["execution_capability"]["capability_id"],
            },
        )
        expected_registry_names = [item["name"] for item in request.get("registry_reads", [])]
        if sorted(readers.registry_calls) != sorted(expected_registry_names):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Registry readers must be consumed exactly once",
                "execution",
            )
        chunk_observation = _audit_chunk_calls(
            operation, request["params"], readers, inputs, request
        )
        if fault_hook:
            fault_hook("after_handler")
        writes = writers.writes
        expected_names = [item["name"] for item in request["outputs"]]
        if list(writes) != expected_names or len(writes) != len(expected_names):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Returned output closure differs", "receipt")
        writers.consume()
        outputs: list[dict[str, Any]] = []
        registry_requests: list[dict[str, Any]] = []
        registry_reads: list[dict[str, Any]] = []
        for read in request.get("registry_reads", []):
            descriptor = read["descriptor"]
            observed_read = {
                key: descriptor[key]
                for key in (
                    "category",
                    "kind",
                    "reader",
                    "descriptor_identity",
                    "content_identity",
                    "source_identity",
                    "ancestry_identity",
                    "payload_manifest_identity",
                )
            }
            observed_read["use_count"] = 1
            observed_read["observed_descriptor_identity"] = _identity(
                "scworkbench.operation_pack.observed_registry_read.v2",
                observed_read,
                "descriptor excluding observed_descriptor_identity",
            )
            registry_reads.append({"name": read["name"], "descriptor": observed_read})
        entries: list[dict[str, str]] = []
        for grant in request["outputs"]:
            write = writes[grant["name"]]
            try:
                staged_value = json.loads(write.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                _fail("PYBENCH_OUTPUT_INVALID", f"Staged output is unreadable: {exc}", "audit")
            if canonical_json(staged_value) != canonical_json(write.value):
                _fail(
                    "PYBENCH_OUT_OF_SCOPE_MUTATION",
                    "Staged payload changed after its sanctioned writer returned",
                    "audit",
                )
            record = {
                "name": grant["name"],
                "allocated_id": grant["allocated_id"],
                "descriptor": _observed_descriptor(grant, write, cell_order),
            }
            target = (
                registry_requests
                if grant["descriptor"]["category"] == "core_registry_request"
                else outputs
            )
            target.append(record)
            entries.append(
                {
                    "path": write.path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(write.path.read_bytes()).hexdigest(),
                }
            )
        changed = _changed_snapshot(root, before)
        tree_after = _tree(root)
        if sorted(item["path"] for item in entries) != changed:
            _fail(
                "PYBENCH_OUT_OF_SCOPE_MUTATION",
                "Independent stage audit found undeclared mutation",
                "audit",
            )
        expected_created: set[str] = set()
        for entry in entries:
            current = Path(entry["path"])
            expected_created.add(current.as_posix())
            for parent in current.parents:
                if parent == Path("."):
                    break
                expected_created.add(parent.as_posix())
        if tree_after - tree_before != expected_created - tree_before:
            _fail(
                "PYBENCH_OUT_OF_SCOPE_MUTATION",
                "Independent stage audit found undeclared staged paths",
                "audit",
            )
        mutation = {
            "entries_identity": _identity(
                "scworkbench.operation_pack.mutation_entries.v2", entries
            ),
            "entry_count": len(entries),
            "unexpected_entries": 0,
        }
        mutation["mutation_identity"] = _identity(
            "scworkbench.operation_pack.mutation_manifest.v2",
            mutation,
            "mutation manifest excluding mutation_identity",
        )
        runtime = _shared_runtime(discovery, operation, preflight)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "message_type": "receipt",
            "process_protocol_version": PROCESS_PROTOCOL_V3,
            "pack_abi_version": "2",
            "canonical_json_scheme": "scworkbench.operation_pack.canonical_json.v1",
            "status": "ok",
            "transition_id": request["transition"]["transition_id"],
            "session_id": request["transition"]["session_id"],
            "run_id": request["transition"]["run_id"],
            "transition_identity": request["transition"]["transition_identity"],
            "request_identity": request["request_identity"],
            "capability_identity": request["execution_capability"]["capability_identity"],
            "outputs": outputs,
            "registry_requests": registry_requests,
            "mutation_manifest": mutation,
            "runtime_attestation": runtime,
        }
        if registry_reads:
            receipt["registry_reads"] = registry_reads
        receipt["receipt_identity"] = _identity(
            RECEIPT_SCHEMA, receipt, "receipt excluding receipt_identity"
        )
        validate_receipt_v3(receipt, request, root)
        sidecar = _python_runtime_sidecar(discovery, request, chunk_observation)
        validate_python_runtime_sidecar_v3(sidecar, request, discovery, inputs)
        return receipt, sidecar
    except BaseException:
        if writers is not None:
            writers.consume()
        _restore(root, before)
        raise


def error_receipt_v3(
    request: Mapping[str, Any], error: BaseException, runtime: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if isinstance(error, OperationPackError):
        code = (
            error.code
            if error.code
            in {
                "OPERATION_PACK_REQUIRED",
                "OPERATION_PACK_INCOMPATIBLE",
                "OPERATION_PACK_DEPENDENCY_MISSING",
                "OPERATION_PACK_DEPENDENCY_INCOMPATIBLE",
                "OPERATION_PACK_DYNAMIC_PROVIDER_INVALID",
                "OPERATION_PACK_DYNAMIC_PROVIDER_CHANGED",
                "OPERATION_PACK_PREFLIGHT_FAILED",
                "PYBENCH_CAPABILITY_MISMATCH",
                "PYBENCH_STALE_STAGING_IDENTITY",
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "PYBENCH_OUTPUT_INVALID",
                "PYBENCH_RECEIPT_MISMATCH",
                "PYBENCH_OUT_OF_SCOPE_MUTATION",
                "PYBENCH_SOURCE_WRITE_ATTEMPT",
                "PYBENCH_PROCESS_FAILED",
            }
            else "PYBENCH_PROCESS_FAILED"
        )
        phase = error.details.get("phase", "execution")
    else:
        code, phase = "PYBENCH_PROCESS_FAILED", "execution"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "message_type": "receipt",
        "process_protocol_version": PROCESS_PROTOCOL_V3,
        "pack_abi_version": "2",
        "canonical_json_scheme": "scworkbench.operation_pack.canonical_json.v1",
        "status": "error",
        "transition_id": request["transition"]["transition_id"],
        "session_id": request["transition"]["session_id"],
        "run_id": request["transition"]["run_id"],
        "transition_identity": request["transition"]["transition_identity"],
        "request_identity": request["request_identity"],
        "capability_identity": request["execution_capability"]["capability_identity"],
        "phase": phase,
        "error_code": code,
        "message": "Python operation failed; inspect the core-owned diagnostic record",
        "retryable": False,
        "runtime_attestation_status": "available" if runtime is not None else "not_established",
    }
    if runtime is not None:
        receipt["runtime_attestation"] = dict(runtime)
    receipt["receipt_identity"] = _identity(
        RECEIPT_SCHEMA, receipt, "receipt excluding receipt_identity"
    )
    return receipt
