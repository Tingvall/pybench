from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from .artifacts_payload import (
    publish_artifact_model_payload,
    write_artifact_cell_vector_payload,
    write_artifact_path_2d_family_payload,
    write_artifact_relation_family_payload,
    write_artifact_table_payload,
)
from .bundle import BundleHandle
from .getters import cells as get_cells
from .spaces_write import add_space


def enabled() -> bool:
    return os.environ.get("PYBENCH_TEST_MOCK_TRAINING") == "1"


def run_mock_training_boundary(
    request: dict[str, Any], bundle: BundleHandle
) -> dict[str, Any]:
    """Exercise real v2 writers while replacing only expensive model computation."""

    outputs = request["outputs"]
    params = request.get("params", {})
    inputs = request.get("inputs", {})
    cell_set = str(inputs.get("cell_set", "core"))
    cell_ids = get_cells(bundle, cell_set=cell_set)
    if not cell_ids:
        raise RuntimeError("test training boundary requires at least one cell")
    result: dict[str, Any] = {}

    space_id = str(params.get("output_space_id") or params.get("space_id_out") or "")
    if space_id:
        dims = max(2, int(params.get("n_latent", params.get("latent_dim", 2))))
        coords = np.asarray(
            [[float(row + col) for col in range(dims)] for row in range(len(cell_ids))]
        )
        summary = add_space(
            bundle,
            space_id,
            coords,
            cell_id=cell_ids,
            source="pybench_test_training_boundary",
        )
        for name, spec in outputs.items():
            if spec.get("type") == "space":
                result[name] = {
                    "artifact_id": spec["artifact_id"],
                    "type": "space",
                    **summary,
                }

    for name, spec in outputs.items():
        artifact_id = str(spec["artifact_id"])
        kind = str(spec["type"])
        if kind == "space":
            continue
        if kind == "cell_vector":
            result[name] = write_artifact_cell_vector_payload(
                bundle,
                artifact_id,
                cell_ids,
                [float(index) for index in range(len(cell_ids))],
                value_type="continuous",
            )
        elif kind == "relation_family":
            result[name] = write_artifact_relation_family_payload(
                bundle,
                artifact_id,
                ["branch:mock"] * len(cell_ids),
                cell_ids,
                weight=[1.0] * len(cell_ids),
            )
        elif kind in {"obs_table", "feature_table"}:
            result[name] = write_artifact_table_payload(
                bundle,
                artifact_id,
                artifact_type=kind,
                table_rows=[
                    {
                        "status": "ok",
                        "metric": "mock_training",
                        "value": float(len(cell_ids)),
                    }
                ],
                table_type="fit_summary",
                entity_kind="run_summary",
                meta={"test_boundary": True},
            )
        elif kind == "path_2d_family":
            n_points = max(3, len(cell_ids))
            result[name] = write_artifact_path_2d_family_payload(
                bundle,
                artifact_id,
                ["branch:mock"] * n_points,
                list(range(1, n_points + 1)),
                [float(index) for index in range(n_points)],
                [float(index) / 2 for index in range(n_points)],
            )
        elif kind == "model_ref":
            model_space_id = space_id or str(inputs.get("fit_space") or "mock_space")
            relative = f"artifact_models/{artifact_id}/mock"
            engine = {
                "scvi_train": "scvi",
                "peakvi_fit": "peakvi",
                "palantir_fit": "palantir",
            }.get(request["op_name"], request["op_name"])

            def writer(path: Path) -> None:
                path.mkdir(parents=True)
                (path / "model.json").write_text(
                    json.dumps(
                        {
                            "engine": engine,
                            "run_id": request["run"]["run_id"],
                            "cell_ids": cell_ids,
                        },
                        sort_keys=True,
                    )
                )
                (path / "weights").mkdir()
                (path / "weights" / "weights.bin").write_bytes(b"mock-training-weights-v2")

            result[name] = publish_artifact_model_payload(
                bundle,
                artifact_id,
                engine=engine,
                model_space_id=model_space_id,
                model_path=relative,
                writer=writer,
                expected_type="directory",
            )
    return result
