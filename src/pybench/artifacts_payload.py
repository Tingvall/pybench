from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .bundle import (
    BundleHandle,
    bundle_relative_path,
    normalize_bundle_relative_path,
    resolve_bundle_path,
)
from .capability import authorize_payload, authorize_sqlite, record_write


class PayloadWriteError(RuntimeError):
    pass


def _one(con, sql: str, params: Sequence[Any]) -> tuple[Any, ...] | None:
    cur = con.execute(sql, tuple(params))
    row = cur.fetchone()
    return tuple(row) if row is not None else None


def _assert_artifact_type(bundle: BundleHandle, artifact_id: str, expected_type: str) -> None:
    row = _one(bundle.con, "SELECT type FROM artifacts WHERE artifact_id = ?", [artifact_id])
    if row is None:
        raise PayloadWriteError(f"Artifact not found: {artifact_id}")
    got = str(row[0])
    if got != expected_type:
        raise PayloadWriteError(f"Artifact {artifact_id} has type={got}, expected={expected_type}")


def write_artifact_cell_vector_payload(
    bundle: BundleHandle,
    artifact_id: str,
    cell_id: Sequence[str],
    value: Sequence[Any],
    value_type: str = "categorical",
) -> dict[str, Any]:
    allowance = authorize_sqlite(bundle, "artifact_labels", "artifact_id", artifact_id)
    n = len(cell_id)
    if len(value) != n:
        raise PayloadWriteError("cell_vector: cell_id/value length mismatch")

    value_type = str(value_type or "categorical")
    if value_type not in {"categorical", "continuous"}:
        raise PayloadWriteError(f"cell_vector: unsupported value_type={value_type}")

    _assert_artifact_type(bundle, artifact_id, "cell_vector")
    con = bundle.con
    with con:
        con.execute("DELETE FROM artifact_labels WHERE artifact_id = ?", (artifact_id,))
        rows = []
        for i in range(n):
            raw = value[i]
            label = "" if raw is None else str(raw)
            value_num = None
            if value_type == "continuous":
                try:
                    value_num = float(raw) if raw is not None else None
                except Exception as e:  # noqa: BLE001
                    raise PayloadWriteError(
                        f"cell_vector: non-numeric value at index {i}: {raw!r}"
                    ) from e
            rows.append((artifact_id, str(cell_id[i]), label, value_type, value_num))
        con.executemany(
            """
            INSERT INTO artifact_labels(artifact_id, cell_id, label, value_type, value_num)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    record_write(bundle, allowance)
    return {
        "artifact_id": artifact_id,
        "type": "cell_vector",
        "rows_written": n,
        "value_type": value_type,
    }


def write_artifact_cell_vector_family_payload(
    bundle: BundleHandle,
    artifact_id: str,
    lineage_id: Sequence[str],
    cell_id: Sequence[str],
    pseudotime: Sequence[float],
) -> dict[str, Any]:
    allowance = authorize_sqlite(bundle, "artifact_pseudotime", "artifact_id", artifact_id)
    n = len(lineage_id)
    if len(cell_id) != n or len(pseudotime) != n:
        raise PayloadWriteError("cell_vector_family: length mismatch")

    _assert_artifact_type(bundle, artifact_id, "cell_vector_family")
    con = bundle.con
    with con:
        con.execute("DELETE FROM artifact_pseudotime WHERE artifact_id = ?", (artifact_id,))
        rows = [
            (artifact_id, str(cell_id[i]), str(lineage_id[i]), float(pseudotime[i]))
            for i in range(n)
        ]
        con.executemany(
            """
            INSERT INTO artifact_pseudotime(artifact_id, cell_id, lineage_id, pseudotime)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
    record_write(bundle, allowance)
    return {"artifact_id": artifact_id, "type": "cell_vector_family", "rows_written": n}


def write_artifact_relation_family_payload(
    bundle: BundleHandle,
    artifact_id: str,
    relation_key: Sequence[str],
    cell_id: Sequence[str],
    weight: Sequence[float] | None = None,
    value: Sequence[float] | None = None,
    distance: Sequence[float] | None = None,
) -> dict[str, Any]:
    allowance = authorize_sqlite(bundle, "artifact_relation_rows", "artifact_id", artifact_id)
    n = len(relation_key)
    if len(cell_id) != n:
        raise PayloadWriteError("relation_family: relation_key/cell_id length mismatch")
    if weight is not None and len(weight) != n:
        raise PayloadWriteError("relation_family: relation_key/weight length mismatch")
    if value is not None and len(value) != n:
        raise PayloadWriteError("relation_family: relation_key/value length mismatch")
    if distance is not None and len(distance) != n:
        raise PayloadWriteError("relation_family: relation_key/distance length mismatch")

    _assert_artifact_type(bundle, artifact_id, "relation_family")
    con = bundle.con
    with con:
        con.execute("DELETE FROM artifact_relation_rows WHERE artifact_id = ?", (artifact_id,))
        rows = []
        for i in range(n):
            rows.append(
                (
                    artifact_id,
                    str(relation_key[i]),
                    str(cell_id[i]),
                    None if weight is None else float(weight[i]),
                    None if value is None else float(value[i]),
                    None if distance is None else float(distance[i]),
                )
            )
        con.executemany(
            """
            INSERT INTO artifact_relation_rows(
              artifact_id, relation_key, cell_id, weight, value, distance
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    record_write(bundle, allowance)
    return {"artifact_id": artifact_id, "type": "relation_family", "rows_written": n}


def write_artifact_path_2d_family_payload(
    bundle: BundleHandle,
    artifact_id: str,
    lineage_id: Sequence[str],
    point: Sequence[int],
    x: Sequence[float],
    y: Sequence[float],
) -> dict[str, Any]:
    allowance = authorize_sqlite(bundle, "artifact_curves_2d", "artifact_id", artifact_id)
    n = len(lineage_id)
    if len(point) != n or len(x) != n or len(y) != n:
        raise PayloadWriteError("path_2d_family: length mismatch")
    _assert_artifact_type(bundle, artifact_id, "path_2d_family")
    con = bundle.con
    with con:
        con.execute("DELETE FROM artifact_curves_2d WHERE artifact_id = ?", (artifact_id,))
        rows = [
            (artifact_id, str(lineage_id[i]), int(point[i]), float(x[i]), float(y[i]))
            for i in range(n)
        ]
        con.executemany(
            """
            INSERT INTO artifact_curves_2d(artifact_id, lineage_id, point, x, y)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    record_write(bundle, allowance)
    return {"artifact_id": artifact_id, "type": "path_2d_family", "rows_written": n}


def write_artifact_model_ref_payload(
    bundle: BundleHandle,
    artifact_id: str,
    *,
    engine: str,
    model_space_id: str,
    model_path: str,
    in_space_id: str | None = None,
    model_type: str | None = None,
) -> dict[str, Any]:
    sqlite_allowance = authorize_sqlite(bundle, "artifact_model_ref", "artifact_id", artifact_id)
    authorize_payload(bundle, model_path, artifact_id)
    _assert_artifact_type(bundle, artifact_id, "model_ref")
    relative, _ = bundle_relative_path(
        bundle,
        model_path,
        must_exist=True,
        inspect_tree=True,
        purpose="artifact model payload",
    )
    con = bundle.con
    with con:
        _write_artifact_model_ref_row(
            bundle,
            artifact_id=artifact_id,
            engine=engine,
            model_space_id=model_space_id,
            model_path=relative,
            in_space_id=in_space_id,
            model_type=model_type,
        )
    record_write(bundle, sqlite_allowance)
    return {
        "artifact_id": artifact_id,
        "type": "model_ref",
        "engine": str(engine),
        "model_space_id": str(model_space_id),
    }


def _write_artifact_model_ref_row(
    bundle: BundleHandle,
    *,
    artifact_id: str,
    engine: str,
    model_space_id: str,
    model_path: str,
    in_space_id: str | None,
    model_type: str | None,
) -> None:
    con = bundle.con
    con.execute("DELETE FROM artifact_model_ref WHERE artifact_id = ?", (artifact_id,))
    con.execute(
        """
        INSERT INTO artifact_model_ref(artifact_id, engine, model_space_id, in_space_id, model_type, model_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            str(engine),
            str(model_space_id),
            None if in_space_id is None else str(in_space_id),
            str(model_type or engine),
            model_path,
        ),
    )


def _remove_model_destination(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _bundle_entry_inventory(root: Path) -> set[str]:
    result: set[str] = set()
    pending = [root]
    while pending:
        parent = pending.pop()
        for item in parent.iterdir():
            relative = item.relative_to(root).as_posix()
            result.add(relative)
            if item.is_dir() and not item.is_symlink():
                pending.append(item)
    return result


def _remove_new_bundle_entries(root: Path, before: set[str]) -> None:
    current = _bundle_entry_inventory(root)
    for relative in sorted(current - before, key=lambda value: value.count("/"), reverse=True):
        _remove_model_destination(root / relative)


def publish_artifact_model_payload(
    bundle: BundleHandle,
    artifact_id: str,
    *,
    engine: str,
    model_space_id: str,
    model_path: str,
    writer: Callable[[Path], None],
    expected_type: str,
    in_space_id: str | None = None,
    model_type: str | None = None,
    transaction_hook: Callable[[sqlite3.Connection, str], None] | None = None,
) -> dict[str, Any]:
    """Stage a model, atomically place it, then transactionally publish its row."""

    sqlite_allowance = authorize_sqlite(bundle, "artifact_model_ref", "artifact_id", artifact_id)
    payload_allowance = authorize_payload(bundle, model_path, artifact_id)
    _assert_artifact_type(bundle, artifact_id, "model_ref")
    relative = normalize_bundle_relative_path(model_path, purpose="artifact model destination")
    final_path = resolve_bundle_path(
        bundle,
        relative,
        expected_type=expected_type,  # type: ignore[arg-type]
        must_exist=False,
        purpose="artifact model destination",
    )
    if os.path.lexists(final_path):
        raise PayloadWriteError(f"artifact model destination already exists: {relative}")

    parent_relative = str(Path(relative).parent).replace("\\", "/")
    if parent_relative == ".":
        raise PayloadWriteError("artifact model destination requires a bundle-owned parent")
    allowance_parent = Path(str(payload_allowance["path"])).parent.as_posix()
    if allowance_parent == ".":
        allowance_parent_path = bundle.bundle_dir
    else:
        allowance_parent_path = resolve_bundle_path(
            bundle,
            allowance_parent,
            expected_type="directory",
            purpose="artifact model allowance parent",
        )
    parent = resolve_bundle_path(
        bundle,
        parent_relative,
        expected_type="directory",
        must_exist=False,
        purpose="artifact model parent",
    )
    parent_preexisted = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    parent = resolve_bundle_path(
        bundle,
        parent_relative,
        expected_type="directory",
        purpose="artifact model parent",
    )
    if allowance_parent_path != bundle.bundle_dir and not allowance_parent_path.exists():
        raise PayloadWriteError("artifact model allowance parent disappeared")
    staging_relative = f"{parent_relative}/.pybench-stage-{uuid.uuid4().hex}"
    staging = resolve_bundle_path(
        bundle,
        staging_relative,
        must_exist=False,
        purpose="artifact model staging destination",
    )

    published = False
    bundle_entries_before = _bundle_entry_inventory(bundle.bundle_dir)
    try:
        writer(staging)
        resolve_bundle_path(
            bundle,
            staging_relative,
            expected_type=expected_type,  # type: ignore[arg-type]
            inspect_tree=True,
            purpose="staged artifact model payload",
        )
        # Recheck the final ancestor immediately before publication.
        final_path = resolve_bundle_path(
            bundle,
            relative,
            expected_type=expected_type,  # type: ignore[arg-type]
            must_exist=False,
            purpose="artifact model destination",
        )
        if os.path.lexists(final_path):
            raise PayloadWriteError(f"artifact model destination already exists: {relative}")
        os.replace(staging, final_path)
        published = True
        resolve_bundle_path(
            bundle,
            relative,
            expected_type=expected_type,  # type: ignore[arg-type]
            inspect_tree=True,
            purpose="published artifact model payload",
        )
        with bundle.con:
            _write_artifact_model_ref_row(
                bundle,
                artifact_id=artifact_id,
                engine=engine,
                model_space_id=model_space_id,
                model_path=relative,
                in_space_id=in_space_id,
                model_type=model_type,
            )
            if transaction_hook is not None:
                transaction_hook(bundle.con, relative)
    except Exception:
        if published:
            _remove_model_destination(final_path)
        _remove_new_bundle_entries(bundle.bundle_dir, bundle_entries_before)
        raise
    finally:
        _remove_model_destination(staging)
        if not parent_preexisted:
            current = parent
            while current != allowance_parent_path:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent

    record_write(bundle, sqlite_allowance)
    record_write(bundle, payload_allowance)
    return {
        "artifact_id": artifact_id,
        "type": "model_ref",
        "engine": str(engine),
        "model_space_id": str(model_space_id),
        "model_path": relative,
    }


def write_artifact_table_payload(
    bundle: BundleHandle,
    artifact_id: str,
    *,
    artifact_type: str,
    table_rows: Sequence[dict[str, Any]],
    table_type: str = "generic",
    entity_kind: str = "generic",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    table_allowance = authorize_sqlite(bundle, "artifact_tables", "artifact_id", artifact_id)
    rows_allowance = authorize_sqlite(bundle, "artifact_table_rows", "artifact_id", artifact_id)
    if artifact_type not in {"feature_table", "obs_table"}:
        raise PayloadWriteError(f"table payload: unsupported artifact_type={artifact_type}")
    _assert_artifact_type(bundle, artifact_id, artifact_type)

    meta_json = json.dumps(meta or {}, ensure_ascii=True, allow_nan=False)

    con = bundle.con
    with con:
        con.execute("DELETE FROM artifact_table_rows WHERE artifact_id = ?", (artifact_id,))
        con.execute("DELETE FROM artifact_tables WHERE artifact_id = ?", (artifact_id,))
        con.execute(
            """
            INSERT INTO artifact_tables(artifact_id, table_type, entity_kind, meta_json)
            VALUES (?, ?, ?, ?)
            """,
            (artifact_id, str(table_type), str(entity_kind), meta_json),
        )
        rows = []
        for i, row in enumerate(table_rows, start=1):
            if not isinstance(row, dict):
                raise PayloadWriteError(
                    f"table payload rows must be dicts, got {type(row)!r} at row {i}"
                )
            row_json = json.dumps(row, ensure_ascii=True, allow_nan=False)
            rows.append((artifact_id, int(i), row_json))
        if rows:
            con.executemany(
                """
                INSERT INTO artifact_table_rows(artifact_id, row_idx, row_json)
                VALUES (?, ?, ?)
                """,
                rows,
            )
    record_write(bundle, table_allowance)
    record_write(bundle, rows_allowance)
    return {
        "artifact_id": artifact_id,
        "type": artifact_type,
        "rows_written": len(table_rows),
        "table_type": str(table_type),
        "entity_kind": str(entity_kind),
    }
