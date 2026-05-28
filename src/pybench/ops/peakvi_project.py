from __future__ import annotations

import argparse
import time
from typing import Any

import numpy as np

from pybench import add_space, close_bundle, open_bundle
from pybench.ops.peakvi_fit import (
    PYBENCH_VERSION,
    PeakVIFitOpError,
    _build_matrix_from_staged_chunks,
    _cell_ids_from_request,
    _import_scvi,
)
from pybench.protocol import error_receipt_from_exception, load_request, ok_receipt, write_receipt


class PeakVIProjectOpError(RuntimeError):
    pass


def _resolve_model_path(req_inputs: dict[str, Any]) -> str:
    model_path = str(req_inputs.get("model_path") or "")
    if not model_path:
        raise PeakVIProjectOpError("peakvi_project requires model_path")
    return model_path


def _build_project_anndata(req_inputs: dict[str, Any], req_params: dict[str, Any], bundle):
    ad, _scvi = _import_scvi()

    matrix_id = str(req_params.get("matrix_id") or req_inputs.get("matrix") or "")
    if not matrix_id:
        raise PeakVIProjectOpError("peakvi_project requires matrix_id")

    staged = _build_matrix_from_staged_chunks(req_inputs)
    if staged is None:
        raise PeakVIProjectOpError("peakvi_project requires matrix_chunks_dir staged input")
    X_fc, feature_ids, cell_ids = staged

    from pybench.getters import meta as get_meta

    X_cf = X_fc.transpose().tocsr()
    obs = get_meta(bundle, cells_order=cell_ids)
    if "cell_id" not in obs.columns:
        raise PeakVIProjectOpError("cells metadata is missing cell_id")
    obs = obs.set_index("cell_id", drop=True)
    obs.index = obs.index.astype(str)
    obs = obs.loc[cell_ids].copy()

    import pandas as pd

    var = pd.DataFrame(index=pd.Index(feature_ids, name="feature_id"))
    adata = ad.AnnData(X=X_cf, obs=obs, var=var)

    batch_key = req_params.get("batch_key")
    if batch_key is not None and str(batch_key):
        bk = str(batch_key)
        if bk not in adata.obs.columns:
            raise PeakVIProjectOpError(f"batch_key not found in obs metadata: {bk}")
    else:
        bk = None
    return adata, bk


def _project_peakvi(model_path: str, adata, use_gpu: bool):
    _ad, scvi = _import_scvi()
    accelerator = "gpu" if use_gpu else "cpu"
    device = "auto"

    first_error: Exception | None = None
    try:
        model = scvi.model.PEAKVI.load(model_path, adata=adata, accelerator=accelerator, device=device)
    except Exception as e:  # noqa: BLE001
        first_error = e
        try:
            model = scvi.model.PEAKVI.load_query_data(
                adata=adata,
                reference_model=model_path,
                inplace_subset_query_vars=True,
                accelerator=accelerator,
                device=device,
            )
        except Exception as ee:  # noqa: BLE001
            raise PeakVIProjectOpError(
                f"failed to load PeakVI model for projection via load() or load_query_data(): {first_error}; {ee}"
            ) from ee

    latent = model.get_latent_representation()
    latent = np.asarray(latent, dtype=float)
    if latent.ndim != 2:
        raise PeakVIProjectOpError("PeakVI projected latent representation is not 2D")
    return latent, getattr(scvi, "__version__", None)


def _run(req: dict[str, Any]) -> dict[str, Any]:
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {}) or {}
        params = req.get("params", {}) or {}

        model_path = _resolve_model_path(inputs)
        matrix_id = str(params.get("matrix_id") or inputs.get("matrix") or "")
        if not matrix_id:
            raise PeakVIProjectOpError("matrix_id is required")
        use_gpu = bool(params.get("use_gpu", False))
        space_id_out = str(params.get("space_id_out") or "peakvi_project_space")

        adata, _batch_key = _build_project_anndata(inputs, params, b)
        latent, _scvi_version = _project_peakvi(model_path=model_path, adata=adata, use_gpu=use_gpu)

        add_space(
            b,
            space_id=space_id_out,
            coords=latent,
            cell_id=[str(x) for x in adata.obs_names.tolist()],
            description=f"PeakVI projected latent space from {matrix_id}",
            source=f"peakvi_project:{matrix_id}",
            overwrite=True,
        )

        dt = time.time() - t0
        return ok_receipt(
            op_name=op_name,
            run_id=run_id,
            outputs={},
            warnings=[],
            timings={"total_sec": round(dt, 6)},
            pybench_version=PYBENCH_VERSION,
        )
    finally:
        close_bundle(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="pybench PeakVI project op")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(
            op_name=str(req.get("op_name", "peakvi_project")),
            run_id=str((req.get("run") or {}).get("run_id", "<unknown>")),
            exc=e,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
