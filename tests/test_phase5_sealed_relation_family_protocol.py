from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.operation_packs_v3 import validate_manifest_v3
from pybench.protocol_v4 import execute_operation_pack_v4, validate_request_v4

REFERENCE_MANIFEST = (
    Path(__file__).parent
    / "fixtures"
    / "operation-pack-v3-python-reference"
    / "operation-pack.json"
)


def _schema(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text())


def test_r_frozen_relation_identity_corpus_matches_python_exactly():
    path = protocol_asset("fixtures/operation-pack-v3/relation-family-identities.json")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "5f94004cc229cc5f02c4b7a9401f5c91a0cb9346469d48499b46809c9a919860"
    )
    corpus = json.loads(path.read_text())
    assert corpus["schema"] == "scworkbench.operation_pack.relation_identity_fixture.v1"
    cases = {case["name"]: case for case in corpus["cases"]}
    assert len(cases) == len(corpus["cases"]) == 14
    for case in cases.values():
        expected = case["identity"]
        assert canonical_sha256(case["preimage"]) == expected["sha256"]
        assert _identity(expected["scheme"], case["preimage"], expected.get("scope")) == expected
        if isinstance(case["value"], dict):
            assert expected in case["value"].values()
        else:
            assert case["value"] == case["preimage"]

    assert cases["relation_key_axis"]["identity"] != cases["relation_swapped_key_axis"]["identity"]
    assert (
        cases["relation_structured_pair_axis"]["identity"]
        != cases["relation_swapped_pair_axis"]["identity"]
    )
    assert (
        cases["relation_separator_left"]["identity"]
        != cases["relation_separator_right"]["identity"]
    )

    schema_cases = (
        (
            "operation-pack-transition-request-v3.schema.json",
            "sealed_relation_known_source_axis",
            "relation_typed_source_axis",
        ),
        (
            "operation-pack-transition-request-v3.schema.json",
            "sealed_relation_family_grant",
            "relation_provisional_grant",
        ),
        (
            "operation-pack-transition-receipt-v3.schema.json",
            "sealed_relation_declaration",
            "relation_declaration",
        ),
        (
            "operation-pack-transition-receipt-v3.schema.json",
            "sealed_relation_seal",
            "relation_seal",
        ),
        (
            "operation-pack-transition-receipt-v3.schema.json",
            "sealed_relation_writer",
            "relation_sealed_writer",
        ),
        (
            "operation-pack-transition-receipt-v3.schema.json",
            "sealed_relation_family_observation",
            "relation_observation",
        ),
    )
    for asset, definition, name in schema_cases:
        root = _schema(asset)
        validator = Draft202012Validator(
            {"$schema": root["$schema"], "$defs": root["$defs"], "$ref": f"#/$defs/{definition}"}
        )
        assert validator.is_valid(cases[name]["value"])

    for name, asset in (
        ("relation_success_request", "operation-pack-transition-request-v3.schema.json"),
        ("relation_success_receipt", "operation-pack-transition-receipt-v3.schema.json"),
    ):
        assert Draft202012Validator(_schema(asset)).is_valid(cases[name]["value"])


def _identity_stub() -> dict:
    return {"scheme": "scworkbench.row_order.v1", "sha256": "0" * 64}


def _scoped_stub(scheme: str, scope: str) -> dict:
    return {"scheme": scheme, "scope": scope, "sha256": "0" * 64}


def _known_axis() -> dict:
    identity = _identity_stub()
    return {
        "schema": "scworkbench.operation_pack.relation_source_axis.v1",
        "source_kind": "typed_input_v1",
        "input_name": "space",
        "input_kind": "space",
        "source_id": "space-1",
        "input_descriptor_identity": identity,
        "input_content_identity": identity,
        "source_state_identity": identity,
        "ordered_cells": ["c2", "c1", "c3"],
        "cell_order_identity": identity,
        "source_axis_identity": _scoped_stub(
            "scworkbench.operation_pack.relation_source_axis.v1",
            "source axis excluding source_axis_identity",
        ),
    }


def _provisional_grant(source_selector: dict, known_axis: dict | None) -> dict:
    identity = _identity_stub()
    grant = {
        "schema": "scworkbench.operation_pack.relation_provisional_grant.v1",
        "manifest_policy_identity": identity,
        "source_selector": source_selector,
        "ordinary_output_base_grant_identity": identity,
        "transition_identity": identity,
        "output_name": "relation_family",
        "kind": "relation_family",
        "limits": {
            "maximum_axis_cells": 3,
            "maximum_keys": 2,
            "maximum_rows": 6,
            "maximum_id_bytes": 1024,
            "maximum_control_bytes": 67108864,
            "maximum_chunk_pairs": 8192,
        },
        "grant_identity": _scoped_stub(
            "scworkbench.operation_pack.relation_provisional_grant.v1",
            "provisional grant excluding grant_identity",
        ),
    }
    if known_axis is not None:
        grant["known_source_axis"] = known_axis
    return grant


def test_schema_binds_known_axis_at_request_and_delays_g2_axis():
    root = _schema("operation-pack-transition-request-v3.schema.json")
    schema = {
        "$schema": root["$schema"],
        "$defs": root["$defs"],
        "$ref": "#/$defs/sealed_relation_family_grant",
    }
    validator = Draft202012Validator(schema)
    identity = _identity_stub()
    typed = _provisional_grant(
        {"source": "typed_input_v1", "input": "space", "source_axis_identity": identity},
        _known_axis(),
    )
    g2 = _provisional_grant(
        {
            "source": "g2_derived_fitted_axis_v1",
            "model_output": "model",
            "g2_grant_identity": identity,
            "parent_snapshot_identity": identity,
        },
        None,
    )
    assert validator.is_valid(typed)
    assert not validator.is_valid({k: v for k, v in typed.items() if k != "known_source_axis"})
    assert validator.is_valid(g2)
    assert not validator.is_valid({**g2, "known_source_axis": _known_axis()})


def test_sealed_grant_attaches_only_to_relation_output_descriptor():
    root = _schema("operation-pack-transition-request-v3.schema.json")
    branches = root["$defs"]["output_descriptor"]["oneOf"]
    by_kind = {
        branch["properties"]["kind"]["const"]: branch
        for branch in branches
        if branch.get("properties", {}).get("kind", {}).get("const")
        in {"relation_family", "cell_vector_family"}
    }
    field = "sealed_relation_family_provisional_grant"
    assert field in by_kind["relation_family"]["properties"]
    assert field not in by_kind["cell_vector_family"]["properties"]


def test_python_manifest_rejects_schema_valid_sealed_relation_policy():
    manifest = json.loads(REFERENCE_MANIFEST.read_text())
    operation = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::all_contracts"
    )
    policy = {
        "schema": "scworkbench.operation_pack.sealed_relation_family_policy.v1",
        "output": "relation_family",
        "policy": "sealed_relation_family_v1",
        "axis_source": {"source": "typed_input_v1", "input": "space_dense"},
        "key_order": "first_occurrence_v1",
        "row_order": "declared_exact_v1",
        "pair_multiplicity": "unique_key_cell_v1",
        "numeric_fields": "declare_presence_finite_v1",
        "declaration_discipline": "exactly_once_before_write",
        "maximum_axis_cells": 1_000_000,
        "maximum_keys": 1_024,
        "maximum_rows": 1_000_000,
        "maximum_id_bytes": 1_024,
        "maximum_control_bytes": 67_108_864,
        "maximum_chunk_pairs": 8_192,
    }
    policy["policy_identity"] = _identity(
        policy["schema"], policy, "policy excluding policy_identity"
    )
    operation.setdefault("dimension_policies", []).append(policy)
    assert Draft202012Validator(_schema("operation-pack-v3.schema.json")).is_valid(manifest)

    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(manifest)
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "semantic_admission"


def test_python_runtime_rejects_relation_grant_before_stage_access(tmp_path):
    corpus = json.loads(
        protocol_asset("fixtures/operation-pack-v3/relation-family-identities.json").read_text()
    )
    request = next(
        case["value"] for case in corpus["cases"] if case["name"] == "relation_success_request"
    )
    stage = tmp_path / "uncreated-stage"
    for call in (
        lambda: validate_request_v4(request, object()),  # type: ignore[arg-type]
        lambda: execute_operation_pack_v4(request, object(), stage),  # type: ignore[arg-type]
    ):
        with pytest.raises(OperationPackError) as caught:
            call()
        assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
        assert caught.value.details["phase"] == "staging"
    assert not stage.exists()
