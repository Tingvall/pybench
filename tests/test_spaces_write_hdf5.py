from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import pytest

from capability_helpers import payload_allowance, raw_write_bundle, sqlite_allowance
from pybench.bundle import close_bundle
from pybench.capability import build_write_manifest
from pybench.protocol import ok_receipt
from pybench.spaces_write import (
    CONTENT_IDENTITY_SCHEME,
    SPACE_SCHEMA,
    SPACE_STORAGE,
    SpaceWriteError,
    add_space,
)


def _bundle(root: Path, space_id: str = "latent", *, enforce: bool = True):
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db/app.sqlite")
    con.executescript(
        """
        CREATE TABLE cells(cell_id TEXT PRIMARY KEY);
        CREATE TABLE spaces(space_id TEXT PRIMARY KEY, source TEXT, dims INTEGER, storage TEXT, description TEXT);
        CREATE TABLE space_payloads(
          space_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL,
          n_cells INTEGER NOT NULL, n_dims INTEGER NOT NULL, cell_ids_sha256 TEXT NOT NULL,
          dim_labels_json TEXT NOT NULL, dtype TEXT NOT NULL, endianness TEXT NOT NULL,
          chunk_rows INTEGER NOT NULL, chunk_dims INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
          content_identity_scheme TEXT NOT NULL, provenance_identity TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO cells VALUES('café'),('细胞β'),('c3');
        """
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    allowances = [
        sqlite_allowance("space_registry", space_id, "space", "spaces", "space_id"),
        sqlite_allowance("space_registry", space_id, "space", "space_payloads", "space_id"),
        payload_allowance("space_registry", space_id, "space", "space_payloads"),
    ]
    return raw_write_bundle(root, allowances, enforce=enforce)


@pytest.mark.parametrize("n_dims", [2, 30, 50])
def test_file_backed_dense_space_layout_registry_and_receipt(tmp_path: Path, n_dims: int):
    bundle = _bundle(tmp_path / f"d{n_dims}")
    labels = [f"轴{index}" for index in range(n_dims)]
    coords = pd.DataFrame(
        np.arange(3 * n_dims, dtype=np.float64).reshape(3, n_dims),
        index=["café", "细胞β", "c3"],
        columns=labels,
    )
    try:
        result = add_space(bundle, "latent", coords, source="test", provenance_identity="prov:1")
        row = bundle.con.execute("SELECT * FROM space_payloads WHERE space_id='latent'").fetchone()
        path = bundle.bundle_dir / row["path"]
        with h5py.File(path, "r") as handle:
            assert set(handle) == {"coordinates", "cell_ids", "dim_labels"}
            # rhdf5's matrix order is the reverse of HDF5's C-order dimensions.
            assert handle["coordinates"].shape == (n_dims, 3)
            assert handle["coordinates"].chunks == (min(n_dims, 16), 3)
            assert np.array_equal(handle["coordinates"][:].T, coords.to_numpy())
            assert handle["cell_ids"].asstr()[:].tolist() == ["café", "细胞β", "c3"]
            assert handle["dim_labels"].asstr()[:].tolist() == labels
            assert handle.attrs["schema_version"] == SPACE_SCHEMA
            assert handle.attrs["space_id"] == "latent"
            assert handle.attrs["dtype"] == "float64"
        assert result["storage"] == SPACE_STORAGE
        assert row["content_identity_scheme"] == CONTENT_IDENTITY_SCHEME
        assert row["provenance_identity"] == "prov:1"
        assert (
            row["cell_ids_sha256"]
            == "afddad36bf2f484778ba12e6fcc82677225dc6b22c32bde41442d655d806ecc9"
        )
        assert row["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = build_write_manifest(bundle)
        tables = [item.get("table") for item in manifest if "table" in item]
        paths = [item.get("path") for item in manifest if "path" in item]
        assert tables == ["spaces", "space_payloads"]
        assert "space_payloads" in paths and row["path"] in paths
        assert not any("pending" in str(value) for value in paths)
        assert not any(
            value in tables for value in ("space_2d", "space_coords", "artifact_space_2d")
        )
    finally:
        close_bundle(bundle)


@pytest.mark.parametrize(
    "coords,cell_ids,labels,error",
    [
        ([[1.0], [2.0]], ["café", "café"], None, "unique"),
        ([[1.0], [2.0]], ["", "c3"], None, "non-empty"),
        ([[1.0], [2.0]], ["café"], None, "count"),
        ([[1.0], [np.inf]], ["café", "c3"], None, "Inf"),
        ([[1.0, 2.0], [2.0, 3.0]], ["café", "c3"], ["x", "x"], "unique"),
        (np.array([[1 + 2j], [3 + 4j]]), ["café", "c3"], None, "numeric"),
    ],
)
def test_dense_space_input_rejections(tmp_path: Path, coords, cell_ids, labels, error: str):
    bundle = _bundle(tmp_path / "bad")
    try:
        with pytest.raises(SpaceWriteError, match=error):
            add_space(bundle, "latent", coords, cell_id=cell_ids, dim_labels=labels)
        assert bundle.con.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
        assert not (bundle.bundle_dir / "space_payloads").exists()
        assert bundle.write_journal == []
    finally:
        close_bundle(bundle)


def test_missing_or_malformed_registry_schema_fails_without_payload(tmp_path: Path):
    bundle = _bundle(tmp_path / "schema", enforce=False)
    try:
        bundle.con.execute("ALTER TABLE space_payloads RENAME TO broken_space_payloads")
        with pytest.raises((SpaceWriteError, sqlite3.DatabaseError)):
            add_space(bundle, "latent", [[1.0], [2.0], [3.0]], cell_id=["café", "细胞β", "c3"])
        assert not (bundle.bundle_dir / "space_payloads").exists()
    finally:
        close_bundle(bundle)


def test_production_writer_source_has_no_legacy_coordinate_mutation():
    source = (Path(__file__).parents[1] / "src/pybench/spaces_write.py").read_text()
    forbidden = ("INSERT INTO space_2d", "INSERT INTO space_coords", "artifact_space_2d")
    assert not any(value in source for value in forbidden)


def test_receipt_construction_failure_rolls_back_space_publication(tmp_path: Path, monkeypatch):
    bundle = _bundle(tmp_path / "receipt")
    root = bundle.bundle_dir
    add_space(
        bundle,
        "latent",
        [[1.0], [2.0], [3.0]],
        cell_id=["café", "细胞β", "c3"],
    )
    monkeypatch.setattr(
        "pybench.protocol.build_write_manifest",
        lambda _bundle: (_ for _ in ()).throw(RuntimeError("receipt fault")),
    )
    with pytest.raises(RuntimeError, match="receipt fault"):
        ok_receipt(
            op_name="test",
            run_id="run_test",
            outputs={},
            request={},
            bundle=bundle,
        )
    close_bundle(bundle)
    con = sqlite3.connect(root / "db/app.sqlite")
    try:
        assert con.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM space_payloads").fetchone()[0] == 0
    finally:
        con.close()
    assert not (root / "space_payloads").exists()
