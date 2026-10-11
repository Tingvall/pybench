from __future__ import annotations

import copy
import importlib.metadata as metadata
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pybench.graph import GraphPrimitive, graph_binding_from_source, identity
from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v4 import (
    activate_operation_pack_v4,
    active_operation_packs_v4,
    discover_operation_pack_v4,
    manifest_identities_v4,
    operation_pack_handlers_v4,
    unload_operation_pack_v4,
    validate_manifest_v4,
)
from pybench.protocol_v5 import (
    execute_operation_pack_v5,
    validate_python_runtime_sidecar_v5,
    validate_receipt_v5,
    validate_request_v5,
)
from test_graph_broker import TOKEN, _serve
from test_graph_protocol import _manifest
from test_graph_transport import _axis, _id, _retained_axis
from v3_helpers import fixture_inputs
from v4_graph_helpers import graph_source_request, matrix_to_graph_request


class _Entry:
    group = "scworkbench.operation_packs.v4"
    name = "fixture"
    value = "scwbgraphfixture.pack:register"

    def __init__(self, callback):
        self.callback = callback
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.callback


class _Distribution:
    version = "0.1.0"
    metadata = {"Name": "scwb-graph-fixture"}

    def __init__(self, root: Path, entry: _Entry):
        self.root = root
        self.files = [metadata.PackagePath("scwbgraphfixture/operation-pack.json")]
        self.entry_points = [entry]

    def locate_file(self, path):
        return self.root / path


def _install_fake(tmp_path: Path, monkeypatch):
    manifest = _manifest()
    path = tmp_path / "scwbgraphfixture" / "operation-pack.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    calls = []

    def handler(readers, writers, params, context):
        calls.append((readers, writers, params, context))

    def register():
        return operation_pack_handlers_v4(
            {item["qualified_name"]: handler for item in manifest["operations"]}
        )

    entry = _Entry(register)
    distribution = _Distribution(tmp_path, entry)
    monkeypatch.setattr("pybench.operation_packs_v4.metadata.distribution", lambda _: distribution)
    return manifest, path, entry, distribution, calls


def test_explicit_installed_discovery_and_activation_are_inert_until_called(tmp_path, monkeypatch):
    manifest, path, entry, _, calls = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    assert entry.loads == 0
    assert calls == []
    assert (
        discovery.identity["manifest_identity"]
        == manifest_identities_v4(manifest)["manifest_identity"]
    )
    assert discovery.identity["pack_identity"]["scheme"] == "scworkbench.operation_pack.pack.v4"
    try:
        activate_operation_pack_v4(discovery)
        assert entry.loads == 1
        assert manifest["namespace"] in active_operation_packs_v4()
        with pytest.raises(OperationPackError, match="NAMESPACE_COLLISION"):
            activate_operation_pack_v4(discovery)
    finally:
        unload_operation_pack_v4(manifest["namespace"])


def test_tampered_installed_manifest_rejects_before_entry_load(tmp_path, monkeypatch):
    _, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    tampered = json.loads(path.read_text())
    tampered["operations"][0]["description"] = "substituted"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(OperationPackError, match="IDENTITY_MISMATCH"):
        activate_operation_pack_v4(discovery)
    assert entry.loads == 0


def test_duplicate_entry_point_rejects_before_activation(tmp_path, monkeypatch):
    _, path, entry, distribution, _ = _install_fake(tmp_path, monkeypatch)
    distribution.entry_points.append(_Entry(entry.callback))
    with pytest.raises(OperationPackError, match="ENTRY_POINT_AMBIGUOUS"):
        discover_operation_pack_v4(path)
    assert entry.loads == 0


def test_graph_replay_identity_substitution_rejects():
    manifest = _manifest()
    bad = copy.deepcopy(manifest)
    bad["operations"][0]["replay_equivalence"]["result"] = "exact_canonical_value"
    with pytest.raises(OperationPackError, match="REPLAY_EQUIVALENCE_INVALID"):
        validate_manifest_v4(bad)


def test_manifest_identity_independent_process(tmp_path):
    path = tmp_path / "operation-pack.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    expected = manifest_identities_v4(_manifest())["manifest_sha256"]
    script = (
        "import sys; from pybench.operation_packs_v4 import manifest_identities_v4; "
        "print(manifest_identities_v4(sys.argv[1])['manifest_sha256'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == expected


def test_full_matrix_to_graph_process_v5_request_admits(tmp_path, monkeypatch):
    _, path, _, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)
    assert (
        validate_request_v5(request, discovery)["graph_write_grants"][0]["output_name"] == "result"
    )


def test_process_v5_matrix_to_graph_uses_existing_executor(tmp_path, monkeypatch):
    manifest, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)

    def handler(readers, writers, params, context):
        assert readers["matrix"]()["values"] == [[1.0]]
        graph = GraphPrimitive(
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
                "nodes": [],
                "edges": [],
                "incidences": [],
                "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
            }
        )
        writers.write_graph("result", graph)

    entry.callback = lambda: operation_pack_handlers_v4(
        {item["qualified_name"]: handler for item in manifest["operations"]}
    )
    try:
        activate_operation_pack_v4(discovery)
        receipt, runtime = execute_operation_pack_v5(request, discovery, stage)
        assert receipt["status"] == "ok"
        assert len(receipt["graph_write_observations"]) == 1
        assert validate_receipt_v5(receipt, request, discovery, stage)["status"] == "ok"
        validate_python_runtime_sidecar_v5(runtime, request, discovery)
        assert (stage / "outputs" / "result" / "graph.parts.ndjson").is_file()
    finally:
        unload_operation_pack_v4(manifest["namespace"])


@pytest.mark.parametrize("input_kind", ["cell_vector", "cell_vector_family"])
def test_process_v5_declared_retained_cell_axis_binding(tmp_path, monkeypatch, input_kind):
    manifest, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    operation = manifest["operations"][0]
    operation["inputs"] = [
        {
            "name": "labels",
            "kind": input_kind,
            "required": True,
            "alignment": "cell_ordered",
        }
    ]
    output = operation["outputs"][0]
    output["binding_policy"] = "required"
    output["axis_sources"] = [
        {
            "input_name": "labels",
            "axis_kind": "cell",
            "selection_mode": "retained_input_axis",
            "source_binding_selector": None,
        }
    ]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)
    axis = _retained_axis(request["transition"]["transition_identity"], output_name="result")
    axis["input_name"] = "labels"
    axis["source_binding_id"] = None
    axis["axis_count"] = "1"
    axis.pop("authority_identity")
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        axis,
        "authority excluding authority_identity",
    )
    grant = request["graph_write_grants"][0]
    grant["binding_policy"] = "required"
    grant["authorized_source_axes"] = [axis]
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    descriptor = request["outputs"][0]["descriptor"]
    descriptor["binding_policy"] = "required"
    descriptor["graph_write_grant_identity"] = grant["grant_identity"]
    descriptor.pop("grant_identity")
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v4",
        descriptor,
        "descriptor excluding grant_identity",
    )
    capability = request["execution_capability"]
    capability["writer_grants"][0]["grant_identity"] = descriptor["grant_identity"]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v4",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v4",
        request,
        "request excluding request_identity",
    )
    validate_request_v5(request, discovery)

    def handler(readers, writers, params, context):
        assert readers["labels"]()["kind"] == input_kind
        content = {
            "schema": "scworkbench.graph.content.v1",
            "direction_mode": "undirected",
            "nodes": [{"node_id": "n0"}],
            "edges": [],
            "incidences": [],
            "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
        }
        public_axis = context["graph_source_axes"]["result"][0]
        assert "authority_identity" not in public_axis
        assert "source_payload_closure_identity" not in public_axis
        binding = graph_binding_from_source(
            public_axis,
            "b0",
            [{"node_id": "n0", "axis_id": "cell_1"}],
        )
        writers.write_graph("result", GraphPrimitive(content, [binding]))

    entry.callback = lambda: operation_pack_handlers_v4(
        {item["qualified_name"]: handler for item in manifest["operations"]}
    )
    try:
        activate_operation_pack_v4(discovery)
        receipt, runtime = execute_operation_pack_v5(request, discovery, stage)
        assert receipt["status"] == "ok"
        assert receipt["graph_write_observations"][0]["component_counts"]["bindings"] == "1"
        validate_receipt_v5(receipt, request, discovery, stage)
        validate_python_runtime_sidecar_v5(runtime, request, discovery)
    finally:
        unload_operation_pack_v4(manifest["namespace"])


def test_process_v5_declared_retained_matrix_axes_binding(tmp_path, monkeypatch):
    manifest, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    output = manifest["operations"][0]["outputs"][0]
    output["binding_policy"] = "required"
    output["axis_sources"] = [
        {
            "input_name": "matrix",
            "axis_kind": axis_kind,
            "selection_mode": "retained_matrix_axis",
            "source_binding_selector": None,
        }
        for axis_kind in ("cell", "feature")
    ]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)
    transition = request["transition"]["transition_identity"]
    cell_axis = _axis(transition, output_name="result")
    feature_axis = copy.deepcopy(cell_axis)
    feature_axis["axis_kind"] = "feature"
    feature_axis["axis_count"] = "1"
    feature_axis["axis_order_identity"] = _id("scworkbench.feature_order.v1")
    feature_axis.pop("authority_identity")
    feature_axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        feature_axis,
        "authority excluding authority_identity",
    )
    grant = request["graph_write_grants"][0]
    grant["binding_policy"] = "required"
    grant["authorized_source_axes"] = [cell_axis, feature_axis]
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    descriptor = request["outputs"][0]["descriptor"]
    descriptor["binding_policy"] = "required"
    descriptor["graph_write_grant_identity"] = grant["grant_identity"]
    descriptor.pop("grant_identity")
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v4",
        descriptor,
        "descriptor excluding grant_identity",
    )
    capability = request["execution_capability"]
    capability["writer_grants"][0]["grant_identity"] = descriptor["grant_identity"]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v4",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v4",
        request,
        "request excluding request_identity",
    )
    validate_request_v5(request, discovery)

    def handler(readers, writers, params, context):
        assert readers["matrix"]()["values"] == [[1.0]]
        sources = {axis["axis_kind"]: axis for axis in context["graph_source_axes"]["result"]}
        assert set(sources) == {"cell", "feature"}
        assert all("source_payload_closure_identity" not in axis for axis in sources.values())
        bindings = [
            graph_binding_from_source(
                sources["cell"], "cell_source", [{"node_id": "n_cell", "axis_id": "cell_1"}]
            ),
            graph_binding_from_source(
                sources["feature"],
                "feature_source",
                [{"node_id": "n_feature", "axis_id": "feature_1"}],
            ),
        ]
        content = {
            "schema": "scworkbench.graph.content.v1",
            "direction_mode": "undirected",
            "nodes": [{"node_id": "n_cell"}, {"node_id": "n_feature"}],
            "edges": [],
            "incidences": [],
            "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
        }
        writers.write_graph("result", GraphPrimitive(content, bindings))

    entry.callback = lambda: operation_pack_handlers_v4(
        {item["qualified_name"]: handler for item in manifest["operations"]}
    )
    try:
        activate_operation_pack_v4(discovery)
        receipt, runtime = execute_operation_pack_v5(request, discovery, stage)
        assert receipt["status"] == "ok"
        assert receipt["graph_write_observations"][0]["component_counts"]["bindings"] == "2"
        validate_receipt_v5(receipt, request, discovery, stage)
        validate_python_runtime_sidecar_v5(runtime, request, discovery)
    finally:
        unload_operation_pack_v4(manifest["namespace"])


@pytest.mark.parametrize("operation_index", [1, 2])
def test_full_graph_source_process_v5_request_admits(tmp_path, monkeypatch, operation_index):
    _, path, _, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = graph_source_request(discovery, stage, operation_index=operation_index)
    assert validate_request_v5(request, discovery)["graph_read_grants"][0]["input_name"] == "source"


@pytest.mark.parametrize("operation_index", [1, 2])
def test_process_v5_graph_source_mixed_closure(tmp_path, monkeypatch, operation_index):
    manifest, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = graph_source_request(discovery, stage, operation_index=operation_index)
    monkeypatch.setenv("PYBENCH_OPERATION_PACK_BROKER_TOKEN", TOKEN)

    def handler(readers, writers, params, context):
        parts = list(readers.read_graph_parts("source"))
        assert [part["component"] for part in parts] == ["header"]
        if operation_index == 1:
            assert readers["metadata"]()["kind"] == "obs_table"
            writers.write_graph(
                "result",
                GraphPrimitive(
                    {
                        "schema": "scworkbench.graph.content.v1",
                        "direction_mode": "undirected",
                        "nodes": [],
                        "edges": [],
                        "incidences": [],
                        "attributes": {
                            scope: [] for scope in ("graph", "node", "edge", "incidence")
                        },
                    }
                ),
            )
        writers.write_terminal_primitive("summary", fixture_inputs()["obs_table"])

    entry.callback = lambda: operation_pack_handlers_v4(
        {item["qualified_name"]: handler for item in manifest["operations"]}
    )
    server = threading.Thread(target=_serve, args=(stage,))
    server.start()
    try:
        activate_operation_pack_v4(discovery)
        receipt, runtime = execute_operation_pack_v5(request, discovery, stage)
        assert receipt["status"] == "ok"
        assert len(receipt["graph_read_usage"]) == 1
        assert len(receipt["outputs"]) == (2 if operation_index == 1 else 1)
        assert validate_receipt_v5(receipt, request, discovery, stage)["status"] == "ok"
        validate_python_runtime_sidecar_v5(runtime, request, discovery)
    finally:
        server.join(timeout=5)
        unload_operation_pack_v4(manifest["namespace"])
    assert not server.is_alive()


@pytest.mark.parametrize(
    "failure", ["handler_exception", "sibling_mutation", "self_rehashed_parts"]
)
def test_process_v5_graph_failure_restores_private_stage(tmp_path, monkeypatch, failure):
    manifest, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)
    before = (stage / "inputs" / "matrix" / "payload.json").read_bytes()

    def handler(readers, writers, params, context):
        writers.write_graph(
            "result",
            GraphPrimitive(
                {
                    "schema": "scworkbench.graph.content.v1",
                    "direction_mode": "undirected",
                    "nodes": [],
                    "edges": [],
                    "incidences": [],
                    "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
                }
            ),
        )
        if failure == "handler_exception":
            raise RuntimeError("intentional handler failure")

    def inject(event):
        if event != "after_handler":
            return
        if failure == "sibling_mutation":
            sibling = stage / "outputs" / "sibling"
            sibling.mkdir()
            (sibling / "payload.json").write_text("{}")
        elif failure == "self_rehashed_parts":
            parts = stage / "outputs" / "result" / "graph.parts.ndjson"
            altered = json.loads(parts.read_text().splitlines()[0])
            altered["rows"][0]["direction_mode"] = "directed"
            altered["partition_identity"] = identity(
                "scworkbench.graph.partition.v1",
                {key: value for key, value in altered.items() if key != "partition_identity"},
            )
            parts.write_text(json.dumps(altered, sort_keys=True, separators=(",", ":")) + "\n")

    entry.callback = lambda: operation_pack_handlers_v4(
        {item["qualified_name"]: handler for item in manifest["operations"]}
    )
    try:
        activate_operation_pack_v4(discovery)
        with pytest.raises((RuntimeError, OperationPackError)):
            execute_operation_pack_v5(request, discovery, stage, fault_hook=inject)
        assert not (stage / "outputs").exists()
        assert (stage / "inputs" / "matrix" / "payload.json").read_bytes() == before
    finally:
        unload_operation_pack_v4(manifest["namespace"])


def test_self_rehashed_wrong_session_capability_rejects_before_dispatch(tmp_path, monkeypatch):
    _, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = matrix_to_graph_request(discovery, stage)
    request["execution_capability"]["session_id"] = "foreign_session"
    capability = request["execution_capability"]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v4",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v4",
        request,
        "request excluding request_identity",
    )
    with pytest.raises(OperationPackError, match="Capability bindings differ"):
        validate_request_v5(request, discovery)
    assert entry.loads == 0
    assert not (stage / "outputs").exists()


def test_self_rehashed_unselected_graph_source_binding_rejects(tmp_path, monkeypatch):
    _, path, entry, _, _ = _install_fake(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v4(path)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = graph_source_request(discovery, stage, operation_index=1)
    axis = _axis(
        request["transition"]["transition_identity"], input_name="source", output_name="result"
    )
    axis["source_binding_id"] = "binding_1"
    axis["source_ref"] = {"kind": "retained_cell_axis", "source_id": "cell_axis_1"}
    axis["source_payload_closure_identity"] = _id(
        "scworkbench.artifact.ordered-object-payload.canonical-json.v1"
    )
    axis.pop("authority_identity")
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        axis,
        "authority excluding authority_identity",
    )
    grant = request["graph_write_grants"][0]
    grant["authorized_source_axes"] = [axis]
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    descriptor = request["outputs"][0]["descriptor"]
    descriptor["graph_write_grant_identity"] = grant["grant_identity"]
    descriptor.pop("grant_identity")
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v4",
        descriptor,
        "descriptor excluding grant_identity",
    )
    capability = request["execution_capability"]
    capability["writer_grants"][0]["grant_identity"] = descriptor["grant_identity"]
    capability.pop("capability_identity")
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v4",
        capability,
        "capability excluding capability_identity",
    )
    request.pop("request_identity")
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v4",
        request,
        "request excluding request_identity",
    )
    with pytest.raises(OperationPackError, match="not selected by its read grant"):
        validate_request_v5(request, discovery)
    assert entry.loads == 0
    assert not (stage / "outputs").exists()
