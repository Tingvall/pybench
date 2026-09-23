from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.operation_packs_v3 import validate_manifest_v3
from pybench.protocol_v4 import execute_operation_pack_v4, validate_request_v4

SPARSE_DOMAINS = {
    "scworkbench.operation_pack.standalone_sparse_cell_family_policy.v1",
    "scworkbench.operation_pack.sparse_family_source_axis.v1",
    "scworkbench.operation_pack.sparse_family_g1_axis_evidence.v2",
    "scworkbench.operation_pack.sparse_family_g1_axis_evidence.v3",
    "scworkbench.operation_pack.sparse_family_provisional_grant.v1",
    "scworkbench.operation_pack.sparse_family_layout_declaration.v1",
    "scworkbench.operation_pack.sparse_family_pair_order.v1",
    "scworkbench.operation_pack.sparse_family_layout_seal.v1",
    "scworkbench.operation_pack.sparse_family_sealed_output_grant.v1",
    "scworkbench.operation_pack.observed_sparse_cell_family.v1",
}
REFERENCE_MANIFEST = (
    Path(__file__).parent
    / "fixtures"
    / "operation-pack-v3-python-reference"
    / "operation-pack.json"
)


def _corpus() -> dict:
    return json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())


def test_python_manifest_sparse_policy_is_rejected_at_admission():
    manifest = json.loads(REFERENCE_MANIFEST.read_text())
    operation = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::all_contracts"
    )
    policy = {
        "schema": "scworkbench.operation_pack.standalone_sparse_cell_family_policy.v1",
        "output": "cell_vector_family",
        "policy": "standalone_sparse_cell_family_v1",
        "axis_source": {"source": "typed_space_input_v1", "input": "space"},
        "key_order": "declared_verbatim_v1",
        "row_order": "key_major_then_source_relative_cell_v1",
        "per_key_coverage": "per_key_source_relative_subset_v1",
        "missing_values": "forbidden",
        "declaration_discipline": "exactly_once_before_write",
        "maximum_axis_cells": 1_000_000,
        "maximum_keys": 64,
        "maximum_rows": 1_000_000,
        "maximum_id_bytes": 1_024,
        "maximum_control_bytes": 67_108_864,
    }
    policy["policy_identity"] = _identity(
        policy["schema"], policy, "policy excluding policy_identity"
    )
    operation.setdefault("dimension_policies", []).append(policy)

    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(manifest)

    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "semantic_admission"


def test_forged_sparse_family_grant_rejects_before_stage_access(tmp_path):
    request = next(
        case["value"] for case in _corpus()["cases"] if case["name"] == "sparse_success_request"
    )
    stage = tmp_path / "uncreated-stage"

    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack_v4(request, object(), stage)  # type: ignore[arg-type]

    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "staging"
    assert not stage.exists()

    with pytest.raises(OperationPackError) as caught:
        validate_request_v4(request, object())  # type: ignore[arg-type]
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"


def test_structured_pair_identity_distinguishes_separator_collision_and_key_order():
    first = [{"key": "a\x1cb", "cell_id": "c"}]
    second = [{"key": "a", "cell_id": "b\x1cc"}]
    assert "\x1c".join(first[0].values()) == "\x1c".join(second[0].values())
    assert canonical_sha256(first) != canonical_sha256(second)

    key_major = [{"key": "L2", "cell_id": "c1"}, {"key": "L1", "cell_id": "c2"}]
    assert canonical_sha256(key_major) != canonical_sha256(list(reversed(key_major)))


def test_sparse_family_identity_stages_match_in_a_separate_python_process():
    cases = {
        case["name"]: case
        for case in _corpus()["cases"]
        if case["identity"]["scheme"] in SPARSE_DOMAINS
        or "standalone_sparse_cell_family_grants" in case["preimage"]
        or "sparse_cell_family_observations" in case["preimage"]
    }
    assert len(cases) == 17
    observed_domains = {case["identity"]["scheme"] for case in cases.values()}
    assert observed_domains == SPARSE_DOMAINS | {
        "scworkbench.operation_pack.transition_request.v3",
        "scworkbench.operation_pack.transition_receipt.v3",
    }
    assert any(
        "standalone_sparse_cell_family_grants" in case["preimage"] for case in cases.values()
    )
    assert any("sparse_cell_family_observations" in case["preimage"] for case in cases.values())
    for case in cases.values():
        expected = case["identity"]
        assert canonical_sha256(case["preimage"]) == expected["sha256"]
        assert _identity(expected["scheme"], case["preimage"], expected.get("scope")) == expected

    script = """
import json
import sys
from pathlib import Path
from pybench.operation_packs import canonical_sha256

corpus = json.loads(Path(sys.argv[1]).read_text())
names = set(json.loads(sys.argv[2]))
print(json.dumps({
    case["name"]: canonical_sha256(case["preimage"])
    for case in corpus["cases"] if case["name"] in names
}, sort_keys=True))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(protocol_asset("fixtures/operation-pack-v3/identities.json")),
            json.dumps(sorted(cases)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        name: case["identity"]["sha256"] for name, case in cases.items()
    }


def test_sparse_family_shared_schemas_and_positive_records_are_closed():
    roots = {
        name: json.loads(protocol_asset(name).read_text())
        for name in (
            "operation-pack-v3.schema.json",
            "operation-pack-transition-request-v3.schema.json",
            "operation-pack-transition-receipt-v3.schema.json",
        )
    }
    for root in roots.values():
        Draft202012Validator.check_schema(root)
    cases = {case["name"]: case["value"] for case in _corpus()["cases"]}

    def validate_def(file: str, definition: str, value: object) -> None:
        schema = copy.deepcopy(roots[file]["$defs"][definition])
        schema["$defs"] = roots[file]["$defs"]
        assert not list(Draft202012Validator(schema).iter_errors(value))

    manifest_file = "operation-pack-v3.schema.json"
    request_file = "operation-pack-transition-request-v3.schema.json"
    receipt_file = "operation-pack-transition-receipt-v3.schema.json"
    validate_def(manifest_file, "dimension_policy", cases["sparse_manifest_policy"])
    for name, definition in (
        ("sparse_typed_source_axis", "sparse_family_source_axis"),
        ("sparse_g1_axis_evidence_v2", "sparse_family_g1_axis_evidence"),
        ("sparse_g1_axis_evidence_v3", "sparse_family_g1_axis_evidence"),
        ("sparse_g1_source_axis_v2", "sparse_family_source_axis"),
        ("sparse_g1_source_axis_v3", "sparse_family_source_axis"),
        ("sparse_provisional_grant", "standalone_sparse_cell_family_grant"),
        ("sparse_layout_declaration", "sparse_family_layout_declaration"),
    ):
        validate_def(request_file, definition, cases[name])
    for name, definition in (
        ("sparse_layout_seal", "sparse_cell_family_layout_seal"),
        ("sparse_sealed_writer_grant", "sparse_cell_family_sealed_output_grant"),
        ("sparse_observed_output", "sparse_cell_family_observation"),
    ):
        validate_def(receipt_file, definition, cases[name])
    assert not list(
        Draft202012Validator(roots[request_file]).iter_errors(cases["sparse_success_request"])
    )
    assert not list(
        Draft202012Validator(roots[receipt_file]).iter_errors(cases["sparse_success_receipt"])
    )


def test_sparse_family_negative_schema_corpus_rejects():
    roots = {
        name: json.loads(protocol_asset(name).read_text())
        for name in (
            "operation-pack-v3.schema.json",
            "operation-pack-transition-request-v3.schema.json",
            "operation-pack-transition-receipt-v3.schema.json",
        )
    }
    negatives = [
        case
        for case in _corpus()["negative_cases"]
        if case["name"].startswith("sparse_") and case["expected"] == "schema_reject"
    ]
    assert len(negatives) == 5
    for case in negatives:
        root = roots[case["schema_file"]]
        schema = copy.deepcopy(root["$defs"][case["definition"]])
        schema["$defs"] = root["$defs"]
        assert list(Draft202012Validator(schema).iter_errors(case["value"])), case["name"]


def test_sparse_family_structured_pairs_and_self_rehashed_negatives_are_distinct():
    cases = {case["name"]: case for case in _corpus()["cases"]}
    assert (
        cases["sparse_structured_pair_order"]["identity"]
        != cases["sparse_swapped_key_pair_order"]["identity"]
    )
    assert (
        cases["sparse_separator_pair_left"]["identity"]
        != cases["sparse_separator_pair_right"]["identity"]
    )
    negatives = {
        case["name"]: case
        for case in _corpus()["negative_cases"]
        if case["name"].startswith("sparse_") and case["expected"] == "semantic_reject"
    }
    assert len(negatives) == 3
    policy = cases["sparse_manifest_policy"]["value"]
    assert (
        negatives["sparse_policy_self_hash_wrong"]["value"]["policy_identity"]
        != policy["policy_identity"]
    )
    declaration = cases["sparse_layout_declaration"]["value"]
    swapped = negatives["sparse_declaration_keys_swapped_self_rehashed"]["value"]
    assert canonical_sha256(swapped["ordered_keys"]) != canonical_sha256(
        declaration["ordered_keys"]
    )
    assert swapped["per_key_cells"] == declaration["per_key_cells"]
    observation = cases["sparse_observed_output"]["value"]
    attack = negatives["sparse_receipt_keys_swapped_self_rehashed"]["value"]
    assert (
        canonical_sha256({k: v for k, v in attack.items() if k != "observation_identity"})
        == (attack["observation_identity"]["sha256"])
    )
    assert attack["observed_keys"] != observation["seal"]["ordered_keys"]


def test_120_cell_two_sparse_80_row_keys_are_not_cartesian_padded():
    source = [f"cell_{index}" for index in range(1, 121)]
    keys = ["lineage_z", "lineage_a"]
    pairs = [{"key": key, "cell_id": cell} for key in keys for cell in source[:80]]
    assert len(pairs) == 160
    assert len(pairs) != len(source) * len(keys)
    assert [pair["key"] for pair in pairs] == [keys[0]] * 80 + [keys[1]] * 80


def test_g1_model_read_schema_accepts_v2_and_v3_linkage_but_rejects_mixed_evidence():
    corpus = {case["name"]: case["value"] for case in _corpus()["cases"]}
    grant = copy.deepcopy(corpus["self_linked_model_read_grant"])
    root = json.loads(
        protocol_asset("operation-pack-transition-request-v3.schema.json").read_text()
    )
    schema = copy.deepcopy(root["$defs"]["self_linked_contained_model_read_grant"])
    schema["$defs"] = root["$defs"]
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(grant))

    v3_linkage = copy.deepcopy(corpus["dfa_model_source_linkage_v3"])
    grant["model_source_linkage"] = v3_linkage
    grant["model_source_linkage_identity"] = {
        "scheme": v3_linkage["linkage_identity"]["scheme"],
        "sha256": v3_linkage["linkage_identity"]["sha256"],
    }
    grant["grant_identity"] = _identity(
        grant["schema"],
        {key: value for key, value in grant.items() if key != "grant_identity"},
        "grant excluding grant_identity",
    )
    assert not list(validator.iter_errors(grant))

    mixed = copy.deepcopy(grant)
    mixed["model_source_linkage"]["source_snapshot_id"] = "v2-only-field"
    assert list(validator.iter_errors(mixed))
    missing = copy.deepcopy(grant)
    missing["model_source_linkage"].pop("selected_axis_evidence")
    assert list(validator.iter_errors(missing))


def test_g1_sibling_write_schema_accepts_v2_and_v3_linkage_but_rejects_mixed_evidence():
    corpus = {case["name"]: case["value"] for case in _corpus()["cases"]}
    root = json.loads(
        protocol_asset("operation-pack-transition-request-v3.schema.json").read_text()
    )
    schema = copy.deepcopy(root["$defs"]["contained_model_sibling_write_grant"])
    schema["$defs"] = root["$defs"]
    validator = Draft202012Validator(schema)
    grant = copy.deepcopy(corpus["contained_model_sibling_write_grant"])
    assert not list(validator.iter_errors(grant))

    grant["model_source_linkage"] = copy.deepcopy(corpus["dfa_model_source_linkage_v3"])
    assert not list(validator.iter_errors(grant))
    mixed = copy.deepcopy(grant)
    mixed["model_source_linkage"]["source_snapshot_id"] = "v2-only-field"
    assert list(validator.iter_errors(mixed))
    missing = copy.deepcopy(grant)
    missing["model_source_linkage"].pop("selected_axis_evidence")
    assert list(validator.iter_errors(missing))
