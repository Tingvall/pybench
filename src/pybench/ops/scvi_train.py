from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pybench import add_space, close_bundle
from pybench.artifacts_payload import (
    publish_artifact_model_payload,
    write_artifact_table_payload,
)
from pybench.bundle import resolve_bundle_path, resolve_declared_input_path
from pybench.protocol import (
    error_receipt_from_exception,
    load_request,
    ok_receipt,
    open_bundle_for_request,
    validate_request,
    write_receipt,
)
from pybench.testing_boundary import enabled as mock_training_enabled
from pybench.testing_boundary import run_mock_training_boundary

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
    try:
        root = Path(stage_dir).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ScVITrainOpError(f"matrix_chunks_dir does not exist: {stage_dir}") from exc
    if not root.is_dir():
        raise ScVITrainOpError(f"matrix_chunks_dir is not a directory: {root}")

    man_path = resolve_bundle_path(
        root, "manifest.json", expected_type="file", purpose="staged matrix manifest"
    )
    try:
        manifest = json.loads(man_path.read_text())
    except Exception as e:  # noqa: BLE001
        raise ScVITrainOpError(f"malformed staged matrix manifest: {man_path}") from e
    if manifest.get("protocol") != STAGED_PROTOCOL:
        raise ScVITrainOpError(f"unsupported staged matrix protocol: {manifest.get('protocol')!r}")
    if str(manifest.get("orientation") or "") != "features_by_cells":
        raise ScVITrainOpError("scvi_train requires staged chunks with orientation='features_by_cells'")

    feat_rel = str(manifest.get("feature_ids_file") or "feature_ids.txt")
    feature_ids = _read_nonempty_lines(
        resolve_bundle_path(
            root, feat_rel, expected_type="file", purpose="staged feature IDs"
        ),
        "feature_ids file",
    )
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
        mtx_path = resolve_bundle_path(
            root,
            spec.get("mtx"),
            expected_type="file",
            purpose="staged matrix chunk",
        )
        cells_path = resolve_bundle_path(
            root,
            spec.get("cells"),
            expected_type="file",
            purpose="staged chunk cell IDs",
        )

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
        p = resolve_declared_input_path(
            root, meta_path, purpose="staged cell metadata"
        )
    else:
        p = resolve_bundle_path(
            root,
            str(manifest.get("cell_metadata_file") or "cell_metadata.tsv"),
            expected_type="file",
            purpose="staged cell metadata",
        )
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


def _metric_scalar(value: Any, metric_name: str) -> float:
    """Return the last finite scalar from a scvi-tools history value."""
    if isinstance(value, pd.DataFrame):
        if value.empty:
            raise ValueError("history table is empty")
        if metric_name in value.columns:
            value = value[metric_name].iloc[-1]
        elif len(value.columns) == 1:
            value = value.iloc[-1, 0]
        else:
            raise ValueError("history table has no matching or unambiguous metric column")
    elif isinstance(value, pd.Series):
        if value.empty:
            raise ValueError("history series is empty")
        value = value.iloc[-1]
    elif isinstance(value, dict):
        if metric_name in value:
            value = value[metric_name]
        elif len(value) == 1:
            value = next(iter(value.values()))
        else:
            raise ValueError("metric mapping has no matching or unambiguous value")

    if isinstance(value, (list, tuple, np.ndarray)):
        values = np.asarray(value).reshape(-1)
        if values.size == 0:
            raise ValueError("metric sequence is empty")
        value = values[-1]
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "item"):
        value = value.item()

    result = float(value)
    if not np.isfinite(result):
        raise ValueError("metric is not finite")
    return result


def _metric_from_mapping(
    mapping: Any,
    *,
    source: str,
    names: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    if mapping is None:
        return None, []

    errors: list[str] = []
    for name in names:
        try:
            value = mapping[name]
        except (KeyError, TypeError, IndexError):
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}:{name}: lookup failed ({type(e).__name__})")
            continue
        try:
            return {
                "value": _metric_scalar(value, name),
                "status": "available",
                "name": name,
                "source": f"{source}:{name}",
                "error": None,
            }, errors
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}:{name}: {type(e).__name__}: {e}")
    return None, errors


def _metric_evidence(model: Any, names: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    try:
        history = getattr(model, "history", None)
    except Exception as e:  # noqa: BLE001
        history = None
        errors.append(f"model.history: lookup failed ({type(e).__name__})")
    found, found_errors = _metric_from_mapping(history, source="model.history", names=names)
    errors.extend(found_errors)
    if found is not None:
        return found

    try:
        trainer = getattr(model, "trainer", None)
        callback_metrics = getattr(trainer, "callback_metrics", None)
    except Exception as e:  # noqa: BLE001
        callback_metrics = None
        errors.append(f"model.trainer.callback_metrics: lookup failed ({type(e).__name__})")
    found, found_errors = _metric_from_mapping(
        callback_metrics,
        source="model.trainer.callback_metrics",
        names=names,
    )
    errors.extend(found_errors)
    if found is not None:
        return found

    return {
        "value": None,
        "status": "extraction_failed" if errors else "unavailable",
        "name": None,
        "source": None,
        "error": " | ".join(errors) if errors else None,
    }


def _training_epoch_evidence(model: Any) -> dict[str, Any]:
    errors: list[str] = []
    try:
        history = getattr(model, "history", None)
        for name in ["train_loss_epoch", "train_loss", "training_loss", "elbo_train"]:
            try:
                value = history[name]
            except (KeyError, TypeError, IndexError):
                continue
            epochs = len(value)
            if epochs > 0:
                return {
                    "value": int(epochs),
                    "status": "available",
                    "source": f"model.history:{name}.length",
                    "error": None,
                }
            errors.append(f"model.history:{name}: history is empty")
    except Exception as e:  # noqa: BLE001
        errors.append(f"model.history: {type(e).__name__}: {e}")

    try:
        trainer = getattr(model, "trainer", None)
        current_epoch = getattr(trainer, "current_epoch", None)
        if current_epoch is not None:
            current_epoch = int(current_epoch)
            if current_epoch >= 0:
                return {
                    "value": current_epoch,
                    "status": "available",
                    "source": "model.trainer.current_epoch",
                    "error": None,
                }
    except Exception as e:  # noqa: BLE001
        errors.append(f"model.trainer.current_epoch: {type(e).__name__}: {e}")
    return {
        "value": None,
        "status": "extraction_failed" if errors else "unavailable",
        "source": None,
        "error": " | ".join(errors) if errors else None,
    }


def _early_stopping_evidence(
    model: Any,
    *,
    requested: bool,
    epochs_completed: int | None,
) -> dict[str, Any]:
    if not requested:
        return {
            "outcome": "not_requested",
            "monitor": None,
            "best_score": None,
            "best_score_status": "unavailable",
            "stopped_epoch": None,
            "wait_count": None,
            "patience": None,
            "source": None,
            "error": None,
        }

    try:
        trainer = getattr(model, "trainer", None)
        callback = getattr(trainer, "early_stopping_callback", None)
        if callback is None:
            for candidate in getattr(trainer, "callbacks", []) or []:
                if hasattr(candidate, "monitor") and hasattr(candidate, "stopped_epoch"):
                    callback = candidate
                    break
        if callback is None:
            return {
                "outcome": "unavailable",
                "monitor": None,
                "best_score": None,
                "best_score_status": "unavailable",
                "stopped_epoch": None,
                "wait_count": None,
                "patience": None,
                "source": None,
                "error": None,
            }

        monitor = getattr(callback, "monitor", None)
        stopped_epoch = getattr(callback, "stopped_epoch", None)
        stopped_epoch = None if stopped_epoch is None else int(stopped_epoch)
        reason = getattr(callback, "early_stopping_reason", None)
        trainer_should_stop = bool(getattr(trainer, "should_stop", False))
        max_epochs = getattr(trainer, "max_epochs", None)
        max_epochs = None if max_epochs is None else int(max_epochs)
        if reason is not None or trainer_should_stop or (stopped_epoch is not None and stopped_epoch > 0):
            outcome = "stopped_early"
        elif max_epochs is not None and epochs_completed is not None and epochs_completed >= max_epochs:
            outcome = "completed_max_epochs"
        else:
            outcome = "completed_without_trigger"

        best_score = getattr(callback, "best_score", None)
        try:
            best_score = _metric_scalar(best_score, "best_score")
            best_score_status = "available"
            best_score_error = None
        except Exception as e:  # noqa: BLE001
            best_score = None
            best_score_status = "unavailable" if getattr(callback, "best_score", None) is None else "extraction_failed"
            best_score_error = None if best_score_status == "unavailable" else f"{type(e).__name__}: {e}"

        return {
            "outcome": outcome,
            "monitor": None if monitor is None else str(monitor),
            "best_score": best_score,
            "best_score_status": best_score_status,
            "stopped_epoch": stopped_epoch,
            "wait_count": getattr(callback, "wait_count", None),
            "patience": getattr(callback, "patience", None),
            "source": "model.trainer.early_stopping_callback",
            "error": best_score_error,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "outcome": "extraction_failed",
            "monitor": None,
            "best_score": None,
            "best_score_status": "extraction_failed",
            "stopped_epoch": None,
            "wait_count": None,
            "patience": None,
            "source": None,
            "error": f"model.trainer.early_stopping_callback: {type(e).__name__}: {e}",
        }


def _training_evidence(model: Any, *, early_stopping_requested: bool) -> dict[str, Any]:
    train = _metric_evidence(
        model,
        ["train_loss_epoch", "train_loss", "training_loss", "elbo_train"],
    )
    validation = _metric_evidence(
        model,
        ["validation_loss", "val_loss", "elbo_validation"],
    )
    epochs = _training_epoch_evidence(model)
    early_stopping = _early_stopping_evidence(
        model,
        requested=early_stopping_requested,
        epochs_completed=epochs["value"],
    )
    return {
        "final_train_loss": train["value"],
        "final_train_loss_status": train["status"],
        "final_train_loss_metric": train["name"],
        "final_train_loss_source": train["source"],
        "final_train_loss_error": train["error"],
        "final_validation_loss": validation["value"],
        "final_validation_loss_status": validation["status"],
        "final_validation_loss_metric": validation["name"],
        "final_validation_loss_source": validation["source"],
        "final_validation_loss_error": validation["error"],
        "training_epochs_completed": epochs["value"],
        "training_epochs_status": epochs["status"],
        "training_epochs_source": epochs["source"],
        "training_epochs_error": epochs["error"],
        "early_stopping_outcome": early_stopping["outcome"],
        "early_stopping_monitor": early_stopping["monitor"],
        "early_stopping_best_score": early_stopping["best_score"],
        "early_stopping_best_score_status": early_stopping["best_score_status"],
        "early_stopping_stopped_epoch": early_stopping["stopped_epoch"],
        "early_stopping_wait_count": early_stopping["wait_count"],
        "early_stopping_patience": early_stopping["patience"],
        "early_stopping_source": early_stopping["source"],
        "early_stopping_error": early_stopping["error"],
        "training_evidence_schema": "scvi_training_evidence_v1",
    }


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
    evidence = _training_evidence(model, early_stopping_requested=early_stopping)
    return model, latent, getattr(scvi, "__version__", None), evidence


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
    validate_request(req)
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle_for_request(req)
    try:
        if mock_training_enabled():
            return ok_receipt(
                op_name=op_name,
                run_id=run_id,
                outputs=run_mock_training_boundary(req, b),
                request=req,
                bundle=b,
            )
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

        out_receipts = {
            "model": publish_artifact_model_payload(
                b,
                artifact_id=model_spec["artifact_id"],
                engine="scvi",
                model_space_id=output_space_id,
                in_space_id=output_space_id,
                model_type="scvi_train",
                model_path=f"artifact_models/{model_spec['artifact_id']}/scvi",
                expected_type="directory",
                writer=lambda path: model.save(
                    str(path), overwrite=True, save_anndata=False
                ),
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
            **metrics,
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
            request=req,
            bundle=b,
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
            request=req,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
