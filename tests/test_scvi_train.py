from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import io as spio
from scipy import sparse as sps

from capability_helpers import payload_allowance, sqlite_allowance, v2_request
from pybench.ops import scvi_train


class FakeAnnData:
    def __init__(self, X, obs, var):
        self.X = X
        self.obs = obs
        self.var = var
        self.obs_names = obs.index
        self.var_names = var.index


def _patch_fake_anndata_only(monkeypatch):
    monkeypatch.setattr(
        scvi_train,
        "_import_scvi",
        lambda: (type("AD", (), {"AnnData": FakeAnnData}), object()),
    )


def _write_stage(root: Path, *, missing_covariate: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "feature_ids.txt").write_text("g1\ng2\ng3\n")
    md = pd.DataFrame(
        {
            "cell_id": ["c3", "c1", "c2"],
            "sample_id": ["s2", "s1", "s1"],
            "pct_mito": ["0.3", "0.1", "0.2"],
        }
    )
    if missing_covariate:
        md = md.drop(columns=["sample_id"])
    md.to_csv(root / "cell_metadata.tsv", sep="\t", index=False)

    (root / "chunk_00001_cells.txt").write_text("c2\nc1\n")
    (root / "chunk_00002_cells.txt").write_text("c3\n")
    spio.mmwrite(root / "chunk_00001.mtx", sps.csc_matrix([[1, 0], [0, 2], [3, 4]]))
    spio.mmwrite(root / "chunk_00002.mtx", sps.csc_matrix([[5], [0], [6]]))
    manifest = {
        "protocol": "scworkbench_sparse_matrix_chunks_v1",
        "matrix_id": "RNA_counts",
        "value_type": "counts",
        "orientation": "features_by_cells",
        "feature_ids_file": "feature_ids.txt",
        "cell_metadata_file": "cell_metadata.tsv",
        "n_features": 3,
        "n_cells": 3,
        "batch_key": "sample_id",
        "categorical_covariates": [],
        "continuous_covariates": ["pct_mito"],
        "chunk_cells": 2,
        "chunks": [
            {
                "index": 1,
                "mtx": "chunk_00001.mtx",
                "cells": "chunk_00001_cells.txt",
                "n_features": 3,
                "n_cells": 2,
            },
            {
                "index": 2,
                "mtx": "chunk_00002.mtx",
                "cells": "chunk_00002_cells.txt",
                "n_features": 3,
                "n_cells": 1,
            },
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return root


def test_build_anndata_from_staged_chunks_preserves_order(monkeypatch, tmp_path: Path):
    _patch_fake_anndata_only(monkeypatch)
    stage = _write_stage(tmp_path / "stage")

    adata, info = scvi_train._build_anndata_from_staged_chunks(
        {"matrix_chunks_dir": str(stage)},
        {"batch_key": "sample_id", "continuous_covariates": ["pct_mito"]},
    )

    assert adata.X.shape == (3, 3)
    assert list(adata.obs.index) == ["c2", "c1", "c3"]
    assert list(adata.var.index) == ["g1", "g2", "g3"]
    assert adata.X.toarray().tolist() == [[1, 0, 3], [0, 2, 4], [5, 0, 6]]
    assert info["matrix_id"] == "RNA_counts"
    assert info["batch_key"] == "sample_id"
    assert info["continuous_covariates"] == ["pct_mito"]


def test_build_anndata_reports_missing_covariate(monkeypatch, tmp_path: Path):
    _patch_fake_anndata_only(monkeypatch)
    stage = _write_stage(tmp_path / "stage", missing_covariate=True)

    with pytest.raises(scvi_train.ScVITrainOpError, match="covariate.*missing"):
        scvi_train._build_anndata_from_staged_chunks(
            {"matrix_chunks_dir": str(stage)},
            {"batch_key": "sample_id"},
        )


def test_train_scvi_wires_scvi_tools_kwargs(monkeypatch):
    captured = {}

    class FakeModelInstance:
        history = {"train_loss": [3.0, 2.0], "validation_loss": [4.0, 2.5]}

        def train(self, **kwargs):
            captured["train_kwargs"] = kwargs

        def get_latent_representation(self):
            return np.array([[1.0, 2.0], [3.0, 4.0]])

    class FakeSCVIModel:
        @staticmethod
        def setup_anndata(
            adata,
            batch_key=None,
            categorical_covariate_keys=None,
            continuous_covariate_keys=None,
        ):
            captured["setup"] = {
                "adata": adata,
                "batch_key": batch_key,
                "categorical_covariate_keys": categorical_covariate_keys,
                "continuous_covariate_keys": continuous_covariate_keys,
            }

        def __new__(cls, adata, **kwargs):
            captured["init"] = {"adata": adata, **kwargs}
            return FakeModelInstance()

    class FakeScvi:
        __version__ = "test-scvi"

        class settings:
            seed = None

        class model:
            SCVI = FakeSCVIModel

    monkeypatch.setattr(scvi_train, "_import_scvi", lambda: (object(), FakeScvi))

    model, latent, version, metrics = scvi_train._train_scvi(
        adata="adata-object",
        batch_key="sample_id",
        categorical_covariates=["donor"],
        continuous_covariates=["pct_mito"],
        n_latent=2,
        n_hidden=64,
        n_layers=1,
        dropout=0.2,
        max_epochs=3,
        early_stopping=True,
        seed=11,
        use_gpu=False,
    )

    assert isinstance(model, FakeModelInstance)
    assert latent.shape == (2, 2)
    assert version == "test-scvi"
    assert metrics["final_train_loss"] == 2.0
    assert metrics["final_train_loss_status"] == "available"
    assert metrics["final_train_loss_metric"] == "train_loss"
    assert metrics["final_train_loss_source"] == "model.history:train_loss"
    assert metrics["final_validation_loss"] == 2.5
    assert metrics["final_validation_loss_status"] == "available"
    assert metrics["training_epochs_completed"] == 2
    assert metrics["training_epochs_status"] == "available"
    assert metrics["training_epochs_source"] == "model.history:train_loss.length"
    assert metrics["early_stopping_outcome"] == "unavailable"
    assert captured["setup"]["batch_key"] == "sample_id"
    assert captured["setup"]["categorical_covariate_keys"] == ["donor"]
    assert captured["setup"]["continuous_covariate_keys"] == ["pct_mito"]
    assert captured["init"]["n_latent"] == 2
    assert captured["init"]["n_hidden"] == 64
    assert captured["init"]["n_layers"] == 1
    assert captured["init"]["dropout_rate"] == 0.2
    assert captured["train_kwargs"] == {
        "early_stopping": True,
        "max_epochs": 3,
        "accelerator": "cpu",
        "devices": 1,
    }


def test_training_evidence_reads_dataframe_history_and_early_stopping_state():
    class FakeEarlyStopping:
        monitor = "elbo_validation"
        best_score = np.array(1.75)
        stopped_epoch = 7
        wait_count = 3
        patience = 45
        early_stopping_reason = "patience exhausted"

    class FakeTrainer:
        current_epoch = 7
        max_epochs = 30
        early_stopping_callback = FakeEarlyStopping()
        callback_metrics = {}

    class FakeModel:
        history = {
            "train_loss_epoch": pd.DataFrame({"train_loss_epoch": [4.0, 2.0]}),
            "validation_loss": pd.DataFrame({"validation_loss": [3.5, 1.8]}),
        }
        trainer = FakeTrainer()

    evidence = scvi_train._training_evidence(FakeModel(), early_stopping_requested=True)

    assert evidence["final_train_loss"] == 2.0
    assert evidence["final_train_loss_metric"] == "train_loss_epoch"
    assert evidence["final_train_loss_source"] == "model.history:train_loss_epoch"
    assert evidence["final_train_loss_status"] == "available"
    assert evidence["final_validation_loss"] == 1.8
    assert evidence["final_validation_loss_metric"] == "validation_loss"
    assert evidence["final_validation_loss_status"] == "available"
    assert evidence["training_epochs_completed"] == 2
    assert evidence["training_epochs_source"] == "model.history:train_loss_epoch.length"
    assert evidence["early_stopping_outcome"] == "stopped_early"
    assert evidence["early_stopping_monitor"] == "elbo_validation"
    assert evidence["early_stopping_best_score"] == 1.75
    assert evidence["early_stopping_best_score_status"] == "available"
    assert evidence["early_stopping_stopped_epoch"] == 7
    assert evidence["early_stopping_wait_count"] == 3
    assert evidence["early_stopping_patience"] == 45
    assert evidence["training_evidence_schema"] == "scvi_training_evidence_v1"


def test_training_evidence_distinguishes_unavailable_and_failed_extraction():
    class MissingMetrics:
        history = {}
        trainer = None

    class BadMetrics:
        history = {"train_loss_epoch": pd.DataFrame({"train_loss_epoch": ["not-a-number"]})}
        trainer = None

    unavailable = scvi_train._training_evidence(MissingMetrics(), early_stopping_requested=False)
    failed = scvi_train._training_evidence(BadMetrics(), early_stopping_requested=False)

    assert unavailable["final_train_loss"] is None
    assert unavailable["final_train_loss_status"] == "unavailable"
    assert unavailable["final_train_loss_error"] is None
    assert unavailable["final_validation_loss_status"] == "unavailable"
    assert unavailable["early_stopping_outcome"] == "not_requested"
    assert failed["final_train_loss"] is None
    assert failed["final_train_loss_status"] == "extraction_failed"
    assert "train_loss_epoch" in failed["final_train_loss_error"]


def test_training_evidence_falls_back_to_trainer_callback_metrics():
    class FakeTrainer:
        current_epoch = 4
        callback_metrics = {
            "elbo_train": np.array(2.75),
            "elbo_validation": np.array(3.25),
        }

    class FakeModel:
        history = {}
        trainer = FakeTrainer()

    evidence = scvi_train._training_evidence(FakeModel(), early_stopping_requested=False)

    assert evidence["final_train_loss"] == 2.75
    assert evidence["final_train_loss_metric"] == "elbo_train"
    assert evidence["final_train_loss_source"] == "model.trainer.callback_metrics:elbo_train"
    assert evidence["final_validation_loss"] == 3.25
    assert evidence["final_validation_loss_metric"] == "elbo_validation"
    assert evidence["training_epochs_completed"] == 4
    assert evidence["training_epochs_source"] == "model.trainer.current_epoch"


def _init_bundle(root: Path) -> None:
    (root / "db").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "db" / "app.sqlite"))
    try:
        con.execute("CREATE TABLE cells (cell_id TEXT PRIMARY KEY)")
        con.execute(
            "CREATE TABLE spaces (space_id TEXT PRIMARY KEY, source TEXT, dims INTEGER, storage TEXT, description TEXT)"
        )
        con.execute(
            "CREATE TABLE space_2d (space_id TEXT, cell_id TEXT, x REAL, y REAL, PRIMARY KEY (space_id, cell_id))"
        )
        con.execute(
            "CREATE TABLE space_coords (space_id TEXT, cell_id TEXT, dim INTEGER, value REAL, PRIMARY KEY (space_id, cell_id, dim))"
        )
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
            CREATE TABLE artifact_model_ref (
              artifact_id TEXT PRIMARY KEY,
              engine TEXT,
              model_space_id TEXT,
              in_space_id TEXT,
              model_type TEXT,
              model_path TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE artifact_tables (
              artifact_id TEXT PRIMARY KEY,
              table_type TEXT,
              entity_kind TEXT,
              meta_json TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE artifact_table_rows (
              artifact_id TEXT,
              row_idx INTEGER,
              row_json TEXT,
              PRIMARY KEY (artifact_id, row_idx)
            )
            """
        )
        con.executemany("INSERT INTO cells(cell_id) VALUES (?)", [("c2",), ("c1",), ("c3",)])
        con.executemany(
            "INSERT INTO artifacts(artifact_id, type, name) VALUES (?, ?, ?)",
            [
                ("model_art", "model_ref", "scVI model"),
                ("summary_art", "obs_table", "scVI fit summary"),
            ],
        )
        con.commit()
    finally:
        con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}}, indent=2)
    )
    (root / "artifact_models").mkdir()


def test_run_writes_space_model_ref_and_fit_summary(monkeypatch, tmp_path: Path):
    stage = _write_stage(tmp_path / "stage")
    bundle_dir = tmp_path / "bundle"
    _init_bundle(bundle_dir)

    class FakeEarlyStopping:
        monitor = "elbo_validation"
        best_score = 1.25
        stopped_epoch = 0
        wait_count = 0
        patience = 45
        early_stopping_reason = None

    class FakeTrainer:
        current_epoch = 1
        max_epochs = 2
        early_stopping_callback = FakeEarlyStopping()
        callback_metrics = {}

    class FakeModelInstance:
        history = {
            "train_loss_epoch": pd.DataFrame({"train_loss_epoch": [1.5, 1.25]}),
            "validation_loss": pd.DataFrame({"validation_loss": [1.7, 1.4]}),
        }
        trainer = FakeTrainer()

        def train(self, **kwargs):
            self.train_kwargs = kwargs

        def get_latent_representation(self):
            return np.array(
                [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]],
                dtype=float,
            )

        def save(self, path, overwrite=True, save_anndata=False):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "model.txt").write_text("saved")

    class FakeSCVIModel:
        @staticmethod
        def setup_anndata(*args, **kwargs):
            return None

        def __new__(cls, *args, **kwargs):
            return FakeModelInstance()

    class FakeScvi:
        __version__ = "test-scvi"

        class settings:
            seed = None

        class model:
            SCVI = FakeSCVIModel

    monkeypatch.setattr(
        scvi_train,
        "_import_scvi",
        lambda: (type("AD", (), {"AnnData": FakeAnnData}), FakeScvi),
    )

    outputs = {
        "model": {"artifact_id": "model_art", "type": "model_ref"},
        "fit_summary": {"artifact_id": "summary_art", "type": "obs_table"},
        "space_registry": {"artifact_id": "rna.scvi", "type": "space"},
    }
    req = v2_request(
        bundle_dir,
        op_name="scvi_train",
        run_id="run_scvi",
        inputs={
            "matrix": "RNA_counts",
            "cell_set": "core",
            "matrix_chunks_dir": str(stage),
            "cell_metadata_path": str(stage / "cell_metadata.tsv"),
        },
        params={
            "matrix_id": "RNA_counts",
            "n_latent": 3,
            "n_hidden": 16,
            "n_layers": 1,
            "dropout": 0.1,
            "batch_key": "sample_id",
            "continuous_covariates": ["pct_mito"],
            "max_epochs": 2,
            "early_stopping": True,
            "seed": 7,
            "use_gpu": False,
            "output_space_id": "rna.scvi",
            "overwrite": False,
        },
        outputs=outputs,
        allowances=[
            sqlite_allowance("space_registry", "rna.scvi", "space", "spaces", "space_id"),
            sqlite_allowance("space_registry", "rna.scvi", "space", "space_coords", "space_id"),
            sqlite_allowance(
                "model", "model_art", "model_ref", "artifact_model_ref", "artifact_id"
            ),
            payload_allowance("model", "model_art", "model_ref", "artifact_models/model_art"),
            sqlite_allowance(
                "fit_summary", "summary_art", "obs_table", "artifact_tables", "artifact_id"
            ),
            sqlite_allowance(
                "fit_summary", "summary_art", "obs_table", "artifact_table_rows", "artifact_id"
            ),
        ],
    )

    rec = scvi_train._run(req)

    assert rec["status"] == "ok"
    assert set(rec["outputs"]) == {"model", "fit_summary", "space_registry"}
    con = sqlite3.connect(str(bundle_dir / "db" / "app.sqlite"))
    con.row_factory = sqlite3.Row
    try:
        space = con.execute("SELECT * FROM spaces WHERE space_id = 'rna.scvi'").fetchone()
        assert dict(space)["dims"] == 3
        coords_n = con.execute(
            "SELECT COUNT(*) FROM space_coords WHERE space_id = 'rna.scvi'"
        ).fetchone()[0]
        assert coords_n == 9

        model_ref = dict(
            con.execute(
                "SELECT * FROM artifact_model_ref WHERE artifact_id = 'model_art'"
            ).fetchone()
        )
        assert model_ref["engine"] == "scvi"
        assert model_ref["model_space_id"] == "rna.scvi"
        assert model_ref["model_path"] == "artifact_models/model_art/scvi"
        assert (bundle_dir / model_ref["model_path"] / "model.txt").exists()

        table = dict(
            con.execute(
                "SELECT * FROM artifact_tables WHERE artifact_id = 'summary_art'"
            ).fetchone()
        )
        assert table["table_type"] == "scvi_fit_summary"
        row_json = con.execute(
            "SELECT row_json FROM artifact_table_rows WHERE artifact_id = 'summary_art'"
        ).fetchone()[0]
        row = json.loads(row_json)
        assert row["matrix_id"] == "RNA_counts"
        assert row["n_cells"] == 3
        assert row["n_features"] == 3
        assert row["n_latent"] == 3
        assert row["batch_key"] == "sample_id"
        assert row["continuous_covariates_json"] == '["pct_mito"]'
        assert row["scvi_tools_version"] == "test-scvi"
        assert row["pybench_version"] == scvi_train.PYBENCH_VERSION
        assert row["final_train_loss"] == 1.25
        assert row["final_train_loss_status"] == "available"
        assert row["final_train_loss_metric"] == "train_loss_epoch"
        assert row["final_train_loss_source"] == "model.history:train_loss_epoch"
        assert row["final_validation_loss"] == 1.4
        assert row["final_validation_loss_status"] == "available"
        assert row["training_epochs_completed"] == 2
        assert row["early_stopping_outcome"] == "completed_max_epochs"
        assert row["early_stopping_monitor"] == "elbo_validation"
        assert row["early_stopping_best_score"] == 1.25
        assert row["training_evidence_schema"] == "scvi_training_evidence_v1"
    finally:
        con.close()
