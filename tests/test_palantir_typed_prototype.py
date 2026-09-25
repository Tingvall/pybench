"""Focused executable proof for private, unregistered Palantir computation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from pybench.prototype.palantir_typed import SCHEMA, TypedPalantirError, compute

ORACLE = Path(__file__).parent / "fixtures" / "phase5_palantir_old_core_oracle_30.json"
NUMERIC_ORACLE = Path(__file__).parent / "fixtures" / "phase5_palantir_old_core_numeric_30.json"


def _cells_and_coordinates() -> tuple[list[str], list[list[float]]]:
    cells = [f"c{i:02d}" for i in range(10)]
    cells += [f"a{i:02d}" for i in range(10)]
    cells += [f"b{i:02d}" for i in range(10)]
    coordinates = []
    for j in range(1, 31):
        i = (j - 1) % 10
        if j <= 10:
            coordinates.append([i / 9, 0.02 * np.sin(j), 0.02 * np.cos(j)])
        else:
            t = (i + 1) / 10
            sign = 1 if j <= 20 else -1
            coordinates.append([1 + t, sign * t + 0.02 * np.sin(j), 0.02 * np.cos(j)])
    return cells, coordinates


def _fit_request() -> dict:
    cells, coordinates = _cells_and_coordinates()
    return {
        "schema": SCHEMA,
        "mode": "fit",
        "cell_ids": cells,
        "coordinates": coordinates,
        "root_cell": "c00",
        "terminal_cells": ["a09", "b09"],
        "params": {
            "dims": [1, 2, 3],
            "knn": 5,
            "n_diffusion_components": 3,
            "n_waypoints": 10,
            "seed": 7,
        },
    }


@pytest.fixture(scope="module")
def fitted(tmp_path_factory):
    stage = tmp_path_factory.mktemp("palantir_private")
    request = _fit_request()
    response = compute(request, stage)
    return stage, request, response


def test_fit_matches_executed_30_cell_old_core_oracle(fitted):
    stage, request, response = fitted
    oracle = json.loads(ORACLE.read_text())
    numeric = json.loads(NUMERIC_ORACLE.read_text())
    assert response["cell_ids"] == request["cell_ids"] == oracle["source_cells"]
    assert len(response["pseudotime"]) == len(response["entropy"]) == 30
    assert all(np.isfinite(response[name]).all() for name in ("pseudotime", "entropy"))
    for name in ("pseudotime", "entropy"):
        np.testing.assert_allclose(response[name], numeric[name], rtol=1e-10, atol=1e-12)
    rows = response["branch_membership"]
    assert len(rows) == oracle["row_count"] == 60
    assert [r["key"] for r in rows] == [r["key"] for r in oracle["ordered_rows"]]
    assert [r["cell_id"] for r in rows] == [r["cell_id"] for r in oracle["ordered_rows"]]
    np.testing.assert_allclose(
        [r["weight"] for r in rows],
        [float.fromhex(r["weight_hex"]) for r in oracle["ordered_rows"]],
        rtol=1e-10,
        atol=1e-12,
    )
    assert list(response["branch_summary"][0]) == [
        "relation_key",
        "branch",
        "n_cells_prob_gt_0",
        "mean_prob",
        "max_prob",
        "top_cell_id",
    ]
    assert [r["relation_key"] for r in response["branch_summary"]] == oracle["ordered_keys"]
    for actual, expected in zip(response["branch_summary"], numeric["branch_summary"]):
        assert actual["relation_key"] == expected["relation_key"]
        assert actual["branch"] == expected["branch"]
        assert actual["n_cells_prob_gt_0"] == expected["n_cells_prob_gt_0"]
        assert actual["top_cell_id"] == expected["top_cell_id"]
        np.testing.assert_allclose(
            [actual["mean_prob"], actual["max_prob"]],
            [expected["mean_prob"], expected["max_prob"]],
            rtol=1e-10,
            atol=1e-12,
        )
    model = response["model"]
    assert model["path"] == "model.pkl"
    data = (stage / model["path"]).read_bytes()
    assert len(data) == model["size_bytes"]
    assert hashlib.sha256(data).hexdigest() == model["sha256"]
    assert response["effective_params"]["seed"] == 7


def test_project_uses_only_stage_local_verified_model_and_typed_coordinates(fitted):
    stage, request, response = fitted
    project = {
        "schema": SCHEMA,
        "mode": "project",
        "cell_ids": request["cell_ids"],
        "coordinates": [row[:2] for row in request["coordinates"]],
        "model": {"path": "model.pkl", "sha256": response["model"]["sha256"]},
        "params": {"sample_points": 20, "min_cells_per_branch": 3},
    }
    result = compute(project, stage)
    assert result["mode"] == "project"
    assert [r["key"] for r in result["curves"]] == ["branch:a09"] * 20 + ["branch:b09"] * 20
    assert [r["point"] for r in result["curves"]] == list(range(1, 21)) * 2
    assert all(np.isfinite([r["x"], r["y"]]).all() for r in result["curves"])
    assert set(result["branch_counts"]) == {"branch:a09", "branch:b09"}
    assert result["effective_params"]["sample_points"] == 20
    numeric = json.loads(NUMERIC_ORACLE.read_text())["curves"]
    assert [r["key"] for r in result["curves"]] == numeric["key"]
    assert [r["point"] for r in result["curves"]] == numeric["point"]
    for axis in ("x", "y"):
        np.testing.assert_allclose(
            [r[axis] for r in result["curves"]], numeric[axis], rtol=1e-10, atol=1e-12
        )
    for mutation, error in [
        ({"model": {"path": "model.pkl", "sha256": "0" * 64}}, "SHA-256 mismatch"),
        ({"model": {"path": "../model.pkl", "sha256": response["model"]["sha256"]}}, "stage-local"),
        ({"cell_ids": list(reversed(project["cell_ids"]))}, "fitted-cell order"),
        ({"bundle_path": "/tmp/forbidden"}, "unknown request fields"),
    ]:
        bad = {**project, **mutation}
        with pytest.raises(TypedPalantirError, match=error):
            compute(bad, stage)
    model_path = stage / "model.pkl"
    original = model_path.read_bytes()
    try:
        model_path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with pytest.raises(TypedPalantirError, match="SHA-256 mismatch"):
            compute(project, stage)
    finally:
        model_path.write_bytes(original)


def test_cli_success_from_frozen_typed_request(tmp_path):
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_bytes(
        (
            Path(__file__).parent / "fixtures" / "phase5_palantir_typed_fit_request_30.json"
        ).read_bytes()
    )
    command = [
        sys.executable,
        "-m",
        "pybench.prototype.palantir_typed",
        "--request",
        str(request_path),
        "--response",
        str(response_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    response = json.loads(response_path.read_text())
    assert response["schema"] == SCHEMA
    assert len(response["branch_membership"]) == 60
    assert (tmp_path / response["model"]["path"]).is_file()


def test_cli_has_no_bundle_authority_and_fails_without_output(tmp_path):
    request = _fit_request()
    request["bundle_path"] = "/tmp/forbidden"
    input_path = tmp_path / "request.json"
    output_path = tmp_path / "response.json"
    input_path.write_text(json.dumps(request))
    command = [
        sys.executable,
        "-m",
        "pybench.prototype.palantir_typed",
        "--request",
        str(input_path),
        "--response",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "unknown request fields" in result.stderr
    assert not output_path.exists()
    assert not (tmp_path / "model.pkl").exists()
