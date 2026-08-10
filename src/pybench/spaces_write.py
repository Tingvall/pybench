from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import h5py
import numpy as np
import pandas as pd

from .bundle import BundleHandle, resolve_bundle_path
from .capability import authorize_payload, authorize_sqlite, record_write

SPACE_STORAGE = "hdf5:dense-space:v1"
SPACE_SCHEMA = "scworkbench.dense-space.hdf5.v1"
CONTENT_IDENTITY_SCHEME = "scworkbench.content.dense-space-hdf5.sha256.v1"
SPACE_PAYLOAD_COLUMNS = (
    "space_id",
    "path",
    "schema_version",
    "n_cells",
    "n_dims",
    "cell_ids_sha256",
    "dim_labels_json",
    "dtype",
    "endianness",
    "chunk_rows",
    "chunk_dims",
    "content_sha256",
    "content_identity_scheme",
    "provenance_identity",
    "created_at",
)


class SpaceWriteError(RuntimeError):
    pass


def _normalize_coords(
    coords, cell_id: list[str] | None = None, dim_labels: list[str] | None = None
) -> tuple[list[str], list[str], np.ndarray]:
    if isinstance(coords, pd.DataFrame):
        if "cell_id" in coords.columns:
            raw_ids = coords["cell_id"].tolist()
            frame = coords.drop(columns=["cell_id"])
        else:
            raw_ids = coords.index.tolist()
            frame = coords
        raw_labels = frame.columns.tolist() if dim_labels is None else dim_labels
        values = frame.to_numpy(copy=True)
    else:
        values = np.asarray(coords)
        if cell_id is None:
            raise SpaceWriteError("cell_id= is required for array-like coords")
        raw_ids = cell_id
        raw_labels = dim_labels

    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise SpaceWriteError("coords must be a non-empty 2D array-like")
    if values.dtype.kind not in "iuf":
        raise SpaceWriteError("coords dtype must be a real numeric type")
    if values.dtype.kind == "f" and values.dtype.itemsize > 8:
        raise SpaceWriteError("coords dtype cannot be represented as IEEE-754 float64")
    matrix = np.asarray(values, dtype=np.float64, order="C")
    if not np.isfinite(matrix).all():
        raise SpaceWriteError("coords contains NA/NaN/Inf values")

    ids = [str(value) for value in raw_ids]
    if len(ids) != matrix.shape[0]:
        raise SpaceWriteError("cell_id count does not match number of coordinate rows")
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise SpaceWriteError("cell_id values must be non-empty and unique")

    labels = (
        [f"dim{index}" for index in range(1, matrix.shape[1] + 1)]
        if raw_labels is None
        else [str(value) for value in raw_labels]
    )
    if len(labels) != matrix.shape[1]:
        raise SpaceWriteError("dim_labels count does not match number of coordinate columns")
    if any(not value for value in labels) or len(set(labels)) != len(labels):
        raise SpaceWriteError("dim_labels must be non-empty and unique")
    return ids, labels, matrix


def _assert_cells_exist(bundle: BundleHandle, cell_ids: list[str]) -> None:
    present: set[str] = set()
    for start in range(0, len(cell_ids), 500):
        block = cell_ids[start : start + 500]
        placeholders = ",".join("?" for _ in block)
        present.update(
            str(row[0])
            for row in bundle.con.execute(
                f"SELECT cell_id FROM cells WHERE cell_id IN ({placeholders})", block
            )
        )
    missing = [value for value in cell_ids if value not in present]
    if missing:
        raise SpaceWriteError(f"Some cell_ids are not present in bundle. First few: {missing[:10]}")


def _require_table(bundle: BundleHandle, table: str, columns: Iterable[str]) -> None:
    actual = tuple(str(row[1]) for row in bundle.con.execute(f'PRAGMA table_info("{table}")'))
    expected = tuple(columns)
    if actual != expected:
        raise SpaceWriteError(
            f"bundle schema mismatch for {table}: expected {expected!r}, observed {actual!r}"
        )


def _r_character_vector_sha256(values: list[str]) -> str:
    # Exact jsonlite::serializeJSON(character_vector, digits=NA) representation.
    canonical = json.dumps(
        {"type": "character", "attributes": {}, "value": values},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_strings(dataset: h5py.Dataset) -> list[str]:
    return [str(value) for value in dataset.asstr(encoding="utf-8")[:].tolist()]


def _validate_payload(
    path: Path, *, space_id: str, cell_ids: list[str], dim_labels: list[str]
) -> dict[str, object]:
    try:
        with h5py.File(path, "r") as handle:
            if set(handle.keys()) != {"coordinates", "cell_ids", "dim_labels"}:
                raise SpaceWriteError("dense-space payload has malformed dataset inventory")
            required_attrs = {
                "schema_version",
                "space_id",
                "n_cells",
                "n_dims",
                "dtype",
                "endianness",
                "chunk_rows",
                "chunk_dims",
            }
            if set(handle.attrs.keys()) != required_attrs:
                raise SpaceWriteError("dense-space payload has malformed root attributes")
            coords = handle["coordinates"]
            n_cells, n_dims = len(cell_ids), len(dim_labels)
            expected_chunks = (min(n_cells, 1024), min(n_dims, 16))
            if handle.attrs["schema_version"] != SPACE_SCHEMA:
                raise SpaceWriteError("unsupported dense-space schema version")
            if handle.attrs["space_id"] != space_id:
                raise SpaceWriteError("dense-space payload space_id mismatch")
            # rhdf5 presents dimensions in R order, opposite HDF5's C order.
            if tuple(coords.shape) != (n_dims, n_cells):
                raise SpaceWriteError("dense-space payload shape mismatch")
            if tuple(coords.chunks or ()) != tuple(reversed(expected_chunks)):
                raise SpaceWriteError("dense-space payload chunk shape mismatch")
            if coords.dtype.kind != "f" or coords.dtype.itemsize != 8:
                raise SpaceWriteError("dense-space payload dtype is not float64")
            if int(handle.attrs["n_cells"]) != n_cells or int(handle.attrs["n_dims"]) != n_dims:
                raise SpaceWriteError("dense-space payload declared shape mismatch")
            if handle.attrs["dtype"] != "float64" or handle.attrs["endianness"] not in {
                "little",
                "big",
            }:
                raise SpaceWriteError("dense-space payload numeric contract mismatch")
            if (
                int(handle.attrs["chunk_rows"]),
                int(handle.attrs["chunk_dims"]),
            ) != expected_chunks:
                raise SpaceWriteError("dense-space payload chunk attributes mismatch")
            if _decode_strings(handle["cell_ids"]) != cell_ids:
                raise SpaceWriteError("dense-space payload cell order mismatch")
            if _decode_strings(handle["dim_labels"]) != dim_labels:
                raise SpaceWriteError("dense-space payload dimension labels mismatch")
            for start in range(0, n_cells, expected_chunks[0]):
                if not np.isfinite(coords[:, start : start + expected_chunks[0]]).all():
                    raise SpaceWriteError("dense-space payload contains non-finite coordinates")
    except OSError as exc:
        raise SpaceWriteError(f"cannot reopen dense-space payload: {exc}") from exc
    return {
        "n_cells": len(cell_ids),
        "n_dims": len(dim_labels),
        "chunk_rows": min(len(cell_ids), 1024),
        "chunk_dims": min(len(dim_labels), 16),
        "endianness": sys.byteorder,
        "content_sha256": _file_sha256(path),
    }


def _write_payload(
    path: Path, *, space_id: str, cell_ids: list[str], dim_labels: list[str], matrix: np.ndarray
) -> None:
    chunks = (min(matrix.shape[0], 1024), min(matrix.shape[1], 16))
    string_type = h5py.string_dtype(encoding="utf-8")
    dtype = np.dtype("<f8" if sys.byteorder == "little" else ">f8")
    with h5py.File(path, "x") as handle:
        handle.create_dataset(
            "coordinates",
            data=matrix.astype(dtype, copy=False).T,
            chunks=tuple(reversed(chunks)),
        )
        handle.create_dataset(
            "cell_ids", data=np.asarray(cell_ids, dtype=object), dtype=string_type
        )
        handle.create_dataset(
            "dim_labels", data=np.asarray(dim_labels, dtype=object), dtype=string_type
        )
        handle.attrs["schema_version"] = SPACE_SCHEMA
        handle.attrs["space_id"] = space_id
        handle.attrs["n_cells"] = matrix.shape[0]
        handle.attrs["n_dims"] = matrix.shape[1]
        handle.attrs["dtype"] = "float64"
        handle.attrs["endianness"] = sys.byteorder
        handle.attrs["chunk_rows"] = chunks[0]
        handle.attrs["chunk_dims"] = chunks[1]
        handle.flush()


def add_space(
    bundle: BundleHandle,
    space_id: str,
    coords,
    *,
    cell_id: list[str] | None = None,
    dim_labels: list[str] | None = None,
    description: str | None = None,
    source: str | None = None,
    overwrite: bool = False,
    provenance_identity: str | None = None,
    chunk: int | None = None,
) -> dict:
    del chunk  # retained for source compatibility; HDF5 chunking is contract-defined.
    sid = str(space_id)
    if not sid:
        raise SpaceWriteError("space_id must be a non-empty string")
    cell_ids, labels, matrix = _normalize_coords(coords, cell_id, dim_labels)

    spaces_allowance = authorize_sqlite(bundle, "spaces", "space_id", sid)
    payload_rows_allowance = authorize_sqlite(bundle, "space_payloads", "space_id", sid)
    payload_allowance = authorize_payload(bundle, "space_payloads", sid)
    _require_table(bundle, "spaces", ("space_id", "source", "dims", "storage", "description"))
    _require_table(bundle, "space_payloads", SPACE_PAYLOAD_COLUMNS)
    _assert_cells_exist(bundle, cell_ids)

    existing_space = bundle.con.execute("SELECT * FROM spaces WHERE space_id=?", (sid,)).fetchone()
    existing_payload = bundle.con.execute(
        "SELECT * FROM space_payloads WHERE space_id=?", (sid,)
    ).fetchone()
    if existing_space is not None and not overwrite:
        raise SpaceWriteError(f"space_id already exists: {sid} (set overwrite=True to replace)")

    root = resolve_bundle_path(
        bundle, "space_payloads", must_exist=False, purpose="dense-space payload root"
    )
    if os.path.lexists(root) and not root.is_dir():
        raise SpaceWriteError("dense-space payload root is not a directory")
    root.mkdir(exist_ok=True)
    token = secrets.token_hex(12)
    pending = root / f".pending-space_{token}.h5"
    final = root / f"space_{token}.h5"
    relative = final.relative_to(bundle.bundle_dir).as_posix()
    if os.path.lexists(pending) or os.path.lexists(final):
        raise SpaceWriteError("refusing to replace an existing dense-space payload destination")

    published = False
    committed = False
    try:
        _write_payload(pending, space_id=sid, cell_ids=cell_ids, dim_labels=labels, matrix=matrix)
        inspected = _validate_payload(pending, space_id=sid, cell_ids=cell_ids, dim_labels=labels)
        os.replace(pending, final)
        published = True
        row = (
            sid,
            relative,
            SPACE_SCHEMA,
            inspected["n_cells"],
            inspected["n_dims"],
            _r_character_vector_sha256(cell_ids),
            json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
            "float64",
            inspected["endianness"],
            inspected["chunk_rows"],
            inspected["chunk_dims"],
            inspected["content_sha256"],
            CONTENT_IDENTITY_SCHEME,
            str(provenance_identity if provenance_identity is not None else (source or "analysis")),
            datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        bundle.con.execute("BEGIN")
        if existing_space is None:
            bundle.con.execute(
                "INSERT INTO spaces(space_id,source,dims,storage,description) VALUES(?,?,?,?,?)",
                (sid, source or "analysis", matrix.shape[1], SPACE_STORAGE, description or sid),
            )
        else:
            bundle.con.execute(
                "UPDATE spaces SET source=?,dims=?,storage=?,description=? WHERE space_id=?",
                (source or "analysis", matrix.shape[1], SPACE_STORAGE, description or sid, sid),
            )
            bundle.con.execute("DELETE FROM space_payloads WHERE space_id=?", (sid,))
        bundle.con.execute(
            f"INSERT INTO space_payloads({','.join(SPACE_PAYLOAD_COLUMNS)}) "
            f"VALUES({','.join('?' for _ in SPACE_PAYLOAD_COLUMNS)})",
            row,
        )
        bundle.con.commit()
        committed = True
    except Exception:
        if bundle.con.in_transaction:
            bundle.con.rollback()
        pending.unlink(missing_ok=True)
        if published and not committed:
            final.unlink(missing_ok=True)
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
        bundle.write_journal = []
        raise

    old_space_values = tuple(existing_space) if existing_space is not None else None
    old_payload_values = tuple(existing_payload) if existing_payload is not None else None

    def rollback_publication() -> None:
        if bundle.con.in_transaction:
            bundle.con.rollback()
        bundle.con.execute("BEGIN")
        bundle.con.execute("DELETE FROM space_payloads WHERE space_id=?", (sid,))
        bundle.con.execute("DELETE FROM spaces WHERE space_id=?", (sid,))
        if old_space_values is not None:
            bundle.con.execute(
                "INSERT INTO spaces(space_id,source,dims,storage,description) VALUES(?,?,?,?,?)",
                old_space_values,
            )
        if old_payload_values is not None:
            bundle.con.execute(
                f"INSERT INTO space_payloads({','.join(SPACE_PAYLOAD_COLUMNS)}) "
                f"VALUES({','.join('?' for _ in SPACE_PAYLOAD_COLUMNS)})",
                old_payload_values,
            )
        bundle.con.commit()
        final.unlink(missing_ok=True)
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
        bundle.write_journal = []

    bundle.rollback_actions = (bundle.rollback_actions or []) + [rollback_publication]

    if existing_payload is not None:
        old_relative = str(existing_payload["path"])
        if old_relative != relative:
            old_path = resolve_bundle_path(
                bundle,
                old_relative,
                must_exist=False,
                purpose="superseded dense-space payload",
            )
            bundle.finalize_actions = (bundle.finalize_actions or []) + [
                lambda: old_path.unlink(missing_ok=True)
            ]

    record_write(bundle, spaces_allowance)
    record_write(bundle, payload_rows_allowance)
    record_write(bundle, payload_allowance)
    return {
        "space_id": sid,
        "dims": matrix.shape[1],
        "n_cells": matrix.shape[0],
        "storage": SPACE_STORAGE,
        "path": relative,
        "content_sha256": inspected["content_sha256"],
        "cell_ids_sha256": _r_character_vector_sha256(cell_ids),
    }
