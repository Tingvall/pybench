import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import io as spio
from scipy import sparse as sps

from capability_helpers import payload_allowance, sqlite_allowance, v2_request
from pybench.bundle import BundlePathError
from pybench.ops import peakvi_fit


def test_train_peakvi_wires_script_parity_kwargs(monkeypatch):
    captured = {}

    class FakeModelInstance:
        def train(self, **kwargs):
            captured["train_kwargs"] = kwargs

        def get_latent_representation(self):
            return np.array([[1.0, 2.0], [3.0, 4.0]], dtype=float)

    class FakePeakVI:
        @staticmethod
        def setup_anndata(adata, batch_key=None):
            captured["setup_anndata"] = {"adata": adata, "batch_key": batch_key}

        def __new__(cls, adata, **kwargs):
            captured["init_kwargs"] = {"adata": adata, **kwargs}
            return FakeModelInstance()

    class FakeScvi:
        __version__ = "test-scvi"

        class settings:
            seed = None

        class model:
            PEAKVI = FakePeakVI

    monkeypatch.setattr(peakvi_fit, "_import_scvi", lambda: (object(), FakeScvi))

    model, latent, version = peakvi_fit._train_peakvi(
        adata="adata-object",
        batch_key="batch_col",
        n_latent=20,
        n_layers=2,
        n_hidden=256,
        dropout=0.2,
        max_epochs=500,
        lr=1e-3,
        early_stopping=True,
        seed=7,
        use_gpu=False,
    )

    assert isinstance(model, FakeModelInstance)
    assert version == "test-scvi"
    assert latent.shape == (2, 2)
    assert captured["setup_anndata"]["batch_key"] == "batch_col"
    assert captured["init_kwargs"]["n_latent"] == 20
    assert captured["init_kwargs"]["n_layers_encoder"] == 2
    assert captured["init_kwargs"]["n_layers_decoder"] == 2
    assert captured["init_kwargs"]["n_hidden"] == 256
    assert captured["init_kwargs"]["dropout_rate"] == 0.2
    assert captured["train_kwargs"]["max_epochs"] == 500
    assert captured["train_kwargs"]["early_stopping"] is True
    assert captured["train_kwargs"]["lr"] == 1e-3
    assert captured["train_kwargs"]["accelerator"] == "cpu"
    assert captured["train_kwargs"]["devices"] == 1


def test_build_anndata_reads_bundle_native_matrix(monkeypatch):
    captured = {}

    class FakeAnnData:
        def __init__(self, X, obs, var):
            captured["X"] = X
            captured["obs"] = obs
            captured["var"] = var
            self.X = X
            self.obs = obs
            self.var = var

    monkeypatch.setattr(
        peakvi_fit, "_import_scvi", lambda: (type("AD", (), {"AnnData": FakeAnnData}), object())
    )
    monkeypatch.setattr(
        peakvi_fit,
        "get_matrix",
        lambda bundle, matrix_id, cells_order: (
            sps.csc_matrix(np.array([[1.0, 0.0], [0.0, 2.0], [3.0, 4.0]], dtype=float)),
            ["f1", "f2", "f3"],
            ["c2", "c1"],
        ),
    )
    monkeypatch.setattr(
        peakvi_fit,
        "get_meta",
        lambda bundle, cells_order: pd.DataFrame(
            {"cell_id": ["c2", "c1"], "batch": ["b", "a"]},
        ),
    )

    adata, batch_key = peakvi_fit._build_anndata(
        req_inputs={"matrix": "ATAC_counts", "cell_ids": ["c2", "c1"]},
        req_params={"matrix_id": "ATAC_counts", "batch_key": "batch"},
        bundle="bundle-handle",
    )

    assert batch_key == "batch"
    assert adata.X.shape == (2, 3)
    assert list(adata.obs.index) == ["c2", "c1"]
    assert list(adata.var.index) == ["f1", "f2", "f3"]


def test_build_anndata_reads_staged_matrix_chunks(tmp_path, monkeypatch):
    captured = {}

    class FakeAnnData:
        def __init__(self, X, obs, var):
            captured["X"] = X
            captured["obs"] = obs
            captured["var"] = var
            self.X = X
            self.obs = obs
            self.var = var

    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (stage_dir / "feature_ids.txt").write_text("f1\nf2\nf3\n")
    (stage_dir / "chunk_00001_cells.txt").write_text("c2\n")
    (stage_dir / "chunk_00002_cells.txt").write_text("c1\n")
    spio.mmwrite(
        stage_dir / "chunk_00001.mtx", sps.csc_matrix(np.array([[1.0], [0.0], [3.0]], dtype=float))
    )
    spio.mmwrite(
        stage_dir / "chunk_00002.mtx", sps.csc_matrix(np.array([[0.0], [2.0], [4.0]], dtype=float))
    )
    (stage_dir / "manifest.json").write_text(
        '{"matrix_id":"ATAC_counts","n_features":3,"n_cells":2,"chunks":['
        '{"mtx":"chunk_00001.mtx","cells":"chunk_00001_cells.txt","n_cells":1},'
        '{"mtx":"chunk_00002.mtx","cells":"chunk_00002_cells.txt","n_cells":1}'
        "]}"
    )

    monkeypatch.setattr(
        peakvi_fit, "_import_scvi", lambda: (type("AD", (), {"AnnData": FakeAnnData}), object())
    )
    monkeypatch.setattr(
        peakvi_fit,
        "get_meta",
        lambda bundle, cells_order: pd.DataFrame(
            {"cell_id": ["c2", "c1"], "batch": ["b", "a"]},
        ),
    )

    adata, batch_key = peakvi_fit._build_anndata(
        req_inputs={
            "matrix": "ATAC_counts",
            "cell_ids": ["c2", "c1"],
            "matrix_chunks_dir": str(stage_dir),
        },
        req_params={"matrix_id": "ATAC_counts", "batch_key": "batch"},
        bundle="bundle-handle",
    )

    assert batch_key == "batch"
    assert adata.X.shape == (2, 3)
    assert list(adata.obs.index) == ["c2", "c1"]
    assert list(adata.var.index) == ["f1", "f2", "f3"]


@pytest.mark.parametrize("chunk_path", ["../outside.mtx", "/tmp/outside.mtx", "C:\\outside.mtx"])
def test_staged_peakvi_chunk_paths_reject_escape_before_matrix_read(
    tmp_path, monkeypatch, chunk_path
):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "feature_ids.txt").write_text("f1\n")
    (stage / "cells.txt").write_text("c1\n")
    (stage / "manifest.json").write_text(
        '{"chunks":[{"mtx":' + repr(chunk_path).replace("'", '"') + ',"cells":"cells.txt"}]}'
    )
    touched = False

    def forbidden_mmread(_path):
        nonlocal touched
        touched = True
        raise AssertionError("matrix bytes must not be read")

    monkeypatch.setattr(spio, "mmread", forbidden_mmread)
    with pytest.raises(BundlePathError):
        peakvi_fit._build_matrix_from_staged_chunks({"matrix_chunks_dir": str(stage)})
    assert touched is False


def test_staged_peakvi_chunk_rejects_symlink_escape_before_matrix_read(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "feature_ids.txt").write_text("f1\n")
    (stage / "cells.txt").write_text("c1\n")
    outside = tmp_path / "outside.mtx"
    outside.write_text("must not be read")
    (stage / "chunk.mtx").symlink_to(outside)
    (stage / "manifest.json").write_text('{"chunks":[{"mtx":"chunk.mtx","cells":"cells.txt"}]}')
    touched = False

    def forbidden_mmread(_path):
        nonlocal touched
        touched = True
        raise AssertionError("matrix bytes must not be read")

    monkeypatch.setattr(spio, "mmread", forbidden_mmread)
    with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
        peakvi_fit._build_matrix_from_staged_chunks({"matrix_chunks_dir": str(stage)})
    assert touched is False


def test_peakvi_fit_persists_relative_contained_model_path(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db" / "app.sqlite")
    con.executescript(
        """
        CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE artifact_model_ref (
          artifact_id TEXT PRIMARY KEY, engine TEXT, model_space_id TEXT,
          in_space_id TEXT, model_type TEXT, model_path TEXT
        );
        INSERT INTO artifacts VALUES ('peakvi_model', 'model_ref');
        """
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    (root / "artifact_models").mkdir()

    class FakeAdata:
        obs_names = pd.Index(["c1", "c2"])

    class FakeModel:
        def save(self, path, **_kwargs):
            target = Path(path)
            target.mkdir(parents=True)
            (target / "model.pt").write_bytes(b"peakvi")

    monkeypatch.setattr(peakvi_fit, "_build_anndata", lambda *_args: (FakeAdata(), None))
    monkeypatch.setattr(
        peakvi_fit,
        "_train_peakvi",
        lambda **_kwargs: (FakeModel(), np.ones((2, 2), dtype=float), "test-scvi"),
    )
    monkeypatch.setattr(peakvi_fit, "add_space", lambda *_args, **_kwargs: {})
    outputs = {"model": {"artifact_id": "peakvi_model", "type": "model_ref"}}
    request = v2_request(
        root,
        op_name="peakvi_fit",
        run_id="fit-relative",
        inputs={"matrix": "ATAC", "cell_ids": ["c1", "c2"]},
        params={"matrix_id": "ATAC", "n_latent": 2},
        outputs=outputs,
        allowances=[
            sqlite_allowance(
                "model", "peakvi_model", "model_ref", "artifact_model_ref", "artifact_id"
            ),
            payload_allowance("model", "peakvi_model", "model_ref", "artifact_models/peakvi_model"),
        ],
    )
    receipt = peakvi_fit._run(request)
    assert receipt["status"] == "ok"
    con = sqlite3.connect(root / "db" / "app.sqlite")
    try:
        stored = con.execute("SELECT model_path FROM artifact_model_ref").fetchone()[0]
    finally:
        con.close()
    assert stored == "artifact_models/peakvi_model/peakvi"
    assert not Path(stored).is_absolute()
    assert (root / stored / "model.pt").exists()
