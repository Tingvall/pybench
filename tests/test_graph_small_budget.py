from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

import pybench.graph_parts as graph_parts
from pybench.graph import (
    GraphPrimitive,
    graph_binding_from_source,
    graph_identities,
    graph_source_axis_projection,
)
from pybench.graph_broker import GraphBrokerClient
from pybench.graph_parts import GraphPartsWriter
from pybench.operation_packs import OperationPackError, canonical_json
from pybench.operation_packs_v2 import _identity
from pybench.protocol_v5 import V5Writers, audit_graph_stage
from test_graph_transport import _grant, _transition
from test_protocol_v5_graph_sdk import _request

NODE_COUNT = 96
PARTITION_ROWS = 4
LABEL_BYTES = 2048
BROKER_TOKEN = "a" * 64


def _content() -> dict:
    return {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "directed",
        "nodes": [{"node_id": f"n{index:03d}"} for index in range(NODE_COUNT)],
        "edges": [{"edge_id": "h0", "kind": "hyper", "direction": "directed"}],
        "incidences": [
            {
                "incidence_id": f"i{index:03d}",
                "edge_id": "h0",
                "node_id": f"n{index:03d}",
                "ordinal": index,
                "role": "tail" if index < NODE_COUNT // 2 else "head",
            }
            for index in range(NODE_COUNT)
        ],
        "attributes": {
            "graph": [],
            "node": [
                {
                    "name": "label",
                    "type": "utf8",
                    "nullable": False,
                    "values": ["x" * LABEL_BYTES + f":{index:03d}" for index in range(NODE_COUNT)],
                }
            ],
            "edge": [],
            "incidence": [],
        },
    }


def _grant_and_transition(
    *,
    staged_bytes: int = 2 * 1024**2,
    spill_bytes: int = 2 * 1024**2,
    memory_bytes: int = 1024**2,
):
    transition = _transition()
    grant = _grant(transition)
    grant["resource_budgets"].update(
        max_nodes=str(NODE_COUNT),
        max_edges="1",
        max_incidences=str(NODE_COUNT),
        max_attribute_fields="1",
        max_staged_bytes=str(staged_bytes),
        max_published_bytes=str(2 * 1024**2),
        max_wal_bytes=str(2 * 1024**2),
        max_spill_bytes=str(spill_bytes),
        max_memory_bytes=str(memory_bytes),
        max_collect_bytes="4096",
        partition_limits={"max_rows": str(PARTITION_ROWS), "max_decoded_bytes": "16384"},
    )
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    return grant, transition


def _named_grant(name: str, transition: dict) -> dict:
    grant, _ = _grant_and_transition()
    grant["output_name"] = name
    for axis in grant["authorized_source_axes"]:
        axis["output_name"] = name
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
    return grant


def _writer(root: Path, grant: dict, transition: dict) -> GraphPartsWriter:
    return GraphPartsWriter(
        grant,
        root,
        transition_identity=transition,
        input_kinds={"source": "graph"},
        output_names=["graph"],
    )


def _chunks(rows: list[dict]):
    for start in range(0, len(rows), PARTITION_ROWS):
        yield rows[start : start + PARTITION_ROWS]


def _write_content(writer: GraphPartsWriter, content: dict, grant: dict, *, header=True) -> dict:
    if header:
        writer.write_part(
            "header",
            [{"schema": content["schema"], "direction_mode": content["direction_mode"]}],
        )
    for component in ("nodes", "edges", "incidences"):
        for rows in _chunks(content[component]):
            writer.write_part(component, rows)
    writer.write_part(
        "attribute_fields",
        [
            {
                "scope": "node",
                "field_ordinal": "0",
                "name": "label",
                "type": "utf8",
                "nullable": False,
            }
        ],
    )
    for rows in _chunks(
        [
            {
                "scope": "node",
                "field_ordinal": "0",
                "entity_ordinal": str(index),
                "value": value,
            }
            for index, value in enumerate(content["attributes"]["node"][0]["values"])
        ]
    ):
        writer.write_part("attribute_values", rows)
    axis = grant["authorized_source_axes"][0]
    binding = graph_binding_from_source(
        graph_source_axis_projection(axis),
        "cell_source",
        [
            {"node_id": "n000", "axis_id": "c0"},
            {"node_id": "n001", "axis_id": "c1"},
        ],
    )
    writer.write_part(
        "bindings",
        [
            {
                "binding_ordinal": "0",
                "binding_id": binding["binding_id"],
                **{
                    key: binding[key]
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
            {"binding_id": binding["binding_id"], "mapping_ordinal": str(index), **mapping}
            for index, mapping in enumerate(binding["mapping"])
        ],
    )
    return binding


def _read_grant(observation: dict, source_snapshot_identity: dict) -> dict:
    value = {
        "schema": "scworkbench.graph.read_grant.v1",
        "input_name": "source",
        "source_artifact_id": "art_graph_small",
        "source_snapshot_identity": source_snapshot_identity,
        "content_identity": observation["content_identity"],
        "binding_identity": observation["binding_identity"],
        "descriptor_identity": observation["descriptor_identity"],
        "component_counts": observation["component_counts"],
        "partition_limits": {"max_rows": str(PARTITION_ROWS), "max_decoded_bytes": "16384"},
        "selected_binding_ids": ["cell_source"],
    }
    value["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_read_grant.v1",
        value,
        "grant excluding grant_identity",
    )
    return value


def _serve_parts(root: Path, parts: list[dict], errors: list[BaseException]) -> None:
    broker = root / "broker"

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.002)
        assert path.exists()

    try:
        for index in range(len(parts) + 1):
            request_path = broker / "request.json"
            wait_for(request_path)
            request = json.loads(request_path.read_text(encoding="utf-8"))
            partition = parts[index] if index < len(parts) else None
            if partition is None:
                assert request["component"] == "end"
            else:
                assert request["component"] == partition["component"]
                assert request["first_ordinal"] == partition["first_ordinal"]
            response = {
                "schema": "scworkbench.graph.broker_read_response.v1",
                "partition": partition,
                "complete": partition is None,
                "call_identity": request["call_identity"],
            }
            response["response_identity"] = _identity(
                "scworkbench.graph.broker_read_response.v1",
                response,
                "response excluding response_identity",
            )
            directory = broker / "response"
            directory.mkdir()
            (directory / "manifest.json").write_text(canonical_json(response), encoding="utf-8")
            acknowledgement = broker / "acknowledgement.json"
            wait_for(acknowledgement)
            assert (
                json.loads(acknowledgement.read_text())["response_identity"]
                == response["response_identity"]
            )
            shutil.rmtree(directory)
            acknowledgement.unlink()
            request_path.unlink()
    except BaseException as exc:
        errors.append(exc)


def test_small_budget_streamed_graph_uses_disk_and_exact_cursor_identity(tmp_path):
    content = _content()
    grant, transition = _grant_and_transition()
    writer = _writer(tmp_path, grant, transition)
    binding = _write_content(writer, content, grant)
    scratch_bytes = writer._scratch_path.stat().st_size
    assert writer._db.execute("PRAGMA temp_store").fetchone()[0] == 1
    assert writer._db.execute("PRAGMA cache_size").fetchone()[0] == -256
    assert scratch_bytes > int(grant["resource_budgets"]["max_collect_bytes"])
    observation = writer.finish()
    expected = graph_identities(content, [binding])
    assert observation["content_identity"] == expected.content
    assert observation["binding_identity"] == expected.bindings
    assert observation["descriptor_identity"] == expected.descriptor
    assert writer._canonical_content_bytes > int(grant["resource_budgets"]["max_collect_bytes"])
    assert observation["component_observations"]["attribute_values"]["partitions"] == "24"
    assert not graph_parts._WRITER_RESERVATIONS
    with pytest.raises(OperationPackError, match="collection exceeds"):
        GraphPrimitive(content, [binding], max_collect_bytes=4096)
    output = tmp_path / "outputs" / "graph"
    assert sorted(path.name for path in output.iterdir()) == ["graph.parts.ndjson", "payload.json"]
    assert (
        audit_graph_stage(
            grant,
            tmp_path,
            observation,
            transition_identity=transition,
            input_kinds={"source": "graph"},
            output_names=["graph"],
        )
        == observation
    )

    parts = [json.loads(line) for line in (output / "graph.parts.ndjson").read_text().splitlines()]
    (tmp_path / "broker").mkdir()
    errors: list[BaseException] = []
    server = threading.Thread(target=_serve_parts, args=(tmp_path, parts, errors))
    server.start()
    try:
        client = GraphBrokerClient(
            _read_grant(
                observation, grant["authorized_source_axes"][0]["source_snapshot_identity"]
            ),
            tmp_path,
            transition_identity=transition,
            authorization_token=BROKER_TOKEN,
            timeout=10,
        )
        observed_parts = list(client)
        assert observed_parts == parts
        assert client.usage()["component_observations"] == observation["component_observations"]
        with pytest.raises(OperationPackError, match="single-use"):
            list(client)
    finally:
        server.join(timeout=10)
    assert not server.is_alive()
    assert not errors
    assert list((tmp_path / "broker").iterdir()) == []


def test_small_budget_late_staged_limit_removes_private_output_only(tmp_path):
    output_parent = tmp_path / "outputs"
    output_parent.mkdir()
    (output_parent / "keep").write_text("preserve", encoding="utf-8")
    grant, transition = _grant_and_transition(staged_bytes=65536)
    writer = _writer(tmp_path, grant, transition)
    with pytest.raises(OperationPackError) as error:
        _write_content(writer, _content(), grant)
    assert error.value.code == "PYBENCH_GRAPH_RESOURCE_LIMIT"
    assert sorted(path.name for path in output_parent.iterdir()) == ["keep"]


def test_small_budget_mixed_sdk_streams_graph_and_summary(tmp_path):
    content = _content()
    grant, transition = _grant_and_transition()
    request = _request()
    request["transition"]["transition_identity"] = transition
    request["inputs"] = [{"name": "source", "descriptor": {"kind": "graph"}}]
    request["graph_write_grants"] = [grant]
    writers = V5Writers(request, tmp_path, [], {})
    handle = writers.open_graph(
        "graph", {"schema": content["schema"], "direction_mode": content["direction_mode"]}
    )
    binding = _write_content(handle, content, grant, header=False)
    writers.write_terminal_primitive("summary", {"kind": "obs_table", "rows": [{"n": NODE_COUNT}]})
    observation = handle.finish()
    expected = graph_identities(content, [binding])
    assert observation["content_identity"] == expected.content
    assert observation["binding_identity"] == expected.bindings
    assert set(writers.writes) == {"graph", "summary"}
    assert writers.graph_write_observations() == [observation]
    assert not graph_parts._WRITER_RESERVATIONS


def test_tiny_working_allowance_rejects_before_connection_or_stage(tmp_path, monkeypatch):
    grant, transition = _grant_and_transition(memory_bytes=65536)
    calls = []
    real_connect = graph_parts.sqlite3.connect

    def track_connect(path):
        calls.append(path)
        return real_connect(path)

    monkeypatch.setattr(graph_parts.sqlite3, "connect", track_connect)
    with pytest.raises(OperationPackError, match="cannot admit cache and partition carriers"):
        _writer(tmp_path, grant, transition)
    assert calls == []
    assert not (tmp_path / "outputs").exists()
    assert not graph_parts._WRITER_RESERVATIONS


def test_live_writer_cache_reservations_aggregate_and_release(tmp_path):
    transition = _transition()
    names = ["graph0", "graph1", "graph2"]
    writers = []
    try:
        for name in names[:2]:
            writers.append(
                GraphPartsWriter(
                    _named_grant(name, transition),
                    tmp_path,
                    transition_identity=transition,
                    input_kinds={"source": "graph"},
                    output_names=names,
                )
            )
            assert writers[-1]._db.execute("PRAGMA cache_size").fetchone()[0] == -256
        with pytest.raises(OperationPackError, match="concurrent graph writers exceed"):
            GraphPartsWriter(
                _named_grant(names[2], transition),
                tmp_path,
                transition_identity=transition,
                input_kinds={"source": "graph"},
                output_names=names,
            )
        assert not (tmp_path / "outputs" / names[2]).exists()
        assert len(graph_parts._WRITER_RESERVATIONS) == 2
        writers[0].abort()
        writers.append(
            GraphPartsWriter(
                _named_grant(names[2], transition),
                tmp_path,
                transition_identity=transition,
                input_kinds={"source": "graph"},
                output_names=names,
            )
        )
        assert len(graph_parts._WRITER_RESERVATIONS) == 2
    finally:
        for writer in writers:
            writer.abort()
    assert not graph_parts._WRITER_RESERVATIONS
    assert list((tmp_path / "outputs").iterdir()) == []


def test_default_cache_and_failed_cache_pragma_release_reservation(tmp_path, monkeypatch):
    default_root = tmp_path / "default"
    default_root.mkdir()
    grant, transition = _grant_and_transition(memory_bytes=4 * 1024**2)
    writer = _writer(default_root, grant, transition)
    assert writer._db.execute("PRAGMA cache_size").fetchone()[0] == -1024
    writer.abort()

    real_connect = graph_parts.sqlite3.connect

    class WrongCacheConnection:
        def __init__(self, path):
            self.inner = real_connect(path)

        def execute(self, statement):
            if statement == "PRAGMA cache_size":
                return self.inner.execute("SELECT 0")
            return self.inner.execute(statement)

        def close(self):
            self.inner.close()

    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    monkeypatch.setattr(graph_parts.sqlite3, "connect", WrongCacheConnection)
    with pytest.raises(OperationPackError, match="cache differs from admitted allowance"):
        _writer(failed_root, grant, transition)
    assert not graph_parts._WRITER_RESERVATIONS
    assert not (failed_root / "outputs").exists()
