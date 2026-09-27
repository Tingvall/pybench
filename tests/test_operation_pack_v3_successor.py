from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_json, canonical_sha256
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.operation_packs_v3 import (
    activate_operation_pack_v3,
    discover_operation_pack_v3,
    unload_operation_pack_v3,
    validate_manifest_v3,
)
from pybench.protocol_v4 import (
    V4Readers,
    _read_provenance,
    _validate_cardinality_payload,
    _validate_provenance_snapshot,
    execute_operation_pack_v4,
    validate_python_runtime_sidecar_v4,
    validate_receipt_v4,
    validate_request_v4,
)
from test_operation_pack_v3_typed_slice import TOKEN, _axis, _serve
from v3_helpers import _input_descriptor, fixture_inputs, make_request

FIXTURE = Path(__file__).parent / "fixtures" / "operation-pack-v3-python-reference"
SHARED_DIGESTS = {
    "operation-pack-v3.schema.json": "870fa7d4d4f506399570c28b50ed8e98bf1ad46a87d94ae34c6b202cf5911298",
    "operation-pack-transition-request-v3.schema.json": "4bae5280730f298147e415e6c4054213d5fd9984e8c438b2ddf40905cc36fb0b",
    "operation-pack-transition-receipt-v3.schema.json": "3c6aa269bbd66bffa7c7a01253e97107d1995395ec84569db3265dc929b38ec1",
    "fixtures/operation-pack-v3/identities.json": "27f0e7317298815b82dc7ab4d2b7f4967549a8f90663bf5d3f337d1e5bca9793",
    "fixtures/operation-pack-v3/relation-family-identities.json": "5f94004cc229cc5f02c4b7a9401f5c91a0cb9346469d48499b46809c9a919860",
    "fixtures/operation-pack-v3/phase6-matrix-model-identities.json": "83a35075b04dc31ffc999e0f10e5d613974d09ab821d29a7cdbf0f165c34a355",
    "fixtures/operation-pack-v3/phase6-ordered-cell-metadata-identities.json": "7317a0a9baea791c3cac688e9528342a4d2d1691a2840add53dde2ffe623b37e",
    "fixtures/operation-pack-v3/phase6-latent-union/request.json": "c6ce8908a1ec748a551dd03d8fd4c48ffd061bb073a4d85669fb8c9a9fab2b2d",
    "fixtures/operation-pack-v3/phase6-latent-union/receipt.json": "8db0b003ebf9a7087aaa35574d50df7a8d4ae698240c485d7288b95fb51a796e",
}


@pytest.fixture(scope="module")
def installed_v3():
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "--force-reinstall",
            "--no-deps",
            str(FIXTURE),
        ],
        check=True,
    )
    distribution = metadata.distribution("scwb-v3-python-reference")
    manifest = distribution.locate_file("scwbpyv3reference/operation-pack.json")
    discovery = discover_operation_pack_v3(manifest)
    yield discovery
    unload_operation_pack_v3(discovery.manifest["namespace"])


def _error_code(function, *args):
    with pytest.raises(OperationPackError) as caught:
        function(*args)
    return caught.value.code


def _request_v4(discovery, stage, operation_name, inputs, params=None, registry_snapshots=None):
    source_operation = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == operation_name
    )
    request = make_request(
        discovery,
        stage,
        operation_name,
        inputs,
        params,
        registry_snapshots=registry_snapshots,
    )
    request["schema"] = "scworkbench.operation_pack.transition_request.v3"
    request["process_protocol_version"] = "pybench-op-v4"
    request["pack_abi_version"] = "3"
    if source_operation.get("provenance_reads"):
        _add_provenance_contract(request, stage)
    if any(
        policy.get("policy") == "authenticated_output_axes_v1"
        for policy in source_operation.get("dimension_policies", [])
    ):
        _add_cardinality_contracts(request, source_operation, inputs, params or {}, stage)
    for output in request["outputs"]:
        descriptor = output["descriptor"]
        descriptor.pop("grant_identity")
        descriptor["grant_identity"] = _identity(
            "scworkbench.operation_pack.output_grant.v3",
            descriptor,
            "descriptor excluding grant_identity",
        )
    capability = request["execution_capability"]
    capability["writer_grants"] = [
        {
            "output_name": item["name"],
            "writer": item["descriptor"]["writer"],
            "grant_identity": item["descriptor"]["grant_identity"],
        }
        for item in request["outputs"]
    ]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v3",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v3",
        request,
        "request excluding request_identity",
    )
    return request


def _add_cardinality_contracts(request, operation, inputs, params, stage=None):
    input_records = {item["name"]: item["descriptor"] for item in request["inputs"]}
    output_records = {item["name"]: item for item in request["outputs"]}
    for policy in operation.get("dimension_policies", []):
        if policy.get("policy") != "authenticated_output_axes_v1":
            continue
        if policy["cell_axis"]["source"] == "session_cell_axis_read_v1":
            read_name = policy["cell_axis"]["registry_read"]
            registry_record = next(
                item for item in request["registry_reads"] if item["name"] == read_name
            )
            registry_descriptor = registry_record["descriptor"]
            snapshot = json.loads(
                (Path(stage) / "registry_reads" / read_name / "payload.json").read_text()
            )
            cells = snapshot["cell_id"]
            cell_axis_source = {
                "source": policy["cell_axis"]["source"],
                "registry_read": read_name,
                "descriptor_identity": {
                    key: registry_descriptor["descriptor_identity"][key]
                    for key in ("scheme", "sha256")
                },
                "selection_identity": registry_descriptor["selection_identity"],
            }
        else:
            source_name = policy["cell_axis"]["input"]
            source = inputs[source_name]
            source_descriptor = input_records[source_name]
            source_identity = {
                "scheme": source_descriptor["descriptor_identity"]["scheme"],
                "sha256": source_descriptor["descriptor_identity"]["sha256"],
            }
            cells = list(dict.fromkeys(source["cell_id"]))
            cell_axis_source = {
                "input_name": source_name,
                "input_descriptor_identity": source_identity,
                "derivation": policy["cell_axis"]["source"],
            }
        grant = {
            "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
            "output": policy["output"],
            "kind": output_records[policy["output"]]["descriptor"]["kind"],
            "policy": policy["policy"],
            "shape": policy["shape"],
            "row_order": policy["row_order"],
            "missing_values": policy["missing_values"],
            "cell_axis_source": cell_axis_source,
            "cell_axis_count": len(cells),
            "cell_axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
        }
        if policy["shape"] in {
            "cell_key_cartesian",
            "bounded_grouped_cell_key_cartesian",
        }:
            resolver = policy["key_axis"]
            bounded = resolver["source"] == "declared_bounded_key_groups_v1"
            if bounded:
                required = resolver["required_keys"]
                optional = [key for group in resolver["optional_groups"] for key in group["keys"]]
                resolution = {
                    key: resolver[key]
                    for key in (
                        "source",
                        "required_keys",
                        "optional_groups",
                        "selection_owner",
                        "group_order",
                    )
                }
            elif resolver["source"] == "resolved_parameter_feature_groups_v1":
                groups = params[resolver["groups_parameter"]]
                keys = list(groups)
                matrix = inputs[resolver["matrix_input"]]
                key_descriptor = input_records[resolver["matrix_input"]]
                available = set(matrix["feature_id"])
                resolved = []
                for key, features in groups.items():
                    unique = list(dict.fromkeys(features))
                    resolved.append(
                        {
                            "key": key,
                            "features_input": unique,
                            "features_matched": [item for item in unique if item in available],
                            "features_missing": [item for item in unique if item not in available],
                        }
                    )
                resolution = {
                    "source": resolver["source"],
                    "groups_parameter": resolver["groups_parameter"],
                    "matrix_input": resolver["matrix_input"],
                    "matrix_input_descriptor_identity": {
                        key: key_descriptor["descriptor_identity"][key]
                        for key in ("scheme", "sha256")
                    },
                    "matrix_feature_axis_identity": key_descriptor["feature_order_identity"],
                    "name_prefix_parameter": resolver["name_prefix_parameter"],
                    "name_prefix_value": params[resolver["name_prefix_parameter"]],
                    "matching": resolver["matching"],
                    "normalization": resolver["normalization"],
                    "generated_names": resolver["generated_names"],
                    "missing_feature_policy": "drop",
                    "empty_group_policy": resolver["empty_group_policy"],
                    "zero_key_policy": resolver["zero_key_policy"],
                    "groups": resolved,
                }
            else:
                key_input = inputs[resolver["input"]]
                keys = list(dict.fromkeys(key_input["key"]))
                selector_parameter = resolver.get("selector_parameter")
                selector_value = params.get(selector_parameter) if selector_parameter else None
                if selector_value is not None:
                    keys = list(selector_value)
                key_descriptor = input_records[resolver["input"]]
                resolution = {
                    "source": resolver["source"],
                    "input": resolver["input"],
                    "input_descriptor_identity": {
                        "scheme": key_descriptor["descriptor_identity"]["scheme"],
                        "sha256": key_descriptor["descriptor_identity"]["sha256"],
                    },
                    "selector_parameter": selector_parameter,
                    "selector_value": selector_value,
                    "no_selector_order": resolver["no_selector_order"],
                    "selector_order": resolver["selector_order"],
                    "unknown_key_policy": resolver["unknown_key_policy"],
                    "zero_key_policy": resolver["zero_key_policy"],
                    "keys": keys,
                }
            if bounded:
                grant.update(
                    minimum_key_count=len(required),
                    maximum_key_count=len(required) + len(optional),
                    minimum_row_count=len(required) * len(cells),
                    maximum_row_count=(len(required) + len(optional)) * len(cells),
                    key_axis_resolution=resolution,
                )
                expected_dimensions = [0, 3]
                output_cells = []
            else:
                rows = [{"key": key, "cell_id": cell} for key in keys for cell in cells]
                grant.update(
                    key_axis_count=len(keys),
                    key_axis=keys,
                    key_axis_resolution=resolution,
                    key_axis_order_identity=_identity("scworkbench.row_order.v1", keys),
                    expected_row_count=len(rows),
                    row_order_identity=_identity("scworkbench.row_order.v1", rows),
                )
                output_cells = [cell for key in keys for cell in cells]
                expected_dimensions = [len(rows), 3]
        else:
            grant.update(
                expected_row_count=len(cells),
                row_order_identity=_identity("scworkbench.row_order.v1", cells),
            )
            output_cells = cells
            expected_dimensions = [len(cells)]
        grant["cardinality_grant_identity"] = _identity(
            "scworkbench.operation_pack.output_cardinality_grant.v1",
            grant,
            "grant excluding cardinality_grant_identity",
        )
        descriptor = output_records[policy["output"]]["descriptor"]
        descriptor["cardinality_grant"] = grant
        descriptor["expected_dimensions"] = expected_dimensions
        if policy["shape"] != "bounded_grouped_cell_key_cartesian":
            descriptor["expected_cell_order_identity"] = _identity(
                "scworkbench.cell_order.v1", output_cells
            )


def _add_typed_slice_contract(
    request, stage, *, selected_features=None, empty_selection_policy="reject"
):
    selected_features = ["feature_1"] if selected_features is None else list(selected_features)
    input_record = next(item for item in request["inputs"] if item["name"] == "matrix")
    descriptor = input_record["descriptor"]
    source = {
        "schema": "scworkbench.operation_pack.typed_slice_source_reference.v1",
        "input_name": "matrix",
        "input_reference": "matrix:m1",
        "source_kind": "bundle_matrix",
        "matrix_id": "m1",
        "input_descriptor_identity": {
            key: descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
        },
        "input_content_identity": descriptor["content_identity"],
        "transition_source_state_identity": request["transition"]["source_state_identity"],
    }
    source["source_reference_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_source_reference.v1",
        source,
        "source reference excluding source_reference_identity",
    )
    grant = {
        "schema": "scworkbench.operation_pack.typed_slice_grant.v1",
        "name": "matrix_chunks",
        "input_name": "matrix",
        "kind": "matrix",
        "reader": "read_typed_slices",
        "input_descriptor_identity": source["input_descriptor_identity"],
        "input_content_identity": source["input_content_identity"],
        "source_reference": source,
        "supported_axes": ["cells"],
        "chunk_axis": "cells",
        "cells_axis": _axis(stage, "matrix_chunks", "cells", ["c2", "c1", "c3"]),
        "features_axis": _axis(stage, "matrix_chunks", "features", selected_features),
        "requested_chunk_size": request["params"]["chunk_size"],
        "call_discipline": "ordered_partition_exactly_once",
        "empty_selection_policy": empty_selection_policy,
        "duplicate_policy": "reject",
        "max_rows": len(selected_features),
        "max_columns": min(request["params"]["chunk_size"], 3),
        "max_elements": len(selected_features) * min(request["params"]["chunk_size"], 3),
        "max_control_bytes": 1_000_000,
        "max_response_bytes": 1_000_000,
        "max_outstanding_responses": 1,
    }
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    request["typed_slice_grants"] = [grant]
    capability = request["execution_capability"]
    capability["reader_grants"] = [
        item for item in capability["reader_grants"] if item.get("input_name") != "matrix"
    ]
    capability["reader_grants"].append(
        {
            "typed_slice_name": "matrix_chunks",
            "reader": "read_typed_slices",
            "grant_identity": grant["grant_identity"],
        }
    )
    capability["typed_slice_grant_identities"] = [
        {key: grant["grant_identity"][key] for key in ("scheme", "sha256")}
    ]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v3",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v3",
        request,
        "request excluding request_identity",
    )
    return grant


def _add_provenance_contract(request, stage):
    input_record = request["inputs"][0]
    input_name = input_record["name"]
    input_descriptor = input_record["descriptor"]
    input_identity = {
        "scheme": input_descriptor["descriptor_identity"]["scheme"],
        "sha256": input_descriptor["descriptor_identity"]["sha256"],
    }
    source_head = {
        "snapshot_id": "snapshot_source",
        "schema_version": "1",
        "head_run_ids": ["run_source"],
        "n_runs": 1,
        "n_artifacts": 1,
        "commit_id": None,
        "replay_status": "verified",
    }
    source_head["source_snapshot_identity"] = _identity(
        "scworkbench.provenance.snapshot_head.v1",
        source_head,
        "snapshot head excluding source_snapshot_identity",
    )
    snapshot = {
        "schema": "scworkbench.artifact_ancestry.snapshot.v1",
        "attestation_name": "ancestry",
        "input_name": input_name,
        "input_descriptor_identity": input_identity,
        "reference_kind": "promoted_artifact",
        "artifact_id": f"artifact_{input_name}",
        "artifact_type": input_descriptor["kind"],
        "artifact_lineage_status": None,
        "artifact_content_identity": input_descriptor["content_identity"],
        "source_run_id": "run_source",
        "source_run_identity": _identity("scworkbench.run_hash.v1", {"run": "source"}),
        "source_snapshot_id": "snapshot_source",
        "source_snapshot": source_head,
        "source_output_name": input_name,
        "source_output_content_identity": input_descriptor["content_identity"],
        "lifecycle": {
            "state": "current",
            "identity": _identity("scworkbench.entity.lifecycle.v1", {"artifact": input_name}),
            "provenance_bound": 1,
            "incoming_supersedes": [],
            "outgoing_superseded_by": [],
            "tombstone_state": None,
        },
    }
    snapshot["snapshot_identity"] = _identity(
        "scworkbench.artifact_ancestry.snapshot.v1",
        snapshot,
        "snapshot excluding snapshot_identity",
    )
    relative = "provenance_reads/ancestry/payload.json"
    payload = canonical_json(snapshot).encode()
    path = stage / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    descriptor = {
        "schema": "scworkbench.operation_pack.provenance_read_descriptor.v1",
        "name": "ancestry",
        "category": "authenticated_core_provenance_snapshot",
        "kind": "artifact_ancestry",
        "reader": "read_artifact_ancestry",
        "input_name": input_name,
        "input_descriptor_identity": input_identity,
        "lifecycle_policy": "require_current",
        "snapshot_identity": snapshot["snapshot_identity"],
        "payload_manifest_identity": _identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            {"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
        ),
    }
    descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.provenance_read_descriptor.v1",
        descriptor,
        "descriptor excluding descriptor_identity",
    )
    request["provenance_reads"] = [descriptor]
    capability = request["execution_capability"]
    capability["reader_grants"].append(
        {
            "provenance_read_name": "ancestry",
            "reader": "read_artifact_ancestry",
            "descriptor_identity": descriptor["descriptor_identity"],
        }
    )
    capability["provenance_read_identities"] = [
        {
            "scheme": descriptor["descriptor_identity"]["scheme"],
            "sha256": descriptor["descriptor_identity"]["sha256"],
        }
    ]


def _cardinality(shape="cell_key_cartesian"):
    cells = ["c2", "c1", "c3"]
    keys = ["sigB", "sigA"]
    value = {
        "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
        "output": "scores",
        "kind": "cell_vector_family" if shape == "cell_key_cartesian" else "cell_vector",
        "policy": "authenticated_output_axes_v1",
        "shape": shape,
        "row_order": "key_major_then_cell_v1"
        if shape == "cell_key_cartesian"
        else "input_cell_order_v1",
        "missing_values": "forbidden",
        "cell_axis_source": {
            "input_name": "matrix",
            "input_descriptor_identity": _identity("fixture.input", {"name": "matrix"}),
            "derivation": "effective_cells_v1",
        },
        "cell_axis_count": len(cells),
        "cell_axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
    }
    if shape == "cell_key_cartesian":
        value.update(
            key_axis_count=len(keys),
            key_axis=keys,
            key_axis_resolution={
                "source": "input_keys_with_optional_selector_v1",
                "input": "signals",
                "input_descriptor_identity": _identity("fixture.input", {"name": "signals"}),
                "selector_parameter": None,
                "selector_value": None,
                "no_selector_order": "first_occurrence_v1",
                "selector_order": "normalized_parameter_order_v1",
                "unknown_key_policy": "error",
                "zero_key_policy": "allow",
                "keys": keys,
            },
            key_axis_order_identity=_identity("scworkbench.row_order.v1", keys),
            expected_row_count=len(cells) * len(keys),
            row_order_identity=_identity(
                "scworkbench.row_order.v1",
                [{"key": key, "cell_id": cell} for key in keys for cell in cells],
            ),
        )
    else:
        value.update(
            expected_row_count=len(cells),
            row_order_identity=_identity("scworkbench.row_order.v1", cells),
        )
    value["cardinality_grant_identity"] = _identity(
        "scworkbench.operation_pack.output_cardinality_grant.v1",
        value,
        "grant excluding cardinality_grant_identity",
    )
    return value


def _family_payload():
    cells = ["c2", "c1", "c3"]
    keys = ["sigB", "sigA"]
    return {
        "kind": "cell_vector_family",
        "dimensions": [6, 3],
        "cell_id": cells + cells,
        "key": [keys[0]] * 3 + [keys[1]] * 3,
        "value": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }


def _cell_major_family_payload():
    return {
        "kind": "cell_vector_family",
        "dimensions": [6, 3],
        "cell_id": ["c2", "c2", "c1", "c1", "c3", "c3"],
        "key": ["sigB", "sigA", "sigB", "sigA", "sigB", "sigA"],
        "value": [1.0, 4.0, 2.0, 5.0, 3.0, 6.0],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }


def test_shared_successor_assets_and_identity_corpus_are_exact():
    for name, expected in SHARED_DIGESTS.items():
        assert hashlib.sha256(protocol_asset(name).read_bytes()).hexdigest() == expected
    corpus = json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())
    assert corpus["schema"] == "scworkbench.operation_pack.v3.identity_fixtures.v1"
    pairs = corpus["authoritative_axes"]["pairs"]
    frozen_row_digest = "9b66cb7cf11a0f95e146903b0e5238337b2ed62d02c97e4eac2fd6882392462d"
    assert _identity("scworkbench.row_order.v1", pairs)["sha256"] == frozen_row_digest
    assert (
        _identity(
            "scworkbench.row_order.v1",
            [[pair["key"], pair["cell_id"]] for pair in pairs],
        )["sha256"]
        != frozen_row_digest
    )
    for case in corpus["cases"]:
        assert canonical_sha256(case["preimage"]) == case["identity"]["sha256"]
        if case["name"].startswith("g7_effective_key_") or case["name"] in {
            "dfa_selected_axis_sidecar",
            "dfa_model_content_identity",
        }:
            assert case["value"] == case["preimage"]
        else:
            assert case["identity"] in case["value"].values()


def test_manifest_v3_is_explicit_and_v2_reader_remains_separate():
    manifest = json.loads((FIXTURE / "operation-pack.json").read_text())
    assert validate_manifest_v3(manifest)["pack_abi_version"] == "3"
    wrong = copy.deepcopy(manifest)
    wrong["pack_abi_version"] = "2"
    assert _error_code(validate_manifest_v3, wrong) == "OPERATION_PACK_MANIFEST_SCHEMA_INVALID"


def test_authenticated_core_reader_name_collision_is_rejected():
    manifest = json.loads((FIXTURE / "operation-pack.json").read_text())
    operation = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::feature_annotation_probe"
    )
    operation["registry_reads"][0]["name"] = "matrix"
    assert _error_code(validate_manifest_v3, manifest) == (
        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID"
    )


@pytest.mark.parametrize("shape", ["cell_key_cartesian", "bounded_grouped_cell_key_cartesian"])
def test_bounded_cardinality_shape_and_resolver_must_be_paired(shape):
    manifest = json.loads((FIXTURE / "operation-pack.json").read_text())
    bounded = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::bounded_group_probe"
    )["dimension_policies"][0]
    ordinary = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::cardinality_family_probe"
    )["dimension_policies"][0]
    if shape == "cell_key_cartesian":
        bounded["shape"] = shape
    else:
        ordinary["shape"] = shape
    assert _error_code(validate_manifest_v3, manifest) == (
        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID"
    )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("operation", "chunking", {"axis": "cells"}),
        ("operation", "summary_finalizer", {"callable": "finalize"}),
        ("typed_reader", "schedule", [{"start": 1, "count": 2}]),
        ("typed_reader", "batches", [{"cells": ["c1", "c2"]}]),
        ("typed_reader", "fragment_output", "scores_fragment"),
        ("typed_reader", "merge", "row_bind_cell_vector_family_v1"),
        ("typed_reader", "summary_finalizer", "finalize"),
        ("typed_reader", "scientific_operation", "ucell"),
    ],
)
def test_slice_n11_operation_shaped_manifest_declarations_are_rejected(target, field, value):
    manifest = json.loads((FIXTURE / "operation-pack.json").read_text())
    operation = next(
        item
        for item in manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::cell_chunk_probe"
    )
    subject = operation if target == "operation" else operation["typed_slice_reads"][0]
    subject[field] = value
    assert _error_code(validate_manifest_v3, manifest) == "OPERATION_PACK_MANIFEST_SCHEMA_INVALID"


def test_v3_discovery_activation_and_plain_v4_execution(installed_v3, tmp_path):
    discovery = installed_v3
    registration = activate_operation_pack_v3(discovery)
    assert set(registration.handlers) == {
        item["qualified_name"] for item in discovery.manifest["operations"]
    }
    assert not hasattr(registration, "finalizers")
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::family_probe",
        fixture_inputs(),
        {
            "family": {
                "kind": "cell_set_family_admission",
                "mode": "add",
                "sets": [],
                "source": {
                    "label": "successor fixture",
                    "created_by": None,
                    "input_artifact_id": None,
                    "input_run_id": None,
                    "input_snapshot_id": None,
                },
            }
        },
    )
    assert validate_request_v4(request, discovery) == request
    receipt, sidecar = execute_operation_pack_v4(request, discovery, stage)
    assert receipt["status"] == "ok"
    assert validate_receipt_v4(receipt, request, discovery, stage) == receipt
    assert validate_python_runtime_sidecar_v4(sidecar, request, discovery) == sidecar


def test_typed_slice_executes_once_and_receipts_transport_neutral_usage(
    installed_v3, tmp_path, monkeypatch
):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    stage = tmp_path / "typed-slice-process"
    stage.mkdir()
    inputs = fixture_inputs()
    inputs["matrix"].update(
        dimensions=[1, 3],
        cell_id=["c2", "c1", "c3"],
        values=[[2.0, 1.0, 3.0]],
    )
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::cell_chunk_probe",
        inputs,
        {
            "features": {"module1": ["feature_1"]},
            "slice_features": ["feature_1"],
            "name": "Module",
            "chunk_size": 2,
        },
    )
    grant = _add_typed_slice_contract(request, stage)
    assert validate_request_v4(request, discovery) == request
    shutil.rmtree(stage / "inputs/matrix")
    (stage / "broker").mkdir()
    monkeypatch.setenv("PYBENCH_OPERATION_PACK_BROKER_TOKEN", TOKEN)
    errors = []
    broker = threading.Thread(target=_serve, args=(stage, request, grant, 2, errors), daemon=True)
    broker.start()
    receipt, sidecar = execute_operation_pack_v4(request, discovery, stage)
    broker.join(5)
    assert not broker.is_alive() and not errors
    usage = receipt["typed_slice_usage"][0]
    assert usage["coverage_status"] == "exact"
    assert usage["call_count"] == 2
    assert all("response_identity" not in call for call in usage["calls"])
    assert not (stage / "inputs/matrix/payload.json").exists()
    assert validate_receipt_v4(receipt, request, discovery, stage) == receipt
    assert validate_python_runtime_sidecar_v4(sidecar, request, discovery) == sidecar


def test_zero_axis_typed_slice_executes_without_a_broker_call(installed_v3, tmp_path, monkeypatch):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    stage = tmp_path / "zero-axis-process"
    stage.mkdir()
    inputs = fixture_inputs()
    inputs["matrix"].update(
        dimensions=[1, 3],
        cell_id=["c2", "c1", "c3"],
        values=[[2.0, 1.0, 3.0]],
    )
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::zero_axis_probe",
        inputs,
        {"slice_features": [], "chunk_size": 2},
    )
    _add_typed_slice_contract(
        request,
        stage,
        selected_features=[],
        empty_selection_policy="allow_zero_axis",
    )
    assert validate_request_v4(request, discovery) == request
    shutil.rmtree(stage / "inputs/matrix")
    (stage / "broker").mkdir()
    monkeypatch.setenv("PYBENCH_OPERATION_PACK_BROKER_TOKEN", TOKEN)
    receipt, sidecar = execute_operation_pack_v4(request, discovery, stage)
    usage = receipt["typed_slice_usage"][0]
    assert usage["coverage_status"] == "empty"
    assert usage["call_count"] == 0
    assert usage["calls"] == []
    assert list((stage / "broker").iterdir()) == []
    assert validate_receipt_v4(receipt, request, discovery, stage) == receipt
    assert validate_python_runtime_sidecar_v4(sidecar, request, discovery) == sidecar


def test_artifact_ancestry_is_single_use_identity_bound_and_mutation_safe(installed_v3, tmp_path):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    stage = tmp_path / "ancestry"
    stage.mkdir()
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _cell_major_family_payload()},
    )
    assert validate_request_v4(request, discovery) == request
    receipt, _sidecar = execute_operation_pack_v4(request, discovery, stage)
    observation = receipt["provenance_reads"][0]
    assert observation["use_count"] == 1
    assert observation["descriptor_identity"] == {
        key: request["provenance_reads"][0]["descriptor_identity"][key]
        for key in ("scheme", "sha256")
    }
    assert observation["snapshot_identity"] == {
        key: request["provenance_reads"][0]["snapshot_identity"][key]
        for key in ("scheme", "sha256")
    }
    assert "scope" not in observation["descriptor_identity"]
    assert "scope" not in observation["snapshot_identity"]
    assert "scope" in observation["observed_descriptor_identity"]

    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    changed_stage = tmp_path / "ancestry-mutated"
    changed_stage.mkdir()
    changed = _request_v4(
        discovery,
        changed_stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _cell_major_family_payload()},
    )
    path = changed_stage / "provenance_reads/ancestry/payload.json"
    payload = json.loads(path.read_text())
    payload["artifact_id"] = "artifact_substituted"
    path.write_text(canonical_json(payload))
    before = {
        item.relative_to(changed_stage): item.read_bytes()
        for item in changed_stage.rglob("*")
        if item.is_file()
    }
    assert (
        _error_code(execute_operation_pack_v4, changed, discovery, changed_stage)
        == "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )
    assert {
        item.relative_to(changed_stage): item.read_bytes()
        for item in changed_stage.rglob("*")
        if item.is_file()
    } == before


def test_artifact_ancestry_payload_manifest_binds_path_size_and_sha256(installed_v3, tmp_path):
    stage = tmp_path / "ancestry-payload-manifest"
    stage.mkdir()
    request = _request_v4(
        installed_v3,
        stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _cell_major_family_payload()},
    )
    operation = next(
        item
        for item in installed_v3.manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::ancestry_probe"
    )
    path = stage / "provenance_reads/ancestry/payload.json"
    expected = _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {
            "path": "provenance_reads/ancestry/payload.json",
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    )
    assert request["provenance_reads"][0]["payload_manifest_identity"] == expected
    assert (
        _read_provenance(request, operation, stage)["ancestry"]["artifact_id"] == "artifact_signals"
    )

    legacy = copy.deepcopy(request)
    legacy["provenance_reads"][0]["payload_manifest_identity"] = _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {
            "path": "provenance_reads/ancestry/payload.json",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    )
    assert (
        _error_code(_read_provenance, legacy, operation, stage) == "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )


@pytest.mark.parametrize("condition", ["stale", "superseded"])
def test_artifact_ancestry_rejects_noncurrent_lifecycle(installed_v3, tmp_path, condition):
    stage = tmp_path / condition
    stage.mkdir()
    request = _request_v4(
        installed_v3,
        stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _cell_major_family_payload()},
    )
    descriptor = request["provenance_reads"][0]
    payload = json.loads((stage / "provenance_reads/ancestry/payload.json").read_text())
    if condition == "stale":
        payload["lifecycle"]["state"] = "historical"
    else:
        payload["lifecycle"]["outgoing_superseded_by"] = [
            {
                "successor_kind": "artifact",
                "successor_id": "artifact_new",
                "predecessor_kind": "artifact",
                "predecessor_id": payload["artifact_id"],
                "relationship_type": "SUPERSEDES",
            }
        ]
    payload.pop("snapshot_identity")
    payload["snapshot_identity"] = _identity(
        "scworkbench.artifact_ancestry.snapshot.v1",
        payload,
        "snapshot excluding snapshot_identity",
    )
    operation = next(
        item
        for item in installed_v3.manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::ancestry_probe"
    )
    assert (
        _error_code(
            _validate_provenance_snapshot,
            payload,
            descriptor,
            operation["provenance_reads"][0],
            request["inputs"][0]["descriptor"],
            request["params"],
        )
        == "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )


def test_cardinality_family_and_reduction_are_exact_and_ordered():
    inputs = {"matrix": {"kind": "matrix", "cell_id": ["c2", "c1", "c3"]}}
    family = _family_payload()
    grant = _cardinality()
    observed = _validate_cardinality_payload(grant, family, inputs, {"value_kind": "continuous"})
    assert observed["observed_row_count"] == 6
    assert observed["observed_cell_axis_order_identity"] == grant["cell_axis_order_identity"]
    vector = {
        "kind": "cell_vector",
        "dimensions": [3],
        "cell_id": ["c2", "c1", "c3"],
        "value": ["B", "A", "B"],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }
    reduction = _validate_cardinality_payload(
        _cardinality("one_per_cell"), vector, inputs, {"value_kind": "categorical"}
    )
    assert reduction["observed_row_count"] == 3


def test_cardinality_shapes_execute_through_the_v4_process_contract(installed_v3, tmp_path):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    family = _cell_major_family_payload()

    family_stage = tmp_path / "cardinality-family"
    family_stage.mkdir()
    family_request = _request_v4(
        discovery,
        family_stage,
        "scwbpyv3reference::cardinality_family_probe",
        {"signals": family},
    )
    family_receipt, _ = execute_operation_pack_v4(family_request, discovery, family_stage)
    family_output = json.loads((family_stage / "outputs/scores/payload.json").read_text())
    family_observed = family_receipt["outputs"][0]["descriptor"]["observed_cardinality"]
    assert family_output == _family_payload()
    assert family_observed["observed_row_count"] == 6
    assert family_observed["observed_cell_axis_count"] == 3
    assert family_observed["observed_key_axis_count"] == 2
    assert family_request["outputs"][0]["descriptor"]["cardinality_grant"]["key_axis"] == [
        "sigB",
        "sigA",
    ]

    reduction_stage = tmp_path / "cardinality-reduction"
    reduction_stage.mkdir()
    reduction_request = _request_v4(
        discovery,
        reduction_stage,
        "scwbpyv3reference::cardinality_reduction_probe",
        {"signals": family},
    )
    reduction_receipt, _ = execute_operation_pack_v4(reduction_request, discovery, reduction_stage)
    reduction_output = json.loads((reduction_stage / "outputs/reduced/payload.json").read_text())
    reduction_observed = reduction_receipt["outputs"][0]["descriptor"]["observed_cardinality"]
    assert reduction_output["cell_id"] == ["c2", "c1", "c3"]
    assert reduction_output["value"] == [5.0, 7.0, 9.0]
    assert reduction_observed["observed_row_count"] == 3
    assert reduction_observed["observed_cell_axis_count"] == 3
    assert "observed_key_axis_count" not in reduction_observed


def _session_axis_snapshot(transition):
    cells = ["c2", "c1", "c3"]
    return {
        "schema": "scworkbench.session_cell_axis.snapshot.v1",
        "selection_policy": "effective_session_cells_v1",
        "coverage_policy": "exact_complete_order_v1",
        "session_id": transition["session_id"],
        "run_id": transition["run_id"],
        "checkpoint_generation": transition["checkpoint_generation"],
        "transition_source_state_identity": transition["source_state_identity"],
        "cell_count": len(cells),
        "cell_id": cells,
        "cell_order_identity": _identity("scworkbench.cell_order.v1", cells),
    }


def _feature_annotation_snapshot(discovery, matrix):
    operation = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::feature_annotation_probe"
    )
    input_descriptor = _input_descriptor("matrix", matrix, operation["inputs"][0])
    rows = [
        {"feature_id": "feature_1", "values": {"feature_type": "gene"}},
        {"feature_id": "feature_2", "values": {"feature_type": None}},
    ]
    return {
        "schema": "scworkbench.matrix_feature_annotations.snapshot.v1",
        "matrix_input": "matrix",
        "matrix_id": matrix["matrix_id"],
        "input_descriptor_identity": {
            key: input_descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
        },
        "input_content_identity": input_descriptor["content_identity"],
        "feature_count": len(rows),
        "feature_order_identity": input_descriptor["feature_order_identity"],
        "fields": ["feature_type"],
        "rows": rows,
    }


@pytest.mark.parametrize(
    ("optional_groups", "expected_keys"),
    [
        ([], ["total_counts", "detected_features"]),
        (
            ["mitochondrial"],
            ["total_counts", "detected_features", "mito_counts", "mito_percent"],
        ),
        (
            ["ribosomal"],
            ["total_counts", "detected_features", "ribo_counts", "ribo_percent"],
        ),
        (
            ["mitochondrial", "ribosomal"],
            [
                "total_counts",
                "detected_features",
                "mito_counts",
                "mito_percent",
                "ribo_counts",
                "ribo_percent",
            ],
        ),
    ],
)
def test_session_axis_and_bounded_group_contracts_execute_in_process(
    installed_v3, tmp_path, optional_groups, expected_keys
):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    stage = tmp_path / f"bounded-{len(optional_groups)}"
    stage.mkdir()
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::bounded_group_probe",
        {},
        {"optional_groups": optional_groups},
        {"session_cells": _session_axis_snapshot},
    )
    receipt, _ = execute_operation_pack_v4(request, discovery, stage)
    observed = receipt["outputs"][0]["descriptor"]["observed_cardinality"]
    assert observed["observed_key_axis"] == expected_keys
    assert observed["selected_optional_groups"] == optional_groups
    assert receipt["registry_reads"][0]["descriptor"]["use_count"] == 1


def test_no_input_session_axis_and_matrix_annotations_are_process_readers(installed_v3, tmp_path):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    session_stage = tmp_path / "session-axis"
    session_stage.mkdir()
    session_request = _request_v4(
        discovery,
        session_stage,
        "scwbpyv3reference::session_axis_probe",
        {},
        registry_snapshots={"session_cells": _session_axis_snapshot},
    )
    session_receipt, _ = execute_operation_pack_v4(session_request, discovery, session_stage)
    labels = json.loads((session_stage / "outputs/labels/payload.json").read_text())
    assert labels["cell_id"] == ["c2", "c1", "c3"]
    assert session_receipt["registry_reads"][0]["descriptor"]["use_count"] == 1

    matrix_stage = tmp_path / "feature-annotations"
    matrix_stage.mkdir()
    matrix = fixture_inputs()["matrix"]
    matrix.update(
        matrix_id="rna",
        dimensions=[2, 1],
        feature_id=["feature_1", "feature_2"],
        values=[[1.0], [2.0]],
    )
    annotation_snapshot = _feature_annotation_snapshot(discovery, matrix)
    annotation_request = _request_v4(
        discovery,
        matrix_stage,
        "scwbpyv3reference::feature_annotation_probe",
        {"matrix": matrix},
        registry_snapshots={"feature_annotations": annotation_snapshot},
    )
    annotation_receipt, _ = execute_operation_pack_v4(annotation_request, discovery, matrix_stage)
    annotations = json.loads((matrix_stage / "outputs/annotations/payload.json").read_text())
    assert annotations["rows"][1] == {"feature_id": "feature_2", "feature_type": None}
    assert annotation_receipt["registry_reads"][0]["descriptor"]["use_count"] == 1


def test_authenticated_core_payload_mutation_fails_before_publication(installed_v3, tmp_path):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    stage = tmp_path / "mutated-session-axis"
    stage.mkdir()
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::session_axis_probe",
        {},
        registry_snapshots={"session_cells": _session_axis_snapshot},
    )
    path = stage / "registry_reads/session_cells/payload.json"
    snapshot = json.loads(path.read_text())
    snapshot["cell_id"] = ["c1", "c2", "c3"]
    path.write_text(canonical_json(snapshot))
    assert _error_code(execute_operation_pack_v4, request, discovery, stage) == (
        "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )
    assert not (stage / "outputs").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: [value[field].pop() for field in ("cell_id", "key", "value")],
        lambda value: [
            value[field].append(value[field][-1]) for field in ("cell_id", "key", "value")
        ],
        lambda value: value["cell_id"].__setitem__(1, value["cell_id"][0]),
        lambda value: value["cell_id"].reverse(),
        lambda value: value["key"].reverse(),
        lambda value: value["value"].__setitem__(0, None),
        lambda value: value["value"].__setitem__(0, "not-continuous"),
    ],
)
def test_cardinality_adversarial_rows_fail_closed(mutation):
    payload = _family_payload()
    mutation(payload)
    inputs = {"matrix": {"kind": "matrix", "cell_id": ["c2", "c1", "c3"]}}
    assert (
        _error_code(
            _validate_cardinality_payload,
            _cardinality(),
            payload,
            inputs,
            {"value_kind": "continuous"},
        )
        == "PYBENCH_OUTPUT_INVALID"
    )


def test_ancestry_reader_is_single_use():
    readers = V4Readers({}, provenance_values={"ancestry": {"artifact_id": "artifact_1"}})
    assert readers["ancestry"]() == {"artifact_id": "artifact_1"}
    assert _error_code(readers["ancestry"]) == "PYBENCH_CAPABILITY_MISMATCH"


def test_frozen_schemas_reject_unknown_nested_successor_fields():
    schema = json.loads(
        protocol_asset("operation-pack-transition-request-v3.schema.json").read_text()
    )
    grant_schema = copy.deepcopy(schema)
    grant_schema["$ref"] = "#/$defs/cardinality_grant"
    case = _cardinality()
    case["cell_axis_source"]["ambient_bundle_path"] = "/private/bundle"
    assert list(Draft202012Validator(grant_schema).iter_errors(case))
