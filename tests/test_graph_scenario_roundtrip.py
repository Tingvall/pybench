from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.graph import GraphPrimitive, graph_identities
from pybench.graph_igraph import from_igraph, to_igraph
from pybench.operation_packs import OperationPackError, canonical_json
from test_graph_parts import _writer

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "protocol/fixtures/graph-v1/scenario-roundtrip-vectors.json").read_text()
)
SCHEMA = json.loads((ROOT / "protocol/graph-v1.schema.json").read_text())
CONTENT_VALIDATOR = Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/content"})
SCOPES = ("graph", "node", "edge", "incidence")
PLAN_COUNTS = {
    "airline_parallel_isolate": (4, 3, 6),
    "social_directed": (3, 2, 4),
    "social_undirected": (3, 2, 4),
    "social_mixed": (3, 2, 4),
    "signed_weighted_grn": (3, 2, 4),
    "ppi_undirected": (3, 2, 4),
    "reaction_hyper_repeated": (4, 1, 5),
    "transport_mixed": (3, 2, 4),
    "empty_undirected": (0, 0, 0),
    "empty_directed": (0, 0, 0),
    "empty_mixed": (0, 0, 0),
    "isolated_only": (2, 0, 0),
    "disconnected_self_loop_mixed": (4, 2, 4),
    "disconnected_undirected": (3, 1, 2),
    "disconnected_undirected_supplemental": (5, 2, 4),
    "self_loops_mixed": (1, 2, 4),
    "repeated_hyper_incidences": (2, 2, 7),
}
FROZEN_PLAN_RECORDS = {
    "airline_parallel_isolate": (["ARN", "LHR", "JFK", "KEF"], ["f1", "f2", "f3"]),
    "social_directed": (["ada", "bea", "cy"], ["follow1", "follow2"]),
    "social_undirected": (["ada", "bea", "cy"], ["friend1", "friend2"]),
    "signed_weighted_grn": (["TF1", "G1", "G2"], ["reg1", "reg2"]),
    "ppi_undirected": (["P1", "P2", "P3"], ["p1", "p2"]),
    "reaction_hyper_repeated": (["A", "B", "C", "D"], ["reaction"]),
    "transport_mixed": (["hub", "port", "rail"], ["ferry", "walk"]),
}


def _component_rows(content):
    rows = {
        "header": [{"schema": content["schema"], "direction_mode": content["direction_mode"]}],
        "nodes": content["nodes"],
        "edges": content["edges"],
        "incidences": content["incidences"],
        "attribute_fields": [],
        "attribute_values": [],
    }
    for scope in SCOPES:
        for field_ordinal, field in enumerate(content["attributes"][scope]):
            rows["attribute_fields"].append(
                {
                    "scope": scope,
                    "field_ordinal": str(field_ordinal),
                    "name": field["name"],
                    "type": field["type"],
                    "nullable": field["nullable"],
                }
            )
            for entity_ordinal, value in enumerate(field["values"]):
                rows["attribute_values"].append(
                    {
                        "scope": scope,
                        "field_ordinal": str(field_ordinal),
                        "entity_ordinal": str(entity_ordinal),
                        "value": value,
                    }
                )
    return rows


def test_scenario_vector_generator_is_stable():
    assert FIXTURE["schema"] == "scworkbench.graph.scenario_roundtrip_fixture.v1"
    assert FIXTURE["accepted_design_commit"] == "fa929601c7b8c67c2947b400341bfd90bd80d925"
    assert len(FIXTURE["cases"]) == 22
    cases_by_id = {case["id"]: case for case in FIXTURE["cases"]}
    assert len(cases_by_id) == len(FIXTURE["cases"])
    for name, counts in PLAN_COUNTS.items():
        content = cases_by_id[name]["content"]
        assert tuple(len(content[key]) for key in ("nodes", "edges", "incidences")) == counts
    for name, (node_ids, edge_ids) in FROZEN_PLAN_RECORDS.items():
        content = cases_by_id[name]["content"]
        assert [row["node_id"] for row in content["nodes"]] == node_ids
        assert [row["edge_id"] for row in content["edges"]] == edge_ids
    grn = cases_by_id["signed_weighted_grn"]["content"]
    assert grn["attributes"]["edge"] == [
        {"name": "sign", "type": "utf8", "nullable": False, "values": ["positive", "negative"]},
        {
            "name": "weight",
            "type": "float64_le_hex",
            "nullable": False,
            "values": ["9a9999999999e93f", "9a9999999999d9bf"],
        },
    ]
    identities = {
        case["id"]: case["expected"]["content_identity"]["sha256"] for case in FIXTURE["cases"]
    }
    assert (
        len({identities[name] for name in ("empty_undirected", "empty_directed", "empty_mixed")})
        == 3
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_graph_scenario_vectors.py"), "--check"],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_accepted_scenario_structure_and_identity(case):
    content, expected = case["content"], case["expected"]
    CONTENT_VALIDATOR.validate(content)
    graph = GraphPrimitive(content)
    assert graph.content == content
    assert graph.bindings == []
    identities = graph_identities(content)
    assert identities.content == expected["content_identity"]
    assert identities.bindings == expected["binding_identity"]
    assert identities.descriptor == expected["descriptor_identity"]
    assert len(canonical_json(content).encode("utf-8")) == int(expected["canonical_content_bytes"])
    rows = _component_rows(content)
    assert {
        component: str(len(rows[component]))
        for component in (
            "nodes",
            "edges",
            "incidences",
            "attribute_fields",
            "attribute_values",
        )
    } == expected["component_counts"]
    observations = expected["observations"]
    participating = {row["node_id"] for row in content["incidences"]}
    isolated = [row["node_id"] for row in content["nodes"] if row["node_id"] not in participating]
    assert isolated == observations.get("isolated_nodes", [])
    if "parallel_edges" in observations:
        assert observations["parallel_edges"] == [
            content["edges"][0]["edge_id"],
            content["edges"][1]["edge_id"],
        ]
        assert content["edges"][0] != content["edges"][1]
        assert [row["node_id"] for row in content["incidences"][:2]] == [
            row["node_id"] for row in content["incidences"][2:4]
        ]
    if "repeated_participation" in observations:
        repeat = observations["repeated_participation"]
        assert (
            sum(
                row["edge_id"] == repeat["edge_id"] and row["node_id"] == repeat["node_id"]
                for row in content["incidences"]
            )
            == repeat["count"]
        )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_accepted_scenario_private_stage_roundtrip(case, tmp_path):
    rows = _component_rows(case["content"])
    writer = _writer(tmp_path)
    for component, values in rows.items():
        for offset in range(0, len(values), 20):
            writer.write_part(component, values[offset : offset + 20])
    observed = writer.finish()
    expected = case["expected"]
    assert observed["content_identity"] == expected["content_identity"]
    assert observed["binding_identity"] == expected["binding_identity"]
    assert observed["descriptor_identity"] == expected["descriptor_identity"]
    assert {
        key: observed["component_counts"][key] for key in expected["component_counts"]
    } == expected["component_counts"]
    output = tmp_path / "outputs" / "graph"
    parts = [json.loads(line) for line in (output / "graph.parts.ndjson").read_text().splitlines()]
    assert [part["component"] for part in parts] == [
        component for component, values in rows.items() for _ in range(0, len(values), 20)
    ]
    assert {
        component: [row for part in parts if part["component"] == component for row in part["rows"]]
        for component in rows
    } == rows
    descriptor = json.loads((output / "payload.json").read_text())
    assert descriptor["observation"] == observed


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_accepted_scenario_igraph_boundary(case):
    graph = GraphPrimitive(case["content"])
    if not case["expected"]["igraph_eligible"]:
        with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
            to_igraph(graph)
        assert graph.content == case["content"]
        return
    pytest.importorskip("igraph")
    converted = to_igraph(graph)
    restored = from_igraph(converted)
    assert restored.content == case["content"]
    assert restored.bindings == []
    assert restored.identities.descriptor == case["expected"]["descriptor_identity"]
