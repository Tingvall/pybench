from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from pybench.artifacts_payload import write_artifact_cell_vector_payload
from pybench.bundle import close_bundle, open_bundle
from pybench.getters import art_get_row, art_list


GOLDEN_BUNDLE = Path(
    "/home/dirk/Projects/lymphocyte_trajectory/fixtures/golden_bundle/scwb_golden"
)


def _copy_golden_bundle(tmp_path: Path) -> Path:
    if not GOLDEN_BUNDLE.exists():
        pytest.skip(f"golden bundle fixture not found: {GOLDEN_BUNDLE}")
    dst = tmp_path / "scwb_golden"
    shutil.copytree(GOLDEN_BUNDLE, dst)
    return dst


def test_golden_bundle_cell_vector_artifact_registry_and_payload(tmp_path: Path):
    bundle_dir = _copy_golden_bundle(tmp_path)
    b = open_bundle(bundle_dir)
    try:
        arts = art_list(b, type="cell_vector")
        assert arts.shape[0] == 1

        artifact_id = str(arts.iloc[0]["artifact_id"])
        row = art_get_row(b, artifact_id)
        assert row["artifact_id"] == artifact_id
        assert row["type"] == "cell_vector"
        assert row["source_op"] == "leiden"
        assert row["cell_set"] == "core"
        assert row["provenance_snapshot_id"]

        labels = b.con.execute(
            """
            SELECT cell_id, label, value_type, value_num
            FROM artifact_labels
            WHERE artifact_id = ?
            ORDER BY cell_id
            """,
            (artifact_id,),
        ).fetchall()
        assert len(labels) == 12
        assert sorted({str(r["label"]) for r in labels}) == [
            "golden_A",
            "golden_B",
            "golden_C",
        ]
        assert {str(r["value_type"]) for r in labels} == {"categorical"}
        assert all(r["value_num"] is None for r in labels)
    finally:
        close_bundle(b)


def _init_artifact_bundle(root: Path) -> None:
    (root / "db").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "db" / "app.sqlite"))
    try:
        con.execute("CREATE TABLE cells (cell_id TEXT PRIMARY KEY)")
        con.execute(
            """
            CREATE TABLE artifacts (
              artifact_id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              name TEXT,
              description TEXT,
              created_at TEXT,
              created_by TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE artifact_labels (
              artifact_id TEXT NOT NULL,
              cell_id TEXT NOT NULL,
              label TEXT,
              value_type TEXT,
              value_num REAL,
              PRIMARY KEY (artifact_id, cell_id)
            )
            """
        )
        con.executemany(
            "INSERT INTO cells(cell_id) VALUES (?)",
            [("cell_01",), ("cell_02",), ("cell_03",)],
        )
        con.execute(
            """
            INSERT INTO artifacts(artifact_id, type, name, description, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cell_vector_test",
                "cell_vector",
                "pybench test cell vector",
                "pre-registered artifact row owned by the fixture",
                "2026-06-04T00:00:00+0000",
                "pytest",
            ),
        )
        con.commit()
    finally:
        con.close()

    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}}, indent=2)
    )


def test_write_artifact_cell_vector_payload_matches_scworkbench_shape(tmp_path: Path):
    bundle_dir = tmp_path / "mini_bundle"
    _init_artifact_bundle(bundle_dir)

    b = open_bundle(bundle_dir)
    try:
        receipt = write_artifact_cell_vector_payload(
            b,
            artifact_id="cell_vector_test",
            cell_id=["cell_01", "cell_02", "cell_03"],
            value=["golden_A", "golden_B", "golden_C"],
            value_type="categorical",
        )
        assert receipt == {
            "artifact_id": "cell_vector_test",
            "type": "cell_vector",
            "rows_written": 3,
            "value_type": "categorical",
        }

        rows = b.con.execute(
            """
            SELECT artifact_id, cell_id, label, value_type, value_num
            FROM artifact_labels
            WHERE artifact_id = ?
            ORDER BY cell_id
            """,
            ("cell_vector_test",),
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("cell_vector_test", "cell_01", "golden_A", "categorical", None),
            ("cell_vector_test", "cell_02", "golden_B", "categorical", None),
            ("cell_vector_test", "cell_03", "golden_C", "categorical", None),
        ]
    finally:
        close_bundle(b)
