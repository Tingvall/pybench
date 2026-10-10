from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pybench import graph_parts
from pybench.graph import graph_identities
from pybench.graph_parts import GraphPartsWriter, partition_chain_seed, partition_chain_update
from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity
from test_graph_transport import _grant, _transition

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "protocol/fixtures/graph-v1/content-identities.json"
    ).read_text()
)


def _writer(tmp_path: Path, *, axis_count: str | None = None) -> GraphPartsWriter:
    transition = _transition()
    grant = _grant(transition)
    if axis_count is not None:
        axis = grant["authorized_source_axes"][0]
        axis["axis_count"] = axis_count
        axis.pop("authority_identity")
        axis["authority_identity"] = _identity(
            "scworkbench.operation_pack.graph_source_axis_authority.v1",
            axis,
            "authority excluding authority_identity",
        )
        grant.pop("grant_identity")
        grant["grant_identity"] = _identity(
            "scworkbench.operation_pack.graph_write_grant.v1",
            grant,
            "grant excluding grant_identity",
        )
    return GraphPartsWriter(
        grant,
        tmp_path,
        transition_identity=transition,
        input_kinds={"source": "graph"},
        output_names=["graph"],
    )


def _content(name: str) -> dict:
    return next(row["content"] for row in FIXTURES["positive"] if row["id"] == name)


@pytest.mark.parametrize("failure", ["connect", "schema"])
@pytest.mark.parametrize("parent_exists", [False, True])
def test_constructor_failure_closes_handles_and_removes_only_owned_stage(
    tmp_path, monkeypatch, failure, parent_exists
):
    output_parent = tmp_path / "outputs"
    if parent_exists:
        output_parent.mkdir()
        (output_parent / "keep").write_text("preserve", encoding="utf-8")
    opened = []
    real_open = Path.open
    real_connect = sqlite3.connect

    def tracked_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path.name == "graph.parts.ndjson":
            opened.append(handle)
        return handle

    class FailingConnection:
        def __init__(self, path):
            self.inner = real_connect(path)
            self.closed = False

        def execute(self, statement):
            if statement.startswith("CREATE TABLE"):
                raise RuntimeError("injected schema failure")
            return self.inner.execute(statement)

        def close(self):
            self.closed = True
            self.inner.close()

    connections = []

    def failing_connect(path):
        if failure == "connect":
            raise RuntimeError("injected connect failure")
        connection = FailingConnection(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(graph_parts.sqlite3, "connect", failing_connect)
    with pytest.raises(RuntimeError, match="injected (connect|schema) failure"):
        _writer(tmp_path)
    assert opened and all(handle.closed for handle in opened)
    assert all(connection.closed for connection in connections)
    assert not (output_parent / "graph").exists()
    if parent_exists:
        assert sorted(path.name for path in output_parent.iterdir()) == ["keep"]
    else:
        assert not output_parent.exists()


def test_stage_empty_graph_has_exact_frozen_identities(tmp_path):
    content = _content("empty_undirected")
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": content["schema"],
                "direction_mode": content["direction_mode"],
            }
        ],
    )
    observed = writer.finish()
    assert observed["content_identity"] == graph_identities(content).content
    assert observed["binding_identity"] == graph_identities(content).bindings
    assert observed["descriptor_identity"] == graph_identities(content).descriptor
    output = tmp_path / "outputs" / "graph"
    assert sorted(item.name for item in output.iterdir()) == [
        "graph.parts.ndjson",
        "payload.json",
    ]
    assert len((output / "graph.parts.ndjson").read_text().splitlines()) == 1


def test_partition_chain_seed_and_ordered_updates():
    first = {"scheme": "scworkbench.graph.partition.v1", "sha256": "a" * 64}
    second = {"scheme": "scworkbench.graph.partition.v1", "sha256": "b" * 64}
    seed = partition_chain_seed("nodes")
    one = partition_chain_update("nodes", 1, seed, first)
    two = partition_chain_update("nodes", 2, one, second)
    reordered = partition_chain_update(
        "nodes",
        2,
        partition_chain_update("nodes", 1, seed, second),
        first,
    )
    assert len({seed["sha256"], one["sha256"], two["sha256"], reordered["sha256"]}) == 4


def test_scratch_queries_use_numeric_indexes_without_temp_sort(tmp_path):
    writer = _writer(tmp_path)
    try:
        statements = (
            (
                "SELECT row_json FROM rows WHERE component='attribute_values' AND scope=? "
                "AND field_ordinal_num=? ORDER BY entity_ordinal_num",
                ("node", 0),
            ),
            (
                "SELECT row_json FROM rows WHERE component='attribute_fields' AND scope=? "
                "ORDER BY field_ordinal_num",
                ("node",),
            ),
            (
                "SELECT row_json FROM rows WHERE component='binding_mappings' AND binding_id=? "
                "ORDER BY mapping_ordinal_num",
                ("b0",),
            ),
        )
        for statement, parameters in statements:
            plan = [
                row[3]
                for row in writer._db.execute(
                    "EXPLAIN QUERY PLAN " + statement,
                    parameters,
                )
            ]
            assert any("USING INDEX" in step for step in plan)
            assert not any("TEMP B-TREE" in step for step in plan)
    finally:
        writer.abort()


def test_stage_self_loop_and_typed_attribute(tmp_path):
    content = _content("directed_self_loop_mixed_mode")
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": content["schema"],
                "direction_mode": content["direction_mode"],
            }
        ],
    )
    writer.write_part("nodes", content["nodes"])
    writer.write_part("edges", content["edges"])
    writer.write_part("incidences", content["incidences"])
    observed = writer.finish()
    assert observed["content_identity"] == graph_identities(content).content


def test_stage_attribute_rows_keep_float_bits(tmp_path):
    content = _content("directed_self_loop_mixed_mode")
    content["attributes"]["node"] = [
        {
            "name": "score",
            "type": "float64_le_hex",
            "nullable": False,
            "values": ["0000000000000080"],
        },
    ]
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": content["schema"],
                "direction_mode": content["direction_mode"],
            }
        ],
    )
    writer.write_part("nodes", content["nodes"])
    writer.write_part("edges", content["edges"])
    writer.write_part("incidences", content["incidences"])
    writer.write_part(
        "attribute_fields",
        [
            {
                "scope": "node",
                "field_ordinal": "0",
                "name": "score",
                "type": "float64_le_hex",
                "nullable": False,
            }
        ],
    )
    writer.write_part(
        "attribute_values",
        [
            {
                "scope": "node",
                "field_ordinal": "0",
                "entity_ordinal": "0",
                "value": "0000000000000080",
            }
        ],
    )
    assert writer.finish()["content_identity"] == graph_identities(content).content


def test_self_rehashed_dangling_incidence_rejects_before_finish(tmp_path):
    content = _content("directed_self_loop_mixed_mode")
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": content["schema"],
                "direction_mode": content["direction_mode"],
            }
        ],
    )
    writer.write_part("nodes", content["nodes"])
    writer.write_part("edges", content["edges"])
    altered = [dict(row) for row in content["incidences"]]
    altered[1]["node_id"] = "missing"
    writer.write_part("incidences", altered)
    with pytest.raises(OperationPackError, match="dangling incidence"):
        writer.finish()
    writer.abort()
    assert not (tmp_path / "outputs" / "graph").exists()


def test_duplicate_id_rejects_and_abort_removes_stage(tmp_path):
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
            }
        ],
    )
    with pytest.raises(OperationPackError, match="duplicate graph ID"):
        writer.write_part("nodes", [{"node_id": "n"}, {"node_id": "n"}])
    with pytest.raises(OperationPackError, match="closed"):
        writer.write_part("nodes", [{"node_id": "fresh"}])
    with pytest.raises(OperationPackError, match="incomplete"):
        writer.finish()
    writer.abort()
    assert not (tmp_path / "outputs" / "graph").exists()


def test_partition_byte_budget_rejects_before_write(tmp_path):
    writer = _writer(tmp_path)
    with pytest.raises(OperationPackError):
        writer.write_part(
            "header",
            [
                {
                    "schema": "scworkbench.graph.content.v1",
                    "direction_mode": "undirected",
                    "extra": "x" * 20000,
                }
            ],
        )
    writer.abort()
    assert not (tmp_path / "outputs" / "graph").exists()


def test_self_rehashed_attribute_value_permutation_rejects(tmp_path):
    writer = _writer(tmp_path)
    writer.write_part(
        "header",
        [
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
            }
        ],
    )
    writer.write_part("nodes", [{"node_id": "n0"}, {"node_id": "n1"}])
    writer.write_part(
        "attribute_fields",
        [
            {
                "scope": "node",
                "field_ordinal": "0",
                "name": "weight",
                "type": "int64_decimal_string",
                "nullable": False,
            }
        ],
    )
    writer.write_part(
        "attribute_values",
        [
            {"scope": "node", "field_ordinal": "0", "entity_ordinal": "1", "value": "2"},
            {"scope": "node", "field_ordinal": "0", "entity_ordinal": "0", "value": "1"},
        ],
    )
    with pytest.raises(OperationPackError, match="declared field/entity order"):
        writer.finish()
    writer.abort()


def test_self_rehashed_binding_mapping_permutation_rejects(tmp_path):
    writer = _writer(tmp_path)
    axis = _grant(_transition())["authorized_source_axes"][0]
    writer.write_part(
        "header",
        [
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
            }
        ],
    )
    writer.write_part("nodes", [{"node_id": "n0"}, {"node_id": "n1"}])
    writer.write_part(
        "bindings",
        [
            {
                "binding_ordinal": "0",
                "binding_id": "b0",
                **{
                    key: axis[key]
                    for key in (
                        "axis_kind",
                        "source_ref",
                        "source_snapshot_identity",
                        "axis_content_identity",
                        "axis_order_identity",
                    )
                },
            }
        ],
    )
    writer.write_part(
        "binding_mappings",
        [
            {"binding_id": "b0", "mapping_ordinal": "1", "node_id": "n1", "axis_id": "a1"},
            {"binding_id": "b0", "mapping_ordinal": "0", "node_id": "n0", "axis_id": "a0"},
        ],
    )
    with pytest.raises(OperationPackError, match="declared binding/ordinal order"):
        writer.finish()
    writer.abort()


def test_binding_mapping_exceeding_issued_axis_count_rejects(tmp_path):
    writer = _writer(tmp_path, axis_count="1")
    axis = writer._grant["authorized_source_axes"][0]
    writer.write_part(
        "header",
        [
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
            }
        ],
    )
    writer.write_part("nodes", [{"node_id": "n0"}, {"node_id": "n1"}])
    writer.write_part(
        "bindings",
        [
            {
                "binding_ordinal": "0",
                "binding_id": "b0",
                **{
                    key: axis[key]
                    for key in (
                        "axis_kind",
                        "source_ref",
                        "source_snapshot_identity",
                        "axis_content_identity",
                        "axis_order_identity",
                    )
                },
            }
        ],
    )
    writer.write_part(
        "binding_mappings",
        [
            {"binding_id": "b0", "mapping_ordinal": "0", "node_id": "n0", "axis_id": "a0"},
            {"binding_id": "b0", "mapping_ordinal": "1", "node_id": "n1", "axis_id": "a1"},
        ],
    )
    with pytest.raises(OperationPackError, match="exceeds the issued source-axis count"):
        writer.finish()
    writer.abort()
