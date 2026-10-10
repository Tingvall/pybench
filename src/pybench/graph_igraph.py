"""Lossless, bounded igraph adapter for the admitted ordinary-graph subset."""

from __future__ import annotations

import json
import struct
from typing import Any

from .graph import GraphPrimitive, _fail, canonical_bytes
from .operation_packs import OperationPackError, canonical_json

_CARRIER = ".scwb_graph_carrier_v1"
_NODE_ID = ".scwb_node_id"
_EDGE_ID = ".scwb_edge_id"
_SCOPES = ("graph", "node", "edge", "incidence")


def _igraph() -> Any:
    try:
        import igraph  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OperationPackError(
            "PYBENCH_GRAPH_ADAPTER_UNAVAILABLE",
            "python-igraph is required for graph conversion",
            phase="graph_conversion",
        ) from exc
    return igraph


def _native(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "float64_le_hex":
        return struct.unpack("<d", bytes.fromhex(value))[0]
    return value


def _same(kind: str, expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual is None
    if kind == "float64_le_hex":
        return type(actual) is float and struct.pack("<d", actual).hex() == expected
    if kind == "bool":
        return type(actual) is bool and actual == expected
    return type(actual) is str and actual == expected


def _reserve(content: dict[str, Any]) -> None:
    for scope in _SCOPES:
        for field in content["attributes"][scope]:
            if field["name"].startswith(".scwb_"):
                _fail("reserved igraph attribute name")


def _admit_carrier(carrier: dict[str, Any], limit: int) -> None:
    used = 0
    for token in canonical_bytes(carrier):
        used += len(token)
        if used > limit:
            raise OperationPackError(
                "PYBENCH_GRAPH_RESOURCE_LIMIT",
                "igraph materialization budget exceeded",
                phase="graph_conversion",
            )


def to_igraph(graph: GraphPrimitive, *, max_collect_bytes: int = 16 * 1024 * 1024) -> Any:
    """Convert an ordinary graph only when every value can be round-tripped."""

    if type(max_collect_bytes) is not int or max_collect_bytes <= 0:
        _fail("max_collect_bytes must be a positive integer")
    content, bindings = graph._content, graph._bindings
    _reserve(content)
    carrier = {
        "schema": "scworkbench.graph.igraph_carrier.v1",
        "content": content,
        "bindings": bindings,
        "descriptor_identity": graph.identities.descriptor,
    }
    _admit_carrier(carrier, max_collect_bytes)
    edges = content["edges"]
    if any(edge["kind"] != "ordinary" for edge in edges):
        _fail("igraph adapter does not support hyperedges")
    directions = {edge["direction"] for edge in edges}
    if len(directions) > 1:
        _fail("igraph adapter does not support mixed actual edge directions")
    directed = (next(iter(directions)) if directions else content["direction_mode"]) == "directed"
    nodes = content["nodes"]
    positions = {row["node_id"]: index for index, row in enumerate(nodes)}
    by_edge: dict[str, list[dict[str, Any]]] = {edge["edge_id"]: [] for edge in edges}
    for row in content["incidences"]:
        by_edge[row["edge_id"]].append(row)
    endpoints = [
        tuple(positions[row["node_id"]] for row in by_edge[edge["edge_id"]]) for edge in edges
    ]
    igraph = _igraph()
    native = igraph.Graph(n=len(nodes), edges=endpoints, directed=directed)
    native.vs[_NODE_ID] = [row["node_id"] for row in nodes]
    native.es[_EDGE_ID] = [row["edge_id"] for row in edges]
    for scope in ("graph", "node", "edge"):
        for field in content["attributes"][scope]:
            values = [_native(field["type"], value) for value in field["values"]]
            if scope == "graph":
                native[field["name"]] = values[0]
            elif scope == "node":
                native.vs[field["name"]] = values
            else:
                native.es[field["name"]] = values
    native[_CARRIER] = canonical_json(carrier)
    return native


def from_igraph(native: Any, *, max_collect_bytes: int = 16 * 1024 * 1024) -> GraphPrimitive:
    """Verify the live native graph against its complete path-free carrier."""

    igraph = _igraph()
    if not isinstance(native, igraph.Graph):
        _fail("expected an igraph Graph")
    if type(max_collect_bytes) is not int or max_collect_bytes <= 0:
        _fail("max_collect_bytes must be a positive integer")
    if _CARRIER not in native.attributes():
        _fail("igraph carrier is missing")
    raw = native[_CARRIER]
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_collect_bytes:
        _fail("igraph carrier is malformed or exceeds materialization budget")
    try:
        carrier = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise OperationPackError(
            "PYBENCH_GRAPH_INVALID",
            "igraph carrier is not valid JSON",
            phase="graph_conversion",
        ) from exc
    if (
        not isinstance(carrier, dict)
        or set(carrier) != {"schema", "content", "bindings", "descriptor_identity"}
        or carrier["schema"] != "scworkbench.graph.igraph_carrier.v1"
    ):
        _fail("igraph carrier is malformed")
    graph = GraphPrimitive(
        carrier["content"], carrier["bindings"], max_collect_bytes=max_collect_bytes
    )
    if graph.identities.descriptor != carrier["descriptor_identity"]:
        _fail("igraph carrier descriptor identity differs")
    content = graph.content
    _reserve(content)
    _admit_carrier(carrier, max_collect_bytes)
    edges, nodes = content["edges"], content["nodes"]
    if len(native.vs) != len(nodes) or len(native.es) != len(edges):
        _fail("igraph vertex or edge count differs from carrier")
    directions = {edge["direction"] for edge in edges}
    if any(edge["kind"] != "ordinary" for edge in edges) or len(directions) > 1:
        _fail("igraph carrier declares an unsupported topology")
    directed = (next(iter(directions)) if directions else content["direction_mode"]) == "directed"
    if native.is_directed() != directed:
        _fail("igraph directedness differs from carrier")
    graph_fields = {field["name"] for field in content["attributes"]["graph"]}
    node_fields = {field["name"] for field in content["attributes"]["node"]}
    edge_fields = {field["name"] for field in content["attributes"]["edge"]}
    if (
        set(native.attributes()) != graph_fields | {_CARRIER}
        or set(native.vs.attributes()) != node_fields | {_NODE_ID}
        or set(native.es.attributes()) != edge_fields | {_EDGE_ID}
    ):
        _fail("igraph attributes differ from carrier")
    if native.vs[_NODE_ID] != [row["node_id"] for row in nodes]:
        _fail("igraph node IDs or order differ from carrier")
    if native.es[_EDGE_ID] != [row["edge_id"] for row in edges]:
        _fail("igraph edge IDs or order differ from carrier")
    positions = {row["node_id"]: index for index, row in enumerate(nodes)}
    by_edge: dict[str, list[dict[str, Any]]] = {edge["edge_id"]: [] for edge in edges}
    for row in content["incidences"]:
        by_edge[row["edge_id"]].append(row)
    for index, edge in enumerate(edges):
        expected = tuple(positions[row["node_id"]] for row in by_edge[edge["edge_id"]])
        actual = native.es[index].tuple
        if actual != expected if directed else sorted(actual) != sorted(expected):
            _fail("igraph endpoints differ from carrier")
    for scope in ("graph", "node", "edge"):
        for field in content["attributes"][scope]:
            observed = (
                [native[field["name"]]]
                if scope == "graph"
                else native.vs[field["name"]]
                if scope == "node"
                else native.es[field["name"]]
            )
            if len(observed) != len(field["values"]) or any(
                not _same(field["type"], expected, actual)
                for expected, actual in zip(field["values"], observed, strict=True)
            ):
                _fail("igraph attribute values differ from carrier")
    return graph
