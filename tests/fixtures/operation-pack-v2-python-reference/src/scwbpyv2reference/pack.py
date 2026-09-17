from pybench.operation_packs_v2 import operation_pack_handlers_v2


def _all_contracts(readers, writers, params, context):
    for reader in readers.values():
        reader()
    cell = ["cell_1"]
    feature = ["feature_1"]
    writers.write_matrix(
        "matrix_ordinary",
        {
            "kind": "matrix",
            "dimensions": [1, 1],
            "cell_id": cell,
            "feature_id": feature,
            "values": [[1.0]],
        },
    )
    writers.write_matrix(
        "matrix_backed",
        {
            "kind": "matrix",
            "dimensions": [1, 1],
            "cell_id": cell,
            "feature_id": feature,
            "values": [[2.0]],
        },
    )
    writers.write_space(
        "space_dense", {"kind": "space", "dimensions": [1, 1], "cell_id": cell, "values": [[1.0]]}
    )
    writers.write_space(
        "space_file", {"kind": "space", "dimensions": [1, 1], "cell_id": cell, "values": [[2.0]]}
    )
    two_d = {"kind": "space_2d", "dimensions": [1, 2], "cell_id": cell, "values": [[0.0, 1.0]]}
    writers.write_space_2d("space_materialized_2d", two_d)
    writers.write_space_2d("space_terminal_2d", two_d)
    writers.write_model_ref(
        "model",
        {
            "kind": "model_ref",
            "dimensions": [1],
            "engine": "synthetic",
            "model_space_id": "space_file",
            "in_space_id": "matrix_backed",
            "payload_tree": {"model.txt": "synthetic"},
        },
    )
    writers.write_terminal_primitive(
        "cell_vector",
        {
            "kind": "cell_vector",
            "dimensions": [1],
            "cell_id": cell,
            "value": ["a"],
            "meta": {"fixture": "v2"},
        },
    )
    writers.write_terminal_primitive(
        "cell_vector_family",
        {
            "kind": "cell_vector_family",
            "dimensions": [1, 3],
            "cell_id": cell,
            "key": ["k"],
            "value": [1.0],
        },
    )
    writers.write_terminal_primitive(
        "relation_family",
        {
            "kind": "relation_family",
            "dimensions": [1, 3],
            "cell_id": cell,
            "key": ["k"],
            "value": [1.0],
        },
    )
    writers.write_terminal_primitive(
        "feature_table",
        {
            "kind": "feature_table",
            "dimensions": [1, 1],
            "feature_id": feature,
            "rows": [{"feature_id": "feature_1"}],
        },
    )
    writers.write_terminal_primitive(
        "obs_table", {"kind": "obs_table", "dimensions": [1, 1], "rows": [{"value": 1}]}
    )
    writers.write_path_2d_family(
        "path_2d",
        {
            "kind": "path_2d_family",
            "dimensions": [1, 4],
            "key": ["p"],
            "point": [1],
            "x": [0.0],
            "y": [1.0],
        },
    )
    writers.write_space_path_family(
        "space_path",
        {
            "kind": "space_path_family",
            "dimensions": [1, 5],
            "space_id": ["space_file"],
            "path_id": ["p"],
            "point": [1],
            "dim": [1],
            "value": [0.0],
        },
    )
    writers.request_cell_set_admission(
        "cell_set",
        {"kind": "cell_set_admission", "dimensions": [1], "name": "v2_cells", "cells": cell},
    )


def _dynamic_provider(readers, writers, params, context):
    matrix = readers["matrix"]()
    cells = matrix["cell_id"]
    writers.write_terminal_primitive(
        "provider",
        {
            "kind": "cell_vector",
            "dimensions": [len(cells)],
            "cell_id": cells,
            "value": ["available"] * len(cells),
            "meta": {"provider": matrix["meta"]["genome_provider"]},
        },
    )


def _chunk_probe(readers, writers, params, context):
    features = params["features"]
    chunk_size = params["chunk_size"]
    observed = []
    for start in range(0, len(features), chunk_size):
        batch = features[start : start + chunk_size]
        observed.extend(readers["matrix"](features=batch)["feature_id"])
    writers.write_terminal_primitive(
        "feature_table",
        {
            "kind": "feature_table",
            "dimensions": [len(observed), 1],
            "feature_id": observed,
            "rows": [{"feature_id": feature} for feature in observed],
        },
    )


def _family_probe(readers, writers, params, context):
    readers["matrix"]()
    writers.request_cell_set_family_admission("family", params["family"])
    writers.write_terminal_primitive(
        "summary", {"kind": "obs_table", "dimensions": [1, 1], "rows": [{"value": 1}]}
    )


def _registry_probe(readers, writers, params, context):
    readers["matrix"]()
    snapshot = readers["feature_sets"]()
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"set_count": len(snapshot["sets"])}],
        },
    )


def register():
    return operation_pack_handlers_v2(
        {
            "scwbpyv2reference::all_contracts": _all_contracts,
            "scwbpyv2reference::dynamic_provider": _dynamic_provider,
            "scwbpyv2reference::chunk_probe": _chunk_probe,
            "scwbpyv2reference::family_probe": _family_probe,
            "scwbpyv2reference::registry_probe": _registry_probe,
        }
    )
