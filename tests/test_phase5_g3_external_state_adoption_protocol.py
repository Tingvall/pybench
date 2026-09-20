from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset

MANIFEST_SCHEMA = "operation-pack-v3.schema.json"
REQUEST_SCHEMA = "operation-pack-transition-request-v3.schema.json"
RECEIPT_SCHEMA = "operation-pack-transition-receipt-v3.schema.json"

G3_CASE_NAMES = {
    "g3_declaration",
    "g3_source_snapshot",
    "g3_candidate_snapshot",
    "g3_adopted_closure",
    "g3_byte_evidence_verified",
    "g3_grant_verified",
    "g3_observation_verified",
    "g3_request_verified",
    "g3_receipt_verified",
    "g3_byte_evidence_not_inspected",
    "g3_grant_not_inspected",
    "g3_observation_not_inspected",
    "g3_request_not_inspected",
    "g3_receipt_not_inspected",
    "g3_byte_evidence_unavailable",
    "g3_grant_unavailable",
    "g3_observation_unavailable",
    "g3_request_unavailable",
    "g3_receipt_unavailable",
}


def _schema(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text())


def _corpus_cases() -> dict[str, dict]:
    corpus = json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())
    return {case["name"]: case for case in corpus["cases"]}


def _subschema(name: str, *path: str) -> Draft202012Validator:
    root = _schema(name)
    value = root
    for key in path:
        value = value[key]
    value = copy.deepcopy(value)
    value["$schema"] = root["$schema"]
    value["$defs"] = root["$defs"]
    return Draft202012Validator(value)


def _declaration_validator() -> Draft202012Validator:
    return _subschema(
        MANIFEST_SCHEMA,
        "$defs",
        "operation",
        "properties",
        "supported_external_state_adoptions",
        "items",
    )


def _grant_validator() -> Draft202012Validator:
    return _subschema(REQUEST_SCHEMA, "$defs", "supported_external_state_adoption_grant")


def _observation_validator() -> Draft202012Validator:
    return _subschema(RECEIPT_SCHEMA, "$defs", "supported_external_state_adoption_observation")


def _assert_rejected(validator: Draft202012Validator, value: object) -> None:
    assert list(validator.iter_errors(value))


def test_g3_shared_schemas_are_valid_draft_2020_12_documents():
    for name in (MANIFEST_SCHEMA, REQUEST_SCHEMA, RECEIPT_SCHEMA):
        Draft202012Validator.check_schema(_schema(name))


def test_g3_identity_corpus_is_closed_and_reproduced_by_python():
    cases = _corpus_cases()
    g3_cases = {name: case for name, case in cases.items() if name.startswith("g3_")}
    assert set(g3_cases) == G3_CASE_NAMES
    for case in g3_cases.values():
        expected = case["identity"]
        assert canonical_sha256(case["preimage"]) == expected["sha256"]
        assert _identity(expected["scheme"], case["preimage"], expected.get("scope")) == expected


@pytest.mark.parametrize("state", ["verified", "not_inspected", "unavailable"])
def test_g3_all_source_byte_states_have_valid_grants_and_observations(state: str):
    cases = _corpus_cases()
    assert not list(_grant_validator().iter_errors(cases[f"g3_grant_{state}"]["value"]))
    assert not list(_observation_validator().iter_errors(cases[f"g3_observation_{state}"]["value"]))


def test_g3_declaration_and_shared_nested_tuples_are_schema_valid():
    cases = _corpus_cases()
    assert not list(_declaration_validator().iter_errors(cases["g3_declaration"]["value"]))
    nested = (
        (REQUEST_SCHEMA, "external_source_adoption_snapshot", "g3_source_snapshot"),
        (REQUEST_SCHEMA, "external_source_byte_evidence", "g3_byte_evidence_verified"),
        (REQUEST_SCHEMA, "external_state_candidate_snapshot", "g3_candidate_snapshot"),
        (REQUEST_SCHEMA, "supported_external_state_adoption_closure", "g3_adopted_closure"),
    )
    for schema_name, definition, case_name in nested:
        validator = _subschema(schema_name, "$defs", definition)
        assert not list(validator.iter_errors(cases[case_name]["value"]))


@pytest.mark.parametrize(
    ("case_name", "field", "replacement"),
    [
        ("g3_grant_verified", "observed_content_identity", None),
        (
            "g3_grant_not_inspected",
            "observed_content_identity",
            {"scheme": "scworkbench.raw_file.sha256.v1", "sha256": "0" * 64},
        ),
        (
            "g3_grant_unavailable",
            "nonverification_reason",
            "policy_did_not_inspect_external_bytes",
        ),
    ],
)
def test_g3_source_byte_evidence_states_are_closed(case_name: str, field: str, replacement: object):
    grant = copy.deepcopy(_corpus_cases()[case_name]["value"])
    grant["source_byte_evidence"][field] = replacement
    _assert_rejected(_grant_validator(), grant)


@pytest.mark.parametrize(
    ("target", "field", "replacement"),
    [
        ("declaration", "ambient_path", "/private/source"),
        ("declaration", "primary_path", "/private/model.rds"),
        ("source_snapshot", "source_path", "/private/source"),
        ("candidate_snapshot", "relative_root", "/tmp/candidate"),
        ("grant", "candidate_handle", {"path": "/tmp/candidate"}),
    ],
)
def test_g3_protocol_rejects_host_paths_and_mutable_handles(
    target: str, field: str, replacement: object
):
    cases = _corpus_cases()
    if target == "declaration":
        value = copy.deepcopy(cases["g3_declaration"]["value"])
        validator = _declaration_validator()
    else:
        value = copy.deepcopy(cases["g3_grant_verified"]["value"])
        validator = _grant_validator()
        if target != "grant":
            value = value[target]
            validator = _subschema(
                REQUEST_SCHEMA,
                "$defs",
                "external_source_adoption_snapshot"
                if target == "source_snapshot"
                else "external_state_candidate_snapshot",
            )
    value[field] = replacement
    _assert_rejected(validator, value)


def test_g3_attestations_and_top_level_cardinality_are_closed():
    cases = _corpus_cases()
    observation = copy.deepcopy(cases["g3_observation_verified"]["value"])
    observation["pack_no_refit_attestation"] = False
    _assert_rejected(_observation_validator(), observation)

    observation = copy.deepcopy(cases["g3_observation_verified"]["value"])
    del observation["pack_source_association_attestation"]
    _assert_rejected(_observation_validator(), observation)

    grants = _subschema(REQUEST_SCHEMA, "properties", "supported_external_state_adoption_grants")
    grant = cases["g3_grant_verified"]["value"]
    _assert_rejected(grants, [grant, grant])

    observations = _subschema(
        RECEIPT_SCHEMA, "properties", "supported_external_state_adoption_observations"
    )
    observed = cases["g3_observation_verified"]["value"]
    _assert_rejected(observations, [observed, observed])


def test_g3_identity_preimages_detect_source_and_adoption_substitution():
    cases = _corpus_cases()
    mutations = (
        ("g3_source_snapshot", ("source_run_id",), "run-substituted"),
        (
            "g3_candidate_snapshot",
            ("tree_manifest_identity", "sha256"),
            "0" * 64,
        ),
        (
            "g3_adopted_closure",
            ("model_source_linkage_identity", "sha256"),
            "0" * 64,
        ),
        (
            "g3_observation_verified",
            ("adopted_model_tree_manifest_identity", "sha256"),
            "0" * 64,
        ),
    )
    for case_name, path, replacement in mutations:
        case = cases[case_name]
        preimage = copy.deepcopy(case["preimage"])
        target = preimage
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        assert canonical_sha256(preimage) != case["identity"]["sha256"]


def test_g3_adopted_tree_and_payload_identities_equal_authenticated_candidate():
    cases = _corpus_cases()
    for state in ("verified", "not_inspected", "unavailable"):
        grant = cases[f"g3_grant_{state}"]["value"]
        observation = cases[f"g3_observation_{state}"]["value"]
        assert (
            observation["adopted_model_tree_manifest_identity"]
            == grant["candidate_snapshot"]["tree_manifest_identity"]
        )
        assert (
            observation["adopted_model_payload_manifest_identity"]
            == grant["candidate_snapshot"]["payload_manifest_identity"]
        )
