"""Generate the proposed closed graph ABI4/process5 shared JSON Schemas.

The generated protocol files are mirrored byte-for-byte into scworkbench only
after joint review. Existing ABI3/process4 assets are never rewritten.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "protocol"
ID_PATTERN = "^[^\\u0000]+$"
UINT64_PATTERN = "^(?:0|[1-9][0-9]*)$"
SHA_PATTERN = "^[0-9a-f]{64}$"
NAME_PATTERN = "^[a-z][a-z0-9_]*$"
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


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties) if required is None else required,
        "properties": properties,
    }


def arr(item: dict[str, Any], *, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "minItems": minimum, "items": item}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def identity(scheme: str | None = None) -> dict[str, Any]:
    return obj(
        {
            "scheme": {"const": scheme} if scheme else {"type": "string", "minLength": 1},
            "sha256": {"type": "string", "pattern": SHA_PATTERN},
        }
    )


def scoped_identity(scheme: str, scope: str) -> dict[str, Any]:
    return obj(
        {
            "scheme": {"const": scheme},
            "scope": {"const": scope},
            "sha256": {"type": "string", "pattern": SHA_PATTERN},
        }
    )


def uint64() -> dict[str, Any]:
    return {"type": "string", "pattern": UINT64_PATTERN, "maxLength": 19}


def graph_defs() -> dict[str, Any]:
    graph_ids = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": ID_PATTERN}
    typed = {"oneOf": [{"type": "null"}, {"type": "boolean"}, {"type": "string"}]}
    defs: dict[str, Any] = {
        "id": graph_ids,
        "uint64": uint64(),
        "identity": identity(),
        "content_identity": identity("scworkbench.graph.content.canonical-json.v1"),
        "binding_identity": identity("scworkbench.graph.axis-bindings.canonical-json.v1"),
        "descriptor_identity": identity("scworkbench.graph.retained-descriptor.canonical-json.v1"),
        "snapshot_identity": identity("scworkbench.provenance.snapshot_head.v1"),
        "axis_content_identity": identity(
            "scworkbench.graph.source-axis-content.canonical-json.v1"
        ),
        "matrix_payload_closure_identity": identity("scworkbench.content.matrix.fragment-tree.v2"),
        "ordered_object_payload_closure_identity": identity(
            "scworkbench.artifact.ordered-object-payload.canonical-json.v1"
        ),
        "partition_identity": identity("scworkbench.graph.partition.v1"),
        "chain_identity": identity("scworkbench.graph.partition_chain.v1"),
        "node": obj({"node_id": ref("id")}),
        "edge": obj(
            {
                "edge_id": ref("id"),
                "kind": {"enum": ["ordinary", "hyper"]},
                "direction": {"enum": ["directed", "undirected"]},
            }
        ),
        "incidence": obj(
            {
                "incidence_id": ref("id"),
                "edge_id": ref("id"),
                "node_id": ref("id"),
                "role": {"enum": ["tail", "head", "member"]},
                "ordinal": {"type": "integer", "minimum": 0, "maximum": 999999999999999},
            }
        ),
        "attribute_field": obj(
            {
                "name": {"type": "string", "minLength": 1, "pattern": ID_PATTERN},
                "type": {"enum": ["bool", "utf8", "int64_decimal_string", "float64_le_hex"]},
                "nullable": {"type": "boolean"},
                "values": arr(typed),
            }
        ),
        "source_ref": obj(
            {
                "kind": {"enum": ["matrix", "retained_cell_axis"]},
                "source_id": ref("id"),
            }
        ),
        "binding_mapping": obj({"node_id": ref("id"), "axis_id": ref("id")}),
        "binding": obj(
            {
                "binding_id": ref("id"),
                "axis_kind": {"enum": ["cell", "feature"]},
                "source_ref": ref("source_ref"),
                "source_snapshot_identity": ref("snapshot_identity"),
                "axis_content_identity": ref("axis_content_identity"),
                "axis_order_identity": ref("identity"),
                "mapping": arr(ref("binding_mapping")),
            }
        ),
        "content": obj(
            {
                "schema": {"const": "scworkbench.graph.content.v1"},
                "direction_mode": {"enum": ["directed", "undirected", "mixed"]},
                "nodes": arr(ref("node")),
                "edges": arr(ref("edge")),
                "incidences": arr(ref("incidence")),
                "attributes": obj(
                    {
                        scope: arr(ref("attribute_field"))
                        for scope in ("graph", "node", "edge", "incidence")
                    }
                ),
            }
        ),
        "binding_preimage": obj(
            {
                "schema": {"const": "scworkbench.graph.axis-bindings.v1"},
                "graph_content_identity": ref("content_identity"),
                "bindings": arr(ref("binding")),
            }
        ),
        "descriptor_preimage": obj(
            {
                "schema": {"const": "scworkbench.graph.retained-descriptor.v1"},
                "content_identity": ref("content_identity"),
                "binding_identity": ref("binding_identity"),
                "binding_count": {"type": "integer", "minimum": 0, "maximum": 999999999999999},
            }
        ),
        "header_row": obj(
            {
                "schema": {"const": "scworkbench.graph.content.v1"},
                "direction_mode": {"enum": ["directed", "undirected", "mixed"]},
            }
        ),
        "node_row": obj({"node_id": ref("id")}),
        "edge_row": obj(
            {
                "edge_id": ref("id"),
                "kind": {"enum": ["ordinary", "hyper"]},
                "direction": {"enum": ["directed", "undirected"]},
            }
        ),
        "incidence_row": ref("incidence"),
        "attribute_field_row": obj(
            {
                "scope": {"enum": ["graph", "node", "edge", "incidence"]},
                "field_ordinal": ref("uint64"),
                "name": {"type": "string", "minLength": 1, "pattern": ID_PATTERN},
                "type": {"enum": ["bool", "utf8", "int64_decimal_string", "float64_le_hex"]},
                "nullable": {"type": "boolean"},
            }
        ),
        "attribute_value_row": obj(
            {
                "scope": {"enum": ["graph", "node", "edge", "incidence"]},
                "field_ordinal": ref("uint64"),
                "entity_ordinal": ref("uint64"),
                "value": typed,
            }
        ),
        "binding_row": obj(
            {
                "binding_ordinal": ref("uint64"),
                "binding_id": ref("id"),
                "axis_kind": {"enum": ["cell", "feature"]},
                "source_ref": ref("source_ref"),
                "source_snapshot_identity": ref("snapshot_identity"),
                "axis_content_identity": ref("axis_content_identity"),
                "axis_order_identity": ref("identity"),
            }
        ),
        "binding_mapping_row": obj(
            {
                "binding_id": ref("id"),
                "mapping_ordinal": ref("uint64"),
                "node_id": ref("id"),
                "axis_id": ref("id"),
            }
        ),
        "component_counts": obj({component: ref("uint64") for component in COMPONENTS}),
        "partition_limits": obj(
            {
                "max_rows": ref("uint64"),
                "max_decoded_bytes": ref("uint64"),
            }
        ),
        "resource_budgets": obj(
            {
                "max_nodes": ref("uint64"),
                "max_edges": ref("uint64"),
                "max_incidences": ref("uint64"),
                "max_attribute_fields": ref("uint64"),
                "max_staged_bytes": ref("uint64"),
                "max_published_bytes": ref("uint64"),
                "max_wal_bytes": ref("uint64"),
                "max_spill_bytes": ref("uint64"),
                "max_memory_bytes": ref("uint64"),
                "max_collect_bytes": ref("uint64"),
                "partition_limits": ref("partition_limits"),
            }
        ),
        "component_observation": obj(
            {
                "rows": ref("uint64"),
                "partitions": ref("uint64"),
                "chain_identity": ref("chain_identity"),
            }
        ),
        "component_observations": obj(
            {component: ref("component_observation") for component in COMPONENTS}
        ),
    }
    partition = obj(
        {
            "schema": {"const": "scworkbench.graph.component_partition.v1"},
            "component": {"enum": list(COMPONENTS)},
            "first_ordinal": ref("uint64"),
            "row_count": ref("uint64"),
            "rows": arr({"type": "object"}, minimum=1),
            "partition_identity": ref("partition_identity"),
        }
    )
    partition["allOf"] = [
        {
            "if": {"properties": {"component": {"const": component}}},
            "then": {"properties": {"rows": arr(ref(row_type), minimum=1)}},
        }
        for component, row_type in zip(
            COMPONENTS,
            (
                "header_row",
                "node_row",
                "edge_row",
                "incidence_row",
                "attribute_field_row",
                "attribute_value_row",
                "binding_row",
                "binding_mapping_row",
            ),
        )
    ]
    defs["component_partition"] = partition
    defs["igraph_carrier"] = obj(
        {
            "schema": {"const": "scworkbench.graph.igraph_carrier.v1"},
            "content": ref("content"),
            "bindings": arr(ref("binding")),
            "descriptor_identity": ref("descriptor_identity"),
        }
    )
    return defs


def graph_schema() -> dict[str, Any]:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://scworkbench.org/protocol/graph-v1.schema.json",
        "title": "scworkbench graph primitive v1",
        "description": "Closed value, component, binding and igraph carrier definitions.",
        "x-document-status": "JOINT_FREEZE_CANDIDATE",
        "$ref": "#/$defs/content",
        "$defs": graph_defs(),
    }
    return schema


def manifest_schema() -> dict[str, Any]:
    root = copy.deepcopy(json.loads((ROOT / "operation-pack-v3.schema.json").read_text()))
    root["$id"] = "https://scworkbench.org/protocol/operation-pack-v4.schema.json"
    root["title"] = "scworkbench operation-pack ABI v4 manifest with graph capability"
    root["description"] = (
        "Closed successor declaration: accepted ABI3 primitives plus generic graph. "
        "Executable backend support is a separate explicit capability inventory."
    )
    for key in list(root):
        if key.startswith("x-"):
            del root[key]
    root["x-document-status"] = "JOINT_FREEZE_CANDIDATE"
    root["properties"]["schema_version"]["const"] = "scworkbench.operation_pack.v4"
    root["properties"]["pack_abi_version"]["const"] = "4"
    compat = root["properties"]["compatibility"]["properties"]
    compat["pack_abi"]["properties"]["minimum"]["const"] = "4"
    compat["pack_abi"]["properties"]["maximum"]["const"] = "4"
    compat["process_protocol"]["properties"]["minimum"]["const"] = "pybench-op-v5"
    compat["process_protocol"]["properties"]["maximum"]["const"] = "pybench-op-v5"
    defs = root["$defs"]
    defs["primitive_kind"]["enum"].append("graph")
    defs["storage_profile"]["enum"].append("graph_sqlite_v1")
    shared = graph_defs()
    for name in ("uint64", "partition_limits", "resource_budgets"):
        defs[name] = shared[name]
    defs["graph_binding_id"] = {"type": "string", "minLength": 1, "maxLength": 256}
    defs["graph_axis_source_selector"] = {
        "oneOf": [
            obj(
                {
                    "kind": {"const": "fixed_binding_id_v1"},
                    "binding_id": ref("graph_binding_id"),
                }
            ),
            obj(
                {
                    "kind": {"const": "parameter_binding_id_v1"},
                    "parameter": {"type": "string", "pattern": NAME_PATTERN},
                }
            ),
        ]
    }
    defs["graph_axis_source"] = {
        "oneOf": [
            obj(
                {
                    "input_name": {"type": "string", "pattern": NAME_PATTERN},
                    "axis_kind": {"const": "cell"},
                    "selection_mode": {"const": "retained_input_axis"},
                    "source_binding_selector": {"type": "null"},
                }
            ),
            obj(
                {
                    "input_name": {"type": "string", "pattern": NAME_PATTERN},
                    "axis_kind": {"enum": ["cell", "feature"]},
                    "selection_mode": {"const": "selected_graph_binding"},
                    "source_binding_selector": ref("graph_axis_source_selector"),
                }
            ),
            obj(
                {
                    "input_name": {"type": "string", "pattern": NAME_PATTERN},
                    "axis_kind": {"enum": ["cell", "feature"]},
                    "selection_mode": {"const": "retained_matrix_axis"},
                    "source_binding_selector": {"type": "null"},
                }
            ),
        ]
    }
    defs["input"]["oneOf"].append(
        obj(
            {
                "name": {"type": "string", "pattern": NAME_PATTERN},
                "kind": {"const": "graph"},
                "required": {"type": "boolean"},
                "alignment": {"const": "none"},
                "storage_profile": {"const": "graph_sqlite_v1"},
                "read_mode": {"const": "bounded_partitions_v1"},
                "binding_policy": {"enum": ["forbidden", "optional", "required"]},
            }
        )
    )
    defs["output"]["oneOf"].append(
        obj(
            {
                "name": {"type": "string", "pattern": NAME_PATTERN},
                "category": {"const": "terminal_artifact"},
                "kind": {"const": "graph"},
                "storage_profile": {"const": "graph_sqlite_v1"},
                "allocated_id_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*_id$"},
                "required": {"const": True},
                "promotion_group": {"type": "string", "pattern": NAME_PATTERN},
                "binding_policy": {"enum": ["forbidden", "optional", "required"]},
                "axis_sources": arr(ref("graph_axis_source")),
                "resource_budgets": ref("resource_budgets"),
            }
        )
    )
    replay = defs["operation"]["properties"]["replay_equivalence"]
    replay["additionalProperties"]["enum"].append("exact_graph_descriptor")
    return root


def _successor(base_name: str, *, title: str) -> dict[str, Any]:
    root = copy.deepcopy(json.loads((ROOT / base_name).read_text()))
    root.pop("examples", None)
    for key in list(root):
        if key.startswith("x-"):
            del root[key]
    root["x-document-status"] = "JOINT_FREEZE_CANDIDATE"
    root["$id"] = root["$id"].replace("-v3.schema.json", "-v4.schema.json")
    root["title"] = title
    root["properties"]["schema"]["const"] = root["properties"]["schema"]["const"].replace(
        ".v3", ".v4"
    )
    root["properties"]["process_protocol_version"]["const"] = "pybench-op-v5"
    root["properties"]["pack_abi_version"]["const"] = "4"

    def replace_identity_schemes(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("const"), str) and value["const"] in {
                "scworkbench.operation_pack.pack.v3",
                "scworkbench.operation_pack.definition.v3",
                "scworkbench.operation_pack.manifest.v3",
                "scworkbench.operation_pack.capability.v3",
                "scworkbench.operation_pack.transition_request.v3",
                "scworkbench.operation_pack.transition_receipt.v3",
            }:
                value["const"] = value["const"][:-1] + "4"
            for child in value.values():
                replace_identity_schemes(child)
        elif isinstance(value, list):
            for child in value:
                replace_identity_schemes(child)

    replace_identity_schemes(root)
    return root


def _graph_transport_defs(root: dict[str, Any]) -> None:
    shared = graph_defs()
    for name in (
        "uint64",
        "content_identity",
        "binding_identity",
        "descriptor_identity",
        "snapshot_identity",
        "chain_identity",
        "component_counts",
        "partition_limits",
        "resource_budgets",
        "component_observation",
        "component_observations",
    ):
        root["$defs"][name] = shared[name]
    root["$defs"]["graph_read_grant_identity"] = scoped_identity(
        "scworkbench.operation_pack.graph_read_grant.v1",
        "grant excluding grant_identity",
    )
    root["$defs"]["graph_write_grant_identity"] = scoped_identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        "grant excluding grant_identity",
    )
    root["$defs"]["graph_source_axis_authority_identity"] = scoped_identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        "authority excluding authority_identity",
    )


def request_schema() -> dict[str, Any]:
    root = _successor(
        "operation-pack-transition-request-v3.schema.json",
        title="scworkbench operation-pack ABI v4 transition request",
    )
    _graph_transport_defs(root)
    defs = root["$defs"]
    shared = graph_defs()
    defs["graph_read_grant"] = obj(
        {
            "schema": {"const": "scworkbench.graph.read_grant.v1"},
            "input_name": {"type": "string", "pattern": NAME_PATTERN},
            "source_artifact_id": ref("id"),
            "source_snapshot_identity": ref("snapshot_identity"),
            "content_identity": ref("content_identity"),
            "binding_identity": ref("binding_identity"),
            "descriptor_identity": ref("descriptor_identity"),
            "component_counts": ref("component_counts"),
            "partition_limits": ref("partition_limits"),
            "selected_binding_ids": arr(ref("id")),
            "grant_identity": ref("graph_read_grant_identity"),
        }
    )
    defs["graph_write_grant"] = obj(
        {
            "schema": {"const": "scworkbench.graph.write_grant.v1"},
            "output_name": {"type": "string", "pattern": NAME_PATTERN},
            "component_order": {"const": "graph_components_v1"},
            "binding_policy": {"enum": ["forbidden", "optional", "required"]},
            "authorized_source_axes": arr(ref("graph_source_axis_authority")),
            "resource_budgets": ref("resource_budgets"),
            "grant_identity": ref("graph_write_grant_identity"),
        }
    )
    defs["graph_source_axis_authority"] = obj(
        {
            "schema": {"const": "scworkbench.graph.source_axis_authority.v1"},
            "output_name": {"type": "string", "pattern": NAME_PATTERN},
            "input_name": {"type": "string", "pattern": NAME_PATTERN},
            "source_binding_id": {"oneOf": [{"type": "null"}, ref("id")]},
            "axis_kind": {"enum": ["cell", "feature"]},
            "source_ref": shared["source_ref"],
            "source_snapshot_identity": ref("snapshot_identity"),
            "source_payload_closure_identity": {
                "oneOf": [
                    shared["matrix_payload_closure_identity"],
                    shared["ordered_object_payload_closure_identity"],
                ]
            },
            "axis_count": ref("uint64"),
            "axis_content_identity": shared["axis_content_identity"],
            "axis_order_identity": shared["identity"],
            "transition_identity": root["properties"]["transition"]["properties"][
                "transition_identity"
            ],
            "authority_identity": ref("graph_source_axis_authority_identity"),
        }
    )
    defs["graph_source_axis_authority"]["allOf"] = [
        {
            "if": {"properties": {"source_ref": {"properties": {"kind": {"const": "matrix"}}}}},
            "then": {
                "properties": {
                    "source_payload_closure_identity": shared["matrix_payload_closure_identity"]
                }
            },
            "else": {
                "properties": {
                    "source_payload_closure_identity": shared[
                        "ordered_object_payload_closure_identity"
                    ]
                }
            },
        }
    ]
    defs["input_descriptor"]["oneOf"].append(
        obj(
            {
                "kind": {"const": "graph"},
                "storage_profile": {"const": "graph_sqlite_v1"},
                "reader": {"const": "read_graph_parts"},
                "alignment": {"const": "none"},
                "content_identity": ref("content_identity"),
                "binding_identity": ref("binding_identity"),
                "retained_descriptor_identity": ref("descriptor_identity"),
                "source_snapshot_identity": ref("snapshot_identity"),
                "component_counts": ref("component_counts"),
                "graph_read_grant_identity": ref("graph_read_grant_identity"),
                "descriptor_identity": scoped_identity(
                    "scworkbench.operation_pack.graph_input_descriptor.v1",
                    "descriptor excluding descriptor_identity",
                ),
            }
        )
    )
    defs["output_descriptor"]["oneOf"].append(
        obj(
            {
                "category": {"const": "terminal_artifact"},
                "kind": {"const": "graph"},
                "storage_profile": {"const": "graph_sqlite_v1"},
                "writer": {"const": "write_graph"},
                "binding_policy": {"enum": ["forbidden", "optional", "required"]},
                "resource_budgets": ref("resource_budgets"),
                "graph_write_grant_identity": ref("graph_write_grant_identity"),
                "grant_identity": scoped_identity(
                    "scworkbench.operation_pack.output_grant.v4",
                    "descriptor excluding grant_identity",
                ),
            }
        )
    )
    capability = defs["capability"]["properties"]
    capability["reader_grants"]["items"]["oneOf"].append(
        obj(
            {
                "input_name": {"type": "string", "pattern": NAME_PATTERN},
                "reader": {"const": "read_graph_parts"},
                "grant_identity": ref("graph_read_grant_identity"),
            }
        )
    )
    capability["writer_grants"]["items"]["properties"]["writer"]["enum"].append("write_graph")
    capability["writer_grants"]["items"]["properties"]["grant_identity"] = {
        "oneOf": [
            capability["writer_grants"]["items"]["properties"]["grant_identity"],
            scoped_identity(
                "scworkbench.operation_pack.output_grant.v4",
                "descriptor excluding grant_identity",
            ),
        ]
    }
    root["properties"]["graph_read_grants"] = arr(ref("graph_read_grant"))
    root["properties"]["graph_write_grants"] = arr(ref("graph_write_grant"))
    return root


def receipt_schema() -> dict[str, Any]:
    root = _successor(
        "operation-pack-transition-receipt-v3.schema.json",
        title="scworkbench operation-pack ABI v4 transition receipt",
    )
    _graph_transport_defs(root)
    defs = root["$defs"]
    defs["graph_read_usage"] = obj(
        {
            "input_name": {"type": "string", "pattern": NAME_PATTERN},
            "grant_identity": ref("graph_read_grant_identity"),
            "component_observations": ref("component_observations"),
            "content_identity": ref("content_identity"),
            "binding_identity": ref("binding_identity"),
            "descriptor_identity": ref("descriptor_identity"),
            "selected_binding_ids": arr(ref("id")),
            "complete": {"const": True},
        }
    )
    defs["graph_write_observation"] = obj(
        {
            "output_name": {"type": "string", "pattern": NAME_PATTERN},
            "grant_identity": ref("graph_write_grant_identity"),
            "component_observations": ref("component_observations"),
            "content_identity": ref("content_identity"),
            "binding_identity": ref("binding_identity"),
            "descriptor_identity": ref("descriptor_identity"),
            "component_counts": ref("component_counts"),
            "sealed_output_identity": scoped_identity(
                "scworkbench.graph.sealed_output.v1",
                "seal excluding sealed_output_identity",
            ),
            "complete": {"const": True},
        }
    )
    defs["observed_output_descriptor"]["oneOf"].append(
        obj(
            {
                "kind": {"const": "graph"},
                "storage_profile": {"const": "graph_sqlite_v1"},
                "writer": {"const": "write_graph"},
                "content_identity": ref("content_identity"),
                "binding_identity": ref("binding_identity"),
                "retained_descriptor_identity": ref("descriptor_identity"),
                "component_counts": ref("component_counts"),
                "sealed_output_identity": scoped_identity(
                    "scworkbench.graph.sealed_output.v1",
                    "seal excluding sealed_output_identity",
                ),
                "observed_descriptor_identity": scoped_identity(
                    "scworkbench.operation_pack.observed_output_descriptor.v4",
                    "descriptor excluding observed_descriptor_identity",
                ),
            }
        )
    )
    root["properties"]["graph_read_usage"] = arr(ref("graph_read_usage"))
    root["properties"]["graph_write_observations"] = arr(ref("graph_write_observation"))
    return root


def python_runtime_schema() -> dict[str, Any]:
    root = copy.deepcopy(
        json.loads((ROOT / "pybench-op-v4-python-runtime.schema.json").read_text())
    )
    root["$id"] = "https://scworkbench.org/protocol/pybench-op-v5-python-runtime.schema.json"
    root["title"] = "PyBench process-v5 validation of unchanged Python runtime v4 sidecar"
    for key in list(root):
        if key.startswith("x-"):
            del root[key]
    root["x-document-status"] = "JOINT_FREEZE_CANDIDATE"

    def replace(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("const"), str) and value["const"] in {
                "scworkbench.operation_pack.transition_request.v3",
                "scworkbench.operation_pack.capability.v3",
                "scworkbench.operation_pack.definition.v3",
            }:
                value["const"] = value["const"][:-1] + "4"
            for child in value.values():
                replace(child)
        elif isinstance(value, list):
            for child in value:
                replace(child)

    replace(root)
    return root


def assets() -> dict[str, dict[str, Any]]:
    return {
        "graph-v1.schema.json": graph_schema(),
        "operation-pack-v4.schema.json": manifest_schema(),
        "operation-pack-transition-request-v4.schema.json": request_schema(),
        "operation-pack-transition-receipt-v4.schema.json": receipt_schema(),
        "pybench-op-v5-python-runtime.schema.json": python_runtime_schema(),
    }


def main() -> None:
    check = "--check" in sys.argv[1:]
    for name, value in assets().items():
        path = ROOT / name
        expected = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if check:
            if not path.is_file() or path.read_text() != expected:
                raise SystemExit(f"generated protocol drift: {name}")
        else:
            path.write_text(expected)
        print(name)


if __name__ == "__main__":
    main()
