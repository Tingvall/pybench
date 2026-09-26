"""Language-neutral Phase 6 matrix-trained model authority; no Python runtime claim."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.protocol_v3 import execute_operation_pack_v3
from pybench.protocol_v4 import execute_operation_pack_v4

FIXTURE = "fixtures/operation-pack-v3/phase6-matrix-model-identities.json"


def _load(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text(encoding="utf-8"))


def _definition(schema: dict, name: str) -> dict:
    return {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}


def _manifest_policy_schema(value):
    if isinstance(value, dict):
        if "matrix_trained_contained_model_closure_policy" in value:
            return value["matrix_trained_contained_model_closure_policy"]
        for child in value.values():
            found = _manifest_policy_schema(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _manifest_policy_schema(child)
            if found is not None:
                return found
    return None


def _assert_self_identity(value: dict, field: str) -> None:
    claimed = value[field]
    preimage = {key: child for key, child in value.items() if key != field}
    assert _identity(claimed["scheme"], preimage, claimed["scope"]) == claimed


def test_phase6_shared_schemas_and_canonical_identity_fixture():
    manifest_schema = _load("operation-pack-v3.schema.json")
    request_schema = _load("operation-pack-transition-request-v3.schema.json")
    receipt_schema = _load("operation-pack-transition-receipt-v3.schema.json")
    for schema in (manifest_schema, request_schema, receipt_schema):
        Draft202012Validator.check_schema(schema)
    fixture = _load(FIXTURE)
    assert fixture["canonical_json_scheme"] == "scworkbench.operation_pack.canonical_json.v1"
    policy_schema = _manifest_policy_schema(manifest_schema)
    assert policy_schema is not None
    assert not list(Draft202012Validator(policy_schema).iter_errors(fixture["manifest_policy"]))
    paired = fixture["paired_space_output_declaration"]
    assert not list(
        Draft202012Validator(_definition(manifest_schema, "output")).iter_errors(paired)
    )
    assert paired["category"] == "materialization"
    assert paired["materialized_by_terminals"] == ["model"]
    pairs = [
        (request_schema, "matrix_model_source_linkage_v1", fixture["model_source_linkage"]),
        (
            request_schema,
            "contained_matrix_model_sibling_write_grant",
            fixture["contained_matrix_model_sibling_write_grant"],
        ),
        (
            receipt_schema,
            "matrix_model_sibling_closure_observation",
            fixture["matrix_model_sibling_closure_observation"],
        ),
    ]
    for schema, name, value in pairs:
        errors = list(Draft202012Validator(_definition(schema, name)).iter_errors(value))
        assert not errors, [error.message for error in errors]
    _assert_self_identity(fixture["model_source_linkage"], "linkage_identity")
    _assert_self_identity(
        fixture["contained_matrix_model_sibling_write_grant"], "write_grant_identity"
    )
    _assert_self_identity(
        fixture["matrix_model_sibling_closure_observation"], "observation_identity"
    )
    _assert_self_identity(
        fixture["matrix_model_sibling_closure_observation"]["tree_manifest"],
        "manifest_identity",
    )
    self_linked = request_schema["$defs"]["self_linked_contained_model_read_grant"]
    options = self_linked["properties"]["model_source_linkage"]["oneOf"]
    assert {item["$ref"] for item in options} >= {
        "#/$defs/model_source_linkage_v2",
        "#/$defs/model_source_linkage_v3",
        "#/$defs/matrix_model_source_linkage_v1",
    }
    grant = fixture["contained_matrix_model_sibling_write_grant"]
    closure_preimage = {
        "model_output": grant["output"],
        "model_grant_identity": _identity(
            "scworkbench.operation_pack.output_grant.v3",
            {"name": "model", "kind": "model_ref"},
            "descriptor excluding grant_identity",
        ),
        "model_source_linkage_identity": grant["model_source_linkage"]["linkage_identity"],
        "promotion_group": "latent_fit",
        "siblings": [
            {key: sibling[key] for key in ("name", "grant_identity", "replay_policy")}
            for sibling in grant["required_sibling_terminals"]
        ],
    }
    closure_preimage["model_grant_identity"].pop("scope")
    closure_preimage["model_source_linkage_identity"] = {
        key: closure_preimage["model_source_linkage_identity"][key] for key in ("scheme", "sha256")
    }
    assert (
        _identity("scworkbench.operation_pack.matrix_model_sibling_closure.v1", closure_preimage)
        == grant["model_sibling_closure_identity"]
    )


def test_phase6_unknown_fields_and_changed_snapshot_fail_closed():
    schema = _load("operation-pack-transition-request-v3.schema.json")
    fixture = _load(FIXTURE)
    linkage = copy.deepcopy(fixture["model_source_linkage"])
    linkage["source_matrix_snapshot_id"] = "0" * 64
    assert (
        _identity(
            linkage["linkage_identity"]["scheme"],
            {key: child for key, child in linkage.items() if key != "linkage_identity"},
            linkage["linkage_identity"]["scope"],
        )
        != linkage["linkage_identity"]
    )
    grant = copy.deepcopy(fixture["contained_matrix_model_sibling_write_grant"])
    grant["source_bundle_path"] = "/forbidden"
    assert list(
        Draft202012Validator(
            _definition(schema, "contained_matrix_model_sibling_write_grant")
        ).iter_errors(grant)
    )


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_python_executor_rejects_r_only_grant_before_stage_mutation(tmp_path: Path, version: str):
    stage = tmp_path / "untouched-stage"
    request = {
        "schema": "scworkbench.operation_pack.transition_request.v3",
        "pack_abi_version": "3" if version == "v4" else "2",
        "process_protocol_version": "pybench-op-v4" if version == "v4" else "pybench-op-v3",
        "outputs": [{"descriptor": {"contained_matrix_model_sibling_write_grant": {}}}],
    }
    executor = execute_operation_pack_v4 if version == "v4" else execute_operation_pack_v3
    with pytest.raises(OperationPackError, match="matrix-trained contained-model authority"):
        executor(request, None, stage)
    assert not stage.exists()
