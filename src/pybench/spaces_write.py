from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .bundle import BundleHandle


class SpaceWriteError(RuntimeError):
    pass


def _df(con, sql: str, params: Iterable[object] | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=tuple(params or ()))


def _normalize_coords(coords, cell_id: list[str] | None = None) -> tuple[list[str], np.ndarray]:
    if isinstance(coords, pd.DataFrame):
        if "cell_id" in coords.columns:
            cell_ids = coords["cell_id"].astype(str).tolist()
            mat_df = coords.drop(columns=["cell_id"]).copy()
        else:
            if coords.index is None:
                raise SpaceWriteError("coords DataFrame must have index or a cell_id column")
            cell_ids = [str(x) for x in coords.index.tolist()]
            mat_df = coords.copy()
        if mat_df.shape[1] < 2:
            raise SpaceWriteError("coords must have at least 2 dimensions")
        # Coerce to numeric and fail on non-numeric / NA / inf
        try:
            mat = mat_df.astype(float).to_numpy(copy=True)
        except Exception as e:  # noqa: BLE001
            raise SpaceWriteError("coords contains non-numeric columns") from e
    else:
        arr = np.asarray(coords, dtype=float)
        if arr.ndim != 2:
            raise SpaceWriteError("coords must be a 2D array-like")
        if arr.shape[1] < 2:
            raise SpaceWriteError("coords must have at least 2 dimensions")
        if cell_id is None:
            raise SpaceWriteError("cell_id= is required for array-like coords without embedded labels")
        cell_ids = [str(x) for x in cell_id]
        mat = np.array(arr, dtype=float, copy=True)

    if len(cell_ids) != mat.shape[0]:
        raise SpaceWriteError("cell_id count does not match number of rows in coords")
    if len(set(cell_ids)) != len(cell_ids):
        raise SpaceWriteError("duplicate cell_id values in coords")
    if not np.isfinite(mat).all():
        raise SpaceWriteError("coords contains NA/NaN/Inf values")

    return cell_ids, mat


def _assert_cells_exist(bundle: BundleHandle, cell_ids: list[str]) -> None:
    con = bundle.con
    present = set()
    chunk_n = 500
    for i in range(0, len(cell_ids), chunk_n):
        blk = cell_ids[i : i + chunk_n]
        ph = ",".join(["?"] * len(blk))
        q = f"SELECT cell_id FROM cells WHERE cell_id IN ({ph})"
        rows = con.execute(q, tuple(blk)).fetchall()
        present.update(str(r[0]) for r in rows)
    missing = [c for c in cell_ids if c not in present]
    if missing:
        raise SpaceWriteError(f"Some cell_ids are not present in bundle. First few: {missing[:10]}")


def _json_sanitize(x):
    if isinstance(x, dict):
        return {str(k): _json_sanitize(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_sanitize(v) for v in x]
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    return x


def _refresh_manifest_spaces(bundle: BundleHandle) -> None:
    man_path = bundle.bundle_dir / "manifest.json"
    if not man_path.exists():
        return
    manifest = json.loads(man_path.read_text())
    spaces_df = _df(bundle.con, "SELECT * FROM spaces")
    # JSON compatibility: convert pandas/numpy NaN to null before writing manifest.json
    spaces_df = spaces_df.where(pd.notna(spaces_df), None)
    manifest["spaces"] = spaces_df.to_dict(orient="list")
    manifest = _json_sanitize(manifest)
    man_path.write_text(json.dumps(manifest, indent=2, allow_nan=False))
    bundle.manifest = manifest


def add_space(
    bundle: BundleHandle,
    space_id: str,
    coords,
    *,
    cell_id: list[str] | None = None,
    description: str | None = None,
    source: str | None = None,
    overwrite: bool = False,
    chunk: int = 20000,
) -> dict:
    con = bundle.con
    sid = str(space_id)
    if not sid:
        raise SpaceWriteError("space_id must be a non-empty string")

    cell_ids, mat = _normalize_coords(coords, cell_id=cell_id)
    dims = int(mat.shape[1])
    _assert_cells_exist(bundle, cell_ids)

    exists = con.execute("SELECT 1 FROM spaces WHERE space_id = ?", (sid,)).fetchone() is not None
    if exists and not overwrite:
        raise SpaceWriteError(f"space_id already exists: {sid} (set overwrite=True to replace)")

    desc = sid if description is None else str(description)
    src = "ingest" if source is None else str(source)

    with con:
        if exists:
            con.execute("DELETE FROM space_2d WHERE space_id = ?", (sid,))
            con.execute("DELETE FROM space_coords WHERE space_id = ?", (sid,))
            con.execute("DELETE FROM spaces WHERE space_id = ?", (sid,))

        storage = "wide:space_2d" if dims == 2 else "long:space_coords"
        con.execute(
            "INSERT INTO spaces(space_id, source, dims, storage, description) VALUES (?, ?, ?, ?, ?)",
            (sid, src, dims, storage, desc),
        )

        if dims == 2:
            rows = [(sid, cell_ids[i], float(mat[i, 0]), float(mat[i, 1])) for i in range(len(cell_ids))]
            con.executemany(
                "INSERT INTO space_2d(space_id, cell_id, x, y) VALUES (?, ?, ?, ?)",
                rows,
            )
        else:
            n = len(cell_ids)
            for start in range(0, n, int(chunk)):
                stop = min(start + int(chunk), n)
                sub_ids = cell_ids[start:stop]
                sub = mat[start:stop, :]
                rows = []
                for d in range(dims):
                    dim_num = d + 1
                    rows.extend((sid, sub_ids[i], dim_num, float(sub[i, d])) for i in range(len(sub_ids)))
                con.executemany(
                    "INSERT INTO space_coords(space_id, cell_id, dim, value) VALUES (?, ?, ?, ?)",
                    rows,
                )

        # best-effort indexes (mirror R behavior)
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_space_2d_space ON space_2d(space_id)",
            "CREATE INDEX IF NOT EXISTS idx_space_2d_cell ON space_2d(cell_id)",
            "CREATE INDEX IF NOT EXISTS idx_space_coords_space ON space_coords(space_id)",
            "CREATE INDEX IF NOT EXISTS idx_space_coords_cell ON space_coords(cell_id)",
        ]:
            try:
                con.execute(sql)
            except Exception:
                pass

    _refresh_manifest_spaces(bundle)
    return {
        "space_id": sid,
        "dims": dims,
        "n_cells": len(cell_ids),
        "storage": "wide:space_2d" if dims == 2 else "long:space_coords",
    }
