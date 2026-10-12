from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.graph import GraphPrimitive, graph_identities
from pybench.graph_broker import GraphBrokerClient
from pybench.graph_parts import GraphPartsWriter
from pybench.graph_transport import validate_graph_write_grant, validate_graph_write_observation
from pybench.operation_packs import OperationPackError, protocol_asset
from pybench.protocol_v5 import audit_graph_stage
from test_graph_small_budget import BROKER_TOKEN, _serve_parts

SAMPLES = Path(__file__).parent / "fixtures" / "graph-small-500"
SAMPLE_HASHES = {
    "bounded_small_case.json": "60741a7915721fb6c8872ad58a656dce3fb1c1de42bad52ab756cacd4ea703d2",
    "R_small_authenticated_expected.json": "06b349cb30745d3b689bb8c8247a6398a3fd744769ea03e07309df61e146d8fb",
    "R_small_request.json": "eeeaa6382efef8aed677b4b54b53b7474f36074af250e409db5369704127f8c0",
    "R_small_receipt.json": "bfb0145132be66b7c34d6e1955e0b4c8b76da279f065ff1ed2f2bf209f331792",
    "R_small_copy_request.json": "904e2ad1e5355a8de0b4c82236144a05c1a73f4cf1a12d127e9fa3dcf56959a9",
    "R_small_copy_receipt.json": "217ce5112b70c6691bd6f726ab9acff97bedbe5f0d91f24494128b8c558cf49e",
}


def _read(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def _rows(content: dict, bindings: list[dict]):
    yield "header", {"schema": content["schema"], "direction_mode": content["direction_mode"]}
    for component in ("nodes", "edges", "incidences"):
        for row in content[component]:
            yield component, row
    for scope in ("graph", "node", "edge", "incidence"):
        for ordinal, field in enumerate(content["attributes"][scope]):
            yield (
                "attribute_fields",
                {
                    "scope": scope,
                    "field_ordinal": str(ordinal),
                    "name": field["name"],
                    "type": field["type"],
                    "nullable": field["nullable"],
                },
            )
    for scope in ("graph", "node", "edge", "incidence"):
        for ordinal, field in enumerate(content["attributes"][scope]):
            for entity, value in enumerate(field["values"]):
                yield (
                    "attribute_values",
                    {
                        "scope": scope,
                        "field_ordinal": str(ordinal),
                        "entity_ordinal": str(entity),
                        "value": value,
                    },
                )
    for ordinal, binding in enumerate(bindings):
        yield (
            "bindings",
            {
                "binding_ordinal": str(ordinal),
                **{key: value for key, value in binding.items() if key != "mapping"},
            },
        )
    for binding in bindings:
        for ordinal, pair in enumerate(binding["mapping"]):
            yield (
                "binding_mappings",
                {
                    "binding_id": binding["binding_id"],
                    "mapping_ordinal": str(ordinal),
                    **pair,
                },
            )


def _stream(writer: GraphPartsWriter, content: dict, bindings: list[dict], rows_per_part: int):
    component = None
    batch = []
    for next_component, row in _rows(content, bindings):
        if batch and (next_component != component or len(batch) == rows_per_part):
            writer.write_part(component, batch)
            batch = []
        component = next_component
        batch.append(row)
    if batch:
        writer.write_part(component, batch)


def test_r_small_bound_graph_streaming_and_broker_parity(tmp_path):
    for name, sha256 in SAMPLE_HASHES.items():
        assert hashlib.sha256((SAMPLES / name).read_bytes()).hexdigest() == sha256
    request = _read("R_small_request.json")
    receipt = _read("R_small_receipt.json")
    copy_request = _read("R_small_copy_request.json")
    copy_receipt = _read("R_small_copy_receipt.json")
    expected = _read("R_small_authenticated_expected.json")
    for value, schema in (
        (request, "operation-pack-transition-request-v4.schema.json"),
        (receipt, "operation-pack-transition-receipt-v4.schema.json"),
        (copy_request, "operation-pack-transition-request-v4.schema.json"),
        (copy_receipt, "operation-pack-transition-receipt-v4.schema.json"),
    ):
        Draft202012Validator(json.loads(protocol_asset(schema).read_text())).validate(value)
    assert expected["fixture_sha256"] == SAMPLE_HASHES["bounded_small_case.json"]
    grant = request["graph_write_grants"][0]
    inputs = {item["name"]: item["descriptor"]["kind"] for item in request["inputs"]}
    outputs = [item["name"] for item in request["outputs"]]
    transition = request["transition"]["transition_identity"]
    validate_graph_write_grant(
        grant, transition_identity=transition, input_kinds=inputs, output_names=outputs
    )
    r_observation = receipt["graph_write_observations"][0]
    validate_graph_write_observation(r_observation, grant)
    identities = graph_identities(expected["content"], expected["bindings"])
    assert [node["node_id"] for node in expected["content"]["nodes"][:3]] == ["n2", "n10", "n1"]
    assert [
        (binding["binding_id"], len(binding["mapping"])) for binding in expected["bindings"]
    ] == [
        ("cells", 32),
        ("features", 2),
        ("empty_features", 0),
    ]
    assert expected["bindings"][1]["mapping"] == [
        {"node_id": "n2", "axis_id": "f2"},
        {"node_id": "n10", "axis_id": "f1"},
    ]
    assert identities.content == expected["identity"]["content_identity"]
    assert identities.bindings == expected["identity"]["binding_identity"]
    assert identities.descriptor == expected["identity"]["descriptor_identity"]
    assert (
        identities.content["sha256"]
        == "d98d32aaceec530597ec03aabb2eeb3aa6697639852cfba754c9165f95735e24"
    )
    assert (
        identities.bindings["sha256"]
        == "c84975af63c2cef24cba0ba84a8a2caec8af973c94eee4fb774d2649bee63148"
    )
    assert (
        identities.descriptor["sha256"]
        == "f3acdb903c88692d72486dfd9ff244501ad948b8e5c7e8bd276bcb61cd38698f"
    )
    for field, claim in (
        ("content_identity", identities.content),
        ("binding_identity", identities.bindings),
        ("descriptor_identity", identities.descriptor),
    ):
        assert r_observation[field] == claim
    with pytest.raises(OperationPackError, match="collection exceeds"):
        GraphPrimitive(expected["content"], expected["bindings"], max_collect_bytes=8192)

    writer = GraphPartsWriter(
        grant,
        tmp_path,
        transition_identity=transition,
        input_kinds=inputs,
        output_names=outputs,
    )
    assert writer._db.execute("PRAGMA cache_size").fetchone()[0] == -256
    _stream(writer, expected["content"], expected["bindings"], rows_per_part=2)
    assert writer._scratch_path.exists()
    observation = writer.finish()
    for field, claim in (
        ("content_identity", identities.content),
        ("binding_identity", identities.bindings),
        ("descriptor_identity", identities.descriptor),
    ):
        assert observation[field] == claim
    assert observation["component_counts"] == r_observation["component_counts"]
    assert observation["component_observations"] == r_observation["component_observations"]
    token = tmp_path / "outputs" / "result" / "graph.parts.ndjson"
    assert token.stat().st_size > 8192
    parts = [json.loads(line) for line in token.read_text(encoding="utf-8").splitlines()]
    assert [
        row["node_id"] for part in parts if part["component"] == "nodes" for row in part["rows"]
    ] == [node["node_id"] for node in expected["content"]["nodes"]]
    assert all(int(part["row_count"]) <= 2 for part in parts)
    assert (
        audit_graph_stage(
            grant,
            tmp_path,
            observation,
            transition_identity=transition,
            input_kinds=inputs,
            output_names=outputs,
        )
        == observation
    )

    read_grant = copy_request["graph_read_grants"][0]
    r_usage = copy_receipt["graph_read_usage"][0]
    assert read_grant["content_identity"] == identities.content
    assert read_grant["binding_identity"] == identities.bindings
    assert read_grant["descriptor_identity"] == identities.descriptor
    assert read_grant["selected_binding_ids"] == ["cells", "features", "empty_features"]
    (tmp_path / "broker").mkdir()
    errors: list[BaseException] = []
    server = threading.Thread(target=_serve_parts, args=(tmp_path, parts, errors))
    server.start()
    try:
        client = GraphBrokerClient(
            read_grant,
            tmp_path,
            transition_identity=copy_request["transition"]["transition_identity"],
            authorization_token=BROKER_TOKEN,
            timeout=10,
        )
        assert list(client) == parts
        assert client.usage() == r_usage
    finally:
        server.join(timeout=10)
    assert not server.is_alive()
    assert not errors
    assert list((tmp_path / "broker").iterdir()) == []
