from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import re
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator

from .operation_packs import OperationPackError, canonical_json
from .operation_packs_v2 import _identity
from .operation_packs_v3 import (
    PROCESS_PROTOCOL_V4,
    PackDiscoveryV3,
    active_operation_packs_v3,
)
from .phase4c_contracts import (
    finite_matrix,
    fragment_payload_identity,
    matrix_slice_content_identity,
    plain,
    publication_observations,
    validate_axis_bound_outputs,
    validate_fragment_grant,
    validate_parameter_axis_grants,
    validate_publication_grant,
)
from .protocol_v3 import (
    V3Readers,
    V3Writers,
    _no_absolute_paths,
    _observed_descriptor,
    _ordered,
    _read_inputs,
    _read_registry_reads,
    _restore,
    _safe_relative,
    _safe_root,
    _snapshot,
    _tree,
    _validate_schema,
    _Write,
    preflight_v3,
)

REQUEST_SCHEMA_V4 = "scworkbench.operation_pack.transition_request.v3"
RECEIPT_SCHEMA_V4 = "scworkbench.operation_pack.transition_receipt.v3"
_REQUEST_ASSET = "operation-pack-transition-request-v3.schema.json"
_RECEIPT_ASSET = "operation-pack-transition-receipt-v3.schema.json"
_CONSUMED_CAPABILITIES: set[str] = set()
_BROKER_TOKEN_ENV = "PYBENCH_OPERATION_PACK_BROKER_TOKEN"
_BROKER_TIMEOUT_ENV = "PYBENCH_OPERATION_PACK_BROKER_TIMEOUT_SECONDS"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: str, message: str, phase: str, **details: Any) -> None:
    raise OperationPackError(code, message, phase=phase, **details)


def _assert_identity(value: Mapping[str, Any], field: str, code: str, phase: str) -> None:
    claimed = value.get(field)
    payload = {key: child for key, child in value.items() if key != field}
    if (
        not isinstance(claimed, Mapping)
        or claimed.get("sha256")
        != _identity(claimed.get("scheme", "") if isinstance(claimed, Mapping) else "", payload)[
            "sha256"
        ]
    ):
        _fail(code, f"Self identity mismatch at {field}", phase, identity_field=field)


def _identity_equal(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _identity_projection(value: Mapping[str, Any]) -> dict[str, str]:
    return {"scheme": value["scheme"], "sha256": value["sha256"]}


def partition_axis(ids: Any, chunk_size: int) -> list[list[str]]:
    values = list(ids)
    if (
        not isinstance(chunk_size, int)
        or isinstance(chunk_size, bool)
        or chunk_size < 1
        or any(not isinstance(item, str) or not item for item in values)
        or len(values) != len(set(values))
    ):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Axis partitions require unique nonempty strings and a positive integer size",
            "execution",
        )
    return [values[start : start + chunk_size] for start in range(0, len(values), chunk_size)]


def _operation(discovery: PackDiscoveryV3, name: str) -> Mapping[str, Any]:
    value = next(
        (item for item in discovery.manifest["operations"] if item["qualified_name"] == name),
        None,
    )
    if value is None:
        _fail("OPERATION_PACK_REQUIRED", f"Operation {name!r} is absent", "resolution")
    return value


def _validate_typed_slice_grants(
    request: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    declarations = operation.get("typed_slice_reads", [])
    grants = request.get("typed_slice_grants", [])
    if [item["name"] for item in grants] != [item["name"] for item in declarations]:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Typed-slice grant closure differs from the manifest",
            "staging",
        )
    input_records = {item["name"]: item["descriptor"] for item in request["inputs"]}
    result: list[dict[str, Any]] = []
    for declaration, grant in zip(declarations, grants, strict=True):
        _assert_identity(grant, "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
        source = grant["source_reference"]
        _assert_identity(
            source,
            "source_reference_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
        if source.get("projection_identity") is not None:
            projection = {
                "schema": "scworkbench.operation_pack.typed_slice_source_projection.v1",
                "original_input_content_identity": source["input_content_identity"],
                "features_axis_manifest_identity": grant["features_axis"]["axis_manifest_identity"],
                "cells_axis_manifest_identity": grant["cells_axis"]["axis_manifest_identity"],
                "projected_content_identity": source["projected_content_identity"],
                "private_source_payload_identity": source["private_source_payload_identity"],
            }
            if source["projection_identity"] != _identity(
                "scworkbench.operation_pack.typed_slice_source_projection.v1",
                projection,
                "projection excluding projection_identity",
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    "Typed-slice private source projection identity differs",
                    "resolution",
                )
        input_name = declaration["input"]
        descriptor = input_records.get(input_name)
        if descriptor is None:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Typed-slice input descriptor is absent",
                "resolution",
            )
        requested = request["params"].get(declaration["chunk_size_parameter"])
        declaration_fields = (
            grant["name"] == declaration["name"],
            grant["input_name"] == input_name,
            grant["kind"] == declaration["kind"] == "matrix",
            grant["reader"] == declaration["reader"] == "read_typed_slices",
            grant["supported_axes"] == declaration["supported_axes"],
            grant["chunk_axis"] == declaration["chunk_axis"],
            grant["call_discipline"] == declaration["call_discipline"],
            grant["empty_selection_policy"] == declaration["empty_selection_policy"],
            grant["duplicate_policy"] == declaration["duplicate_policy"] == "reject",
            grant["requested_chunk_size"] == requested,
            grant["input_descriptor_identity"]
            == _identity_projection(descriptor["descriptor_identity"]),
            grant["input_content_identity"] == descriptor["content_identity"],
            source["input_name"] == input_name,
            source["input_descriptor_identity"] == grant["input_descriptor_identity"],
            source["input_content_identity"] == grant["input_content_identity"],
            source["transition_source_state_identity"]
            == request["transition"]["source_state_identity"],
            grant["max_outstanding_responses"] == 1,
        )
        if not all(declaration_fields):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice grant differs from its manifest, input, or transition",
                "staging",
            )
        cells = grant["cells_axis"]["count"]
        features = grant["features_axis"]["count"]
        if not cells or not features:
            if grant["empty_selection_policy"] != "allow_zero_axis":
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Typed-slice grant rejects its empty selected axis",
                    "staging",
                )
        if grant["chunk_axis"] == "cells":
            expected_rows = features
            expected_columns = min(grant["requested_chunk_size"], cells)
        else:
            expected_rows = min(grant["requested_chunk_size"], features)
            expected_columns = cells
        if (
            grant["max_rows"] != expected_rows
            or grant["max_columns"] != expected_columns
            or grant["max_elements"] != expected_rows * expected_columns
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice dimensional limits differ from the selected axes",
                "staging",
            )
        result.append(copy.deepcopy(grant))
    return result


def validate_request_v4(request: Mapping[str, Any], discovery: PackDiscoveryV3) -> dict[str, Any]:
    value = copy.deepcopy(dict(request))
    if (
        value.get("schema") != REQUEST_SCHEMA_V4
        or value.get("pack_abi_version") != "3"
        or value.get("process_protocol_version") != PROCESS_PROTOCOL_V4
    ):
        _fail(
            "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            "V4 execution requires the explicit manifest-v3/request-v3 tuple",
            "staging",
        )
    _validate_schema(value, _REQUEST_ASSET, "PYBENCH_CAPABILITY_MISMATCH", "staging")
    _no_absolute_paths(value)
    _assert_identity(
        value["transition"], "transition_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging"
    )
    for item in value["inputs"]:
        _assert_identity(
            item["descriptor"],
            "descriptor_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
    for item in value.get("registry_reads", []):
        _assert_identity(
            item["descriptor"],
            "descriptor_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
    for item in value.get("provenance_reads", []):
        _assert_identity(
            item,
            "descriptor_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "resolution",
        )
    for item in value["outputs"]:
        descriptor = item["descriptor"]
        if "cardinality_grant" in descriptor:
            _assert_identity(
                descriptor["cardinality_grant"],
                "cardinality_grant_identity",
                "PYBENCH_CAPABILITY_MISMATCH",
                "staging",
            )
        _assert_identity(descriptor, "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
        if "publication_grant" in descriptor:
            _assert_identity(
                descriptor["publication_grant"],
                "publication_grant_identity",
                "PYBENCH_CAPABILITY_MISMATCH",
                "staging",
            )
        if "matrix_fragment_grant" in descriptor:
            _assert_identity(
                descriptor["matrix_fragment_grant"],
                "grant_identity",
                "PYBENCH_CAPABILITY_MISMATCH",
                "staging",
            )
    for grant in value.get("typed_slice_grants", []):
        _assert_identity(grant, "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
    _assert_identity(
        value["execution_capability"],
        "capability_identity",
        "PYBENCH_CAPABILITY_MISMATCH",
        "staging",
    )
    _assert_identity(value, "request_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")

    name = value["operation_authentication"]["name"]
    operation = _operation(discovery, name)
    expected_authentication = {
        "name": name,
        "pack_identity": discovery.identity["pack_identity"],
        "manifest_identity": discovery.identity["manifest_identity"],
        "manifest_source_identity": discovery.manifest_source_identity,
        "distribution_identity": discovery.distribution_identity,
        "operation_definition_identity": discovery.identity["operation_definition_identities"].get(
            name
        ),
        "entry_point": {
            "value": discovery.manifest["entry_point"],
            "identity": discovery.entry_point_identity,
        },
    }
    if not _identity_equal(value["operation_authentication"], expected_authentication):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Request operation authentication differs from installed pack",
            "staging",
        )
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
                "Request input type or alignment differs",
                "resolution",
            )

    registry_declarations = {item["name"]: item for item in operation.get("registry_reads", [])}
    registry_grants = value.get("registry_reads", [])
    if [item["name"] for item in registry_grants] != sorted(registry_declarations):
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Registry read closure differs", "resolution")
    input_descriptors = {item["name"]: item["descriptor"] for item in value["inputs"]}
    for item in registry_grants:
        declaration = registry_declarations[item["name"]]
        descriptor = item["descriptor"]
        kind = declaration["kind"]
        valid = descriptor["kind"] == kind and descriptor["reader"] == declaration["reader"]
        if kind == "feature_set_registry":
            selector_binding = {
                key: declaration.get(key)
                for key in (
                    "namespace_parameter",
                    "namespaces_parameter",
                    "feature_sets_parameter",
                    "no_selector_policy",
                    "empty_match_policy",
                )
            }
            matrix = input_descriptors[declaration["matrix_input"]]
            valid &= (
                descriptor["matrix_input"] == declaration["matrix_input"]
                and descriptor["selector_binding"] == selector_binding
                and descriptor["matrix_content_identity"] == matrix["content_identity"]
                and descriptor["feature_order_identity"] == matrix["feature_order_identity"]
            )
        elif kind == "session_cell_axis":
            valid &= (
                descriptor["selection_policy"] == declaration["selection_policy"]
                and descriptor["coverage_policy"] == declaration["coverage_policy"]
                and descriptor["session_id"] == value["transition"]["session_id"]
                and descriptor["run_id"] == value["transition"]["run_id"]
                and descriptor["checkpoint_generation"]
                == value["transition"]["checkpoint_generation"]
                and descriptor["transition_source_state_identity"]
                == value["transition"]["source_state_identity"]
            )
        elif kind == "matrix_feature_annotations":
            matrix = input_descriptors[declaration["matrix_input"]]
            valid &= (
                descriptor["matrix_input"] == declaration["matrix_input"]
                and descriptor["fields"] == declaration["fields"]
                and descriptor["row_order"] == declaration["row_order"]
                and descriptor["missing_annotation_policy"]
                == declaration["missing_annotation_policy"]
                and descriptor["input_descriptor_identity"]
                == _identity_projection(matrix["descriptor_identity"])
                and descriptor["input_content_identity"] == matrix["content_identity"]
                and descriptor["feature_order_identity"] == matrix["feature_order_identity"]
            )
        else:
            valid = False
        if not valid:
            _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Registry read binding differs", "resolution")

    output_names = [item["name"] for item in value["outputs"]]
    if output_names != [item["name"] for item in operation["outputs"]]:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Request output closure differs", "staging")
    policies = {item["output"]: item for item in operation.get("dimension_policies", [])}
    outputs = {item["name"]: item for item in operation["outputs"]}
    publication_policies = {
        item["output"]: item for item in operation.get("publication_policies", [])
    }
    for item in value["outputs"]:
        declaration = outputs[item["name"]]
        descriptor = item["descriptor"]
        if (
            descriptor["category"],
            descriptor["kind"],
            descriptor["storage_profile"],
        ) != (declaration["category"], declaration["kind"], declaration["storage_profile"]):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH", "Output type tuple differs from manifest", "staging"
            )
        policy = policies.get(item["name"])
        grant = descriptor.get("cardinality_grant")
        if (policy is not None and policy.get("policy") == "authenticated_output_axes_v1") != (
            grant is not None
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Output cardinality grant presence differs",
                "staging",
            )
        if grant is not None:
            if (
                any(
                    grant[key] != policy[key]
                    for key in ("output", "policy", "shape", "row_order", "missing_values")
                )
                or grant["kind"] != declaration["kind"]
            ):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Output cardinality grant differs from manifest",
                    "staging",
                )
            cell_policy = policy["cell_axis"]
            cell_source = grant["cell_axis_source"]
            if cell_policy["source"] == "session_cell_axis_read_v1":
                registry_record = next(
                    record
                    for record in registry_grants
                    if record["name"] == cell_policy["registry_read"]
                )
                registry_descriptor = registry_record["descriptor"]
                valid_source = (
                    cell_source["source"] == cell_policy["source"]
                    and cell_source["registry_read"] == cell_policy["registry_read"]
                    and cell_source["descriptor_identity"]
                    == _identity_projection(registry_descriptor["descriptor_identity"])
                    and cell_source["selection_identity"]
                    == registry_descriptor["selection_identity"]
                    and grant["cell_axis_count"] == registry_descriptor["dimensions"][0]
                    and grant["cell_axis_order_identity"]
                    == registry_descriptor["cell_order_identity"]
                )
            else:
                input_descriptor = input_descriptors[cell_policy["input"]]
                valid_source = (
                    cell_source["input_name"] == cell_policy["input"]
                    and cell_source["input_descriptor_identity"]
                    == _identity_projection(input_descriptor["descriptor_identity"])
                    and cell_source["derivation"] == cell_policy["source"]
                )
            if not valid_source:
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Output cardinality cell-axis source differs from manifest",
                    "staging",
                )
            bounded = grant["shape"] == "bounded_grouped_cell_key_cartesian"
            expected_dimensions = [0 if bounded else grant["expected_row_count"]]
            if grant["shape"] in {"cell_key_cartesian", "bounded_grouped_cell_key_cartesian"}:
                expected_dimensions.append(3)
            if bounded:
                key_policy = policy["key_axis"]
                expected_resolution = {
                    "source": "declared_bounded_key_groups_v1",
                    "required_keys": key_policy["required_keys"],
                    "optional_groups": key_policy["optional_groups"],
                    "selection_owner": key_policy["selection_owner"],
                    "group_order": key_policy["group_order"],
                }
                if grant["key_axis_resolution"] != expected_resolution:
                    _fail(
                        "PYBENCH_CAPABILITY_MISMATCH",
                        "Bounded key-axis resolution differs from manifest",
                        "staging",
                    )
            if descriptor["expected_dimensions"] != expected_dimensions:
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Granted dimensions differ from cardinality",
                    "staging",
                )

        publication = descriptor.get("publication_grant")
        publication_policy = publication_policies.get(item["name"])
        if (publication is None) != (publication_policy is None):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Publication grant presence differs from the manifest",
                "staging",
            )
        if publication is not None:
            validate_publication_grant(publication, publication_policy, value, item)
            promotion_group = declaration["promotion_group"]
            expected_closure = sorted(
                child["name"]
                for child in operation["outputs"]
                if child["promotion_group"] == promotion_group
            )
            if (
                publication["promotion_group"] != promotion_group
                or publication["closure_outputs"] != expected_closure
            ):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Publication closure differs from the manifest promotion group",
                    "staging",
                )

        fragment = descriptor.get("matrix_fragment_grant")
        fragment_policy = declaration.get("fragment_policy")
        if (fragment is None) != (fragment_policy is None):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Matrix fragment grant presence differs from the manifest",
                "staging",
            )
        if fragment is not None:
            validate_fragment_grant(fragment, fragment_policy, value)
        axis_policy = policy is not None and policy.get("policy") in {
            "cell_by_parameter_axis_v1",
            "feature_parameter_axis_cartesian_v1",
            "one_per_parameter_axis_v1",
            "singleton_parameter_echo_v1",
        }
        if (descriptor.get("parameter_axis_grant_identity") is not None) != axis_policy:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Parameter-axis output binding presence differs from the manifest",
                "staging",
            )

    typed_slice_grants = _validate_typed_slice_grants(value, operation)
    parameter_axis_grants = validate_parameter_axis_grants(value, operation)
    for policy in operation.get("dimension_policies", []):
        kind = policy.get("policy")
        if kind not in {
            "cell_by_parameter_axis_v1",
            "feature_parameter_axis_cartesian_v1",
            "one_per_parameter_axis_v1",
            "singleton_parameter_echo_v1",
        }:
            continue
        axis_name = policy.get("parameter_axis", policy.get("dimension_axis"))
        axis = parameter_axis_grants[axis_name]
        descriptor = next(
            item["descriptor"] for item in value["outputs"] if item["name"] == policy["output"]
        )
        if descriptor.get("parameter_axis_grant_identity") != plain(axis["grant_identity"]):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Output parameter-axis binding differs from its grant",
                "staging",
            )
        expected_dimensions = {
            "cell_by_parameter_axis_v1": [axis["source"]["cell_axis_count"], axis["value"]],
            "feature_parameter_axis_cartesian_v1": [
                axis["source"]["feature_axis_count"] * axis["value"],
                0,
            ],
            "one_per_parameter_axis_v1": [axis["value"], 0],
            "singleton_parameter_echo_v1": [1, 0],
        }[kind]
        if descriptor.get("expected_dimensions") != expected_dimensions:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Output dimensions differ from its parameter-axis grant",
                "staging",
            )
        if (
            kind == "cell_by_parameter_axis_v1"
            and descriptor.get("expected_dimension_order_identity") != axis["axis_order_identity"]
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Space dimension order differs from its parameter-axis grant",
                "staging",
            )
        if kind == "cell_by_parameter_axis_v1":
            typed = next(
                item
                for item in typed_slice_grants
                if item["name"] == policy["cell_axis"]["typed_slice_read"]
            )
            if (
                descriptor.get("expected_cell_order_identity")
                != typed["cells_axis"]["order_identity"]
            ):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Space cell order differs from its typed-slice grant",
                    "staging",
                )

    declared_reads = {item["name"]: item for item in operation.get("provenance_reads", [])}
    observed_names = [item["name"] for item in value.get("provenance_reads", [])]
    if observed_names != sorted(observed_names) or not set(observed_names) <= set(declared_reads):
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Provenance read closure differs", "resolution")
    for item in value.get("provenance_reads", []):
        declaration = declared_reads[item["name"]]
        descriptor = item
        if (
            descriptor["input_name"] != declaration["input"]
            or descriptor["input_descriptor_identity"]
            != _identity_projection(input_descriptors[declaration["input"]]["descriptor_identity"])
            or descriptor["lifecycle_policy"] != declaration["lifecycle_policy"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH", "Provenance read binding differs", "resolution"
            )

    capability = value["execution_capability"]
    typed_input_names = {item["input_name"] for item in typed_slice_grants}
    expected_readers = [
        {
            "input_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in value["inputs"]
        if item["name"] not in typed_input_names
    ]
    expected_readers += [
        {
            "registry_read_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in value.get("registry_reads", [])
    ]
    expected_readers += [
        {
            "provenance_read_name": item["name"],
            "reader": item["reader"],
            "descriptor_identity": item["descriptor_identity"],
        }
        for item in value.get("provenance_reads", [])
    ]
    expected_readers += [
        {
            "typed_slice_name": item["name"],
            "reader": item["reader"],
            "grant_identity": item["grant_identity"],
        }
        for item in typed_slice_grants
    ]
    expected_writers = [
        {
            "output_name": item["name"],
            "writer": item["descriptor"]["writer"],
            "grant_identity": item["descriptor"]["grant_identity"],
        }
        for item in value["outputs"]
    ]
    bindings = (
        capability["session_id"] == value["transition"]["session_id"],
        capability["run_id"] == value["transition"]["run_id"],
        capability["checkpoint_generation"] == value["transition"]["checkpoint_generation"],
        capability["transition_identity"] == value["transition"]["transition_identity"],
        capability["source_state_identity"] == value["transition"]["source_state_identity"],
        capability["pack_identity"] == discovery.identity["pack_identity"],
        capability["distribution_identity"] == discovery.distribution_identity,
        capability["manifest_source_identity"] == discovery.manifest_source_identity,
        capability["operation_definition_identity"]
        == discovery.identity["operation_definition_identities"][name],
        capability["entry_point_identity"] == discovery.entry_point_identity,
        capability["reader_grants"] == expected_readers,
        capability["writer_grants"] == expected_writers,
        capability.get("typed_slice_grant_identities", [])
        == [_identity_projection(item["grant_identity"]) for item in typed_slice_grants],
        capability.get("parameter_axis_grant_identities", [])
        == [plain(item["grant_identity"]) for item in parameter_axis_grants.values()],
        capability.get("provenance_read_identities", [])
        == [
            _identity_projection(item["descriptor_identity"])
            for item in value.get("provenance_reads", [])
        ],
        capability["selected_requirement_set_identity"]
        == value["dependency_resolution"]["selected_requirement_set_identity"],
        capability["preflight_report_identity"]
        == value["dependency_resolution"]["preflight_report_identity"],
    )
    if (
        not all(bindings)
        or capability["mode"] != "private_staging_write"
        or capability["single_use"] is not True
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Capability bindings differ from request", "staging")
    errors = sorted(
        Draft202012Validator(operation["params_schema"]).iter_errors(value["params"]),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    if errors:
        _fail("OPERATION_PACK_PARAM_SCHEMA_INVALID", errors[0].message, "resolution")
    return value


def _distinct(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _row_pairs(keys: list[str], cells: list[str]) -> list[dict[str, str]]:
    return [{"key": key, "cell_id": cell} for key in keys for cell in cells]


def _validate_cardinality_grant(
    grant: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
    registry_values: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    source = grant["cell_axis_source"]
    if source.get("source") == "session_cell_axis_read_v1":
        payload = (registry_values or {}).get(source["registry_read"])
        if payload is None:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Cardinality session-cell snapshot is absent",
                "audit",
            )
        cells = _ordered(payload, "cell_id")
    else:
        payload = inputs.get(source["input_name"])
        if payload is None:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Cardinality cell-axis input is absent", "audit")
        cells = _distinct(_ordered(payload, "cell_id"))
    if grant["cell_axis_count"] != len(cells) or grant["cell_axis_order_identity"] != _identity(
        "scworkbench.cell_order.v1", cells
    ):
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Cardinality cell axis differs from authenticated input",
            "audit",
        )
    keys = list(grant.get("key_axis", []))
    if grant["shape"] == "cell_key_cartesian":
        expected_rows = len(cells) * len(keys)
        pairs = _row_pairs(keys, cells)
        if (
            grant["key_axis_count"] != len(keys)
            or grant["key_axis_order_identity"] != _identity("scworkbench.row_order.v1", keys)
            or grant["expected_row_count"] != expected_rows
            or grant["row_order_identity"] != _identity("scworkbench.row_order.v1", pairs)
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Cardinality key or row grant is inconsistent",
                "audit",
            )
    elif grant["shape"] == "bounded_grouped_cell_key_cartesian":
        resolution = grant["key_axis_resolution"]
        required = list(resolution["required_keys"])
        optional = [key for group in resolution["optional_groups"] for key in group["keys"]]
        if (
            grant["minimum_key_count"] != len(required)
            or grant["maximum_key_count"] != len(required) + len(optional)
            or grant["minimum_row_count"] != len(required) * len(cells)
            or grant["maximum_row_count"] != (len(required) + len(optional)) * len(cells)
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Bounded cardinality limits are inconsistent",
                "audit",
            )
    else:
        if grant["expected_row_count"] != len(cells) or grant["row_order_identity"] != _identity(
            "scworkbench.row_order.v1", cells
        ):
            _fail("PYBENCH_CAPABILITY_MISMATCH", "One-per-cell row grant is inconsistent", "audit")
    return cells, keys


def _validate_cardinality_payload(
    grant: Mapping[str, Any],
    payload: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
    semantic_constraints: Mapping[str, Any] | None = None,
    registry_values: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    cells, keys = _validate_cardinality_grant(grant, inputs, registry_values)
    payload_cells = _ordered(payload, "cell_id")
    values = payload.get("value")
    if not isinstance(values, list) or len(values) != len(payload_cells):
        _fail("PYBENCH_OUTPUT_INVALID", "Cardinality output values are malformed", "audit")
    expected_cells = cells
    expected_keys: list[str] | None = None
    selected_groups: list[str] | None = None
    if grant["shape"] in {"cell_key_cartesian", "bounded_grouped_cell_key_cartesian"}:
        if grant["shape"] == "bounded_grouped_cell_key_cartesian":
            observed_keys = _distinct(_ordered(payload, "key"))
            resolution = grant["key_axis_resolution"]
            required = list(resolution["required_keys"])
            selected_groups = []
            expected = list(required)
            cursor = len(required)
            for group in resolution["optional_groups"]:
                group_keys = list(group["keys"])
                if observed_keys[cursor : cursor + len(group_keys)] == group_keys:
                    selected_groups.append(group["name"])
                    expected.extend(group_keys)
                    cursor += len(group_keys)
            if observed_keys != expected:
                _fail(
                    "PYBENCH_OUTPUT_INVALID",
                    "Family keys are not complete bounded groups in manifest order",
                    "audit",
                )
            keys = observed_keys
        expected_cells = [cell for key in keys for cell in cells]
        expected_keys = [key for key in keys for cell in cells]
        if _ordered(payload, "key") != expected_keys:
            _fail("PYBENCH_OUTPUT_INVALID", "Family key axis/order differs from its grant", "audit")
    if payload_cells != expected_cells or len(
        set(zip(expected_keys or [""] * len(payload_cells), payload_cells, strict=True))
    ) != len(payload_cells):
        _fail("PYBENCH_OUTPUT_INVALID", "Output cell rows differ from cardinality grant", "audit")
    missing = sum(value is None for value in values)
    if grant["missing_values"] == "forbidden" and missing:
        _fail("PYBENCH_OUTPUT_INVALID", "Output contains forbidden missing values", "audit")
    for value in values:
        if isinstance(value, float) and not math.isfinite(value):
            _fail("PYBENCH_OUTPUT_INVALID", "Output contains a non-finite value", "audit")
    value_kind = (semantic_constraints or {}).get("value_kind")
    present = [value for value in values if value is not None]
    if value_kind == "continuous" and any(
        not isinstance(value, (int, float)) or isinstance(value, bool) for value in present
    ):
        _fail("PYBENCH_OUTPUT_INVALID", "Continuous output contains a nonnumeric value", "audit")
    if value_kind == "categorical" and any(not isinstance(value, str) for value in present):
        _fail("PYBENCH_OUTPUT_INVALID", "Categorical output contains a nonstring value", "audit")
    observed = {
        "schema": "scworkbench.operation_pack.observed_output_cardinality.v1",
        "cardinality_grant_identity": grant["cardinality_grant_identity"],
        "shape": grant["shape"],
        "observed_row_count": len(payload_cells),
        "observed_cell_axis_count": len(cells),
        "observed_cell_axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
    }
    if expected_keys is not None:
        observed["observed_key_axis_count"] = len(keys)
        if selected_groups is not None:
            observed["observed_key_axis"] = keys
        observed["observed_key_axis_order_identity"] = _identity("scworkbench.row_order.v1", keys)
        rows: Any = _row_pairs(keys, cells)
    else:
        rows = cells
    observed.update(
        observed_row_order_identity=_identity("scworkbench.row_order.v1", rows),
        missing_count=missing,
        semantic_validation="passed",
    )
    if selected_groups is not None:
        observed["selected_optional_groups"] = selected_groups
        observed["selected_optional_groups_identity"] = _identity(
            "scworkbench.operation_pack.selected_optional_groups.v1", selected_groups
        )
    observed["observed_cardinality_identity"] = _identity(
        "scworkbench.operation_pack.observed_output_cardinality.v1",
        observed,
        "observation excluding observed_cardinality_identity",
    )
    return observed


def _strict_json_file(path: Path, code: str, phase: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code, f"JSON repeats object key {key!r}", phase)
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except OperationPackError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(code, f"JSON payload is unreadable: {exc}", phase)
    if not isinstance(value, dict):
        _fail(code, "JSON payload root is not an object", phase)
    return value


def _axis_manifest(root: Path, descriptor: Mapping[str, Any], expected_axis: str) -> dict[str, Any]:
    if descriptor["axis"] != expected_axis:
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Typed-slice axis descriptor names the wrong axis",
            "resolution",
        )
    path = _safe_relative(root, descriptor["relative_path"], must_exist=True)
    if path.is_symlink() or not path.is_file():
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Typed-slice axis manifest is not a regular contained file",
            "resolution",
        )
    raw = path.read_bytes()
    value = _strict_json_file(path, "PYBENCH_INPUT_IDENTITY_MISMATCH", "resolution")
    expected_fields = {
        "schema",
        "axis",
        "count",
        "ids",
        "order_identity",
        "manifest_identity",
    }
    if set(value) != expected_fields or raw != canonical_json(value).encode("utf-8"):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Typed-slice axis manifest is not the closed canonical representation",
            "resolution",
        )
    _assert_identity(
        value,
        "manifest_identity",
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
        "resolution",
    )
    ids = value["ids"]
    order_scheme = (
        "scworkbench.cell_order.v1" if expected_axis == "cells" else "scworkbench.feature_order.v1"
    )
    payload_preimage = {
        "path": descriptor["relative_path"],
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    bindings = (
        value["schema"] == "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
        value["axis"] == expected_axis,
        isinstance(ids, list),
        all(isinstance(item, str) and item for item in ids),
        len(ids) == len(set(ids)),
        value["count"] == len(ids) == descriptor["count"],
        value["order_identity"] == _identity(order_scheme, ids),
        value["order_identity"] == descriptor["order_identity"],
        value["manifest_identity"] == descriptor["axis_manifest_identity"],
        descriptor["payload_manifest_identity"]
        == _identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            payload_preimage,
            "registry read payload manifest",
        ),
    )
    if not all(bindings):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Typed-slice axis manifest differs from its request binding",
            "resolution",
        )
    return value


def _wait_for(predicate: Callable[[], bool], timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            _fail("PYBENCH_PROCESS_FAILED", message, "execution")
        time.sleep(0.01)


def _regular_response_files(response: Path) -> dict[str, Path]:
    if response.is_symlink() or not response.is_dir():
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Broker response is not a directory", "execution")
    expected = {"matrix.mtx", "features.json", "cells.json", "manifest.json"}
    try:
        children = {item.name: item for item in response.iterdir()}
    except OSError as exc:
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH", f"Broker response is unreadable: {exc}", "execution"
        )
    if set(children) != expected or any(
        path.is_symlink() or not path.is_file() for path in children.values()
    ):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Broker response contains an unexpected or non-regular file",
            "execution",
        )
    return children


def _parse_matrix_market(path: Path) -> tuple[list[list[float]], list[dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            f"Matrix Market payload is unreadable: {exc}",
            "execution",
        )
    if not lines or lines[0] != "%%MatrixMarket matrix coordinate real general":
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Matrix Market header differs", "execution")
    body = [line for line in lines[1:] if line and not line.startswith("%")]
    if not body:
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Matrix Market dimensions are absent", "execution")
    try:
        nrow, ncolumn, nonzero = (int(item) for item in body[0].split())
    except (TypeError, ValueError) as exc:
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            f"Matrix Market dimensions are invalid: {exc}",
            "execution",
        )
    if nrow < 0 or ncolumn < 0 or nonzero < 0 or len(body) != nonzero + 1:
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Matrix Market cardinality differs", "execution")
    values = [[0.0 for _ in range(ncolumn)] for _ in range(nrow)]
    entries: list[dict[str, Any]] = []
    prior = (0, 0)
    for line in body[1:]:
        fields = line.split()
        if len(fields) != 3:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH", "Matrix Market entry is malformed", "execution"
            )
        try:
            row, column, value = int(fields[0]), int(fields[1]), float(fields[2])
        except ValueError as exc:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Matrix Market entry is invalid: {exc}",
                "execution",
            )
        key = (column, row)
        if (
            row < 1
            or row > nrow
            or column < 1
            or column > ncolumn
            or key <= prior
            or not math.isfinite(value)
            or value == 0
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Matrix Market coordinates, order, or values are invalid",
                "execution",
            )
        prior = key
        values[row - 1][column - 1] = value
        entries.append({"row": row, "column": column, "value": value})
    return values, entries


class TypedSliceBrokerClient:
    def __init__(
        self,
        request: Mapping[str, Any],
        grant: Mapping[str, Any],
        stage_root: Path,
        *,
        authorization_token: str | None = None,
        timeout: float | None = None,
    ):
        self._request = request
        self._grant = copy.deepcopy(dict(grant))
        self._root = _safe_root(stage_root)
        self._broker = _safe_relative(self._root, "broker")
        self._token = authorization_token or os.environ.get(_BROKER_TOKEN_ENV, "")
        if not _HEX64.fullmatch(self._token):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice broker authorization token is absent or malformed",
                "staging",
            )
        raw_timeout = os.environ.get(_BROKER_TIMEOUT_ENV, "300")
        try:
            self._timeout = float(raw_timeout) if timeout is None else float(timeout)
        except ValueError:
            self._timeout = 0
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Typed-slice broker timeout is invalid", "staging")
        if self._broker.is_symlink() or not self._broker.is_dir():
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice broker endpoint is absent or invalid",
                "staging",
            )
        self._cells = _axis_manifest(self._root, grant["cells_axis"], "cells")["ids"]
        self._features = _axis_manifest(self._root, grant["features_axis"], "features")["ids"]
        self._calls: list[dict[str, Any]] = []
        self._chunk_values: list[str] = []

    @property
    def selected_cells(self) -> list[str]:
        return list(self._cells)

    @property
    def selected_features(self) -> list[str]:
        return list(self._features)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._calls)

    def _selection(self, features: Any, cells: Any) -> tuple[list[str], list[str]]:
        if features is None or cells is None:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice reader requires explicit feature and cell axes",
                "execution",
            )
        selected_features = list(features)
        selected_cells = list(cells)

        def valid(selected: list[Any], allowed: list[str]) -> bool:
            return (
                bool(selected)
                and all(isinstance(item, str) and item for item in selected)
                and len(selected) == len(set(selected))
                and all(item in allowed for item in selected)
                and selected == [item for item in allowed if item in set(selected)]
            )

        if not valid(selected_features, self._features) or not valid(selected_cells, self._cells):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Typed-slice selection is empty, duplicate, unknown, or out of order",
                "execution",
            )
        chunk_axis = self._grant["chunk_axis"]
        chunk = selected_cells if chunk_axis == "cells" else selected_features
        nonchunk = selected_features if chunk_axis == "cells" else selected_cells
        allowed_chunk = self._cells if chunk_axis == "cells" else self._features
        allowed_nonchunk = self._features if chunk_axis == "cells" else self._cells
        if len(chunk) > self._grant["requested_chunk_size"]:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice chunk exceeds its requested size",
                "execution",
            )
        if self._grant["call_discipline"] == "ordered_partition_exactly_once":
            start = len(self._chunk_values)
            if nonchunk != allowed_nonchunk or chunk != allowed_chunk[start : start + len(chunk)]:
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Typed-slice call violates ordered exactly-once partitioning",
                    "execution",
                )
        else:
            nonchunk_axis = "features" if chunk_axis == "cells" else "cells"
            if nonchunk_axis not in self._grant["supported_axes"] and nonchunk != allowed_nonchunk:
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Typed-slice non-chunk axis exceeds its supported selection",
                    "execution",
                )
        if (
            len(selected_features) > self._grant["max_rows"]
            or len(selected_cells) > self._grant["max_columns"]
            or len(selected_features) * len(selected_cells) > self._grant["max_elements"]
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH", "Typed-slice request exceeds its limits", "execution"
            )
        return selected_features, selected_cells

    def __call__(self, *, features=None, cells=None) -> dict[str, Any]:
        selected_features, selected_cells = self._selection(features, cells)
        ordinal = len(self._calls) + 1
        control = {
            "schema": "scworkbench.operation_pack.typed_slice_control_request.v1",
            "request_identity": copy.deepcopy(self._request["request_identity"]),
            "grant_identity": copy.deepcopy(self._grant["grant_identity"]),
            "ordinal": ordinal,
            "features": selected_features,
            "cells": selected_cells,
            "authorization_token": self._token,
        }
        control["control_identity"] = _identity(
            "scworkbench.operation_pack.typed_slice_control_request.v1",
            control,
            "control request excluding control_identity",
        )
        encoded = canonical_json(control).encode("utf-8")
        if len(encoded) > self._grant["max_control_bytes"]:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice control exceeds its byte limit",
                "execution",
            )
        pending = self._broker / "request.json.pending"
        target = self._broker / "request.json"
        if pending.exists() or target.exists() or (self._broker / "response").exists():
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Typed-slice broker is not ready", "execution")
        try:
            pending.write_bytes(encoded)
            os.replace(pending, target)
            response = self._broker / "response"
            _wait_for(response.exists, self._timeout, "Timed out waiting for typed-slice response")
            payload, result, response_identity = self._consume_response(
                response, ordinal, selected_features, selected_cells
            )
            acknowledgement = {
                "schema": "scworkbench.operation_pack.typed_slice_acknowledgement.v1",
                "request_identity": copy.deepcopy(self._request["request_identity"]),
                "grant_identity": copy.deepcopy(self._grant["grant_identity"]),
                "ordinal": ordinal,
                "response_identity": copy.deepcopy(response_identity),
                "status": "consumed",
                "authorization_token": self._token,
            }
            acknowledgement["acknowledgement_identity"] = _identity(
                "scworkbench.operation_pack.typed_slice_acknowledgement.v1",
                acknowledgement,
                "acknowledgement excluding acknowledgement_identity",
            )
            ack_pending = self._broker / "acknowledgement.json.pending"
            ack = self._broker / "acknowledgement.json"
            ack_pending.write_text(canonical_json(acknowledgement), encoding="utf-8")
            os.replace(ack_pending, ack)
            _wait_for(
                lambda: not response.exists() and not ack.exists() and not target.exists(),
                self._timeout,
                "Timed out waiting for typed-slice acknowledgement",
            )
        finally:
            pending.unlink(missing_ok=True)
        call = copy.deepcopy(result)
        call["schema"] = "scworkbench.operation_pack.observed_typed_slice_call.v1"
        call.pop("source_reference_identity")
        call["call_identity"] = _identity(
            "scworkbench.operation_pack.observed_typed_slice_call.v1",
            call,
            "call excluding call_identity",
        )
        self._calls.append(call)
        self._chunk_values.extend(
            selected_cells if self._grant["chunk_axis"] == "cells" else selected_features
        )
        return payload

    def _consume_response(
        self,
        response: Path,
        ordinal: int,
        selected_features: list[str],
        selected_cells: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        files = _regular_response_files(response)
        total_bytes = sum(path.stat().st_size for path in files.values())
        if total_bytes > self._grant["max_response_bytes"]:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Typed-slice response exceeds its byte limit",
                "execution",
            )
        manifest = _strict_json_file(
            files["manifest.json"], "PYBENCH_INPUT_IDENTITY_MISMATCH", "execution"
        )
        _assert_identity(
            manifest,
            "response_identity",
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "execution",
        )
        response_fields = {
            "schema",
            "request_identity",
            "grant_identity",
            "source_reference_identity",
            "ordinal",
            "dimensions",
            "nonzero_count",
            "feature_axis_manifest_identity",
            "cell_axis_manifest_identity",
            "files",
            "semantic_content_identity",
            "slice_result_identity",
            "response_identity",
        }
        expected_response_identity = _identity(
            "scworkbench.operation_pack.typed_slice_response.v1",
            {key: value for key, value in manifest.items() if key != "response_identity"},
            "response excluding response_identity",
        )
        expected_entries = []
        for role, name in (
            ("matrix_market", "matrix.mtx"),
            ("features_axis", "features.json"),
            ("cells_axis", "cells.json"),
        ):
            raw = files[name].read_bytes()
            expected_entries.append(
                {
                    "role": role,
                    "path": name,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        if manifest.get("files") != expected_entries:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Typed-slice response file manifest differs",
                "execution",
            )
        feature_axis = self._response_axis(files["features.json"], "features", selected_features)
        cell_axis = self._response_axis(files["cells.json"], "cells", selected_cells)
        values, entries = _parse_matrix_market(files["matrix.mtx"])
        dimensions = [len(selected_features), len(selected_cells)]
        semantic_value = {
            "schema": "scworkbench.operation_pack.matrix_slice.content.v1",
            "kind": "matrix",
            "dimensions": dimensions,
            "feature_id": selected_features,
            "cell_id": selected_cells,
            "entries": entries,
        }
        semantic_identity = _identity(
            "scworkbench.operation_pack.matrix_slice.content.v1", semantic_value
        )
        result = {
            "schema": "scworkbench.operation_pack.typed_slice_result.v1",
            "source_reference_identity": _identity_projection(
                self._grant["source_reference"]["source_reference_identity"]
            ),
            "grant_identity": _identity_projection(self._grant["grant_identity"]),
            "ordinal": ordinal,
            "feature_count": len(selected_features),
            "feature_order_identity": feature_axis["order_identity"],
            "cell_count": len(selected_cells),
            "cell_order_identity": cell_axis["order_identity"],
            "dimensions": dimensions,
            "semantic_content_identity": semantic_identity,
        }
        result["slice_result_identity"] = _identity(
            "scworkbench.operation_pack.typed_slice_result.v1",
            result,
            "slice result excluding slice_result_identity",
        )
        bindings = (
            set(manifest) == response_fields,
            manifest.get("schema") == "scworkbench.operation_pack.typed_slice_response.v1",
            manifest.get("response_identity") == expected_response_identity,
            manifest.get("request_identity") == self._request["request_identity"],
            manifest.get("grant_identity") == self._grant["grant_identity"],
            manifest.get("source_reference_identity")
            == self._grant["source_reference"]["source_reference_identity"],
            manifest.get("ordinal") == ordinal,
            manifest.get("dimensions") == dimensions,
            manifest.get("nonzero_count") == len(entries),
            manifest.get("feature_axis_manifest_identity") == feature_axis["manifest_identity"],
            manifest.get("cell_axis_manifest_identity") == cell_axis["manifest_identity"],
            manifest.get("semantic_content_identity") == semantic_identity,
            manifest.get("slice_result_identity") == result["slice_result_identity"],
        )
        if not all(bindings):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Typed-slice response binding differs",
                "execution",
            )
        if [len(values), len(values[0]) if values else len(selected_cells)] != dimensions:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Typed-slice matrix dimensions differ",
                "execution",
            )
        payload = {
            "kind": "matrix",
            "dimensions": dimensions,
            "feature_id": selected_features,
            "cell_id": selected_cells,
            "values": values,
            "storage_profile": "matrix_ordinary_v1",
            "meta": {},
        }
        return payload, result, copy.deepcopy(manifest["response_identity"])

    def _response_axis(self, path: Path, axis: str, expected: list[str]) -> dict[str, Any]:
        value = _strict_json_file(path, "PYBENCH_INPUT_IDENTITY_MISMATCH", "execution")
        if path.read_bytes() != canonical_json(value).encode("utf-8"):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Response axis is not canonical JSON",
                "execution",
            )
        _assert_identity(value, "manifest_identity", "PYBENCH_INPUT_IDENTITY_MISMATCH", "execution")
        scheme = "scworkbench.cell_order.v1" if axis == "cells" else "scworkbench.feature_order.v1"
        if (
            set(value) != {"schema", "axis", "count", "ids", "order_identity", "manifest_identity"}
            or value["schema"] != "scworkbench.operation_pack.typed_slice_axis_manifest.v1"
            or value["axis"] != axis
            or value["ids"] != expected
            or value["count"] != len(expected)
            or value["order_identity"] != _identity(scheme, expected)
            or value["manifest_identity"]
            != _identity(
                "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
                {key: child for key, child in value.items() if key != "manifest_identity"},
                "axis manifest excluding manifest_identity",
            )
        ):
            _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Response axis binding differs", "execution")
        return value

    def usage(self) -> dict[str, Any]:
        allowed = self._cells if self._grant["chunk_axis"] == "cells" else self._features
        empty_selection = not self._cells or not self._features
        if empty_selection:
            if self._calls or self._chunk_values:
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Empty typed-slice selection unexpectedly served a call",
                    "receipt",
                )
            coverage = "empty"
        elif self._grant["call_discipline"] == "ordered_partition_exactly_once":
            if self._chunk_values != allowed:
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Typed-slice execution did not cover its ordered axis exactly once",
                    "receipt",
                )
            coverage = "exact"
        else:
            coverage = "independent"
        scheme = (
            "scworkbench.cell_order.v1"
            if self._grant["chunk_axis"] == "cells"
            else "scworkbench.feature_order.v1"
        )
        usage = {
            "schema": "scworkbench.operation_pack.typed_slice_usage.v1",
            "request_identity": _identity_projection(self._request["request_identity"]),
            "capability_identity": _identity_projection(
                self._request["execution_capability"]["capability_identity"]
            ),
            "operation_definition_identity": _identity_projection(
                self._request["operation_authentication"]["operation_definition_identity"]
            ),
            "grant_identity": _identity_projection(self._grant["grant_identity"]),
            "requested_chunk_size": self._grant["requested_chunk_size"],
            "calls": copy.deepcopy(self._calls),
            "call_count": len(self._calls),
            "concatenated_chunk_axis_order_identity": _identity(scheme, self._chunk_values),
            "unique_chunk_axis_count": len(set(self._chunk_values)),
            "duplicate_chunk_axis_count": len(self._chunk_values) - len(set(self._chunk_values)),
            "coverage_status": coverage,
        }
        usage["usage_identity"] = _identity(
            "scworkbench.operation_pack.typed_slice_usage.v1",
            usage,
            "usage excluding usage_identity",
        )
        return usage


class V4Readers(V3Readers):
    def __init__(
        self,
        values,
        registry_values=None,
        provenance_values=None,
        typed_slice_clients: Mapping[str, TypedSliceBrokerClient] | None = None,
    ):
        super().__init__(values, registry_values)
        self._provenance_values = dict(provenance_values or {})
        self._provenance_calls: list[str] = []
        self._typed_slice_clients = dict(typed_slice_clients or {})

    @property
    def provenance_calls(self) -> list[str]:
        return list(self._provenance_calls)

    def __getitem__(self, key):
        if key in self._typed_slice_clients:
            return self._typed_slice_clients[key]
        if key in self._provenance_values:

            def read_ancestry(*args, **kwargs):
                if args or kwargs or key in self._provenance_calls:
                    _fail(
                        "PYBENCH_CAPABILITY_MISMATCH",
                        "Ancestry reader is no-argument and single-use",
                        "execution",
                    )
                self._provenance_calls.append(key)
                return copy.deepcopy(self._provenance_values[key])

            return read_ancestry
        return super().__getitem__(key)

    def __iter__(self):
        return iter((*super().__iter__(), *self._provenance_values, *self._typed_slice_clients))

    def __len__(self):
        return super().__len__() + len(self._provenance_values) + len(self._typed_slice_clients)


class MatrixFragmentWriter:
    def __init__(self, owner: "V4Writers", name: str):
        self._owner = owner
        self._name = name
        self._grant_record = owner._grants.get(name)
        if (
            owner._consumed
            or self._grant_record is None
            or self._grant_record["descriptor"]["writer"] != "write_matrix"
            or "matrix_fragment_grant" not in self._grant_record["descriptor"]
            or name in owner._writes
            or name in owner._fragment_handles
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                f"Fragment writer is not granted for {name!r}",
                "execution",
            )
        owner._fragment_handles[name] = self
        self._grant = self._grant_record["descriptor"]["matrix_fragment_grant"]
        self._source = owner._inputs[self._grant["source_input"]]
        self._features = list(self._source["feature_id"])
        self._cells = list(self._source["cell_id"])
        self._schedule = self._grant["source_fragment_schedule"]["fragments"]
        if self._grant["feature_order_identity"] != _identity(
            "scworkbench.feature_order.v1", self._features
        ) or self._grant["cell_order_identity"] != _identity(
            "scworkbench.cell_order.v1", self._cells
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Matrix fragment grant axes differ from the authenticated input",
                "resolution",
            )
        for parent in self._schedule:
            start = parent["cell_start"] - 1
            stop = start + parent["cell_count"]
            if parent["cell_order_identity"] != _identity(
                "scworkbench.cell_order.v1", self._cells[start:stop]
            ):
                _fail(
                    "PYBENCH_INPUT_IDENTITY_MISMATCH",
                    "Source fragment schedule differs from the authenticated cell axis",
                    "resolution",
                )
        self._next_cell = 1
        self._entries: list[dict[str, Any]] = []
        self._paths: list[Path] = []
        self._parts: dict[int, int] = {}
        self._finished = False

    @property
    def source_fragments(self) -> list[dict[str, int]]:
        return [
            {
                "ordinal": item["ordinal"],
                "cell_start": item["cell_start"],
                "cell_count": item["cell_count"],
            }
            for item in self._schedule
        ]

    def write_fragment(
        self,
        source_fragment_ordinal: int,
        values: Any,
        feature_ids: Any,
        cell_ids: Any,
    ) -> dict[str, Any]:
        if self._owner._consumed or self._finished:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Fragment writer is consumed", "execution")
        if (
            not isinstance(source_fragment_ordinal, int)
            or isinstance(source_fragment_ordinal, bool)
            or not 1 <= source_fragment_ordinal <= len(self._schedule)
        ):
            _fail("PYBENCH_OUTPUT_INVALID", "Source fragment ordinal is invalid", "audit")
        feature_ids = list(feature_ids)
        cell_ids = list(cell_ids)
        parent = self._schedule[source_fragment_ordinal - 1]
        parent_start = parent["cell_start"]
        parent_end = parent_start + parent["cell_count"] - 1
        observed_end = self._next_cell + len(cell_ids) - 1
        expected_cells = self._cells[self._next_cell - 1 : observed_end]
        if (
            feature_ids != self._features
            or not cell_ids
            or cell_ids != expected_cells
            or self._next_cell < parent_start
            or observed_end > parent_end
            or not finite_matrix(values, len(feature_ids), len(cell_ids))
        ):
            _fail(
                "PYBENCH_OUTPUT_INVALID",
                "Matrix fragment axes, parent boundary, or values are invalid",
                "audit",
            )
        ordinal = len(self._entries) + 1
        part = self._parts.get(source_fragment_ordinal, 0) + 1
        self._parts[source_fragment_ordinal] = part
        payload = {
            "schema": "scworkbench.operation_pack.matrix_fragment.v1",
            "ordinal": ordinal,
            "source_fragment_ordinal": source_fragment_ordinal,
            "source_fragment_identity": parent["source_fragment_identity"],
            "part_ordinal": part,
            "cell_start": self._next_cell,
            "dimensions": [len(feature_ids), len(cell_ids)],
            "feature_id": feature_ids,
            "cell_id": cell_ids,
            "values": copy.deepcopy(values),
        }
        root = _safe_relative(
            self._owner._stage_root,
            f"outputs/{self._name}/fragments/{ordinal:06d}",
        )
        if root.exists():
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "Matrix fragment path exists", "execution")
        root.parent.mkdir(parents=True, exist_ok=True)
        path = root.with_suffix(".json")
        path.write_text(canonical_json(payload), encoding="utf-8")
        relative = path.relative_to(self._owner._stage_root).as_posix()
        entry = {
            "ordinal": ordinal,
            "source_fragment_ordinal": source_fragment_ordinal,
            "source_fragment_identity": parent["source_fragment_identity"],
            "part_ordinal": part,
            "cell_start": self._next_cell,
            "cell_count": len(cell_ids),
            "dimensions": [len(feature_ids), len(cell_ids)],
            "feature_order_identity": _identity("scworkbench.feature_order.v1", feature_ids),
            "cell_order_identity": _identity("scworkbench.cell_order.v1", cell_ids),
            "content_identity": matrix_slice_content_identity(values, feature_ids, cell_ids),
            "payload_manifest_identity": fragment_payload_identity(path, relative),
        }
        self._entries.append(entry)
        self._paths.append(path)
        self._next_cell += len(cell_ids)
        return copy.deepcopy(entry)

    def finish(self, semantic_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if (
            self._owner._consumed
            or self._finished
            or self._next_cell != len(self._cells) + 1
            or len(self._entries) < self._grant["fragment_policy"]["minimum_fragment_count"]
        ):
            _fail(
                "PYBENCH_OUTPUT_INVALID",
                "Matrix fragment closure is incomplete or already consumed",
                "audit",
            )
        manifest = {
            "schema": "scworkbench.operation_pack.matrix_fragment_manifest.v1",
            "source_fragment_schedule_identity": self._grant["source_fragment_schedule"][
                "schedule_identity"
            ],
            "dimensions": [len(self._features), len(self._cells)],
            "feature_order_identity": self._grant["feature_order_identity"],
            "cell_order_identity": self._grant["cell_order_identity"],
            "fragment_count": len(self._entries),
            "fragments": copy.deepcopy(self._entries),
        }
        manifest["fragment_manifest_identity"] = _identity(
            manifest["schema"], manifest, "manifest excluding fragment_manifest_identity"
        )
        root = _safe_relative(self._owner._stage_root, f"outputs/{self._name}")
        value = {
            "kind": "matrix",
            "dimensions": manifest["dimensions"],
            "feature_id": self._features,
            "cell_id": self._cells,
            "matrix_fragment_manifest": manifest,
            "meta": copy.deepcopy(dict(semantic_metadata or {})),
        }
        path = root / "payload.json"
        path.write_text(canonical_json(value), encoding="utf-8")
        self._paths.append(path)
        self._owner._writes[self._name] = _Write(
            value,
            path,
            dict(semantic_metadata or {}),
            "write_matrix",
            list(self._paths),
        )
        self._finished = True
        return {
            "name": self._name,
            "allocated_id": self._grant_record["allocated_id"],
            "grant_identity": self._grant_record["descriptor"]["grant_identity"],
        }


def _validate_matrix_fragment_stage(
    root: Path,
    output_name: str,
    value: Mapping[str, Any],
    grant: Mapping[str, Any],
) -> list[Path]:
    manifest = value.get("matrix_fragment_manifest")
    if not isinstance(manifest, Mapping):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment manifest is absent", "receipt")
    _assert_identity(
        manifest,
        "fragment_manifest_identity",
        "PYBENCH_RECEIPT_MISMATCH",
        "receipt",
    )
    entries = manifest.get("fragments")
    if not isinstance(entries, list) or manifest.get("fragment_count") != len(entries):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment closure differs", "receipt")
    fragment_root = _safe_relative(root, f"outputs/{output_name}/fragments")
    if not fragment_root.is_dir():
        _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment directory is absent", "receipt")
    paths = sorted(fragment_root.glob("*.json"))
    expected_names = [f"{ordinal:06d}.json" for ordinal in range(1, len(entries) + 1)]
    if [path.name for path in paths] != expected_names:
        _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment path closure differs", "receipt")
    features = list(value.get("feature_id", []))
    cells = list(value.get("cell_id", []))
    schedule = grant["source_fragment_schedule"]["fragments"]
    next_cell = 1
    part_counts: dict[int, int] = {}
    payload_fields = {
        "schema",
        "ordinal",
        "source_fragment_ordinal",
        "source_fragment_identity",
        "part_ordinal",
        "cell_start",
        "dimensions",
        "feature_id",
        "cell_id",
        "values",
    }
    for ordinal, (entry, path) in enumerate(zip(entries, paths, strict=True), start=1):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                f"Matrix fragment payload is unreadable: {exc}",
                "receipt",
            )
        if not isinstance(payload, Mapping) or set(payload) != payload_fields:
            _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment payload shape differs", "receipt")
        source_ordinal_value = payload.get("source_fragment_ordinal")
        if (
            not isinstance(source_ordinal_value, (int, float))
            or isinstance(source_ordinal_value, bool)
            or not math.isfinite(source_ordinal_value)
            or source_ordinal_value != int(source_ordinal_value)
            or not 1 <= int(source_ordinal_value) <= len(schedule)
        ):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment parent differs", "receipt")
        source_ordinal = int(source_ordinal_value)
        parent = schedule[source_ordinal - 1]
        part_ordinal = part_counts.get(source_ordinal, 0) + 1
        part_counts[source_ordinal] = part_ordinal
        fragment_cells = list(payload.get("cell_id", []))
        values = payload.get("values")
        parent_end = parent["cell_start"] + parent["cell_count"] - 1
        observed_end = next_cell + len(fragment_cells) - 1
        if (
            payload.get("schema") != "scworkbench.operation_pack.matrix_fragment.v1"
            or payload.get("ordinal") != ordinal
            or payload.get("part_ordinal") != part_ordinal
            or payload.get("cell_start") != next_cell
            or payload.get("source_fragment_identity") != parent["source_fragment_identity"]
            or list(payload.get("feature_id", [])) != features
            or fragment_cells != cells[next_cell - 1 : observed_end]
            or next_cell < parent["cell_start"]
            or observed_end > parent_end
            or not finite_matrix(values, len(features), len(fragment_cells))
        ):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment payload differs", "receipt")
        relative = path.relative_to(root).as_posix()
        expected_entry = {
            "ordinal": ordinal,
            "source_fragment_ordinal": source_ordinal,
            "source_fragment_identity": parent["source_fragment_identity"],
            "part_ordinal": part_ordinal,
            "cell_start": next_cell,
            "cell_count": len(fragment_cells),
            "dimensions": [len(features), len(fragment_cells)],
            "feature_order_identity": _identity("scworkbench.feature_order.v1", features),
            "cell_order_identity": _identity("scworkbench.cell_order.v1", fragment_cells),
            "content_identity": matrix_slice_content_identity(values, features, fragment_cells),
            "payload_manifest_identity": fragment_payload_identity(path, relative),
        }
        if entry != expected_entry:
            _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment evidence differs", "receipt")
        next_cell += len(fragment_cells)
    if (
        next_cell != len(cells) + 1
        or manifest.get("dimensions") != [len(features), len(cells)]
        or manifest.get("feature_order_identity")
        != _identity("scworkbench.feature_order.v1", features)
        or manifest.get("cell_order_identity") != _identity("scworkbench.cell_order.v1", cells)
        or manifest.get("source_fragment_schedule_identity")
        != grant["source_fragment_schedule"]["schedule_identity"]
    ):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Matrix fragment manifest differs", "receipt")
    return paths


class V4Writers(V3Writers):
    def __init__(self, request, stage_root, cell_order, inputs):
        super().__init__(request, stage_root, cell_order)
        self._inputs = inputs
        self._fragment_handles: dict[str, MatrixFragmentWriter] = {}

    def open_matrix_fragments(self, name: str) -> MatrixFragmentWriter:
        return MatrixFragmentWriter(self, name)


def _validate_provenance_snapshot(
    payload: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    declaration: Mapping[str, Any],
    input_descriptor: Mapping[str, Any],
    params: Mapping[str, Any],
) -> None:
    top_fields = {
        "schema",
        "attestation_name",
        "input_name",
        "input_descriptor_identity",
        "reference_kind",
        "artifact_id",
        "artifact_type",
        "artifact_lineage_status",
        "artifact_content_identity",
        "source_run_id",
        "source_run_identity",
        "source_snapshot_id",
        "source_snapshot",
        "source_output_name",
        "source_output_content_identity",
        "lifecycle",
        "snapshot_identity",
    }
    head_fields = {
        "snapshot_id",
        "schema_version",
        "head_run_ids",
        "n_runs",
        "n_artifacts",
        "commit_id",
        "replay_status",
        "source_snapshot_identity",
    }
    lifecycle_fields = {
        "state",
        "identity",
        "provenance_bound",
        "incoming_supersedes",
        "outgoing_superseded_by",
        "tombstone_state",
    }
    edge_fields = {
        "successor_kind",
        "successor_id",
        "predecessor_kind",
        "predecessor_id",
        "relationship_type",
    }
    source_head = payload.get("source_snapshot")
    lifecycle = payload.get("lifecycle")
    if (
        set(payload) != top_fields
        or not isinstance(source_head, Mapping)
        or set(source_head) != head_fields
        or not isinstance(lifecycle, Mapping)
        or set(lifecycle) != lifecycle_fields
    ):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Artifact ancestry snapshot is not the closed canonical tuple",
            "resolution",
        )
    _assert_identity(
        source_head,
        "source_snapshot_identity",
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
        "resolution",
    )
    incoming = lifecycle["incoming_supersedes"]
    outgoing = lifecycle["outgoing_superseded_by"]
    if not isinstance(incoming, list) or not isinstance(outgoing, list):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Artifact ancestry lifecycle edges are malformed",
            "resolution",
        )
    for edges in (incoming, outgoing):
        if any(
            not isinstance(edge, Mapping)
            or set(edge) != edge_fields
            or any(not isinstance(edge[field], str) or not edge[field] for field in edge_fields)
            or edge["relationship_type"] != "SUPERSEDES"
            for edge in edges
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Artifact ancestry lifecycle edge is malformed",
                "resolution",
            )
        ordered = sorted(
            edges,
            key=lambda edge: tuple(
                edge[field].encode("utf-8")
                for field in (
                    "successor_kind",
                    "successor_id",
                    "predecessor_kind",
                    "predecessor_id",
                    "relationship_type",
                )
            ),
        )
        if list(edges) != ordered:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Artifact ancestry lifecycle edges are not canonically ordered",
                "resolution",
            )
    input_identity = _identity_projection(input_descriptor["descriptor_identity"])
    content_identity = input_descriptor["content_identity"]
    required_strings = (
        "artifact_id",
        "artifact_type",
        "source_run_id",
        "source_snapshot_id",
        "source_output_name",
    )
    if (
        payload["schema"] != "scworkbench.artifact_ancestry.snapshot.v1"
        or any(
            not isinstance(payload[field], str) or not payload[field] for field in required_strings
        )
        or payload["attestation_name"] != descriptor["name"]
        or payload["input_name"] != descriptor["input_name"]
        or payload["input_descriptor_identity"] != input_identity
        or payload["reference_kind"] != "promoted_artifact"
        or payload["artifact_type"] != input_descriptor["kind"]
        or payload["artifact_content_identity"] != content_identity
        or payload["source_output_content_identity"] != content_identity
        or payload["source_snapshot_id"] != source_head["snapshot_id"]
        or not isinstance(source_head["head_run_ids"], list)
        or any(not isinstance(run_id, str) or not run_id for run_id in source_head["head_run_ids"])
        or len(source_head["head_run_ids"]) != len(set(source_head["head_run_ids"]))
        or payload["source_run_id"] not in source_head["head_run_ids"]
        or not isinstance(source_head["n_runs"], (int, float))
        or isinstance(source_head["n_runs"], bool)
        or not math.isfinite(source_head["n_runs"])
        or source_head["n_runs"] != int(source_head["n_runs"])
        or source_head["n_runs"] < 0
        or not isinstance(source_head["n_artifacts"], (int, float))
        or isinstance(source_head["n_artifacts"], bool)
        or not math.isfinite(source_head["n_artifacts"])
        or source_head["n_artifacts"] != int(source_head["n_artifacts"])
        or source_head["n_artifacts"] < 0
        or lifecycle["state"] != "current"
        or lifecycle["provenance_bound"] != 1
        or outgoing
        or lifecycle["tombstone_state"] is not None
    ):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Artifact ancestry is stale, superseded, or differs from its typed input",
            "resolution",
        )
    for identity in (payload["source_run_identity"], lifecycle["identity"]):
        if (
            not isinstance(identity, Mapping)
            or not isinstance(identity.get("scheme"), str)
            or not identity["scheme"]
            or not isinstance(identity.get("sha256"), str)
            or len(identity["sha256"]) != 64
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Artifact ancestry contains an invalid durable identity",
                "resolution",
            )
    assertions = declaration.get("parameter_assertions", {})
    asserted = {
        "artifact_id": payload["artifact_id"],
        "run_id": payload["source_run_id"],
        "snapshot_id": payload["source_snapshot_id"],
    }
    if any(params.get(parameter) != asserted[field] for field, parameter in assertions.items()):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Artifact ancestry disagrees with a declared parameter assertion",
            "resolution",
        )


def _read_provenance(
    request: Mapping[str, Any], operation: Mapping[str, Any], root: Path
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    declarations = {item["name"]: item for item in operation.get("provenance_reads", [])}
    input_descriptors = {item["name"]: item["descriptor"] for item in request["inputs"]}
    for item in request.get("provenance_reads", []):
        name, descriptor = item["name"], item
        path = _safe_relative(root, f"provenance_reads/{name}/payload.json", must_exist=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                f"Provenance payload is unreadable: {exc}",
                "resolution",
            )
        _assert_identity(
            payload, "snapshot_identity", "PYBENCH_INPUT_IDENTITY_MISMATCH", "resolution"
        )
        manifest_identity = _identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            {
                "path": f"provenance_reads/{name}/payload.json",
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        )
        if (
            payload["snapshot_identity"] != descriptor["snapshot_identity"]
            or manifest_identity != descriptor["payload_manifest_identity"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Provenance snapshot or payload identity differs",
                "resolution",
            )
        declaration = declarations[name]
        _validate_provenance_snapshot(
            payload,
            descriptor,
            declaration,
            input_descriptors[declaration["input"]],
            request["params"],
        )
        result[name] = payload
    return result


def _python_runtime_sidecar_v4(
    discovery: PackDiscoveryV3, request: Mapping[str, Any]
) -> dict[str, Any]:
    seed = request["params"].get("seed")
    value = {
        "schema": "pybench.operation_pack.python_runtime.v4",
        "backend": "Python",
        "distribution_identity": discovery.distribution_identity,
        "request_identity": request["request_identity"],
        "capability_identity": request["execution_capability"]["capability_identity"],
        "operation_definition_identity": request["operation_authentication"][
            "operation_definition_identity"
        ],
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
    value["runtime_identity"] = _identity(
        "pybench.operation_pack.python_runtime.v4",
        value,
        "python runtime sidecar excluding runtime_identity",
    )
    return value


def validate_python_runtime_sidecar_v4(
    sidecar: Mapping[str, Any], request: Mapping[str, Any], discovery: PackDiscoveryV3
) -> dict[str, Any]:
    request = validate_request_v4(request, discovery)
    value = copy.deepcopy(dict(sidecar))
    _validate_schema(
        value,
        "pybench-op-v4-python-runtime.schema.json",
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
    )
    _no_absolute_paths(
        value, code="PYBENCH_RUNTIME_ATTESTATION_MISMATCH", phase="runtime_attestation"
    )
    _assert_identity(
        value, "runtime_identity", "PYBENCH_RUNTIME_ATTESTATION_MISMATCH", "runtime_attestation"
    )
    for key, expected in (
        ("distribution_identity", discovery.distribution_identity),
        ("request_identity", request["request_identity"]),
        ("capability_identity", request["execution_capability"]["capability_identity"]),
        (
            "operation_definition_identity",
            request["operation_authentication"]["operation_definition_identity"],
        ),
    ):
        if value[key] != expected:
            _fail(
                "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
                f"Runtime sidecar {key} differs",
                "runtime_attestation",
            )
    return value


def _shared_runtime_v3(
    discovery: PackDiscoveryV3,
    operation: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schema": "scworkbench.operation_pack.runtime.v3",
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
        "scworkbench.operation_pack.runtime.v3",
        value,
        "runtime attestation excluding runtime_identity",
    )
    return value


def _observed_descriptor_v4(
    grant: Mapping[str, Any],
    write: _Write,
    cell_order: list[str],
    inputs: Mapping[str, Mapping[str, Any]],
    registry_values: Mapping[str, Mapping[str, Any]] | None = None,
    parameter_axis_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    inherited_grant = grant
    cardinality = grant["descriptor"].get("cardinality_grant")
    if cardinality is not None and cardinality["shape"] == "bounded_grouped_cell_key_cartesian":
        inherited_grant = copy.deepcopy(dict(grant))
        inherited_grant["descriptor"]["expected_dimensions"] = [
            len(write.value.get("value", [])),
            3,
        ]
        inherited_grant["descriptor"]["expected_cell_order_identity"] = _identity(
            "scworkbench.cell_order.v1", _ordered(write.value, "cell_id")
        )
    elif grant["descriptor"].get("parameter_axis_grant_identity") is not None and grant[
        "descriptor"
    ]["kind"] in {"feature_table", "obs_table"}:
        inherited_grant = copy.deepcopy(dict(grant))
        inherited_grant["descriptor"]["expected_dimensions"] = list(
            write.value.get("dimensions", [])
        )
        if grant["descriptor"]["kind"] == "feature_table":
            inherited_grant["descriptor"]["expected_feature_order_identity"] = _identity(
                "scworkbench.feature_order.v1", _ordered(write.value, "feature_id")
            )
    observed = _observed_descriptor(inherited_grant, write, cell_order)
    if grant["descriptor"]["category"] == "core_registry_request":
        return observed
    observed.pop("observed_descriptor_identity")
    fragment_manifest = write.value.get("matrix_fragment_manifest")
    if fragment_manifest is not None:
        observed["matrix_fragment_manifest"] = copy.deepcopy(fragment_manifest)
        observed["matrix_fragment_manifest_identity"] = plain(
            fragment_manifest["fragment_manifest_identity"]
        )
        observed["content_identity"] = _identity(
            "scworkbench.matrix.content.v1",
            {
                "dimensions": fragment_manifest["dimensions"],
                "feature_order_identity": fragment_manifest["feature_order_identity"],
                "cell_order_identity": fragment_manifest["cell_order_identity"],
                "fragment_content_identities": [
                    item["content_identity"] for item in fragment_manifest["fragments"]
                ],
            },
        )
        observed["payload_manifest_identity"] = _identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            {
                "fragment_manifest_identity": fragment_manifest["fragment_manifest_identity"],
                "fragment_payload_identities": [
                    item["payload_manifest_identity"] for item in fragment_manifest["fragments"]
                ],
            },
        )
    if cardinality is not None:
        observed["observed_cardinality"] = _validate_cardinality_payload(
            cardinality,
            write.value,
            inputs,
            grant["descriptor"].get("semantic_constraints"),
            registry_values,
        )
    if parameter_axis_observation is not None:
        observed["observed_parameter_axis"] = copy.deepcopy(dict(parameter_axis_observation))
    observed["observed_descriptor_identity"] = _identity(
        "scworkbench.operation_pack.observed_output.v3",
        observed,
        "descriptor excluding observed_descriptor_identity",
    )
    return observed


def _provenance_observations(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in request.get("provenance_reads", []):
        descriptor = item
        observed = {
            "schema": "scworkbench.operation_pack.observed_provenance_read.v1",
            "name": item["name"],
            "descriptor_identity": _identity_projection(descriptor["descriptor_identity"]),
            "snapshot_identity": _identity_projection(descriptor["snapshot_identity"]),
            "payload_manifest_identity": descriptor["payload_manifest_identity"],
            "use_count": 1,
        }
        observed["observed_descriptor_identity"] = _identity(
            "scworkbench.operation_pack.observed_provenance_read.v1",
            observed,
            "observation excluding observed_descriptor_identity",
        )
        result.append(observed)
    return result


def _registry_observations(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in request.get("registry_reads", []):
        source = item["descriptor"]
        observed = {
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
        observed["use_count"] = 1
        observed["observed_descriptor_identity"] = _identity(
            "scworkbench.operation_pack.observed_registry_read.v2",
            observed,
            "descriptor excluding observed_descriptor_identity",
        )
        result.append({"name": item["name"], "descriptor": observed})
    return result


def _read_inputs_v4(request: Mapping[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    typed_inputs = {item["input_name"] for item in request.get("typed_slice_grants", [])}
    for name in typed_inputs:
        eager = _safe_relative(root, f"inputs/{name}/payload.json")
        if eager.exists():
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Broker-enabled matrix was eagerly staged across the process boundary",
                "staging",
            )
    projected = copy.deepcopy(dict(request))
    projected["inputs"] = [item for item in request["inputs"] if item["name"] not in typed_inputs]
    return _read_inputs(projected, root)


def _typed_slice_clients(
    request: Mapping[str, Any], root: Path
) -> dict[str, TypedSliceBrokerClient]:
    return {
        grant["name"]: TypedSliceBrokerClient(request, grant, root)
        for grant in request.get("typed_slice_grants", [])
    }


def execute_operation_pack_v4(
    request: Mapping[str, Any],
    discovery: PackDiscoveryV3,
    stage_root: str | os.PathLike[str],
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = validate_request_v4(request, discovery)
    root = _safe_root(Path(stage_root))
    before = _snapshot(root)
    tree_before = _tree(root)
    operation = _operation(discovery, request["operation_authentication"]["name"])
    active = active_operation_packs_v3().get(discovery.manifest["namespace"])
    if active is None or not _identity_equal(active[0].identity, discovery.identity):
        _fail("OPERATION_PACK_REQUIRED", "Exact operation pack is not active", "resolution")
    capability_id = request["execution_capability"]["capability_identity"]["sha256"]
    if capability_id in _CONSUMED_CAPABILITIES:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Capability is already consumed", "staging")
    _CONSUMED_CAPABILITIES.add(capability_id)
    writers: V4Writers | None = None
    try:
        if fault_hook:
            fault_hook("before_input_read")
        inputs = _read_inputs_v4(request, root)
        registry_values = _read_registry_reads(request, operation, root)
        provenance_values = _read_provenance(request, operation, root)
        preflight = preflight_v3(operation, inputs, request["params"], request)
        slice_clients = _typed_slice_clients(request, root)
        slice_inputs = {
            grant["input_name"]: {
                "kind": "matrix",
                "dimensions": [
                    grant["features_axis"]["count"],
                    grant["cells_axis"]["count"],
                ],
                "feature_id": slice_clients[grant["name"]].selected_features,
                "cell_id": slice_clients[grant["name"]].selected_cells,
            }
            for grant in request.get("typed_slice_grants", [])
        }
        audit_inputs = {**inputs, **slice_inputs}
        cell_order = (
            list(next(iter(audit_inputs.values())).get("cell_id", [])) if audit_inputs else []
        )
        writers = V4Writers(request, root, cell_order, audit_inputs)
        readers = V4Readers(inputs, registry_values, provenance_values, slice_clients)
        if fault_hook:
            fault_hook("before_dispatch")
        context = {
            "transition_id": request["transition"]["transition_id"],
            "capability_id": request["execution_capability"]["capability_id"],
            "typed_slice_reads": {
                grant["name"]: {
                    "name": grant["name"],
                    "input_name": grant["input_name"],
                    "chunk_axis": grant["chunk_axis"],
                    "requested_chunk_size": grant["requested_chunk_size"],
                    "call_discipline": grant["call_discipline"],
                    "cells": slice_clients[grant["name"]].selected_cells,
                    "features": slice_clients[grant["name"]].selected_features,
                }
                for grant in request.get("typed_slice_grants", [])
            },
        }
        active[1].handlers[operation["qualified_name"]](
            readers,
            writers,
            copy.deepcopy(request["params"]),
            context,
        )
        if sorted(readers.registry_calls) != sorted(
            item["name"] for item in request.get("registry_reads", [])
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Registry readers must be consumed exactly once",
                "execution",
            )
        if sorted(readers.provenance_calls) != sorted(
            item["name"] for item in request.get("provenance_reads", [])
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Provenance readers must be consumed exactly once",
                "execution",
            )
        typed_slice_usage = [
            slice_clients[grant["name"]].usage() for grant in request.get("typed_slice_grants", [])
        ]
        if fault_hook:
            fault_hook("after_handler")
        writes = writers.writes
        expected_names = [item["name"] for item in request["outputs"]]
        if set(writes) != set(expected_names) or len(writes) != len(expected_names):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Returned output closure differs", "receipt")
        parameter_axis_observations = validate_axis_bound_outputs(
            operation, request, writes, audit_inputs
        )
        writers.consume()
        outputs: list[dict[str, Any]] = []
        registry_requests: list[dict[str, Any]] = []
        entries: list[dict[str, str]] = []
        for grant in request["outputs"]:
            write = writes[grant["name"]]
            try:
                staged_value = json.loads(write.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                _fail("PYBENCH_OUTPUT_INVALID", f"Staged output is unreadable: {exc}", "audit")
            expected_staged = write.value
            if canonical_json(staged_value) != canonical_json(expected_staged):
                _fail(
                    "PYBENCH_OUT_OF_SCOPE_MUTATION",
                    "Staged payload changed after writer return",
                    "audit",
                )
            descriptor = _observed_descriptor_v4(
                grant,
                write,
                cell_order,
                audit_inputs,
                registry_values,
                parameter_axis_observations.get(grant["name"]),
            )
            record = {
                "name": grant["name"],
                "allocated_id": grant["allocated_id"],
                "descriptor": descriptor,
            }
            target = (
                registry_requests
                if grant["descriptor"]["category"] == "core_registry_request"
                else outputs
            )
            target.append(record)
            for path in write.paths or [write.path]:
                entries.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        after = _snapshot(root)
        tree_after = _tree(root)
        changed = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
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
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA_V4,
            "message_type": "receipt",
            "process_protocol_version": PROCESS_PROTOCOL_V4,
            "pack_abi_version": "3",
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
            "runtime_attestation": _shared_runtime_v3(discovery, operation, preflight),
        }
        if request.get("registry_reads"):
            receipt["registry_reads"] = _registry_observations(request)
        if request.get("provenance_reads"):
            receipt["provenance_reads"] = _provenance_observations(request)
        if typed_slice_usage:
            receipt["typed_slice_usage"] = typed_slice_usage
        publication = publication_observations(request, outputs)
        if publication:
            receipt["publication_observations"] = publication
        receipt["receipt_identity"] = _identity(
            RECEIPT_SCHEMA_V4, receipt, "receipt excluding receipt_identity"
        )
        validate_receipt_v4(receipt, request, discovery, root)
        sidecar = _python_runtime_sidecar_v4(discovery, request)
        validate_python_runtime_sidecar_v4(sidecar, request, discovery)
        return receipt, sidecar
    except BaseException:
        if writers is not None:
            writers.consume()
        _restore(root, before)
        raise


def _validate_typed_slice_usage(
    request: Mapping[str, Any], usage_values: list[Mapping[str, Any]]
) -> None:
    grants = request.get("typed_slice_grants", [])
    if len(usage_values) != len(grants):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Typed-slice usage closure differs", "receipt")
    for usage, grant in zip(usage_values, grants, strict=True):
        _assert_identity(usage, "usage_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt")
        calls = usage["calls"]
        if usage["call_count"] != len(calls):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Typed-slice call count differs", "receipt")
        for ordinal, call in enumerate(calls, start=1):
            _assert_identity(call, "call_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt")
            result = {
                key: copy.deepcopy(value)
                for key, value in call.items()
                if key not in {"schema", "call_identity"}
            }
            result["schema"] = "scworkbench.operation_pack.typed_slice_result.v1"
            result["source_reference_identity"] = _identity_projection(
                grant["source_reference"]["source_reference_identity"]
            )
            ordered_result = {
                "schema": result.pop("schema"),
                "source_reference_identity": result.pop("source_reference_identity"),
                **result,
            }
            expected_slice_identity = _identity(
                "scworkbench.operation_pack.typed_slice_result.v1",
                {
                    key: value
                    for key, value in ordered_result.items()
                    if key != "slice_result_identity"
                },
                "slice result excluding slice_result_identity",
            )
            if (
                call["ordinal"] != ordinal
                or call["grant_identity"] != _identity_projection(grant["grant_identity"])
                or call["dimensions"] != [call["feature_count"], call["cell_count"]]
                or call["feature_count"] > grant["max_rows"]
                or call["cell_count"] > grant["max_columns"]
                or call["feature_count"] * call["cell_count"] > grant["max_elements"]
                or call["slice_result_identity"] != expected_slice_identity
            ):
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Typed-slice call result or resource binding differs",
                    "receipt",
                )
        chunk_axis = grant["chunk_axis"]
        selected_count = grant[f"{chunk_axis}_axis"]["count"]
        selected_identity = grant[f"{chunk_axis}_axis"]["order_identity"]
        empty_selection = not grant["cells_axis"]["count"] or not grant["features_axis"]["count"]
        bindings = (
            usage["request_identity"] == _identity_projection(request["request_identity"]),
            usage["capability_identity"]
            == _identity_projection(request["execution_capability"]["capability_identity"]),
            usage["operation_definition_identity"]
            == _identity_projection(
                request["operation_authentication"]["operation_definition_identity"]
            ),
            usage["grant_identity"] == _identity_projection(grant["grant_identity"]),
            usage["requested_chunk_size"] == grant["requested_chunk_size"],
            0
            <= usage["unique_chunk_axis_count"]
            <= sum(
                call["cell_count"] if chunk_axis == "cells" else call["feature_count"]
                for call in calls
            ),
            usage["duplicate_chunk_axis_count"]
            == sum(
                call["cell_count"] if chunk_axis == "cells" else call["feature_count"]
                for call in calls
            )
            - usage["unique_chunk_axis_count"],
        )
        if not all(bindings):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Typed-slice usage binding differs", "receipt")
        if empty_selection:
            empty_scheme = (
                "scworkbench.cell_order.v1"
                if chunk_axis == "cells"
                else "scworkbench.feature_order.v1"
            )
            if (
                usage["coverage_status"] != "empty"
                or calls
                or usage["unique_chunk_axis_count"] != 0
                or usage["duplicate_chunk_axis_count"] != 0
                or usage["concatenated_chunk_axis_order_identity"] != _identity(empty_scheme, [])
            ):
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Empty typed-slice usage differs from its grant",
                    "receipt",
                )
        elif grant["call_discipline"] == "ordered_partition_exactly_once":
            if selected_count:
                expected_coverage = "exact"
                exact = (
                    usage["unique_chunk_axis_count"] == selected_count
                    and usage["duplicate_chunk_axis_count"] == 0
                    and usage["concatenated_chunk_axis_order_identity"] == selected_identity
                )
            else:
                expected_coverage = "empty"
                exact = not calls and usage["unique_chunk_axis_count"] == 0
            if usage["coverage_status"] != expected_coverage or not exact:
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Typed-slice ordered coverage differs from its grant",
                    "receipt",
                )
        elif usage["coverage_status"] != "independent":
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Typed-slice independent coverage status differs",
                "receipt",
            )


def validate_receipt_v4(
    receipt: Mapping[str, Any],
    request: Mapping[str, Any],
    discovery: PackDiscoveryV3,
    stage_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    request = validate_request_v4(request, discovery)
    value = copy.deepcopy(dict(receipt))
    if (
        value.get("schema") != RECEIPT_SCHEMA_V4
        or value.get("pack_abi_version") != "3"
        or value.get("process_protocol_version") != PROCESS_PROTOCOL_V4
    ):
        _fail("OPERATION_PACK_PROTOCOL_INCOMPATIBLE", "Receipt protocol tuple differs", "receipt")
    _validate_schema(value, _RECEIPT_ASSET, "PYBENCH_RECEIPT_MISMATCH", "receipt")
    _no_absolute_paths(value)
    _assert_identity(value, "receipt_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt")
    for left, right in (
        (value["transition_id"], request["transition"]["transition_id"]),
        (value["session_id"], request["transition"]["session_id"]),
        (value["run_id"], request["transition"]["run_id"]),
        (value["transition_identity"], request["transition"]["transition_identity"]),
        (value["request_identity"], request["request_identity"]),
        (value["capability_identity"], request["execution_capability"]["capability_identity"]),
    ):
        if left != right:
            _fail("PYBENCH_RECEIPT_MISMATCH", "Receipt binding differs from request", "receipt")
    if value["status"] != "ok":
        return value
    observed = value["outputs"] + value["registry_requests"]
    observed_keys = [(item["name"], item["allocated_id"]) for item in observed]
    granted_keys = [(item["name"], item["allocated_id"]) for item in request["outputs"]]
    if sorted(observed_keys) != sorted(granted_keys) or len(observed_keys) != len(
        set(observed_keys)
    ):
        _fail(
            "PYBENCH_RECEIPT_MISMATCH", "Receipt is not a bijection with output grants", "receipt"
        )
    for item in observed:
        descriptor = item["descriptor"]
        if "observed_cardinality" in descriptor:
            _assert_identity(
                descriptor["observed_cardinality"],
                "observed_cardinality_identity",
                "PYBENCH_RECEIPT_MISMATCH",
                "receipt",
            )
        _assert_identity(
            descriptor, "observed_descriptor_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt"
        )
    expected_provenance = _provenance_observations(request)
    if value.get("provenance_reads", []) != expected_provenance:
        _fail("PYBENCH_RECEIPT_MISMATCH", "Provenance read receipt differs", "receipt")
    if value.get("registry_reads", []) != _registry_observations(request):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Registry read receipt differs", "receipt")
    _validate_typed_slice_usage(request, value.get("typed_slice_usage", []))
    if value.get("publication_observations", []) != publication_observations(
        request, value["outputs"]
    ):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Publication observation differs", "receipt")
    if stage_root is not None:
        root = _safe_root(Path(stage_root))
        operation = _operation(discovery, request["operation_authentication"]["name"])
        inputs = _read_inputs_v4(request, root)
        registry_values = _read_registry_reads(request, operation, root)
        for grant in request.get("typed_slice_grants", []):
            inputs[grant["input_name"]] = {
                "kind": "matrix",
                "dimensions": [grant["features_axis"]["count"], grant["cells_axis"]["count"]],
                "feature_id": _axis_manifest(root, grant["features_axis"], "features")["ids"],
                "cell_id": _axis_manifest(root, grant["cells_axis"], "cells")["ids"],
            }
        cell_order = list(next(iter(inputs.values())).get("cell_id", [])) if inputs else []
        by_name = {item["name"]: item for item in observed}
        stage_writes: dict[str, _Write] = {}
        for grant in request["outputs"]:
            default_root = (
                f"registry_requests/{grant['name']}"
                if grant["descriptor"]["category"] == "core_registry_request"
                else f"outputs/{grant['name']}"
            )
            relative_root = grant["descriptor"].get("relative_root", default_root)
            fragment_grant = grant["descriptor"].get("matrix_fragment_grant")
            if fragment_grant is None:
                path = _safe_relative(root, relative_root + "/payload.json", must_exist=True)
                payload = json.loads(path.read_text(encoding="utf-8"))
                write = _Write(payload, path, {}, grant["descriptor"]["writer"])
            else:
                path = _safe_relative(root, relative_root + "/payload.json", must_exist=True)
                payload = json.loads(path.read_text(encoding="utf-8"))
                paths = _validate_matrix_fragment_stage(
                    root,
                    grant["name"],
                    payload,
                    fragment_grant,
                ) + [path]
                write = _Write(
                    payload,
                    path,
                    dict(payload.get("meta", {})),
                    grant["descriptor"]["writer"],
                    paths,
                )
            stage_writes[grant["name"]] = write
        expected_axis_observations = validate_axis_bound_outputs(
            operation, request, stage_writes, inputs
        )
        for grant in request["outputs"]:
            write = stage_writes[grant["name"]]
            expected_descriptor = _observed_descriptor_v4(
                grant,
                write,
                cell_order,
                inputs,
                registry_values,
                expected_axis_observations.get(grant["name"]),
            )
            if by_name[grant["name"]]["descriptor"] != expected_descriptor:
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Output receipt differs from staged payload",
                    "receipt",
                )
    return value


def error_receipt_v4(
    request: Mapping[str, Any],
    error: BaseException,
    runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    code = error.code if isinstance(error, OperationPackError) else "PYBENCH_PROCESS_FAILED"
    phase = (
        error.details.get("phase", "execution")
        if isinstance(error, OperationPackError)
        else "execution"
    )
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA_V4,
        "message_type": "receipt",
        "process_protocol_version": PROCESS_PROTOCOL_V4,
        "pack_abi_version": "3",
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
        RECEIPT_SCHEMA_V4, receipt, "receipt excluding receipt_identity"
    )
    return receipt
