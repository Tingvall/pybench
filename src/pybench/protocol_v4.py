from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
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
from .protocol_v3 import (
    V3Readers,
    V3Writers,
    _dimensions,
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


def _operation(discovery: PackDiscoveryV3, name: str) -> Mapping[str, Any]:
    value = next(
        (item for item in discovery.manifest["operations"] if item["qualified_name"] == name),
        None,
    )
    if value is None:
        _fail("OPERATION_PACK_REQUIRED", f"Operation {name!r} is absent", "resolution")
    return value


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
    for schedule in value.get("chunk_schedules", []):
        for batch in schedule["batches"]:
            _assert_identity(
                batch["fragment_grant"],
                "fragment_grant_identity",
                "PYBENCH_CAPABILITY_MISMATCH",
                "staging",
            )
        _assert_identity(
            schedule["summary_finalizer_definition"],
            "finalizer_definition_identity",
            "PYBENCH_CAPABILITY_MISMATCH",
            "staging",
        )
        _assert_identity(schedule, "schedule_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
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
        if (
            descriptor["matrix_input"] != declaration["matrix_input"]
            or descriptor["selector_binding"] != selector_binding
            or descriptor["matrix_content_identity"] != matrix["content_identity"]
            or descriptor["feature_order_identity"] != matrix["feature_order_identity"]
        ):
            _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Registry read binding differs", "resolution")

    output_names = [item["name"] for item in value["outputs"]]
    if output_names != [item["name"] for item in operation["outputs"]]:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Request output closure differs", "staging")
    policies = {item["output"]: item for item in operation.get("dimension_policies", [])}
    outputs = {item["name"]: item for item in operation["outputs"]}
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
            if any(
                grant[key] != policy[key]
                for key in ("output", "policy", "shape", "row_order", "missing_values")
            ):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Output cardinality grant differs from manifest",
                    "staging",
                )
            expected_dimensions = [grant["expected_row_count"]]
            if grant["shape"] == "cell_key_cartesian":
                expected_dimensions.append(3)
            if descriptor["expected_dimensions"] != expected_dimensions:
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Granted dimensions differ from cardinality",
                    "staging",
                )

    expected_schedules = 1 if operation.get("chunking", {}).get("axis") == "cells" else 0
    schedules = value.get("chunk_schedules", [])
    if len(schedules) != expected_schedules:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Cell chunk schedule closure differs", "staging")
    if schedules:
        _validate_schedule(schedules[0], operation, value)

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
    expected_readers = [
        {
            "input_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in value["inputs"]
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
        capability.get("chunk_schedule_identities", [])
        == [_identity_projection(item["schedule_identity"]) for item in schedules],
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


def _validate_schedule(
    schedule: Mapping[str, Any], operation: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    declaration = operation["chunking"]
    if (
        schedule["input_name"] != declaration["input"]
        or schedule["axis"] != "cells"
        or schedule["merge_policy"] != declaration["merge"]
        or schedule["fragment_output"] != declaration["fragment_output"]
        or schedule["summary_output"] != declaration["summary_output"]
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Cell chunk schedule differs from manifest", "staging")
    input_record = next(item for item in request["inputs"] if item["name"] == declaration["input"])
    if schedule["input_descriptor_identity"] != _identity_projection(
        input_record["descriptor"]["descriptor_identity"]
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk input descriptor differs", "staging")
    output_records = {item["name"]: item for item in request["outputs"]}
    cardinality = output_records[declaration["fragment_output"]]["descriptor"].get(
        "cardinality_grant"
    )
    finalizer_definition = {
        "schema": "scworkbench.operation_pack.summary_finalizer_definition.v1",
        "interface": declaration["summary"]["interface"],
        "callable": declaration["summary"]["callable"],
        "isolation": declaration["summary"]["isolation"],
        "merged_input": declaration["fragment_output"],
        "summary_output": declaration["summary_output"],
        "metadata_sources": declaration["summary"]["metadata_sources"],
    }
    finalizer_definition["finalizer_definition_identity"] = _identity(
        "scworkbench.operation_pack.summary_finalizer_definition.v1",
        finalizer_definition,
        "definition excluding finalizer_definition_identity",
    )
    requested_size = request["params"].get(declaration["size_parameter"])
    if (
        cardinality is None
        or schedule["requested_chunk_size"] != requested_size
        or schedule["summary_finalizer_definition"] != finalizer_definition
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk schedule binding differs", "staging")
    prior_end = 0
    for expected_index, batch in enumerate(schedule["batches"], start=1):
        if (
            batch["index"] != expected_index
            or batch["start"] != prior_end + 1
            or batch["length"] < 1
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Chunk ranges are not contiguous 1-based ranges",
                "staging",
            )
        grant = batch["fragment_grant"]
        if (
            grant["batch_index"] != batch["index"]
            or grant["batch_start"] != batch["start"]
            or grant["batch_length"] != batch["length"]
            or grant["batch_cell_order_identity"] != batch["cell_order_identity"]
            or grant["fragment_output"] != declaration["fragment_output"]
            or grant["parent_cardinality_grant_identity"]
            != cardinality["cardinality_grant_identity"]
            or grant["input_descriptor_identity"]
            != _identity_projection(input_record["descriptor"]["descriptor_identity"])
            or grant["key_axis"] != cardinality["key_axis"]
            or grant["key_axis_count"] != cardinality["key_axis_count"]
            or grant["key_axis_order_identity"] != cardinality["key_axis_order_identity"]
            or grant["expected_row_count"] != batch["length"] * cardinality["key_axis_count"]
            or grant["missing_values"] != cardinality["missing_values"]
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Fragment grant differs from enclosing batch",
                "staging",
            )
        prior_end += batch["length"]
    if prior_end != schedule["axis_count"]:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk schedule does not cover its axis", "staging")


def _distinct(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _row_pairs(keys: list[str], cells: list[str]) -> list[dict[str, str]]:
    return [{"key": key, "cell_id": cell} for key in keys for cell in cells]


def _validate_cardinality_grant(
    grant: Mapping[str, Any], inputs: Mapping[str, Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    source = grant["cell_axis_source"]
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
) -> dict[str, Any]:
    cells, keys = _validate_cardinality_grant(grant, inputs)
    payload_cells = _ordered(payload, "cell_id")
    values = payload.get("value")
    if not isinstance(values, list) or len(values) != len(payload_cells):
        _fail("PYBENCH_OUTPUT_INVALID", "Cardinality output values are malformed", "audit")
    expected_cells = cells
    expected_keys: list[str] | None = None
    if grant["shape"] == "cell_key_cartesian":
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
        observed["observed_key_axis_order_identity"] = _identity("scworkbench.row_order.v1", keys)
        rows: Any = _row_pairs(keys, cells)
    else:
        rows = cells
    observed.update(
        observed_row_order_identity=_identity("scworkbench.row_order.v1", rows),
        missing_count=missing,
        semantic_validation="passed",
    )
    observed["observed_cardinality_identity"] = _identity(
        "scworkbench.operation_pack.observed_output_cardinality.v1",
        observed,
        "observation excluding observed_cardinality_identity",
    )
    return observed


class V4Readers(V3Readers):
    def __init__(self, values, registry_values=None, provenance_values=None):
        super().__init__(values, registry_values)
        self._provenance_values = dict(provenance_values or {})
        self._provenance_calls: list[str] = []
        self._scheduled_input: str | None = None
        self._scheduled_cells: list[str] | None = None

    def set_scheduled_cells(self, input_name: str, cells: list[str]) -> None:
        self._scheduled_input = input_name
        self._scheduled_cells = list(cells)

    @property
    def provenance_calls(self) -> list[str]:
        return list(self._provenance_calls)

    def __getitem__(self, key):
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
        reader = super().__getitem__(key)
        if key != self._scheduled_input:
            return reader

        def read_scheduled_matrix(*, features=None, cells=None):
            expected = self._scheduled_cells
            if expected is None or (cells is not None and list(cells) != expected):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Matrix read differs from the active scheduled cell slice",
                    "execution",
                )
            return reader(features=features, cells=expected)

        return read_scheduled_matrix

    def __iter__(self):
        return iter((*super().__iter__(), *self._provenance_values))

    def __len__(self):
        return super().__len__() + len(self._provenance_values)


class V4Writers(V3Writers):
    def __init__(self, request, stage_root, cell_order, inputs):
        super().__init__(request, stage_root, cell_order)
        self._inputs = inputs
        self._schedule = request.get("chunk_schedules", [None])[0]
        self._fragments: dict[int, _Write] = {}
        self._adapter_write = False

    @property
    def fragments(self) -> Mapping[int, _Write]:
        return dict(self._fragments)

    def _write(self, writer, name, value, semantic_metadata=None):
        if (
            self._schedule
            and not self._adapter_write
            and name in {self._schedule["fragment_output"], self._schedule["summary_output"]}
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH", "Cell-chunk terminals are adapter-owned", "execution"
            )
        return super()._write(writer, name, value, semantic_metadata)

    def write_chunk(self, name, index, value):
        if self._consumed or self._schedule is None or name != self._schedule["fragment_output"]:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk writer is not granted", "execution")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or index > len(self._schedule["batches"])
            or index in self._fragments
        ):
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Chunk index is invalid or repeated", "execution")
        batch = self._schedule["batches"][index - 1]
        payload = copy.deepcopy(dict(value))
        grant = batch["fragment_grant"]
        _validate_fragment(grant, payload, self._inputs)
        root = _safe_relative(self._stage_root, f"chunk_fragments/{name}/{index}")
        root.mkdir(parents=True)
        path = root / "payload.json"
        path.write_text(canonical_json(payload), encoding="utf-8")
        self._fragments[index] = _Write(payload, path, {}, "write_chunk")
        return {
            "name": name,
            "index": index,
            "fragment_grant_identity": grant["fragment_grant_identity"],
        }

    def stage_adapter_output(self, name, value):
        self._adapter_write = True
        try:
            return super()._write("write_terminal_primitive", name, value, None)
        finally:
            self._adapter_write = False


def _validate_fragment(
    grant: Mapping[str, Any],
    payload: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    del inputs  # parent schedule validation binds the fragment to the input
    cells = _ordered(payload, "cell_id")
    keys = list(grant["key_axis"])
    expected_keys = [key for key in keys for _ in range(grant["batch_length"])]
    if (
        payload.get("kind") != "cell_vector_family"
        or _dimensions(payload) != [grant["expected_row_count"], 3]
        or len(cells) != grant["expected_row_count"]
        or _ordered(payload, "key") != expected_keys
        or _identity("scworkbench.cell_order.v1", _distinct(cells))
        != grant["batch_cell_order_identity"]
        or _identity(
            "scworkbench.row_order.v1",
            [{"key": key, "cell_id": cell} for key, cell in zip(expected_keys, cells, strict=True)],
        )
        != grant["row_order_identity"]
    ):
        _fail("PYBENCH_OUTPUT_INVALID", "Chunk fragment differs from its exact grant", "execution")
    values = payload.get("value")
    if not isinstance(values, list) or len(values) != len(cells):
        _fail("PYBENCH_OUTPUT_INVALID", "Chunk values are malformed", "execution")
    missing = sum(value is None for value in values)
    if grant["missing_values"] == "forbidden" and missing:
        _fail("PYBENCH_OUTPUT_INVALID", "Chunk has forbidden missing values", "execution")
    return {"cells": _distinct(cells), "keys": keys, "missing": missing}


def _merge_fragments(
    schedule: Mapping[str, Any], fragments: Mapping[int, _Write]
) -> dict[str, Any]:
    if sorted(fragments) != list(range(1, len(schedule["batches"]) + 1)):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Chunk fragment closure differs from schedule", "receipt")
    keys = list(schedule["batches"][0]["fragment_grant"]["key_axis"]) if schedule["batches"] else []
    merged = {
        "kind": "cell_vector_family",
        "dimensions": [0, 3],
        "cell_id": [],
        "key": [],
        "value": [],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }
    for key in keys:
        for index in sorted(fragments):
            value = fragments[index].value
            for row, observed_key in enumerate(value["key"]):
                if observed_key == key:
                    merged["key"].append(key)
                    merged["cell_id"].append(value["cell_id"][row])
                    merged["value"].append(value["value"][row])
    merged["dimensions"][0] = len(merged["value"])
    return merged


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
) -> dict[str, Any]:
    observed = _observed_descriptor(grant, write, cell_order)
    if grant["descriptor"]["category"] == "core_registry_request":
        return observed
    observed.pop("observed_descriptor_identity")
    cardinality = grant["descriptor"].get("cardinality_grant")
    if cardinality is not None:
        observed["observed_cardinality"] = _validate_cardinality_payload(
            cardinality,
            write.value,
            inputs,
            grant["descriptor"].get("semantic_constraints"),
        )
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
            "descriptor_identity": descriptor["descriptor_identity"],
            "snapshot_identity": descriptor["snapshot_identity"],
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


def _finalizer_metadata(
    operation: Mapping[str, Any],
    request: Mapping[str, Any],
    schedule: Mapping[str, Any],
) -> dict[str, Any]:
    input_records = {item["name"]: item for item in request["inputs"]}
    output_records = {item["name"]: item for item in request["outputs"]}
    fragment_grant = output_records[schedule["fragment_output"]]["descriptor"]["cardinality_grant"]
    values: list[dict[str, Any]] = []
    for source in operation["chunking"]["summary"]["metadata_sources"]:
        kind = source["source"]
        if kind == "resolved_key_axis":
            value = fragment_grant["key_axis_resolution"]
        elif kind == "normalized_parameter":
            value = request["params"][source["parameter"]]
        elif kind == "input_reference":
            value = input_records[source["input"]]["reference"]
        elif kind == "input_descriptor":
            value = input_records[source["input"]]["descriptor"]
        else:  # manifest admission makes this unreachable
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Unknown finalizer metadata source", "execution")
        values.append({"name": source["name"], "value": copy.deepcopy(value)})
    metadata = {
        "schema": "scworkbench.operation_pack.summary_finalizer_metadata.v1",
        "finalizer_definition_identity": schedule["summary_finalizer_definition"][
            "finalizer_definition_identity"
        ],
        "values": values,
    }
    metadata["finalizer_metadata_identity"] = _identity(
        "scworkbench.operation_pack.summary_finalizer_metadata.v1",
        metadata,
        "metadata excluding finalizer_metadata_identity",
    )
    return metadata


def _invoke_finalizer_isolated(
    discovery: PackDiscoveryV3,
    callable_name: str,
    merged_family: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = Path(
        discovery.distribution.locate_file(discovery.identity["manifest_relative_path"])
    ).resolve(strict=True)
    command = [
        sys.executable,
        "-m",
        "pybench.summary_finalizer_worker",
        str(manifest),
        callable_name,
    ]
    completed = subprocess.run(
        command,
        input=canonical_json({"merged_family": dict(merged_family), "metadata": dict(metadata)}),
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        _fail(
            "PYBENCH_PROCESS_FAILED",
            "Pure summary finalizer failed in its isolated invocation",
            "execution",
            finalizer_exit_code=completed.returncode,
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        _fail(
            "PYBENCH_OUTPUT_INVALID",
            "Pure summary finalizer returned invalid JSON",
            "execution",
        )
    if not isinstance(value, dict):
        _fail(
            "PYBENCH_OUTPUT_INVALID",
            "Pure summary finalizer did not return one primitive",
            "execution",
        )
    return value


def _chunk_execution(
    request: Mapping[str, Any],
    operation: Mapping[str, Any],
    discovery: PackDiscoveryV3,
    readers: V4Readers,
    writers: V4Writers,
    inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    schedules = request.get("chunk_schedules", [])
    if not schedules:
        return None
    schedule = schedules[0]
    source = inputs[schedule["input_name"]]
    source_cells = _ordered(source, "cell_id")
    requested_size = schedule["requested_chunk_size"]
    expected_effective = (
        0
        if not source_cells
        else len(source_cells)
        if requested_size is None
        else min(requested_size, len(source_cells))
    )
    if (
        schedule["axis_count"] != len(source_cells)
        or schedule["axis_order_identity"] != _identity("scworkbench.cell_order.v1", source_cells)
        or schedule["effective_chunk_size"] != expected_effective
    ):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Chunk schedule cell axis differs from staged input",
            "execution",
        )
    expected_calls: list[list[str]] = []
    for batch in schedule["batches"]:
        start = batch["start"] - 1
        cells = source_cells[start : start + batch["length"]]
        expected_calls.append(cells)
        if len(cells) > expected_effective:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Chunk batch exceeds effective size",
                "execution",
            )
        grant = batch["fragment_grant"]
        if (
            batch["cell_order_identity"] != _identity("scworkbench.cell_order.v1", cells)
            or grant["batch_cell_order_identity"] != batch["cell_order_identity"]
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Chunk batch identity differs from staged input slice",
                "execution",
            )
    observed_calls = [
        item["cells"] for item in readers.calls if item["input"] == schedule["input_name"]
    ]
    if observed_calls != expected_calls:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Matrix reads are not bijective with scheduled cell slices",
            "execution",
        )
    fragments = writers.fragments
    merged = _merge_fragments(schedule, fragments)
    writers.stage_adapter_output(schedule["fragment_output"], merged)
    metadata = _finalizer_metadata(operation, request, schedule)
    summary = _invoke_finalizer_isolated(
        discovery,
        schedule["summary_finalizer_definition"]["callable"],
        merged,
        metadata,
    )
    writers.stage_adapter_output(schedule["summary_output"], summary)

    observed_chunks: list[dict[str, Any]] = []
    for batch in schedule["batches"]:
        index = batch["index"]
        write = fragments[index]
        details = _validate_fragment(batch["fragment_grant"], write.value, inputs)
        descriptor_identity = _identity(
            "scworkbench.operation_pack.observed_output.v3", write.value
        )
        content_identity = _identity("scworkbench.cell_vector_family.content.v1", write.value)
        observed = {
            "schema": "scworkbench.operation_pack.observed_chunk_fragment.v1",
            "index": index,
            "fragment_grant_identity": batch["fragment_grant"]["fragment_grant_identity"],
            "observed_row_count": len(write.value["value"]),
            "observed_cell_order_identity": _identity(
                "scworkbench.cell_order.v1", details["cells"]
            ),
            "observed_key_order_identity": _identity("scworkbench.row_order.v1", details["keys"]),
            "observed_row_order_identity": _identity(
                "scworkbench.row_order.v1",
                [
                    {"key": key, "cell_id": cell}
                    for key, cell in zip(write.value["key"], write.value["cell_id"], strict=True)
                ],
            ),
            "missing_count": details["missing"],
            "descriptor_identity": descriptor_identity,
            "content_identity": content_identity,
            "semantic_validation": "passed",
        }
        observed["observed_fragment_identity"] = _identity(
            "scworkbench.operation_pack.observed_chunk_fragment.v1",
            observed,
            "observation excluding observed_fragment_identity",
        )
        observed_chunks.append(observed)

    merged_identity = _identity("scworkbench.cell_vector_family.content.v1", merged)
    summary_identity = _identity("scworkbench.obs_table.content.v1", summary)
    invocation = {
        "schema": "scworkbench.operation_pack.summary_finalizer_invocation.v1",
        "finalizer_definition_identity": schedule["summary_finalizer_definition"][
            "finalizer_definition_identity"
        ],
        "merged_family_content_identity": merged_identity,
        "finalizer_metadata_identity": metadata["finalizer_metadata_identity"],
        "summary_content_identity": summary_identity,
    }
    invocation["finalizer_invocation_identity"] = _identity(
        "scworkbench.operation_pack.summary_finalizer_invocation.v1",
        invocation,
        "invocation excluding finalizer_invocation_identity",
    )
    execution = {
        "schema": "scworkbench.operation_pack.cell_chunk_observation.v1",
        "request_identity": request["request_identity"],
        "capability_identity": request["execution_capability"]["capability_identity"],
        "operation_definition_identity": request["operation_authentication"][
            "operation_definition_identity"
        ],
        "schedule_identity": schedule["schedule_identity"],
        "observed_chunks": observed_chunks,
        "merged_family_descriptor_identity": _identity(
            "scworkbench.operation_pack.observed_output.v3", merged
        ),
        "merged_family_content_identity": merged_identity,
        "finalizer_definition_identity": schedule["summary_finalizer_definition"][
            "finalizer_definition_identity"
        ],
        "finalizer_metadata_identity": metadata["finalizer_metadata_identity"],
        "finalizer_invocation_identity": invocation["finalizer_invocation_identity"],
        "summary_source_identity": merged_identity,
        "summary_descriptor_identity": _identity(
            "scworkbench.operation_pack.observed_output.v3", summary
        ),
        "summary_content_identity": summary_identity,
    }
    execution["chunk_execution_identity"] = _identity(
        "scworkbench.operation_pack.cell_chunk_observation.v1",
        execution,
        "observation excluding chunk_execution_identity",
    )
    shutil.rmtree(writers._stage_root / "chunk_fragments", ignore_errors=True)
    return execution


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
        inputs = _read_inputs(request, root)
        registry_values = _read_registry_reads(request, operation, root)
        provenance_values = _read_provenance(request, operation, root)
        preflight = preflight_v3(operation, inputs, request["params"], request)
        cell_order = list(next(iter(inputs.values())).get("cell_id", [])) if inputs else []
        writers = V4Writers(request, root, cell_order, inputs)
        readers = V4Readers(inputs, registry_values, provenance_values)
        if fault_hook:
            fault_hook("before_dispatch")
        context = {
            "transition_id": request["transition"]["transition_id"],
            "capability_id": request["execution_capability"]["capability_id"],
        }
        schedules = request.get("chunk_schedules", [])
        if schedules:
            schedule = schedules[0]
            source_cells = _ordered(inputs[schedule["input_name"]], "cell_id")
            for batch in schedule["batches"]:
                start = batch["start"] - 1
                readers.set_scheduled_cells(
                    schedule["input_name"],
                    source_cells[start : start + batch["length"]],
                )
                active[1].handlers[operation["qualified_name"]](
                    readers,
                    writers,
                    copy.deepcopy(request["params"]),
                    {**context, "chunk_batch_index": batch["index"]},
                )
        else:
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
        chunk_execution = _chunk_execution(request, operation, discovery, readers, writers, inputs)
        if fault_hook:
            fault_hook("after_handler")
        writes = writers.writes
        expected_names = [item["name"] for item in request["outputs"]]
        if set(writes) != set(expected_names) or len(writes) != len(expected_names):
            _fail("PYBENCH_RECEIPT_MISMATCH", "Returned output closure differs", "receipt")
        writers.consume()
        outputs: list[dict[str, Any]] = []
        registry_requests: list[dict[str, Any]] = []
        entries: list[dict[str, str]] = []
        observed_by_name: dict[str, dict[str, Any]] = {}
        for grant in request["outputs"]:
            write = writes[grant["name"]]
            try:
                staged_value = json.loads(write.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                _fail("PYBENCH_OUTPUT_INVALID", f"Staged output is unreadable: {exc}", "audit")
            if canonical_json(staged_value) != canonical_json(write.value):
                _fail(
                    "PYBENCH_OUT_OF_SCOPE_MUTATION",
                    "Staged payload changed after writer return",
                    "audit",
                )
            descriptor = _observed_descriptor_v4(grant, write, cell_order, inputs)
            record = {
                "name": grant["name"],
                "allocated_id": grant["allocated_id"],
                "descriptor": descriptor,
            }
            observed_by_name[grant["name"]] = descriptor
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
        if chunk_execution is not None:
            schedule = request["chunk_schedules"][0]
            fragment_descriptor = observed_by_name[schedule["fragment_output"]]
            summary_descriptor = observed_by_name[schedule["summary_output"]]
            chunk_execution["merged_family_descriptor_identity"] = fragment_descriptor[
                "observed_descriptor_identity"
            ]
            chunk_execution["summary_descriptor_identity"] = summary_descriptor[
                "observed_descriptor_identity"
            ]
            chunk_execution.pop("chunk_execution_identity")
            chunk_execution["chunk_execution_identity"] = _identity(
                "scworkbench.operation_pack.cell_chunk_observation.v1",
                chunk_execution,
                "observation excluding chunk_execution_identity",
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
            receipt["registry_reads"] = []  # populated by inherited registry contract below
            for item in request["registry_reads"]:
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
                receipt["registry_reads"].append({"name": item["name"], "descriptor": observed})
        if request.get("provenance_reads"):
            receipt["provenance_reads"] = _provenance_observations(request)
        if chunk_execution is not None:
            receipt["chunk_executions"] = [chunk_execution]
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
    schedules = request.get("chunk_schedules", [])
    chunk_executions = value.get("chunk_executions", [])
    if len(chunk_executions) != len(schedules):
        _fail("PYBENCH_RECEIPT_MISMATCH", "Chunk execution closure differs", "receipt")
    observed_by_name = {item["name"]: item["descriptor"] for item in observed}
    for item, schedule in zip(chunk_executions, schedules, strict=True):
        operation = _operation(discovery, request["operation_authentication"]["name"])
        expected_bindings = (
            item["request_identity"] == request["request_identity"],
            item["capability_identity"] == request["execution_capability"]["capability_identity"],
            item["operation_definition_identity"]
            == request["operation_authentication"]["operation_definition_identity"],
            item["schedule_identity"] == schedule["schedule_identity"],
            item["finalizer_definition_identity"]
            == schedule["summary_finalizer_definition"]["finalizer_definition_identity"],
        )
        fragments = item["observed_chunks"]
        if not all(expected_bindings) or len(fragments) != len(schedule["batches"]):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Chunk observation closure or request binding differs",
                "receipt",
            )
        for fragment, batch in zip(fragments, schedule["batches"], strict=True):
            _assert_identity(
                fragment, "observed_fragment_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt"
            )
            grant = batch["fragment_grant"]
            if (
                fragment["index"] != batch["index"]
                or fragment["fragment_grant_identity"] != grant["fragment_grant_identity"]
                or fragment["observed_row_count"] != grant["expected_row_count"]
                or fragment["observed_cell_order_identity"] != grant["batch_cell_order_identity"]
                or fragment["observed_key_order_identity"] != grant["key_axis_order_identity"]
                or fragment["observed_row_order_identity"] != grant["row_order_identity"]
                or fragment["semantic_validation"] != "passed"
            ):
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Chunk observation differs from its exact fragment grant",
                    "receipt",
                )
        _assert_identity(item, "chunk_execution_identity", "PYBENCH_RECEIPT_MISMATCH", "receipt")
        fragment_descriptor = observed_by_name[schedule["fragment_output"]]
        summary_descriptor = observed_by_name[schedule["summary_output"]]
        invocation = {
            "schema": "scworkbench.operation_pack.summary_finalizer_invocation.v1",
            "finalizer_definition_identity": item["finalizer_definition_identity"],
            "merged_family_content_identity": item["merged_family_content_identity"],
            "finalizer_metadata_identity": item["finalizer_metadata_identity"],
            "summary_content_identity": item["summary_content_identity"],
        }
        invocation["finalizer_invocation_identity"] = _identity(
            "scworkbench.operation_pack.summary_finalizer_invocation.v1",
            invocation,
            "invocation excluding finalizer_invocation_identity",
        )
        if (
            item["summary_source_identity"] != item["merged_family_content_identity"]
            or item["merged_family_descriptor_identity"]
            != fragment_descriptor["observed_descriptor_identity"]
            or item["merged_family_content_identity"] != fragment_descriptor["content_identity"]
            or item["summary_descriptor_identity"]
            != summary_descriptor["observed_descriptor_identity"]
            or item["summary_content_identity"] != summary_descriptor["content_identity"]
            or item["finalizer_invocation_identity"] != invocation["finalizer_invocation_identity"]
            or operation["chunking"]["fragment_output"] != schedule["fragment_output"]
            or operation["chunking"]["summary_output"] != schedule["summary_output"]
        ):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Merged family, finalizer, or summary evidence differs from terminal outputs",
                "receipt",
            )
    if stage_root is not None:
        root = _safe_root(Path(stage_root))
        inputs = _read_inputs(request, root)
        cell_order = list(next(iter(inputs.values())).get("cell_id", [])) if inputs else []
        by_name = {item["name"]: item for item in observed}
        for grant in request["outputs"]:
            default_root = (
                f"registry_requests/{grant['name']}"
                if grant["descriptor"]["category"] == "core_registry_request"
                else f"outputs/{grant['name']}"
            )
            path = _safe_relative(
                root,
                grant["descriptor"].get("relative_root", default_root) + "/payload.json",
                must_exist=True,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            write = _Write(payload, path, {}, grant["descriptor"]["writer"])
            expected_descriptor = _observed_descriptor_v4(grant, write, cell_order, inputs)
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
