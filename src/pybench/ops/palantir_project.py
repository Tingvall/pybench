from __future__ import annotations

import argparse
import pickle
import time
from typing import Any

import numpy as np
import palantir
import pandas as pd
from anndata import AnnData

from pybench import close_bundle, open_bundle
from pybench.artifacts_payload import write_artifact_path_2d_family_payload
from pybench.getters import space as get_space
from pybench.protocol import (
    TrustedModelPath,
    error_receipt_from_exception,
    load_request,
    ok_receipt,
    trusted_model_path,
    validate_request,
    write_receipt,
)

PYBENCH_VERSION = "0.1.0"


class PalantirProjectOpError(RuntimeError):
    pass


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return bool(default)
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    s = str(x).strip().lower()
    if s in {"true", "t", "1", "yes", "y"}:
        return True
    if s in {"false", "f", "0", "no", "n"}:
        return False
    raise PalantirProjectOpError(f"invalid boolean value: {x!r}")


def _resolve_plot_space_id(inputs: dict[str, Any]) -> str:
    val = inputs.get("plot_space", inputs.get("space_id"))
    if isinstance(val, str) and val:
        return val
    if isinstance(val, dict):
        sid = val.get("space_id")
        if isinstance(sid, str) and sid:
            return sid
    raise PalantirProjectOpError("inputs.plot_space (space_id string) is required")


def _resolve_model_path(inputs: dict[str, Any]) -> str:
    for key in ("model_path", "model"):
        v = inputs.get(key)
        if isinstance(v, str) and v:
            # `model` may be passed as a path string by the R bridge.
            if key == "model" and not (v.endswith('.pkl') or v.endswith('.rds') or '/' in v):
                continue
            return v
        if isinstance(v, dict):
            mp = v.get("model_path")
            if isinstance(mp, str) and mp:
                return mp
    raise PalantirProjectOpError("inputs.model_path is required (palantir model_ref payload path)")


def _space_to_df(space_obj) -> pd.DataFrame:
    # 2D path from pybench.getters.space returns DataFrame(cell_id, x, y)
    df = space_obj.copy()
    if "cell_id" not in df.columns:
        raise PalantirProjectOpError("2D space getter result missing cell_id column")
    df["cell_id"] = df["cell_id"].astype(str)
    feat_cols = [c for c in df.columns if c != "cell_id"]
    if len(feat_cols) != 2:
        raise PalantirProjectOpError("palantir_project requires a 2D plot space")
    out = df.set_index("cell_id")[feat_cols].astype(float)
    out.columns = ["x", "y"]
    return out


def _canonical_branch_key(col_name: Any) -> str:
    return f"branch:{str(col_name)}"


def _load_palantir_model_payload(model_path: TrustedModelPath) -> dict[str, Any]:
    if not isinstance(model_path, TrustedModelPath):
        raise PalantirProjectOpError(
            "MODEL_DESERIALIZATION_TRUST_REQUIRED: Palantir loading requires a validated operation path"
        )
    with model_path.absolute.open("rb") as fh:
        payload = pickle.load(fh)
    if not isinstance(payload, dict):
        raise PalantirProjectOpError("palantir model payload is not a dict")
    if str(payload.get("engine", "")) != "palantir":
        raise PalantirProjectOpError("model payload engine is not 'palantir'")
    return payload


def _compute_branch_masks(pseudotime: pd.Series, branch_probs: pd.DataFrame, *, q: float = 1e-2, eps: float = 1e-2) -> pd.DataFrame:
    # Use Palantir's native branch selection logic instead of a simple branch_prob threshold.
    idx = [str(x) for x in pseudotime.index.astype(str).tolist()]
    pt = pd.Series(pd.to_numeric(pseudotime, errors="coerce"), index=idx)
    bp = branch_probs.copy()
    bp.index = bp.index.astype(str)
    bp = bp.reindex(idx)
    if bp.isna().any(axis=None):
        raise PalantirProjectOpError("branch_probs contains missing values after alignment for branch_masks")
    ad = AnnData(X=np.zeros((len(idx), 0), dtype=float))
    ad.obs_names = idx
    ad.obs["palantir_pseudotime"] = pt.to_numpy(dtype=float)
    ad.obsm["palantir_fate_probabilities"] = bp.copy()
    masks = palantir.presults.select_branch_cells(
        ad,
        pseudo_time_key="palantir_pseudotime",
        fate_prob_key="palantir_fate_probabilities",
        q=float(q),
        eps=float(eps),
        masks_key="branch_masks",
        save_as_df=True,
    )
    if isinstance(masks, pd.DataFrame):
        out = masks.copy()
    else:
        cols = list(bp.columns)
        out = pd.DataFrame(masks, index=idx, columns=cols)
    out.index = out.index.astype(str)
    return out


def _fit_branch_curve(pt: np.ndarray,
                      xy: np.ndarray,
                      *,
                      smoothness: float,
                      sample_points: int,
                      pt_interval: tuple[float, float] | None = None) -> np.ndarray:
    if pt.size < 3 or xy.shape[0] < 3:
        raise PalantirProjectOpError("branch has too few cells for trajectory interpolation")
    if xy.shape[1] != 2:
        raise PalantirProjectOpError("plot_space must be 2D")

    order = np.argsort(pt)
    pt = pt[order]
    xy = xy[order, :]

    keep = np.isfinite(pt) & np.isfinite(xy[:, 0]) & np.isfinite(xy[:, 1])
    pt = pt[keep]
    xy = xy[keep, :]
    if pt.size < 3:
        raise PalantirProjectOpError("branch has too few finite points for interpolation")

    # Collapse duplicate pseudotime values by weighted mean in observed order; mellon expects x/y paired to scalar predictor.
    xy_df = pd.DataFrame({"pt": pt, "x": xy[:, 0], "y": xy[:, 1]})
    xy_df = xy_df.groupby("pt", sort=True, as_index=False)[["x", "y"]].mean()
    pt_u = xy_df["pt"].to_numpy(dtype=float)
    xy_u = xy_df[["x", "y"]].to_numpy(dtype=float)
    if pt_u.size < 3:
        raise PalantirProjectOpError("branch has <3 unique pseudotime values after deduplication")

    if pt_interval is None:
        lo = float(np.min(pt_u))
        hi = float(np.max(pt_u))
    else:
        lo, hi = float(pt_interval[0]), float(pt_interval[1])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        raise PalantirProjectOpError("invalid pseudotime interval for branch interpolation")

    grid = np.linspace(lo, hi, int(sample_points), dtype=float)
    span = np.max(xy_u, axis=0) - np.min(xy_u, axis=0)
    ls = float(smoothness) * float(np.sqrt(np.sum(span ** 2))) / 20.0
    if not np.isfinite(ls) or ls <= 0:
        ls = 1.0

    # Mirror Palantir plotting interpolation (mellon FunctionEstimator) without plotting.
    try:
        from palantir.plot import no_mellon_log_messages  # type: ignore
    except Exception:  # noqa: BLE001
        no_mellon_log_messages = None
    import mellon  # type: ignore

    if no_mellon_log_messages is None:
        umap_est = mellon.FunctionEstimator(ls=ls, sigma=ls, n_landmarks=50)
    else:
        with no_mellon_log_messages():
            umap_est = mellon.FunctionEstimator(ls=ls, sigma=ls, n_landmarks=50)
    pred = umap_est.fit_predict(pt_u, xy_u, grid)
    pred = np.asarray(pred, dtype=float)
    if pred.ndim != 2 or pred.shape[1] != 2:
        raise PalantirProjectOpError("mellon trajectory interpolation returned unexpected shape")
    return pred


def _run(req: dict[str, Any]) -> dict[str, Any]:
    validate_request(req)
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {}) or {}
        params = req.get("params", {}) or {}
        outputs_spec = req.get("outputs", {}) or {}

        plot_space_id = _resolve_plot_space_id(inputs)
        model_path = trusted_model_path(
            req,
            b,
            _resolve_model_path(inputs),
            expected_type="file",
        )
        payload = _load_palantir_model_payload(model_path)
        pres = payload.get("presults")
        if pres is None:
            raise PalantirProjectOpError("palantir model payload missing presults")

        pseudotime = getattr(pres, "pseudotime", None)
        branch_probs = getattr(pres, "branch_probs", None)
        if pseudotime is None:
            raise PalantirProjectOpError("PResults missing pseudotime")
        if branch_probs is None:
            raise PalantirProjectOpError("PResults missing branch_probs")
        pseudotime = pd.Series(pseudotime)
        pseudotime.index = pseudotime.index.astype(str)
        branch_probs = pd.DataFrame(branch_probs)
        branch_probs.index = branch_probs.index.astype(str)
        if branch_probs.empty or branch_probs.shape[1] == 0:
            raise PalantirProjectOpError("PResults branch_probs is empty; palantir_project cannot derive branch trajectories")

        prob_min = float(params.get("prob_min", 0.05))
        smoothness = float(params.get("smoothness", 1.0))
        sample_points = int(params.get("sample_points", params.get("approx_points", 200)))
        min_cells_per_branch = int(params.get("min_cells_per_branch", 50))
        mask_q = float(params.get("mask_q", 1e-2))
        mask_eps = float(params.get("mask_eps", 1e-2))
        if sample_points < 10:
            sample_points = 10
        if min_cells_per_branch < 3:
            min_cells_per_branch = 3

        pt_interval = None
        lo = params.get("pseudotime_min")
        hi = params.get("pseudotime_max")
        if lo is not None or hi is not None:
            if lo is None or hi is None:
                raise PalantirProjectOpError("pseudotime_min and pseudotime_max must be provided together")
            pt_interval = (float(lo), float(hi))

        # Align plot space rows to pseudotime/branch_probs cell order.
        cells_order = [str(c) for c in pseudotime.index.tolist()]
        plot_df = _space_to_df(get_space(b, plot_space_id, cells_order=cells_order))
        plot_df = plot_df.reindex(cells_order)
        if plot_df.isna().any(axis=None):
            missing = int(plot_df.isna().any(axis=1).sum())
            raise PalantirProjectOpError(f"plot_space missing coordinates for {missing} Palantir cells")

        branch_masks = _compute_branch_masks(pseudotime, branch_probs, q=mask_q, eps=mask_eps)
        if branch_masks.empty or branch_masks.shape[1] == 0:
            raise PalantirProjectOpError("Palantir branch_masks selection returned no branches")
        branch_masks = branch_masks.reindex(index=cells_order, columns=branch_probs.columns, fill_value=False)

        lineage_ids: list[str] = []
        points: list[int] = []
        xs: list[float] = []
        ys: list[float] = []
        branch_counts: dict[str, int] = {}

        for col in branch_probs.columns:
            bp = pd.to_numeric(branch_probs[col], errors="coerce").reindex(cells_order)
            pt = pd.to_numeric(pseudotime.reindex(cells_order), errors="coerce")
            mask_native = pd.Series(branch_masks[col].astype(bool).to_numpy(), index=cells_order)
            # Optional tightening: enforce a minimum branch probability within the native mask.
            mask = mask_native & bp.notna() & pt.notna() & (bp > prob_min)
            if int(mask.sum()) < min_cells_per_branch:
                # Fall back to Palantir native mask before broadening to top-probability cells.
                mask = mask_native & bp.notna() & pt.notna()
            if int(mask.sum()) < min_cells_per_branch:
                ord_idx = np.argsort((-bp.fillna(-np.inf)).to_numpy())
                top_n = min(len(ord_idx), max(min_cells_per_branch, sample_points // 2))
                keep_idx = ord_idx[:top_n]
                mask2 = np.zeros(len(bp), dtype=bool)
                mask2[keep_idx] = True
                mask = pd.Series(mask2, index=bp.index) & bp.notna() & pt.notna() & (bp > 0)
            n_keep = int(mask.sum())
            if n_keep < min_cells_per_branch:
                continue

            branch_pt = pt.loc[mask].to_numpy(dtype=float)
            branch_xy = plot_df.loc[mask.values, ["x", "y"]].to_numpy(dtype=float)
            traj_xy = _fit_branch_curve(
                branch_pt,
                branch_xy,
                smoothness=smoothness,
                sample_points=sample_points,
                pt_interval=pt_interval,
            )
            k = _canonical_branch_key(col)
            branch_counts[k] = int(n_keep)
            n_curve = traj_xy.shape[0]
            lineage_ids.extend([k] * n_curve)
            points.extend(list(range(1, n_curve + 1)))
            xs.extend([float(v) for v in traj_xy[:, 0]])
            ys.extend([float(v) for v in traj_xy[:, 1]])

        if not lineage_ids:
            raise PalantirProjectOpError(
                "Failed to derive any branch trajectories from Palantir branch_probs in the requested plot space"
            )

        out_receipts: dict[str, Any] = {}
        if "curves" in outputs_spec:
            spec = outputs_spec["curves"]
            out_receipts["curves"] = write_artifact_path_2d_family_payload(
                b,
                artifact_id=spec["artifact_id"],
                lineage_id=lineage_ids,
                point=points,
                x=xs,
                y=ys,
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
    ap = argparse.ArgumentParser(description="pybench Palantir project op")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(
            op_name=str(req.get("op_name", "palantir_project")),
            run_id=str((req.get("run") or {}).get("run_id", "<unknown>")),
            exc=e,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
