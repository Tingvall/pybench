"""Generate accepted generic-graph scenario vectors for R/Python parity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pybench.graph import graph_identities
from pybench.operation_packs import canonical_json

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "protocol/fixtures/graph-v1/scenario-roundtrip-vectors.json"
DESIGN_COMMIT = "fa929601c7b8c67c2947b400341bfd90bd80d925"


def _edge(edge_id, kind, direction, *incidences):
    return edge_id, kind, direction, incidences


def _case(case_id, mode, nodes, edges, *, attributes=None, igraph=True, observations=None):
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": mode,
        "nodes": [{"node_id": node_id} for node_id in nodes],
        "edges": [
            {"edge_id": edge_id, "kind": kind, "direction": direction}
            for edge_id, kind, direction, _ in edges
        ],
        "incidences": [
            {
                "incidence_id": incidence_id,
                "edge_id": edge_id,
                "node_id": node_id,
                "ordinal": ordinal,
                "role": role,
            }
            for edge_id, _, _, members in edges
            for ordinal, (role, node_id, incidence_id) in enumerate(members)
        ],
        "attributes": {
            scope: (attributes or {}).get(scope, [])
            for scope in ("graph", "node", "edge", "incidence")
        },
    }
    identities = graph_identities(content)
    return {
        "id": case_id,
        "content": content,
        "expected": {
            "content_identity": identities.content,
            "binding_identity": identities.bindings,
            "descriptor_identity": identities.descriptor,
            "canonical_content_bytes": str(len(canonical_json(content).encode("utf-8"))),
            "component_counts": {
                "nodes": str(len(content["nodes"])),
                "edges": str(len(content["edges"])),
                "incidences": str(len(content["incidences"])),
                "attribute_fields": str(
                    sum(len(fields) for fields in content["attributes"].values())
                ),
                "attribute_values": str(
                    sum(
                        len(field["values"])
                        for fields in content["attributes"].values()
                        for field in fields
                    )
                ),
            },
            "igraph_eligible": igraph,
            "observations": observations or {},
        },
    }


def _typed_fields(scope):
    values = {
        "graph": ([True], ["-9223372036854775808"], ["0000000000000080"], [""]),
        "node": (
            [True, None],
            ["-9223372036854775808", "9223372036854775807"],
            ["0000000000000000", "0100000000000000"],
            ["alpha", ""],
        ),
        "edge": ([None], ["0"], ["000000000000f03f"], ["edge"]),
        "incidence": (
            [False, True],
            ["-1", "1"],
            ["0000000000000080", "0000000000000000"],
            ["tail", "head"],
        ),
    }[scope]
    return [
        {"name": name, "type": kind, "nullable": None in entries, "values": entries}
        for (name, kind), entries in zip(
            (
                ("flag", "bool"),
                ("count", "int64_decimal_string"),
                ("measure", "float64_le_hex"),
                ("label", "utf8"),
            ),
            values,
            strict=True,
        )
    ]


def scenarios():
    return [
        _case(
            "airline_parallel_isolate",
            "directed",
            ["ARN", "LHR", "JFK", "KEF"],
            [
                _edge(
                    "f1",
                    "ordinary",
                    "directed",
                    ("tail", "ARN", "i1"),
                    ("head", "LHR", "i2"),
                ),
                _edge("f2", "ordinary", "directed", ("tail", "ARN", "i3"), ("head", "LHR", "i4")),
                _edge("f3", "ordinary", "directed", ("tail", "LHR", "i5"), ("head", "JFK", "i6")),
            ],
            observations={
                "isolated_nodes": ["KEF"],
                "parallel_edges": ["f1", "f2"],
            },
        ),
        _case(
            "social_mixed",
            "mixed",
            ["alice", "bob", "carol"],
            [
                _edge(
                    "follows",
                    "ordinary",
                    "directed",
                    ("tail", "alice", "i1"),
                    ("head", "bob", "i2"),
                ),
                _edge(
                    "friendship",
                    "ordinary",
                    "undirected",
                    ("member", "bob", "i3"),
                    ("member", "carol", "i4"),
                ),
            ],
            igraph=False,
        ),
        _case(
            "social_directed",
            "directed",
            ["ada", "bea", "cy"],
            [
                _edge(
                    "follow1",
                    "ordinary",
                    "directed",
                    ("tail", "ada", "i1"),
                    ("head", "bea", "i2"),
                ),
                _edge(
                    "follow2", "ordinary", "directed", ("tail", "bea", "i3"), ("head", "ada", "i4")
                ),
            ],
            observations={"isolated_nodes": ["cy"]},
        ),
        _case(
            "social_undirected",
            "undirected",
            ["ada", "bea", "cy"],
            [
                _edge(
                    "friend1",
                    "ordinary",
                    "undirected",
                    ("member", "ada", "i1"),
                    ("member", "bea", "i2"),
                ),
                _edge(
                    "friend2",
                    "ordinary",
                    "undirected",
                    ("member", "bea", "i3"),
                    ("member", "cy", "i4"),
                ),
            ],
        ),
        _case(
            "signed_weighted_grn",
            "directed",
            ["TF1", "G1", "G2"],
            [
                _edge(
                    "reg1",
                    "ordinary",
                    "directed",
                    ("tail", "TF1", "i1"),
                    ("head", "G1", "i2"),
                ),
                _edge(
                    "reg2",
                    "ordinary",
                    "directed",
                    ("tail", "TF1", "i3"),
                    ("head", "G2", "i4"),
                ),
            ],
            attributes={
                "edge": [
                    {
                        "name": "sign",
                        "type": "utf8",
                        "nullable": False,
                        "values": ["positive", "negative"],
                    },
                    {
                        "name": "weight",
                        "type": "float64_le_hex",
                        "nullable": False,
                        "values": ["9a9999999999e93f", "9a9999999999d9bf"],
                    },
                ]
            },
        ),
        _case(
            "ppi_undirected",
            "undirected",
            ["P1", "P2", "P3"],
            [
                _edge(
                    "p1", "ordinary", "undirected", ("member", "P1", "i1"), ("member", "P2", "i2")
                ),
                _edge(
                    "p2", "ordinary", "undirected", ("member", "P2", "i3"), ("member", "P3", "i4")
                ),
            ],
        ),
        _case(
            "reaction_hyper_repeated",
            "directed",
            ["A", "B", "C", "D"],
            [
                _edge(
                    "reaction",
                    "hyper",
                    "directed",
                    ("tail", "A", "i1"),
                    ("tail", "A", "i2"),
                    ("tail", "B", "i3"),
                    ("head", "C", "i4"),
                    ("head", "D", "i5"),
                )
            ],
            attributes={
                "incidence": [
                    {
                        "name": "coefficient",
                        "type": "int64_decimal_string",
                        "nullable": False,
                        "values": ["1", "1", "1", "1", "1"],
                    }
                ]
            },
            igraph=False,
            observations={
                "repeated_participation": {"edge_id": "reaction", "node_id": "A", "count": 2}
            },
        ),
        _case(
            "transport_mixed",
            "mixed",
            ["hub", "port", "rail"],
            [
                _edge(
                    "ferry", "ordinary", "directed", ("tail", "hub", "i1"), ("head", "port", "i2")
                ),
                _edge(
                    "walk",
                    "ordinary",
                    "undirected",
                    ("member", "port", "i3"),
                    ("member", "rail", "i4"),
                ),
            ],
            igraph=False,
        ),
        _case("empty_undirected", "undirected", [], []),
        _case("empty_directed", "directed", [], []),
        _case("empty_mixed", "mixed", [], []),
        _case(
            "isolated_only",
            "undirected",
            ["x", "y"],
            [],
            attributes={
                "graph": [
                    {"name": "label", "type": "utf8", "nullable": False, "values": ["isolates"]}
                ]
            },
            observations={"isolated_nodes": ["x", "y"]},
        ),
        _case(
            "mixed_declared_directed",
            "mixed",
            ["a", "b"],
            [_edge("d1", "ordinary", "directed", ("tail", "a", "i1"), ("head", "b", "i2"))],
        ),
        _case(
            "mixed_declared_undirected",
            "mixed",
            ["a", "b"],
            [_edge("u1", "ordinary", "undirected", ("member", "a", "i1"), ("member", "b", "i2"))],
        ),
        _case(
            "disconnected_undirected",
            "undirected",
            ["a", "b", "c"],
            [_edge("link", "ordinary", "undirected", ("member", "a", "i1"), ("member", "b", "i2"))],
            observations={"isolated_nodes": ["c"]},
        ),
        _case(
            "disconnected_undirected_supplemental",
            "undirected",
            ["a", "b", "c", "d", "e"],
            [
                _edge("ab", "ordinary", "undirected", ("member", "a", "i1"), ("member", "b", "i2")),
                _edge("cd", "ordinary", "undirected", ("member", "c", "i3"), ("member", "d", "i4")),
            ],
            observations={"isolated_nodes": ["e"]},
        ),
        _case(
            "disconnected_self_loop_mixed",
            "mixed",
            ["a", "b", "c", "d"],
            [
                _edge("self", "ordinary", "directed", ("tail", "a", "i1"), ("head", "a", "i2")),
                _edge(
                    "pair", "ordinary", "undirected", ("member", "c", "i3"), ("member", "d", "i4")
                ),
            ],
            igraph=False,
            observations={"isolated_nodes": ["b"]},
        ),
        _case(
            "directed_self_loop",
            "directed",
            ["a"],
            [_edge("loop", "ordinary", "directed", ("tail", "a", "i1"), ("head", "a", "i2"))],
        ),
        _case(
            "undirected_self_loop",
            "undirected",
            ["a"],
            [_edge("loop", "ordinary", "undirected", ("member", "a", "i1"), ("member", "a", "i2"))],
        ),
        _case(
            "self_loops_mixed",
            "mixed",
            ["a"],
            [
                _edge(
                    "directed_loop",
                    "ordinary",
                    "directed",
                    ("tail", "a", "i1"),
                    ("head", "a", "i2"),
                ),
                _edge(
                    "undirected_loop",
                    "ordinary",
                    "undirected",
                    ("member", "a", "i3"),
                    ("member", "a", "i4"),
                ),
            ],
            igraph=False,
        ),
        _case(
            "repeated_hyper_incidences",
            "mixed",
            ["a", "b"],
            [
                _edge(
                    "h",
                    "hyper",
                    "undirected",
                    ("member", "a", "i1"),
                    ("member", "a", "i2"),
                    ("member", "b", "i3"),
                ),
                _edge(
                    "d",
                    "hyper",
                    "directed",
                    ("tail", "a", "i4"),
                    ("tail", "a", "i5"),
                    ("head", "a", "i6"),
                    ("head", "b", "i7"),
                ),
            ],
            igraph=False,
            observations={"repeated_participation": {"edge_id": "h", "node_id": "a", "count": 2}},
        ),
        _case(
            "unary_undirected_hyperedge",
            "undirected",
            ["a"],
            [_edge("h", "hyper", "undirected", ("member", "a", "i1"))],
            igraph=False,
        ),
        _case(
            "typed_all_scopes",
            "directed",
            ["a", "b"],
            [_edge("e", "ordinary", "directed", ("tail", "a", "i1"), ("head", "b", "i2"))],
            attributes={
                scope: _typed_fields(scope) for scope in ("graph", "node", "edge", "incidence")
            },
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = {
        "schema": "scworkbench.graph.scenario_roundtrip_fixture.v1",
        "accepted_design_commit": DESIGN_COMMIT,
        "cases": scenarios(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != encoded:
            raise SystemExit("graph scenario fixture differs; regenerate explicitly")
    else:
        TARGET.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
