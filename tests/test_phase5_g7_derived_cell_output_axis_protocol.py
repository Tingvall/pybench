from __future__ import annotations

import copy
import json
import subprocess
import sys

from jsonschema import Draft202012Validator

from pybench.operation_packs import canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset

MANIFEST_SCHEMA = "operation-pack-v3.schema.json"
REQUEST_SCHEMA = "operation-pack-transition-request-v3.schema.json"
RECEIPT_SCHEMA = "operation-pack-transition-receipt-v3.schema.json"

G7_CASE_NAMES = {
    "g7_manifest_declaration",
    "g7_subset_source_reference",
    "g7_subset_source_aggregate",
    "g7_linked_output_closure",
    "g7_subset_grant",
    "g7_subset_pack_declaration",
    "g7_subset_derivation",
    "g7_subset_sealed_axis",
    "g7_subset_sealed_cardinality",
    "g7_subset_observed_sealed_cardinality",
    "g7_subset_observed_descriptor",
    "g7_subset_axis_observation",
    "g7_subset_request",
    "g7_subset_receipt",
    "g7_union_manifest_declaration",
    "g7_union_selector_left",
    "g7_union_selector_right",
    "g7_union_source_reference_left",
    "g7_union_source_reference_right",
    "g7_union_source_aggregate",
    "g7_union_vector_output_grant",
    "g7_union_family_output_grant",
    "g7_union_linked_output_closure",
    "g7_union_grant",
    "g7_union_nonlexicographic_declaration",
    "g7_union_alternate_order_declaration",
    "g7_union_nonlexicographic_derivation",
    "g7_union_alternate_order_derivation",
    "g7_union_nonlexicographic_sealed_axis",
    "g7_union_alternate_order_sealed_axis",
    "g7_union_nonlexicographic_vector_cardinality",
    "g7_union_alternate_order_vector_cardinality",
    "g7_union_nonlexicographic_family_cardinality",
    "g7_union_alternate_order_family_cardinality",
    "g7_union_nonlexicographic_vector_observed_cardinality",
    "g7_union_alternate_order_vector_observed_cardinality",
    "g7_union_nonlexicographic_family_observed_cardinality",
    "g7_union_alternate_order_family_observed_cardinality",
    "g7_union_nonlexicographic_vector_descriptor",
    "g7_union_alternate_order_vector_descriptor",
    "g7_union_nonlexicographic_family_descriptor",
    "g7_union_alternate_order_family_descriptor",
    "g7_union_nonlexicographic_axis_observation",
    "g7_union_alternate_order_axis_observation",
    "g7_union_request",
    "g7_union_nonlexicographic_receipt",
    "g7_union_alternate_order_receipt",
    "g7_effective_key_primary",
    "g7_effective_key_null_fallback",
    "g7_effective_key_schema_default",
    "g7_effective_key_override",
    "g7_effective_key_same_no_presence",
    "g7_effective_key_same_with_presence",
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


def _manifest_declaration_validator() -> Draft202012Validator:
    return _subschema(
        MANIFEST_SCHEMA,
        "$defs",
        "operation",
        "properties",
        "derived_cell_output_axes",
        "items",
    )


def _request_validator(definition: str) -> Draft202012Validator:
    return _subschema(REQUEST_SCHEMA, "$defs", definition)


def _receipt_validator(definition: str) -> Draft202012Validator:
    return _subschema(RECEIPT_SCHEMA, "$defs", definition)


def _assert_valid(validator: Draft202012Validator, value: object) -> None:
    assert not list(validator.iter_errors(value))


def _assert_rejected(validator: Draft202012Validator, value: object) -> None:
    assert list(validator.iter_errors(value))


def test_g7_shared_schemas_are_valid_draft_2020_12_documents():
    for name in (MANIFEST_SCHEMA, REQUEST_SCHEMA, RECEIPT_SCHEMA):
        Draft202012Validator.check_schema(_schema(name))


def test_g7_identity_corpus_is_closed_and_reproduced_by_python():
    cases = _cases()
    g7_cases = {name: case for name, case in cases.items() if name.startswith("g7_")}
    assert set(g7_cases) == G7_CASE_NAMES
    assert len({name for name in cases if name.startswith("g3_")}) == 19
    for case in g7_cases.values():
        expected = case["identity"]
        assert canonical_sha256(case["preimage"]) == expected["sha256"]
        assert _identity(expected["scheme"], case["preimage"], expected.get("scope")) == expected


def test_g7_identity_corpus_is_reproduced_in_an_independent_process():
    corpus_path = protocol_asset("fixtures/operation-pack-v3/identities.json")
    script = """
import json
import sys
from pathlib import Path
from pybench.operation_packs import canonical_sha256

corpus = json.loads(Path(sys.argv[1]).read_text())
print(json.dumps({
    case["name"]: canonical_sha256(case["preimage"])
    for case in corpus["cases"] if case["name"].startswith("g7_")
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
        if name.startswith("g7_")
    }
    assert observed == expected


def test_g7_positive_records_validate_against_shared_subschemas():
    cases = _cases()
    manifest_validator = _manifest_declaration_validator()
    for name in ("g7_manifest_declaration", "g7_union_manifest_declaration"):
        _assert_valid(manifest_validator, cases[name]["value"])

    source_validator = _request_validator("derived_cell_source_axis_reference")
    for name in (
        "g7_subset_source_reference",
        "g7_union_source_reference_left",
        "g7_union_source_reference_right",
    ):
        _assert_valid(source_validator, cases[name]["value"])

    output_validator = _request_validator("output_descriptor")
    for name in ("g7_union_vector_output_grant", "g7_union_family_output_grant"):
        _assert_valid(output_validator, cases[name]["value"])

    grant_validator = _request_validator("derived_cell_output_axis_grant")
    _assert_valid(grant_validator, cases["g7_subset_grant"]["value"])
    _assert_valid(grant_validator, cases["g7_union_grant"]["value"])

    declaration_validator = _request_validator("derived_cell_output_axis_declaration")
    for name in (
        "g7_subset_pack_declaration",
        "g7_union_nonlexicographic_declaration",
        "g7_union_alternate_order_declaration",
    ):
        _assert_valid(declaration_validator, cases[name]["value"])

    receipt_cases = {
        "g7_subset_derivation": "derived_cell_axis_derivation",
        "g7_subset_sealed_axis": "sealed_derived_cell_axis",
        "g7_subset_sealed_cardinality": "sealed_output_cardinality_grant",
        "g7_subset_observed_sealed_cardinality": "observed_sealed_output_cardinality",
        "g7_subset_axis_observation": "derived_cell_output_axis_observation",
    }
    for order in ("nonlexicographic", "alternate_order"):
        receipt_cases.update(
            {
                f"g7_union_{order}_derivation": "derived_cell_axis_derivation",
                f"g7_union_{order}_sealed_axis": "sealed_derived_cell_axis",
                f"g7_union_{order}_vector_cardinality": "sealed_output_cardinality_grant",
                f"g7_union_{order}_family_cardinality": "sealed_output_cardinality_grant",
                f"g7_union_{order}_vector_observed_cardinality": (
                    "observed_sealed_output_cardinality"
                ),
                f"g7_union_{order}_family_observed_cardinality": (
                    "observed_sealed_output_cardinality"
                ),
                f"g7_union_{order}_axis_observation": "derived_cell_output_axis_observation",
            }
        )
    for case_name, definition in receipt_cases.items():
        _assert_valid(_receipt_validator(definition), cases[case_name]["value"])

    descriptor_validator = _receipt_validator("observed_output_descriptor")
    _assert_valid(descriptor_validator, cases["g7_subset_observed_descriptor"]["value"])
    for order in ("nonlexicographic", "alternate_order"):
        for kind in ("vector", "family"):
            _assert_valid(
                descriptor_validator,
                cases[f"g7_union_{order}_{kind}_descriptor"]["value"],
            )


def test_g7_mode_specific_transient_declarations_are_closed():
    cases = _cases()
    validator = _request_validator("derived_cell_output_axis_declaration")
    subset = cases["g7_subset_pack_declaration"]["value"]
    union = cases["g7_union_nonlexicographic_declaration"]["value"]

    invalid_subset = copy.deepcopy(subset)
    invalid_subset["member_selections"] = []
    _assert_rejected(validator, invalid_subset)

    for replacement in (None, "missing"):
        invalid_union = copy.deepcopy(union)
        if replacement == "missing":
            invalid_union.pop("member_selections")
        else:
            invalid_union["member_selections"] = replacement
        _assert_rejected(validator, invalid_union)

    duplicate = copy.deepcopy(union)
    duplicate["member_selections"][0]["selected_cell_ids"] = ["cell_2", "cell_2"]
    _assert_rejected(validator, duplicate)

    empty_final = copy.deepcopy(subset)
    empty_final["final_cell_ids"] = []
    _assert_rejected(validator, empty_final)


def test_g7_derivation_evidence_is_pinned_to_its_mode():
    cases = _cases()
    validator = _receipt_validator("derived_cell_axis_derivation")
    subset = cases["g7_subset_derivation"]["value"]

    wrong_mode = copy.deepcopy(subset)
    wrong_mode["derivation_mode"] = "declared_ordered_unique_union_v1"
    _assert_rejected(validator, wrong_mode)

    union = copy.deepcopy(subset)
    union["derivation_mode"] = "declared_ordered_unique_union_v1"
    union.pop("source_ordered_subset_status")
    union.pop("selected_count")
    union.update(
        member_subset_status="passed",
        union_membership_equality_status="passed",
        distinct_selected_membership_count=2,
        aggregate_selected_count=3,
        overlap_collapse_count=1,
    )
    _assert_valid(validator, union)

    wrong_union_mode = copy.deepcopy(union)
    wrong_union_mode["derivation_mode"] = "ordered_unique_subset_v1"
    _assert_rejected(validator, wrong_union_mode)


def test_g7_projection_and_selector_forms_are_mutually_exclusive():
    cases = _cases()
    manifest_validator = _manifest_declaration_validator()
    declaration = cases["g7_manifest_declaration"]["value"]

    family_without_selector = copy.deepcopy(declaration)
    family_without_selector["source"]["projection"] = "family_key_cell_axis_v1"
    _assert_rejected(manifest_validator, family_without_selector)

    vector_with_selector = copy.deepcopy(declaration)
    vector_with_selector["source"]["family_key_selector"] = {
        "mode": "literal",
        "key": "score",
    }
    _assert_rejected(manifest_validator, vector_with_selector)

    reference_validator = _request_validator("derived_cell_source_axis_reference")
    reference = cases["g7_subset_source_reference"]["value"]
    family_reference_without_selector = copy.deepcopy(reference)
    family_reference_without_selector["projection"] = "family_key_cell_axis_v1"
    _assert_rejected(reference_validator, family_reference_without_selector)

    vector_reference_with_selector = copy.deepcopy(reference)
    vector_reference_with_selector["source_axis_selector_identity"] = {
        "scheme": "scworkbench.operation_pack.derived_cell_axis_selector.v1",
        "sha256": "0" * 64,
    }
    _assert_rejected(reference_validator, vector_reference_with_selector)


def test_g7_union_order_is_pack_declared_array_identity_not_set_iteration():
    cases = _cases()
    first = cases["g7_union_nonlexicographic_declaration"]
    second = cases["g7_union_alternate_order_declaration"]
    first_ids = first["value"]["final_cell_ids"]
    second_ids = second["value"]["final_cell_ids"]

    assert first_ids == ["cell_1", "cell_10", "cell_2"]
    assert second_ids == ["cell_2", "cell_1", "cell_10"]
    assert set(first_ids) == set(second_ids)
    assert first["identity"] != second["identity"]
    assert canonical_sha256(first["preimage"]) != canonical_sha256(second["preimage"])

    insertion_reordered = dict(reversed(list(first["preimage"].items())))
    assert canonical_sha256(insertion_reordered) == first["identity"]["sha256"]
    array_reordered = copy.deepcopy(first["preimage"])
    array_reordered["final_cell_ids"] = list(reversed(array_reordered["final_cell_ids"]))
    assert canonical_sha256(array_reordered) != first["identity"]["sha256"]

    order_sensitive_suffixes = (
        "declaration",
        "derivation",
        "sealed_axis",
        "vector_cardinality",
        "family_cardinality",
        "vector_observed_cardinality",
        "family_observed_cardinality",
        "vector_descriptor",
        "family_descriptor",
        "axis_observation",
        "receipt",
    )
    for suffix in order_sensitive_suffixes:
        assert (
            cases[f"g7_union_nonlexicographic_{suffix}"]["identity"]
            != cases[f"g7_union_alternate_order_{suffix}"]["identity"]
        )

    first_family = cases["g7_union_nonlexicographic_family_descriptor"]["preimage"]
    second_family = cases["g7_union_alternate_order_family_descriptor"]["preimage"]
    assert first_family["content_identity"] != second_family["content_identity"]
    assert first_family["payload_manifest_identity"] != second_family["payload_manifest_identity"]
    assert first_family["sealed_output_cardinality_grant"]["key_axis"] == [
        "score",
        "confidence",
    ]
    assert first_family["sealed_output_cardinality_grant"]["expected_row_count"] == 6


def test_g7_shared_fixture_contains_no_host_path_or_runtime_authority():
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    corpus = _corpus()
    collect([case for case in corpus["cases"] if case["name"].startswith("g7_")])
    collect(corpus["g7_positive_examples"])
    forbidden = ("/home/", "/tmp/", "file://", "localhost", "state_authority_handle")
    assert not [
        value for value in values if value.startswith("/") or any(x in value for x in forbidden)
    ]


def test_g7_closed_records_reject_unknown_fields_and_v1_cardinality_substitution():
    cases = _cases()
    declaration = copy.deepcopy(cases["g7_manifest_declaration"]["value"])
    declaration["trajectory_filter"] = "not-core-authority"
    _assert_rejected(_manifest_declaration_validator(), declaration)

    sealed = copy.deepcopy(cases["g7_subset_sealed_cardinality"]["value"])
    sealed["schema"] = "scworkbench.operation_pack.output_cardinality_grant.v1"
    _assert_rejected(_receipt_validator("sealed_output_cardinality_grant"), sealed)

    observed = copy.deepcopy(cases["g7_subset_observed_sealed_cardinality"]["value"])
    observed["ambient_bundle_path"] = "/private/bundle"
    _assert_rejected(_receipt_validator("observed_sealed_output_cardinality"), observed)
