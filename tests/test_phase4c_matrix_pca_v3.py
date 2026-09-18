from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

import pytest

from pybench.operation_packs import OperationPackError, canonical_json
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v3 import activate_operation_pack_v3, validate_manifest_v3
from pybench.protocol_v4 import execute_operation_pack_v4, validate_receipt_v4, validate_request_v4
from test_operation_pack_v3_successor import (
    TOKEN,
    _add_typed_slice_contract,
    _request_v4,
    _serve,
)
from test_operation_pack_v3_successor import (
    installed_v3 as _installed_v3,
)
from test_operation_pack_v3_typed_slice import _axis


@pytest.fixture(scope="module")
def installed_v3():
    yield from _installed_v3.__wrapped__()


@pytest.fixture(scope="module")
def active_v3(installed_v3):
    activate_operation_pack_v3(installed_v3)
    return installed_v3


def _matrix() -> dict:
    cells = ["c2", "c1", "c4", "c3"]
    features = ["g3", "g1", "g5", "g2", "g4"]
    return {
        "kind": "matrix",
        "dimensions": [len(features), len(cells)],
        "feature_id": features,
        "cell_id": cells,
        "values": [[float(row + column) for column in range(4)] for row in range(5)],
        "storage_profile": "matrix_backed_chunked_v1",
        "meta": {},
    }


def _refresh_bindings(request: dict) -> None:
    for output in request["outputs"]:
        descriptor = output["descriptor"]
        descriptor.pop("grant_identity", None)
        descriptor["grant_identity"] = _identity(
            "scworkbench.operation_pack.output_grant.v3",
            descriptor,
            "descriptor excluding grant_identity",
        )
    capability = request["execution_capability"]
    capability["writer_grants"] = [
        {
            "output_name": output["name"],
            "writer": output["descriptor"]["writer"],
            "grant_identity": output["descriptor"]["grant_identity"],
        }
        for output in request["outputs"]
    ]
    capability.pop("capability_identity", None)
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v3",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity", None)
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v3",
        request,
        "request excluding request_identity",
    )


def _typed_request(discovery, stage: Path, operation: str, params: dict) -> tuple[dict, dict]:
    matrix = _matrix()
    request = _request_v4(discovery, stage, operation, {"matrix": matrix}, params)
    (stage / "inputs" / "matrix" / "payload.json").unlink()
    (stage / "inputs" / "matrix").rmdir()
    (stage / "inputs").rmdir()
    grant = _add_typed_slice_contract(request, stage, selected_features=matrix["feature_id"])
    grant["cells_axis"] = _axis(stage, "matrix_chunks", "cells", matrix["cell_id"])
    grant["requested_chunk_size"] = params["chunk_size"]
    grant["max_rows"] = len(matrix["feature_id"])
    grant["max_columns"] = params["chunk_size"]
    grant["max_elements"] = len(matrix["feature_id"]) * params["chunk_size"]
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    capability = request["execution_capability"]
    typed = next(item for item in capability["reader_grants"] if "typed_slice_name" in item)
    typed["grant_identity"] = grant["grant_identity"]
    capability["typed_slice_grant_identities"] = [
        {key: grant["grant_identity"][key] for key in ("scheme", "sha256")}
    ]
    _refresh_bindings(request)
    return request, grant


def _publication(request: dict, operation: dict, output_name: str, target: str) -> None:
    output = next(item for item in request["outputs"] if item["name"] == output_name)
    group = next(
        item["promotion_group"] for item in operation["outputs"] if item["name"] == output_name
    )
    closure = sorted(
        item["name"] for item in operation["outputs"] if item["promotion_group"] == group
    )
    preimage = {
        "state": "absent",
        "registry_absence_identity": _identity(
            "scworkbench.operation_pack.registry_absence.v1",
            {
                "kind": output["descriptor"]["kind"],
                "id": target,
                "source_state_identity": request["transition"]["source_state_identity"],
            },
        ),
        "transition_source_state_identity": request["transition"]["source_state_identity"],
    }
    grant = {
        "schema": "scworkbench.operation_pack.materialization_publication_grant.v1",
        "output": output_name,
        "promotion_group": group,
        "closure_outputs": closure,
        "target_kind": output["descriptor"]["kind"],
        "target_id": target,
        "mode": "add",
        "conflict_policy": "require_absent_v1",
        "preimage": preimage,
    }
    grant["publication_grant_identity"] = _identity(
        grant["schema"], grant, "grant excluding publication_grant_identity"
    )
    output["allocated_id"] = target
    output["descriptor"]["publication_grant"] = grant


def _fragment_contract(request: dict, operation: dict, typed: dict) -> None:
    output = next(item for item in request["outputs"] if item["name"] == "normalized")
    cells = _matrix()["cell_id"]
    fragments = []
    for ordinal, (start, count) in enumerate(((1, 2), (3, 2)), start=1):
        part = cells[start - 1 : start - 1 + count]
        fragments.append(
            {
                "ordinal": ordinal,
                "source_fragment_identity": _identity(
                    "scworkbench.operation_pack.source_matrix_fragment.v1",
                    {"fragment": ordinal},
                ),
                "cell_start": start,
                "cell_count": count,
                "cell_order_identity": _identity("scworkbench.cell_order.v1", part),
            }
        )
    schedule = {
        "schema": "scworkbench.operation_pack.source_fragment_schedule.v1",
        "input_descriptor_identity": typed["input_descriptor_identity"],
        "matrix_content_identity": typed["input_content_identity"],
        "source_fragment_manifest_identity": _identity(
            "scworkbench.operation_pack.source_fragment_manifest.v1", fragments
        ),
        "fragments": fragments,
    }
    schedule["schedule_identity"] = _identity(
        schedule["schema"], schedule, "schedule excluding schedule_identity"
    )
    declaration = next(item for item in operation["outputs"] if item["name"] == "normalized")
    grant = {
        "schema": "scworkbench.operation_pack.matrix_fragment_grant.v1",
        "source_input": "matrix",
        "typed_slice_grant_identity": {
            key: typed["grant_identity"][key] for key in ("scheme", "sha256")
        },
        "expected_dimensions": [5, 4],
        "feature_order_identity": typed["features_axis"]["order_identity"],
        "cell_order_identity": typed["cells_axis"]["order_identity"],
        "source_fragment_schedule": schedule,
        "fragment_policy": declaration["fragment_policy"],
    }
    grant["grant_identity"] = _identity(grant["schema"], grant, "grant excluding grant_identity")
    output["descriptor"]["matrix_fragment_grant"] = grant
    output["descriptor"]["expected_dimensions"] = [5, 4]
    output["descriptor"]["expected_feature_order_identity"] = typed["features_axis"][
        "order_identity"
    ]
    output["descriptor"]["expected_cell_order_identity"] = typed["cells_axis"]["order_identity"]


def _parameter_contract(request: dict, typed: dict) -> None:
    labels = ["PC1", "PC2", "PC3"]
    grant = {
        "schema": "scworkbench.operation_pack.parameter_axis_grant.v1",
        "name": "components",
        "parameter": "n_components",
        "value": 3,
        "minimum": 1,
        "maximum": 3,
        "labels": labels,
        "source": {
            "typed_slice_grant_identity": {
                key: typed["grant_identity"][key] for key in ("scheme", "sha256")
            },
            "cell_axis_count": 4,
            "feature_axis_count": 5,
        },
        "axis_order_identity": _identity("scworkbench.dimension_order.v1", labels),
    }
    grant["grant_identity"] = _identity(grant["schema"], grant, "grant excluding grant_identity")
    request["parameter_axis_grants"] = [grant]
    request["execution_capability"]["parameter_axis_grant_identities"] = [
        {key: grant["grant_identity"][key] for key in ("scheme", "sha256")}
    ]
    dimensions = {
        "space": [4, 3],
        "loadings": [15, 0],
        "variance": [3, 0],
        "summary": [1, 0],
    }
    for output in request["outputs"]:
        descriptor = output["descriptor"]
        descriptor["parameter_axis_grant_identity"] = {
            key: grant["grant_identity"][key] for key in ("scheme", "sha256")
        }
        descriptor["expected_dimensions"] = dimensions[output["name"]]
        if output["name"] == "space":
            descriptor["expected_cell_order_identity"] = typed["cells_axis"]["order_identity"]
            descriptor["expected_dimension_order_identity"] = grant["axis_order_identity"]


def _operation(discovery, name: str) -> dict:
    return next(item for item in discovery.manifest["operations"] if item["qualified_name"] == name)


def _matrix_request(discovery, stage: Path) -> tuple[dict, dict]:
    name = "scwbpyv3reference::matrix_fragment_probe"
    request, typed = _typed_request(
        discovery,
        stage,
        name,
        {"output_matrix_id": "normalized", "overwrite": False, "chunk_size": 2},
    )
    operation = _operation(discovery, name)
    _publication(request, operation, "normalized", "normalized")
    _fragment_contract(request, operation, typed)
    _refresh_bindings(request)
    return request, typed


def _pca_request(discovery, stage: Path) -> tuple[dict, dict]:
    name = "scwbpyv3reference::parameter_axis_probe"
    request, typed = _typed_request(
        discovery,
        stage,
        name,
        {"space_id": "pca", "overwrite": False, "n_components": 3, "chunk_size": 4},
    )
    operation = _operation(discovery, name)
    _publication(request, operation, "space", "pca")
    _parameter_contract(request, typed)
    _refresh_bindings(request)
    return request, typed


def _run_broker(stage: Path, request: dict, grant: dict, calls: int):
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_serve, args=(stage, request, grant, calls, errors), daemon=True
    )
    old = os.environ.get("PYBENCH_OPERATION_PACK_BROKER_TOKEN")
    os.environ["PYBENCH_OPERATION_PACK_BROKER_TOKEN"] = TOKEN
    thread.start()
    return thread, errors, old


def _restore_token(old):
    if old is None:
        os.environ.pop("PYBENCH_OPERATION_PACK_BROKER_TOKEN", None)
    else:
        os.environ["PYBENCH_OPERATION_PACK_BROKER_TOKEN"] = old


def test_matrix_fragments_execute_with_exact_parent_schedule(active_v3, tmp_path):
    request, grant = _matrix_request(active_v3, tmp_path)
    validate_request_v4(request, active_v3)
    thread, errors, old = _run_broker(tmp_path, request, grant, 2)
    try:
        receipt, _ = execute_operation_pack_v4(request, active_v3, tmp_path)
    finally:
        thread.join(timeout=5)
        _restore_token(old)
    assert errors == []
    descriptor = next(item for item in receipt["outputs"] if item["name"] == "normalized")[
        "descriptor"
    ]
    manifest = descriptor["matrix_fragment_manifest"]
    assert [item["source_fragment_ordinal"] for item in manifest["fragments"]] == [1, 2]
    assert (
        manifest["source_fragment_schedule_identity"]
        == request["outputs"][0]["descriptor"]["matrix_fragment_grant"]["source_fragment_schedule"][
            "schedule_identity"
        ]
    )
    assert receipt["publication_observations"][0]["status"] == "validated_not_published"


def test_second_fragment_failure_restores_entire_private_stage(active_v3, tmp_path, monkeypatch):
    request, grant = _matrix_request(active_v3, tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    monkeypatch.setenv("SCWBPYV3_MATRIX_FRAGMENT_FAULT", "second")
    thread, errors, old = _run_broker(tmp_path, request, grant, 2)
    try:
        with pytest.raises(RuntimeError, match="second-fragment"):
            execute_operation_pack_v4(request, active_v3, tmp_path)
    finally:
        thread.join(timeout=5)
        _restore_token(old)
    assert errors == []
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


def test_fragment_payload_mutation_is_rejected_on_stage_revalidation(active_v3, tmp_path):
    request, grant = _matrix_request(active_v3, tmp_path)
    thread, errors, old = _run_broker(tmp_path, request, grant, 2)
    try:
        receipt, _ = execute_operation_pack_v4(request, active_v3, tmp_path)
    finally:
        thread.join(timeout=5)
        _restore_token(old)
    assert errors == []
    path = tmp_path / "outputs" / "normalized" / "fragments" / "000001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["values"][0][0] += 1
    path.write_text(canonical_json(payload), encoding="utf-8")
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v4(receipt, request, active_v3, tmp_path)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"


def test_parameter_axis_executes_exact_four_output_closure(active_v3, tmp_path):
    request, grant = _pca_request(active_v3, tmp_path)
    validate_request_v4(request, active_v3)
    thread, errors, old = _run_broker(tmp_path, request, grant, 1)
    try:
        receipt, _ = execute_operation_pack_v4(request, active_v3, tmp_path)
    finally:
        thread.join(timeout=5)
        _restore_token(old)
    assert errors == []
    outputs = {item["name"]: item for item in receipt["outputs"]}
    assert set(outputs) == {"space", "loadings", "variance", "summary"}
    assert outputs["space"]["descriptor"]["observed_parameter_axis"]["dimensions"] == [4, 3]
    assert outputs["loadings"]["descriptor"]["observed_parameter_axis"]["dimensions"] == [15, 4]
    assert outputs["variance"]["descriptor"]["observed_parameter_axis"]["dimensions"] == [3, 3]
    assert outputs["summary"]["descriptor"]["observed_parameter_axis"]["echoed_value"] == 3


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("two_columns", "PYBENCH_OUTPUT_INVALID"),
        ("four_columns", "PYBENCH_OUTPUT_INVALID"),
        ("missing_cell", "PYBENCH_OUTPUT_INVALID"),
        ("duplicate_dimension", "PYBENCH_OUTPUT_INVALID"),
        ("partial_output", "PYBENCH_RECEIPT_MISMATCH"),
    ],
)
def test_parameter_axis_output_mutations_fail_before_receipt_publication(
    active_v3, tmp_path, monkeypatch, fault, code
):
    request, grant = _pca_request(active_v3, tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    monkeypatch.setenv("SCWBPYV3_PARAMETER_AXIS_FAULT", fault)
    thread, errors, old = _run_broker(tmp_path, request, grant, 1)
    try:
        with pytest.raises(OperationPackError) as caught:
            execute_operation_pack_v4(request, active_v3, tmp_path)
    finally:
        thread.join(timeout=5)
        _restore_token(old)
    assert caught.value.code == code
    assert errors == []
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda request: request["parameter_axis_grants"][0].update(value=4),
            "PYBENCH_CAPABILITY_MISMATCH",
        ),
        (
            lambda request: request["outputs"][0]["descriptor"].update(expected_dimensions=[4, 2]),
            "PYBENCH_CAPABILITY_MISMATCH",
        ),
        (lambda request: request["outputs"].pop(), "PYBENCH_CAPABILITY_MISMATCH"),
    ],
)
def test_parameter_axis_request_mutations_fail_closed(installed_v3, tmp_path, mutation, code):
    request, _ = _pca_request(installed_v3, tmp_path)
    mutation(request)
    _refresh_bindings(request)
    with pytest.raises(OperationPackError) as caught:
        validate_request_v4(request, installed_v3)
    assert caught.value.code == code


def test_replace_preimage_and_publication_grant_are_authenticated(installed_v3, tmp_path):
    request, _ = _pca_request(installed_v3, tmp_path)
    publication = request["outputs"][0]["descriptor"]["publication_grant"]
    publication["preimage"] = {
        "state": "present",
        "registry_identity": _identity("fixture.registry", {"v": 1}),
        "content_identity": _identity("fixture.content", {"v": 1}),
        "payload_manifest_identity": _identity("fixture.payload", {"v": 1}),
        "provenance_identity": _identity("fixture.provenance", {"v": 1}),
        "lifecycle_identity": _identity("fixture.lifecycle", {"v": 1}),
        "transition_source_state_identity": request["transition"]["source_state_identity"],
    }
    publication["mode"] = "replace"
    publication["conflict_policy"] = "replace_exact_preimage_v1"
    request["params"]["overwrite"] = True
    publication.pop("publication_grant_identity")
    publication["publication_grant_identity"] = _identity(
        publication["schema"], publication, "grant excluding publication_grant_identity"
    )
    _refresh_bindings(request)
    validate_request_v4(request, installed_v3)
    mutant = copy.deepcopy(request)
    mutant["outputs"][0]["descriptor"]["publication_grant"]["preimage"]["content_identity"] = (
        _identity("fixture.content", {"v": 2})
    )
    _refresh_bindings(mutant)
    with pytest.raises(OperationPackError) as caught:
        validate_request_v4(mutant, installed_v3)
    assert caught.value.code == "PYBENCH_CAPABILITY_MISMATCH"


def test_manifest_fragment_publication_and_axis_fields_are_closed(installed_v3):
    validate_manifest_v3(installed_v3.manifest)
    missing_publication = copy.deepcopy(installed_v3.manifest)
    operation = next(
        item
        for item in missing_publication["operations"]
        if item["qualified_name"] == "scwbpyv3reference::matrix_fragment_probe"
    )
    operation.pop("publication_policies")
    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(missing_publication)
    assert caught.value.code == "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID"

    collided_fields = copy.deepcopy(installed_v3.manifest)
    operation = next(
        item
        for item in collided_fields["operations"]
        if item["qualified_name"] == "scwbpyv3reference::parameter_axis_probe"
    )
    operation["dimension_policies"][1]["axis_label_field"] = "feature_id"
    with pytest.raises(OperationPackError) as caught:
        validate_manifest_v3(collided_fields)
    assert caught.value.code == "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID"
