from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.operation_packs_v3 import activate_operation_pack_v3
from pybench.phase4d_contracts import inventory_model_tree, tree_payload_identity
from pybench.phase5_contracts import (
    MODEL_SOURCE_LINKAGE_SCHEMA,
    SELF_LINKED_GRANT_SCHEMA,
    SIBLING_GRANT_SCHEMA,
    model_sibling_closure_value,
    self_linked_declaration_identity,
)
from pybench.protocol_v4 import execute_operation_pack_v4, validate_receipt_v4, validate_request_v4
from test_operation_pack_v3_successor import _request_v4
from test_operation_pack_v3_successor import installed_v3 as _installed_v3


@pytest.fixture(scope="module")
def installed_v3():
    yield from _installed_v3.__wrapped__()


@pytest.fixture(scope="module")
def active_v3(installed_v3):
    activate_operation_pack_v3(installed_v3)
    return installed_v3


def _plain(value):
    return {key: value[key] for key in ("scheme", "sha256")}


def _refresh_output(output: dict) -> None:
    descriptor = output["descriptor"]
    descriptor.pop("grant_identity", None)
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v3",
        descriptor,
        "descriptor excluding grant_identity",
    )


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


def _space() -> dict:
    return {
        "kind": "space",
        "space_id": "fit-space",
        "dimensions": [3, 3],
        "cell_id": ["c2", "c1", "c3"],
        "dimension_id": ["D1", "D2", "D3"],
        "values": [[2.0, 1.0, 0.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
        "storage_profile": "space_dense_v1",
    }


def _linkage(source_descriptor: dict, *, engine="synthetic", model_type="synthetic_trajectory"):
    value = {
        "engine": engine,
        "model_type": model_type,
        "source_space_id": "fit-space",
        "source_space_content_identity": source_descriptor["content_identity"],
        "fitted_cell_order_identity": source_descriptor["cell_order_identity"],
        "selected_dimension_count": 2,
        "selected_dimension_order_identity": _identity(
            "scworkbench.dimension_order.v1", ["D2", "D1"]
        ),
        "source_snapshot_id": source_descriptor["content_identity"]["sha256"],
    }
    value["linkage_identity"] = _identity(
        MODEL_SOURCE_LINKAGE_SCHEMA,
        value,
        "linkage excluding linkage_identity",
    )
    return value


def _self_linked_request(discovery, stage: Path, *, reader_calls: int = 1) -> dict:
    placeholder = {
        "kind": "model_ref",
        "dimensions": [1],
        "engine": "synthetic",
        "model_type": "synthetic_trajectory",
        "in_space_id": "fit-space",
        "payload_tree": {"model.bin": "placeholder"},
        "storage_profile": "model_ref_contained_tree_v1",
    }
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::self_linked_model_read_probe",
        {"model": placeholder},
        {"reader_calls": reader_calls},
    )
    root = stage / "model_payload_reads" / "fit_model" / "tree"
    root.mkdir(parents=True)
    (root / "model.bin").write_bytes(b"synthetic-sibling-model")
    tree, _paths = inventory_model_tree(
        root,
        engine="synthetic",
        model_type="synthetic_trajectory",
        primary_path="model.bin",
        maximum_file_count=4,
        maximum_total_bytes=1024,
    )
    descriptor = request["inputs"][0]["descriptor"]
    source_descriptor = {
        "content_identity": _identity("scworkbench.space.content.v1", {"fit": True}),
        "cell_order_identity": _identity("scworkbench.cell_order.v1", ["c2", "c1", "c3"]),
    }
    linkage = _linkage(source_descriptor)
    linkage_identity = _plain(linkage["linkage_identity"])
    contained_identity = _identity(
        "scworkbench.operation_pack.contained_payload.v2",
        _plain(tree["manifest_identity"]),
    )
    content_identity = _identity(
        "scworkbench.model_ref.content.v1",
        {
            "engine": "synthetic",
            "model_type": "synthetic_trajectory",
            "linkage_identity": linkage["linkage_identity"],
            "tree_manifest_identity": _plain(tree["manifest_identity"]),
        },
    )
    descriptor.update(
        content_identity=content_identity,
        model_linkage_identity=linkage_identity,
        contained_payload_identity=contained_identity,
        payload_manifest_identity=tree_payload_identity(tree),
    )
    descriptor.pop("descriptor_identity")
    descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.input_descriptor.v2",
        descriptor,
        "descriptor excluding descriptor_identity",
    )
    declaration = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == "scwbpyv3reference::self_linked_model_read_probe"
    )["self_linked_contained_model_reads"][0]
    grant = {
        "schema": SELF_LINKED_GRANT_SCHEMA,
        "name": "fit_model",
        "input_name": "model",
        "reader": "read_self_linked_contained_model_payload",
        "declaration_identity": self_linked_declaration_identity(declaration),
        "selected_model": {"engine": "synthetic", "model_type": "synthetic_trajectory"},
        "input_descriptor_identity": _plain(descriptor["descriptor_identity"]),
        "model_content_identity": content_identity,
        "model_source_linkage": linkage,
        "model_source_linkage_identity": linkage_identity,
        "contained_payload_identity": contained_identity,
        "payload_manifest_identity": tree_payload_identity(tree),
        "tree_manifest": tree,
        "limits": {"maximum_file_count": 4, "maximum_total_bytes": 1024},
        "relative_root": "model_payload_reads/fit_model/tree",
        "access": "verified_private_copy_read_only_v1",
        "call_discipline": "exactly_once",
    }
    grant["grant_identity"] = _identity(
        SELF_LINKED_GRANT_SCHEMA, grant, "grant excluding grant_identity"
    )
    request["self_linked_model_payload_grants"] = [grant]
    capability = request["execution_capability"]
    capability["reader_grants"] = []
    capability["reader_grants"].append(
        {
            "self_linked_model_payload_name": "fit_model",
            "reader": grant["reader"],
            "grant_identity": grant["grant_identity"],
        }
    )
    capability["self_linked_model_payload_grant_identities"] = [_plain(grant["grant_identity"])]
    _refresh_request(request)
    return request


def _v3_self_linked_request(discovery, stage: Path) -> dict:
    request = _self_linked_request(discovery, stage)
    corpus = json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())
    linkage = copy.deepcopy(
        next(
            case["value"]
            for case in corpus["cases"]
            if case["name"] == "dfa_model_source_linkage_v3"
        )
    )
    linkage["engine"] = "synthetic"
    linkage["model_type"] = "synthetic_trajectory"
    linkage["source_space_id"] = "fit-space"
    linkage.pop("linkage_identity")
    linkage["linkage_identity"] = _identity(
        linkage["schema"], linkage, "linkage excluding linkage_identity"
    )
    grant = request["self_linked_model_payload_grants"][0]
    grant["model_source_linkage"] = linkage
    grant["model_source_linkage_identity"] = _plain(linkage["linkage_identity"])
    descriptor = request["inputs"][0]["descriptor"]
    content_identity = _identity(
        "scworkbench.model_ref.content.v1",
        {
            "engine": linkage["engine"],
            "model_type": linkage["model_type"],
            "linkage_identity": linkage["linkage_identity"],
            "tree_manifest_identity": _plain(grant["tree_manifest"]["manifest_identity"]),
        },
    )
    descriptor["model_linkage_identity"] = grant["model_source_linkage_identity"]
    descriptor["content_identity"] = content_identity
    descriptor.pop("descriptor_identity")
    descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.input_descriptor.v2",
        descriptor,
        "descriptor excluding descriptor_identity",
    )
    grant["input_descriptor_identity"] = _plain(descriptor["descriptor_identity"])
    grant["model_content_identity"] = content_identity
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        SELF_LINKED_GRANT_SCHEMA, grant, "grant excluding grant_identity"
    )
    capability = request["execution_capability"]
    capability["reader_grants"][0]["grant_identity"] = grant["grant_identity"]
    capability["self_linked_model_payload_grant_identities"] = [_plain(grant["grant_identity"])]
    _refresh_request(request)
    return request


def _sibling_request(discovery, stage: Path, *, omit_terminal: str = "none") -> dict:
    dims = [2, 1]
    request = _request_v4(
        discovery,
        stage,
        "scwbpyv3reference::sibling_model_fit_probe",
        {"fit_space": _space()},
        {"dims": dims, "omit_terminal": omit_terminal},
    )
    source_descriptor = request["inputs"][0]["descriptor"]
    source_binding = {
        "input_name": "fit_space",
        "input_descriptor_identity": _plain(source_descriptor["descriptor_identity"]),
        "content_identity": source_descriptor["content_identity"],
        "cell_order_identity": source_descriptor["cell_order_identity"],
        "dimensions": source_descriptor["dimensions"],
        "selected_dimension_count": 2,
        "selected_dimension_order_identity": _identity(
            "scworkbench.dimension_order.v1", ["D2", "D1"]
        ),
    }
    linkage = _linkage(source_descriptor)
    outputs = {item["name"]: item for item in request["outputs"]}
    for name in ("pseudotime", "entropy"):
        outputs[name]["descriptor"]["expected_dimensions"] = [3]
        _refresh_output(outputs[name])
    base_model_identity = outputs["model"]["descriptor"]["grant_identity"]
    siblings = []
    for name in ("pseudotime", "entropy", "summary"):
        output = outputs[name]
        siblings.append(
            {
                "name": name,
                "allocated_id": output["allocated_id"],
                "kind": output["descriptor"]["kind"],
                "grant_identity": _plain(output["descriptor"]["grant_identity"]),
                "replay_policy": "exact_canonical_value",
                "expected_cell_order_identity_if_cell_indexed": (
                    source_descriptor["cell_order_identity"] if name != "summary" else None
                ),
            }
        )
    closure = model_sibling_closure_value(
        "model",
        base_model_identity,
        linkage["linkage_identity"],
        siblings,
        "fit",
    )
    model_descriptor = outputs["model"]["descriptor"]
    model_descriptor["expected_model_linkage"] = {
        "mode": "input",
        "reference_name": "fit_space",
        "reference_identity": source_binding["input_descriptor_identity"],
    }
    # The base output identity is formed after the durable linkage declaration.
    _refresh_output(outputs["model"])
    base_model_identity = outputs["model"]["descriptor"]["grant_identity"]
    closure = model_sibling_closure_value(
        "model",
        base_model_identity,
        linkage["linkage_identity"],
        siblings,
        "fit",
    )
    grant = {
        "schema": SIBLING_GRANT_SCHEMA,
        "output": "model",
        "engine": "synthetic",
        "model_type": "synthetic_trajectory",
        "primary_path": "model.bin",
        "relative_root": "outputs/model/model_tree",
        "source_space": source_binding,
        "model_source_linkage": linkage,
        "required_sibling_terminals": siblings,
        "model_sibling_closure_identity": _plain(closure["closure_identity"]),
        "limits": {"maximum_file_count": 4, "maximum_total_bytes": 1024},
    }
    grant["write_grant_identity"] = _identity(
        SIBLING_GRANT_SCHEMA, grant, "grant excluding write_grant_identity"
    )
    model_descriptor["contained_model_sibling_write_grant"] = grant
    _refresh_output(outputs["model"])
    _refresh_request(request)
    return request


def _error_code(function, *args):
    with pytest.raises(OperationPackError) as caught:
        function(*args)
    return caught.value.code


def test_phase5_shared_identity_cases_match_python_canonicalization():
    corpus = json.loads(protocol_asset("fixtures/operation-pack-v3/identities.json").read_text())
    phase5_names = {
        "model_source_linkage_v2",
        "self_linked_model_read_declaration",
        "self_linked_model_read_grant",
        "self_linked_model_read_observation",
        "model_sibling_closure",
        "contained_model_sibling_write_grant",
        "contained_model_sibling_tree_observation",
        "retained_artifact_reference",
        "retained_artifact_collection_item",
        "retained_artifact_collection",
        "retained_artifact_collection_grant",
        "retained_artifact_collection_result",
        "named_cell_axis_source",
        "named_cell_axis_selection",
        "named_cell_axis",
        "named_cell_axis_collection",
        "named_cell_axis_collection_grant",
        "optional_display_space",
        "optional_display_axis_group_grant",
    }
    cases = {case["name"]: case for case in corpus["cases"] if case["name"] in phase5_names}
    assert set(cases) == phase5_names
    for case in cases.values():
        expected = case["identity"]
        assert (
            _identity(
                expected["scheme"],
                case["preimage"],
                expected.get("scope"),
            )
            == expected
        )


def test_self_linked_model_executes_and_records_complete_observation(active_v3, tmp_path):
    request = _self_linked_request(active_v3, tmp_path)
    validate_request_v4(request, active_v3)
    receipt, _sidecar = execute_operation_pack_v4(request, active_v3, tmp_path)
    assert receipt["self_linked_model_payload_reads"][0]["use_count"] == 1
    assert receipt["self_linked_model_payload_reads"][0]["integrity_status"] == "unchanged"
    assert validate_receipt_v4(receipt, request, active_v3, tmp_path) == receipt


def test_v3_g1_model_read_is_schema_valid_but_unsupported_before_stage_mutation(
    installed_v3, tmp_path
):
    request = _v3_self_linked_request(installed_v3, tmp_path)
    schema = json.loads(
        protocol_asset("operation-pack-transition-request-v3.schema.json").read_text()
    )
    assert not list(Draft202012Validator(schema).iter_errors(request))
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    for call in (
        lambda: validate_request_v4(request, installed_v3),
        lambda: execute_operation_pack_v4(request, installed_v3, tmp_path),
    ):
        with pytest.raises(OperationPackError) as caught:
            call()
        assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
        assert caught.value.details["phase"] == "staging"
        assert "derived fitted-axis model-read authority" in str(caught.value)
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize("reader_calls", [0, 2])
def test_self_linked_model_reader_call_discipline_is_exactly_once(
    active_v3, tmp_path, reader_calls
):
    request = _self_linked_request(active_v3, tmp_path, reader_calls=reader_calls)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert _error_code(execute_operation_pack_v4, request, active_v3, tmp_path) in {
        "PYBENCH_CAPABILITY_MISMATCH",
        "PYBENCH_RECEIPT_MISMATCH",
    }
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "mutation",
    [
        "engine",
        "model_type",
        "linkage",
        "source_content",
        "cell_order",
        "declaration",
        "tree_manifest",
        "payload_manifest",
        "limits",
        "relative_root",
    ],
)
def test_self_linked_model_adversarial_grants_fail_before_dispatch(
    installed_v3, tmp_path, mutation
):
    request = _self_linked_request(installed_v3, tmp_path)
    grant = request["self_linked_model_payload_grants"][0]
    if mutation in {"engine", "model_type"}:
        grant["selected_model"][mutation] = "substituted"
    elif mutation == "linkage":
        grant["model_source_linkage_identity"]["sha256"] = "0" * 64
    elif mutation == "source_content":
        grant["model_source_linkage"]["source_space_content_identity"]["sha256"] = "0" * 64
    elif mutation == "cell_order":
        grant["model_source_linkage"]["fitted_cell_order_identity"]["sha256"] = "0" * 64
    elif mutation == "declaration":
        grant["declaration_identity"]["sha256"] = "0" * 64
    elif mutation == "tree_manifest":
        grant["tree_manifest"]["entries"] = []
    elif mutation == "payload_manifest":
        grant["payload_manifest_identity"]["sha256"] = "0" * 64
    elif mutation == "limits":
        grant["limits"]["maximum_file_count"] = 3
    else:
        grant["relative_root"] = "model_payload_reads/other/tree"
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        SELF_LINKED_GRANT_SCHEMA, grant, "grant excluding grant_identity"
    )
    request["execution_capability"]["reader_grants"][0]["grant_identity"] = grant["grant_identity"]
    request["execution_capability"]["self_linked_model_payload_grant_identities"] = [
        _plain(grant["grant_identity"])
    ]
    _refresh_request(request)
    assert _error_code(validate_request_v4, request, installed_v3) in {
        "PYBENCH_CAPABILITY_MISMATCH",
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
    }


def test_sibling_model_executes_one_complete_closure(active_v3, tmp_path):
    request = _sibling_request(active_v3, tmp_path)
    validate_request_v4(request, active_v3)
    receipt, _sidecar = execute_operation_pack_v4(request, active_v3, tmp_path)
    model = next(item for item in receipt["outputs"] if item["name"] == "model")
    descriptor = model["descriptor"]
    observation = descriptor["contained_model_sibling_tree"]
    grant = request["outputs"][0]["descriptor"]["contained_model_sibling_write_grant"]
    assert observation["loadability_status"] == "contained_tree_authenticated"
    assert len(observation["ordered_sibling_observed_descriptor_identities"]) == 3
    assert observation["write_grant_identity"] == _plain(grant["write_grant_identity"])
    assert observation["tree_manifest_identity"] == _plain(
        observation["tree_manifest"]["manifest_identity"]
    )
    assert descriptor["model_linkage_identity"] == _plain(
        grant["model_source_linkage"]["linkage_identity"]
    )
    assert descriptor["content_identity"] == _identity(
        "scworkbench.model_ref.content.v1",
        {
            "engine": grant["engine"],
            "model_type": grant["model_type"],
            "linkage_identity": descriptor["model_linkage_identity"],
            "tree_manifest_identity": observation["tree_manifest_identity"],
        },
    )
    assert validate_receipt_v4(receipt, request, active_v3, tmp_path) == receipt

    forged = copy.deepcopy(receipt)
    forged_model = next(item for item in forged["outputs"] if item["name"] == "model")
    forged_tree = forged_model["descriptor"]["contained_model_sibling_tree"]
    forged_tree["ordered_sibling_observed_descriptor_identities"].reverse()
    forged_tree.pop("observation_identity")
    forged_tree["observation_identity"] = _identity(
        forged_tree["schema"],
        forged_tree,
        "contained_model_sibling_tree_observation excluding observation_identity",
    )
    forged_model["descriptor"].pop("observed_descriptor_identity")
    forged_model["descriptor"]["observed_descriptor_identity"] = _identity(
        "scworkbench.operation_pack.observed_output.v3",
        forged_model["descriptor"],
        "descriptor excluding observed_descriptor_identity",
    )
    forged.pop("receipt_identity")
    forged["receipt_identity"] = _identity(
        "scworkbench.operation_pack.transition_receipt.v3",
        forged,
        "receipt excluding receipt_identity",
    )
    assert _error_code(validate_receipt_v4, forged, request, active_v3) == (
        "PYBENCH_RECEIPT_MISMATCH"
    )


@pytest.mark.parametrize("missing", ["pseudotime", "entropy", "summary"])
def test_sibling_model_missing_terminal_rolls_back_complete_closure(active_v3, tmp_path, missing):
    request = _sibling_request(active_v3, tmp_path, omit_terminal=missing)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert _error_code(execute_operation_pack_v4, request, active_v3, tmp_path) in {
        "PYBENCH_OUTPUT_INVALID",
        "PYBENCH_RECEIPT_MISMATCH",
    }
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "engine",
        "model_type",
        "source_content",
        "cell_order",
        "dimension_order",
        "missing_sibling",
        "extra_sibling",
        "sibling_kind",
        "sibling_grant",
        "sibling_axis",
        "closure",
    ],
)
def test_sibling_model_adversarial_grants_fail_before_dispatch(installed_v3, tmp_path, mutation):
    request = _sibling_request(installed_v3, tmp_path)
    model = request["outputs"][0]
    grant = model["descriptor"]["contained_model_sibling_write_grant"]
    if mutation in {"engine", "model_type"}:
        grant[mutation] = "substituted"
    elif mutation == "source_content":
        grant["source_space"]["content_identity"]["sha256"] = "0" * 64
    elif mutation == "cell_order":
        grant["model_source_linkage"]["fitted_cell_order_identity"]["sha256"] = "0" * 64
    elif mutation == "dimension_order":
        grant["source_space"]["selected_dimension_order_identity"]["sha256"] = "0" * 64
    elif mutation == "missing_sibling":
        grant["required_sibling_terminals"].pop()
    elif mutation == "extra_sibling":
        grant["required_sibling_terminals"].append(
            copy.deepcopy(grant["required_sibling_terminals"][0])
        )
        grant["required_sibling_terminals"][-1]["name"] = "extra"
    elif mutation == "sibling_kind":
        grant["required_sibling_terminals"][0]["kind"] = "obs_table"
    elif mutation == "sibling_grant":
        grant["required_sibling_terminals"][0]["grant_identity"]["sha256"] = "0" * 64
    elif mutation == "sibling_axis":
        grant["required_sibling_terminals"][0]["expected_cell_order_identity_if_cell_indexed"][
            "sha256"
        ] = "0" * 64
    else:
        grant["model_sibling_closure_identity"]["sha256"] = "0" * 64
    grant.pop("write_grant_identity")
    grant["write_grant_identity"] = _identity(
        SIBLING_GRANT_SCHEMA, grant, "grant excluding write_grant_identity"
    )
    _refresh_output(model)
    _refresh_request(request)
    assert _error_code(validate_request_v4, request, installed_v3) in {
        "PYBENCH_CAPABILITY_MISMATCH",
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
    }


def test_model_tree_tampering_rolls_back_all_sibling_outputs(active_v3, tmp_path):
    request = _sibling_request(active_v3, tmp_path)

    def tamper(phase):
        if phase == "after_handler":
            (tmp_path / "outputs" / "model" / "model_tree" / "model.bin").write_bytes(b"tampered")

    assert (
        _error_code(
            lambda: execute_operation_pack_v4(request, active_v3, tmp_path, fault_hook=tamper)
        )
        == "PYBENCH_OUT_OF_SCOPE_MUTATION"
    )
    assert not (tmp_path / "outputs").exists()
