from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.graph import GraphPrimitive, graph_binding_from_source, graph_source_axis_projection
from pybench.graph_transport import (
    validate_graph_write_grant,
    validate_graph_write_observation,
)
from pybench.operation_packs import OperationPackError, protocol_asset
from pybench.operation_packs_v2 import _identity
from pybench.protocol_v5 import V5Writers, audit_graph_stage

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = Path(__file__).parent / "fixtures" / "graph-native-matrix-330"
SHARED_ASSETS = {
    "operation-pack-v4.schema.json": "0c38f16eb811dd40111c0b82ebcb519d9e8bd06bccaaf13f1aa366a94cc6b13f",
    "operation-pack-transition-request-v4.schema.json": "e95e0dd7e86456edccc3b4097a9d10a9210d3371c64e3ce2e14159053d14bb1c",
    "operation-pack-transition-receipt-v4.schema.json": "9eb38dc446aa0791d16868660c10901ad5db933ed73ba6cdf03abfd34173aa2e",
    "graph-v1.schema.json": "760e031210948c1194183061472921520037f6dbb34bd1388233ce9476f0a007",
    "pybench-op-v5-python-runtime.schema.json": "d8a2c7cc15d276f3d72eeffbb153fa336f4fc6b8b6532907d58abf3920d642b1",
    "fixtures/graph-v1/binding-descriptor-identities.json": "75c182556ae1b099ca63ff5505692fbf11bc8602b40d4f0d73f896cc4aeddd2e",
    "fixtures/graph-v1/content-identities.json": "5a83d3a8b3abacf826843120861d759a3fa04202c2ff5dca998dc18ca665f9fd",
    "fixtures/graph-v1/negative-rehash-identities.csv": "4e51eb802a6b71ad36236aea2bbfea6164c36ed2da278668ec59b91563203bc6",
    "fixtures/graph-v1/scenario-roundtrip-vectors.json": "90d6cbc098acee167ede37bfc658e31b7b2cf9681bd840554fe6ed6b0762bb38",
}
SAMPLE_HASHES = {
    "direct_matrix_request.json": "180698fc88889b287c8fc7cd2c49f0e6b1bb1efb38747032e2981e52a86c0837",
    "direct_matrix_receipt.json": "b32ab4eccbfc15cc0e53deb6ae4dc62a842d519f1a96fe057cff28cd8e24e03c",
    "selected_graph_request.json": "511fddb529d982b4490833ce9c042bcdd087b53c82900ab5e085a50a215dbbb8",
    "selected_graph_receipt.json": "1a4ab848e5ef2cb57c09ddcb91ef5c0c6536ef3beec8571c52b59163b9463a70",
}


def _read(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def _assert_self_identity(value: dict, field: str) -> None:
    claimed = value[field]
    assert claimed == _identity(
        claimed["scheme"],
        {key: child for key, child in value.items() if key != field},
        claimed.get("scope"),
    )


def _rehash_grant(grant: dict) -> None:
    for axis in grant["authorized_source_axes"]:
        claim = axis["authority_identity"]
        axis["authority_identity"] = _identity(
            claim["scheme"],
            {key: value for key, value in axis.items() if key != "authority_identity"},
            claim["scope"],
        )
    claim = grant["grant_identity"]
    grant["grant_identity"] = _identity(
        claim["scheme"],
        {key: value for key, value in grant.items() if key != "grant_identity"},
        claim["scope"],
    )


def test_frozen_shared_graph_protocol_assets_match_forge_bytes():
    for name, expected in SHARED_ASSETS.items():
        assert hashlib.sha256((ROOT / "protocol" / name).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("case", ["direct_matrix", "selected_graph"])
def test_real_r_matrix_graph_request_receipt_parity(case):
    for suffix in ("request", "receipt"):
        name = f"{case}_{suffix}.json"
        assert hashlib.sha256((SAMPLES / name).read_bytes()).hexdigest() == SAMPLE_HASHES[name]
    request = _read(f"{case}_request.json")
    receipt = _read(f"{case}_receipt.json")
    for value, name in (
        (request, "operation-pack-transition-request-v4.schema.json"),
        (receipt, "operation-pack-transition-receipt-v4.schema.json"),
    ):
        Draft202012Validator(json.loads(protocol_asset(name).read_text())).validate(value)
    _assert_self_identity(request, "request_identity")
    _assert_self_identity(receipt, "receipt_identity")
    assert receipt["request_identity"] == request["request_identity"]
    assert receipt["status"] == "ok"

    grant = request["graph_write_grants"][0]
    _assert_self_identity(grant, "grant_identity")
    for axis in grant["authorized_source_axes"]:
        _assert_self_identity(axis, "authority_identity")
        assert axis["source_ref"]["kind"] == "matrix"
        assert axis["source_payload_closure_identity"]["scheme"] == (
            "scworkbench.content.matrix.fragment-tree.v2"
        )
    assert {axis["axis_kind"] for axis in grant["authorized_source_axes"]} == {
        "cell",
        "feature",
    }
    validate_graph_write_grant(
        grant,
        transition_identity=request["transition"]["transition_identity"],
        input_kinds={item["name"]: item["descriptor"]["kind"] for item in request["inputs"]},
        output_names=[item["name"] for item in request["outputs"]],
    )
    validate_graph_write_observation(receipt["graph_write_observations"][0], grant)
    if case == "direct_matrix":
        assert all(axis["source_binding_id"] is None for axis in grant["authorized_source_axes"])
        assert request["inputs"][0]["descriptor"]["dimensions"] == [2, 2]
        assert [axis["axis_count"] for axis in grant["authorized_source_axes"]] == ["3", "2"]
    else:
        selected = request["graph_read_grants"][0]["selected_binding_ids"]
        assert selected == [axis["source_binding_id"] for axis in grant["authorized_source_axes"]]


@pytest.mark.parametrize("case", ["direct_matrix", "selected_graph"])
def test_public_graph_writer_stages_binding_from_r_issued_matrix_grant(case, tmp_path):
    request = _read(f"{case}_request.json")
    grant = request["graph_write_grants"][0]
    source_axes = {
        axis["axis_kind"]: graph_source_axis_projection(axis)
        for axis in grant["authorized_source_axes"]
    }
    bindings = [
        graph_binding_from_source(
            source_axes["cell"],
            "cell_source",
            [{"node_id": "cell_node", "axis_id": "c2"}],
        ),
        graph_binding_from_source(
            source_axes["feature"],
            "feature_source",
            [{"node_id": "feature_node", "axis_id": "g2"}],
        ),
    ]
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [{"node_id": "cell_node"}, {"node_id": "feature_node"}],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    writer = V5Writers(request, tmp_path, [], {})
    observation = writer.write_graph("result", GraphPrimitive(content, bindings))
    assert observation["component_counts"]["bindings"] == "2"
    assert observation["component_counts"]["binding_mappings"] == "2"
    assert (
        audit_graph_stage(
            grant,
            tmp_path,
            observation,
            transition_identity=request["transition"]["transition_identity"],
            input_kinds={item["name"]: item["descriptor"]["kind"] for item in request["inputs"]},
            output_names=[item["name"] for item in request["outputs"]],
        )
        == observation
    )


@pytest.mark.parametrize("case", ["direct_matrix", "selected_graph"])
@pytest.mark.parametrize(
    "tamper", ["wrong_input", "wrong_closure", "wrong_transition", "duplicate"]
)
def test_self_rehashed_matrix_authority_cannot_escape_closed_grant(case, tamper):
    request = _read(f"{case}_request.json")
    grant = copy.deepcopy(request["graph_write_grants"][0])
    axis = grant["authorized_source_axes"][0]
    if tamper == "wrong_input":
        axis["input_name"] = "undeclared"
    elif tamper == "wrong_closure":
        axis["source_payload_closure_identity"]["scheme"] = (
            "scworkbench.artifact.ordered-object-payload.canonical-json.v1"
        )
    elif tamper == "wrong_transition":
        axis["transition_identity"]["sha256"] = "f" * 64
    else:
        grant["authorized_source_axes"].append(copy.deepcopy(axis))
    _rehash_grant(grant)
    with pytest.raises(OperationPackError):
        validate_graph_write_grant(
            grant,
            transition_identity=request["transition"]["transition_identity"],
            input_kinds={item["name"]: item["descriptor"]["kind"] for item in request["inputs"]},
            output_names=[item["name"] for item in request["outputs"]],
        )
