from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_json, canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.protocol_v4 import execute_operation_pack_v4, validate_request_v4

MANIFEST_SCHEMA = "operation-pack-v3.schema.json"
REQUEST_SCHEMA = "operation-pack-transition-request-v3.schema.json"
RECEIPT_SCHEMA = "operation-pack-transition-receipt-v3.schema.json"
LEGACY_IDENTITY_LEDGER_SHA256 = "c5e82d12aad4b02ae211b511ae6b232e5221f75cdea6f52ab851df906c788327"

DFA_CASE_NAMES = {
    "dfa_manifest_policy",
    "dfa_full_parent_snapshot",
    "dfa_session_projection",
    "dfa_provisional_grant",
    "dfa_private_declaration",
    "dfa_sealed_axis",
    "dfa_selected_axis_sidecar",
    "dfa_sealed_model_grant",
    "dfa_sealed_embedding_grant",
    "dfa_sealed_weights_grant",
    "dfa_model_source_linkage_v3",
    "dfa_exact_vector_coverage",
    "dfa_sparse_family_coverage",
    "dfa_derived_model_tree_observation",
    "dfa_model_content_identity",
    "dfa_model_observed_descriptor",
    "dfa_complete_observation",
    "dfa_full_axis_session",
    "dfa_full_axis_provisional_grant",
    "dfa_full_axis_declaration",
    "dfa_full_axis_seal",
    "dfa_success_request",
    "dfa_success_receipt",
}


def _schema(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text())


def _corpus() -> dict:
    return json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())


def _cases() -> dict[str, dict]:
    return {case["name"]: case for case in _corpus()["cases"]}


def _subschema(name: str, *path: str) -> Draft202012Validator:
    root = _schema(name)
    value = root
    for key in path:
        value = value[key]
    value = copy.deepcopy(value)
    value["$schema"] = root["$schema"]
    value["$defs"] = root["$defs"]
    return Draft202012Validator(value)


def _manifest_policy_validator() -> Draft202012Validator:
    root = _schema(MANIFEST_SCHEMA)
    variant = next(
        item
        for item in root["$defs"]["output"]["oneOf"]
        if "derived_fitted_axis_policy" in item.get("properties", {})
    )
    value = copy.deepcopy(variant["properties"]["derived_fitted_axis_policy"])
    value["$schema"] = root["$schema"]
    value["$defs"] = root["$defs"]
    return Draft202012Validator(value)


def _request_validator(definition: str) -> Draft202012Validator:
    return _subschema(REQUEST_SCHEMA, "$defs", definition)


def _receipt_validator(definition: str) -> Draft202012Validator:
    return _subschema(RECEIPT_SCHEMA, "$defs", definition)


def _assert_valid(validator: Draft202012Validator, value: object) -> None:
    assert not list(validator.iter_errors(value))


def _assert_rejected(validator: Draft202012Validator, value: object) -> None:
    assert list(validator.iter_errors(value))


def test_derived_fitted_axis_shared_schemas_are_valid_draft_2020_12_documents():
    for name in (MANIFEST_SCHEMA, REQUEST_SCHEMA, RECEIPT_SCHEMA):
        Draft202012Validator.check_schema(_schema(name))


def test_derived_fitted_axis_identity_corpus_is_closed_and_reproduced_by_python():
    cases = _cases()
    dfa_cases = {name: case for name, case in cases.items() if name.startswith("dfa_")}
    assert set(dfa_cases) == DFA_CASE_NAMES
    assert len([name for name in cases if not name.startswith("sparse_")]) == 142
    for case in dfa_cases.values():
        expected = case["identity"]
        assert canonical_sha256(case["preimage"]) == expected["sha256"]
        assert _identity(expected["scheme"], case["preimage"], expected.get("scope")) == expected

    legacy = [
        {"name": case["name"], "identity": case["identity"]}
        for case in _corpus()["cases"]
        if not case["name"].startswith(("dfa_", "sparse_"))
    ]
    assert len(legacy) == 119
    assert hashlib.sha256(canonical_json(legacy).encode()).hexdigest() == (
        LEGACY_IDENTITY_LEDGER_SHA256
    )


def test_derived_fitted_axis_identities_reproduce_in_an_independent_process():
    corpus_path = protocol_asset("fixtures/operation-pack-v3/identities.json")
    script = """
import json
import sys
from pathlib import Path
from pybench.operation_packs import canonical_sha256

corpus = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps({
    case["name"]: canonical_sha256(case["preimage"])
    for case in corpus["cases"] if case["name"].startswith("dfa_")
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(corpus_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    observed = json.loads(result.stdout)
    expected = {
        name: case["identity"]["sha256"]
        for name, case in _cases().items()
        if name.startswith("dfa_")
    }
    assert observed == expected


def test_derived_fitted_axis_positive_records_validate_against_shared_schemas():
    cases = _cases()
    _assert_valid(_manifest_policy_validator(), cases["dfa_manifest_policy"]["value"])

    request_cases = {
        "dfa_full_parent_snapshot": "fitted_axis_parent_snapshot",
        "dfa_session_projection": "derived_fitted_session",
        "dfa_provisional_grant": "derived_fitted_axis_grant",
        "dfa_private_declaration": "derived_fitted_axis_declaration",
        "dfa_sealed_axis": "derived_fitted_axis_seal",
        "dfa_selected_axis_sidecar": "selected_axis_evidence_sidecar",
        "dfa_sealed_model_grant": "sealed_model_sibling_grant",
        "dfa_sealed_embedding_grant": "sealed_sibling_output_grant",
        "dfa_sealed_weights_grant": "sealed_sibling_output_grant",
        "dfa_full_axis_session": "derived_fitted_session",
        "dfa_full_axis_provisional_grant": "derived_fitted_axis_grant",
        "dfa_full_axis_declaration": "derived_fitted_axis_declaration",
        "dfa_full_axis_seal": "derived_fitted_axis_seal",
    }
    for case_name, definition in request_cases.items():
        _assert_valid(_request_validator(definition), cases[case_name]["value"])

    receipt_cases = {
        "dfa_model_source_linkage_v3": "model_source_linkage_v3",
        "dfa_exact_vector_coverage": "observed_sealed_sibling_coverage",
        "dfa_sparse_family_coverage": "observed_sealed_sibling_coverage",
        "dfa_derived_model_tree_observation": "derived_fitted_model_tree_observation",
        "dfa_complete_observation": "derived_fitted_axis_observation",
    }
    for case_name, definition in receipt_cases.items():
        _assert_valid(_receipt_validator(definition), cases[case_name]["value"])
    _assert_valid(
        _receipt_validator("observed_output_descriptor"),
        cases["dfa_model_observed_descriptor"]["value"],
    )
    _assert_valid(
        Draft202012Validator(_schema(REQUEST_SCHEMA)), cases["dfa_success_request"]["value"]
    )
    _assert_valid(
        Draft202012Validator(_schema(RECEIPT_SCHEMA)), cases["dfa_success_receipt"]["value"]
    )


def test_derived_fitted_axis_negative_fixture_dispositions_match_python_schema():
    corpus = _corpus()
    schemas = {
        MANIFEST_SCHEMA: _schema(MANIFEST_SCHEMA),
        REQUEST_SCHEMA: _schema(REQUEST_SCHEMA),
        RECEIPT_SCHEMA: _schema(RECEIPT_SCHEMA),
    }
    negatives = [item for item in corpus["negative_cases"] if item["name"].startswith("dfa_")]
    assert len(negatives) == 14
    for item in negatives:
        root = schemas[item["schema_file"]]
        validator = (
            Draft202012Validator(root)
            if item["definition"] == "root"
            else _subschema(item["schema_file"], "$defs", item["definition"])
        )
        if item["expected"] == "schema_reject":
            _assert_rejected(validator, item["value"])
        else:
            assert item["expected"] == "semantic_reject"
            _assert_valid(validator, item["value"])


def test_derived_fitted_axis_execution_is_unsupported_before_stage_mutation(tmp_path):
    request = _cases()["dfa_success_request"]["value"]
    stage = tmp_path / "stage"
    stage.mkdir()
    sentinel = stage / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")
    before = {path.name: path.read_bytes() for path in stage.iterdir()}

    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack_v4(request, object(), stage)  # type: ignore[arg-type]

    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "staging"
    assert {path.name: path.read_bytes() for path in stage.iterdir()} == before


def test_derived_fitted_axis_request_validation_is_explicitly_unsupported():
    request = _cases()["dfa_success_request"]["value"]
    with pytest.raises(OperationPackError, match="does not implement derived fitted-axis"):
        validate_request_v4(request, object())  # type: ignore[arg-type]
