"""Small public-only handlers for cross-language graph ABI4 qualification."""

from __future__ import annotations

from pybench import GraphPrimitive, graph_binding_from_source, operation_pack_handlers_v4


def _content(direction_mode: str, nodes: list[dict]) -> dict:
    return {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": direction_mode,
        "nodes": nodes,
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }


def _summary(nodes: int) -> dict:
    return {
        "kind": "obs_table",
        "dimensions": [1, 1],
        "rows": [{"nodes": nodes}],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }


def matrix_to_graph(readers, writers, params, context):
    matrix = readers["matrix"]()
    nodes = [{"node_id": cell_id} for cell_id in matrix["cell_id"]]
    writers.write_graph("result", GraphPrimitive(_content("undirected", nodes)))


def graph_and_metadata(readers, writers, params, context):
    metadata = readers["metadata"]()
    if metadata["kind"] != "obs_table":
        raise ValueError("Metadata input is not an obs_table")
    parts = iter(readers.read_graph_parts("source"))
    header = next(parts)
    if header["component"] != "header" or len(header["rows"]) != 1:
        raise ValueError("Graph broker omitted its header")
    graph = writers.open_graph("result", header["rows"][0])
    nodes = 0
    try:
        for part in parts:
            component = part["component"]
            if component == "nodes":
                nodes += len(part["rows"])
            if component in {
                "nodes",
                "edges",
                "incidences",
                "attribute_fields",
                "attribute_values",
            }:
                graph.write_part(component, part["rows"])
        graph.finish()
    except BaseException:
        graph.abort()
        raise
    writers.write_terminal_primitive("summary", _summary(nodes))


def graph_to_table(readers, writers, params, context):
    nodes = 0
    for part in readers.read_graph_parts("source"):
        if part["component"] == "nodes":
            nodes += len(part["rows"])
    writers.write_terminal_primitive("summary", _summary(nodes))


def graph_and_cell_vector(readers, writers, params, context):
    labels = readers["labels"]()
    cells = set(labels["cell_id"])
    parts = iter(readers.read_graph_parts("source"))
    header = next(parts)
    if header["component"] != "header" or len(header["rows"]) != 1:
        raise ValueError("Graph broker omitted its header")
    graph = writers.open_graph("result", header["rows"][0])
    nodes = []
    try:
        for part in parts:
            component = part["component"]
            if component == "nodes":
                nodes.extend(row["node_id"] for row in part["rows"])
            if component in {
                "nodes",
                "edges",
                "incidences",
                "attribute_fields",
                "attribute_values",
            }:
                graph.write_part(component, part["rows"])
        source = context["graph_source_axes"]["result"][0]
        binding = graph_binding_from_source(
            source,
            "cell_nodes",
            [{"node_id": node_id, "axis_id": node_id} for node_id in nodes if node_id in cells],
        )
        graph.write_part(
            "bindings",
            [
                {
                    "binding_ordinal": "0",
                    "binding_id": binding["binding_id"],
                    "axis_kind": binding["axis_kind"],
                    "source_ref": binding["source_ref"],
                    "source_snapshot_identity": binding["source_snapshot_identity"],
                    "axis_content_identity": binding["axis_content_identity"],
                    "axis_order_identity": binding["axis_order_identity"],
                }
            ],
        )
        if binding["mapping"]:
            graph.write_part(
                "binding_mappings",
                [
                    {"binding_id": binding["binding_id"], "mapping_ordinal": str(index), **pair}
                    for index, pair in enumerate(binding["mapping"])
                ],
            )
        graph.finish()
    except BaseException:
        graph.abort()
        raise
    writers.write_terminal_primitive("summary", _summary(len(nodes)))


def register():
    return operation_pack_handlers_v4(
        {
            "scwbgraphfixture::matrix_to_graph": matrix_to_graph,
            "scwbgraphfixture::graph_and_metadata": graph_and_metadata,
            "scwbgraphfixture::graph_to_table": graph_to_table,
            "scwbgraphfixture::graph_and_cell_vector": graph_and_cell_vector,
        }
    )
