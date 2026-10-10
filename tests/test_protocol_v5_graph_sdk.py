from __future__ import annotations

import json

import pytest

from pybench.graph import GraphPrimitive, identity
from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity
from pybench.protocol_v5 import DiskStageSnapshot, V5Readers, V5Writers, audit_graph_stage
from test_graph_transport import _grant, _transition


def _request() -> dict:
    transition = _transition()
    graph_grant = _grant(transition)
    graph_grant["authorized_source_axes"] = []
    graph_grant.pop("grant_identity")
    graph_grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        graph_grant,
        "grant excluding grant_identity",
    )
    return {
        "transition": {"transition_identity": transition},
        "inputs": [{"name": "matrix", "descriptor": {"kind": "matrix"}}],
        "outputs": [
            {
                "name": "graph",
                "allocated_id": "art_graph_1",
                "descriptor": {
                    "kind": "graph",
                    "writer": "write_graph",
                },
            },
            {
                "name": "summary",
                "allocated_id": "art_summary_1",
                "descriptor": {
                    "kind": "obs_table",
                    "writer": "write_terminal_primitive",
                    "relative_root": "outputs/summary",
                    "grant_identity": {
                        "scheme": "scworkbench.operation_pack.output_grant.v3",
                        "sha256": "a" * 64,
                    },
                },
            },
        ],
        "graph_write_grants": [graph_grant],
    }


def test_mixed_graph_and_ordinary_output_share_writer_closure(tmp_path):
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    writers = V5Writers(_request(), tmp_path, [], {})
    graph_observation = writers.write_graph("graph", GraphPrimitive(content))
    writers.write_terminal_primitive("summary", {"kind": "obs_table", "rows": [{"n": 0}]})
    assert set(writers.writes) == {"graph", "summary"}
    assert writers.graph_write_observations() == [graph_observation]
    graph_root = tmp_path / "outputs" / "graph"
    assert sorted(path.name for path in graph_root.iterdir()) == [
        "graph.parts.ndjson",
        "payload.json",
    ]
    assert json.loads((graph_root / "payload.json").read_text())["observation"] == graph_observation


def test_graph_output_cannot_be_written_twice(tmp_path):
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    writers = V5Writers(_request(), tmp_path, [], {})
    writers.write_graph("graph", GraphPrimitive(content))
    with pytest.raises(OperationPackError, match="already opened"):
        writers.write_graph("graph", GraphPrimitive(content))


def test_materialized_graph_exceeding_output_collection_cap_leaves_no_stage(tmp_path):
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    request = _request()
    request["graph_write_grants"][0]["resource_budgets"]["max_collect_bytes"] = "1"
    writers = V5Writers(request, tmp_path, [], {})
    with pytest.raises(OperationPackError, match="exceeds issued collection budget"):
        writers.write_graph("graph", GraphPrimitive(content))
    assert not (tmp_path / "outputs").exists()


class _Reader:
    def __init__(self):
        self.complete = False

    def __iter__(self):
        yield {"component": "header"}
        self.complete = True

    def usage(self):
        if not self.complete:
            raise OperationPackError(
                "PYBENCH_GRAPH_CAPABILITY_MISMATCH", "incomplete", phase="graph_read"
            )
        return {"complete": True}


def test_graph_reader_is_single_use_and_must_be_consumed():
    readers = V5Readers({}, {"source": _Reader()})
    with pytest.raises(OperationPackError, match="incomplete"):
        readers.graph_read_usage()
    assert list(readers.read_graph_parts("source")) == [{"component": "header"}]
    assert readers.graph_read_usage() == [{"complete": True}]
    with pytest.raises(OperationPackError, match="already used"):
        list(readers.read_graph_parts("source"))


def test_disk_snapshot_restores_created_replaced_deleted_and_nested_files(tmp_path):
    original = tmp_path / "inputs" / "large.bin"
    original.parent.mkdir()
    original.write_bytes(b"abc" * 1024 * 1024)
    nested = tmp_path / "nested" / "keep.txt"
    nested.parent.mkdir()
    nested.write_text("original")
    snapshot = DiskStageSnapshot(tmp_path)
    try:
        assert all(
            isinstance(value[0], int) and len(value[1]) == 64 for value in snapshot.before.values()
        )
        original.write_bytes(b"changed")
        nested.unlink()
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "new.txt").write_text("new")
        assert snapshot.after() != snapshot.before
        snapshot.restore()
        assert snapshot.after() == snapshot.before
        assert not (tmp_path / "outputs").exists()
    finally:
        snapshot.close()


def test_graph_stage_reaudit_rejects_self_rehashed_payload_tamper(tmp_path):
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    request = _request()
    writers = V5Writers(request, tmp_path, [], {})
    observation = writers.write_graph("graph", GraphPrimitive(content))
    kwargs = {
        "transition_identity": request["transition"]["transition_identity"],
        "input_kinds": {"matrix": "matrix"},
        "output_names": ["graph"],
    }
    assert (
        audit_graph_stage(request["graph_write_grants"][0], tmp_path, observation, **kwargs)
        == observation
    )
    parts = tmp_path / "outputs" / "graph" / "graph.parts.ndjson"
    altered = json.loads(parts.read_text().splitlines()[0])
    altered["rows"][0]["direction_mode"] = "directed"
    altered["partition_identity"] = identity(
        "scworkbench.graph.partition.v1",
        {key: value for key, value in altered.items() if key != "partition_identity"},
    )
    parts.write_text(json.dumps(altered, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(OperationPackError, match="observation differs"):
        audit_graph_stage(request["graph_write_grants"][0], tmp_path, observation, **kwargs)
