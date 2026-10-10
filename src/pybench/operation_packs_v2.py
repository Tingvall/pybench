from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import inspect
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .operation_packs import (
    IDENTITY_SCHEME,
    OperationPackError,
    _distribution_build_metadata,
    canonical_json,
    canonical_sha256,
    protocol_asset,
)

ENTRY_POINT_GROUP_V2 = "scworkbench.operation_packs.v2"
MANIFEST_SCHEMA_V2 = "scworkbench.operation_pack.v2"
PROCESS_PROTOCOL_V3 = "pybench-op-v3"
_ENTRY_VALUE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_R_PACKAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9.]*$")
_PARAM_KEYWORDS = {
    "$schema",
    "$defs",
    "$ref",
    "$comment",
    "title",
    "description",
    "type",
    "enum",
    "const",
    "default",
    "examples",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "properties",
    "required",
    "additionalProperties",
    "minProperties",
    "maxProperties",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
}
_PARAM_TYPES = {"null", "boolean", "object", "array", "number", "integer", "string"}
_RESERVED_NAMESPACES = {"scworkbench", "pybench", "core", "wb", "scwb"}


def _fail(code: str, message: str, phase: str, **details: Any) -> None:
    raise OperationPackError(code, message, phase=phase, **details)


def _identity(scheme: str, value: Any, scope: str | None = None) -> dict[str, str]:
    result = {"scheme": scheme}
    if scope is not None:
        result["scope"] = scope
    result["sha256"] = canonical_sha256(value)
    return result


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(
                "OPERATION_PACK_MANIFEST_PARSE_INVALID",
                f"Operation-pack v2 manifest repeats raw object key {key!r}",
                "parse",
            )
        result[key] = value
    return result


def parse_manifest_v2_bytes(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("OPERATION_PACK_MANIFEST_PARSE_INVALID", f"Manifest is not UTF-8: {exc}", "parse")
    try:
        value = json.loads(text, object_pairs_hook=_strict_pairs)
    except OperationPackError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        _fail(
            "OPERATION_PACK_MANIFEST_PARSE_INVALID", f"Manifest is not strict JSON: {exc}", "parse"
        )
    if not isinstance(value, dict):
        _fail("OPERATION_PACK_MANIFEST_PARSE_INVALID", "Manifest root must be an object", "parse")
    return value


def load_manifest_v2(path: str | os.PathLike[str]) -> tuple[dict[str, Any], bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        _fail("OPERATION_PACK_MANIFEST_NOT_FOUND", f"Manifest cannot be read: {exc}", "parse")
    return parse_manifest_v2_bytes(raw), raw


def _schema_errors(value: Any, schema: Mapping[str, Any]) -> list[Any]:
    return sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: (list(error.absolute_path), error.message),
    )


def _validate_parameter_profile(schema: Any, operation: str) -> None:
    dialect = "https://json-schema.org/draft/2020-12/schema"

    def walk(node: Any, path: str, root: bool = False) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            _fail(
                "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                f"Parameter schema at {path} is not an object or boolean schema",
                "parameter_schema",
                operation=operation,
            )
        extras = sorted(set(node) - _PARAM_KEYWORDS)
        if extras:
            _fail(
                "OPERATION_PACK_PARAM_SCHEMA_PROFILE_UNSUPPORTED",
                f"Unsupported parameter-schema keyword at {path}: {extras[0]}",
                "parameter_schema",
                operation=operation,
            )
        if "$schema" in node and (not root or node["$schema"] != dialect):
            _fail(
                "OPERATION_PACK_PARAM_SCHEMA_DIALECT_UNSUPPORTED",
                f"Unsupported parameter-schema dialect at {path}",
                "parameter_schema",
                operation=operation,
            )
        if "$ref" in node and not re.fullmatch(
            r"#/\$defs/[A-Za-z][A-Za-z0-9._-]*(?:/[A-Za-z0-9._~-]+)*", node["$ref"]
        ):
            _fail(
                "OPERATION_PACK_PARAM_SCHEMA_PROFILE_UNSUPPORTED",
                f"Only local #/$defs references are allowed at {path}",
                "parameter_schema",
                operation=operation,
            )
        if "type" in node:
            types = node["type"] if isinstance(node["type"], list) else [node["type"]]
            if (
                not types
                or len(types) != len(set(types))
                or any(x not in _PARAM_TYPES for x in types)
            ):
                _fail(
                    "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                    f"Invalid type declaration at {path}",
                    "parameter_schema",
                    operation=operation,
                )
        required = node.get("required")
        if required is not None and (
            not isinstance(required, list)
            or any(not isinstance(x, str) for x in required)
            or len(required) != len(set(required))
        ):
            _fail(
                "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                f"Invalid required declaration at {path}",
                "parameter_schema",
                operation=operation,
            )
        for key in ("$defs", "properties"):
            if key in node:
                if not isinstance(node[key], dict):
                    _fail(
                        "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                        f"Invalid schema map at {path}/{key}",
                        "parameter_schema",
                        operation=operation,
                    )
                for name, child in node[key].items():
                    walk(child, f"{path}/{key}/{name}")
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if key in node:
                children = node[key]
                if not isinstance(children, list) or (key != "prefixItems" and not children):
                    _fail(
                        "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                        f"Invalid schema array at {path}/{key}",
                        "parameter_schema",
                        operation=operation,
                    )
                for index, child in enumerate(children):
                    walk(child, f"{path}/{key}/{index}")
        for key in ("not", "if", "then", "else", "items", "additionalProperties"):
            if key in node:
                walk(node[key], f"{path}/{key}")
        if "default" in node:
            without_default = {key: value for key, value in node.items() if key != "default"}
            errors = _schema_errors(node["default"], without_default)
            if errors:
                _fail(
                    "OPERATION_PACK_PARAM_SCHEMA_INVALID",
                    f"Default does not validate at {path}: {errors[0].message}",
                    "parameter_schema",
                    operation=operation,
                )

    walk(schema, "$", root=True)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        _fail(
            "OPERATION_PACK_PARAM_SCHEMA_INVALID",
            f"Embedded parameter schema is invalid: {exc}",
            "parameter_schema",
            operation=operation,
        )


def _duplicates(values: list[str]) -> bool:
    return len(values) != len(set(values))


def _declaration_error(operation: str, message: str) -> None:
    _fail(
        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
        f"Operation {operation!r} {message}",
        "semantic_admission",
        operation=operation,
    )


def _catalog_param_type(schema: Mapping[str, Any] | None) -> str:
    if not schema:
        return "list"
    declared = schema.get("type", "list")
    types = [declared] if isinstance(declared, str) else declared
    if "integer" in types:
        return "int"
    if "number" in types:
        return "dbl"
    if "boolean" in types:
        return "bool"
    if "string" in types:
        return "choice" if "enum" in schema else "string"
    if "array" in types and isinstance(schema.get("items"), Mapping):
        if schema["items"].get("type") == "string":
            return "string_vec"
    return "list"


def _semantic_admission(
    manifest: Mapping[str, Any], *, allow_graph_replay: bool = False
) -> None:
    namespace = manifest["namespace"]
    operations = manifest["operations"]
    short = [operation["name"] for operation in operations]
    qualified = [operation["qualified_name"] for operation in operations]
    for index, operation in enumerate(operations):
        expected = f"{namespace}::{operation['name']}"
        if operation["qualified_name"] != expected:
            _fail(
                "OPERATION_PACK_MANIFEST_QUALIFIED_NAME_MISMATCH",
                f"Qualified operation name must be exactly {expected!r}",
                "semantic_admission",
                operation_index=index + 1,
            )
    if _duplicates(short) or _duplicates(qualified):
        _fail(
            "OPERATION_PACK_MANIFEST_OPERATION_DUPLICATE",
            "Operation short and qualified names must be unique",
            "semantic_admission",
        )
    for operation in operations:
        opname = operation["qualified_name"]
        input_names = [item["name"] for item in operation["inputs"]]
        output_names = [item["name"] for item in operation["outputs"]]
        allocation = [item["allocated_id_key"] for item in operation["outputs"]]
        dependencies = [item["requirement_id"] for item in operation["dependencies"]]
        checks = (
            (input_names, "OPERATION_PACK_MANIFEST_INPUT_DUPLICATE", "input name"),
            (output_names, "OPERATION_PACK_MANIFEST_OUTPUT_DUPLICATE", "output name"),
            (allocation, "OPERATION_PACK_MANIFEST_ALLOCATION_KEY_DUPLICATE", "allocated ID key"),
            (
                dependencies,
                "OPERATION_PACK_MANIFEST_DEFINITION_KEY_DUPLICATE",
                "dependency requirement ID",
            ),
        )
        for values, code, label in checks:
            if _duplicates(values):
                _fail(code, f"Operation {opname!r} repeats a {label}", "semantic_admission")
        for dependency in operation["dependencies"]:
            if (
                dependency["kind"] == "dynamic_input_metadata_package"
                and dependency["input_name"] not in input_names
            ):
                _fail(
                    "OPERATION_PACK_MANIFEST_DEFINITION_KEY_DUPLICATE",
                    "Dynamic dependency names an undeclared input",
                    "semantic_admission",
                    operation=opname,
                )
        properties = operation["params_schema"].get("properties", {})
        registry_reads = operation.get("registry_reads", [])
        registry_names = [item["name"] for item in registry_reads]
        if _duplicates(registry_names) or set(registry_names) & set(input_names):
            _declaration_error(opname, "repeats a registry-read name or collides with an input")
        input_map = {item["name"]: item for item in operation["inputs"]}
        for item in registry_reads:
            matrix = input_map.get(item["matrix_input"])
            if matrix is None or matrix["kind"] != "matrix":
                _declaration_error(opname, "has a registry read without a declared matrix input")
            selectors = [
                item.get(key)
                for key in ("namespace_parameter", "namespaces_parameter", "feature_sets_parameter")
                if item.get(key) is not None
            ]
            if _duplicates(selectors):
                _declaration_error(opname, "reuses a parameter for registry selector roles")
            for key in ("namespace_parameter", "namespaces_parameter", "feature_sets_parameter"):
                parameter = item.get(key)
                if parameter is None:
                    continue
                schema = properties.get(parameter)
                types = schema.get("type", []) if isinstance(schema, Mapping) else []
                types = [types] if isinstance(types, str) else types
                if key == "namespace_parameter":
                    valid = "string" in types
                else:
                    valid = (
                        "array" in types
                        and isinstance(schema.get("items"), Mapping)
                        and schema["items"].get("type") == "string"
                    )
                if not valid:
                    _declaration_error(
                        opname, f"registry selector {parameter!r} has an incompatible type"
                    )
        public = operation.get("public_interface")
        if public is not None:
            public_inputs = [item["name"] for item in public["inputs"]]
            public_params = [item["name"] for item in public["parameters"]]
            public_outputs = [item["name"] for item in public["outputs"]]
            if (
                _duplicates(public_inputs)
                or not set(public_inputs) <= set(input_names)
                or _duplicates(public_params)
                or not set(public_params) <= set(properties)
                or _duplicates(public_outputs)
                or set(public_outputs) != set(output_names)
            ):
                _declaration_error(
                    opname,
                    "public interface is not an exact bijection with its inputs, parameters, and outputs",
                )
            for item in public["parameters"]:
                expected = _catalog_param_type(properties[item["name"]])
                declared = item["type"]
                compatible = (
                    declared == expected
                    or (expected == "string_vec" and declared in {"cell_ids_or_set", "string_vec"})
                    or (expected == "string" and declared == "cell_set")
                )
                if not compatible:
                    _declaration_error(
                        opname,
                        f"public parameter {item['name']!r} type conflicts with params_schema",
                    )
        bindings = operation.get("parameter_bindings", [])
        binding_names = [item["parameter"] for item in bindings]
        if _duplicates(binding_names):
            _declaration_error(opname, "repeats a parameter binding")
        inputs_by_name = {item["name"]: item for item in operation["inputs"]}
        for binding in bindings:
            parameter = binding["parameter"]
            source_input = inputs_by_name.get(binding["input"])
            parameter_schema = properties.get(parameter)
            if source_input is None or parameter_schema is None or source_input["kind"] != "matrix":
                _declaration_error(
                    opname,
                    "has a parameter binding with a missing or incompatible input/parameter reference",
                )
            schema_types = parameter_schema.get("type", [])
            schema_types = [schema_types] if isinstance(schema_types, str) else schema_types
            items = parameter_schema.get("items")
            if (
                "array" not in schema_types
                or not isinstance(items, Mapping)
                or items.get("type") != "string"
            ):
                _declaration_error(
                    opname,
                    f"binding policy {binding['policy']!r} requires an array-of-string parameter",
                )
        policies = operation.get("dimension_policies", [])
        policy_outputs = [item["output"] for item in policies]
        if _duplicates(policy_outputs):
            _declaration_error(opname, "repeats a dimension policy output")
        outputs_by_name = {item["name"]: item for item in operation["outputs"]}
        for policy in policies:
            output = outputs_by_name.get(policy["output"])
            if output is None or not (
                output["category"] == "terminal_artifact"
                and output["kind"] in {"feature_table", "obs_table"}
                and output["storage_profile"] == "terminal_primitive_v1"
            ):
                _declaration_error(
                    opname, "declares data-dependent dimensions for an incompatible output"
                )
        chunk = operation.get("chunking")
        if chunk is not None:
            source_input = inputs_by_name.get(chunk["input"])
            output = outputs_by_name.get(chunk["output"])
            sequence_schema = properties.get(chunk["sequence_parameter"])
            size_schema = properties.get(chunk["size_parameter"])
            adjust_schema = properties.get(chunk["adjust_parameter"])
            valid = (
                source_input is not None
                and output is not None
                and source_input["kind"] == "matrix"
                and output["kind"] == "feature_table"
                and chunk["output"] in policy_outputs
                and _catalog_param_type(sequence_schema) == "string_vec"
                and _catalog_param_type(size_schema) == "int"
                and _catalog_param_type(adjust_schema) == "string"
                and isinstance(size_schema.get("minimum") if size_schema else None, (int, float))
                and not isinstance(size_schema.get("minimum") if size_schema else None, bool)
                and size_schema["minimum"] >= 1
                and isinstance(size_schema.get("default") if size_schema else None, (int, float))
                and not isinstance(size_schema.get("default") if size_schema else None, bool)
                and size_schema["default"] >= 1
                and chunk["sequence_parameter"] in binding_names
            )
            if not valid:
                _declaration_error(
                    opname,
                    "has a chunk declaration with a missing, incompatible, or undeclared reference",
                )
        retained = operation.get("retained_parameters", [])
        retained_names = [item["name"] for item in retained]
        if _duplicates(retained_names):
            _declaration_error(opname, "repeats a retained parameter name")
        for item in retained:
            source = item["source"]
            needs_output = source in {"output_meta", "output_first_row", "output_row_count"}
            needs_input = source == "input_feature_count"
            needs_parameter = source in {
                "parameter_supplied",
                "parameter_count",
                "parameter_value",
            }
            needs_field = source in {"output_meta", "output_first_row"}
            valid = (
                (not needs_output or item.get("output") in output_names)
                and (not needs_input or item.get("input") in input_names)
                and (not needs_parameter or item.get("parameter") in properties)
                and (not needs_field or bool(item.get("field")))
            )
            if not valid:
                _declaration_error(
                    opname,
                    f"retained parameter {item['name']!r} has an invalid source reference",
                )
        groups = {item["name"]: item["promotion_group"] for item in operation["outputs"]}
        categories = {item["name"]: item["category"] for item in operation["outputs"]}
        terminals = [name for name in output_names if categories[name] == "terminal_artifact"]
        closure = operation["promotion_closure"]
        valid_closure = set(closure) == set(terminals)
        for terminal in terminals:
            expected = sorted(name for name in output_names if groups[name] == groups[terminal])
            valid_closure &= closure.get(terminal) == expected and not _duplicates(
                closure.get(terminal, [])
            )
        for group in set(groups.values()):
            witnesses = sorted(name for name in terminals if groups[name] == group)
            valid_closure &= bool(witnesses)
            for output in operation["outputs"]:
                if output["promotion_group"] == group and output["category"] == "materialization":
                    valid_closure &= output.get("materialized_by_terminals", []) == witnesses
        if not valid_closure:
            _fail(
                "OPERATION_PACK_MANIFEST_PROMOTION_CLOSURE_INVALID",
                f"Operation {opname!r} promotion closure is not exact",
                "semantic_admission",
            )
        replay = operation["replay_equivalence"]
        valid_replay = set(replay) == set(output_names)
        for output in operation["outputs"]:
            category, kind = output["category"], output["kind"]
            if allow_graph_replay and category == "terminal_artifact" and kind == "graph":
                allowed = {"exact_graph_descriptor"}
            elif category == "materialization" and kind in {"matrix", "space"}:
                allowed = {"exact_canonical_value"}
            elif kind == "space_2d" and category in {"materialization", "terminal_artifact"}:
                allowed = {"exact_canonical_value", "cell_aligned_embedding"}
            elif category == "terminal_artifact" and kind == "model_ref":
                allowed = {"loadable_reference"}
            elif category == "terminal_artifact":
                allowed = {"exact_canonical_value"}
            elif category == "core_registry_request" and kind == "cell_set_admission":
                allowed = {"exact_membership_and_order"}
            elif category == "core_registry_request" and kind == "cell_set_family_admission":
                allowed = {"exact_family_membership_and_order"}
            else:
                allowed = set()
            valid_replay &= replay.get(output["name"]) in allowed
        if not valid_replay:
            _fail(
                "OPERATION_PACK_MANIFEST_REPLAY_EQUIVALENCE_INVALID",
                f"Operation {opname!r} replay map is incomplete or incompatible",
                "semantic_admission",
            )


def validate_manifest_v2(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
    *,
    expected_backend: str | None = "Python",
) -> dict[str, Any]:
    if isinstance(manifest_or_path, (str, os.PathLike)):
        manifest, _ = load_manifest_v2(manifest_or_path)
    else:
        manifest = copy.deepcopy(dict(manifest_or_path))
    schema = json.loads(protocol_asset("operation-pack-v2.schema.json").read_text())
    errors = _schema_errors(manifest, schema)
    if errors:
        first = errors[0]
        _fail(
            "OPERATION_PACK_MANIFEST_SCHEMA_INVALID",
            f"Manifest failed Draft 2020-12 validation: {first.message}",
            "outer_schema",
            instance_path=list(first.absolute_path),
        )
    _semantic_admission(manifest)
    for operation in manifest["operations"]:
        _validate_parameter_profile(operation["params_schema"], operation["qualified_name"])
    if manifest["identity_scheme"] != IDENTITY_SCHEME:
        _fail(
            "OPERATION_PACK_IDENTITY_SCHEME_MISMATCH",
            "Operation-pack v2 requires canonical JSON v1",
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
            f"Backend {manifest['backend']!r} cannot use the Python v2 path",
            "authentication",
        )
    process = manifest["compatibility"].get("process_protocol")
    if manifest["backend"] == "Python" and process != {
        "minimum": PROCESS_PROTOCOL_V3,
        "maximum": PROCESS_PROTOCOL_V3,
    }:
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "Python ABI v2 packs require exactly pybench-op-v3",
            "semantic_admission",
        )
    return manifest


def manifest_identities_v2(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    manifest = validate_manifest_v2(manifest_or_path, expected_backend=None)
    definitions = {
        operation["qualified_name"]: _identity(
            "scworkbench.operation_pack.definition.v2", operation
        )
        for operation in manifest["operations"]
    }
    manifest_identity = _identity("scworkbench.operation_pack.manifest.v2", manifest)
    return {
        "manifest_identity": manifest_identity,
        "manifest_sha256": manifest_identity["sha256"],
        "operation_definition_identities": definitions,
        "operation_definition_sha256": {
            name: identity["sha256"] for name, identity in definitions.items()
        },
    }


def _distribution_files(distribution: metadata.Distribution) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for item in distribution.files or []:
        relative = str(item).replace("\\", "/")
        parts = relative.split("/")
        if relative.startswith("/") or ".." in parts or any(not part for part in parts):
            _fail(
                "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
                "Distribution contains an unsafe relative path",
                "authentication",
            )
        path = Path(distribution.locate_file(item))
        if path.is_symlink():
            _fail(
                "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
                "Installed v2 pack content may not contain symbolic links",
                "authentication",
            )
        if (
            path.is_file()
            and not relative.endswith((".pyc", ".pyo"))
            and "__pycache__" not in parts
        ):
            files.append((relative, path))
    if not files:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "Distribution has no auditable files",
            "authentication",
        )
    return sorted(files)


def distribution_identity_v2(distribution: metadata.Distribution) -> dict[str, str]:
    entries = [
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for relative, path in _distribution_files(distribution)
    ]
    return _identity(
        "scworkbench.operation_pack.content.v1",
        {"scheme": "scworkbench.operation_pack.content_manifest.v1", "files": entries},
    )


def _installed_manifest_v2(
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
    return validate_manifest_v2(parse_manifest_v2_bytes(raw)), raw, relative


def _parse_entry_point(value: str) -> tuple[str, str, str]:
    try:
        group_name, target = value.split("=", 1)
        group, name = group_name.split(":", 1)
    except ValueError:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Python v2 entry point must be group:name=module:callable",
            "authentication",
        )
    if group != ENTRY_POINT_GROUP_V2 or not name or not _ENTRY_VALUE.fullmatch(target):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Python v2 entry point uses the wrong group or malformed target",
            "authentication",
        )
    return group, name, target


@dataclass(frozen=True)
class OperationPackHandlersV2:
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]]


def operation_pack_handlers_v2(
    handlers: Mapping[str, Callable[[Any, Any, Mapping[str, Any], Mapping[str, Any]], Any]],
) -> OperationPackHandlersV2:
    if not isinstance(handlers, Mapping):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler bindings must be a mapping",
            "authentication",
        )
    return OperationPackHandlersV2(dict(handlers))


@dataclass(frozen=True)
class PackDiscoveryV2:
    manifest: dict[str, Any]
    identity: dict[str, Any]
    distribution: metadata.Distribution
    entry_point: metadata.EntryPoint
    distribution_identity: dict[str, str]
    manifest_source_identity: dict[str, str]
    entry_point_identity: dict[str, str]

    def public_record(self) -> dict[str, Any]:
        return {
            "schema": "pybench.operation_pack.discovery.v2",
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


def _pack_identity(
    manifest: Mapping[str, Any],
    identities: Mapping[str, Any],
    distribution_identity: Mapping[str, str],
) -> dict[str, str]:
    value = {
        "scheme": "scworkbench.operation_pack.pack.v2",
        "manifest_sha256": identities["manifest_sha256"],
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": manifest["backend"],
        "entry_point": manifest["entry_point"],
        "operation_definition_sha256": identities["operation_definition_sha256"],
        "pack_content_sha256": distribution_identity["sha256"],
    }
    return _identity("scworkbench.operation_pack.pack.v2", value)


def discover_operation_pack_v2(manifest_path: str | os.PathLike[str]) -> PackDiscoveryV2:
    probe, _ = load_manifest_v2(manifest_path)
    probe = validate_manifest_v2(probe)
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
    manifest, raw, relative = _installed_manifest_v2(distribution, manifest_path)
    if canonical_json(manifest) != canonical_json(probe):
        _fail(
            "OPERATION_PACK_MANIFEST_IDENTITY_MISMATCH",
            "Supplied manifest differs from installed manifest",
            "authentication",
        )
    group, name, target = _parse_entry_point(manifest["entry_point"])
    candidates = [
        entry
        for entry in distribution.entry_points
        if entry.group == group and entry.name == name and entry.value == target
    ]
    if not candidates:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_UNAVAILABLE",
            "Distribution has no exact v2 entry point",
            "authentication",
        )
    if len(candidates) != 1:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_AMBIGUOUS",
            "Distribution has duplicate v2 entry points",
            "authentication",
        )
    identities = manifest_identities_v2(manifest)
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
        "schema_version": MANIFEST_SCHEMA_V2,
        "pack_abi_version": "2",
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
    identity["pack_identity"] = _pack_identity(manifest, identity, distribution_identity)
    identity["pack_sha256"] = identity["pack_identity"]["sha256"]
    identity["manifest_relative_path"] = relative
    return PackDiscoveryV2(
        manifest,
        identity,
        distribution,
        candidates[0],
        distribution_identity,
        source_identity,
        entry_identity,
    )


_ACTIVE_PACKS_V2: dict[str, tuple[PackDiscoveryV2, OperationPackHandlersV2]] = {}


def active_operation_packs_v2() -> Mapping[str, tuple[PackDiscoveryV2, OperationPackHandlersV2]]:
    return dict(_ACTIVE_PACKS_V2)


def activate_operation_pack_v2(
    discovery: PackDiscoveryV2,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> OperationPackHandlersV2:
    rediscovered = discover_operation_pack_v2(
        Path(discovery.distribution.locate_file(discovery.identity["manifest_relative_path"]))
    )
    if canonical_json(rediscovered.identity) != canonical_json(discovery.identity):
        _fail(
            "OPERATION_PACK_IDENTITY_MISMATCH",
            "Installed pack identity changed before activation",
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
    if not isinstance(registration, OperationPackHandlersV2):
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Entry point returned an invalid v2 registration",
            "authentication",
        )
    expected = {operation["qualified_name"] for operation in rediscovered.manifest["operations"]}
    if set(registration.handlers) != expected:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_INVALID",
            "Handler names differ from manifest operations",
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
                f"Handler {name!r} must accept readers, writers, params, context",
                "authentication",
            )
    candidate = dict(_ACTIVE_PACKS_V2)
    namespace = rediscovered.manifest["namespace"]
    if namespace in candidate:
        _fail(
            "OPERATION_PACK_NAMESPACE_COLLISION",
            f"Namespace {namespace!r} is already active",
            "catalog_publication",
        )
    active_operations = {
        name for _pack, handlers in candidate.values() for name in handlers.handlers
    }
    if expected & active_operations:
        _fail(
            "OPERATION_PACK_OPERATION_COLLISION",
            "Operation name collides with an active pack",
            "catalog_publication",
        )
    candidate[namespace] = (rediscovered, registration)
    before = dict(_ACTIVE_PACKS_V2)
    try:
        for event in ("catalog_before_pack", "catalog_pack_installed", "catalog_before_commit"):
            if fault_hook:
                fault_hook(event)
        _ACTIVE_PACKS_V2.clear()
        _ACTIVE_PACKS_V2.update(candidate)
    except Exception as exc:
        _ACTIVE_PACKS_V2.clear()
        _ACTIVE_PACKS_V2.update(before)
        if isinstance(exc, OperationPackError):
            raise
        _fail(
            "OPERATION_PACK_CATALOG_PUBLICATION_FAILED",
            f"Atomic v2 activation failed: {exc}",
            "catalog_publication",
        )
    return registration


def unload_operation_pack_v2(namespace: str) -> None:
    _ACTIVE_PACKS_V2.pop(namespace, None)


def _version_satisfies(version: str, specifier: str) -> bool:
    try:
        return Version(version) in SpecifierSet(specifier)
    except (InvalidVersion, InvalidSpecifier):
        return False
