from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity as _canonical_identity
from pybench.operation_packs_v2 import protocol_asset
from pybench.operation_packs_v3 import validate_manifest_v3
from pybench.protocol_v4 import execute_operation_pack_v4, validate_receipt_v4, validate_request_v4
from test_phase4e_sparse_knn_protocol import _fixture as phase4e_fixture

FIXTURE_ROOT = Path(__file__).parents[1] / "protocol" / "fixtures" / "operation-pack-v3"
UNION_FIXTURE = Path(__file__).parent / "fixtures" / "phase6_latent_union"
REFERENCE_MANIFEST = (
    Path(__file__).parent
    / "fixtures"
    / "operation-pack-v3-python-reference"
    / "operation-pack.json"
)


def _schema(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text())


def _validator(asset: str, definition: str) -> Draft202012Validator:
    root = _schema(asset)
    return Draft202012Validator(
        {"$schema": root["$schema"], "$defs": root["$defs"], "$ref": f"#/$defs/{definition}"}
    )


def _identity() -> dict:
    return {"scheme": "scworkbench.test.v1", "sha256": "0" * 64}


def _self_identity() -> dict:
    return {
        "scheme": "scworkbench.test.v1",
        "scope": "record excluding identity",
        "sha256": "0" * 64,
    }


def _assert_rehashed(record: dict, field: str) -> None:
    expected = record[field]
    preimage = {key: value for key, value in record.items() if key != field}
    assert _canonical_identity(expected["scheme"], preimage, expected.get("scope")) == expected


def test_phase6_real_retained_union_request_receipt_byte_and_identity_parity():
    request_path = UNION_FIXTURE / "request.json"
    receipt_path = UNION_FIXTURE / "receipt.json"
    assert hashlib.sha256(request_path.read_bytes()).hexdigest() == (
        "c61b1d9afc7180a7510acc08aca6af8ff8a6440162ea4a73c8b6299504b9663b"
    )
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == (
        "41919a49dfb7f65ba6f79c7d447db1cd33e98addd70bb0d0c6bedc5e41a0503a"
    )
    request = json.loads(request_path.read_text())
    receipt = json.loads(receipt_path.read_text())
    assert Draft202012Validator(
        _schema("operation-pack-transition-request-v3.schema.json")
    ).is_valid(request)
    assert Draft202012Validator(
        _schema("operation-pack-transition-receipt-v3.schema.json")
    ).is_valid(receipt)

    request_checks = [
        (request, "request_identity"),
        (request["execution_capability"], "capability_identity"),
        (request["producer_lineage_grants"][0], "grant_identity"),
        (request["derived_cell_output_axis_grants"][0], "grant_identity"),
    ]
    request_checks.extend((item, "grant_identity") for item in request["typed_space_grants"])
    axis_grant = request["derived_cell_output_axis_grants"][0]
    request_checks.extend(
        (item, "source_axis_reference_identity") for item in axis_grant["source_axes"]
    )
    request_checks.extend(
        [
            (axis_grant["source_axis_aggregate"], "source_axis_aggregate_identity"),
            (axis_grant["linked_output_closure"], "linked_output_closure_identity"),
        ]
    )
    for record, field in request_checks:
        _assert_rehashed(record, field)

    receipt_checks = [(receipt, "receipt_identity")]
    receipt_checks.extend(
        (item["descriptor"], "observed_descriptor_identity") for item in receipt["outputs"]
    )
    receipt_checks.extend(
        (item, "observation_identity") for item in receipt["producer_lineage_reads"]
    )
    receipt_checks.extend(
        (item, "observation_identity") for item in receipt["derived_cell_output_axis_observations"]
    )
    space = receipt["outputs"][0]["descriptor"]
    receipt_checks.extend(
        [
            (space["sealed_output_cardinality_grant"], "sealed_cardinality_grant_identity"),
            (space["observed_sealed_output_cardinality"], "observed_sealed_cardinality_identity"),
        ]
    )
    for record, field in receipt_checks:
        _assert_rehashed(record, field)
    assert receipt["request_identity"] == request["request_identity"]
    assert receipt["capability_identity"] == request["execution_capability"]["capability_identity"]


def test_phase6_real_linked_space_descriptor_requires_sealed_order_and_dimensions():
    request = json.loads((UNION_FIXTURE / "request.json").read_text())
    receipt = json.loads((UNION_FIXTURE / "receipt.json").read_text())
    request_space = request["outputs"][0]["descriptor"]
    output_validator = _validator(
        "operation-pack-transition-request-v3.schema.json", "output_descriptor"
    )
    assert output_validator.is_valid(request_space)
    without_policy = copy.deepcopy(request_space)
    without_policy.pop("derived_cell_output_axis_policy")
    assert not output_validator.is_valid(without_policy)
    early_axis = copy.deepcopy(request_space)
    early_axis["expected_cell_order_identity"] = request["derived_cell_output_axis_grants"][0][
        "source_axes"
    ][0]["source_cell_order_identity"]
    assert not output_validator.is_valid(early_axis)

    sealed = receipt["outputs"][0]["descriptor"]["sealed_output_cardinality_grant"]
    cardinality_validator = _validator(
        "operation-pack-transition-receipt-v3.schema.json", "sealed_output_cardinality_grant"
    )
    assert cardinality_validator.is_valid(sealed)
    absent_dimension = copy.deepcopy(sealed)
    absent_dimension.pop("dimension_count")
    assert not cardinality_validator.is_valid(absent_dimension)


def test_phase6_full_typed_space_axis_is_closed_and_opt_in():
    grant = json.loads((FIXTURE_ROOT / "phase4e-sparse-knn" / "request.json").read_text())[
        "typed_space_grants"
    ][0]
    validator = _validator("operation-pack-transition-request-v3.schema.json", "typed_space_grant")
    assert validator.is_valid(grant)

    full = copy.deepcopy(grant)
    full["selector_parameter"] = None
    full["full_source_cell_axis"] = {
        "ordered_cell_ids": ["query_2", "query_1"],
        "cell_count": 2,
        "cell_order_identity": _identity(),
    }
    assert validator.is_valid(full)
    for changed in (
        {**full, "selector_parameter": "cells"},
        {**full, "full_source_cell_axis": {**full["full_source_cell_axis"], "extra": True}},
        {
            **full,
            "full_source_cell_axis": {
                **full["full_source_cell_axis"],
                "ordered_cell_ids": ["query_1", "query_1"],
            },
        },
    ):
        assert not validator.is_valid(changed)

    manifest = json.loads((FIXTURE_ROOT / "phase4e-sparse-knn" / "operation-pack.json").read_text())
    for operation in manifest["operations"]:
        for read in operation.get("typed_space_reads", []):
            read["selection"] = {"policy": "full_input_axis_v1"}
    assert validate_manifest_v3(manifest, expected_backend=None)["operations"]


def test_phase6_g7_static_space_source_and_linked_space_are_schema_closed():
    corpus = json.loads((FIXTURE_ROOT / "identities.json").read_text())
    cases = {case["name"]: case["value"] for case in corpus["cases"]}
    declaration = copy.deepcopy(cases["g7_union_manifest_declaration"])
    declaration["source"] = {
        "mode": "static_typed_space_union_v1",
        "typed_space_reads": ["reference_read", "query_read"],
        "projection": "space_cell_axis_v1",
        "overlap_policy": "reject_any_overlap_v1",
    }
    declaration["linked_outputs"] = [
        {
            "name": "union_space",
            "shape": "one_per_cell_space_v1",
            "row_order": "sealed_cell_axis_order_v1",
            "missing_values": "forbidden",
            "dimension_source": {"mode": "typed_space_read_v1", "read": "reference_read"},
        }
    ]
    manifest = _schema("operation-pack-v3.schema.json")
    manifest_validator = Draft202012Validator(
        {
            "$schema": manifest["$schema"],
            "$defs": manifest["$defs"],
            "$ref": "#/$defs/operation/properties/derived_cell_output_axes/items",
        }
    )
    assert manifest_validator.is_valid(declaration)
    duplicate = copy.deepcopy(declaration)
    duplicate["source"]["typed_space_reads"] = ["reference_read", "reference_read"]
    assert not manifest_validator.is_valid(duplicate)

    source = {
        "source_mode": "static_typed_space_input_axis_v1",
        "typed_space_read_name": "reference_read",
        "typed_space_grant_identity": _identity(),
        "resolved_ordinal": 1,
        "input_name": "reference_space",
        "input_descriptor_identity": _identity(),
        "input_content_identity": _identity(),
        "primitive_kind": "space",
        "projection": "space_cell_axis_v1",
        "source_axis_selector_identity": None,
        "source_cell_count": 2,
        "source_cell_order_identity": _identity(),
        "source_dimension_count": 3,
        "source_axis_reference_identity": _self_identity(),
    }
    source_validator = _validator(
        "operation-pack-transition-request-v3.schema.json", "derived_cell_source_axis_reference"
    )
    assert source_validator.is_valid(source)
    for missing in ("resolved_ordinal", "source_dimension_count", "typed_space_grant_identity"):
        changed = copy.deepcopy(source)
        changed.pop(missing)
        assert not source_validator.is_valid(changed)

    linked = copy.deepcopy(cases["g7_union_grant"])
    output = linked["linked_output_closure"]["linked_outputs"][0]
    output["kind"] = "space"
    output["shape"] = "one_per_cell_space_v1"
    output["dimension_source"] = {"mode": "typed_space_read_v1", "read": "reference_read"}
    output["dimension_count"] = 3
    grant_validator = _validator(
        "operation-pack-transition-request-v3.schema.json", "derived_cell_output_axis_grant"
    )
    assert grant_validator.is_valid(linked)
    output.pop("dimension_count")
    assert not grant_validator.is_valid(linked)

    sealed = copy.deepcopy(cases["g7_union_nonlexicographic_vector_cardinality"])
    sealed["kind"] = "space"
    sealed["shape"] = "one_per_cell_space_v1"
    sealed["expected_dimensions"] = [sealed["cell_axis_count"], 3]
    sealed["dimension_count"] = 3
    sealed["dimension_source"] = {"mode": "typed_space_read_v1", "read": "reference_read"}
    cardinality_validator = _validator(
        "operation-pack-transition-receipt-v3.schema.json", "sealed_output_cardinality_grant"
    )
    assert cardinality_validator.is_valid(sealed)
    without_dimension = copy.deepcopy(sealed)
    without_dimension.pop("dimension_count")
    assert not cardinality_validator.is_valid(without_dimension)
    old_shape_with_dimension = copy.deepcopy(sealed)
    old_shape_with_dimension["shape"] = "one_per_cell"
    assert not cardinality_validator.is_valid(old_shape_with_dimension)
    observed = copy.deepcopy(cases["g7_union_nonlexicographic_vector_observed_cardinality"])
    observed["shape"] = "one_per_cell_space_v1"
    assert _validator(
        "operation-pack-transition-receipt-v3.schema.json", "observed_sealed_output_cardinality"
    ).is_valid(observed)


def test_phase6_retained_producer_lineage_grant_and_observation_are_closed():
    source = {"input_descriptor_identity": _identity(), "content_identity": _identity()}
    grant = {
        "schema": "scworkbench.operation_pack.producer_lineage_grant.v1",
        "name": "producer",
        "reader": "read_producer_transition_lineage",
        "source_mode": "retained_producer_v1",
        "source_snapshot_id": "snapshot_1",
        "source_run_id": "run_1",
        "source_op_name": "scwblatent::project",
        "source_run_identity": _identity(),
        "query_space": {**source, "space_id": "query_1", "source_output_name": "projected_space"},
        "projection_summary": {
            **source,
            "artifact_id": "summary_1",
            "source_output_name": "summary",
        },
        "model": {**source, "artifact_id": "model_1", "fitted_space_id": "fit_1"},
        "fitted_space": {**source, "space_id": "fit_1"},
        "source_state_identity": _identity(),
        "grant_identity": _self_identity(),
    }
    validator = _validator(
        "operation-pack-transition-request-v3.schema.json", "producer_lineage_grant"
    )
    assert validator.is_valid(grant)
    for changed in (
        {**grant, "source_mode": "staged_producer_v1"},
        {**grant, "caller_model_path": "/tmp/model"},
    ):
        assert not validator.is_valid(changed)

    request_schema = _schema("operation-pack-transition-request-v3.schema.json")
    reader_validator = Draft202012Validator(
        {
            "$schema": request_schema["$schema"],
            "$defs": request_schema["$defs"],
            **request_schema["$defs"]["capability"]["properties"]["reader_grants"]["items"],
        }
    )
    reader_grant = {
        "producer_lineage_read_name": "producer",
        "reader": "read_producer_transition_lineage",
        "grant_identity": _self_identity(),
    }
    assert reader_validator.is_valid(reader_grant)
    assert not reader_validator.is_valid({**reader_grant, "caller_path": "/tmp/model"})

    observation = {
        "schema": "scworkbench.operation_pack.producer_lineage_read_observation.v1",
        "name": "producer",
        "grant_identity": _identity(),
        "use_count": 1,
        "observation_identity": _self_identity(),
    }
    receipt_validator = _validator(
        "operation-pack-transition-receipt-v3.schema.json", "producer_lineage_read_observation"
    )
    assert receipt_validator.is_valid(observation)
    observation["use_count"] = 0
    assert not receipt_validator.is_valid(observation)


def test_phase6_python_rejects_new_authority_before_stage_mutation(tmp_path: Path):
    manifest = json.loads(REFERENCE_MANIFEST.read_text())
    manifest["operations"][0]["producer_lineage_reads"] = [
        {
            "name": "producer",
            "reader": "read_producer_transition_lineage",
            "query_space_input": "space",
            "summary_input": "obs_table",
            "model_input": "model_ref",
            "fitted_space_input": "space",
            "source_policy": "accepted_staged_or_retained_v1",
            "call_discipline": "exactly_once",
        }
    ]
    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(manifest)
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"

    base = json.loads((FIXTURE_ROOT / "phase4e-sparse-knn" / "request.json").read_text())
    requests = {}
    requests["full_axis"] = copy.deepcopy(base)
    requests["full_axis"]["typed_space_grants"][0]["selector_parameter"] = None
    requests["lineage"] = copy.deepcopy(base)
    requests["lineage"]["producer_lineage_grants"] = [{"name": "producer"}]
    requests["linked_space"] = copy.deepcopy(base)
    requests["linked_space"]["derived_cell_output_axis_grants"] = [
        {"source_axes": [{"source_mode": "static_typed_space_input_axis_v1"}]}
    ]
    for name, request in requests.items():
        with pytest.raises(OperationPackError) as caught:
            validate_request_v4(request, None)  # type: ignore[arg-type]
        assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
        stage = tmp_path / name
        with pytest.raises(OperationPackError) as caught:
            execute_operation_pack_v4(request, None, stage)  # type: ignore[arg-type]
        assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
        assert not stage.exists()

    _, receipt, discovery = phase4e_fixture()
    receipt["producer_lineage_reads"] = [{"name": "producer"}]
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v4(receipt, base, discovery)
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    linked_receipt = copy.deepcopy(receipt)
    linked_receipt.pop("producer_lineage_reads")
    linked_receipt["outputs"][0]["descriptor"]["sealed_output_cardinality_grant"] = {
        "shape": "one_per_cell_space_v1"
    }
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v4(linked_receipt, base, discovery)
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
