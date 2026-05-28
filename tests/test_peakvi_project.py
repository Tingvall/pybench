import numpy as np
import pandas as pd
from scipy import sparse as sps

from pybench.ops import peakvi_project


def test_project_peakvi_prefers_load_and_returns_latent(monkeypatch):
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
    latent, version = peakvi_project._project_peakvi("model-dir", adata=type("AD", (), {"shape": (2, 3)})(), use_gpu=False)

    assert version == "test-scvi"
    assert latent.shape == (2, 2)
    assert captured["load"]["model_path"] == "model-dir"
    assert captured["load"]["accelerator"] == "cpu"


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
