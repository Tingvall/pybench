from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from .bundle import BundleHandle


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
                    raise PayloadWriteError(f"cell_vector: non-numeric value at index {i}: {raw!r}") from e
            rows.append((artifact_id, str(cell_id[i]), label, value_type, value_num))
        con.executemany(
            """
            INSERT INTO artifact_labels(artifact_id, cell_id, label, value_type, value_num)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    return {"artifact_id": artifact_id, "type": "cell_vector", "rows_written": n, "value_type": value_type}


def write_artifact_cell_vector_family_payload(
    bundle: BundleHandle,
    artifact_id: str,
    lineage_id: Sequence[str],
    cell_id: Sequence[str],
    pseudotime: Sequence[float],
) -> dict[str, Any]:
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
    return {"artifact_id": artifact_id, "type": "relation_family", "rows_written": n}


def write_artifact_path_2d_family_payload(
    bundle: BundleHandle,
    artifact_id: str,
    lineage_id: Sequence[str],
    point: Sequence[int],
    x: Sequence[float],
    y: Sequence[float],
) -> dict[str, Any]:
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
    _assert_artifact_type(bundle, artifact_id, "model_ref")
    con = bundle.con
    with con:
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
                str(model_path),
            ),
        )
    return {"artifact_id": artifact_id, "type": "model_ref", "engine": str(engine), "model_space_id": str(model_space_id)}


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
                raise PayloadWriteError(f"table payload rows must be dicts, got {type(row)!r} at row {i}")
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
    return {"artifact_id": artifact_id, "type": artifact_type, "rows_written": len(table_rows), "table_type": str(table_type), "entity_kind": str(entity_kind)}
