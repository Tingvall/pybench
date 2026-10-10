from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity as protocol_identity
from pybench.operation_packs_v4 import validate_manifest_v4

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocol"
SCHEMAS = (
    "graph-v1.schema.json",
    "operation-pack-v4.schema.json",
    "operation-pack-transition-request-v4.schema.json",
    "operation-pack-transition-receipt-v4.schema.json",
    "pybench-op-v5-python-runtime.schema.json",
)
MANIFEST_SCHEMA = json.loads((PROTOCOL / "operation-pack-v4.schema.json").read_text())
REQUEST_SCHEMA = json.loads(
    (PROTOCOL / "operation-pack-transition-request-v4.schema.json").read_text()
)
RECEIPT_SCHEMA = json.loads(
    (PROTOCOL / "operation-pack-transition-receipt-v4.schema.json").read_text()
)
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


def _definition(schema, name):
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{name}"})


def _identity(scheme, scope=None):
    value = {"scheme": scheme, "sha256": "0" * 64}
    if scope is not None:
        value["scope"] = scope
    return value


def _budgets():
    return {
        "max_nodes": "1000000",
        "max_edges": "1000000",
        "max_incidences": "8000000",
        "max_attribute_fields": "100000",
        "max_staged_bytes": "1073741824",
        "max_published_bytes": "1073741824",
        "max_wal_bytes": "1073741824",
        "max_spill_bytes": "1073741824",
        "max_memory_bytes": "67108864",
        "max_collect_bytes": "16777216",
        "partition_limits": {"max_rows": "4096", "max_decoded_bytes": "1048576"},
    }


def _graph_input(name="source"):
    return {
        "name": name,
        "kind": "graph",
        "required": True,
        "alignment": "none",
        "storage_profile": "graph_sqlite_v1",
        "read_mode": "bounded_partitions_v1",
        "binding_policy": "optional",
    }


def _graph_output(name="result"):
    return {
        "name": name,
        "category": "terminal_artifact",
        "kind": "graph",
        "storage_profile": "graph_sqlite_v1",
        "allocated_id_key": f"{name}_id",
        "required": True,
        "promotion_group": name,
        "binding_policy": "optional",
        "axis_sources": [],
        "resource_budgets": _budgets(),
    }


def _obs_output(name="summary", promotion_group=None):
    return {
        "name": name,
        "category": "terminal_artifact",
        "kind": "obs_table",
        "storage_profile": "terminal_primitive_v1",
        "allocated_id_key": f"{name}_id",
        "required": True,
        "promotion_group": promotion_group or name,
        "table_type": "graph_summary",
    }


def _operation(name, inputs, outputs):
    output_names = [item["name"] for item in outputs]
    return {
        "name": name,
        "qualified_name": f"scwbgraphfixture::{name}",
        "title": name,
        "description": "Generic graph ABI4 mixed-primitive contract fixture.",
        "runner": f".{name}",
        "inputs": inputs,
        "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputs": outputs,
        "dependencies": [],
        "promotion_closure": {item: output_names for item in output_names},
        "replay_equivalence": {
            item["name"]: (
                "exact_graph_descriptor" if item["kind"] == "graph" else "exact_canonical_value"
            )
            for item in outputs
        },
    }


def _manifest():
    manifest = {
        "schema_version": "scworkbench.operation_pack.v4",
        "pack_abi_version": "4",
        "identity_scheme": "scworkbench.operation_pack.canonical_json.v1",
        "namespace": "scwbgraphfixture",
        "package": {"name": "scwb-graph-fixture", "version": "0.1.0"},
        "backend": "Python",
        "compatibility": {
            "scworkbench": {"minimum": "0.1.0", "maximum_exclusive": "0.2.0"},
            "pack_abi": {"minimum": "4", "maximum": "4"},
            "process_protocol": {"minimum": "pybench-op-v5", "maximum": "pybench-op-v5"},
        },
        "entry_point": "scworkbench.operation_packs.v4:fixture=scwbgraphfixture.pack:register",
        "operations": [
            _operation(
                "matrix_to_graph",
                [
                    {
                        "name": "matrix",
                        "kind": "matrix",
                        "required": True,
                        "alignment": "cell_and_feature_ordered",
                    }
                ],
                [_graph_output()],
            ),
            _operation(
                "graph_and_metadata",
                [
                    _graph_input(),
                    {
                        "name": "metadata",
                        "kind": "obs_table",
                        "required": True,
                        "alignment": "none",
                    },
                ],
                [_graph_output(), _obs_output(promotion_group="result")],
            ),
            _operation("graph_to_table", [_graph_input()], [_obs_output()]),
        ],
    }
    manifest["operations"][1]["outputs"][0]["axis_sources"] = [
        {
            "input_name": "source",
            "axis_kind": "cell",
            "selection_mode": "selected_graph_binding",
            "source_binding_selector": {"kind": "fixed_binding_id_v1", "binding_id": "binding_1"},
        }
    ]
    return manifest


@pytest.mark.parametrize("name", SCHEMAS)
def test_shared_graph_schema_is_valid_and_generated(name):
    Draft202012Validator.check_schema(json.loads((PROTOCOL / name).read_text()))
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_graph_protocol.py"), "--check"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_mixed_primitive_manifest_declarations_are_closed():
    fixture = _manifest()
    Draft202012Validator(MANIFEST_SCHEMA).validate(fixture)
    assert {item["name"] for item in fixture["operations"]} == {
        "matrix_to_graph",
        "graph_and_metadata",
        "graph_to_table",
    }
    bad = copy.deepcopy(fixture)
    bad["operations"][0]["outputs"][0]["resource_budgets"]["max_nodes"] = -1
    assert not Draft202012Validator(MANIFEST_SCHEMA).is_valid(bad)
    bad = copy.deepcopy(fixture)
    bad["operations"][1]["inputs"][0]["alignment"] = "cell_ordered"
    assert not Draft202012Validator(MANIFEST_SCHEMA).is_valid(bad)
    bad = copy.deepcopy(fixture)
    bad["operations"][0]["outputs"][0]["host_path"] = "/tmp/forbidden"
    assert not Draft202012Validator(MANIFEST_SCHEMA).is_valid(bad)


@pytest.mark.parametrize("kind", ["cell_vector", "cell_vector_family"])
def test_retained_cell_axis_source_is_explicit_manifest_declaration(kind):
    manifest = _manifest()
    operation = manifest["operations"][0]
    operation["inputs"] = [
        {
            "name": "labels",
            "kind": kind,
            "required": True,
            "alignment": "cell_ordered",
        }
    ]
    operation["outputs"][0]["axis_sources"] = [
        {
            "input_name": "labels",
            "axis_kind": "cell",
            "selection_mode": "retained_input_axis",
            "source_binding_selector": None,
        }
    ]
    assert validate_manifest_v4(manifest)["operations"][0]["outputs"][0]["axis_sources"]


def test_graph_binding_selector_must_be_required_nonempty_string():
    manifest = _manifest()
    operation = manifest["operations"][1]
    operation["params_schema"] = {
        "type": "object",
        "properties": {"binding_name": {"type": "string", "minLength": 1}},
        "required": ["binding_name"],
        "additionalProperties": False,
    }
    selector = operation["outputs"][0]["axis_sources"][0]["source_binding_selector"]
    selector.clear()
    selector.update(kind="parameter_binding_id_v1", parameter="binding_name")
    validate_manifest_v4(manifest)
    operation["params_schema"]["required"] = []
    with pytest.raises(OperationPackError, match="required nonempty string"):
        validate_manifest_v4(manifest)


def test_fixed_graph_binding_selector_rejects_nul_without_regex():
    manifest = _manifest()
    selector = manifest["operations"][1]["outputs"][0]["axis_sources"][0]["source_binding_selector"]
    selector["binding_id"] = "binding\x00one"
    with pytest.raises(OperationPackError, match="Fixed graph binding ID is invalid"):
        validate_manifest_v4(manifest)


def test_graph_axis_source_rejects_undeclared_input_and_forbidden_policy():
    manifest = _manifest()
    source = manifest["operations"][1]["outputs"][0]["axis_sources"][0]
    source["input_name"] = "missing"
    with pytest.raises(OperationPackError, match="undeclared input"):
        validate_manifest_v4(manifest)
    source["input_name"] = "source"
    manifest["operations"][1]["outputs"][0]["binding_policy"] = "forbidden"
    with pytest.raises(OperationPackError, match="Unbound graph"):
        validate_manifest_v4(manifest)


def test_old_abi3_schema_remains_closed_to_graph():
    old = json.loads((PROTOCOL / "operation-pack-v3.schema.json").read_text())
    assert "graph" not in old["$defs"]["primitive_kind"]["enum"]
    assert "graph" in MANIFEST_SCHEMA["$defs"]["primitive_kind"]["enum"]


def test_draft_mixed_interop_fixture_is_closed_and_self_hashed():
    fixture = json.loads((PROTOCOL / "fixtures/graph-v1/draft-abi4-mixed-interop.json").read_text())
    assert fixture["status"] == "draft"
    manifest, request, receipt = (fixture["manifest"], fixture["request"], fixture["receipt"])
    Draft202012Validator(MANIFEST_SCHEMA).validate(manifest)
    Draft202012Validator(REQUEST_SCHEMA).validate(request)
    Draft202012Validator(RECEIPT_SCHEMA).validate(receipt)
    assert request["request_identity"] == protocol_identity(
        "scworkbench.operation_pack.transition_request.v4",
        {key: value for key, value in request.items() if key != "request_identity"},
        "request excluding request_identity",
    )
    assert receipt["receipt_identity"] == protocol_identity(
        "scworkbench.operation_pack.transition_receipt.v4",
        {key: value for key, value in receipt.items() if key != "receipt_identity"},
        "receipt excluding receipt_identity",
    )
    assert receipt["status"] == "ok"
    assert [item["name"] for item in receipt["outputs"]] == ["result", "summary"]
    assert receipt["graph_read_usage"][0]["complete"] is True
    assert receipt["graph_write_observations"][0]["complete"] is True


def test_graph_grants_and_compact_receipt_observations_are_closed():
    counts = {name: "0" for name in COMPONENTS}
    counts.update(header="1", nodes="1", edges="1", incidences="2")
    read_grant = {
        "schema": "scworkbench.graph.read_grant.v1",
        "input_name": "source",
        "source_artifact_id": "art_graph_1",
        "source_snapshot_identity": _identity("scworkbench.provenance.snapshot_head.v1"),
        "content_identity": _identity("scworkbench.graph.content.canonical-json.v1"),
        "binding_identity": _identity("scworkbench.graph.axis-bindings.canonical-json.v1"),
        "descriptor_identity": _identity("scworkbench.graph.retained-descriptor.canonical-json.v1"),
        "component_counts": counts,
        "partition_limits": {"max_rows": "2", "max_decoded_bytes": "4096"},
        "selected_binding_ids": [],
        "grant_identity": _identity(
            "scworkbench.operation_pack.graph_read_grant.v1", "grant excluding grant_identity"
        ),
    }
    write_grant = {
        "schema": "scworkbench.graph.write_grant.v1",
        "output_name": "result",
        "component_order": "graph_components_v1",
        "binding_policy": "optional",
        "authorized_source_axes": [],
        "resource_budgets": _budgets(),
        "grant_identity": _identity(
            "scworkbench.operation_pack.graph_write_grant.v1", "grant excluding grant_identity"
        ),
    }
    _definition(REQUEST_SCHEMA, "graph_read_grant").validate(read_grant)
    _definition(REQUEST_SCHEMA, "graph_write_grant").validate(write_grant)
    observations = {
        name: {
            "rows": count,
            "partitions": "0" if count == "0" else "1",
            "chain_identity": _identity("scworkbench.graph.partition_chain.v1"),
        }
        for name, count in counts.items()
    }
    write_observation = {
        "output_name": "result",
        "grant_identity": write_grant["grant_identity"],
        "component_observations": observations,
        "content_identity": read_grant["content_identity"],
        "binding_identity": read_grant["binding_identity"],
        "descriptor_identity": read_grant["descriptor_identity"],
        "component_counts": counts,
        "sealed_output_identity": _identity(
            "scworkbench.graph.sealed_output.v1", "seal excluding sealed_output_identity"
        ),
        "complete": True,
    }
    _definition(RECEIPT_SCHEMA, "graph_write_observation").validate(write_observation)
    bad = copy.deepcopy(write_observation)
    bad["component_observations"]["nodes"]["rows"] = 1
    assert not _definition(RECEIPT_SCHEMA, "graph_write_observation").is_valid(bad)
    bad = copy.deepcopy(write_observation)
    bad["host_path"] = "/tmp/forbidden"
    assert not _definition(RECEIPT_SCHEMA, "graph_write_observation").is_valid(bad)
