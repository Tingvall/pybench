from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pybench import add_space, close_bundle, open_bundle
from pybench.artifacts_payload import (
    write_artifact_model_ref_payload,
    write_artifact_table_payload,
)
from pybench.protocol import error_receipt_from_exception, load_request, ok_receipt, write_receipt

PYBENCH_VERSION = "0.1.0"
STAGED_PROTOCOL = "scworkbench_sparse_matrix_chunks_v1"


class ScVITrainOpError(RuntimeError):
    pass


def _import_scvi():
    try:
        import anndata as ad  # type: ignore
        import scvi  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise ScVITrainOpError(
            "scvi_train requires anndata and scvi-tools. Install pybench with the 'scvi' extra."
        ) from e
    return ad, scvi


def _as_str_list(x: Any) -> list[str]:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [str(v) for v in x if str(v)]
    if isinstance(x, str):
        return [x] if x else []
    return [str(x)]


def _read_nonempty_lines(path: Path, what: str) -> list[str]:
    if not path.exists():
        raise ScVITrainOpError(f"missing {what}: {path}")
    out = [str(x) for x in path.read_text().splitlines() if str(x)]
    if not out:
        raise ScVITrainOpError(f"{what} is empty: {path}")
    return out


def _load_staged_matrix(req_inputs: dict[str, Any]):
    try:
        from scipy import io as spio  # type: ignore
        from scipy import sparse as sps  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise ScVITrainOpError("scvi_train staged chunk loading requires scipy") from e

    stage_dir = str(req_inputs.get("matrix_chunks_dir") or "")
    if not stage_dir:
        raise ScVITrainOpError("scvi_train requires inputs.matrix_chunks_dir")
    root = Path(stage_dir)
    if not root.exists():
        raise ScVITrainOpError(f"matrix_chunks_dir does not exist: {root}")

    man_path = root / "manifest.json"
    if not man_path.exists():
        raise ScVITrainOpError(f"missing staged matrix manifest: {man_path}")
    try:
        manifest = json.loads(man_path.read_text())
    except Exception as e:  # noqa: BLE001
        raise ScVITrainOpError(f"malformed staged matrix manifest: {man_path}") from e
    if manifest.get("protocol") != STAGED_PROTOCOL:
        raise ScVITrainOpError(f"unsupported staged matrix protocol: {manifest.get('protocol')!r}")
    if str(manifest.get("orientation") or "") != "features_by_cells":
        raise ScVITrainOpError("scvi_train requires staged chunks with orientation='features_by_cells'")

    feat_rel = str(manifest.get("feature_ids_file") or "feature_ids.txt")
    feature_ids = _read_nonempty_lines(root / feat_rel, "feature_ids file")
    if len(set(feature_ids)) != len(feature_ids):
        raise ScVITrainOpError("feature_ids contain duplicates")

    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ScVITrainOpError(f"staged matrix manifest has no chunks: {man_path}")

    parts = []
    cell_ids: list[str] = []
    for spec in chunks:
        if not isinstance(spec, dict):
            raise ScVITrainOpError(f"invalid staged chunk entry: {spec!r}")
        mtx_path = root / str(spec.get("mtx") or "")
        cells_path = root / str(spec.get("cells") or "")
        if not mtx_path.exists() or not cells_path.exists():
            raise ScVITrainOpError(f"missing staged chunk file(s): {mtx_path} / {cells_path}")

        part = spio.mmread(str(mtx_path))
        part = part.tocsc() if sps.issparse(part) else sps.csc_matrix(part)
        chunk_cells = _read_nonempty_lines(cells_path, "chunk cell-id file")

        if part.shape[0] != len(feature_ids):
            raise ScVITrainOpError(
                f"staged chunk row count mismatch for {mtx_path.name}: "
                f"{part.shape[0]} vs {len(feature_ids)}"
            )
        if part.shape[1] != len(chunk_cells):
            raise ScVITrainOpError(
                f"staged chunk column mismatch for {mtx_path.name}: "
                f"{part.shape[1]} vs {len(chunk_cells)}"
            )
        if "n_features" in spec and int(spec["n_features"]) != part.shape[0]:
            raise ScVITrainOpError(f"staged chunk n_features mismatch for {mtx_path.name}")
        if "n_cells" in spec and int(spec["n_cells"]) != part.shape[1]:
            raise ScVITrainOpError(f"staged chunk n_cells mismatch for {mtx_path.name}")
        parts.append(part)
        cell_ids.extend(chunk_cells)

    if len(set(cell_ids)) != len(cell_ids):
        raise ScVITrainOpError("staged chunk cell IDs contain duplicates")
    X_fc = parts[0] if len(parts) == 1 else sps.hstack(parts, format="csc")
    if int(manifest.get("n_features", X_fc.shape[0])) != X_fc.shape[0]:
        raise ScVITrainOpError("staged manifest n_features does not match assembled matrix")
    if int(manifest.get("n_cells", X_fc.shape[1])) != X_fc.shape[1]:
        raise ScVITrainOpError("staged manifest n_cells does not match assembled matrix")
    return X_fc, feature_ids, cell_ids, manifest, root


def _load_cell_metadata(
    req_inputs: dict[str, Any],
    manifest: dict[str, Any],
    root: Path,
    cell_ids: list[str],
) -> pd.DataFrame:
    meta_path = str(req_inputs.get("cell_metadata_path") or "")
    if meta_path:
        p = Path(meta_path)
    else:
        p = root / str(manifest.get("cell_metadata_file") or "cell_metadata.tsv")
    if not p.exists():
        raise ScVITrainOpError(f"missing cell_metadata.tsv: {p}")
    obs = pd.read_csv(p, sep="\t", dtype=str, keep_default_na=False)
    if "cell_id" not in obs.columns:
        raise ScVITrainOpError("cell_metadata.tsv missing cell_id column")
    obs["cell_id"] = obs["cell_id"].astype(str)
    if obs["cell_id"].duplicated().any():
        raise ScVITrainOpError("cell_metadata.tsv contains duplicate cell_id values")
    missing = [cid for cid in cell_ids if cid not in set(obs["cell_id"])]
    if missing:
        raise ScVITrainOpError(f"cell_metadata.tsv missing staged cell IDs: {missing[:5]}")
    obs = obs.set_index("cell_id", drop=True).reindex(cell_ids)
    if obs.isna().any(axis=None):
        raise ScVITrainOpError("cell_metadata.tsv could not be aligned to staged cells")
    obs.index = obs.index.astype(str)
    return obs


def _validate_covariates(
    obs: pd.DataFrame,
    batch_key: str | None,
    categorical_covariates: list[str],
    continuous_covariates: list[str],
) -> None:
    requested = [x for x in [batch_key, *categorical_covariates, *continuous_covariates] if x]
    missing = [x for x in requested if x not in obs.columns]
    if missing:
        raise ScVITrainOpError(f"scvi_train covariate(s) missing from cell metadata: {missing}")


def _build_anndata_from_staged_chunks(req_inputs: dict[str, Any], req_params: dict[str, Any]):
    ad, _scvi = _import_scvi()
    X_fc, feature_ids, cell_ids, manifest, root = _load_staged_matrix(req_inputs)
    obs = _load_cell_metadata(req_inputs, manifest, root, cell_ids)

    batch_key = str(req_params.get("batch_key") or manifest.get("batch_key") or "")
    batch_key = batch_key or None
    categorical_covariates = _as_str_list(
        req_params.get("categorical_covariates", manifest.get("categorical_covariates"))
    )
    continuous_covariates = _as_str_list(
        req_params.get("continuous_covariates", manifest.get("continuous_covariates"))
    )
    _validate_covariates(obs, batch_key, categorical_covariates, continuous_covariates)

    var = pd.DataFrame(index=pd.Index(feature_ids, name="feature_id"))
    adata = ad.AnnData(X=X_fc.transpose().tocsr(), obs=obs, var=var)
    return adata, {
        "matrix_id": str(req_params.get("matrix_id") or manifest.get("matrix_id") or ""),
        "feature_ids": feature_ids,
        "cell_ids": cell_ids,
        "manifest": manifest,
        "batch_key": batch_key,
        "categorical_covariates": categorical_covariates,
        "continuous_covariates": continuous_covariates,
    }


def _set_seed(scvi, seed: int | None) -> None:
    if seed is None:
        return
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


def _history_last(model, names: list[str]) -> float | None:
    hist = getattr(model, "history", None)
    if hist is None:
        return None
    for name in names:
        try:
            val = hist[name]
            if hasattr(val, "iloc"):
                val = val.iloc[-1]
            elif isinstance(val, (list, tuple)):
                val = val[-1]
            if isinstance(val, dict) and name in val:
                val = val[name]
            return float(val)
        except Exception:
            continue
    return None


def _train_scvi(
    adata,
    *,
    batch_key: str | None,
    categorical_covariates: list[str],
    continuous_covariates: list[str],
    n_latent: int,
    n_hidden: int,
    n_layers: int,
    dropout: float,
    max_epochs: int | None,
    early_stopping: bool,
    seed: int | None,
    use_gpu: bool,
):
    _ad, scvi = _import_scvi()
    _set_seed(scvi, seed)

    scvi.model.SCVI.setup_anndata(
        adata,
        batch_key=batch_key,
        categorical_covariate_keys=categorical_covariates or None,
        continuous_covariate_keys=continuous_covariates or None,
    )
    model = scvi.model.SCVI(
        adata,
        n_latent=int(n_latent),
        n_hidden=int(n_hidden),
        n_layers=int(n_layers),
        dropout_rate=float(dropout),
    )

    train_kwargs: dict[str, Any] = {"early_stopping": bool(early_stopping)}
    if max_epochs is not None:
        train_kwargs["max_epochs"] = int(max_epochs)
    train_kwargs["accelerator"] = "gpu" if use_gpu else "cpu"
    train_kwargs["devices"] = 1
    model.train(**train_kwargs)

    latent = np.asarray(model.get_latent_representation(), dtype=float)
    if latent.ndim != 2:
        raise ScVITrainOpError("scVI latent representation is not 2D")
    metrics = {
        "final_train_loss": _history_last(model, ["train_loss", "training_loss", "elbo_train"]),
        "final_validation_loss": _history_last(
            model, ["validation_loss", "val_loss", "elbo_validation"]
        ),
    }
    return model, latent, getattr(scvi, "__version__", None), metrics


def _save_model(bundle, artifact_id: str, model) -> str:
    rel = Path("artifact_models") / f"{artifact_id}_scvi"
    model_dir = bundle.bundle_dir / rel
    model_dir.mkdir(parents=True, exist_ok=True)
    model.save(str(model_dir), overwrite=True, save_anndata=False)
    return rel.as_posix()


def _required_output(outputs_spec: dict[str, Any], name: str, expected_type: str) -> dict[str, Any]:
    spec = outputs_spec.get(name)
    if not isinstance(spec, dict):
        raise ScVITrainOpError(f"scvi_train requires preregistered '{name}' output")
    if str(spec.get("type") or "") != expected_type:
        raise ScVITrainOpError(f"scvi_train output '{name}' must have type={expected_type}")
    if not str(spec.get("artifact_id") or ""):
        raise ScVITrainOpError(f"scvi_train output '{name}' requires artifact_id")
    return spec


def _run(req: dict[str, Any]) -> dict[str, Any]:
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {}) or {}
        params = req.get("params", {}) or {}
        outputs_spec = req.get("outputs", {}) or {}
        model_spec = _required_output(outputs_spec, "model", "model_ref")
        fit_summary_spec = _required_output(outputs_spec, "fit_summary", "obs_table")

        n_latent = int(params.get("n_latent", 30))
        if n_latent < 2:
            raise ScVITrainOpError("n_latent must be >= 2")
        n_hidden = int(params.get("n_hidden", 128))
        if n_hidden < 1:
            raise ScVITrainOpError("n_hidden must be >= 1")
        n_layers = int(params.get("n_layers", 2))
        if n_layers < 1:
            raise ScVITrainOpError("n_layers must be >= 1")
        dropout = float(params.get("dropout", 0.1))
        if not (0.0 <= dropout <= 1.0):
            raise ScVITrainOpError("dropout must be between 0 and 1")
        max_epochs = params.get("max_epochs")
        max_epochs = None if max_epochs is None else int(max_epochs)
        early_stopping = bool(params.get("early_stopping", True))
        seed = params.get("seed")
        seed = None if seed is None else int(seed)
        use_gpu = bool(params.get("use_gpu", False))
        output_space_id = str(
            params.get("output_space_id") or params.get("space_id_out") or "rna.scvi"
        )
        overwrite = bool(params.get("overwrite", False))
        cell_set = str(inputs.get("cell_set") or params.get("cell_set") or "all")

        adata, staged = _build_anndata_from_staged_chunks(inputs, params)
        matrix_id = str(params.get("matrix_id") or staged["matrix_id"] or inputs.get("matrix") or "")
        if not matrix_id:
            raise ScVITrainOpError("matrix_id is required")
        model, latent, scvi_version, metrics = _train_scvi(
            adata,
            batch_key=staged["batch_key"],
            categorical_covariates=staged["categorical_covariates"],
            continuous_covariates=staged["continuous_covariates"],
            n_latent=n_latent,
            n_hidden=n_hidden,
            n_layers=n_layers,
            dropout=dropout,
            max_epochs=max_epochs,
            early_stopping=early_stopping,
            seed=seed,
            use_gpu=use_gpu,
        )
        if latent.shape[0] != len(staged["cell_ids"]):
            raise ScVITrainOpError("scVI latent row count does not match staged cells")

        space_summary = add_space(
            b,
            space_id=output_space_id,
            coords=latent,
            cell_id=[str(x) for x in adata.obs_names.tolist()],
            description=f"scVI latent space from {matrix_id}",
            source=f"scvi_train:{matrix_id}",
            overwrite=overwrite,
        )

        model_path = _save_model(b, model_spec["artifact_id"], model)
        out_receipts = {
            "model": write_artifact_model_ref_payload(
                b,
                artifact_id=model_spec["artifact_id"],
                engine="scvi",
                model_space_id=output_space_id,
                in_space_id=output_space_id,
                model_type="scvi_train",
                model_path=model_path,
            )
        }

        fit_row = {
            "matrix_id": matrix_id,
            "cell_set": cell_set,
            "n_cells": len(staged["cell_ids"]),
            "n_features": len(staged["feature_ids"]),
            "n_latent": n_latent,
            "n_hidden": n_hidden,
            "n_layers": n_layers,
            "dropout": dropout,
            "batch_key": staged["batch_key"],
            "categorical_covariates_json": json.dumps(staged["categorical_covariates"]),
            "continuous_covariates_json": json.dumps(staged["continuous_covariates"]),
            "max_epochs": max_epochs,
            "early_stopping": early_stopping,
            "seed": seed,
            "use_gpu": use_gpu,
            "output_space_id": output_space_id,
            "scvi_tools_version": scvi_version,
            "pybench_version": PYBENCH_VERSION,
            "final_train_loss": metrics["final_train_loss"],
            "final_validation_loss": metrics["final_validation_loss"],
        }
        out_receipts["fit_summary"] = write_artifact_table_payload(
            b,
            artifact_id=fit_summary_spec["artifact_id"],
            artifact_type="obs_table",
            table_rows=[fit_row],
            table_type="scvi_fit_summary",
            entity_kind="run_summary",
            meta={"space": space_summary, "engine": "scvi"},
        )

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
    ap = argparse.ArgumentParser(description="pybench scVI train op")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(
            op_name=str(req.get("op_name", "scvi_train")),
            run_id=str((req.get("run") or {}).get("run_id", "<unknown>")),
            exc=e,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
