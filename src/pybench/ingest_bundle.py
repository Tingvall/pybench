from __future__ import annotations

import json
import random
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .bundle import BundleHandle
from .getters import cells as get_cells


class IngestError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _df(con, sql: str, params: Iterable[object] | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=tuple(params or ()))


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (str(name),),
    ).fetchone()
    return row is not None


def _require_tables(bundle: BundleHandle, names: list[str]) -> None:
    missing = [name for name in names if not _table_exists(bundle.con, name)]
    if missing:
        raise IngestError(f"bundle missing required tables: {', '.join(missing)}")


def _refresh_manifest(bundle: BundleHandle) -> None:
    man_path = bundle.bundle_dir / "manifest.json"
    if not man_path.exists():
        return
    manifest = json.loads(man_path.read_text())
    con = bundle.con
    for table_name in ("datasets", "matrices", "spaces"):
        if _table_exists(con, table_name):
            df = _df(con, f"SELECT * FROM {table_name}")
            df = df.where(pd.notna(df), None)
            manifest[table_name] = df.to_dict(orient="list")
    if _table_exists(con, "cell_set_membership"):
        vals = _df(
            con,
            "SELECT DISTINCT cell_set FROM cell_set_membership WHERE cell_set IS NOT NULL AND cell_set <> '' ORDER BY cell_set",
        )["cell_set"].astype(str).tolist()
        if "core" in vals:
            vals = ["core"] + [v for v in vals if v != "core"]
        manifest["cell_sets"] = vals
    man_path.write_text(json.dumps(manifest, indent=2, allow_nan=False))
    bundle.manifest = manifest


def _json_list(values: Iterable[object]) -> str:
    return json.dumps([str(x) for x in values], allow_nan=False)


def _new_fragment_id(matrix_id: str) -> str:
    suffix = "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(12))
    return f"frag_{matrix_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{suffix}"


def _normalize_cell_ids(cell_ids: Iterable[object]) -> list[str]:
    out = [str(x) for x in cell_ids]
    if not out:
        raise IngestError("cell_ids is empty")
    if any(not x for x in out):
        raise IngestError("cell_ids contains empty values")
    if len(set(out)) != len(out):
        raise IngestError("duplicate cell_ids detected")
    return out


def _ensure_cell_columns(bundle: BundleHandle, df: pd.DataFrame) -> None:
    cols_df = _df(bundle.con, "PRAGMA table_info(cells)")
    existing_cols = cols_df["name"].astype(str).tolist()
    for nm in [c for c in df.columns if c not in existing_cols]:
        x = df[nm]
        col_type = "TEXT"
        if pd.api.types.is_integer_dtype(x):
            col_type = "INTEGER"
        elif pd.api.types.is_bool_dtype(x):
            col_type = "INTEGER"
        elif pd.api.types.is_numeric_dtype(x):
            col_type = "REAL"
        bundle.con.execute(f'ALTER TABLE cells ADD COLUMN "{nm}" {col_type}')


def _coerce_metadata(md: pd.DataFrame) -> pd.DataFrame:
    out = md.copy()
    for col in out.columns:
        if col == "cell_id":
            continue
        if pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("Int64")
        elif isinstance(out[col].dtype, pd.CategoricalDtype):
            out[col] = out[col].astype(str)
        elif out[col].dtype == object:
            bad = out[col].map(lambda x: isinstance(x, (list, dict, set, tuple))).fillna(False)
            if bool(bad.any()):
                raise IngestError(f"metadata column {col} contains unsupported object values")
    return out


def define_cells(
    bundle: BundleHandle,
    cell_ids: Iterable[object],
    *,
    dataset_id: str = "dataset_1",
    set_name: str = "core",
    metadata: pd.DataFrame | None = None,
    source: str = "assembly",
    created_by: str | None = None,
) -> dict[str, Any]:
    _require_tables(bundle, ["cells", "cell_set_membership", "datasets"])
    ids = _normalize_cell_ids(cell_ids)
    con = bundle.con

    existing_n = int(_df(con, "SELECT COUNT(*) AS n FROM cells").iloc[0]["n"])
    if existing_n > 0:
        raise IngestError("define_cells is only valid for initial cell-universe definition")

    cell_set_name = "core" if str(set_name) == "core" else f"set:{str(set_name)}"
    cells_df = pd.DataFrame({"cell_id": ids, "dataset_id": [str(dataset_id)] * len(ids)})
    if metadata is not None:
        md = metadata.copy()
        if "cell_id" not in md.columns:
            raise IngestError("metadata must contain cell_id column")
        md["cell_id"] = md["cell_id"].astype(str)
        md = md.set_index("cell_id").reindex(ids)
        if md.shape[0] != len(ids):
            raise IngestError("metadata rows do not align to requested cell_ids")
        md = _coerce_metadata(md)
        cells_df = pd.concat([cells_df, md.reset_index(drop=True)], axis=1)

    _ensure_cell_columns(bundle, cells_df)
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in cells_df.itertuples(index=False, name=None)]
    cols = list(cells_df.columns)
    ph = ",".join(["?"] * len(cols))
    col_sql = ", ".join([f'"{c}"' for c in cols])
    with con:
        con.executemany(f"INSERT INTO cells({col_sql}) VALUES ({ph})", rows)
        csm_rows = [
            (cell_set_name, cid, str(source), _now(), created_by, f"Defined cells for {cell_set_name}")
            for cid in ids
        ]
        con.executemany(
            "INSERT INTO cell_set_membership(cell_set, cell_id, source, created_at, created_by, description) VALUES (?, ?, ?, ?, ?, ?)",
            csm_rows,
        )
        con.execute("DELETE FROM datasets")
        con.execute(
            "INSERT INTO datasets(dataset_id, n_cells) VALUES (?, ?)",
            (str(dataset_id), int(len(ids))),
        )
    _refresh_manifest(bundle)
    return {"n_defined": len(ids), "set_name": cell_set_name, "dataset_id": str(dataset_id)}


def attach_metadata(bundle: BundleHandle, metadata: pd.DataFrame) -> dict[str, Any]:
    _require_tables(bundle, ["cells"])
    if not isinstance(metadata, pd.DataFrame):
        raise IngestError("metadata must be a pandas DataFrame")
    if "cell_id" not in metadata.columns:
        raise IngestError("metadata must contain cell_id column")
    md = metadata.copy()
    md["cell_id"] = md["cell_id"].astype(str)
    if md["cell_id"].duplicated().any():
        raise IngestError("metadata contains duplicate cell_id values")
    present = set(get_cells(bundle, cell_set="all"))
    missing = [cid for cid in md["cell_id"].tolist() if cid not in present]
    if missing:
        raise IngestError(f"metadata references unknown cell_ids: {missing[:10]}")
    md = _coerce_metadata(md)
    _ensure_cell_columns(bundle, md)
    cols = [c for c in md.columns if c != "cell_id"]
    with bundle.con:
        for col in cols:
            rows = [
                (None if pd.isna(v) else v, cid)
                for v, cid in zip(md[col].tolist(), md["cell_id"].tolist(), strict=False)
            ]
            bundle.con.executemany(f'UPDATE cells SET "{col}" = ? WHERE cell_id = ?', rows)
    _refresh_manifest(bundle)
    return {"n_rows": int(md.shape[0]), "cols": cols}


def _parse_peak_id(feature_id: str) -> tuple[str | None, int | None, int | None]:
    parts = str(feature_id).replace(":", "-").split("-")
    if len(parts) < 3:
        return None, None, None
    try:
        return str(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return None, None, None


def _coerce_count_matrix(mat, *, require_integer: bool = True):
    try:
        from scipy import sparse as sps
    except Exception as e:  # noqa: BLE001
        raise IngestError("scipy is required for matrix ingest") from e

    if sps.issparse(mat):
        out = mat.tocsc(copy=True)
        vals = out.data
    else:
        arr = np.asarray(mat)
        if arr.ndim != 2:
            raise IngestError("matrix payload must be 2D")
        out = sps.csc_matrix(arr)
        vals = out.data

    if vals.size and not np.all(np.isfinite(vals)):
        raise IngestError("matrix payload contains non-finite values")
    if vals.size and np.any(vals < 0):
        raise IngestError("count matrix contains negative values")
    if require_integer and vals.size and not np.allclose(vals, np.round(vals)):
        raise IngestError("count matrix contains non-integer values; set require_integer=False for explicit experimental float ingest")

    if require_integer:
        out.data = np.asarray(np.round(out.data), dtype=np.uint32)
    return out


def _ensure_matrix_tables(bundle: BundleHandle) -> None:
    con = bundle.con
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS matrices (
          matrix_id TEXT PRIMARY KEY,
          layer TEXT,
          feature_type TEXT,
          n_features INTEGER,
          n_cells INTEGER,
          storage TEXT,
          path TEXT,
          description TEXT,
          organism TEXT,
          genome_build TEXT,
          genome_provider TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS features (
          matrix_id TEXT NOT NULL,
          feature_id TEXT NOT NULL,
          feature_type TEXT,
          chr TEXT,
          start INTEGER,
          end INTEGER,
          PRIMARY KEY (matrix_id, feature_id)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_features_matrix ON features(matrix_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_features_feature ON features(feature_id)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS matrix_fragments (
          fragment_id TEXT PRIMARY KEY,
          matrix_id TEXT NOT NULL,
          storage TEXT NOT NULL,
          path TEXT NOT NULL,
          n_features INTEGER,
          n_cells INTEGER,
          feature_ids_json TEXT NOT NULL,
          cell_ids_json TEXT NOT NULL,
          source TEXT,
          created_at TEXT NOT NULL,
          description TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_mf_matrix_id ON matrix_fragments(matrix_id)")


def _write_bpcells_matrix(mat, out_dir: Path) -> None:
    try:
        import bpcells.experimental as bp  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise IngestError("bpcells is required for BPCells matrix ingest") from e

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    bp.DirMatrix.from_scipy_sparse(mat, str(out_dir))


def attach_matrix(
    bundle: BundleHandle,
    matrix_id: str,
    mat,
    *,
    feature_ids: Iterable[object],
    cell_ids: Iterable[object],
    layer: str = "counts",
    feature_type: str = "feature",
    description: str | None = None,
    source: str = "assembly",
    require_integer: bool = True,
    overwrite: bool = True,
) -> dict[str, Any]:
    if not matrix_id or not str(matrix_id).strip():
        raise IngestError("matrix_id is required")
    matrix_id = str(matrix_id)
    feature_ids_norm = _normalize_cell_ids(feature_ids)
    cell_ids_norm = _normalize_cell_ids(cell_ids)
    _require_tables(bundle, ["cells"])
    _ensure_matrix_tables(bundle)

    present = set(get_cells(bundle, cell_set="all"))
    missing_cells = [cid for cid in cell_ids_norm if cid not in present]
    if missing_cells:
        raise IngestError(f"matrix references unknown cell_ids: {missing_cells[:10]}")

    mat2 = _coerce_count_matrix(mat, require_integer=require_integer)
    if mat2.shape != (len(feature_ids_norm), len(cell_ids_norm)):
        raise IngestError(
            "matrix shape does not match feature/cell ids: "
            f"{mat2.shape} vs ({len(feature_ids_norm)}, {len(cell_ids_norm)})"
        )

    rel_path = Path("mat") / str(matrix_id)
    abs_path = bundle.bundle_dir / rel_path
    if abs_path.exists() and not overwrite:
        raise IngestError(f"matrix path already exists: {abs_path}")
    _write_bpcells_matrix(mat2, abs_path)

    created_at = _now()
    fragment_id = _new_fragment_id(matrix_id)
    con = bundle.con

    with con:
        if overwrite:
            con.execute("DELETE FROM matrix_fragments WHERE matrix_id = ?", (matrix_id,))
            con.execute("DELETE FROM features WHERE matrix_id = ?", (matrix_id,))
            con.execute("DELETE FROM matrices WHERE matrix_id = ?", (matrix_id,))
        con.execute(
            """
            INSERT INTO matrices(
              matrix_id, layer, feature_type, n_features, n_cells,
              storage, path, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                matrix_id,
                str(layer),
                str(feature_type),
                int(mat2.shape[0]),
                int(mat2.shape[1]),
                "bpcells",
                str(rel_path),
                description,
            ),
        )
        feature_rows = []
        for fid in feature_ids_norm:
            chrom, start, end = _parse_peak_id(fid) if feature_type == "peak" else (None, None, None)
            feature_rows.append((matrix_id, fid, str(feature_type), chrom, start, end))
        con.executemany(
            "INSERT INTO features(matrix_id, feature_id, feature_type, chr, start, end) VALUES (?, ?, ?, ?, ?, ?)",
            feature_rows,
        )
        con.execute(
            """
            INSERT INTO matrix_fragments(
              fragment_id, matrix_id, storage, path, n_features, n_cells,
              feature_ids_json, cell_ids_json, source, created_at, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fragment_id,
                matrix_id,
                "bpcells",
                str(rel_path),
                int(mat2.shape[0]),
                int(mat2.shape[1]),
                _json_list(feature_ids_norm),
                _json_list(cell_ids_norm),
                str(source),
                created_at,
                description,
            ),
        )
    _refresh_manifest(bundle)
    return {
        "matrix_id": matrix_id,
        "fragment_id": fragment_id,
        "n_features": int(mat2.shape[0]),
        "n_cells": int(mat2.shape[1]),
        "path": str(rel_path),
        "require_integer": bool(require_integer),
    }
