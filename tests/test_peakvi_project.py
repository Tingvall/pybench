import json
import shutil
import sqlite3

import numpy as np
import pandas as pd
import pytest
from scipy import sparse as sps

from pybench.ops import peakvi_project
from pybench.protocol import PATH_CONTRACT, TrustedModelPath


def test_project_peakvi_prefers_load_and_returns_latent(monkeypatch, tmp_path):
    captured = {}

    class FakeModel:
        def get_latent_representation(self):
            return np.array([[1.0, 2.0], [3.0, 4.0]], dtype=float)

    class FakePeakVI:
        @staticmethod
        def load(model_path, adata=None, accelerator="auto", device="auto"):
            captured["load"] = {
                "model_path": model_path,
                "adata_shape": adata.shape,
                "accelerator": accelerator,
                "device": device,
            }
            return FakeModel()

    class FakeScvi:
        __version__ = "test-scvi"

        class model:
            PEAKVI = FakePeakVI

    monkeypatch.setattr(peakvi_project, "_import_scvi", lambda: (object(), FakeScvi))
    trusted = TrustedModelPath(
        relative="artifact_models/model-dir",
        absolute=tmp_path / "model-dir",
        contract_version="scworkbench-bundle-path-v1",
    )
    latent, version = peakvi_project._project_peakvi(
        trusted, adata=type("AD", (), {"shape": (2, 3)})(), use_gpu=False
    )

    assert version == "test-scvi"
    assert latent.shape == (2, 2)
    assert captured["load"]["model_path"] == str(tmp_path / "model-dir")
    assert captured["load"]["accelerator"] == "cpu"


def test_project_peakvi_rejects_untrusted_deserializer_call():
    with pytest.raises(peakvi_project.PeakVIProjectOpError, match="TRUST_REQUIRED"):
        peakvi_project._project_peakvi(
            "model-dir",  # type: ignore[arg-type]
            adata=type("AD", (), {"shape": (2, 3)})(),
            use_gpu=False,
        )


def test_build_project_anndata_uses_staged_chunks(monkeypatch, tmp_path):
    captured = {}

    class FakeAnnData:
        def __init__(self, X, obs, var):
            captured["X"] = X
            captured["obs"] = obs
            captured["var"] = var
            self.X = X
            self.obs = obs
            self.var = var
            self.obs_names = obs.index

    monkeypatch.setattr(peakvi_project, "_import_scvi", lambda: (type("AD", (), {"AnnData": FakeAnnData}), object()))
    monkeypatch.setattr(
        peakvi_project,
        "_build_matrix_from_staged_chunks",
        lambda req_inputs: (
            sps.csc_matrix(np.array([[1.0, 0.0], [0.0, 2.0], [3.0, 4.0]], dtype=float)),
            ["f1", "f2", "f3"],
            ["c2", "c1"],
        ),
    )
    monkeypatch.setattr(
        peakvi_project,
        "get_meta",
        lambda bundle, cells_order: pd.DataFrame(
            {"cell_id": ["c2", "c1"], "batch": ["b", "a"]},
        ),
        raising=False,
    )

    # patch imported getter inside function namespace
    import pybench.getters as getters
    monkeypatch.setattr(
        getters,
        "meta",
        lambda bundle, cells_order: pd.DataFrame({"cell_id": ["c2", "c1"], "batch": ["b", "a"]}),
    )

    adata, batch_key = peakvi_project._build_project_anndata(
        req_inputs={"matrix": "ATAC_counts", "matrix_chunks_dir": str(tmp_path)},
        req_params={"matrix_id": "ATAC_counts", "batch_key": "batch"},
        bundle="bundle-handle",
    )

    assert batch_key == "batch"
    assert adata.X.shape == (2, 3)
    assert list(adata.obs.index) == ["c2", "c1"]
    assert list(adata.var.index) == ["f1", "f2", "f3"]


def test_peakvi_project_resolves_relative_model_after_bundle_move(monkeypatch, tmp_path):
    source = tmp_path / "source.bundle"
    (source / "db").mkdir(parents=True)
    sqlite3.connect(source / "db" / "app.sqlite").close()
    (source / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    model_relative = "artifact_models/model_peakvi"
    (source / model_relative).mkdir(parents=True)
    (source / model_relative / "model.pt").write_bytes(b"safe-test-state")
    moved = tmp_path / "moved" / "portable.bundle"
    moved.parent.mkdir()
    shutil.copytree(source, moved)

    captured = {}

    class FakeModel:
        def get_latent_representation(self):
            return np.array([[1.0, 2.0]], dtype=float)

    class FakePeakVI:
        @staticmethod
        def load(model_path, **_kwargs):
            captured["model_path"] = model_path
            return FakeModel()

    class FakeScvi:
        __version__ = "test-scvi"

        class model:
            PEAKVI = FakePeakVI

    adata = type("AD", (), {"shape": (1, 2), "obs_names": pd.Index(["c1"])})()
    monkeypatch.setattr(peakvi_project, "_import_scvi", lambda: (object(), FakeScvi))
    monkeypatch.setattr(
        peakvi_project, "_build_project_anndata", lambda *_args, **_kwargs: (adata, None)
    )
    monkeypatch.setattr(peakvi_project, "add_space", lambda *_args, **_kwargs: {})
    req = {
        "protocol_version": "pybench-op-v1",
        "path_contract": dict(PATH_CONTRACT),
        "bundle_dir": str(moved),
        "op_name": "peakvi_project",
        "run": {"run_id": "moved_project"},
        "inputs": {"matrix": "ATAC", "model_path": model_relative},
        "params": {"matrix_id": "ATAC", "space_id_out": "projected"},
        "outputs": {},
    }
    receipt = peakvi_project._run(req)
    assert receipt["status"] == "ok"
    assert captured["model_path"] == str((moved / model_relative).resolve())
