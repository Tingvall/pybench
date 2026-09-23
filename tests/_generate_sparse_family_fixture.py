"""Deterministically append the standalone sparse-family protocol corpus.

Run with ``PYTHONPATH=src python tests/_generate_sparse_family_fixture.py``.
The pre-existing corpus is left byte-for-byte unchanged except for new cases.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from pybench.operation_packs import canonical_sha256

CORPUS = Path("protocol/fixtures/operation-pack-v3/identities.json")
PREFIX = "scworkbench.operation_pack."


def identity(scheme: str, value: object, scope: str | None = None) -> dict[str, str]:
    result = {"scheme": scheme, "sha256": canonical_sha256(value)}
    if scope is not None:
        result["scope"] = scope
    return result


def reference(value: dict[str, str]) -> dict[str, str]:
    return {"scheme": value["scheme"], "sha256": value["sha256"]}


def self_identified(
    cases: list[dict],
    name: str,
    value: dict,
    scheme: str,
    field: str,
    scope: str,
) -> dict:
    preimage = copy.deepcopy(value)
    preimage.pop(field, None)
    value[field] = identity(scheme, preimage, scope)
    cases.append(
        {
            "name": name,
            "preimage": preimage,
            "identity": value[field],
            "value": copy.deepcopy(value),
        }
    )
    return value


def main() -> None:
    corpus = json.loads(CORPUS.read_text())
    previous = {case["name"]: case for case in corpus["cases"]}
    corpus["cases"] = [case for case in corpus["cases"] if not case["name"].startswith("sparse_")]
    corpus["negative_cases"] = [
        case for case in corpus["negative_cases"] if not case["name"].startswith("sparse_")
    ]
    cases = corpus["cases"]
    negatives = corpus["negative_cases"]

    policy = {
        "schema": PREFIX + "standalone_sparse_cell_family_policy.v1",
        "output": "sparse_family",
        "policy": "standalone_sparse_cell_family_v1",
        "axis_source": {"source": "typed_space_input_v1", "input": "source"},
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
    self_identified(
        cases,
        "sparse_manifest_policy",
        policy,
        policy["schema"],
        "policy_identity",
        "policy excluding policy_identity",
    )
    cells = ["cell_2", "cell_10", "cell_1", "x|y", "z"]
    original_request = previous["dfa_success_request"]["value"]
    source_input = original_request["inputs"][0]["descriptor"]
    typed_source = {
        "schema": PREFIX + "sparse_family_source_axis.v1",
        "source_kind": "typed_space_input_v1",
        "input_name": "source",
        "source_space_id": "pca",
        "input_descriptor_identity": reference(source_input["descriptor_identity"]),
        "input_content_identity": source_input["content_identity"],
        "ordered_cells": cells,
        "cell_order_identity": identity("scworkbench.cell_order.v1", cells),
        "source_state_identity": original_request["transition"]["source_state_identity"],
    }
    self_identified(
        cases,
        "sparse_typed_source_axis",
        typed_source,
        typed_source["schema"],
        "source_axis_identity",
        "source axis excluding source_axis_identity",
    )

    g1_evidence = []
    for version, fields in (
        (
            "v2",
            {
                "verified_source_space_snapshot_identity": identity(
                    "scworkbench.operation_pack.model_source_snapshot.v2", cells
                ),
                "verified_full_axis_order_identity": identity("scworkbench.cell_order.v1", cells),
            },
        ),
        (
            "v3",
            {
                "verified_parent_snapshot_identity": identity(
                    "scworkbench.operation_pack.fitted_axis_parent_snapshot.v1", cells
                ),
                "selected_axis_seal_identity": identity(
                    PREFIX + "derived_fitted_axis_seal.v1", cells[:4]
                ),
                "selected_axis_sidecar_identity": identity(
                    PREFIX + "selected_axis_evidence.v1", cells[:4]
                ),
            },
        ),
    ):
        scheme = PREFIX + "sparse_family_g1_axis_evidence." + version
        evidence = {"schema": scheme, **fields}
        self_identified(
            cases,
            "sparse_g1_axis_evidence_" + version,
            evidence,
            scheme,
            "evidence_identity",
            "evidence excluding evidence_identity",
        )
        g1_evidence.append(evidence)
        fitted = cells if version == "v2" else cells[:4]
        model_source = {
            "schema": PREFIX + "sparse_family_source_axis.v1",
            "source_kind": "verified_g1_model_fitted_axis_v1",
            "model_read_name": "model_read",
            "model_descriptor_identity": identity(
                PREFIX + "input_descriptor.v2", [version, "model"]
            ),
            "model_content_identity": identity(
                "scworkbench.model_ref.content.v1", [version, "model"]
            ),
            "g1_read_grant_identity": identity(
                PREFIX + "self_linked_contained_model_read_grant.v1", version
            ),
            "model_linkage_identity": identity(PREFIX + "model_source_linkage.v3", version),
            "contained_tree_identity": identity(
                PREFIX + "contained_model_tree_manifest.v1", version
            ),
            "axis_evidence": evidence,
            "ordered_fitted_cells": fitted,
            "cell_order_identity": identity("scworkbench.cell_order.v1", fitted),
        }
        self_identified(
            cases,
            "sparse_g1_source_axis_" + version,
            model_source,
            model_source["schema"],
            "source_axis_identity",
            "source axis excluding source_axis_identity",
        )

    output = copy.deepcopy(original_request["outputs"][2])
    output["name"] = "sparse_family"
    output["allocated_id"] = "sparse_family_id_fixture"
    descriptor = output["descriptor"]
    descriptor["relative_root"] = "outputs/sparse_family"
    descriptor["expected_dimensions"] = [4, 3]
    descriptor["expected_cell_order_identity"] = None
    descriptor["grant_identity"] = identity(
        PREFIX + "output_grant.v3",
        {key: value for key, value in descriptor.items() if key != "grant_identity"},
        "descriptor excluding grant_identity",
    )
    base_grant_identity = reference(descriptor["grant_identity"])
    limits = {
        name: policy[name]
        for name in (
            "maximum_axis_cells",
            "maximum_keys",
            "maximum_rows",
            "maximum_id_bytes",
            "maximum_control_bytes",
        )
    }
    grant = {
        "schema": PREFIX + "sparse_family_provisional_grant.v1",
        "manifest_policy_identity": reference(policy["policy_identity"]),
        "source_axis": typed_source,
        "source_axis_identity": reference(typed_source["source_axis_identity"]),
        "ordinary_output_base_grant_identity": base_grant_identity,
        "transition_identity": reference(original_request["transition"]["transition_identity"]),
        "output_name": "sparse_family",
        "kind": "cell_vector_family",
        "limits": limits,
    }
    self_identified(
        cases,
        "sparse_provisional_grant",
        grant,
        grant["schema"],
        "grant_identity",
        "provisional grant excluding grant_identity",
    )
    descriptor["standalone_sparse_cell_family_provisional_grant"] = grant
    descriptor["grant_identity"] = identity(
        PREFIX + "output_grant.v3",
        {key: value for key, value in descriptor.items() if key != "grant_identity"},
        "descriptor excluding grant_identity",
    )

    keys = ["zeta|alpha", "alpha"]
    per_key = [
        {"key": keys[0], "cell_ids": ["cell_2", "cell_1"]},
        {"key": keys[1], "cell_ids": ["cell_10", "x|y"]},
    ]
    declaration = {
        "schema": PREFIX + "sparse_family_layout_declaration.v1",
        "provisional_grant_identity": reference(grant["grant_identity"]),
        "output_name": "sparse_family",
        "ordered_keys": keys,
        "per_key_cells": per_key,
        "declared_row_count": 4,
    }
    self_identified(
        cases,
        "sparse_layout_declaration",
        declaration,
        declaration["schema"],
        "declaration_identity",
        "declaration excluding declaration_identity",
    )
    pairs = [
        {"key": group["key"], "cell_id": cell} for group in per_key for cell in group["cell_ids"]
    ]
    pair_identity = identity(PREFIX + "sparse_family_pair_order.v1", pairs)
    cases.append(
        {
            "name": "sparse_structured_pair_order",
            "preimage": pairs,
            "identity": pair_identity,
            "value": pairs,
        }
    )
    swapped_pairs = pairs[2:] + pairs[:2]
    cases.append(
        {
            "name": "sparse_swapped_key_pair_order",
            "preimage": swapped_pairs,
            "identity": identity(PREFIX + "sparse_family_pair_order.v1", swapped_pairs),
            "value": swapped_pairs,
        }
    )
    for name, pair in (
        ("sparse_separator_pair_left", [{"key": "a\u001cb", "cell_id": "c"}]),
        ("sparse_separator_pair_right", [{"key": "a", "cell_id": "b\u001cc"}]),
    ):
        cases.append(
            {
                "name": name,
                "preimage": pair,
                "identity": identity(PREFIX + "sparse_family_pair_order.v1", pair),
                "value": pair,
            }
        )
    cell_order_ids = [identity("scworkbench.cell_order.v1", group["cell_ids"]) for group in per_key]
    seal = {
        "schema": PREFIX + "sparse_family_layout_seal.v1",
        "provisional_grant_identity": reference(grant["grant_identity"]),
        "declaration_identity": reference(declaration["declaration_identity"]),
        "source_axis_identity": reference(typed_source["source_axis_identity"]),
        "ordered_keys": keys,
        "per_key_cell_counts": [2, 2],
        "per_key_cell_order_identities": cell_order_ids,
        "key_major_pair_order_identity": pair_identity,
        "row_count": 4,
    }
    self_identified(
        cases,
        "sparse_layout_seal",
        seal,
        seal["schema"],
        "seal_identity",
        "seal excluding seal_identity",
    )
    sealed = {
        "schema": PREFIX + "sparse_family_sealed_output_grant.v1",
        "base_grant_identity": reference(descriptor["grant_identity"]),
        "layout_seal_identity": reference(seal["seal_identity"]),
        "output_name": "sparse_family",
        "kind": "cell_vector_family",
        "exact_dimensions": [4, 3],
        "source_axis_identity": reference(typed_source["source_axis_identity"]),
    }
    self_identified(
        cases,
        "sparse_sealed_writer_grant",
        sealed,
        sealed["schema"],
        "grant_identity",
        "sealed output grant excluding grant_identity",
    )

    request = copy.deepcopy(original_request)
    request.pop("derived_fitted_axis_grants")
    request["outputs"].append(output)
    request["standalone_sparse_cell_family_grants"] = [grant]
    capability = request["execution_capability"]
    capability.pop("derived_fitted_axis_grant_identities")
    capability["writer_grants"].append(
        {
            "output_name": output["name"],
            "writer": descriptor["writer"],
            "grant_identity": descriptor["grant_identity"],
        }
    )
    capability["standalone_sparse_cell_family_grant_identities"] = [
        reference(grant["grant_identity"])
    ]
    capability["capability_identity"] = identity(
        PREFIX + "capability.v3",
        {k: v for k, v in capability.items() if k != "capability_identity"},
        "capability excluding capability_identity",
    )
    self_identified(
        cases,
        "sparse_success_request",
        request,
        PREFIX + "transition_request.v3",
        "request_identity",
        "request excluding request_identity",
    )

    receipt = copy.deepcopy(previous["dfa_success_receipt"]["value"])
    receipt.pop("derived_fitted_axis_observations")
    receipt["request_identity"] = request["request_identity"]
    receipt["capability_identity"] = capability["capability_identity"]
    receipt_output = copy.deepcopy(receipt["outputs"][2])
    receipt_output["name"] = "sparse_family"
    receipt_output["allocated_id"] = output["allocated_id"]
    receipt_descriptor = receipt_output["descriptor"]
    receipt_descriptor["grant_identity"] = descriptor["grant_identity"]
    receipt_descriptor["dimensions"] = [4, 3]
    receipt_descriptor["cell_order_identity"] = pair_identity
    receipt_descriptor["observed_descriptor_identity"] = identity(
        PREFIX + "observed_output.v3",
        {k: v for k, v in receipt_descriptor.items() if k != "observed_descriptor_identity"},
        "descriptor excluding observed_descriptor_identity",
    )
    receipt["outputs"].append(receipt_output)
    observation = {
        "schema": PREFIX + "observed_sparse_cell_family.v1",
        "provisional_grant_identity": reference(grant["grant_identity"]),
        "declaration": declaration,
        "seal": seal,
        "sealed_writer_grant": sealed,
        "observed_keys": keys,
        "per_key_cell_counts": [2, 2],
        "per_key_cell_order_identities": cell_order_ids,
        "observed_pair_order_identity": pair_identity,
        "observed_row_count": 4,
        "observed_dimensions": [4, 3],
        "output_descriptor_identity": reference(receipt_descriptor["observed_descriptor_identity"]),
        "output_content_identity": receipt_descriptor["content_identity"],
        "payload_manifest_identity": receipt_descriptor["payload_manifest_identity"],
        "validation_status": "passed",
    }
    self_identified(
        cases,
        "sparse_observed_output",
        observation,
        observation["schema"],
        "observation_identity",
        "observation excluding observation_identity",
    )
    receipt["sparse_cell_family_observations"] = [observation]
    self_identified(
        cases,
        "sparse_success_receipt",
        receipt,
        PREFIX + "transition_receipt.v3",
        "receipt_identity",
        "receipt excluding receipt_identity",
    )

    # The negative corpus intentionally distinguishes schema from semantic/identity
    # rejection. Shared JSON shape is not itself proof of authenticated authority.
    wrong_v2 = copy.deepcopy(g1_evidence[0])
    wrong_v2["selected_axis_sidecar_identity"] = identity("sidecar", "wrong")
    missing_v3 = copy.deepcopy(g1_evidence[1])
    del missing_v3["selected_axis_seal_identity"]
    both_sources = copy.deepcopy(policy)
    both_sources["axis_source"]["model_read"] = "model_read"
    bad_scope = copy.deepcopy(policy)
    bad_scope["policy_identity"]["scope"] = "wrong scope"
    no_declaration = copy.deepcopy(observation)
    del no_declaration["declaration"]
    for name, schema_file, definition, value in (
        (
            "sparse_v2_with_v3_sidecar",
            "operation-pack-transition-request-v3.schema.json",
            "sparse_family_g1_axis_evidence",
            wrong_v2,
        ),
        (
            "sparse_v3_without_seal",
            "operation-pack-transition-request-v3.schema.json",
            "sparse_family_g1_axis_evidence",
            missing_v3,
        ),
        (
            "sparse_both_axis_sources",
            "operation-pack-v3.schema.json",
            "dimension_policy",
            both_sources,
        ),
        (
            "sparse_wrong_policy_scope",
            "operation-pack-v3.schema.json",
            "dimension_policy",
            bad_scope,
        ),
        (
            "sparse_receipt_without_declaration",
            "operation-pack-transition-receipt-v3.schema.json",
            "sparse_cell_family_observation",
            no_declaration,
        ),
    ):
        negatives.append(
            {
                "name": name,
                "schema_file": schema_file,
                "definition": definition,
                "value": value,
                "expected": "schema_reject",
                "expected_code": "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
            }
        )

    wrong_policy_digest = copy.deepcopy(policy)
    wrong_policy_digest["policy_identity"]["sha256"] = "0" * 64
    contradictory_receipt = copy.deepcopy(observation)
    contradictory_receipt["observed_keys"] = list(reversed(keys))
    contradictory_receipt["observation_identity"] = identity(
        observation["schema"],
        {k: v for k, v in contradictory_receipt.items() if k != "observation_identity"},
        "observation excluding observation_identity",
    )
    swapped_declaration = copy.deepcopy(declaration)
    swapped_declaration["ordered_keys"] = list(reversed(keys))
    swapped_declaration["declaration_identity"] = identity(
        declaration["schema"],
        {k: v for k, v in swapped_declaration.items() if k != "declaration_identity"},
        "declaration excluding declaration_identity",
    )
    for name, schema_file, definition, value in (
        (
            "sparse_policy_self_hash_wrong",
            "operation-pack-v3.schema.json",
            "dimension_policy",
            wrong_policy_digest,
        ),
        (
            "sparse_declaration_keys_swapped_self_rehashed",
            "operation-pack-transition-request-v3.schema.json",
            "sparse_family_layout_declaration",
            swapped_declaration,
        ),
        (
            "sparse_receipt_keys_swapped_self_rehashed",
            "operation-pack-transition-receipt-v3.schema.json",
            "sparse_cell_family_observation",
            contradictory_receipt,
        ),
    ):
        negatives.append(
            {
                "name": name,
                "schema_file": schema_file,
                "definition": definition,
                "value": value,
                "expected": "semantic_reject",
                "expected_code": "PYBENCH_RECEIPT_MISMATCH"
                if "receipt" in name
                else "PYBENCH_CAPABILITY_MISMATCH",
            }
        )

    CORPUS.write_text(json.dumps(corpus, indent=2))


if __name__ == "__main__":
    main()
