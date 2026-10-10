from __future__ import annotations

import copy
import shutil
from pathlib import Path
from types import SimpleNamespace

from pybench.graph import graph_identities
from pybench.operation_packs_v2 import _identity
from test_graph_protocol import _budgets, _obs_output
from test_graph_transport import _retained_axis
from v3_helpers import fixture_inputs, make_request


def matrix_to_graph_request(discovery, stage: Path) -> dict:
    synthetic_manifest = copy.deepcopy(discovery.manifest)
    operation = synthetic_manifest["operations"][0]
    operation["outputs"] = [_obs_output("result")]
    operation["replay_equivalence"] = {"result": "exact_canonical_value"}
    helper_discovery = SimpleNamespace(
        manifest=synthetic_manifest,
        identity=discovery.identity,
        manifest_source_identity=discovery.manifest_source_identity,
        distribution_identity=discovery.distribution_identity,
        entry_point_identity=discovery.entry_point_identity,
    )
    inputs = {
        declaration["name"]: fixture_inputs()[declaration["kind"]]
        for declaration in operation["inputs"]
    }
    request = make_request(helper_discovery, stage, "scwbgraphfixture::matrix_to_graph", inputs)
    grant = {
        "schema": "scworkbench.graph.write_grant.v1",
        "output_name": "result",
        "component_order": "graph_components_v1",
        "binding_policy": "optional",
        "authorized_source_axes": [],
        "resource_budgets": _budgets(),
    }
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    descriptor = {
        "category": "terminal_artifact",
        "kind": "graph",
        "storage_profile": "graph_sqlite_v1",
        "writer": "write_graph",
        "binding_policy": "optional",
        "resource_budgets": _budgets(),
        "graph_write_grant_identity": grant["grant_identity"],
    }
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v4",
        descriptor,
        "descriptor excluding grant_identity",
    )
    request["outputs"][0]["descriptor"] = descriptor
    request["schema"] = "scworkbench.operation_pack.transition_request.v4"
    request["process_protocol_version"] = "pybench-op-v5"
    request["pack_abi_version"] = "4"
    request["graph_read_grants"] = []
    request["graph_write_grants"] = [grant]
    capability = request["execution_capability"]
    capability["writer_grants"] = [
        {
            "output_name": "result",
            "writer": "write_graph",
            "grant_identity": descriptor["grant_identity"],
        }
    ]
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
    return request


def graph_source_request(discovery, stage: Path, *, operation_index: int) -> dict:
    if operation_index not in (1, 2, 3):
        raise ValueError("graph source fixture index must be 1, 2 or 3")
    manifest = copy.deepcopy(discovery.manifest)
    operation = manifest["operations"][operation_index]
    operation["inputs"][0] = {
        "name": "source",
        "kind": "obs_table",
        "required": True,
        "alignment": "none",
    }
    if operation_index in (1, 3):
        operation["outputs"][0] = _obs_output("result", promotion_group="result")
        operation["replay_equivalence"]["result"] = "exact_canonical_value"
    helper_discovery = SimpleNamespace(
        manifest=manifest,
        identity=discovery.identity,
        manifest_source_identity=discovery.manifest_source_identity,
        distribution_identity=discovery.distribution_identity,
        entry_point_identity=discovery.entry_point_identity,
    )
    sources = fixture_inputs()
    inputs = {"source": sources["obs_table"]}
    if operation_index == 1:
        inputs["metadata"] = sources["obs_table"]
    elif operation_index == 3:
        inputs["labels"] = sources["cell_vector"]
    request = make_request(helper_discovery, stage, operation["qualified_name"], inputs)
    shutil.rmtree(stage / "inputs" / "source")
    content = {
        "schema": "scworkbench.graph.content.v1",
        "direction_mode": "undirected",
        "nodes": [],
        "edges": [],
        "incidences": [],
        "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
    }
    ids = graph_identities(content)
    counts = {
        component: "0"
        for component in (
            "header",
            "nodes",
            "edges",
            "incidences",
            "attribute_fields",
            "attribute_values",
            "bindings",
            "binding_mappings",
        )
    }
    counts["header"] = "1"
    snapshot_id = {
        "scheme": "scworkbench.provenance.snapshot_head.v1",
        "sha256": "a" * 64,
    }
    read_grant = {
        "schema": "scworkbench.graph.read_grant.v1",
        "input_name": "source",
        "source_artifact_id": "art_graph_source",
        "source_snapshot_identity": snapshot_id,
        "content_identity": ids.content,
        "binding_identity": ids.bindings,
        "descriptor_identity": ids.descriptor,
        "component_counts": counts,
        "partition_limits": {"max_rows": "10", "max_decoded_bytes": "4096"},
        "selected_binding_ids": [],
    }
    read_grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_read_grant.v1",
        read_grant,
        "grant excluding grant_identity",
    )
    input_descriptor = {
        "kind": "graph",
        "storage_profile": "graph_sqlite_v1",
        "reader": "read_graph_parts",
        "alignment": "none",
        "content_identity": ids.content,
        "binding_identity": ids.bindings,
        "retained_descriptor_identity": ids.descriptor,
        "source_snapshot_identity": snapshot_id,
        "component_counts": counts,
        "graph_read_grant_identity": read_grant["grant_identity"],
    }
    input_descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.graph_input_descriptor.v1",
        input_descriptor,
        "descriptor excluding descriptor_identity",
    )
    request["inputs"][0] = {
        "name": "source",
        "reference": "graph:art_graph_source",
        "descriptor": input_descriptor,
    }
    request["schema"] = "scworkbench.operation_pack.transition_request.v4"
    request["process_protocol_version"] = "pybench-op-v5"
    request["pack_abi_version"] = "4"
    request["graph_read_grants"] = [read_grant]
    request["graph_write_grants"] = []
    if operation_index in (1, 3):
        authorized_axes = []
        if operation_index == 3:
            axis = _retained_axis(
                request["transition"]["transition_identity"], output_name="result"
            )
            axis["input_name"] = "labels"
            axis["source_binding_id"] = None
            axis["axis_count"] = "1"
            axis.pop("authority_identity")
            axis["authority_identity"] = _identity(
                "scworkbench.operation_pack.graph_source_axis_authority.v1",
                axis,
                "authority excluding authority_identity",
            )
            authorized_axes = [axis]
        binding_policy = "required" if operation_index == 3 else "optional"
        graph_write = {
            "schema": "scworkbench.graph.write_grant.v1",
            "output_name": "result",
            "component_order": "graph_components_v1",
            "binding_policy": binding_policy,
            "authorized_source_axes": authorized_axes,
            "resource_budgets": _budgets(),
        }
        graph_write["grant_identity"] = _identity(
            "scworkbench.operation_pack.graph_write_grant.v1",
            graph_write,
            "grant excluding grant_identity",
        )
        descriptor = {
            "category": "terminal_artifact",
            "kind": "graph",
            "storage_profile": "graph_sqlite_v1",
            "writer": "write_graph",
            "binding_policy": binding_policy,
            "resource_budgets": _budgets(),
            "graph_write_grant_identity": graph_write["grant_identity"],
        }
        descriptor["grant_identity"] = _identity(
            "scworkbench.operation_pack.output_grant.v4",
            descriptor,
            "descriptor excluding grant_identity",
        )
        request["outputs"][0]["descriptor"] = descriptor
        request["graph_write_grants"] = [graph_write]
    for output in request["outputs"]:
        if output["descriptor"]["kind"] != "graph":
            descriptor = output["descriptor"]
            descriptor.pop("grant_identity")
            descriptor["grant_identity"] = _identity(
                "scworkbench.operation_pack.output_grant.v3",
                descriptor,
                "descriptor excluding grant_identity",
            )
    capability = request["execution_capability"]
    capability["reader_grants"] = [
        {
            "input_name": "source",
            "reader": "read_graph_parts",
            "grant_identity": read_grant["grant_identity"],
        }
    ] + [
        {
            "input_name": item["name"],
            "reader": item["descriptor"]["reader"],
            "descriptor_identity": item["descriptor"]["descriptor_identity"],
        }
        for item in request["inputs"][1:]
    ]
    capability["writer_grants"] = [
        {
            "output_name": item["name"],
            "writer": item["descriptor"]["writer"],
            "grant_identity": item["descriptor"]["grant_identity"],
        }
        for item in request["outputs"]
    ]
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
    (stage / "broker").mkdir()
    return request
