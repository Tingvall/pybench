from __future__ import annotations

import argparse
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd
import palantir

from pybench import close_bundle, open_bundle
from pybench.artifacts_payload import (
    write_artifact_cell_vector_payload,
    write_artifact_model_ref_payload,
    write_artifact_relation_family_payload,
    write_artifact_table_payload,
)
from pybench.getters import cells as get_cells
from pybench.getters import space as get_space
from pybench.protocol import error_receipt_from_exception, load_request, ok_receipt, write_receipt


PYBENCH_VERSION = "0.1.0"


class PalantirOpError(RuntimeError):
    pass


def _as_int_list(x: Any) -> list[int] | None:
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return [int(v) for v in x]
    return [int(x)]


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
    raise PalantirOpError(f"invalid boolean value: {x!r}")


def _resolve_fit_space_id(inputs: dict[str, Any]) -> str:
    val = inputs.get("fit_space", inputs.get("space_id"))
    if isinstance(val, str) and val:
        return val
    if isinstance(val, dict):
        sid = val.get("space_id")
        if isinstance(sid, str) and sid:
            return sid
    raise PalantirOpError("inputs.fit_space (space_id string) is required")


def _resolve_cells(inputs: dict[str, Any], bundle) -> tuple[list[str], str]:
    if isinstance(inputs.get("cell_ids"), list) and inputs["cell_ids"]:
        return [str(x) for x in inputs["cell_ids"]], str(inputs.get("cell_set", "all"))
    cell_set = str(inputs.get("cell_set", "core"))
    return get_cells(bundle, cell_set=cell_set), cell_set


def _resolve_terminal_cells(inputs: dict[str, Any], params: dict[str, Any]) -> list[str] | None:
    for src in (inputs, params):
        vals = src.get("terminal_cells")
        if vals is None:
            continue
        if not isinstance(vals, (list, tuple)):
            raise PalantirOpError("terminal_cells must be a list of cell IDs when provided")
        out = [str(v) for v in vals if str(v)]
        return list(dict.fromkeys(out))
    return None


def _terminal_states_from_cells(terminal_cells: list[str] | None) -> list[str] | None:
    if terminal_cells is None:
        return None
    if len(terminal_cells) == 0:
        return None
    # Pass explicit terminal cell IDs directly. Palantir accepts list-like terminal
    # states and avoids Series/dict label remapping quirks in some versions.
    return [str(cid) for cid in terminal_cells]


def _space_to_df(space_obj, *, cols_prefix: str = "dim") -> pd.DataFrame:
    if isinstance(space_obj, tuple):
        mat, cell_order, dims = space_obj
        return pd.DataFrame(mat, index=[str(c) for c in cell_order], columns=[f"{cols_prefix}{d}" for d in dims])
    # 2D path from pybench.getters.space returns DataFrame(cell_id, x, y)
    df = space_obj.copy()
    if "cell_id" not in df.columns:
        raise PalantirOpError("2D space getter result missing cell_id column")
    df["cell_id"] = df["cell_id"].astype(str)
    feat_cols = [c for c in df.columns if c != "cell_id"]
    if len(feat_cols) < 2:
        raise PalantirOpError("2D space getter result missing coordinate columns")
    out = df.set_index("cell_id")[feat_cols].astype(float)
    return out


def _canonical_branch_key(col_name: Any) -> str:
    return f"branch:{str(col_name)}"


def _build_branch_membership(branch_probs: pd.DataFrame) -> tuple[list[str], list[str], list[float]]:
    keys: list[str] = []
    cell_ids: list[str] = []
    weights: list[float] = []
    if branch_probs is None or branch_probs.empty:
        return keys, cell_ids, weights
    bp = branch_probs.copy()
    bp.index = bp.index.astype(str)
    for col in bp.columns:
        k = _canonical_branch_key(col)
        s = pd.to_numeric(bp[col], errors="coerce")
        mask = s.notna()
        if not mask.any():
            continue
        idx = s.index[mask].astype(str).tolist()
        vals = s.loc[mask].astype(float).tolist()
        keys.extend([k] * len(idx))
        cell_ids.extend(idx)
        weights.extend(vals)
    return keys, cell_ids, weights


def _build_branch_summary(branch_probs: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if branch_probs is None or branch_probs.empty:
        return rows
    bp = branch_probs.copy()
    bp.index = bp.index.astype(str)
    for col in bp.columns:
        s = pd.to_numeric(bp[col], errors="coerce")
        s_non_na = s.dropna()
        if s_non_na.empty:
            rows.append({
                "relation_key": _canonical_branch_key(col),
                "branch": str(col),
                "n_cells_prob_gt_0": 0,
                "mean_prob": 0.0,
                "max_prob": 0.0,
                "top_cell_id": None,
            })
            continue
        gt0 = s_non_na[s_non_na > 0]
        top_idx = s_non_na.idxmax() if len(s_non_na) else None
        rows.append({
            "relation_key": _canonical_branch_key(col),
            "branch": str(col),
            "n_cells_prob_gt_0": int((s_non_na > 0).sum()),
            "mean_prob": float(s_non_na.mean()),
            "max_prob": float(s_non_na.max()),
            "top_cell_id": None if top_idx is None else str(top_idx),
        })
    return rows


def _serialize_model(bundle, artifact_id: str, payload: Any) -> str:
    model_dir = Path(bundle.bundle_dir) / "artifact_models"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{artifact_id}.pkl"
    with model_path.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return str(model_path)


def _run(req: dict[str, Any]) -> dict[str, Any]:
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {})
        params = req.get("params", {})
        outputs_spec = req.get("outputs", {})

        fit_space_id = _resolve_fit_space_id(inputs)
        cell_ids, cell_set_name = _resolve_cells(inputs, b)
        if not cell_ids:
            raise PalantirOpError("effective cell universe is empty")

        root_cell = inputs.get("root_cell", params.get("root_cell"))
        if not isinstance(root_cell, str) or not root_cell:
            raise PalantirOpError("root_cell (single cell id) is required")
        root_cell = str(root_cell)
        if root_cell not in set(cell_ids):
            raise PalantirOpError(f"root_cell not found in effective cells: {root_cell}")
        terminal_cells = _resolve_terminal_cells(inputs, params)
        if terminal_cells is not None:
            missing_terminals = [cid for cid in terminal_cells if cid not in set(cell_ids)]
            if missing_terminals:
                raise PalantirOpError(
                    "terminal_cells not found in effective cells: " + ", ".join(missing_terminals[:10])
                )

        dims = _as_int_list(params.get("dims"))
        knn = int(params.get("knn", 30))
        n_diffusion_components = int(params.get("n_diffusion_components", 10))
        n_waypoints = int(params.get("n_waypoints", 300))
        seed = params.get("seed", 20)
        seed = None if seed is None else int(seed)
        use_early_cell_as_start = _as_bool(params.get("use_early_cell_as_start", True), default=True)

        X = _space_to_df(get_space(b, fit_space_id, cells_order=cell_ids, dims=dims))
        if root_cell not in X.index:
            raise PalantirOpError(f"root_cell missing after fit_space alignment: {root_cell}")

        dm = palantir.utils.run_diffusion_maps(
            X,
            n_components=n_diffusion_components,
            knn=knn,
            seed=0 if seed is None else seed,
        )
        ms = palantir.utils.determine_multiscale_space(dm)
        if ms is None:
            raise PalantirOpError("palantir.utils.determine_multiscale_space() returned None")

        pres = palantir.core.run_palantir(
            ms,
            early_cell=root_cell,
            terminal_states=_terminal_states_from_cells(terminal_cells),
            knn=knn,
            num_waypoints=n_waypoints,
            use_early_cell_as_start=use_early_cell_as_start,
            seed=20 if seed is None else seed,
        )
        if pres is None:
            raise PalantirOpError("palantir.core.run_palantir() returned None")

        # Normalize outputs to pandas objects
        pseudotime = getattr(pres, "pseudotime", None)
        branch_probs = getattr(pres, "branch_probs", None)
        entropy = getattr(pres, "entropy", None)
        if pseudotime is None:
            raise PalantirOpError("Palantir result missing pseudotime")
        pseudotime = pd.Series(pseudotime)
        pseudotime.index = pseudotime.index.astype(str)

        if branch_probs is not None:
            branch_probs = pd.DataFrame(branch_probs)
            branch_probs.index = branch_probs.index.astype(str)
        if terminal_cells and (branch_probs is None or branch_probs.shape[1] == 0):
            raise PalantirOpError(
                "Palantir returned no branch probabilities despite explicit terminal_cells. "
                "Check terminal endpoints and fit space."
            )
        if entropy is not None:
            entropy = pd.Series(entropy)
            entropy.index = entropy.index.astype(str)

        out_receipts: dict[str, Any] = {}

        if "pseudotime" in outputs_spec:
            spec = outputs_spec["pseudotime"]
            pt_aligned = pseudotime.reindex(cell_ids)
            if pt_aligned.isna().any():
                raise PalantirOpError("pseudotime contains missing values after reindex to effective cells")
            out_receipts["pseudotime"] = write_artifact_cell_vector_payload(
                b,
                artifact_id=spec["artifact_id"],
                cell_id=cell_ids,
                value=pt_aligned.astype(float).tolist(),
                value_type="continuous",
            )

        if "branch_membership" in outputs_spec:
            spec = outputs_spec["branch_membership"]
            keys, rel_cells, weights = _build_branch_membership(branch_probs if branch_probs is not None else pd.DataFrame(index=cell_ids))
            out_receipts["branch_membership"] = write_artifact_relation_family_payload(
                b,
                artifact_id=spec["artifact_id"],
                relation_key=keys,
                cell_id=rel_cells,
                weight=weights,
            )

        if "branch_summary" in outputs_spec:
            spec = outputs_spec["branch_summary"]
            rows = _build_branch_summary(branch_probs if branch_probs is not None else pd.DataFrame(index=cell_ids))
            out_receipts["branch_summary"] = write_artifact_table_payload(
                b,
                artifact_id=spec["artifact_id"],
                artifact_type=spec.get("type", "obs_table"),
                table_rows=rows,
                table_type="branch_summary",
                entity_kind="branch",
                meta={
                    "fit_space_id": fit_space_id,
                    "weight_semantics": "palantir_branch_probability",
                    "branch_membership_key_prefix": "branch:",
                },
            )

        if "entropy" in outputs_spec and entropy is not None:
            spec = outputs_spec["entropy"]
            ent_aligned = entropy.reindex(cell_ids)
            # entropy may be missing for some cells in edge cases; write NaN will fail. enforce presence in v1.
            if ent_aligned.isna().any():
                raise PalantirOpError("entropy contains missing values after reindex to effective cells")
            out_receipts["entropy"] = write_artifact_cell_vector_payload(
                b,
                artifact_id=spec["artifact_id"],
                cell_id=cell_ids,
                value=ent_aligned.astype(float).tolist(),
                value_type="continuous",
            )

        if "model" in outputs_spec:
            spec = outputs_spec["model"]
            model_payload = {
                "engine": "palantir",
                "fit_space_id": fit_space_id,
                "root_cell": root_cell,
                "terminal_cells": terminal_cells,
                "dims": dims,
                "params": {
                    "knn": knn,
                    "n_diffusion_components": n_diffusion_components,
                        "n_waypoints": n_waypoints,
                        "seed": seed,
                        "use_early_cell_as_start": use_early_cell_as_start,
                },
                "palantir_version": getattr(palantir, "__version__", None),
                "presults": pres,
            }
            model_path = _serialize_model(b, spec["artifact_id"], model_payload)
            out_receipts["model"] = write_artifact_model_ref_payload(
                b,
                artifact_id=spec["artifact_id"],
                engine="palantir",
                model_space_id=fit_space_id,
                in_space_id=fit_space_id,
                model_type="palantir_fit",
                model_path=model_path,
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
    ap = argparse.ArgumentParser(description="pybench Palantir fit op")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(
            op_name=str(req.get("op_name", "palantir_fit")),
            run_id=str((req.get("run") or {}).get("run_id", "<unknown>")),
            exc=e,
        )
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
