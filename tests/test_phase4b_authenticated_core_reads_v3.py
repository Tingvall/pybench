from __future__ import annotations

import hashlib

import pytest

from pybench.authenticated_core_reads import validate_authenticated_core_read
from pybench.operation_packs import OperationPackError, canonical_json
from pybench.operation_packs_v2 import _identity
from pybench.protocol_v4 import _validate_cardinality_payload


def _raw(value):
    return canonical_json(value).encode("utf-8")


def _manifest(raw, name):
    return _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {
            "path": f"registry_reads/{name}/payload.json",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "registry read payload manifest",
    )


def _descriptor(value):
    value["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.registry_read_descriptor.v3",
        value,
        "descriptor excluding descriptor_identity",
    )
    return value


def _transition():
    return {
        "session_id": "session-1",
        "run_id": "run-1",
        "checkpoint_generation": "7",
        "source_state_identity": _identity("source.state.v1", {"generation": 7}),
    }


def _session_contract():
    transition = _transition()
    declaration = {
        "name": "cells",
        "category": "authenticated_core_axis_snapshot",
        "kind": "session_cell_axis",
        "reader": "read_session_cell_axis",
        "selection_policy": "effective_session_cells_v1",
        "coverage_policy": "exact_complete_order_v1",
        "call_discipline": "single_use",
    }
    cells = ["c2", "c1", "c3"]
    snapshot = {
        "schema": "scworkbench.session_cell_axis.snapshot.v1",
        "selection_policy": declaration["selection_policy"],
        "coverage_policy": declaration["coverage_policy"],
        "session_id": transition["session_id"],
        "run_id": transition["run_id"],
        "checkpoint_generation": transition["checkpoint_generation"],
        "transition_source_state_identity": transition["source_state_identity"],
        "cell_count": len(cells),
        "cell_id": cells,
        "cell_order_identity": _identity("scworkbench.cell_order.v1", cells),
    }
    raw = _raw(snapshot)
    selection = _identity(
        "scworkbench.session_cell_axis.selection.v1",
        {
            "selection_policy": declaration["selection_policy"],
            "coverage_policy": declaration["coverage_policy"],
            "cell_id": cells,
        },
        "ordered effective-session selection",
    )
    source = _identity(
        "scworkbench.session_cell_axis.source.v1",
        {
            "session_id": transition["session_id"],
            "run_id": transition["run_id"],
            "checkpoint_generation": transition["checkpoint_generation"],
            "transition_source_state_identity": transition["source_state_identity"],
            "cell_order_identity": snapshot["cell_order_identity"],
        },
        "session cell-axis source state",
    )
    descriptor = _descriptor(
        {
            "category": declaration["category"],
            "kind": declaration["kind"],
            "reader": declaration["reader"],
            "selection_policy": declaration["selection_policy"],
            "coverage_policy": declaration["coverage_policy"],
            "session_id": transition["session_id"],
            "run_id": transition["run_id"],
            "checkpoint_generation": transition["checkpoint_generation"],
            "transition_source_state_identity": transition["source_state_identity"],
            "dimensions": [len(cells)],
            "cell_order_identity": snapshot["cell_order_identity"],
            "selection_identity": selection,
            "content_identity": _identity(
                snapshot["schema"], snapshot, "canonical session cell-axis snapshot"
            ),
            "source_identity": source,
            "ancestry_identity": _identity(
                "scworkbench.provenance.ancestry.v1",
                {"source_identity": source, "selection_identity": selection},
                "session cell-axis ancestry",
            ),
            "payload_manifest_identity": _manifest(raw, "cells"),
        }
    )
    return declaration, snapshot, raw, descriptor, {"transition": transition, "inputs": []}


def _matrix_contract():
    input_descriptor = {
        "descriptor_identity": _identity("input.descriptor.v1", {"name": "matrix"}),
        "content_identity": _identity("matrix.content.v1", {"name": "matrix"}),
        "feature_order_identity": _identity("scworkbench.feature_order.v1", ["g2", "g1", "g3"]),
    }
    declaration = {
        "name": "annotations",
        "category": "authenticated_core_registry_snapshot",
        "kind": "matrix_feature_annotations",
        "reader": "read_matrix_feature_annotations",
        "matrix_input": "matrix",
        "fields": ["feature_type"],
        "row_order": "matrix_feature_order_v1",
        "missing_annotation_policy": "preserve_null",
        "call_discipline": "single_use",
    }
    rows = [
        {"feature_id": "g2", "values": {"feature_type": "gene"}},
        {"feature_id": "g1", "values": {"feature_type": None}},
        {"feature_id": "g3", "values": {"feature_type": "gene"}},
    ]
    snapshot = {
        "schema": "scworkbench.matrix_feature_annotations.snapshot.v1",
        "matrix_input": "matrix",
        "matrix_id": "rna",
        "input_descriptor_identity": {
            key: input_descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
        },
        "input_content_identity": input_descriptor["content_identity"],
        "feature_count": len(rows),
        "feature_order_identity": input_descriptor["feature_order_identity"],
        "fields": ["feature_type"],
        "rows": rows,
    }
    raw = _raw(snapshot)
    source = _identity(
        "scworkbench.matrix_feature_annotations.source.v1",
        {"matrix_id": "rna", "fields": ["feature_type"], "rows": rows},
        "ordered matrix feature annotations",
    )
    descriptor = _descriptor(
        {
            "category": declaration["category"],
            "kind": declaration["kind"],
            "reader": declaration["reader"],
            "matrix_input": "matrix",
            "fields": ["feature_type"],
            "row_order": declaration["row_order"],
            "missing_annotation_policy": declaration["missing_annotation_policy"],
            "matrix_id": "rna",
            "dimensions": [3, 1],
            "input_descriptor_identity": snapshot["input_descriptor_identity"],
            "input_content_identity": snapshot["input_content_identity"],
            "feature_order_identity": snapshot["feature_order_identity"],
            "content_identity": _identity(
                snapshot["schema"], snapshot, "canonical matrix feature annotations"
            ),
            "source_identity": source,
            "ancestry_identity": _identity(
                "scworkbench.provenance.ancestry.v1",
                {
                    "input_descriptor_identity": snapshot["input_descriptor_identity"],
                    "input_content_identity": snapshot["input_content_identity"],
                    "source_identity": source,
                },
                "matrix feature annotation ancestry",
            ),
            "payload_manifest_identity": _manifest(raw, "annotations"),
        }
    )
    request = {
        "transition": _transition(),
        "inputs": [{"name": "matrix", "descriptor": input_descriptor}],
    }
    return declaration, snapshot, raw, descriptor, request


def test_session_cell_axis_snapshot_is_exact_and_bound_to_transition():
    declaration, snapshot, raw, descriptor, request = _session_contract()
    assert (
        validate_authenticated_core_read(raw, "cells", descriptor, declaration, request) == snapshot
    )
    mutated = dict(snapshot)
    mutated["cell_id"] = ["c1", "c2", "c3"]
    with pytest.raises(OperationPackError, match="Session-cell snapshot"):
        validate_authenticated_core_read(_raw(mutated), "cells", descriptor, declaration, request)


def test_matrix_feature_annotations_preserve_order_nulls_and_matrix_binding():
    declaration, snapshot, raw, descriptor, request = _matrix_contract()
    assert (
        validate_authenticated_core_read(raw, "annotations", descriptor, declaration, request)
        == snapshot
    )
    mutated = dict(snapshot)
    mutated["rows"] = list(reversed(snapshot["rows"]))
    with pytest.raises(OperationPackError, match="Matrix-feature annotation snapshot"):
        validate_authenticated_core_read(
            _raw(mutated), "annotations", descriptor, declaration, request
        )
    foreign_request = {
        **request,
        "inputs": [
            {
                **request["inputs"][0],
                "descriptor": {
                    **request["inputs"][0]["descriptor"],
                    "content_identity": _identity("matrix.content.v1", {"name": "foreign-matrix"}),
                },
            }
        ],
    }
    with pytest.raises(OperationPackError, match="Matrix-feature annotation snapshot"):
        validate_authenticated_core_read(
            raw, "annotations", descriptor, declaration, foreign_request
        )
    reordered_request = {
        **request,
        "inputs": [
            {
                **request["inputs"][0],
                "descriptor": {
                    **request["inputs"][0]["descriptor"],
                    "feature_order_identity": _identity(
                        "scworkbench.feature_order.v1", ["g1", "g2", "g3"]
                    ),
                },
            }
        ],
    }
    with pytest.raises(OperationPackError, match="Matrix-feature annotation snapshot"):
        validate_authenticated_core_read(
            raw, "annotations", descriptor, declaration, reordered_request
        )


@pytest.mark.parametrize(
    ("keys", "groups"),
    [
        (["total_counts", "detected_features"], []),
        (
            ["total_counts", "detected_features", "mito_counts", "mito_percent"],
            ["mitochondrial"],
        ),
        (
            [
                "total_counts",
                "detected_features",
                "mito_counts",
                "mito_percent",
                "ribo_counts",
                "ribo_percent",
            ],
            ["mitochondrial", "ribosomal"],
        ),
    ],
)
def test_bounded_group_cardinality_accepts_only_complete_declared_groups(keys, groups):
    declaration, snapshot, _, descriptor, _ = _session_contract()
    cells = snapshot["cell_id"]
    grant = {
        "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
        "output": "metrics",
        "kind": "cell_vector_family",
        "policy": "authenticated_output_axes_v1",
        "shape": "bounded_grouped_cell_key_cartesian",
        "row_order": "key_major_then_cell_v1",
        "missing_values": "forbidden",
        "cell_axis_source": {
            "source": "session_cell_axis_read_v1",
            "registry_read": declaration["name"],
            "descriptor_identity": {
                key: descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
            },
            "selection_identity": descriptor["selection_identity"],
        },
        "cell_axis_count": len(cells),
        "cell_axis_order_identity": snapshot["cell_order_identity"],
        "minimum_key_count": 2,
        "maximum_key_count": 6,
        "minimum_row_count": 6,
        "maximum_row_count": 18,
        "key_axis_resolution": {
            "source": "declared_bounded_key_groups_v1",
            "required_keys": ["total_counts", "detected_features"],
            "optional_groups": [
                {"name": "mitochondrial", "keys": ["mito_counts", "mito_percent"]},
                {"name": "ribosomal", "keys": ["ribo_counts", "ribo_percent"]},
            ],
            "selection_owner": "operation_pack_v1",
            "group_order": "manifest_order_v1",
        },
    }
    grant["cardinality_grant_identity"] = _identity(
        grant["schema"], grant, "grant excluding cardinality_grant_identity"
    )
    payload = {
        "kind": "cell_vector_family",
        "dimensions": [len(keys) * len(cells), 3],
        "key": [key for key in keys for _ in cells],
        "cell_id": [cell for _ in keys for cell in cells],
        "value": [0 for _ in keys for _ in cells],
    }
    observed = _validate_cardinality_payload(
        grant,
        payload,
        {},
        {"value_kind": "continuous"},
        {"cells": snapshot},
    )
    assert observed["selected_optional_groups"] == groups
    assert observed["observed_key_axis"] == keys


def test_bounded_group_cardinality_rejects_partial_group_and_cell_mutation():
    declaration, snapshot, _, descriptor, _ = _session_contract()
    cells = snapshot["cell_id"]
    required = ["total_counts", "detected_features"]
    grant = {
        "schema": "scworkbench.operation_pack.output_cardinality_grant.v1",
        "output": "metrics",
        "kind": "cell_vector_family",
        "policy": "authenticated_output_axes_v1",
        "shape": "bounded_grouped_cell_key_cartesian",
        "row_order": "key_major_then_cell_v1",
        "missing_values": "forbidden",
        "cell_axis_source": {
            "source": "session_cell_axis_read_v1",
            "registry_read": declaration["name"],
            "descriptor_identity": {
                key: descriptor["descriptor_identity"][key] for key in ("scheme", "sha256")
            },
            "selection_identity": descriptor["selection_identity"],
        },
        "cell_axis_count": 3,
        "cell_axis_order_identity": snapshot["cell_order_identity"],
        "minimum_key_count": 2,
        "maximum_key_count": 4,
        "minimum_row_count": 6,
        "maximum_row_count": 12,
        "key_axis_resolution": {
            "source": "declared_bounded_key_groups_v1",
            "required_keys": required,
            "optional_groups": [{"name": "mitochondrial", "keys": ["mito_counts", "mito_percent"]}],
            "selection_owner": "operation_pack_v1",
            "group_order": "manifest_order_v1",
        },
    }
    grant["cardinality_grant_identity"] = _identity(grant["schema"], grant)

    def payload(keys, axis=cells):
        return {
            "key": [key for key in keys for _ in axis],
            "cell_id": [cell for _ in keys for cell in axis],
            "value": [1.0 for _ in keys for _ in axis],
        }

    with pytest.raises(OperationPackError, match="complete bounded groups"):
        _validate_cardinality_payload(
            grant,
            payload(required + ["mito_counts"]),
            {},
            registry_values={"cells": snapshot},
        )
    with pytest.raises(OperationPackError, match="cell rows"):
        _validate_cardinality_payload(
            grant,
            payload(required, ["c1", "c2", "c3"]),
            {},
            registry_values={"cells": snapshot},
        )
