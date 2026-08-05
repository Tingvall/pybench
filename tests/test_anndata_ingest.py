from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import anndata
import numpy as np
import pandas as pd

from capability_helpers import raw_write_bundle, sqlite_allowance
from pybench.bundle import close_bundle
from pybench.getters import cells, feature_ids, matrix, matrix_info, meta, space, spaces
from pybench.ingest.anndata import ingest_anndata_stage


def _init_min_bundle(root: Path) -> None:
    (root / "db").mkdir(parents=True, exist_ok=True)
    (root / "mat").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "db" / "app.sqlite"))
    try:
        con.execute("CREATE TABLE datasets (dataset_id TEXT PRIMARY KEY, n_cells INTEGER)")
        con.execute("CREATE TABLE cells (cell_id TEXT PRIMARY KEY, dataset_id TEXT)")
        con.execute(
            "CREATE TABLE cell_set_membership (cell_set TEXT NOT NULL, cell_id TEXT NOT NULL, source TEXT, created_at TEXT NOT NULL, created_by TEXT, description TEXT, PRIMARY KEY (cell_set, cell_id))"
        )
        con.execute(
            "CREATE TABLE spaces (space_id TEXT PRIMARY KEY, source TEXT, dims INTEGER, storage TEXT, description TEXT)"
        )
        con.execute(
            "CREATE TABLE space_coords (space_id TEXT, cell_id TEXT, dim INTEGER, value REAL, PRIMARY KEY (space_id, cell_id, dim))"
        )
        con.execute(
            "CREATE TABLE space_2d (space_id TEXT, cell_id TEXT, x REAL, y REAL, PRIMARY KEY (space_id, cell_id))"
        )
        con.commit()
    finally:
        con.close()
    manifest = {
        "bundle_id": root.name,
        "bundle_name": root.name,
        "storage": {"sqlite": {"path": "db/app.sqlite"}},
        "datasets": {},
        "matrices": {},
        "spaces": {},
        "cell_sets": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))


def test_ingest_anndata_stage_obs_and_obsm(tmp_path: Path):
    bundle_dir = tmp_path / "mini_bundle"
    _init_min_bundle(bundle_dir)

    ad = anndata.AnnData(
        X=np.array([[1.0, 0.0], [0.0, 2.0], [3.0, 4.0]], dtype=float),
        obs=pd.DataFrame(
            {"batch": ["a", "a", "b"], "score": [1.0, 2.0, 3.0]}, index=["c1", "c2", "c3"]
        ),
        var=pd.DataFrame(index=["g1", "g2"]),
    )
    ad.obsm["X_umap"] = np.array([[0.0, 0.0], [1.0, 1.5], [2.0, 0.5]], dtype=float)
    ad.obsm["X_pca"] = np.array([[1.0, 2.0, 3.0], [1.5, 2.5, 3.5], [2.0, 3.0, 4.0]], dtype=float)
    h5ad = tmp_path / "demo.h5ad"
    ad.write_h5ad(h5ad)

    allowances = []
    for space_id in ("umap", "pca"):
        allowances.extend(
            [
                sqlite_allowance(space_id, space_id, "space", "spaces", "space_id"),
                sqlite_allowance(
                    space_id,
                    space_id,
                    "space",
                    "space_2d" if space_id == "umap" else "space_coords",
                    "space_id",
                ),
            ]
        )
    b = raw_write_bundle(bundle_dir, allowances)
    try:
        out = ingest_anndata_stage(
            b,
            h5ad,
            define_cells_stage=True,
            dataset_id="demo_ds",
            set_name="core",
            space_map={"X_umap": "umap", "X_pca": "pca"},
        )
        assert out["cells"]["n_defined"] == 3
        assert out["matrix_writer"] == "skipped"
        assert cells(b, cell_set="core") == ["c1", "c2", "c3"]

        md = meta(b, cols=["cell_id", "dataset_id", "batch", "score"])
        assert md["cell_id"].tolist() == ["c1", "c2", "c3"]
        assert md["dataset_id"].tolist() == ["demo_ds", "demo_ds", "demo_ds"]
        assert md["batch"].tolist() == ["a", "a", "b"]

        sp = spaces(b)
        assert set(sp["space_id"].tolist()) == {"umap", "pca"}
        umap = space(b, "umap")
        assert umap["cell_id"].tolist() == ["c1", "c2", "c3"]
        pca = space(b, "pca", cells_order=["c3", "c1"], dims=[1, 3])
        mat, cell_order, dims = pca
        assert cell_order == ["c3", "c1"]
        assert dims == [1, 3]
        assert mat.shape == (2, 2)
    finally:
        close_bundle(b)


def test_space_getter_uses_canonical_present_cell_order_when_omitted(tmp_path: Path):
    bundle_dir = tmp_path / "ordered_space_bundle"
    _init_min_bundle(bundle_dir)
    con = sqlite3.connect(str(bundle_dir / "db" / "app.sqlite"))
    try:
        con.executemany(
            "INSERT INTO cells(cell_id, dataset_id) VALUES(?, 'ds')",
            [("cell_z",), ("cell_a",), ("cell_m",), ("cell_absent",)],
        )
        con.execute(
            "INSERT INTO spaces(space_id, source, dims, storage, description) VALUES('ordered', 'test', 2, 'wide:space_2d', 'test')"
        )
        con.executemany(
            "INSERT INTO space_2d(space_id, cell_id, x, y) VALUES('ordered', ?, ?, ?)",
            [("cell_m", 3.0, 30.0), ("cell_z", 1.0, 10.0), ("cell_a", 2.0, 20.0)],
        )
        con.commit()
    finally:
        con.close()

    b = raw_write_bundle(bundle_dir)
    try:
        got = space(b, "ordered")
        assert got["cell_id"].tolist() == ["cell_z", "cell_a", "cell_m"]
        assert got["x"].tolist() == [1.0, 2.0, 3.0]
    finally:
        close_bundle(b)


def test_ingest_anndata_stage_writes_integer_matrix(tmp_path: Path):
    bundle_dir = tmp_path / "mini_bundle"
    _init_min_bundle(bundle_dir)

    ad = anndata.AnnData(
        X=np.array([[1, 0], [0, 2], [3, 4]], dtype=np.uint32),
        obs=pd.DataFrame(index=["c1", "c2", "c3"]),
        var=pd.DataFrame(index=["g1", "g2"]),
    )
    h5ad = tmp_path / "demo_matrix.h5ad"
    ad.write_h5ad(h5ad)

    b = raw_write_bundle(bundle_dir)
    try:
        out = ingest_anndata_stage(
            b,
            h5ad,
            define_cells_stage=True,
            dataset_id="demo_ds",
            matrix_id="RNA_counts",
            write_matrix=True,
        )
        assert out["matrix_writer"] == "bpcells"
        assert out["matrix"]["n_features"] == 2
        assert out["matrix"]["n_cells"] == 3

        mi = matrix_info(b, "RNA_counts")
        assert mi.iloc[0]["storage"] == "bpcells"
        assert feature_ids(b, "RNA_counts") == ["g1", "g2"]

        m, feats, cids = matrix(
            b, "RNA_counts", cells_order=["c3", "c1"], feature_order=["g2", "g1"]
        )
        assert feats == ["g2", "g1"]
        assert cids == ["c3", "c1"]
        assert m.toarray().tolist() == [[4, 0], [3, 1]]
    finally:
        close_bundle(b)
