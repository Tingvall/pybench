import os

from pybench import partition_axis
from pybench.operation_packs_v3 import operation_pack_handlers_v3


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
    fault = os.environ.get("SCWBPYV3_REGISTRY_PROBE_FAULT", "")
    if fault == "missing_reader":
        snapshot = {"sets": []}
    elif fault == "duplicate_reader":
        snapshot = readers["feature_sets"]()
        readers["feature_sets"]()
    elif fault == "undeclared_reader":
        readers["undeclared"]()
    else:
        snapshot = readers["feature_sets"]()
    if fault == "mutate_private_copy":
        snapshot["sets"].clear()
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"set_count": len(snapshot["sets"])}],
        },
    )


def _cell_chunk_probe(readers, writers, params, context):
    capability = context["typed_slice_reads"]["matrix_chunks"]
    cells = capability["cells"]
    features = capability["features"]
    observed = []
    for selected in partition_axis(cells, params["chunk_size"]):
        matrix = readers["matrix_chunks"](features=features, cells=selected)
        observed.extend(float(value) for value in matrix["values"][0])
    scores = {
        "kind": "cell_vector_family",
        "dimensions": [len(cells), 3],
        "cell_id": cells,
        "key": ["module1"] * len(cells),
        "value": observed,
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    }
    writers.write_terminal_primitive(
        "scores",
        scores,
    )
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"score_rows": len(scores["value"])}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _zero_axis_probe(readers, writers, params, context):
    capability = context["typed_slice_reads"]["matrix_chunks"]
    if capability["features"]:
        raise ValueError("zero-axis fixture requires an empty selected feature axis")
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"typed_slice_calls": 0}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _cardinality_family_probe(readers, writers, params, context):
    signals = readers["signals"]()
    cell_order = list(dict.fromkeys(signals["cell_id"]))
    key_order = list(dict.fromkeys(signals["key"]))
    values = {
        (key, cell): value
        for key, cell, value in zip(
            signals["key"], signals["cell_id"], signals["value"], strict=True
        )
    }
    rows = [(key, cell) for key in key_order for cell in cell_order]
    writers.write_terminal_primitive(
        "scores",
        {
            "kind": "cell_vector_family",
            "dimensions": [len(rows), 3],
            "cell_id": [cell for _, cell in rows],
            "key": [key for key, _ in rows],
            "value": [values[(key, cell)] for key, cell in rows],
            "storage_profile": "terminal_primitive_v1",
            "meta": dict(signals.get("meta", {})),
        },
    )


def _cardinality_reduction_probe(readers, writers, params, context):
    signals = readers["signals"]()
    cell_order = list(dict.fromkeys(signals["cell_id"]))
    totals = {cell: 0.0 for cell in cell_order}
    for cell, value in zip(signals["cell_id"], signals["value"], strict=True):
        totals[cell] += float(value)
    writers.write_terminal_primitive(
        "reduced",
        {
            "kind": "cell_vector",
            "dimensions": [len(cell_order)],
            "cell_id": cell_order,
            "value": [totals[cell] for cell in cell_order],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _ancestry_probe(readers, writers, params, context):
    readers["signals"]()
    ancestry = readers["ancestry"]()
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"artifact_id": ancestry["artifact_id"]}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _session_axis_probe(readers, writers, params, context):
    cells = readers["session_cells"]()["cell_id"]
    writers.write_terminal_primitive(
        "labels",
        {
            "kind": "cell_vector",
            "dimensions": [len(cells)],
            "cell_id": cells,
            "value": [f"label_{index}" for index, _ in enumerate(cells, start=1)],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _feature_annotation_probe(readers, writers, params, context):
    readers["matrix"]()
    annotations = readers["feature_annotations"]()
    rows = [
        {
            "feature_id": row["feature_id"],
            "feature_type": row["values"]["feature_type"],
        }
        for row in annotations["rows"]
    ]
    writers.write_terminal_primitive(
        "annotations",
        {
            "kind": "feature_table",
            "dimensions": [len(rows), 2],
            "feature_id": [row["feature_id"] for row in rows],
            "rows": rows,
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _bounded_group_probe(readers, writers, params, context):
    cells = readers["session_cells"]()["cell_id"]
    groups = {
        "mitochondrial": ["mito_counts", "mito_percent"],
        "ribosomal": ["ribo_counts", "ribo_percent"],
    }
    keys = ["total_counts", "detected_features"]
    for name in ("mitochondrial", "ribosomal"):
        if name in params["optional_groups"]:
            keys.extend(groups[name])
    writers.write_terminal_primitive(
        "metrics",
        {
            "kind": "cell_vector_family",
            "dimensions": [len(keys) * len(cells), 3],
            "key": [key for key in keys for _ in cells],
            "cell_id": [cell for _ in keys for cell in cells],
            "value": [float(index) for index, _ in enumerate(keys) for _ in cells],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )


def _matrix_fragment_probe(readers, writers, params, context):
    capability = context["typed_slice_reads"]["matrix_chunks"]
    features = capability["features"]
    cells = capability["cells"]
    writer = writers.open_matrix_fragments("normalized")
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"matrix_id": params["output_matrix_id"]}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
    )
    for source in writer.source_fragments:
        start = source["cell_start"] - 1
        selected = cells[start : start + source["cell_count"]]
        matrix = readers["matrix_chunks"](features=features, cells=selected)
        if os.environ.get("SCWBPYV3_MATRIX_FRAGMENT_FAULT") == "second" and source["ordinal"] == 2:
            raise RuntimeError("synthetic second-fragment failure")
        writer.write_fragment(
            source["ordinal"],
            matrix["values"],
            matrix["feature_id"],
            matrix["cell_id"],
        )
    writer.finish({"fixture": "matrix_fragment_probe"})


def _parameter_axis_probe(readers, writers, params, context):
    capability = context["typed_slice_reads"]["matrix_chunks"]
    cells = capability["cells"]
    features = capability["features"]
    matrix = readers["matrix_chunks"](features=features, cells=cells)
    count = params["n_components"]
    labels = [f"PC{index}" for index in range(1, count + 1)]
    fault = os.environ.get("SCWBPYV3_PARAMETER_AXIS_FAULT", "")
    output_cells = cells[:-1] if fault == "missing_cell" else cells
    if fault == "two_columns":
        output_labels = labels[:2]
    elif fault == "four_columns":
        output_labels = [*labels, "PC4"]
    elif fault == "duplicate_dimension":
        output_labels = [labels[0], labels[0], *labels[2:]]
    else:
        output_labels = labels
    writers.write_space(
        "space",
        {
            "kind": "space",
            "dimensions": [len(output_cells), len(output_labels)],
            "cell_id": output_cells,
            "dimension_id": output_labels,
            "values": [
                [
                    float(matrix["values"][0][index]) + component
                    for component in range(len(output_labels))
                ]
                for index in range(len(output_cells))
            ],
        },
    )
    loading_rows = [
        {
            "feature_id": feature,
            "PC": label,
            "component_index": index,
            "loading": float(feature_index + index),
        }
        for index, label in enumerate(labels, start=1)
        for feature_index, feature in enumerate(features, start=1)
    ]
    writers.write_terminal_primitive(
        "loadings",
        {
            "kind": "feature_table",
            "dimensions": [len(loading_rows), 4],
            "feature_id": [row["feature_id"] for row in loading_rows],
            "rows": loading_rows,
        },
    )
    writers.write_terminal_primitive(
        "variance",
        {
            "kind": "obs_table",
            "dimensions": [count, 3],
            "rows": [
                {"PC": label, "component_index": index, "variance": float(index)}
                for index, label in enumerate(labels, start=1)
            ],
        },
    )
    if fault != "partial_output":
        writers.write_terminal_primitive(
            "summary",
            {
                "kind": "obs_table",
                "dimensions": [1, 1],
                "rows": [{"n_components": count}],
            },
        )


def _contained_model_fit_probe(readers, writers, params, context):
    space = readers["space"]()
    model = writers.open_model_tree("model")
    model.write_bytes("model.bin", b"synthetic-contained-model")
    model.write_bytes("state/axes.txt", ",".join(space["dimension_id"]).encode("utf-8"))
    model.finish(
        {
            "kind": "model_ref",
            "in_space_id": space["space_id"],
        },
        {"fixture": "contained_model_fit_probe"},
    )
    writers.write_space_2d(
        "embedding",
        {
            "kind": "space_2d",
            "dimensions": [len(space["cell_id"]), 2],
            "cell_id": list(space["cell_id"]),
            "values": [list(row[:2]) for row in space["values"]],
        },
    )


def _contained_model_transform_probe(readers, writers, params, context):
    space = readers["space"]()
    model = readers["model_payload"]()
    if model.read_bytes(model.primary_path) != b"synthetic-contained-model":
        raise RuntimeError("synthetic model payload differs")
    writers.write_space_2d(
        "embedding",
        {
            "kind": "space_2d",
            "dimensions": [len(space["cell_id"]), 2],
            "cell_id": list(space["cell_id"]),
            "values": [list(row[:2]) for row in space["values"]],
        },
    )
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"projected_cells": len(space["cell_id"])}],
        },
    )


def _self_linked_model_read_probe(readers, writers, params, context):
    payload = b""
    for _call in range(params.get("reader_calls", 1)):
        model = readers["fit_model"]()
        payload = model.read_bytes(model.primary_path)
    writers.write_terminal_primitive(
        "summary",
        {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"model_bytes": len(payload)}],
        },
    )


def _sibling_model_fit_probe(readers, writers, params, context):
    space = readers["fit_space"]()
    model = writers.open_model_tree("model")
    model.write_bytes("model.bin", b"synthetic-sibling-model")
    model.finish(
        {"kind": "model_ref", "in_space_id": space["space_id"]},
        {"fixture": "sibling_model_fit_probe"},
    )
    cells = list(space["cell_id"])
    if params.get("omit_terminal", "none") != "pseudotime":
        writers.write_terminal_primitive(
            "pseudotime",
            {
                "kind": "cell_vector",
                "dimensions": [len(cells)],
                "cell_id": cells,
                "value": [float(index) for index in range(len(cells))],
            },
        )
    if params.get("omit_terminal", "none") != "entropy":
        writers.write_terminal_primitive(
            "entropy",
            {
                "kind": "cell_vector",
                "dimensions": [len(cells)],
                "cell_id": cells,
                "value": [0.5 for _cell in cells],
            },
        )
    if params.get("omit_terminal", "none") != "summary":
        writers.write_terminal_primitive(
            "summary",
            {
                "kind": "obs_table",
                "dimensions": [1, 1],
                "rows": [{"fitted_cells": len(cells)}],
            },
        )


def register():
    return operation_pack_handlers_v3(
        {
            "scwbpyv3reference::all_contracts": _all_contracts,
            "scwbpyv3reference::dynamic_provider": _dynamic_provider,
            "scwbpyv3reference::chunk_probe": _chunk_probe,
            "scwbpyv3reference::cell_chunk_probe": _cell_chunk_probe,
            "scwbpyv3reference::zero_axis_probe": _zero_axis_probe,
            "scwbpyv3reference::cardinality_family_probe": _cardinality_family_probe,
            "scwbpyv3reference::cardinality_reduction_probe": _cardinality_reduction_probe,
            "scwbpyv3reference::ancestry_probe": _ancestry_probe,
            "scwbpyv3reference::session_axis_probe": _session_axis_probe,
            "scwbpyv3reference::feature_annotation_probe": _feature_annotation_probe,
            "scwbpyv3reference::bounded_group_probe": _bounded_group_probe,
            "scwbpyv3reference::matrix_fragment_probe": _matrix_fragment_probe,
            "scwbpyv3reference::parameter_axis_probe": _parameter_axis_probe,
            "scwbpyv3reference::contained_model_fit_probe": _contained_model_fit_probe,
            "scwbpyv3reference::contained_model_transform_probe": _contained_model_transform_probe,
            "scwbpyv3reference::self_linked_model_read_probe": _self_linked_model_read_probe,
            "scwbpyv3reference::sibling_model_fit_probe": _sibling_model_fit_probe,
            "scwbpyv3reference::family_probe": _family_probe,
            "scwbpyv3reference::registry_probe": _registry_probe,
            "scwbpyv3reference::registry_probe_strict": _registry_probe,
        }
    )
