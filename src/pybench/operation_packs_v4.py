"""Explicit installed-distribution discovery for operation-pack ABI4."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .operation_packs import IDENTITY_SCHEME, OperationPackError, canonical_json, protocol_asset
from .operation_packs_v2 import (
    _RESERVED_NAMESPACES,
    _distribution_build_metadata,
    _distribution_files,
    _identity,
    _parse_entry_point,
    _validate_parameter_profile,
    distribution_identity_v2,
    parse_manifest_v2_bytes,
)
from .operation_packs_v3 import _semantic_admission_v3, active_operation_packs_v3

ENTRY_POINT_GROUP_V4 = "scworkbench.operation_packs.v4"
MANIFEST_SCHEMA_V4 = "scworkbench.operation_pack.v4"
PROCESS_PROTOCOL_V5 = "pybench-op-v5"
_ACTIVE_PACKS_V4: dict[str, tuple[PackDiscoveryV4, OperationPackHandlersV4]] = {}


def _fail(code: str, message: str, phase: str) -> None:
    raise OperationPackError(code, message, phase=phase)


def _unsupported_python_capability(manifest: Mapping[str, Any]) -> bool:
    for operation in manifest["operations"]:
        if (
            any(
                policy.get("policy")
                in {
                    "standalone_sparse_cell_family_v1",
                    "sealed_relation_family_v1",
                    "runtime_ordered_cell_family_v1",
                }
                for policy in operation.get("dimension_policies", [])
            )
            or any(
                read.get("kind") in {"genomic_matrix_source", "motif_library", "motif_match_set"}
                for read in operation.get("registry_reads", [])
            )
            or any(
                dependency.get("kind") == "dynamic_matrix_registry_package"
                for dependency in operation["dependencies"]
            )
            or operation.get("producer_lineage_reads")
            or any(
                read.get("selection", {}).get("policy") == "full_input_axis_v1"
                for read in operation.get("typed_space_reads", [])
            )
            or any(
                grant.get("source", {}).get("mode") == "static_typed_space_union_v1"
                or any(
                    link.get("shape") == "one_per_cell_space_v1"
                    for link in grant.get("linked_outputs", [])
                )
                for grant in operation.get("derived_cell_output_axes", [])
            )
        ):
            return True
    return False


def _validate_graph_axis_sources(operation: Mapping[str, Any]) -> None:
    inputs = {item["name"]: item for item in operation["inputs"]}
    parameters = operation["params_schema"].get("properties", {})
    required_parameters = set(operation["params_schema"].get("required", []))
    for output in operation["outputs"]:
        if output["kind"] != "graph":
            continue
        sources = output["axis_sources"]
        if output["binding_policy"] == "forbidden" and sources:
            _fail(
                "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                "Unbound graph declares axis sources",
                "semantic_admission",
            )
        if output["binding_policy"] == "required" and not sources:
            _fail(
                "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                "Required graph binding has no source declaration",
                "semantic_admission",
            )
        seen: set[str] = set()
        for source in sources:
            name = source["input_name"]
            declaration = inputs.get(name)
            if declaration is None:
                _fail(
                    "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                    "Graph axis source names undeclared input",
                    "semantic_admission",
                )
            if source["selection_mode"] == "retained_input_axis":
                if declaration["kind"] not in {"cell_vector", "cell_vector_family"}:
                    _fail(
                        "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                        "Retained graph axis requires a cell vector input",
                        "semantic_admission",
                    )
            elif source["selection_mode"] == "retained_matrix_axis":
                if declaration["kind"] != "matrix":
                    _fail(
                        "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                        "Retained matrix axis requires a matrix input",
                        "semantic_admission",
                    )
            else:
                if declaration["kind"] != "graph":
                    _fail(
                        "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                        "Selected graph binding requires a graph input",
                        "semantic_admission",
                    )
                selector = source["source_binding_selector"]
                if selector["kind"] == "fixed_binding_id_v1":
                    binding_id = selector["binding_id"]
                    try:
                        encoded = binding_id.encode("utf-8")
                    except UnicodeEncodeError:
                        encoded = b""
                    if "\x00" in binding_id or not encoded or len(encoded) > 256:
                        _fail(
                            "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                            "Fixed graph binding ID is invalid",
                            "semantic_admission",
                        )
                else:
                    parameter = selector["parameter"]
                    profile = parameters.get(parameter, {})
                    if (
                        parameter not in required_parameters
                        or profile.get("type") != "string"
                        or profile.get("minLength", 0) < 1
                    ):
                        _fail(
                            "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                            "Graph binding selector parameter must be a required nonempty string",
                            "semantic_admission",
                        )
            key = canonical_json(source)
            if key in seen:
                _fail(
                    "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
                    "Duplicate graph axis source declaration",
                    "semantic_admission",
                )
            seen.add(key)


def validate_manifest_v4(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
    *,
    expected_backend: str | None = "Python",
) -> dict[str, Any]:
    if isinstance(manifest_or_path, (str, os.PathLike)):
        try:
            manifest = parse_manifest_v2_bytes(Path(manifest_or_path).read_bytes())
        except OSError as exc:
            _fail("OPERATION_PACK_MANIFEST_NOT_FOUND", f"Manifest cannot be read: {exc}", "parse")
    else:
        manifest = copy.deepcopy(dict(manifest_or_path))
    schema = json.loads(protocol_asset("operation-pack-v4.schema.json").read_text())
    errors = sorted(
        Draft202012Validator(schema).iter_errors(manifest),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        _fail(
            "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
            f"Manifest failed Draft 2020-12 validation: {errors[0].message}",
            "outer_schema",
        )
    _semantic_admission_v3(manifest, allow_graph_replay=True)
    for operation in manifest["operations"]:
        _validate_parameter_profile(operation["params_schema"], operation["qualified_name"])
        _validate_graph_axis_sources(operation)
    if manifest["identity_scheme"] != IDENTITY_SCHEME:
        _fail(
            "OPERATION_PACK_IDENTITY_SCHEME_MISMATCH", "ABI4 requires canonical JSON v1", "identity"
        )
    if manifest["namespace"] in _RESERVED_NAMESPACES:
        _fail(
            "OPERATION_PACK_NAMESPACE_RESERVED", "Pack namespace is reserved", "semantic_admission"
        )
    if expected_backend is not None and manifest["backend"] != expected_backend:
        _fail(
            "OPERATION_PACK_BACKEND_MISMATCH", "Manifest cannot use Python ABI4", "authentication"
        )
    if manifest["backend"] == "Python":
        if manifest["compatibility"]["process_protocol"] != {
            "minimum": PROCESS_PROTOCOL_V5,
            "maximum": PROCESS_PROTOCOL_V5,
        }:
            _fail(
                "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
                "Python ABI4 requires pybench-op-v5",
                "semantic_admission",
            )
        if _unsupported_python_capability(manifest):
            _fail(
                "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
                "Python ABI4 does not implement one or more declared legacy grants",
                "semantic_admission",
            )
    return manifest


def manifest_identities_v4(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    manifest = validate_manifest_v4(manifest_or_path, expected_backend=None)
    definitions = {
        operation["qualified_name"]: _identity(
            "scworkbench.operation_pack.definition.v4", operation
        )
        for operation in manifest["operations"]
    }
    manifest_id = _identity("scworkbench.operation_pack.manifest.v4", manifest)
    return {
        "manifest_identity": manifest_id,
        "manifest_sha256": manifest_id["sha256"],
        "operation_definition_identities": definitions,
        "operation_definition_sha256": {
            name: value["sha256"] for name, value in definitions.items()
        },
    }


@dataclass(frozen=True)
class OperationPackHandlersV4:
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]]


def operation_pack_handlers_v4(
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]],
) -> OperationPackHandlersV4:
    if not isinstance(handlers, Mapping):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler bindings must be a mapping",
            "authentication",
        )
    return OperationPackHandlersV4(dict(handlers))


@dataclass(frozen=True)
class PackDiscoveryV4:
    manifest: dict[str, Any]
    identity: dict[str, Any]
    distribution: metadata.Distribution
    entry_point: metadata.EntryPoint
    distribution_identity: dict[str, str]
    manifest_source_identity: dict[str, str]
    entry_point_identity: dict[str, str]

    def public_record(self) -> dict[str, Any]:
        return {
            "schema": "pybench.operation_pack.discovery.v4",
            "backend": "Python",
            "identity": self.identity,
            "distribution": {
                "declared_name": self.manifest["package"]["name"],
                "normalized_name": canonicalize_name(self.manifest["package"]["name"]),
                "version": self.manifest["package"]["version"],
                "artifact_identity": self.distribution_identity,
                "build_metadata": _distribution_build_metadata(self.distribution),
            },
            "manifest_source_identity": self.manifest_source_identity,
            "entry_point": {
                "group": self.entry_point.group,
                "name": self.entry_point.name,
                "value": self.entry_point.value,
                "identity": self.entry_point_identity,
            },
        }


def _installed_manifest_v4(
    distribution: metadata.Distribution, supplied_path: str | os.PathLike[str]
) -> tuple[dict[str, Any], bytes, str]:
    candidates = [
        (relative, path)
        for relative, path in _distribution_files(distribution)
        if relative.endswith("/operation-pack.json")
    ]
    if len(candidates) != 1:
        _fail(
            "OPERATION_PACK_MANIFEST_SOURCE_REQUIRED",
            "Distribution must contain exactly one operation-pack.json",
            "authentication",
        )
    relative, installed_path = candidates[0]
    try:
        supplied = Path(supplied_path).resolve(strict=True)
        installed = installed_path.resolve(strict=True)
    except OSError:
        _fail(
            "OPERATION_PACK_MANIFEST_SOURCE_REQUIRED",
            "Installed manifest is unavailable",
            "authentication",
        )
    if supplied != installed or not supplied.samefile(installed):
        _fail(
            "OPERATION_PACK_MANIFEST_SOURCE_REQUIRED",
            "Supplied manifest is not installed",
            "authentication",
        )
    raw = installed.read_bytes()
    return validate_manifest_v4(parse_manifest_v2_bytes(raw)), raw, relative


def _entry_point_v4(value: str) -> tuple[str, str, str]:
    _group, name, target = _parse_entry_point(
        value.replace(f"{ENTRY_POINT_GROUP_V4}:", "scworkbench.operation_packs.v2:", 1)
    )
    return ENTRY_POINT_GROUP_V4, name, target


def discover_operation_pack_v4(manifest_path: str | os.PathLike[str]) -> PackDiscoveryV4:
    try:
        probe = validate_manifest_v4(parse_manifest_v2_bytes(Path(manifest_path).read_bytes()))
    except OSError as exc:
        _fail("OPERATION_PACK_MANIFEST_NOT_FOUND", f"Manifest cannot be read: {exc}", "parse")
    declared = probe["package"]
    try:
        distribution = metadata.distribution(declared["name"])
    except metadata.PackageNotFoundError:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_UNAVAILABLE",
            "Declared distribution is absent",
            "authentication",
        )
    if canonicalize_name(distribution.metadata.get("Name", "")) != canonicalize_name(
        declared["name"]
    ):
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "Distribution name differs",
            "authentication",
        )
    try:
        versions_match = Version(distribution.version) == Version(declared["version"])
    except InvalidVersion:
        versions_match = False
    if not versions_match:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "Distribution version differs",
            "authentication",
        )
    manifest, raw, relative = _installed_manifest_v4(distribution, manifest_path)
    if canonical_json(manifest) != canonical_json(probe):
        _fail(
            "OPERATION_PACK_MANIFEST_IDENTITY_MISMATCH",
            "Installed manifest differs",
            "authentication",
        )
    group, name, target = _entry_point_v4(manifest["entry_point"])
    candidates = [
        entry
        for entry in distribution.entry_points
        if entry.group == group and entry.name == name and entry.value == target
    ]
    if len(candidates) != 1:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_UNAVAILABLE"
            if not candidates
            else "OPERATION_PACK_ENTRY_POINT_AMBIGUOUS",
            "Distribution does not expose one exact v4 entry point",
            "authentication",
        )
    identities = manifest_identities_v4(manifest)
    distribution_id = distribution_identity_v2(distribution)
    source_id = {
        "scheme": "scworkbench.operation_pack.manifest_source.bytes.v1",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    entry_id = _identity(
        "scworkbench.operation_pack.entry_point.v2",
        {"backend": "Python", "package": manifest["package"], "value": manifest["entry_point"]},
    )
    pack_preimage = {
        "scheme": "scworkbench.operation_pack.pack.v4",
        "manifest_sha256": identities["manifest_sha256"],
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": manifest["backend"],
        "entry_point": manifest["entry_point"],
        "operation_definition_sha256": identities["operation_definition_sha256"],
        "pack_content_sha256": distribution_id["sha256"],
    }
    pack_id = _identity("scworkbench.operation_pack.pack.v4", pack_preimage)
    identity_record = {
        "identity_scheme": IDENTITY_SCHEME,
        "schema_version": MANIFEST_SCHEMA_V4,
        "pack_abi_version": "4",
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": "Python",
        "compatibility": manifest["compatibility"],
        "entry_point": manifest["entry_point"],
        **identities,
        "manifest_source_identity": source_id,
        "distribution_identity": distribution_id,
        "entry_point_identity": entry_id,
        "pack_identity": pack_id,
        "pack_sha256": pack_id["sha256"],
        "manifest_relative_path": relative,
    }
    return PackDiscoveryV4(
        manifest,
        identity_record,
        distribution,
        candidates[0],
        distribution_id,
        source_id,
        entry_id,
    )


def active_operation_packs_v4() -> Mapping[str, tuple[PackDiscoveryV4, OperationPackHandlersV4]]:
    return dict(_ACTIVE_PACKS_V4)


def activate_operation_pack_v4(
    discovery: PackDiscoveryV4, *, fault_hook: Callable[[str], None] | None = None
) -> OperationPackHandlersV4:
    installed = Path(
        discovery.distribution.locate_file(discovery.identity["manifest_relative_path"])
    )
    rediscovered = discover_operation_pack_v4(installed)
    if canonical_json(rediscovered.identity) != canonical_json(discovery.identity):
        _fail(
            "OPERATION_PACK_IDENTITY_MISMATCH",
            "Installed pack changed before activation",
            "authentication",
        )
    target = rediscovered.entry_point.load()
    if not callable(target):
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "Entry point is not callable", "authentication")
    try:
        registration = target()
    except OperationPackError:
        raise
    except Exception as exc:
        _fail("OPERATION_PACK_ENTRY_POINT_FAILED", f"Entry point failed: {exc}", "authentication")
    if not isinstance(registration, OperationPackHandlersV4):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Entry point returned invalid v4 registration",
            "authentication",
        )
    expected = {item["qualified_name"] for item in rediscovered.manifest["operations"]}
    if set(registration.handlers) != expected:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler closure differs from manifest",
            "authentication",
        )
    for name, handler in registration.handlers.items():
        if not callable(handler) or list(inspect.signature(handler).parameters) != [
            "readers",
            "writers",
            "params",
            "context",
        ]:
            _fail(
                "OPERATION_PACK_ENTRY_POINT_INVALID",
                f"Handler {name!r} has wrong signature",
                "authentication",
            )
    namespace = rediscovered.manifest["namespace"]
    existing = {**active_operation_packs_v3(), **_ACTIVE_PACKS_V4}
    if namespace in existing or expected & {
        name for _, handlers in existing.values() for name in handlers.handlers
    }:
        _fail(
            "OPERATION_PACK_NAMESPACE_COLLISION",
            "Pack namespace or operation collides",
            "catalog_publication",
        )
    candidate = dict(_ACTIVE_PACKS_V4)
    candidate[namespace] = (rediscovered, registration)
    before = dict(_ACTIVE_PACKS_V4)
    try:
        for event in ("catalog_before_pack", "catalog_pack_installed", "catalog_before_commit"):
            if fault_hook:
                fault_hook(event)
        _ACTIVE_PACKS_V4.clear()
        _ACTIVE_PACKS_V4.update(candidate)
    except Exception:
        _ACTIVE_PACKS_V4.clear()
        _ACTIVE_PACKS_V4.update(before)
        raise
    return registration


def unload_operation_pack_v4(namespace: str) -> None:
    _ACTIVE_PACKS_V4.pop(namespace, None)
