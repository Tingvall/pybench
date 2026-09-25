"""Bounded typed-only Palantir computation; no bundle or registry authority.

This is deliberately not an operation-pack entry point. The caller authenticates
typed inputs, owns the private stage, and publishes outputs only after validating
this process's response. Model pickle loading requires a caller-verified G1 copy
and an independently checked SHA-256; neither check makes pickle safe for an
untrusted model.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pickle
import sys
from pathlib import Path
from typing import Any

import numpy as np
import palantir
import pandas as pd
from anndata import AnnData

SCHEMA = "pybench.prototype.palantir_typed.v1"
MAX_CELLS = 10_000
MAX_DIMS = 64
MAX_POINTS = 10_000


class TypedPalantirError(ValueError):
    """Malformed typed input or incompatible private model."""


def _build_branch_membership(probs: pd.DataFrame) -> tuple[list[str], list[str], list[float]]:
    keys: list[str] = []
    cell_ids: list[str] = []
    weights: list[float] = []
    for col in probs.columns:
        values = pd.to_numeric(probs[col], errors="coerce")
        present = values.notna()
        ids = values.index[present].astype(str).tolist()
        keys.extend([f"branch:{col}"] * len(ids))
        cell_ids.extend(ids)
        weights.extend(values.loc[present].astype(float).tolist())
    return keys, cell_ids, weights


def _build_branch_summary(probs: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for col in probs.columns:
        present = pd.to_numeric(probs[col], errors="coerce").dropna()
        rows.append(
            {
                "relation_key": f"branch:{col}",
                "branch": str(col),
                "n_cells_prob_gt_0": int((present > 0).sum()),
                "mean_prob": float(present.mean()) if not present.empty else 0.0,
                "max_prob": float(present.max()) if not present.empty else 0.0,
                "top_cell_id": str(present.idxmax()) if not present.empty else None,
            }
        )
    return rows


def _compute_branch_masks(
    pt: pd.Series, probs: pd.DataFrame, *, q: float, eps: float
) -> pd.DataFrame:
    cells = pt.index.astype(str).tolist()
    aligned = probs.reindex(cells)
    if aligned.isna().any(axis=None):
        raise TypedPalantirError("branch probabilities missing after cell alignment")
    ad = AnnData(X=np.zeros((len(cells), 0), dtype=float))
    ad.obs_names = cells
    ad.obs["palantir_pseudotime"] = pd.to_numeric(pt, errors="coerce").to_numpy(dtype=float)
    ad.obsm["palantir_fate_probabilities"] = aligned.copy()
    masks = palantir.presults.select_branch_cells(
        ad,
        pseudo_time_key="palantir_pseudotime",
        fate_prob_key="palantir_fate_probabilities",
        q=q,
        eps=eps,
        masks_key="branch_masks",
        save_as_df=True,
    )
    if isinstance(masks, pd.DataFrame):
        result = masks.copy()
    else:
        result = pd.DataFrame(masks, index=cells, columns=list(aligned.columns))
    result.index = result.index.astype(str)
    return result


def _fit_branch_curve(
    pt: np.ndarray,
    xy: np.ndarray,
    *,
    smoothness: float,
    sample_points: int,
    pt_interval: tuple[float, float] | None,
) -> np.ndarray:
    if pt.size < 3 or xy.shape[0] < 3 or xy.shape[1] != 2:
        raise TypedPalantirError("branch has too few cells or plot space is not 2D")
    order = np.argsort(pt)
    pt, xy = pt[order], xy[order, :]
    keep = np.isfinite(pt) & np.isfinite(xy[:, 0]) & np.isfinite(xy[:, 1])
    pt, xy = pt[keep], xy[keep, :]
    if pt.size < 3:
        raise TypedPalantirError("branch has too few finite points")
    unique = pd.DataFrame({"pt": pt, "x": xy[:, 0], "y": xy[:, 1]})
    unique = unique.groupby("pt", sort=True, as_index=False)[["x", "y"]].mean()
    pt_unique = unique["pt"].to_numpy(dtype=float)
    xy_unique = unique[["x", "y"]].to_numpy(dtype=float)
    if pt_unique.size < 3:
        raise TypedPalantirError("branch has fewer than three unique pseudotimes")
    lo, hi = (
        (float(pt_unique.min()), float(pt_unique.max()))
        if pt_interval is None
        else (float(pt_interval[0]), float(pt_interval[1]))
    )
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        raise TypedPalantirError("invalid branch pseudotime interval")
    grid = np.linspace(lo, hi, sample_points, dtype=float)
    span = xy_unique.max(axis=0) - xy_unique.min(axis=0)
    ls = smoothness * float(np.sqrt(np.sum(span**2))) / 20.0
    if not np.isfinite(ls) or ls <= 0:
        ls = 1.0
    import mellon  # noqa: PLC0415 - optional scientific dependency, only needed for projection

    try:
        from palantir.plot import no_mellon_log_messages  # type: ignore
    except ImportError:
        no_mellon_log_messages = None
    if no_mellon_log_messages is None:
        estimator = mellon.FunctionEstimator(ls=ls, sigma=ls, n_landmarks=50)
    else:
        with no_mellon_log_messages():
            estimator = mellon.FunctionEstimator(ls=ls, sigma=ls, n_landmarks=50)
    predicted = np.asarray(estimator.fit_predict(pt_unique, xy_unique, grid), dtype=float)
    if predicted.ndim != 2 or predicted.shape[1] != 2:
        raise TypedPalantirError("trajectory interpolation did not return 2D coordinates")
    return predicted


def _closed(request: dict[str, Any], allowed: set[str]) -> None:
    extra = set(request) - allowed
    if extra:
        raise TypedPalantirError(f"unknown request fields: {sorted(extra)}")


def _cells(request: dict[str, Any]) -> list[str]:
    value = request.get("cell_ids")
    if not isinstance(value, list) or not 3 <= len(value) <= MAX_CELLS:
        raise TypedPalantirError("cell_ids must contain 3..10000 ordered cells")
    if any(not isinstance(x, str) or not x for x in value) or len(set(value)) != len(value):
        raise TypedPalantirError("cell_ids must be nonempty unique strings")
    return value


def _coordinates(value: Any, cells: list[str], *, plot: bool) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise TypedPalantirError("coordinates must be numeric") from exc
    if array.ndim != 2 or array.shape[0] != len(cells):
        raise TypedPalantirError("coordinates must have one row per ordered cell")
    if plot and array.shape[1] != 2:
        raise TypedPalantirError("plot coordinates must have exactly two columns")
    if not plot and not 2 <= array.shape[1] <= MAX_DIMS:
        raise TypedPalantirError("fit coordinates must have 2..64 columns")
    if not np.isfinite(array).all():
        raise TypedPalantirError("coordinates must be finite")
    return array


def _int_param(params: dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise TypedPalantirError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def _float_param(params: dict[str, Any], name: str, default: float, lo: float, hi: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypedPalantirError(f"{name} must be numeric")
    result = float(value)
    if not np.isfinite(result) or not lo <= result <= hi:
        raise TypedPalantirError(f"{name} must be finite and in [{lo}, {hi}]")
    return result


def _parameters(raw: Any, *, project: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypedPalantirError("params must be an object")
    if project:
        _closed(
            raw,
            {
                "prob_min",
                "smoothness",
                "sample_points",
                "min_cells_per_branch",
                "mask_q",
                "mask_eps",
                "pseudotime_min",
                "pseudotime_max",
            },
        )
        params = {
            "prob_min": _float_param(raw, "prob_min", 0.05, 0, 1),
            "smoothness": _float_param(raw, "smoothness", 1, 0, 100),
            "sample_points": _int_param(raw, "sample_points", 200, 1, MAX_POINTS),
            "min_cells_per_branch": _int_param(raw, "min_cells_per_branch", 50, 1, MAX_CELLS),
            "mask_q": _float_param(raw, "mask_q", 1e-2, 0, 1),
            "mask_eps": _float_param(raw, "mask_eps", 1e-2, 0, 1),
        }
        # Preserve the legacy operation's effective lower bounds.
        params["sample_points"] = max(10, params["sample_points"])
        params["min_cells_per_branch"] = max(3, params["min_cells_per_branch"])
        lo, hi = raw.get("pseudotime_min"), raw.get("pseudotime_max")
        if (lo is None) != (hi is None):
            raise TypedPalantirError("pseudotime_min and pseudotime_max must occur together")
        if lo is not None:
            params["pseudotime_min"] = _float_param(raw, "pseudotime_min", 0, -1e9, 1e9)
            params["pseudotime_max"] = _float_param(raw, "pseudotime_max", 0, -1e9, 1e9)
            if params["pseudotime_max"] <= params["pseudotime_min"]:
                raise TypedPalantirError("pseudotime interval must increase")
        return params
    _closed(
        raw,
        {"knn", "n_diffusion_components", "n_waypoints", "seed", "use_early_cell_as_start", "dims"},
    )
    dims = raw.get("dims")
    if dims is not None and (
        not isinstance(dims, list)
        or not dims
        or any(isinstance(x, bool) or not isinstance(x, int) or x < 1 or x > MAX_DIMS for x in dims)
        or len(set(dims)) != len(dims)
    ):
        raise TypedPalantirError("dims must be unique positive 1-based column indexes")
    early = raw.get("use_early_cell_as_start", True)
    if not isinstance(early, bool):
        raise TypedPalantirError("use_early_cell_as_start must be boolean")
    return {
        "knn": _int_param(raw, "knn", 30, 2, MAX_CELLS - 1),
        "n_diffusion_components": _int_param(raw, "n_diffusion_components", 10, 2, MAX_DIMS),
        "n_waypoints": _int_param(raw, "n_waypoints", 300, 2, MAX_CELLS),
        "seed": _int_param(raw, "seed", 20, 0, 2**31 - 1),
        "use_early_cell_as_start": early,
        "dims": dims,
    }


def _stage_path(stage: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
        raise TypedPalantirError("model path must be a stage-local basename")
    path = stage / name
    if path.is_symlink() or path.exists() and not path.is_file():
        raise TypedPalantirError("model path is not a regular stage-local file")
    return path


def compute_fit(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    _closed(
        request,
        {"schema", "mode", "cell_ids", "coordinates", "root_cell", "terminal_cells", "params"},
    )
    cells = _cells(request)
    coords = _coordinates(request.get("coordinates"), cells, plot=False)
    params = _parameters(request.get("params", {}), project=False)
    dims = params["dims"]
    if dims is not None:
        if max(dims) > coords.shape[1]:
            raise TypedPalantirError("dims exceeds coordinate column count")
        coords = coords[:, np.asarray(dims) - 1]
    root = request.get("root_cell")
    terminals = request.get("terminal_cells")
    if root not in cells:
        raise TypedPalantirError("root_cell is not on the ordered cell axis")
    if (
        not isinstance(terminals, list)
        or len(terminals) == 0
        or len(set(terminals)) != len(terminals)
        or any(x not in cells for x in terminals)
    ):
        raise TypedPalantirError("terminal_cells must be distinct source cells")
    frame = pd.DataFrame(
        coords, index=cells, columns=[f"dim{i + 1}" for i in range(coords.shape[1])]
    )
    dm = palantir.utils.run_diffusion_maps(
        frame, n_components=params["n_diffusion_components"], knn=params["knn"], seed=params["seed"]
    )
    ms = palantir.utils.determine_multiscale_space(dm)
    if ms is None:
        raise TypedPalantirError("diffusion multiscale space is missing")
    pres = palantir.core.run_palantir(
        ms,
        early_cell=root,
        terminal_states=terminals,
        knn=params["knn"],
        num_waypoints=params["n_waypoints"],
        use_early_cell_as_start=params["use_early_cell_as_start"],
        seed=params["seed"],
    )
    if pres is None:
        raise TypedPalantirError("Palantir returned no result")
    pt = pd.Series(pres.pseudotime).reindex(cells)
    ent = pd.Series(pres.entropy).reindex(cells)
    probs = pd.DataFrame(pres.branch_probs)
    probs.index = probs.index.astype(str)
    if (
        probs.shape[1] == 0
        or pt.isna().any()
        or ent.isna().any()
        or not np.isfinite(pt.to_numpy(dtype=float)).all()
        or not np.isfinite(ent.to_numpy(dtype=float)).all()
    ):
        raise TypedPalantirError("Palantir result is missing required finite outputs")
    keys, rel_cells, weights = _build_branch_membership(probs)
    if not keys or len(keys) > len(cells) * len(probs.columns) or not np.isfinite(weights).all():
        raise TypedPalantirError("invalid branch relation output")
    # The R relation primitive's observed order is key-major, then cell-ID
    # lexical order; PyBench's helper alone follows the fitted source axis.
    key_rank = {f"branch:{col}": i for i, col in enumerate(probs.columns)}
    relation = sorted(zip(keys, rel_cells, weights), key=lambda row: (key_rank[row[0]], row[1]))
    branch_summary = _build_branch_summary(probs)
    model = {
        "engine": "palantir",
        "fit_space_id": None,
        "root_cell": root,
        "terminal_cells": terminals,
        "dims": dims,
        "params": {
            k: params[k]
            for k in (
                "knn",
                "n_diffusion_components",
                "n_waypoints",
                "seed",
                "use_early_cell_as_start",
            )
        },
        "palantir_version": getattr(palantir, "__version__", None),
        "presults": pres,
    }
    model_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    path = _stage_path(stage, "model.pkl")
    if path.exists():
        raise TypedPalantirError("model output already exists")
    path.write_bytes(model_bytes)
    return {
        "schema": SCHEMA,
        "mode": "fit",
        "cell_ids": cells,
        "pseudotime": [float(x) for x in pt],
        "entropy": [float(x) for x in ent],
        "branch_membership": [{"key": k, "cell_id": c, "weight": float(w)} for k, c, w in relation],
        "branch_summary": branch_summary,
        "effective_params": params,
        "model": {
            "path": path.name,
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
            "size_bytes": len(model_bytes),
        },
    }


def compute_project(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    _closed(request, {"schema", "mode", "cell_ids", "coordinates", "model", "params"})
    cells = _cells(request)
    coords = _coordinates(request.get("coordinates"), cells, plot=True)
    params = _parameters(request.get("params", {}), project=True)
    model_desc = request.get("model")
    if not isinstance(model_desc, dict):
        raise TypedPalantirError("model descriptor is required")
    _closed(model_desc, {"path", "sha256"})
    path = _stage_path(stage, model_desc.get("path"))
    digest = model_desc.get("sha256")
    if not path.is_file() or not isinstance(digest, str) or len(digest) != 64:
        raise TypedPalantirError("private model file and SHA-256 are required")
    model_bytes = path.read_bytes()
    if hashlib.sha256(model_bytes).hexdigest() != digest:
        raise TypedPalantirError("private model SHA-256 mismatch")
    payload = pickle.loads(model_bytes)  # noqa: S301 - caller-authenticated, stage-local G1 bytes
    if not isinstance(payload, dict) or payload.get("engine") != "palantir":
        raise TypedPalantirError("private model is not a Palantir model")
    pres = payload.get("presults")
    if pres is None:
        raise TypedPalantirError("private model has no fitted result")
    pt = pd.Series(pres.pseudotime)
    pt.index = pt.index.astype(str)
    probs = pd.DataFrame(pres.branch_probs)
    probs.index = probs.index.astype(str)
    if list(pt.index) != cells or list(probs.index) != cells or probs.empty:
        raise TypedPalantirError("model fitted-cell order differs from typed plot-space axis")
    plot_df = pd.DataFrame(coords, index=cells, columns=["x", "y"])
    masks = _compute_branch_masks(pt, probs, q=params["mask_q"], eps=params["mask_eps"])
    masks = masks.reindex(index=cells, columns=probs.columns, fill_value=False)
    curves: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    interval = None
    if "pseudotime_min" in params:
        interval = (params["pseudotime_min"], params["pseudotime_max"])
    for col in probs.columns:
        bp = pd.to_numeric(probs[col], errors="coerce").reindex(cells)
        ptime = pd.to_numeric(pt.reindex(cells), errors="coerce")
        native = pd.Series(masks[col].astype(bool).to_numpy(), index=cells)
        mask = native & bp.notna() & ptime.notna() & (bp > params["prob_min"])
        minimum = params["min_cells_per_branch"]
        if int(mask.sum()) < minimum:
            mask = native & bp.notna() & ptime.notna()
        if int(mask.sum()) < minimum:
            order = np.argsort((-bp.fillna(-np.inf)).to_numpy())
            top_n = min(len(order), max(minimum, params["sample_points"] // 2))
            broad = np.zeros(len(bp), dtype=bool)
            broad[order[:top_n]] = True
            mask = pd.Series(broad, index=bp.index) & bp.notna() & ptime.notna() & (bp > 0)
        if int(mask.sum()) < minimum:
            continue
        xy = _fit_branch_curve(
            ptime.loc[mask].to_numpy(dtype=float),
            plot_df.loc[mask.values, ["x", "y"]].to_numpy(dtype=float),
            smoothness=params["smoothness"],
            sample_points=params["sample_points"],
            pt_interval=interval,
        )
        key = f"branch:{col}"
        counts[key] = int(mask.sum())
        curves.extend(
            {"key": key, "point": i + 1, "x": float(x), "y": float(y)}
            for i, (x, y) in enumerate(xy)
        )
    if not curves:
        raise TypedPalantirError("no Palantir curves could be projected")
    return {
        "schema": SCHEMA,
        "mode": "project",
        "curves": curves,
        "branch_counts": counts,
        "effective_params": params,
    }


def compute(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema") != SCHEMA:
        raise TypedPalantirError("unsupported typed Palantir request schema")
    mode = request.get("mode")
    if mode == "fit":
        return compute_fit(request, stage)
    if mode == "project":
        return compute_project(request, stage)
    raise TypedPalantirError("mode must be fit or project")


def main() -> int:
    parser = argparse.ArgumentParser(description="unregistered typed-only Palantir prototype")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args()
    stage = args.request.parent.resolve(strict=True)
    if (
        args.request.is_symlink()
        or args.response.parent.resolve(strict=True) != stage
        or args.response.is_symlink()
    ):
        raise TypedPalantirError("request and response must be regular files in one private stage")
    request = json.loads(args.request.read_text(encoding="utf-8"))
    temporary = stage / ".response.tmp"
    model_preexisted = (stage / "model.pkl").exists()
    try:
        # Palantir and mellon can print progress; reserve stdout for no protocol data.
        with contextlib.redirect_stdout(sys.stderr):
            response = compute(request, stage)
        encoded = json.dumps(response, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if args.response.exists():
            raise TypedPalantirError("response output already exists")
        with temporary.open("xb") as stream:
            stream.write(encoded)
        os.replace(temporary, args.response)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if request.get("mode") == "fit" and not model_preexisted:
            (stage / "model.pkl").unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
