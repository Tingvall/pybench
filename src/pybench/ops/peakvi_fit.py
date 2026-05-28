from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pybench import add_space, close_bundle, open_bundle
from pybench.artifacts_payload import write_artifact_model_ref_payload
from pybench.getters import matrix as get_matrix
from pybench.getters import meta as get_meta
from pybench.protocol import error_receipt_from_exception, load_request, ok_receipt, write_receipt

PYBENCH_VERSION = "0.1.0"


class PeakVIFitOpError(RuntimeError):
    pass


def _cell_ids_from_request(req_inputs: dict[str, Any]) -> list[str]:
    cell_ids = req_inputs.get("cell_ids")
    if isinstance(cell_ids, list):
        out = [str(x) for x in cell_ids if str(x)]
        if out:
            return out

    cell_ids_path = str(req_inputs.get("cell_ids_path") or "")
    if cell_ids_path:
        p = Path(cell_ids_path)
        if not p.exists():
            raise PeakVIFitOpError(f"missing required file: {p}")
        out = [str(x) for x in p.read_text().splitlines() if str(x)]
        if out:
            return out

    raise PeakVIFitOpError("peakvi_fit requires cell_ids in request inputs")


def _import_scvi():
    try:
        import anndata as ad  # type: ignore
        import scvi  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise PeakVIFitOpError(
            "peakvi_fit requires anndata and scvi-tools. Install pybench with the 'scvi' extra."
        ) from e
    return ad, scvi


def _build_matrix_from_staged_chunks(req_inputs: dict[str, Any]):
    try:
        from scipy import io as spio  # type: ignore
        from scipy import sparse as sps  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise PeakVIFitOpError("peakvi_fit staged chunk loading requires scipy") from e

    stage_dir = str(req_inputs.get("matrix_chunks_dir") or "")
    if not stage_dir:
        return None
    root = Path(stage_dir)
    if not root.exists():
        raise PeakVIFitOpError(f"matrix_chunks_dir does not exist: {root}")

    man_path = root / "manifest.json"
    feat_path = root / "feature_ids.txt"
    if not man_path.exists() or not feat_path.exists():
        raise PeakVIFitOpError(f"matrix chunk staging is incomplete under: {root}")

    manifest = json.loads(man_path.read_text())
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise PeakVIFitOpError(f"matrix chunk manifest has no chunks: {man_path}")

    feature_ids = [str(x) for x in feat_path.read_text().splitlines() if str(x)]
    if not feature_ids:
        raise PeakVIFitOpError(f"feature_ids.txt is empty under: {root}")

    parts = []
    cell_ids: list[str] = []
    for spec in chunks:
        if not isinstance(spec, dict):
            raise PeakVIFitOpError(f"invalid chunk entry in manifest: {spec!r}")
        mtx_path = root / str(spec.get("mtx") or "")
        cells_path = root / str(spec.get("cells") or "")
        if not mtx_path.exists() or not cells_path.exists():
            raise PeakVIFitOpError(f"missing staged chunk file(s): {mtx_path} / {cells_path}")

        part = spio.mmread(str(mtx_path))
        if sps.issparse(part):
            part = part.tocsc()
        else:
            part = sps.csc_matrix(part)
        if part.shape[0] != len(feature_ids):
            raise PeakVIFitOpError(
                f"staged chunk row count mismatch for {mtx_path.name}: {part.shape[0]} vs {len(feature_ids)}"
            )
        parts.append(part)

        chunk_cells = [str(x) for x in cells_path.read_text().splitlines() if str(x)]
        if part.shape[1] != len(chunk_cells):
            raise PeakVIFitOpError(
                f"staged chunk column mismatch for {mtx_path.name}: {part.shape[1]} vs {len(chunk_cells)}"
            )
        cell_ids.extend(chunk_cells)

    if len(parts) == 1:
        X_fc = parts[0]
    else:
        X_fc = sps.hstack(parts, format="csc")
    return X_fc, feature_ids, cell_ids


def _build_anndata(req_inputs: dict[str, Any], req_params: dict[str, Any], bundle):
    ad, _scvi = _import_scvi()

    matrix_id = str(req_params.get("matrix_id") or req_inputs.get("matrix") or "")
    if not matrix_id:
        raise PeakVIFitOpError("peakvi_fit requires matrix_id")
    staged = _build_matrix_from_staged_chunks(req_inputs)
    if staged is not None:
        X_fc, feature_ids, cell_ids = staged
    else:
        cell_ids = _cell_ids_from_request(req_inputs)
        try:
            X_fc, feature_ids, cell_ids = get_matrix(bundle, matrix_id=matrix_id, cells_order=cell_ids)
        except Exception as e:  # noqa: BLE001
            raise PeakVIFitOpError(f"failed to read bundle matrix '{matrix_id}' via Python BPCells: {e}") from e

    X_cf = X_fc.transpose().tocsr()

    obs = get_meta(bundle, cells_order=cell_ids)
    if "cell_id" not in obs.columns:
        raise PeakVIFitOpError("cells metadata is missing cell_id")
    obs = obs.set_index("cell_id", drop=True)
    obs.index = obs.index.astype(str)
    obs = obs.loc[cell_ids].copy()

    var = pd.DataFrame(index=pd.Index(feature_ids, name="feature_id"))
    adata = ad.AnnData(X=X_cf, obs=obs, var=var)

    batch_key = req_params.get("batch_key")
    if batch_key is not None and str(batch_key):
        bk = str(batch_key)
        if bk not in adata.obs.columns:
            raise PeakVIFitOpError(f"batch_key not found in obs metadata: {bk}")
    else:
        bk = None

    return adata, bk


def _train_peakvi(
    adata,
    batch_key: str | None,
    n_latent: int,
    n_layers: int,
    n_hidden: int,
    dropout: float,
    max_epochs: int | None,
    lr: float | None,
    early_stopping: bool,
    seed: int | None,
    use_gpu: bool,
):
    _ad, scvi = _import_scvi()

    if seed is not None:
        try:
            scvi.settings.seed = int(seed)
        except Exception:
            pass
        try:
            import torch  # type: ignore
            torch.manual_seed(int(seed))
        except Exception:
            pass
        np.random.seed(int(seed))

    scvi.model.PEAKVI.setup_anndata(adata, batch_key=batch_key)
    model = scvi.model.PEAKVI(
        adata,
        n_latent=int(n_latent),
        n_layers_encoder=int(n_layers),
        n_layers_decoder=int(n_layers),
        n_hidden=int(n_hidden),
        dropout_rate=float(dropout),
    )

    train_kwargs: dict[str, Any] = {}
    if max_epochs is not None:
        train_kwargs["max_epochs"] = int(max_epochs)
    train_kwargs["early_stopping"] = bool(early_stopping)
    if lr is not None:
        train_kwargs["lr"] = float(lr)
    train_kwargs["accelerator"] = "gpu" if use_gpu else "cpu"
    train_kwargs["devices"] = 1
    model.train(**train_kwargs)

    latent = model.get_latent_representation()
    latent = np.asarray(latent, dtype=float)
    if latent.ndim != 2:
        raise PeakVIFitOpError("PeakVI latent representation is not 2D")
    return model, latent, getattr(scvi, "__version__", None)


def _save_model(bundle, artifact_id: str, model) -> str:
    model_dir = Path(bundle.bundle_dir) / "artifact_models" / f"{artifact_id}_peakvi"
    model_dir.mkdir(parents=True, exist_ok=True)
    model.save(str(model_dir), overwrite=True, save_anndata=False)
    return str(model_dir)


def _run(req: dict[str, Any]) -> dict[str, Any]:
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {})
        params = req.get("params", {})
        outputs_spec = req.get("outputs", {})
        if "model" not in outputs_spec:
            raise PeakVIFitOpError("peakvi_fit requires preregistered 'model' output")

        n_latent = int(params.get("n_latent", 20))
        if n_latent < 2:
            raise PeakVIFitOpError("n_latent must be >= 2")
        n_layers = int(params.get("n_layers", 2))
        if n_layers < 1:
            raise PeakVIFitOpError("n_layers must be >= 1")
        n_hidden = int(params.get("n_hidden", 256))
        if n_hidden < 1:
            raise PeakVIFitOpError("n_hidden must be >= 1")
        dropout = float(params.get("dropout", 0.2))
        if not (0.0 <= dropout <= 1.0):
            raise PeakVIFitOpError("dropout must be between 0 and 1")
        max_epochs = params.get("max_epochs")
        max_epochs = None if max_epochs is None else int(max_epochs)
        lr = params.get("lr", 1e-3)
        lr = None if lr is None else float(lr)
        if lr is not None and lr <= 0:
            raise PeakVIFitOpError("lr must be > 0 when provided")
        early_stopping = bool(params.get("early_stopping", True))
        seed = params.get("seed")
        seed = None if seed is None else int(seed)
        use_gpu = bool(params.get("use_gpu", False))
        space_id_out = str(params.get("space_id_out") or "peakvi_space")
        matrix_id = str(params.get("matrix_id") or inputs.get("matrix") or "")
        if not matrix_id:
            raise PeakVIFitOpError("matrix_id is required")

        adata, batch_key = _build_anndata(inputs, params, b)
        model, latent, _scvi_version = _train_peakvi(
            adata=adata,
            batch_key=batch_key,
            n_latent=n_latent,
            n_layers=n_layers,
            n_hidden=n_hidden,
            dropout=dropout,
            max_epochs=max_epochs,
            lr=lr,
            early_stopping=early_stopping,
            seed=seed,
            use_gpu=use_gpu,
        )

        add_space(
            b,
            space_id=space_id_out,
            coords=latent,
            cell_id=[str(x) for x in adata.obs_names.tolist()],
            description=f"PeakVI latent space from {matrix_id}",
            source=f"peakvi_fit:{matrix_id}",
            overwrite=True,
        )

        model_path = _save_model(b, outputs_spec["model"]["artifact_id"], model)
        out_receipts = {
            "model": write_artifact_model_ref_payload(
                b,
                artifact_id=outputs_spec["model"]["artifact_id"],
                engine="peakvi",
                model_space_id=space_id_out,
                in_space_id=space_id_out,
                model_type="peakvi_fit",
                model_path=model_path,
            )
        }

        dt = time.time() - t0
        return ok_receipt(
            op_name=op_name,
            run_id=run_id,
            outputs=out_receipts,
            warnings=[],
            timings={"total_sec": round(dt, 6)},
            pybench_version=PYBENCH_VERSION,
        )
    finally:
        close_bundle(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="pybench PeakVI fit op")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(
            op_name=str(req.get("op_name", "peakvi_fit")),
            run_id=str((req.get("run") or {}).get("run_id", "<unknown>")),
            exc=e,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
