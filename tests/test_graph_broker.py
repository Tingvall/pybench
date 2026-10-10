from __future__ import annotations

import json
import shutil
import threading
import time

import pytest

from pybench.graph import identity
from pybench.graph_broker import GraphBrokerClient
from pybench.operation_packs import OperationPackError, canonical_json
from pybench.operation_packs_v2 import _identity
from test_graph_transport import _id, _transition

TOKEN = "a" * 64
COMPONENTS = (
    "header",
    "nodes",
    "edges",
    "incidences",
    "attribute_fields",
    "attribute_values",
    "bindings",
    "binding_mappings",
)


def _grant() -> dict:
    counts = {component: "0" for component in COMPONENTS}
    counts["header"] = "1"
    value = {
        "schema": "scworkbench.graph.read_grant.v1",
        "input_name": "source",
        "source_artifact_id": "art_graph_1",
        "source_snapshot_identity": _id("scworkbench.provenance.snapshot_head.v1"),
        "content_identity": _id("scworkbench.graph.content.canonical-json.v1"),
        "binding_identity": _id("scworkbench.graph.axis-bindings.canonical-json.v1"),
        "descriptor_identity": _id("scworkbench.graph.retained-descriptor.canonical-json.v1"),
        "component_counts": counts,
        "partition_limits": {"max_rows": "2", "max_decoded_bytes": "4096"},
        "selected_binding_ids": [],
    }
    value["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_read_grant.v1",
        value,
        "grant excluding grant_identity",
    )
    return value


def _part(*, wrong_offset=False) -> dict:
    value = {
        "schema": "scworkbench.graph.component_partition.v1",
        "component": "header",
        "first_ordinal": "1" if wrong_offset else "0",
        "row_count": "1",
        "rows": [{"schema": "scworkbench.graph.content.v1", "direction_mode": "undirected"}],
    }
    value["partition_identity"] = identity("scworkbench.graph.partition.v1", value)
    return value


def _serve(root, *, wrong_offset=False, stop_after_header=False):
    broker = root / "broker"
    for expected_component in ("header", "end"):
        request_path = broker / "request.json"
        deadline = time.monotonic() + 5
        while not request_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert request_path.exists()
        request = json.loads(request_path.read_text())
        assert request["component"] == expected_component
        response = {
            "schema": "scworkbench.graph.broker_read_response.v1",
            "partition": _part(wrong_offset=wrong_offset)
            if expected_component == "header"
            else None,
            "complete": expected_component == "end",
            "call_identity": request["call_identity"],
        }
        response["response_identity"] = _identity(
            "scworkbench.graph.broker_read_response.v1",
            response,
            "response excluding response_identity",
        )
        directory = broker / "response"
        directory.mkdir()
        (directory / "manifest.json").write_text(canonical_json(response))
        ack = broker / "acknowledgement.json"
        while not ack.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ack.exists()
        receipt = json.loads(ack.read_text())
        assert receipt["response_identity"] == response["response_identity"]
        shutil.rmtree(directory)
        ack.unlink()
        request_path.unlink()
        if wrong_offset or stop_after_header:
            return


def test_graph_broker_exact_header_then_end(tmp_path):
    (tmp_path / "broker").mkdir()
    server = threading.Thread(target=_serve, args=(tmp_path,))
    server.start()
    try:
        client = GraphBrokerClient(
            _grant(),
            tmp_path,
            transition_identity=_transition(),
            authorization_token=TOKEN,
            timeout=3,
        )
        parts = list(client)
        assert [part["component"] for part in parts] == ["header"]
        assert client.usage()["complete"] is True
        with pytest.raises(OperationPackError, match="single-use"):
            list(client)
    finally:
        server.join(timeout=5)
    assert not server.is_alive()


def test_self_rehashed_wrong_offset_rejects(tmp_path):
    (tmp_path / "broker").mkdir()
    server = threading.Thread(target=_serve, args=(tmp_path,), kwargs={"wrong_offset": True})
    server.start()
    try:
        client = GraphBrokerClient(
            _grant(),
            tmp_path,
            transition_identity=_transition(),
            authorization_token=TOKEN,
            timeout=3,
        )
        with pytest.raises(OperationPackError, match="out of order"):
            list(client)
        with pytest.raises(OperationPackError, match="not completely consumed"):
            client.usage()
    finally:
        server.join(timeout=5)
    assert not server.is_alive()


def test_self_rehashed_partition_over_decoded_budget_rejects(tmp_path):
    (tmp_path / "broker").mkdir()
    grant = _grant()
    grant["partition_limits"]["max_decoded_bytes"] = "100"
    server = threading.Thread(target=_serve, args=(tmp_path,), kwargs={"stop_after_header": True})
    server.start()
    try:
        client = GraphBrokerClient(
            grant,
            tmp_path,
            transition_identity=_transition(),
            authorization_token=TOKEN,
            timeout=3,
        )
        with pytest.raises(OperationPackError, match="stale, malformed, or out of order"):
            list(client)
        with pytest.raises(OperationPackError, match="not completely consumed"):
            client.usage()
    finally:
        server.join(timeout=5)
    assert not server.is_alive()
