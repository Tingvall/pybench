"""Phase 7 shared identities and fail-closed Python execution boundary."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_sha256, protocol_asset
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v3 import validate_manifest_v3

_CORPUS = "fixtures/operation-pack-v3/phase7-genomic-runtime-family-identities.json"
_REFERENCE = Path(__file__).parent / "fixtures" / "operation-pack-v3-python-reference"


def _cases() -> dict[str, dict]:
    corpus = json.loads(protocol_asset(_CORPUS).read_text())
    assert corpus["schema"] == "scworkbench.operation_pack.phase7_identity_fixtures.v1"
    assert corpus["canonical_json_scheme"] == "scworkbench.operation_pack.canonical_json.v1"
    return {case["name"]: case for case in corpus["cases"]}


def _validate_definition(schema_file: str, definition: str, value: dict) -> None:
    parent = json.loads(protocol_asset(schema_file).read_text())
    definition_schema = {
        "$schema": parent["$schema"],
        "$defs": parent["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    assert list(Draft202012Validator(definition_schema).iter_errors(value)) == []


def test_phase7_shared_corpus_recomputes_all_scoped_canonical_identities():
    cases = _cases()
    assert set(cases) == {
        "ordered_genomic_matrix_source",
        "ordered_motif_library",
        "optional_match_set_absent",
        "cached_match_set",
        "dynamic_provider_resolution",
        "two_family_one_table_grant",
        "nonlexical_runtime_key_seal",
        "complete_runtime_family_receipt",
    }
    assert hashlib.sha256(protocol_asset(_CORPUS).read_bytes()).hexdigest() == (
        "3f100e548c3aadbf8d250249394a2314f52b1df34840250f0b341b752a746959"
    )
    for case in cases.values():
        assert case["identities"]
        assert {identity["sha256"] for identity in case["identities"]} == {
            canonical_sha256(case["preimage"])
        }
        assert all(identity["scope"] for identity in case["identities"])

    genomic = cases["ordered_genomic_matrix_source"]["preimage"]
    assert genomic["cell_order_identity"] == _identity(
        "scworkbench.cell_order.v1", genomic["cell_id"]
    )
    assert genomic["feature_order_identity"] == _identity(
        "scworkbench.feature_order.v1", [row["feature_id"] for row in genomic["peaks"]]
    )
    grant = cases["two_family_one_table_grant"]["preimage"]
    seal = cases["nonlexical_runtime_key_seal"]["preimage"]
    receipt = cases["complete_runtime_family_receipt"]["preimage"]
    assert grant["source_content_identity"] == genomic_identity(cases)
    assert grant["cell_order_identity"] == genomic["cell_order_identity"]
    assert seal["grant_identity"]["sha256"] == canonical_sha256(grant)
    assert seal["key_order_identity"] == _identity(
        "scworkbench.operation_pack.runtime_family_keys.v1", seal["ordered_keys"]
    )
    assert receipt["grant_identity"] == seal["grant_identity"]
    assert receipt["seal"]["seal_identity"]["sha256"] == canonical_sha256(seal)
    assert [entry["name"] for entry in receipt["closure"]] == grant["closure_outputs"]
    assert cases["optional_match_set_absent"]["preimage"]["selection"] is None
    assert cases["cached_match_set"]["preimage"]["selection"] is not None


def genomic_identity(cases: dict[str, dict]) -> dict:
    return cases["ordered_genomic_matrix_source"]["identities"][0]


def test_phase7_grant_seal_and_receipt_observation_match_closed_schemas():
    cases = _cases()
    grant = cases["two_family_one_table_grant"]
    seal = cases["nonlexical_runtime_key_seal"]
    observation = cases["complete_runtime_family_receipt"]
    _validate_definition(
        "operation-pack-transition-request-v3.schema.json",
        "runtime_cell_family_key_grant",
        dict(grant["preimage"], grant_identity=grant["identities"][0]),
    )
    _validate_definition(
        "operation-pack-transition-receipt-v3.schema.json",
        "runtime_cell_family_key_seal",
        dict(seal["preimage"], seal_identity=seal["identities"][0]),
    )
    _validate_definition(
        "operation-pack-transition-receipt-v3.schema.json",
        "runtime_cell_family_key_observation",
        dict(observation["preimage"], observation_identity=observation["identities"][0]),
    )


def _python_manifest_with_phase7_authority(kind: str) -> dict:
    manifest = json.loads((_REFERENCE / "operation-pack.json").read_text())
    operation = next(op for op in manifest["operations"] if op["name"] == "chunk_probe")
    if kind == "genomic_matrix_source":
        operation["registry_reads"] = [
            {
                "name": "genomic_source",
                "category": "authenticated_core_registry_snapshot",
                "kind": kind,
                "reader": "read_genomic_matrix_source",
                "matrix_input": "matrix",
                "row_order": "matrix_feature_order_v1",
                "call_discipline": "single_use",
            }
        ]
    elif kind == "motif_library":
        operation["params_schema"]["properties"]["motif_library"] = {"type": "string"}
        operation["registry_reads"] = [
            {
                "name": "motifs",
                "category": "authenticated_core_registry_snapshot",
                "kind": kind,
                "reader": "read_motif_library",
                "library_parameter": "motif_library",
                "call_discipline": "single_use",
            }
        ]
    elif kind == "motif_match_set":
        operation["params_schema"]["properties"]["motif_match_set"] = {"type": ["string", "null"]}
        operation["registry_reads"] = [
            {
                "name": "matches",
                "category": "authenticated_core_registry_snapshot",
                "kind": kind,
                "reader": "read_motif_match_set",
                "match_set_parameter": "motif_match_set",
                "call_discipline": "single_use",
            }
        ]
    elif kind == "dynamic_matrix_registry_package":
        operation["dependencies"].append(
            {
                "requirement_id": "provider",
                "kind": kind,
                "matrix_input": "matrix",
                "registry_read": "genomic_source",
                "metadata_field": "genome_provider",
                "ecosystem": "R",
                "package_name_scheme": "r_package_name_v1",
                "probe": "r_namespace_exports_v1",
                "disposition": "required",
                "version_policy": "record_resolved_installed_version_v1",
            }
        )
    else:
        operation["dimension_policies"].append(
            {
                "output": "feature_table",
                "policy": "runtime_ordered_cell_family_v1",
                "group": "chromvar",
                "source_read": "genomic_source",
                "row_order": "cell_major_then_declared_key_v1",
                "missing_values": "allowed",
                "maximum_keys": 10000,
                "maximum_rows": 10000000,
            }
        )
    return manifest


@pytest.mark.parametrize(
    "kind",
    [
        "genomic_matrix_source",
        "motif_library",
        "motif_match_set",
        "dynamic_matrix_registry_package",
        "runtime_ordered_cell_family_v1",
    ],
)
def test_python_phase7_authority_rejects_before_stage_mutation(kind, tmp_path):
    manifest = _python_manifest_with_phase7_authority(kind)
    schema = json.loads(protocol_asset("operation-pack-v3.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []
    stage = tmp_path / "stage"
    stage.mkdir()
    before = list(stage.iterdir())
    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(copy.deepcopy(manifest))
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "semantic_admission"
    assert list(stage.iterdir()) == before
