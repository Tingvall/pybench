from __future__ import annotations

import copy
import json

from jsonschema import Draft202012Validator

from pybench.operation_packs import canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset

MANIFEST_SCHEMA = "operation-pack-v3.schema.json"
REQUEST_SCHEMA = "operation-pack-transition-request-v3.schema.json"
RECEIPT_SCHEMA = "operation-pack-transition-receipt-v3.schema.json"

EXPECTED_IDENTITIES = {
    "g7_effective_key_primary": "41fc556b8956f70752b78ecdd2ec0fc6e16f5653fc631b9fe21a29b771602f1a",
    "g7_effective_key_null_fallback": (
        "c7e26a6987eaeba2131a574c6e8f3732886d71bbdd16a0e28d310b7974bd071d"
    ),
    "g7_effective_key_schema_default": (
        "3879604c3d7939422b4bfd83cb1f20449949b5aa60ce7c244e795fd4d97aeeae"
    ),
    "g7_effective_key_override": "33672c5b3f08411bea4605b38e445165e4eb4ff9cbe5824d2719a415b915b6a4",
    "g7_effective_key_same_no_presence": (
        "3207954905f3d149e9a05f2c35587324efe4590871e1b0c57d37d5ac88deed0f"
    ),
    "g7_effective_key_same_with_presence": (
        "f939e5ed81f919558b602fa151cc6c215811d40f77dad739d9a568ab9bcf67d4"
    ),
}


def _schema(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text())


def _fixture() -> dict:
    return json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())


def _subschema(name: str, *path: str) -> Draft202012Validator:
    root = _schema(name)
    value = root
    for key in path:
        value = value[key]
    value = copy.deepcopy(value)
    value["$schema"] = root["$schema"]
    value["$defs"] = root["$defs"]
    return Draft202012Validator(value)


def _manifest_effective_key_validator() -> Draft202012Validator:
    return _subschema(
        MANIFEST_SCHEMA,
        "$defs",
        "operation",
        "properties",
        "derived_cell_output_axes",
        "items",
        "properties",
        "linked_outputs",
        "items",
        "oneOf",
        1,
        "properties",
        "key_axis",
        "oneOf",
        4,
    )


def _request_resolution_validator() -> Draft202012Validator:
    return _subschema(
        REQUEST_SCHEMA,
        "$defs",
        "derived_cell_linked_output_policy",
        "properties",
        "resolved_key_axis",
        "oneOf",
        0,
    )


def _receipt_resolution_validator() -> Draft202012Validator:
    return _subschema(
        RECEIPT_SCHEMA,
        "$defs",
        "sealed_output_cardinality_grant",
        "properties",
        "key_axis_resolution",
    )


def _errors(validator: Draft202012Validator, value: object) -> list:
    return list(validator.iter_errors(value))


def _resolved_key(declaration: dict, parameters: dict, presence: dict[str, bool]) -> str:
    candidates = declaration["parameter_candidates"]
    selected_parameter = next(
        (name for name in candidates if parameters.get(name) is not None),
        None,
    )
    if selected_parameter is None:
        raise ValueError("no non-null candidate")

    override = declaration.get("input_presence_override")
    if override is not None:
        if override["present"] == override["absent"]:
            raise ValueError("presence authorities must be distinct")
        if presence.get(override["present"], False) and not presence.get(override["absent"], False):
            selected_parameter = override["use_parameter"]

    selected = parameters.get(selected_parameter)
    if not isinstance(selected, str) or not selected:
        raise ValueError("selected key must be a nonempty string")
    return selected


def test_g7_effective_key_positive_fixture_identities_are_exact():
    fixture = _fixture()
    cases = {
        case["name"]: case
        for case in fixture["cases"]
        if case["name"].startswith("g7_effective_key_")
    }
    assert set(cases) == set(EXPECTED_IDENTITIES)
    assert set(fixture["g7_effective_key_positive_examples"]) == {
        "primary",
        "null_fallback",
        "schema_default",
        "override",
        "same_key_no_presence",
        "same_key_with_presence",
    }
    for name, expected_sha256 in EXPECTED_IDENTITIES.items():
        case = cases[name]
        assert case["identity"] == {
            "scheme": "scworkbench.operation_pack.resolved_key_axis_policy.v1",
            "sha256": expected_sha256,
        }
        assert canonical_sha256(case["preimage"]) == expected_sha256
        assert _identity(case["identity"]["scheme"], case["preimage"]) == case["identity"]


def test_g7_effective_key_positive_records_validate_in_all_shared_schemas():
    positive = _fixture()["g7_effective_key_positive_examples"]
    declaration_validator = _manifest_effective_key_validator()
    request_validator = _request_resolution_validator()
    receipt_validator = _receipt_resolution_validator()
    for example in positive.values():
        assert not _errors(declaration_validator, example["value"]["declaration"])
        assert not _errors(request_validator, example["value"])
        assert not _errors(receipt_validator, example["value"])


def test_g7_effective_key_manifest_schema_negatives_are_rejected():
    negatives = {case["name"]: case for case in _fixture()["g7_effective_key_negative_examples"]}
    assert set(negatives) == {
        "duplicate_candidates",
        "too_many_candidates",
        "empty_candidate",
        "ambiguous_override_inputs",
        "empty_effective_value",
        "self_rehashed_wrong_branch",
    }
    validator = _manifest_effective_key_validator()
    for name in ("duplicate_candidates", "too_many_candidates", "empty_candidate"):
        assert _errors(validator, negatives[name]["declaration"])

    ambiguous = negatives["ambiguous_override_inputs"]
    assert not _errors(validator, ambiguous["declaration"])
    try:
        _resolved_key(ambiguous["declaration"], {"primary_key": "a"}, {})
    except ValueError as error:
        assert str(error) == "presence authorities must be distinct"
    else:
        raise AssertionError("ambiguous presence authority was accepted")


def test_g7_effective_key_empty_resolved_value_is_schema_and_semantic_invalid():
    fixture = _fixture()
    negative = next(
        item
        for item in fixture["g7_effective_key_negative_examples"]
        if item["name"] == "empty_effective_value"
    )
    candidate = copy.deepcopy(fixture["g7_effective_key_positive_examples"]["primary"]["value"])
    candidate["declaration"] = negative["declaration"]
    candidate["keys"] = [""]
    candidate["resolution"]["effective_candidate_values"][0]["value"] = ""
    candidate["resolution"]["selected_key"] = ""
    assert _errors(_request_resolution_validator(), candidate)
    assert _errors(_receipt_resolution_validator(), candidate)
    try:
        _resolved_key(
            negative["declaration"],
            negative["effective_parameters"],
            negative["input_presence"],
        )
    except ValueError as error:
        assert str(error) == "selected key must be a nonempty string"
    else:
        raise AssertionError("empty effective key was accepted")


def test_g7_effective_key_self_rehashed_wrong_branch_is_detected():
    negative = next(
        item
        for item in _fixture()["g7_effective_key_negative_examples"]
        if item["name"] == "self_rehashed_wrong_branch"
    )
    expected = _resolved_key(
        negative["declaration"],
        negative["effective_parameters"],
        negative["input_presence"],
    )
    assert expected == "L1"
    assert negative["substituted_key"] == "custom"
    assert negative["substituted_key"] != expected


def test_g7_effective_key_presence_evidence_and_array_order_are_identity_bearing():
    fixture = _fixture()
    cases = {case["name"]: case for case in fixture["cases"]}
    absent = cases["g7_effective_key_same_no_presence"]
    present = cases["g7_effective_key_same_with_presence"]
    assert absent["value"]["keys"] == present["value"]["keys"] == ["same"]
    assert absent["identity"] != present["identity"]

    primary = cases["g7_effective_key_primary"]
    object_reordered = dict(reversed(list(primary["preimage"].items())))
    assert canonical_sha256(object_reordered) == primary["identity"]["sha256"]
    array_reordered = copy.deepcopy(primary["preimage"])
    array_reordered["declaration"]["parameter_candidates"].reverse()
    assert canonical_sha256(array_reordered) != primary["identity"]["sha256"]


def test_g7_effective_key_fixtures_are_path_free_and_do_not_add_runtime_authority():
    fixture = _fixture()
    values = json.dumps(
        {
            "positive": fixture["g7_effective_key_positive_examples"],
            "negative": fixture["g7_effective_key_negative_examples"],
        },
        sort_keys=True,
    )
    for forbidden in ("/home/", "/tmp/", "file://", "state_authority_handle"):
        assert forbidden not in values
