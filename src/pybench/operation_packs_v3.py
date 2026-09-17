from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import inspect
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
    _semantic_admission,
    _validate_parameter_profile,
    distribution_identity_v2,
    parse_manifest_v2_bytes,
)

ENTRY_POINT_GROUP_V3 = "scworkbench.operation_packs.v3"
MANIFEST_SCHEMA_V3 = "scworkbench.operation_pack.v3"
PROCESS_PROTOCOL_V4 = "pybench-op-v4"


def _fail(code: str, message: str, phase: str, **details: Any) -> None:
    raise OperationPackError(code, message, phase=phase, **details)


def _schema_errors(value: Any, schema: Mapping[str, Any]) -> list[Any]:
    return sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: (list(error.absolute_path), error.message),
    )


def _properties(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = operation["params_schema"].get("properties", {})
    return value if isinstance(value, Mapping) else {}


def _is_string_array(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    return (
        "array" in types
        and isinstance(schema.get("items"), Mapping)
        and schema["items"].get("type") == "string"
    )


def _semantic_admission_v3(manifest: Mapping[str, Any]) -> None:
    """Apply frozen v2 semantics plus the three closed v3 declarations.

    The shared schema owns each successor tuple.  This admission layer owns
    cross-references that JSON Schema cannot express and deliberately reduces
    the inherited portion to the frozen v2 semantic validator.
    """

    inherited = copy.deepcopy(dict(manifest))
    inherited["schema_version"] = "scworkbench.operation_pack.v2"
    inherited["pack_abi_version"] = "2"
    inherited["compatibility"]["process_protocol"] = {
        "minimum": "pybench-op-v3",
        "maximum": "pybench-op-v3",
    }
    for operation in inherited["operations"]:
        operation.pop("provenance_reads", None)
        operation.pop("typed_slice_reads", None)
        policies = operation.get("dimension_policies", [])
        operation["dimension_policies"] = [
            policy for policy in policies if policy.get("policy") != "authenticated_output_axes_v1"
        ]
    _semantic_admission(inherited)

    for operation in manifest["operations"]:
        opname = operation["qualified_name"]
        inputs = {item["name"]: item for item in operation["inputs"]}
        outputs = {item["name"]: item for item in operation["outputs"]}
        params = _properties(operation)
        policies = {
            item["output"]: item
            for item in operation.get("dimension_policies", [])
            if item.get("policy") == "authenticated_output_axes_v1"
        }
        for policy in policies.values():
            output = outputs.get(policy["output"])
            cell_input = inputs.get(policy["cell_axis"]["input"])
            expected_kind = (
                "cell_vector_family" if policy["shape"] == "cell_key_cartesian" else "cell_vector"
            )
            expected_order = (
                "key_major_then_cell_v1"
                if policy["shape"] == "cell_key_cartesian"
                else "input_cell_order_v1"
            )
            if (
                output is None
                or output["category"] != "terminal_artifact"
                or output["kind"] != expected_kind
                or output["storage_profile"] != "terminal_primitive_v1"
                or cell_input is None
                or cell_input.get("alignment") not in {"cell_ordered", "cell_and_feature_ordered"}
                or policy["row_order"] != expected_order
            ):
                _fail(
                    "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                    f"Operation {opname!r} has an incompatible authenticated output-axis policy",
                    "semantic_admission",
                )
            key_axis = policy.get("key_axis")
            if policy["shape"] == "one_per_cell":
                if key_axis is not None:
                    _fail(
                        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                        f"Operation {opname!r} gives a one-per-cell output a key axis",
                        "semantic_admission",
                    )
                continue
            if not isinstance(key_axis, Mapping):
                _fail(
                    "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                    f"Operation {opname!r} omits the family key-axis resolver",
                    "semantic_admission",
                )
            if key_axis["source"] == "resolved_parameter_feature_groups_v1":
                group_schema = params.get(key_axis["groups_parameter"])
                matrix = inputs.get(key_axis["matrix_input"])
                prefix_schema = params.get(key_axis["name_prefix_parameter"])
                missing = key_axis["missing_feature_policy"]
                if (
                    not isinstance(group_schema, Mapping)
                    or matrix is None
                    or matrix["kind"] != "matrix"
                    or not isinstance(prefix_schema, Mapping)
                    or prefix_schema.get("type") != "string"
                    or (missing["source"] == "parameter_v1" and missing["parameter"] not in params)
                ):
                    _fail(
                        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                        f"Operation {opname!r} has unresolved parameter feature groups",
                        "semantic_admission",
                    )
            else:
                source = inputs.get(key_axis["input"])
                selector = key_axis.get("selector_parameter")
                if (
                    source is None
                    or source["kind"] != "cell_vector_family"
                    or (selector is not None and not _is_string_array(params.get(selector)))
                ):
                    _fail(
                        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                        f"Operation {opname!r} has an unresolved input-family key axis",
                        "semantic_admission",
                    )

        slices = operation.get("typed_slice_reads", [])
        slice_names = [item["name"] for item in slices]
        other_reader_names = (
            set(inputs)
            | {item["name"] for item in operation.get("registry_reads", [])}
            | {item["name"] for item in operation.get("provenance_reads", [])}
        )
        if len(slice_names) != len(set(slice_names)) or set(slice_names) & other_reader_names:
            _fail(
                "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                f"Operation {opname!r} has duplicate or colliding typed-slice readers",
                "semantic_admission",
            )
        for declaration in slices:
            source = inputs.get(declaration["input"])
            size_schema = params.get(declaration["chunk_size_parameter"])
            valid = (
                source is not None
                and source["kind"] == "matrix"
                and source["alignment"] == "cell_and_feature_ordered"
                and declaration["chunk_axis"] in declaration["supported_axes"]
                and isinstance(size_schema, Mapping)
                and size_schema.get("type") == "integer"
                and size_schema.get("minimum", 0) >= 1
            )
            for selection in declaration["selection"].values():
                if selection["policy"] == "parameter_ordered_subset_v1":
                    valid &= _is_string_array(params.get(selection["parameter"]))
            if not valid:
                _fail(
                    "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                    f"Operation {opname!r} has an invalid typed-slice declaration",
                    "semantic_admission",
                )

        reads = operation.get("provenance_reads", [])
        read_names = [item["name"] for item in reads]
        if len(read_names) != len(set(read_names)) or set(read_names) & set(inputs):
            _fail(
                "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                f"Operation {opname!r} has duplicate or colliding provenance reads",
                "semantic_admission",
            )
        for read in reads:
            assertions = read.get("parameter_assertions", {})
            if read["input"] not in inputs or any(
                value not in params for value in assertions.values()
            ):
                _fail(
                    "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
                    f"Operation {opname!r} has an unresolved provenance-read binding",
                    "semantic_admission",
                )


def validate_manifest_v3(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
    *,
    expected_backend: str | None = "Python",
) -> dict[str, Any]:
    if isinstance(manifest_or_path, (str, os.PathLike)):
        try:
            raw = Path(manifest_or_path).read_bytes()
        except OSError as exc:
            _fail("OPERATION_PACK_MANIFEST_NOT_FOUND", f"Manifest cannot be read: {exc}", "parse")
        manifest = parse_manifest_v2_bytes(raw)
    else:
        manifest = copy.deepcopy(dict(manifest_or_path))
    schema = __import__("json").loads(protocol_asset("operation-pack-v3.schema.json").read_text())
    errors = _schema_errors(manifest, schema)
    if errors:
        first = errors[0]
        _fail(
            "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
            f"Manifest failed Draft 2020-12 validation: {first.message}",
            "outer_schema",
            instance_path=list(first.absolute_path),
        )
    _semantic_admission_v3(manifest)
    for operation in manifest["operations"]:
        _validate_parameter_profile(operation["params_schema"], operation["qualified_name"])
    if manifest["identity_scheme"] != IDENTITY_SCHEME:
        _fail(
            "OPERATION_PACK_IDENTITY_SCHEME_MISMATCH",
            "Operation-pack v3 requires canonical JSON v1",
            "identity",
        )
    if manifest["namespace"] in _RESERVED_NAMESPACES:
        _fail(
            "OPERATION_PACK_NAMESPACE_RESERVED",
            "Operation-pack namespace is reserved",
            "semantic_admission",
        )
    if expected_backend is not None and manifest["backend"] != expected_backend:
        _fail(
            "OPERATION_PACK_BACKEND_MISMATCH",
            "Manifest cannot use the Python v3 path",
            "authentication",
        )
    process = manifest["compatibility"].get("process_protocol")
    if manifest["backend"] == "Python" and process != {
        "minimum": PROCESS_PROTOCOL_V4,
        "maximum": PROCESS_PROTOCOL_V4,
    }:
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "Python ABI v3 packs require exactly pybench-op-v4",
            "semantic_admission",
        )
    return manifest


def manifest_identities_v3(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    manifest = validate_manifest_v3(manifest_or_path, expected_backend=None)
    definitions = {
        operation["qualified_name"]: _identity(
            "scworkbench.operation_pack.definition.v3", operation
        )
        for operation in manifest["operations"]
    }
    manifest_identity = _identity("scworkbench.operation_pack.manifest.v3", manifest)
    return {
        "manifest_identity": manifest_identity,
        "manifest_sha256": manifest_identity["sha256"],
        "operation_definition_identities": definitions,
        "operation_definition_sha256": {
            name: value["sha256"] for name, value in definitions.items()
        },
    }


@dataclass(frozen=True)
class OperationPackHandlersV3:
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]]


def operation_pack_handlers_v3(
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]],
) -> OperationPackHandlersV3:
    if not isinstance(handlers, Mapping):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler bindings must be a mapping",
            "authentication",
        )
    return OperationPackHandlersV3(dict(handlers))


@dataclass(frozen=True)
class PackDiscoveryV3:
    manifest: dict[str, Any]
    identity: dict[str, Any]
    distribution: metadata.Distribution
    entry_point: metadata.EntryPoint
    distribution_identity: dict[str, str]
    manifest_source_identity: dict[str, str]
    entry_point_identity: dict[str, str]

    def public_record(self) -> dict[str, Any]:
        return {
            "schema": "pybench.operation_pack.discovery.v3",
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


def _installed_manifest_v3(
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
            observed_count=len(candidates),
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
            "Supplied manifest is not the installed distribution manifest",
            "authentication",
        )
    raw = installed.read_bytes()
    return validate_manifest_v3(parse_manifest_v2_bytes(raw)), raw, relative


def _entry_point_v3(value: str) -> tuple[str, str, str]:
    group, name, target = _parse_entry_point(
        value.replace(f"{ENTRY_POINT_GROUP_V3}:", "scworkbench.operation_packs.v2:", 1)
    )
    return ENTRY_POINT_GROUP_V3, name, target


def _pack_identity_v3(
    manifest: Mapping[str, Any],
    identities: Mapping[str, Any],
    distribution_identity: Mapping[str, str],
) -> dict[str, str]:
    value = {
        "scheme": "scworkbench.operation_pack.pack.v3",
        "manifest_sha256": identities["manifest_sha256"],
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": manifest["backend"],
        "entry_point": manifest["entry_point"],
        "operation_definition_sha256": identities["operation_definition_sha256"],
        "pack_content_sha256": distribution_identity["sha256"],
    }
    return _identity("scworkbench.operation_pack.pack.v3", value)


def discover_operation_pack_v3(manifest_path: str | os.PathLike[str]) -> PackDiscoveryV3:
    probe = validate_manifest_v3(parse_manifest_v2_bytes(Path(manifest_path).read_bytes()))
    declared = probe["package"]
    try:
        distribution = metadata.distribution(declared["name"])
    except metadata.PackageNotFoundError:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_UNAVAILABLE",
            f"Distribution {declared['name']!r} is absent",
            "authentication",
        )
    if canonicalize_name(distribution.metadata.get("Name", "")) != canonicalize_name(
        declared["name"]
    ):
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "Distribution metadata name differs",
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
    manifest, raw, relative = _installed_manifest_v3(distribution, manifest_path)
    if canonical_json(manifest) != canonical_json(probe):
        _fail(
            "OPERATION_PACK_MANIFEST_IDENTITY_MISMATCH",
            "Supplied manifest differs from installed manifest",
            "authentication",
        )
    group, name, target = _entry_point_v3(manifest["entry_point"])
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
            "Distribution does not expose one exact v3 entry point",
            "authentication",
        )
    identities = manifest_identities_v3(manifest)
    distribution_identity = distribution_identity_v2(distribution)
    source_identity = {
        "scheme": "scworkbench.operation_pack.manifest_source.bytes.v1",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    entry_identity = _identity(
        "scworkbench.operation_pack.entry_point.v2",
        {"backend": "Python", "package": manifest["package"], "value": manifest["entry_point"]},
    )
    identity = {
        "identity_scheme": IDENTITY_SCHEME,
        "schema_version": MANIFEST_SCHEMA_V3,
        "pack_abi_version": "3",
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": "Python",
        "compatibility": manifest["compatibility"],
        "entry_point": manifest["entry_point"],
        **identities,
        "manifest_source_identity": source_identity,
        "distribution_identity": distribution_identity,
        "entry_point_identity": entry_identity,
    }
    identity["pack_identity"] = _pack_identity_v3(manifest, identity, distribution_identity)
    identity["pack_sha256"] = identity["pack_identity"]["sha256"]
    identity["manifest_relative_path"] = relative
    return PackDiscoveryV3(
        manifest,
        identity,
        distribution,
        candidates[0],
        distribution_identity,
        source_identity,
        entry_identity,
    )


_ACTIVE_PACKS_V3: dict[str, tuple[PackDiscoveryV3, OperationPackHandlersV3]] = {}


def active_operation_packs_v3() -> Mapping[str, tuple[PackDiscoveryV3, OperationPackHandlersV3]]:
    return dict(_ACTIVE_PACKS_V3)


def activate_operation_pack_v3(
    discovery: PackDiscoveryV3, *, fault_hook: Callable[[str], None] | None = None
) -> OperationPackHandlersV3:
    rediscovered = discover_operation_pack_v3(
        Path(discovery.distribution.locate_file(discovery.identity["manifest_relative_path"]))
    )
    if canonical_json(rediscovered.identity) != canonical_json(discovery.identity):
        _fail(
            "OPERATION_PACK_IDENTITY_MISMATCH",
            "Installed pack identity changed before activation",
            "authentication",
        )
    target = rediscovered.entry_point.load()
    try:
        registration = target()
    except OperationPackError:
        raise
    except Exception as exc:
        _fail("OPERATION_PACK_ENTRY_POINT_FAILED", f"Entry point failed: {exc}", "authentication")
    if not isinstance(registration, OperationPackHandlersV3):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Entry point returned an invalid v3 registration",
            "authentication",
        )
    expected = {item["qualified_name"] for item in rediscovered.manifest["operations"]}
    if set(registration.handlers) != expected:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler closure differs from the manifest",
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
                f"Handler {name!r} has the wrong interface",
                "authentication",
            )
    candidate = dict(_ACTIVE_PACKS_V3)
    namespace = rediscovered.manifest["namespace"]
    if namespace in candidate or expected & {
        name for _, handlers in candidate.values() for name in handlers.handlers
    }:
        _fail(
            "OPERATION_PACK_NAMESPACE_COLLISION",
            "V3 pack namespace or operation collides",
            "catalog_publication",
        )
    candidate[namespace] = (rediscovered, registration)
    before = dict(_ACTIVE_PACKS_V3)
    try:
        for event in ("catalog_before_pack", "catalog_pack_installed", "catalog_before_commit"):
            if fault_hook:
                fault_hook(event)
        _ACTIVE_PACKS_V3.clear()
        _ACTIVE_PACKS_V3.update(candidate)
    except Exception as exc:
        _ACTIVE_PACKS_V3.clear()
        _ACTIVE_PACKS_V3.update(before)
        if isinstance(exc, OperationPackError):
            raise
        _fail(
            "OPERATION_PACK_CATALOG_PUBLICATION_FAILED",
            f"Atomic v3 activation failed: {exc}",
            "catalog_publication",
        )
    return registration


def unload_operation_pack_v3(namespace: str) -> None:
    _ACTIVE_PACKS_V3.pop(namespace, None)
