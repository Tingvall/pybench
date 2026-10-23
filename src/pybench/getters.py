from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .bundle import BundleHandle, bundle_relative_path, resolve_bundle_path


class GetterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactTable:
    artifact_id: str
    kind: str
    table_type: str
    entity_kind: str
    meta: dict[str, Any]
    storage_format: str | None
    columns: tuple[str, ...]
    types: tuple[str, ...]
    rows: list[dict[str, Any]]


def _table_json_number(text: str) -> int | float:
    return -0.0 if text == "-0" else int(text)


def _reject_json_constant(text: str) -> None:
    raise GetterError(f"invalid bare JSON constant in table: {text}")


def artifact_table(bundle: BundleHandle, artifact_id: str) -> ArtifactTable:
    """Read a registered table, retaining R-declared types and missing values."""

    header = bundle.con.execute(
        "SELECT a.type,a.provenance_json,t.table_type,t.entity_kind,t.meta_json "
        "FROM artifacts a JOIN artifact_tables t ON t.artifact_id=a.artifact_id "
        "WHERE a.artifact_id=?",
        (artifact_id,),
    ).fetchone()
    if header is None or header[0] not in {"obs_table", "feature_table"}:
        raise GetterError(f"table artifact not found: {artifact_id}")
    kind, provenance_text, table_type, entity_kind, meta_text = header
    provenance = json.loads(provenance_text) if provenance_text else {}
    marker = provenance.get("table_storage")
    if marker not in {None, "scworkbench.table.typed-json.v1"}:
        raise GetterError("unsupported table storage marker")
    stored_meta = json.loads(meta_text) if meta_text else {}
    if marker is None:
        names: tuple[str, ...] = ()
        types: tuple[str, ...] = ()
        meta = stored_meta
    else:
        if not isinstance(stored_meta, dict) or set(stored_meta) != {
            "storage_format",
            "columns",
            "meta",
        }:
            raise GetterError("typed table metadata envelope is malformed")
        if stored_meta["storage_format"] != marker:
            raise GetterError("typed table metadata format differs from provenance")
        columns = stored_meta["columns"]
        if not isinstance(columns, dict) or set(columns) != {"names", "types"}:
            raise GetterError("typed table column declaration is malformed")
        raw_names, raw_types = columns["names"], columns["types"]
        raw_names = [raw_names] if isinstance(raw_names, str) else raw_names
        raw_types = [raw_types] if isinstance(raw_types, str) else raw_types
        if (
            not isinstance(raw_names, list)
            or not isinstance(raw_types, list)
            or len(raw_names) != len(raw_types)
            or any(not isinstance(name, str) or not name for name in raw_names)
            or len(set(raw_names)) != len(raw_names)
            or any(
                kind not in {"integer", "double", "logical", "character", "legacy"}
                for kind in raw_types
            )
        ):
            raise GetterError("typed table column declaration is invalid")
        names, types = tuple(raw_names), tuple(raw_types)
        meta = stored_meta["meta"]
    rows: list[dict[str, Any]] = []
    cursor = bundle.con.execute(
        "SELECT row_idx,row_json FROM artifact_table_rows WHERE artifact_id=? ORDER BY row_idx",
        (artifact_id,),
    )
    while block := cursor.fetchmany(1000):
        for row_index, row_text in block:
            if row_index != len(rows) + 1:
                raise GetterError("table row indices are not contiguous")
            row = json.loads(
                row_text, parse_int=_table_json_number, parse_constant=_reject_json_constant
            )
            if not isinstance(row, dict):
                raise GetterError("table row must be a JSON object")
            if marker is not None:
                if tuple(row) != names:
                    raise GetterError("typed table row columns differ from declaration")
                for name, value_type in zip(names, types, strict=True):
                    value = row[name]
                    if value is None:
                        continue
                    if value_type == "double":
                        if isinstance(value, str):
                            if value not in {"NaN", "Inf", "-Inf"}:
                                raise GetterError("invalid non-finite double encoding")
                            row[name] = {
                                "NaN": float("nan"),
                                "Inf": float("inf"),
                                "-Inf": -float("inf"),
                            }[value]
                        elif type(value) in {int, float}:
                            if not math.isfinite(value):
                                raise GetterError(
                                    "typed double numeric token overflows its finite range"
                                )
                            row[name] = float(value)
                        else:
                            raise GetterError("typed double value has wrong JSON type")
                    elif value_type == "integer":
                        if type(value) is not int or not -(2**31) < value < 2**31:
                            raise GetterError("typed integer value has wrong JSON type")
                    elif value_type == "logical" and type(value) is not bool:
                        raise GetterError("typed logical value has wrong JSON type")
                    elif value_type == "character" and not isinstance(value, str):
                        raise GetterError("typed character value has wrong JSON type")
            rows.append(row)
    return ArtifactTable(
        artifact_id, kind, table_type, entity_kind, meta, marker, names, types, rows
    )


def _df(con, sql: str, params: Iterable[object] | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=tuple(params or ()))


def meta(
    bundle: BundleHandle,
    cells_order: list[str] | None = None,
    cols: list[str] | None = None,
    dataset_id: str | None = None,
) -> pd.DataFrame:
    con = bundle.con

    cols_df = _df(con, "PRAGMA table_info(cells)")
    all_cols = cols_df["name"].astype(str).tolist()
    if "cell_id" not in all_cols:
        raise GetterError("cells table missing cell_id column")

    if cols is None:
        use_cols = all_cols
    else:
        use_cols = [str(c) for c in cols]
        missing = [c for c in use_cols if c not in all_cols]
        if missing:
            raise GetterError(f"Unknown cells columns: {', '.join(missing)}")

    sel = ", ".join([f'"{c}"' for c in use_cols])
    q = f"SELECT {sel} FROM cells"
    params: list[object] = []
    if dataset_id is not None:
        q += " WHERE dataset_id = ?"
        params.append(dataset_id)
    q += " ORDER BY rowid"

    df = _df(con, q, params)
    if cells_order is not None:
        want = [str(c) for c in cells_order]
        have = set(df["cell_id"].astype(str))
        missing = [c for c in want if c not in have]
        if missing:
            raise GetterError(f"Some requested cells not found in cells table: {missing[:5]}")
        pos = {c: i for i, c in enumerate(want)}
        df["cell_id"] = df["cell_id"].astype(str)
        df = df[df["cell_id"].isin(want)].copy()
        df = df.assign(_ord=df["cell_id"].map(pos)).sort_values("_ord").drop(columns=["_ord"])
    return df.reset_index(drop=True)


def cells(bundle: BundleHandle, cell_set: str = "core", dataset_id: str | None = None) -> list[str]:
    con = bundle.con

    if cell_set == "all":
        if dataset_id is None:
            q = "SELECT cell_id FROM cells ORDER BY rowid"
            return _df(con, q)["cell_id"].astype(str).tolist()
        q = "SELECT cell_id FROM cells WHERE dataset_id = ? ORDER BY rowid"
        return _df(con, q, [dataset_id])["cell_id"].astype(str).tolist()

    if dataset_id is None:
        q = """
        SELECT c.cell_id
        FROM cell_set_membership m
        JOIN cells c ON c.cell_id = m.cell_id
        WHERE m.cell_set = ?
        ORDER BY c.rowid
        """
        return _df(con, q, [cell_set])["cell_id"].astype(str).tolist()

    q = """
    SELECT c.cell_id
    FROM cell_set_membership m
    JOIN cells c ON c.cell_id = m.cell_id
    WHERE m.cell_set = ? AND c.dataset_id = ?
    ORDER BY c.rowid
    """
    return _df(con, q, [cell_set, dataset_id])["cell_id"].astype(str).tolist()


def cell_sets(bundle: BundleHandle) -> list[str]:
    q = """
    SELECT DISTINCT cell_set
    FROM cell_set_membership
    WHERE cell_set IS NOT NULL AND cell_set <> ''
    ORDER BY cell_set
    """
    vals = _df(bundle.con, q)["cell_set"].astype(str).tolist()
    if "core" in vals:
        vals = ["core"] + [v for v in vals if v != "core"]
    return vals


def resolve_cell_set(bundle: BundleHandle, cell_set: str = "core") -> list[str]:
    if cell_set == "all":
        return cells(bundle, cell_set="all")
    available = set(cell_sets(bundle))
    if cell_set not in available:
        raise GetterError(f"Unknown cell_set: {cell_set}")
    q = "SELECT c.cell_id FROM cell_set_membership m JOIN cells c ON c.cell_id = m.cell_id WHERE m.cell_set = ? ORDER BY c.rowid"
    return _df(bundle.con, q, [cell_set])["cell_id"].astype(str).tolist()


def matrices(bundle: BundleHandle) -> pd.DataFrame:
    q = """
    SELECT matrix_id, layer, feature_type, n_features, n_cells, storage, path
    FROM matrices
    ORDER BY matrix_id
    """
    return _df(bundle.con, q)


def matrix_info(bundle: BundleHandle, matrix_id: str | None = None) -> pd.DataFrame:
    if matrix_id is None:
        return _df(bundle.con, "SELECT * FROM matrices")
    return _df(bundle.con, "SELECT * FROM matrices WHERE matrix_id = ?", [matrix_id])


def matrix_dir(bundle: BundleHandle, matrix_id: str) -> Path:
    info = matrix_info(bundle, matrix_id)
    if info.empty:
        raise GetterError(f"Unknown matrix_id: {matrix_id}")
    rel = str(info.iloc[0].get("path") or "")
    if not rel:
        raise GetterError(f"Matrix path missing for matrix_id: {matrix_id}")
    _, absolute = bundle_relative_path(
        bundle,
        rel,
        expected_type="directory",
        inspect_tree=True,
        purpose=f"matrix payload {matrix_id}",
    )
    return absolute


def _matrix_relative_path(bundle: BundleHandle, matrix_id: str) -> str:
    info = matrix_info(bundle, matrix_id)
    if info.empty:
        raise GetterError(f"Unknown matrix_id: {matrix_id}")
    raw = info.iloc[0].get("path")
    relative, _ = bundle_relative_path(
        bundle,
        raw,
        expected_type="directory",
        inspect_tree=True,
        purpose=f"matrix payload {matrix_id}",
    )
    return relative


def features(
    bundle: BundleHandle, matrix_id: str | None = None, feature_type: str | None = None
) -> pd.DataFrame:
    q = "SELECT matrix_id, feature_id, feature_type, chr, start, end FROM features"
    where = []
    params: list[object] = []
    if matrix_id is not None:
        where.append("matrix_id = ?")
        params.append(matrix_id)
    if feature_type is not None:
        where.append("feature_type = ?")
        params.append(feature_type)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY matrix_id, rowid"
    return _df(bundle.con, q, params)


def feature_ids(bundle: BundleHandle, matrix_id: str, feature_type: str | None = None) -> list[str]:
    q = "SELECT feature_id FROM features WHERE matrix_id = ?"
    params: list[object] = [matrix_id]
    if feature_type is not None:
        q += " AND feature_type = ?"
        params.append(feature_type)
    q += " ORDER BY rowid"
    df = _df(bundle.con, q, params)
    out = df["feature_id"].astype(str).tolist()
    if not out:
        raise GetterError(f"No features found for matrix_id: {matrix_id}")
    return out


def _read_matrix_chunk_subprocess(
    bundle: BundleHandle,
    matrix_relative: str,
    feature_idx: list[int] | None,
    col_idx: list[int],
):
    try:
        from scipy import sparse as sps  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise GetterError("scipy is required for bundle-native matrix reads") from e

    with tempfile.TemporaryDirectory(prefix="pybench_matrix_chunk_") as tmpdir:
        tmp = Path(tmpdir)
        cols_path = tmp / "cols.npy"
        out_path = tmp / "chunk.npz"
        np.save(cols_path, np.asarray(col_idx, dtype=np.int64))

        cmd = [
            sys.executable,
            "-m",
            "pybench.tools.read_matrix_chunk",
            "--bundle-dir",
            str(bundle.bundle_dir),
            "--matrix-path",
            matrix_relative,
            "--cols-path",
            str(cols_path),
            "--out-path",
            str(out_path),
        ]

        if feature_idx is not None:
            rows_path = tmp / "rows.npy"
            np.save(rows_path, np.asarray(feature_idx, dtype=np.int64))
            cmd.extend(["--rows-path", str(rows_path)])

        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            stderr = (res.stderr or "").strip()
            stdout = (res.stdout or "").strip()
            detail = stderr or stdout or f"exit status {res.returncode}"
            raise GetterError(
                "subprocess matrix read failed for "
                f"{Path(matrix_relative).name} cols={col_idx[:3]}... len={len(col_idx)}: {detail}"
            )
        return sps.load_npz(out_path).tocsc()


def matrix(
    bundle: BundleHandle,
    matrix_id: str,
    cells_order: list[str] | None = None,
    feature_order: list[str] | None = None,
):
    try:
        from scipy import sparse as sps  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise GetterError("scipy is required for bundle-native matrix reads") from e

    chunk_cols = int(os.getenv("PYBENCH_MATRIX_READ_CHUNK_CELLS", "1"))
    if chunk_cols < 1:
        raise GetterError("PYBENCH_MATRIX_READ_CHUNK_CELLS must be >= 1")

    matrix_relative = _matrix_relative_path(bundle, matrix_id)

    all_feature_ids = feature_ids(bundle, matrix_id)
    all_cell_ids = cells(bundle, cell_set="all")

    if feature_order is None:
        feature_idx = None
        use_feature_ids = all_feature_ids
    else:
        use_feature_ids = [str(x) for x in feature_order]
        pos = {fid: i for i, fid in enumerate(all_feature_ids)}
        missing = [fid for fid in use_feature_ids if fid not in pos]
        if missing:
            raise GetterError(
                f"Some requested features not found in matrix {matrix_id}: {missing[:5]}"
            )
        feature_idx = [int(pos[fid]) for fid in use_feature_ids]

    if cells_order is None:
        use_cell_ids = all_cell_ids
    else:
        use_cell_ids = [str(x) for x in cells_order]
        pos = {cid: i for i, cid in enumerate(all_cell_ids)}
        missing = [cid for cid in use_cell_ids if cid not in pos]
        if missing:
            raise GetterError(
                f"Some requested cells not found in matrix {matrix_id}: {missing[:5]}"
            )
    cell_pos = {cid: i for i, cid in enumerate(all_cell_ids)}
    cell_idx = [int(cell_pos[cid]) for cid in use_cell_ids]

    parts = []
    for start in range(0, len(cell_idx), chunk_cols):
        part_idx = cell_idx[start : start + chunk_cols]
        part = _read_matrix_chunk_subprocess(bundle, matrix_relative, feature_idx, part_idx)
        parts.append(part)

    if not parts:
        out = sps.csc_matrix((len(use_feature_ids), 0))
    elif len(parts) == 1:
        out = parts[0]
    else:
        out = sps.hstack(parts, format="csc")
    return out, use_feature_ids, use_cell_ids


def feature_sets(
    bundle: BundleHandle,
    matrix_id: str | None = None,
    namespace: str | None = None,
    organism: str | None = None,
    origin: str | None = None,
) -> pd.DataFrame:
    q = """
    SELECT
      fs.feature_set,
      fs.matrix_id,
      COALESCE(COUNT(fsm.feature_id), 0) AS n_features,
      fs.set_id,
      fs.namespace,
      fs.organism,
      fs.source_id,
      fs.source_version,
      fs.origin,
      fs.recipe_json,
      fs.is_readonly,
      fs.created_at,
      fs.updated_at,
      fs.created_by,
      fs.description
    FROM feature_sets fs
    LEFT JOIN feature_set_membership fsm
      ON fs.feature_set = fsm.feature_set
     AND fs.matrix_id = fsm.matrix_id
    """
    where = []
    params: list[object] = []
    for col, val in (
        ("fs.matrix_id", matrix_id),
        ("fs.namespace", namespace),
        ("fs.organism", organism),
        ("fs.origin", origin),
    ):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += """
    GROUP BY
      fs.feature_set, fs.matrix_id, fs.set_id, fs.namespace, fs.organism,
      fs.source_id, fs.source_version, fs.origin, fs.recipe_json, fs.is_readonly,
      fs.created_at, fs.updated_at, fs.created_by, fs.description
    ORDER BY fs.namespace, fs.feature_set, fs.matrix_id
    """
    return _df(bundle.con, q, params)


def feature_set_get(bundle: BundleHandle, feature_set: str, matrix_id: str) -> list[str]:
    q = """
    SELECT feature_id
    FROM feature_set_membership
    WHERE feature_set = ? AND matrix_id = ?
    ORDER BY rank ASC
    """
    return _df(bundle.con, q, [feature_set, matrix_id])["feature_id"].astype(str).tolist()


def spaces(bundle: BundleHandle) -> pd.DataFrame:
    q = "SELECT space_id, source, dims, storage, description FROM spaces ORDER BY space_id"
    df = _df(bundle.con, q)
    if "space_id" in df.columns:
        df = df[df["space_id"].notna() & (df["space_id"].astype(str) != "")].copy()
    return df


def space_info(bundle: BundleHandle, space_id: str | None = None) -> pd.DataFrame:
    if space_id is None:
        return _df(bundle.con, "SELECT * FROM spaces")
    return _df(bundle.con, "SELECT * FROM spaces WHERE space_id = ?", [space_id])


def space(
    bundle: BundleHandle,
    space_id: str,
    cells_order: list[str] | None = None,
    dims: list[int] | None = None,
):
    info = space_info(bundle, space_id)
    if info.empty:
        raise GetterError(f"Unknown space_id: {space_id}")

    storage = str(info.iloc[0].get("storage") or "")
    dims_total = info.iloc[0].get("dims")
    dims_total = int(dims_total) if pd.notna(dims_total) else None

    con = bundle.con
    if storage == "hdf5:dense-space:v1":
        import h5py

        row = con.execute("SELECT * FROM space_payloads WHERE space_id = ?", (space_id,)).fetchone()
        if row is None:
            raise GetterError(f"No payload found for space_id: {space_id}")
        payload = resolve_bundle_path(
            bundle, str(row["path"]), expected_type="file", purpose="dense-space payload"
        )
        with h5py.File(payload, "r") as handle:
            all_cells = [str(value) for value in handle["cell_ids"].asstr(encoding="utf-8")[:]]
            all_labels = [str(value) for value in handle["dim_labels"].asstr(encoding="utf-8")[:]]
            selected_dims = (
                list(range(1, len(all_labels) + 1)) if dims is None else [int(d) for d in dims]
            )
            if any(d < 1 or d > len(all_labels) for d in selected_dims):
                raise GetterError("Requested dense-space dimensions are invalid")
            requested = all_cells if cells_order is None else [str(value) for value in cells_order]
            positions = {value: index for index, value in enumerate(all_cells)}
            missing = [value for value in requested if value not in positions]
            if missing:
                raise GetterError(f"Some requested cells not found in space payload: {missing[:5]}")
            row_index = [positions[value] for value in requested]
            values = np.asarray(handle["coordinates"][[d - 1 for d in selected_dims], :]).T
            values = values[row_index, :]
        if len(all_labels) == 2 and dims is None:
            return pd.DataFrame({"cell_id": requested, "x": values[:, 0], "y": values[:, 1]})
        return values, requested, selected_dims

    if storage.startswith("wide:space_2d") or (dims_total == 2):
        df = _df(
            con,
            """
            SELECT s.cell_id, s.x, s.y
            FROM space_2d s
            INNER JOIN cells c ON c.cell_id = s.cell_id
            WHERE s.space_id = ?
            ORDER BY c.rowid
            """,
            [space_id],
        )
        df["cell_id"] = df["cell_id"].astype(str)
        if cells_order is not None:
            pos = {c: i for i, c in enumerate(cells_order)}
            have = set(df["cell_id"])
            if not set(cells_order).issubset(have):
                missing = [c for c in cells_order if c not in have]
                raise GetterError(f"Some requested cells not found in space_2d: {missing[:5]}")
            df = df[df["cell_id"].isin(cells_order)].copy()
            df = df.assign(_ord=df["cell_id"].map(pos)).sort_values("_ord").drop(columns=["_ord"])
        return df.reset_index(drop=True)

    if dims is None:
        q = "SELECT MAX(dim) AS dmax FROM space_coords WHERE space_id = ?"
        dmax = _df(con, q, [space_id]).iloc[0]["dmax"]
        if pd.isna(dmax):
            raise GetterError(f"No coords found for space_id: {space_id}")
        dims = list(range(1, int(dmax) + 1))

    dims = [int(d) for d in dims]
    placeholders = ",".join(["?"] * len(dims))
    q = f"SELECT cell_id, dim, value FROM space_coords WHERE space_id = ? AND dim IN ({placeholders})"
    df = _df(con, q, [space_id, *dims])
    if df.empty:
        raise GetterError(f"No coords found for space_id: {space_id}")

    if cells_order is None:
        present = set(df["cell_id"].astype(str))
        cells_order = [c for c in cells(bundle, cell_set="all") if c in present]
    keep = set(cells_order)
    df = df[df["cell_id"].astype(str).isin(keep)].copy()

    mat = np.full((len(cells_order), len(dims)), np.nan, dtype=float)
    cell_pos = {c: i for i, c in enumerate(cells_order)}
    dim_pos = {d: i for i, d in enumerate(dims)}
    for row in df.itertuples(index=False):
        ci = cell_pos.get(str(row.cell_id))
        di = dim_pos.get(int(row.dim))
        if ci is not None and di is not None:
            mat[ci, di] = float(row.value)
    return mat, cells_order, dims


def art_list(bundle: BundleHandle, type: str | None = None) -> pd.DataFrame:
    if type is None:
        return _df(bundle.con, "SELECT * FROM artifacts ORDER BY created_at DESC")
    return _df(
        bundle.con, "SELECT * FROM artifacts WHERE type = ? ORDER BY created_at DESC", [type]
    )


def art_get_row(bundle: BundleHandle, artifact_id: str) -> dict:
    df = _df(
        bundle.con,
        "SELECT * FROM artifacts WHERE artifact_id = ?",
        [artifact_id],
    )
    if df.empty:
        raise GetterError(f"Artifact not found: {artifact_id}")
    row = df.iloc[0].to_dict()
    return row
