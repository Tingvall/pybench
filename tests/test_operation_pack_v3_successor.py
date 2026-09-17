from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import json
import subprocess
import sys
from dataclasses import replace
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
from pybench.protocol_v3 import _Write
from pybench.protocol_v4 import (
    V4Readers,
    _merge_fragments,
    _validate_cardinality_payload,
    _validate_fragment,
    _validate_provenance_snapshot,
    execute_operation_pack_v4,
    validate_python_runtime_sidecar_v4,
    validate_receipt_v4,
    validate_request_v4,
)
from v3_helpers import fixture_inputs, make_request

FIXTURE = Path(__file__).parent / "fixtures" / "operation-pack-v3-python-reference"
SHARED_DIGESTS = {
    "operation-pack-v3.schema.json": "a4f095902b5dcb71bc4d84b5b248997b3c9748ef7e51e91c7ab6b77483efee8b",
    "operation-pack-transition-request-v3.schema.json": "9ff65685bcad2358b26a325beeed617cd6695ebf629b5c6412df3762ff929052",
    "operation-pack-transition-receipt-v3.schema.json": "e6ab7b7eeac2a4b60a4da405cdd70425d417a2fc61689dca585335f3f5eec25d",
    "fixtures/operation-pack-v3/identities.json": "31e104e2167a5c5c9b147afe280bd7b36220c3d61f1773445ded6ee9d52bbbee",
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


def _request_v4(discovery, stage, operation_name, inputs, params=None):
    source_operation = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == operation_name
    )
    if source_operation.get("chunking", {}).get("axis") == "cells":
        manifest = copy.deepcopy(discovery.manifest)
        shadow = next(
            item for item in manifest["operations"] if item["qualified_name"] == operation_name
        )
        shadow.pop("chunking")
        shadow.pop("dimension_policies")
        request = make_request(
            replace(discovery, manifest=manifest), stage, operation_name, inputs, params
        )
        _add_cell_chunk_contract(request, source_operation, inputs, params or {})
    else:
        request = make_request(discovery, stage, operation_name, inputs, params)
    request["schema"] = "scworkbench.operation_pack.transition_request.v3"
    request["process_protocol_version"] = "pybench-op-v4"
    request["pack_abi_version"] = "3"
    if source_operation.get("provenance_reads"):
        _add_provenance_contract(request, stage)
    if any(
        policy.get("policy") == "authenticated_output_axes_v1"
        for policy in source_operation.get("dimension_policies", [])
    ) and not source_operation.get("chunking"):
        _add_cardinality_contracts(request, source_operation, inputs, params or {})
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


def _add_cardinality_contracts(request, operation, inputs, params):
    input_records = {item["name"]: item["descriptor"] for item in request["inputs"]}
    output_records = {item["name"]: item for item in request["outputs"]}
    for policy in operation.get("dimension_policies", []):
        if policy.get("policy") != "authenticated_output_axes_v1":
            continue
        source_name = policy["cell_axis"]["input"]
        source = inputs[source_name]
        source_descriptor = input_records[source_name]
        source_identity = {
            "scheme": source_descriptor["descriptor_identity"]["scheme"],
            "sha256": source_descriptor["descriptor_identity"]["sha256"],
        }
        cells = list(dict.fromkeys(source["cell_id"]))
        grant = {
            "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
            "output": policy["output"],
            "kind": output_records[policy["output"]]["descriptor"]["kind"],
            "policy": policy["policy"],
            "shape": policy["shape"],
            "row_order": policy["row_order"],
            "missing_values": policy["missing_values"],
            "cell_axis_source": {
                "input_name": source_name,
                "input_descriptor_identity": source_identity,
                "derivation": policy["cell_axis"]["source"],
            },
            "cell_axis_count": len(cells),
            "cell_axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
        }
        if policy["shape"] == "cell_key_cartesian":
            resolver = policy["key_axis"]
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
            rows = [[key, cell] for key in keys for cell in cells]
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
        descriptor["expected_cell_order_identity"] = _identity(
            "scworkbench.cell_order.v1", output_cells
        )


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
            {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()},
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


def _add_cell_chunk_contract(request, operation, inputs, params):
    matrix = inputs["matrix"]
    cells = matrix["cell_id"]
    input_descriptor = request["inputs"][0]["descriptor"]
    input_identity = {
        "scheme": input_descriptor["descriptor_identity"]["scheme"],
        "sha256": input_descriptor["descriptor_identity"]["sha256"],
    }
    scores = next(item for item in request["outputs"] if item["name"] == "scores")
    resolution = {
        "source": "resolved_parameter_feature_groups_v1",
        "groups_parameter": "features",
        "matrix_input": "matrix",
        "matrix_input_descriptor_identity": input_identity,
        "matrix_feature_axis_identity": input_descriptor["feature_order_identity"],
        "name_prefix_parameter": "name",
        "name_prefix_value": params["name"],
        "matching": "exact_feature_id_v1",
        "normalization": "ordered_unique_features_v1",
        "generated_names": "legacy_module_group_names_v1",
        "missing_feature_policy": "drop",
        "empty_group_policy": "error",
        "zero_key_policy": "error",
        "groups": [
            {
                "key": "module1",
                "features_input": params["features"],
                "features_matched": params["features"],
                "features_missing": [],
            }
        ],
    }
    cardinality = {
        "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
        "output": "scores",
        "kind": "cell_vector_family",
        "policy": "authenticated_output_axes_v1",
        "shape": "cell_key_cartesian",
        "row_order": "key_major_then_cell_v1",
        "missing_values": "forbidden",
        "cell_axis_source": {
            "input_name": "matrix",
            "input_descriptor_identity": input_identity,
            "derivation": "effective_cells_v1",
        },
        "cell_axis_count": len(cells),
        "cell_axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
        "key_axis_count": 1,
        "key_axis": ["module1"],
        "key_axis_resolution": resolution,
        "key_axis_order_identity": _identity("scworkbench.row_order.v1", ["module1"]),
        "expected_row_count": len(cells),
        "row_order_identity": _identity(
            "scworkbench.row_order.v1", [["module1", cell] for cell in cells]
        ),
    }
    cardinality["cardinality_grant_identity"] = _identity(
        "scworkbench.operation_pack.output_cardinality_grant.v1",
        cardinality,
        "grant excluding cardinality_grant_identity",
    )
    scores["descriptor"]["cardinality_grant"] = cardinality
    scores["descriptor"]["expected_dimensions"] = [len(cells), 3]
    scores["descriptor"]["expected_cell_order_identity"] = _identity(
        "scworkbench.cell_order.v1", cells
    )
    summary = operation["chunking"]["summary"]
    finalizer = {
        "schema": "scworkbench.operation_pack.summary_finalizer_definition.v1",
        "interface": summary["interface"],
        "callable": summary["callable"],
        "isolation": summary["isolation"],
        "merged_input": "scores",
        "summary_output": "summary",
        "metadata_sources": summary["metadata_sources"],
    }
    finalizer["finalizer_definition_identity"] = _identity(
        "scworkbench.operation_pack.summary_finalizer_definition.v1",
        finalizer,
        "definition excluding finalizer_definition_identity",
    )
    chunk_size = params["chunk_size"]
    effective = len(cells) if chunk_size is None else min(chunk_size, len(cells))
    batches = []
    for index, start0 in enumerate(range(0, len(cells), effective), start=1):
        batch_cells = cells[start0 : start0 + effective]
        fragment = {
            "schema": "scworkbench.operation_pack.chunk_fragment_grant.v1",
            "fragment_output": "scores",
            "parent_cardinality_grant_identity": cardinality["cardinality_grant_identity"],
            "input_descriptor_identity": input_identity,
            "batch_index": index,
            "batch_start": start0 + 1,
            "batch_length": len(batch_cells),
            "batch_cell_order_identity": _identity("scworkbench.cell_order.v1", batch_cells),
            "key_axis_count": 1,
            "key_axis": ["module1"],
            "key_axis_order_identity": cardinality["key_axis_order_identity"],
            "expected_row_count": len(batch_cells),
            "row_order_identity": _identity(
                "scworkbench.row_order.v1", [["module1", cell] for cell in batch_cells]
            ),
            "missing_values": "forbidden",
        }
        fragment["fragment_grant_identity"] = _identity(
            "scworkbench.operation_pack.chunk_fragment_grant.v1",
            fragment,
            "grant excluding fragment_grant_identity",
        )
        batches.append(
            {
                "index": index,
                "start": start0 + 1,
                "length": len(batch_cells),
                "cell_order_identity": fragment["batch_cell_order_identity"],
                "fragment_grant": fragment,
            }
        )
    schedule = {
        "schema": "scworkbench.operation_pack.cell_chunk_schedule.v1",
        "input_name": "matrix",
        "input_descriptor_identity": input_identity,
        "axis": "cells",
        "axis_count": len(cells),
        "axis_order_identity": _identity("scworkbench.cell_order.v1", cells),
        "requested_chunk_size": chunk_size,
        "effective_chunk_size": effective,
        "batches": batches,
        "merge_policy": "row_bind_cell_vector_family_v1",
        "fragment_output": "scores",
        "summary_output": "summary",
        "summary_finalizer_definition": finalizer,
    }
    schedule["schedule_identity"] = _identity(
        "scworkbench.operation_pack.cell_chunk_schedule.v1",
        schedule,
        "schedule excluding schedule_identity",
    )
    request["chunk_schedules"] = [schedule]
    request["execution_capability"]["chunk_schedule_identities"] = [
        {
            "scheme": schedule["schedule_identity"]["scheme"],
            "sha256": schedule["schedule_identity"]["sha256"],
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
                "scworkbench.row_order.v1", [[key, cell] for key in keys for cell in cells]
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


def test_shared_successor_assets_and_identity_corpus_are_exact():
    for name, expected in SHARED_DIGESTS.items():
        assert hashlib.sha256(protocol_asset(name).read_bytes()).hexdigest() == expected
    corpus = json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())
    assert corpus["schema"] == "scworkbench.operation_pack.v3.identity_fixtures.v1"
    for case in corpus["cases"]:
        assert canonical_sha256(case["preimage"]) == case["identity"]["sha256"]
        assert case["identity"] in case["value"].values()


def test_manifest_v3_is_explicit_and_v2_reader_remains_separate():
    manifest = json.loads((FIXTURE / "operation-pack.json").read_text())
    assert validate_manifest_v3(manifest)["pack_abi_version"] == "3"
    wrong = copy.deepcopy(manifest)
    wrong["pack_abi_version"] = "2"
    assert _error_code(validate_manifest_v3, wrong) == "OPERATION_PACK_MANIFEST_SCHEMA_INVALID"


def test_v3_discovery_activation_and_plain_v4_execution(installed_v3, tmp_path):
    discovery = installed_v3
    registration = activate_operation_pack_v3(discovery)
    assert set(registration.finalizers) == {"score_summary"}
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


def test_cell_chunk_schedule_merge_and_isolated_finalizer_are_receipted(installed_v3, tmp_path):
    discovery = installed_v3
    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    identities = []
    last_execution = None
    for chunk_size in (3, 2):
        stage = tmp_path / f"chunks-{chunk_size}"
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
            {"features": ["feature_1"], "name": "Module", "chunk_size": chunk_size},
        )
        assert validate_request_v4(request, discovery) == request
        receipt, _sidecar = execute_operation_pack_v4(request, discovery, stage)
        execution = receipt["chunk_executions"][0]
        last_execution = (receipt, request, stage)
        assert [item["index"] for item in execution["observed_chunks"]] == list(
            range(1, len(request["chunk_schedules"][0]["batches"]) + 1)
        )
        assert execution["summary_source_identity"] == execution["merged_family_content_identity"]
        identities.append(
            (
                execution["merged_family_content_identity"],
                execution["summary_content_identity"],
            )
        )
    assert identities[0] == identities[1]

    receipt, request, stage = last_execution
    forged = copy.deepcopy(receipt)
    execution = forged["chunk_executions"][0]
    execution["summary_content_identity"] = _identity("fixture.summary", {"wrong": True})
    invocation = {
        "schema": "scworkbench.operation_pack.summary_finalizer_invocation.v1",
        "finalizer_definition_identity": execution["finalizer_definition_identity"],
        "merged_family_content_identity": execution["merged_family_content_identity"],
        "finalizer_metadata_identity": execution["finalizer_metadata_identity"],
        "summary_content_identity": execution["summary_content_identity"],
    }
    invocation["finalizer_invocation_identity"] = _identity(
        "scworkbench.operation_pack.summary_finalizer_invocation.v1",
        invocation,
        "invocation excluding finalizer_invocation_identity",
    )
    execution["finalizer_invocation_identity"] = invocation["finalizer_invocation_identity"]
    execution.pop("chunk_execution_identity")
    execution["chunk_execution_identity"] = _identity(
        "scworkbench.operation_pack.cell_chunk_observation.v1",
        execution,
        "observation excluding chunk_execution_identity",
    )
    forged.pop("receipt_identity")
    forged["receipt_identity"] = _identity(
        "scworkbench.operation_pack.transition_receipt.v3",
        forged,
        "receipt excluding receipt_identity",
    )
    assert (
        _error_code(validate_receipt_v4, forged, request, discovery, stage)
        == "PYBENCH_RECEIPT_MISMATCH"
    )


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
        {"signals": _family_payload()},
    )
    assert validate_request_v4(request, discovery) == request
    receipt, _sidecar = execute_operation_pack_v4(request, discovery, stage)
    assert receipt["provenance_reads"][0]["use_count"] == 1
    assert (
        receipt["provenance_reads"][0]["snapshot_identity"]
        == request["provenance_reads"][0]["snapshot_identity"]
    )

    unload_operation_pack_v3(discovery.manifest["namespace"])
    activate_operation_pack_v3(discovery)
    changed_stage = tmp_path / "ancestry-mutated"
    changed_stage.mkdir()
    changed = _request_v4(
        discovery,
        changed_stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _family_payload()},
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


@pytest.mark.parametrize("condition", ["stale", "superseded"])
def test_artifact_ancestry_rejects_noncurrent_lifecycle(installed_v3, tmp_path, condition):
    stage = tmp_path / condition
    stage.mkdir()
    request = _request_v4(
        installed_v3,
        stage,
        "scwbpyv3reference::ancestry_probe",
        {"signals": _family_payload()},
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
    family = _family_payload()

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
    assert family_output == family
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


def test_fragment_validation_merge_and_ancestry_reader_are_deterministic(tmp_path):
    grant = _cardinality()
    cells = ["c2", "c1"]
    fragment_grant = {
        "schema": "scworkbench.operation_pack.chunk_fragment_grant.v1",
        "fragment_output": "scores",
        "parent_cardinality_grant_identity": grant["cardinality_grant_identity"],
        "input_descriptor_identity": grant["cell_axis_source"]["input_descriptor_identity"],
        "batch_index": 1,
        "batch_start": 1,
        "batch_length": 2,
        "batch_cell_order_identity": _identity("scworkbench.cell_order.v1", cells),
        "key_axis_count": 2,
        "key_axis": ["sigB", "sigA"],
        "key_axis_order_identity": _identity("scworkbench.row_order.v1", ["sigB", "sigA"]),
        "expected_row_count": 4,
        "row_order_identity": _identity(
            "scworkbench.row_order.v1",
            [[key, cell] for key in ["sigB", "sigA"] for cell in cells],
        ),
        "missing_values": "forbidden",
    }
    fragment_grant["fragment_grant_identity"] = _identity(
        "scworkbench.operation_pack.chunk_fragment_grant.v1",
        fragment_grant,
        "grant excluding fragment_grant_identity",
    )
    fragment = {
        "kind": "cell_vector_family",
        "dimensions": [4, 3],
        "cell_id": cells + cells,
        "key": ["sigB", "sigB", "sigA", "sigA"],
        "value": [1.0, 2.0, 4.0, 5.0],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }
    assert _validate_fragment(fragment_grant, fragment, {})["cells"] == cells
    schedule = {"batches": [{"fragment_grant": fragment_grant}]}
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(fragment))
    merged = _merge_fragments(schedule, {1: _Write(fragment, path, {}, "write_chunk")})
    assert list(zip(merged["key"], merged["cell_id"], strict=True)) == [
        ("sigB", "c2"),
        ("sigB", "c1"),
        ("sigA", "c2"),
        ("sigA", "c1"),
    ]
    assert _error_code(_merge_fragments, schedule, {}) == "PYBENCH_RECEIPT_MISMATCH"
    reversed_fragment = copy.deepcopy(fragment)
    reversed_fragment["cell_id"].reverse()
    assert (
        _error_code(_validate_fragment, fragment_grant, reversed_fragment, {})
        == "PYBENCH_OUTPUT_INVALID"
    )
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
