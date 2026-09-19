from __future__ import annotations

import copy
from pathlib import Path

import pytest

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v3 import activate_operation_pack_v3
from pybench.phase4d_contracts import (
    inventory_model_tree,
    tree_payload_identity,
    validate_private_model_copy,
    validate_space_2d_candidates,
    validate_tree_manifest,
)
from pybench.protocol_v4 import (
    V4Writers,
    execute_operation_pack_v4,
    validate_receipt_v4,
    validate_request_v4,
)
from test_operation_pack_v3_successor import _request_v4
from test_operation_pack_v3_successor import installed_v3 as _installed_v3


@pytest.fixture(scope="module")
def installed_v3():
    yield from _installed_v3.__wrapped__()


@pytest.fixture(scope="module")
def active_v3(installed_v3):
    activate_operation_pack_v3(installed_v3)
    return installed_v3


def _error_code(function, *args):
    with pytest.raises(OperationPackError) as caught:
        function(*args)
    return caught.value.code


def _tree_grant(root: Path) -> dict:
    manifest, _ = inventory_model_tree(
        root,
        engine="synthetic",
        model_type="synthetic_embedding",
        primary_path="model.bin",
        maximum_file_count=4,
        maximum_total_bytes=1024,
    )
    grant = {
        "schema": "scworkbench.operation_pack.contained_model_read_grant.v1",
        "name": "model_payload",
        "input_name": "model",
        "reader": "read_contained_model_payload",
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "input_descriptor_identity": _identity("input", {}),
        "model_content_identity": _identity("model", {}),
        "model_linkage_identity": _identity("linkage", {}),
        "contained_payload_identity": _identity("contained", {}),
        "payload_manifest_identity": tree_payload_identity(manifest),
        "tree_manifest": manifest,
        "source_space_binding": {
            "input_name": "space",
            "input_descriptor_identity": _identity("input", {"space": True}),
            "selected_dimension_count": 2,
            "selected_dimension_order_identity": _identity(
                "scworkbench.dimension_order.v1", ["D2", "D1"]
            ),
        },
        "relative_root": "model_payload_reads/model_payload/tree",
        "access": "verified_private_copy_read_only_v1",
        "call_discipline": "exactly_once",
    }
    grant["grant_identity"] = _identity(grant["schema"], grant, "grant excluding grant_identity")
    return grant


def _refresh_request(request: dict) -> None:
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


def _refresh_output(output: dict) -> None:
    descriptor = output["descriptor"]
    descriptor.pop("grant_identity", None)
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v3",
        descriptor,
        "descriptor excluding grant_identity",
    )


def _space() -> dict:
    return {
        "kind": "space",
        "space_id": "source-space",
        "dimensions": [3, 3],
        "cell_id": ["c2", "c1", "c3"],
        "dimension_id": ["D1", "D2", "D3"],
        "values": [[2.0, 1.0, 0.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
        "storage_profile": "space_dense_v1",
    }


def _source_binding(descriptor: dict, dims: list[int]) -> dict:
    labels = [f"D{index}" for index in dims]
    return {
        "input_name": "space",
        "input_descriptor_identity": {
            key: descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
        },
        "content_identity": descriptor["content_identity"],
        "cell_order_identity": descriptor["cell_order_identity"],
        "dimensions": descriptor["dimensions"],
        "selected_dimension_count": len(dims),
        "selected_dimension_order_identity": _identity("scworkbench.dimension_order.v1", labels),
    }


def _absent_preimage(request: dict, target: str) -> dict:
    return {
        "state": "absent",
        "registry_absence_identity": _identity(
            "scworkbench.operation_pack.materialization_registry_absence.v1",
            {"kind": "space_2d", "target": target},
        ),
        "transition_source_state_identity": request["transition"]["source_state_identity"],
    }


def _fit_request(discovery, stage: Path) -> dict:
    dims = [2, 1]
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::contained_model_fit_probe",
        {"space": _space()},
        {"dims": dims, "space_id_out": "umap-fit"},
    )
    source_descriptor = request["inputs"][0]["descriptor"]
    source = _source_binding(source_descriptor, dims)
    embedding, model = request["outputs"]
    embedding["allocated_id"] = "umap-fit"
    embedding["descriptor"]["expected_dimensions"] = [3, 2]
    embedding["descriptor"]["expected_cell_order_identity"] = source["cell_order_identity"]
    model["descriptor"]["expected_model_linkage"] = {
        "mode": "input",
        "reference_name": "space",
        "reference_identity": source["input_descriptor_identity"],
    }
    write_grant = {
        "schema": "scworkbench.operation_pack.contained_model_write_grant.v1",
        "output": "model",
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "primary_path": "model.bin",
        "relative_root": "outputs/model/model_tree",
        "source_space": source,
        "paired_space_output": {
            "name": "embedding",
            "allocated_id": embedding["allocated_id"],
            "expected_dimensions": [3, 2],
            "expected_cell_order_identity": source["cell_order_identity"],
        },
        "limits": {"maximum_file_count": 4, "maximum_total_bytes": 1024},
    }
    write_grant["write_grant_identity"] = _identity(
        write_grant["schema"], write_grant, "grant excluding write_grant_identity"
    )
    model["descriptor"]["contained_model_write_grant"] = write_grant
    source_model = {
        "source": "sibling_output",
        "name": "model",
        "allocated_id": model["allocated_id"],
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "source_space_binding_identity": _identity(
            "scworkbench.operation_pack.model_source_space.v1",
            {
                key: source[key]
                for key in (
                    "input_name",
                    "input_descriptor_identity",
                    "selected_dimension_count",
                    "selected_dimension_order_identity",
                )
            },
        ),
    }
    publication = {
        "schema": "scworkbench.operation_pack.space_2d_publication_grant.v1",
        "output": "embedding",
        "promotion_group": "fit",
        "closure_outputs": ["embedding", "model"],
        "target_kind": "space_2d",
        "target_id": "umap-fit",
        "mode": "add",
        "conflict_policy": "require_absent_v1",
        "overwrite_existing_cells": False,
        "selected_cells": {
            "count": 3,
            "order_identity": source["cell_order_identity"],
            "membership_identity": _identity(
                "scworkbench.cell_membership.v1", sorted(_space()["cell_id"])
            ),
        },
        "recipient_cell_axis_identity": _identity(
            "scworkbench.cell_order.v1", ["c2", "c1", "c3", "c4"]
        ),
        "expected_final_cell_order_identity": source["cell_order_identity"],
        "source_model_binding": source_model,
        "preimage": _absent_preimage(request, "umap-fit"),
    }
    publication["publication_grant_identity"] = _identity(
        publication["schema"], publication, "grant excluding publication_grant_identity"
    )
    embedding["descriptor"]["publication_grant"] = publication
    _refresh_output(embedding)
    _refresh_output(model)
    _refresh_request(request)
    return request


def _transform_request(discovery, stage: Path) -> dict:
    dims = [2, 1]
    placeholder_model = {
        "kind": "model_ref",
        "dimensions": [1],
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "in_space_id": "source-space",
        "payload_tree": {"model.bin": "placeholder"},
        "storage_profile": "model_ref_contained_tree_v1",
    }
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::contained_model_transform_probe",
        {"space": _space(), "model": placeholder_model},
        {"dims": dims, "space_id_out": "umap-transform", "overwrite": False},
    )
    input_records = {item["name"]: item["descriptor"] for item in request["inputs"]}
    source = _source_binding(input_records["space"], dims)
    model_root = stage / "model_payload_reads" / "model_payload" / "tree"
    model_root.mkdir(parents=True)
    (model_root / "model.bin").write_bytes(b"synthetic-contained-model")
    (model_root / "state").mkdir()
    (model_root / "state" / "axes.txt").write_bytes(b"D1,D2,D3")
    tree, _paths = inventory_model_tree(
        model_root,
        engine="synthetic",
        model_type="synthetic_embedding",
        primary_path="model.bin",
        maximum_file_count=4,
        maximum_total_bytes=1024,
    )
    linkage = {
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "source_space_id": "source-space",
        "selected_dimension_count": 2,
        "selected_dimension_order_identity": source["selected_dimension_order_identity"],
    }
    linkage["linkage_identity"] = _identity(
        "scworkbench.operation_pack.model_linkage.v1",
        linkage,
        "linkage excluding linkage_identity",
    )
    linkage_identity = {key: linkage["linkage_identity"][key] for key in ("scheme", "sha256")}
    contained_identity = _identity(
        "scworkbench.operation_pack.contained_payload.v2",
        {key: tree["manifest_identity"][key] for key in ("scheme", "sha256")},
    )
    content_identity = _identity(
        "scworkbench.model_ref.content.v1",
        {
            "engine": "synthetic",
            "model_type": "synthetic_embedding",
            "linkage_identity": linkage_identity,
            "tree_manifest_identity": {
                key: tree["manifest_identity"][key] for key in ("scheme", "sha256")
            },
        },
    )
    model_descriptor = input_records["model"]
    model_descriptor.update(
        content_identity=content_identity,
        model_linkage_identity=linkage_identity,
        contained_payload_identity=contained_identity,
        payload_manifest_identity=tree_payload_identity(tree),
    )
    model_descriptor.pop("descriptor_identity")
    model_descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.input_descriptor.v2",
        model_descriptor,
        "descriptor excluding descriptor_identity",
    )
    model_grant = {
        "schema": "scworkbench.operation_pack.contained_model_read_grant.v1",
        "name": "model_payload",
        "input_name": "model",
        "reader": "read_contained_model_payload",
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "input_descriptor_identity": {
            key: model_descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
        },
        "model_content_identity": content_identity,
        "model_linkage_identity": linkage_identity,
        "contained_payload_identity": contained_identity,
        "payload_manifest_identity": tree_payload_identity(tree),
        "tree_manifest": tree,
        "source_space_binding": {
            key: source[key]
            for key in (
                "input_name",
                "input_descriptor_identity",
                "selected_dimension_count",
                "selected_dimension_order_identity",
            )
        },
        "relative_root": "model_payload_reads/model_payload/tree",
        "access": "verified_private_copy_read_only_v1",
        "call_discipline": "exactly_once",
    }
    model_grant["grant_identity"] = _identity(
        model_grant["schema"], model_grant, "grant excluding grant_identity"
    )
    request["model_payload_grants"] = [model_grant]
    embedding, _summary = request["outputs"]
    embedding["allocated_id"] = "umap-transform"
    embedding["descriptor"]["expected_dimensions"] = [3, 2]
    embedding["descriptor"]["expected_cell_order_identity"] = source["cell_order_identity"]
    publication = {
        "schema": "scworkbench.operation_pack.space_2d_publication_grant.v1",
        "output": "embedding",
        "promotion_group": "transform",
        "closure_outputs": ["embedding", "summary"],
        "target_kind": "space_2d",
        "target_id": "umap-transform",
        "mode": "add",
        "conflict_policy": "require_absent_v1",
        "overwrite_existing_cells": False,
        "selected_cells": {
            "count": 3,
            "order_identity": source["cell_order_identity"],
            "membership_identity": _identity(
                "scworkbench.cell_membership.v1", sorted(_space()["cell_id"])
            ),
        },
        "recipient_cell_axis_identity": source["cell_order_identity"],
        "expected_final_cell_order_identity": source["cell_order_identity"],
        "source_model_binding": {
            "source": "input",
            "name": "model",
            "engine": "synthetic",
            "model_type": "synthetic_embedding",
            "model_linkage_identity": linkage_identity,
            "contained_payload_identity": contained_identity,
        },
        "preimage": _absent_preimage(request, "umap-transform"),
    }
    publication["publication_grant_identity"] = _identity(
        publication["schema"], publication, "grant excluding publication_grant_identity"
    )
    embedding["descriptor"]["publication_grant"] = publication
    for output in request["outputs"]:
        _refresh_output(output)
    capability = request["execution_capability"]
    capability["reader_grants"] = [
        item for item in capability["reader_grants"] if item.get("input_name") != "model"
    ]
    capability["reader_grants"].append(
        {
            "model_payload_name": "model_payload",
            "reader": "read_contained_model_payload",
            "grant_identity": model_grant["grant_identity"],
        }
    )
    capability["model_payload_grant_identities"] = [
        {key: model_grant["grant_identity"][key] for key in ("scheme", "sha256")}
    ]
    _refresh_request(request)
    return request


def test_contained_tree_inventory_is_byte_ordered_and_mutation_sensitive(tmp_path: Path):
    root = tmp_path / "tree"
    (root / "weights").mkdir(parents=True)
    (root / "weights" / "z.bin").write_bytes(b"z")
    (root / "model.bin").write_bytes(b"model")
    manifest, paths = inventory_model_tree(
        root,
        engine="synthetic",
        model_type="synthetic_embedding",
        primary_path="model.bin",
        maximum_file_count=4,
        maximum_total_bytes=1024,
    )
    assert [entry["path"] for entry in manifest["entries"]] == [
        "model.bin",
        "weights/z.bin",
    ]
    assert [path.relative_to(root).as_posix() for path in paths] == [
        "model.bin",
        "weights/z.bin",
    ]
    assert validate_tree_manifest(manifest) == manifest
    grant = _tree_grant(root)
    assert validate_private_model_copy(root, grant) == paths
    (root / "model.bin").write_bytes(b"mutated")
    assert _error_code(validate_private_model_copy, root, grant) == (
        "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "a\\b", "a/./b"])
def test_contained_tree_rejects_unsafe_or_noncanonical_paths(tmp_path: Path, relative: str):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "model.bin").write_bytes(b"model")
    grant = _tree_grant(root)
    changed = copy.deepcopy(grant["tree_manifest"])
    changed["entries"][0]["path"] = relative
    changed.pop("manifest_identity")
    changed["manifest_identity"] = _identity(
        changed["schema"], changed, "manifest excluding manifest_identity"
    )
    assert _error_code(validate_tree_manifest, changed) == "PYBENCH_CAPABILITY_MISMATCH"


def test_contained_tree_rejects_symlink_and_empty_file_entries(tmp_path: Path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "model.bin").symlink_to(outside)
    assert (
        _error_code(
            lambda: inventory_model_tree(
                linked,
                engine="synthetic",
                model_type="synthetic_embedding",
                primary_path="model.bin",
                maximum_file_count=4,
                maximum_total_bytes=1024,
            )
        )
        == "PYBENCH_OUT_OF_SCOPE_MUTATION"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "model.bin").write_bytes(b"")
    assert (
        _error_code(
            lambda: inventory_model_tree(
                empty,
                engine="synthetic",
                model_type="synthetic_embedding",
                primary_path="model.bin",
                maximum_file_count=4,
                maximum_total_bytes=1024,
            )
        )
        == "PYBENCH_OUTPUT_INVALID"
    )


def test_private_model_writer_closes_one_authenticated_tree(tmp_path: Path):
    source_identity = _identity("input", {"space": True})
    source_order = _identity("scworkbench.cell_order.v1", ["c2", "c1"])
    dimension_order = _identity("scworkbench.dimension_order.v1", ["D2", "D1"])
    write_grant = {
        "schema": "scworkbench.operation_pack.contained_model_write_grant.v1",
        "output": "model",
        "engine": "synthetic",
        "model_type": "synthetic_embedding",
        "primary_path": "model.bin",
        "relative_root": "outputs/model/model_tree",
        "source_space": {
            "input_name": "space",
            "input_descriptor_identity": source_identity,
            "content_identity": _identity("space", {}),
            "cell_order_identity": source_order,
            "dimensions": [2, 3],
            "selected_dimension_count": 2,
            "selected_dimension_order_identity": dimension_order,
        },
        "paired_space_output": {
            "name": "embedding",
            "allocated_id": "embedding",
            "expected_dimensions": [2, 2],
            "expected_cell_order_identity": source_order,
        },
        "limits": {"maximum_file_count": 4, "maximum_total_bytes": 1024},
    }
    write_grant["write_grant_identity"] = _identity(
        write_grant["schema"], write_grant, "grant excluding write_grant_identity"
    )
    output_descriptor = {
        "writer": "write_model_ref",
        "contained_model_write_grant": write_grant,
        "grant_identity": _identity("output", {}),
    }
    request = {
        "outputs": [{"name": "model", "allocated_id": "model", "descriptor": output_descriptor}]
    }
    writers = V4Writers(
        request,
        tmp_path,
        ["c2", "c1"],
        {
            "space": {
                "kind": "space",
                "space_id": "source-space",
                "dimensions": [2, 3],
                "cell_id": ["c2", "c1"],
            }
        },
    )
    tree = writers.open_model_tree("model")
    tree.write_bytes("model.bin", b"model")
    tree.write_bytes("weights/state.bin", b"weights")
    result = tree.finish(
        {"kind": "model_ref", "in_space_id": "source-space"},
        {"presentation": "synthetic"},
    )
    assert result["allocated_id"] == "model"
    write = writers.writes["model"]
    assert write.value["model_linkage"]["selected_dimension_order_identity"] == dimension_order
    assert [entry["path"] for entry in write.value["contained_model_tree_manifest"]["entries"]] == [
        "model.bin",
        "weights/state.bin",
    ]
    assert len(write.paths) == 3


def test_space_2d_candidate_binds_exact_selected_order_membership_and_values():
    cells = ["c2", "c1"]
    grant = {
        "schema": "scworkbench.operation_pack.space_2d_publication_grant.v1",
        "selected_cells": {
            "count": 2,
            "order_identity": _identity("scworkbench.cell_order.v1", cells),
            "membership_identity": _identity("scworkbench.cell_membership.v1", sorted(cells)),
        },
    }
    request = {
        "outputs": [
            {
                "name": "embedding",
                "descriptor": {"publication_grant": grant},
            }
        ]
    }
    write = type(
        "Write",
        (),
        {
            "value": {
                "kind": "space_2d",
                "dimensions": [2, 2],
                "cell_id": cells,
                "values": [[0.0, 1.0], [2.0, 3.0]],
            }
        },
    )()
    validate_space_2d_candidates(request, {"embedding": write})
    write.value["cell_id"] = list(reversed(cells))
    assert _error_code(validate_space_2d_candidates, request, {"embedding": write}) == (
        "PYBENCH_OUTPUT_INVALID"
    )


def test_contained_model_fit_executes_one_atomic_model_embedding_closure(active_v3, tmp_path: Path):
    request = _fit_request(active_v3, tmp_path)
    assert validate_request_v4(request, active_v3) == request
    receipt, _sidecar = execute_operation_pack_v4(request, active_v3, tmp_path)
    model = next(item for item in receipt["outputs"] if item["name"] == "model")
    tree = model["descriptor"]["contained_model_tree"]
    assert tree["file_count"] == 2
    assert tree["primary_path"] == "model.bin"
    assert tree["status"] == "validated_not_published"
    publication = receipt["publication_observations"][0]
    assert publication["mode"] == "add"
    assert [item["name"] for item in publication["closure_output_identities"]] == [
        "embedding",
        "model",
    ]


def test_contained_model_fit_fault_restores_the_source_only_stage(active_v3, tmp_path: Path):
    request = _fit_request(active_v3, tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def fail_after_handler(phase: str) -> None:
        if phase == "after_handler":
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        execute_operation_pack_v4(request, active_v3, tmp_path, fault_hook=fail_after_handler)
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "outputs").exists()


def test_contained_model_grant_identity_and_engine_mutations_fail_before_dispatch(
    installed_v3, tmp_path: Path
):
    request = _fit_request(installed_v3, tmp_path)
    changed = copy.deepcopy(request)
    model = next(item for item in changed["outputs"] if item["name"] == "model")
    grant = model["descriptor"]["contained_model_write_grant"]
    grant["engine"] = "substituted"
    grant.pop("write_grant_identity")
    grant["write_grant_identity"] = _identity(
        grant["schema"], grant, "grant excluding write_grant_identity"
    )
    _refresh_output(model)
    _refresh_request(changed)
    assert _error_code(validate_request_v4, changed, installed_v3) == (
        "PYBENCH_CAPABILITY_MISMATCH"
    )


def test_contained_model_read_executes_once_and_receipts_unchanged_payload(
    active_v3, tmp_path: Path
):
    request = _transform_request(active_v3, tmp_path)
    assert validate_request_v4(request, active_v3) == request
    receipt, _sidecar = execute_operation_pack_v4(request, active_v3, tmp_path)
    assert len(receipt["model_payload_reads"]) == 1
    observed = receipt["model_payload_reads"][0]
    assert observed["name"] == "model_payload"
    assert observed["use_count"] == 1
    assert observed["integrity_status"] == "unchanged"
    assert validate_receipt_v4(receipt, request, active_v3, tmp_path) == receipt


def test_contained_model_input_corruption_fails_before_output_and_is_retry_safe(
    active_v3, tmp_path: Path
):
    request = _transform_request(active_v3, tmp_path)
    source = tmp_path / "model_payload_reads" / "model_payload" / "tree" / "model.bin"
    source.write_bytes(b"corrupt")
    assert _error_code(execute_operation_pack_v4, request, active_v3, tmp_path) == (
        "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )
    assert not (tmp_path / "outputs").exists()


def test_contained_model_post_execution_mutation_invalidates_receipt(active_v3, tmp_path: Path):
    request = _transform_request(active_v3, tmp_path)
    receipt, _sidecar = execute_operation_pack_v4(request, active_v3, tmp_path)
    source = tmp_path / "model_payload_reads" / "model_payload" / "tree" / "model.bin"
    source.write_bytes(b"post-receipt mutation")
    assert _error_code(validate_receipt_v4, receipt, request, active_v3, tmp_path) == (
        "PYBENCH_INPUT_IDENTITY_MISMATCH"
    )
