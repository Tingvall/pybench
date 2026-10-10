from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pybench.graph import GraphPrimitive
from pybench.graph_igraph import from_igraph, to_igraph
from pybench.operation_packs import OperationPackError, canonical_json

igraph = pytest.importorskip("igraph")
FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "protocol/fixtures/graph-v1/content-identities.json"
    ).read_text()
)


def _content(name: str) -> dict:
    return copy.deepcopy(next(row["content"] for row in FIXTURES["positive"] if row["id"] == name))


def _ordinary() -> dict:
    content = _content("directed_self_loop_mixed_mode")
    content["attributes"]["graph"] = [
        {"name": "title", "type": "utf8", "nullable": False, "values": ["loop"]}
    ]
    content["attributes"]["node"] = [
        {"name": "enabled", "type": "bool", "nullable": True, "values": [None]},
        {
            "name": "score",
            "type": "float64_le_hex",
            "nullable": False,
            "values": ["0000000000000080"],
        },
        {
            "name": "large",
            "type": "int64_decimal_string",
            "nullable": False,
            "values": ["9223372036854775807"],
        },
    ]
    content["attributes"]["incidence"] = [
        {"name": "port", "type": "utf8", "nullable": False, "values": ["a", "b"]}
    ]
    return content


@pytest.mark.parametrize("name", ["empty_undirected", "directed_self_loop_mixed_mode"])
def test_igraph_round_trip_frozen_values(name):
    value = GraphPrimitive(_content(name))
    native = to_igraph(value)
    restored = from_igraph(native)
    assert restored.content == value.content
    assert restored.identities == value.identities


def test_igraph_round_trip_typed_values_and_signed_zero():
    value = GraphPrimitive(_ordinary())
    native = to_igraph(value)
    assert native.vs["large"] == ["9223372036854775807"]
    assert native.vs["score"] == [-0.0]
    assert from_igraph(native).identities == value.identities
    native.vs["score"] = [0.0]
    with pytest.raises(OperationPackError, match="attribute values differ"):
        from_igraph(native)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g.vs.__setitem__(".scwb_node_id", ["changed"]),
        lambda g: g.es.__setitem__(".scwb_edge_id", ["changed"]),
        lambda g: g.vs.__setitem__("extra", [1]),
        lambda g: g.__setitem__(".scwb_graph_carrier_v1", "{}"),
    ],
)
def test_igraph_rejects_stale_or_extra_native_state(mutation):
    native = to_igraph(GraphPrimitive(_ordinary()))
    mutation(native)
    with pytest.raises(OperationPackError):
        from_igraph(native)


def test_igraph_rejects_hyper_and_materialization_budget():
    value = GraphPrimitive(_content("directed_hyper_repeated_tail_typed_incidence"))
    with pytest.raises(OperationPackError, match="hyperedges"):
        to_igraph(value)
    with pytest.raises(OperationPackError, match="budget"):
        to_igraph(GraphPrimitive(_ordinary()), max_collect_bytes=1)


def test_igraph_budget_includes_complete_carrier_envelope():
    value = GraphPrimitive(_content("empty_undirected"))
    carrier = {
        "schema": "scworkbench.graph.igraph_carrier.v1",
        "content": value.content,
        "bindings": value.bindings,
        "descriptor_identity": value.identities.descriptor,
    }
    exact = len(canonical_json(carrier).encode("utf-8"))
    with pytest.raises(OperationPackError, match="budget"):
        to_igraph(value, max_collect_bytes=exact - 1)
    assert (
        from_igraph(to_igraph(value, max_collect_bytes=exact), max_collect_bytes=exact).identities
        == value.identities
    )


def test_igraph_rejects_topology_edit():
    content = _ordinary()
    content["nodes"].append({"node_id": "isolate"})
    for field in content["attributes"]["node"]:
        field["values"].append(None if field["nullable"] else field["values"][0])
    native = to_igraph(GraphPrimitive(content))
    native.delete_edges([0])
    with pytest.raises(OperationPackError, match="count differs"):
        from_igraph(native)
