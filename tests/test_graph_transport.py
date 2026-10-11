from __future__ import annotations

import copy

import pytest

from pybench.graph_transport import (
    validate_graph_write_grant,
    validate_graph_write_observation,
)
from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity

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


def _hash(scheme: str, value: dict, field: str, scope: str | None = None) -> dict:
    result = copy.deepcopy(value)
    result[field] = _identity(scheme, result, scope)
    return result


def _id(scheme: str) -> dict:
    return {"scheme": scheme, "sha256": "a" * 64}


def _budgets() -> dict:
    return {
        "max_nodes": "100",
        "max_edges": "100",
        "max_incidences": "200",
        "max_attribute_fields": "20",
        "max_staged_bytes": "1000000",
        "max_published_bytes": "1000000",
        "max_wal_bytes": "1000000",
        "max_spill_bytes": "1000000",
        "max_memory_bytes": "1000000",
        "max_collect_bytes": "100000",
        "partition_limits": {
            "max_rows": "20",
            "max_decoded_bytes": "10000",
        },
    }


def _axis(transition: dict, *, input_name: str = "matrix", output_name: str = "graph") -> dict:
    return _hash(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        {
            "schema": "scworkbench.graph.source_axis_authority.v1",
            "output_name": output_name,
            "input_name": input_name,
            "source_binding_id": None,
            "axis_kind": "cell",
            "source_ref": {"kind": "matrix", "source_id": "matrix_1"},
            "source_snapshot_identity": _id("scworkbench.provenance.snapshot_head.v1"),
            "source_payload_closure_identity": _id("scworkbench.content.matrix.fragment-tree.v2"),
            "axis_count": "2",
            "axis_content_identity": _id("scworkbench.graph.source-axis-content.canonical-json.v1"),
            "axis_order_identity": _id("scworkbench.cell_order.v1"),
            "transition_identity": transition,
        },
        "authority_identity",
        "authority excluding authority_identity",
    )


def _retained_axis(transition: dict, *, output_name: str = "graph") -> dict:
    axis = _axis(transition, input_name="source", output_name=output_name)
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
    return axis


def _grant(transition: dict) -> dict:
    return _hash(
        "scworkbench.operation_pack.graph_write_grant.v1",
        {
            "schema": "scworkbench.graph.write_grant.v1",
            "output_name": "graph",
            "component_order": "graph_components_v1",
            "binding_policy": "optional",
            "authorized_source_axes": [_retained_axis(transition)],
            "resource_budgets": _budgets(),
        },
        "grant_identity",
        "grant excluding grant_identity",
    )


def _transition() -> dict:
    return _id("scworkbench.operation_pack.transition.v2") | {
        "scope": "transition excluding transition_identity",
    }


def test_issued_source_axis_grant_is_closed():
    transition = _transition()
    validate_graph_write_grant(
        _grant(transition),
        transition_identity=transition,
        input_kinds={"source": "graph"},
        output_names=["graph"],
    )


def test_direct_matrix_axis_grant_accepts_issued_native_closure():
    transition = _transition()
    grant = _grant(transition)
    grant["authorized_source_axes"] = [_axis(transition)]
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    validate_graph_write_grant(
        grant,
        transition_identity=transition,
        input_kinds={"matrix": "matrix"},
        output_names=["graph"],
    )


def test_selected_graph_binding_with_matrix_source_accepts_issued_closure():
    transition = _transition()
    axis = _axis(transition, input_name="source")
    axis["source_binding_id"] = "binding_1"
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        {k: v for k, v in axis.items() if k != "authority_identity"},
        "authority excluding authority_identity",
    )
    grant = _grant(transition)
    grant["authorized_source_axes"] = [axis]
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        {k: v for k, v in grant.items() if k != "grant_identity"},
        "grant excluding grant_identity",
    )
    validate_graph_write_grant(
        grant,
        transition_identity=transition,
        input_kinds={"source": "graph"},
        output_names=["graph"],
    )


@pytest.mark.parametrize(
    ("input_kind", "axis_factory", "direct", "message"),
    [
        ("cell_vector", _axis, True, "direct matrix axis authority requires a matrix input"),
        ("matrix", _axis, False, "selected matrix axis authority requires a graph input"),
        ("matrix", _retained_axis, True, "retained cell axis authority differs"),
        ("cell_vector", _retained_axis, False, "graph source requires an explicit"),
        ("graph", _retained_axis, False, "feature binding cannot claim a retained cell axis"),
    ],
)
def test_source_axis_kind_must_match_declared_input(input_kind, axis_factory, direct, message):
    transition = _transition()
    axis = axis_factory(transition)
    if direct:
        axis["source_binding_id"] = None
    elif axis_factory is _axis:
        axis["source_binding_id"] = "binding_1"
    elif input_kind == "graph":
        axis["axis_kind"] = "feature"
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        {k: v for k, v in axis.items() if k != "authority_identity"},
        "authority excluding authority_identity",
    )
    grant = _grant(transition)
    grant["authorized_source_axes"] = [axis]
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        {k: v for k, v in grant.items() if k != "grant_identity"},
        "grant excluding grant_identity",
    )
    with pytest.raises(OperationPackError, match=message):
        validate_graph_write_grant(
            grant,
            transition_identity=transition,
            input_kinds={axis["input_name"]: input_kind},
            output_names=["graph"],
        )


def test_self_rehashed_source_axis_count_above_int64_rejects():
    transition = _transition()
    grant = _grant(transition)
    axis = grant["authorized_source_axes"][0]
    axis["axis_count"] = "9223372036854775808"
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
    with pytest.raises(OperationPackError, match="exceeds signed int64"):
        validate_graph_write_grant(
            grant,
            transition_identity=transition,
            input_kinds={"source": "graph"},
            output_names=["graph"],
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda grant: grant["authorized_source_axes"][0].update(input_name="undeclared"),
        lambda grant: grant["authorized_source_axes"][0].update(output_name="other"),
        lambda grant: grant["authorized_source_axes"][0].update(source_binding_id=None),
        lambda grant: grant["authorized_source_axes"][0].update(
            source_payload_closure_identity=_id("scworkbench.content.matrix.fragment-tree.v2")
        ),
        lambda grant: grant["authorized_source_axes"][0].update(
            transition_identity=_id("scworkbench.operation_pack.transition.v2")
            | {
                "scope": "transition excluding transition_identity",
                "sha256": "b" * 64,
            }
        ),
        lambda grant: grant.update(binding_policy="forbidden"),
    ],
)
def test_self_rehashed_wrong_authority_still_rejects(mutator):
    transition = _transition()
    grant = _grant(transition)
    mutator(grant)
    axis = grant["authorized_source_axes"][0]
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        {k: v for k, v in axis.items() if k != "authority_identity"},
        "authority excluding authority_identity",
    )
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        {k: v for k, v in grant.items() if k != "grant_identity"},
        "grant excluding grant_identity",
    )
    with pytest.raises(OperationPackError):
        validate_graph_write_grant(
            grant,
            transition_identity=transition,
            input_kinds={"source": "graph"},
            output_names=["graph"],
        )


def _observation(grant: dict) -> dict:
    counts = {component: "0" for component in COMPONENTS}
    counts.update(header="1", nodes="1")
    observations = {
        component: {
            "rows": count,
            "partitions": "0" if count == "0" else "1",
            "chain_identity": _id("scworkbench.graph.partition_chain.v1"),
        }
        for component, count in counts.items()
    }
    return _hash(
        "scworkbench.graph.sealed_output.v1",
        {
            "output_name": "graph",
            "grant_identity": grant["grant_identity"],
            "component_observations": observations,
            "content_identity": _id("scworkbench.graph.content.canonical-json.v1"),
            "binding_identity": _id("scworkbench.graph.axis-bindings.canonical-json.v1"),
            "descriptor_identity": _id("scworkbench.graph.retained-descriptor.canonical-json.v1"),
            "component_counts": counts,
            "complete": True,
        },
        "sealed_output_identity",
        "seal excluding sealed_output_identity",
    )


def test_compact_graph_receipt_count_and_grant_closure():
    grant = _grant(_transition())
    observation = _observation(grant)
    validate_graph_write_observation(observation, grant)
    observation["component_counts"]["nodes"] = "2"
    observation["sealed_output_identity"] = _identity(
        "scworkbench.graph.sealed_output.v1",
        {k: v for k, v in observation.items() if k != "sealed_output_identity"},
        "seal excluding sealed_output_identity",
    )
    with pytest.raises(OperationPackError, match="count differs"):
        validate_graph_write_observation(observation, grant)
