from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any

from .bundle import BundleError, BundleHandle, normalize_bundle_relative_path, resolve_bundle_path

STAGE_MARKER = ".scworkbench-pybench-stage.json"
STAGE_MARKER_SCHEMA = "scworkbench.pybench.stage.v2"
_R_EMPTY_DIRECTORY_SHA256 = "b804748400571cd5c13d08a42b3e18d3ba79f4c11f1d8e20256b336d3ed1351f"


class CapabilityError(BundleError):
    """Stable failure for an invalid or exceeded execution capability."""


def _fail(code: str, message: str) -> None:
    raise CapabilityError(f"{code}: {message}")


def install_sqlite_authorizer(bundle: BundleHandle) -> None:
    """Deny DDL and undeclared table writes at SQLite's execution boundary."""

    if bundle.authorizer_installed:
        return
    allowed_tables = {
        str(item["table"])
        for item in (bundle.capability or {}).get("write_allowlist", [])
        if item.get("mutation_class") == "sqlite_rows"
    }
    scopes: dict[tuple[str, str], set[str]] = {}
    for item in (bundle.capability or {}).get("write_allowlist", []):
        if item.get("mutation_class") != "sqlite_rows":
            continue
        scope = item["key_scope"]
        key = (str(item["table"]), str(scope["column"]))
        scopes.setdefault(key, set()).update(str(value) for value in scope["values"])
    if bundle.access_mode == "staging_write":
        bundle.con.execute("PRAGMA recursive_triggers = ON")
    for index, ((table, column), values) in enumerate(sorted(scopes.items())):
        quoted_table = '"' + table.replace('"', '""') + '"'
        quoted_column = '"' + column.replace('"', '""') + '"'
        literals = ",".join("'" + value.replace("'", "''") + "'" for value in sorted(values))
        for operation, condition in (
            (
                "insert",
                f"NEW.{quoted_column} IS NULL OR NEW.{quoted_column} NOT IN ({literals})",
            ),
            (
                "delete",
                f"OLD.{quoted_column} IS NULL OR OLD.{quoted_column} NOT IN ({literals})",
            ),
            (
                "update",
                f"OLD.{quoted_column} IS NULL OR NEW.{quoted_column} IS NULL "
                f"OR OLD.{quoted_column} NOT IN ({literals}) "
                f"OR NEW.{quoted_column} NOT IN ({literals})",
            ),
        ):
            bundle.con.execute(
                f"CREATE TEMP TRIGGER pybench_scope_{index}_{operation} "
                f"BEFORE {operation.upper()} ON {quoted_table} WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'PYBENCH_OUT_OF_SCOPE_MUTATION'); END"
            )
    schema_actions = {
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
    }
    write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}

    def authorize(action: int, arg1: str | None, _arg2, _database, _trigger) -> int:
        if action in schema_actions:
            return sqlite3.SQLITE_DENY
        # Staging operations do not need PRAGMAs after setup.  Denying all of
        # them closes database-header, WAL, writable_schema, and checkpoint
        # mutation routes as well as future stateful PRAGMAs by default.
        if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() != "table_info":
            return sqlite3.SQLITE_DENY
        if action in write_actions and str(arg1) not in allowed_tables:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    installer = getattr(bundle.con, "_install_locked_authorizer", None)
    if installer is None:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "staging connection cannot lock its authorizer")
    installer(authorize)
    bundle.authorizer_installed = True
    if bundle.access_mode == "staging_write":
        initialize_write_baseline(bundle)


def validate_stage_marker(bundle_dir: str | Path, capability: dict[str, Any]) -> None:
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    marker_path = root / STAGE_MARKER
    if not marker_path.is_file() or marker_path.is_symlink():
        _fail("PYBENCH_SOURCE_WRITE_ATTEMPT", "writable target is not an engine-created stage")
    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        _fail("PYBENCH_STALE_STAGING_IDENTITY", "stage marker is unreadable")
    expected = {
        "schema": STAGE_MARKER_SCHEMA,
        "protocol_version": "pybench-op-v2",
        "capability_id": capability["capability_id"],
        "session_id": capability["session_id"],
        "run_id": capability["run_id"],
        "source_attestation": capability["source_attestation"],
        "staging_identity": capability["staging_identity"],
    }
    if marker != expected:
        _fail("PYBENCH_STALE_STAGING_IDENTITY", "stage marker does not match capability")


def _matching_allowances(
    bundle: BundleHandle,
    *,
    mutation_class: str,
    output_id: str,
    table: str | None = None,
    path: str | None = None,
) -> list[dict[str, Any]]:
    cap = bundle.capability or {}
    matches: list[dict[str, Any]] = []
    for allowance in cap.get("write_allowlist", []):
        if allowance.get("mutation_class") != mutation_class:
            continue
        if allowance.get("output_id") != str(output_id):
            continue
        if table is not None and allowance.get("table") != table:
            continue
        if path is not None:
            prefix = str(allowance.get("path", "")).rstrip("/")
            rel = str(path).rstrip("/")
            extension_sibling = (
                Path(rel).parent == Path(prefix).parent
                and re.fullmatch(
                    re.escape(Path(prefix).name) + r"\.[A-Za-z0-9][A-Za-z0-9._-]*",
                    Path(rel).name,
                )
                is not None
            )
            if not (rel == prefix or rel.startswith(prefix + "/") or extension_sibling):
                continue
        matches.append(allowance)
    return matches


def record_write(bundle: BundleHandle, allowance: dict[str, Any]) -> None:
    if bundle.write_journal is None:
        bundle.write_journal = []
    identity = json.dumps(allowance, sort_keys=True, separators=(",", ":"))
    if all(
        json.dumps(item, sort_keys=True, separators=(",", ":")) != identity
        for item in bundle.write_journal
    ):
        bundle.write_journal.append(dict(allowance))


def authorize_sqlite(
    bundle: BundleHandle, table: str, key_column: str, output_id: str
) -> dict[str, Any]:
    if bundle.access_mode != "staging_write":
        _fail("PYBENCH_SOURCE_WRITE_ATTEMPT", "SQLite mutation denied for read-only bundle")
    matches = _matching_allowances(
        bundle, mutation_class="sqlite_rows", output_id=output_id, table=table
    )
    matches = [
        item
        for item in matches
        if item.get("key_scope", {}).get("column") == key_column
        and item.get("key_scope", {}).get("values") == [str(output_id)]
    ]
    if len(matches) != 1:
        _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", f"undeclared table/key scope: {table}")
    return matches[0]


def authorize_payload(bundle: BundleHandle, path: str, output_id: str) -> dict[str, Any]:
    if bundle.access_mode != "staging_write":
        _fail("PYBENCH_SOURCE_WRITE_ATTEMPT", "payload mutation denied for read-only bundle")
    relative = normalize_bundle_relative_path(path, purpose="capability payload destination")
    resolve_bundle_path(
        bundle,
        relative,
        must_exist=False,
        purpose="capability payload destination",
    )
    matches = _matching_allowances(
        bundle, mutation_class="payload_tree", output_id=output_id, path=relative
    )
    if len(matches) != 1:
        _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "undeclared payload destination")
    return matches[0]


def _identity_token(value: str) -> bytes:
    payload = value.encode("utf-8")
    return str(len(payload)).encode("ascii") + b":" + payload


def _typed_sqlite_scalar(quoted_column: str) -> str:
    return (
        f"CASE typeof({quoted_column}) "
        "WHEN 'null' THEN 'null:' "
        f"WHEN 'integer' THEN 'integer:' || printf('%lld',{quoted_column}) "
        "WHEN 'real' THEN NULL "
        f"WHEN 'text' THEN 'text:' || hex(CAST({quoted_column} AS BLOB)) "
        f"WHEN 'blob' THEN 'blob:' || hex({quoted_column}) "
        f"ELSE 'unknown:' || hex(CAST({quoted_column} AS BLOB)) END"
    )


def sqlite_rows_content_identity(
    bundle: BundleHandle, table: str, key_column: str, key_values: list[str]
) -> tuple[int, str]:
    """Hash explicitly formatted typed SQLite values with language-neutral framing."""

    placeholders = ",".join("?" for _ in key_values)
    quoted_key = '"' + key_column.replace('"', '""') + '"'
    return _sqlite_content_identity(
        bundle,
        table,
        where_sql=f"{quoted_key} IN ({placeholders})",
        params=key_values,
        required_column=key_column,
    )


def _sqlite_content_identity(
    bundle: BundleHandle,
    table: str,
    *,
    where_sql: str = "",
    params: list[str] | None = None,
    required_column: str | None = None,
) -> tuple[int, str]:
    """Canonical identity for an arbitrary already-authorized table scope."""

    quoted_table = '"' + table.replace('"', '""') + '"'
    info = bundle.con.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    columns = sorted(str(row[1]) for row in info)
    if not columns or (required_column is not None and required_column not in columns):
        _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", f"allowed table/key is absent: {table}")
    quoted_columns = [f'"{name.replace(chr(34), chr(34) * 2)}"' for name in columns]
    typed_columns: list[str] = []
    for column in quoted_columns:
        typed_columns.extend(
            [
                f"typeof({column})",
                _typed_sqlite_scalar(column),
                f"CASE WHEN typeof({column})='real' THEN {column} ELSE NULL END",
            ]
        )
    order_by = ",".join(quoted_columns)
    where = f" WHERE {where_sql}" if where_sql else ""
    query = f"SELECT {','.join(typed_columns)} FROM {quoted_table}{where} ORDER BY {order_by}"
    rows: list[tuple[str, ...]] = []
    for raw_row in bundle.con.execute(query, params or []).fetchall():
        encoded: list[str] = []
        for index in range(len(columns)):
            value_type = str(raw_row[index * 3])
            sql_encoding = raw_row[index * 3 + 1]
            real_value = raw_row[index * 3 + 2]
            if value_type == "real":
                encoded.append("real:" + struct.pack(">d", float(real_value)).hex().upper())
            else:
                encoded.append(str(sql_encoding))
        rows.append(tuple(encoded))
    # The R contract re-sorts the canonical token frame before hashing; raw
    # SQLite ordering is not the identity ordering for mixed storage classes.
    rows.sort()
    canonical = bytearray(b"scworkbench-sqlite-rows-v2\n")
    canonical.extend(f"C{len(columns)}\n".encode("ascii"))
    for name in columns:
        canonical.extend(_identity_token(name))
    canonical.extend(f"R{len(rows)}\n".encode("ascii"))
    for row in rows:
        for value in row:
            canonical.extend(_identity_token(value))
    return len(rows), hashlib.sha256(canonical).hexdigest()


def _rows_descriptor(bundle: BundleHandle, allowance: dict[str, Any]) -> dict[str, Any]:
    table = str(allowance["table"])
    column = str(allowance["key_scope"]["column"])
    values = [str(value) for value in allowance["key_scope"]["values"]]
    row_count, content_sha256 = sqlite_rows_content_identity(bundle, table, column, values)
    return {
        **allowance,
        "row_count": row_count,
        "content_sha256": content_sha256,
    }


def _payload_entries(bundle: BundleHandle, allowance: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = normalize_bundle_relative_path(allowance["path"])
    parent_relative = Path(prefix).parent.as_posix()
    if parent_relative == ".":
        parent = bundle.bundle_dir
    else:
        parent = resolve_bundle_path(
            bundle, parent_relative, must_exist=False, expected_type="directory"
        )
    if not parent.exists():
        return []
    wanted = Path(prefix).name
    roots = [
        item
        for item in sorted(parent.iterdir())
        if item.name == wanted
        or re.fullmatch(re.escape(wanted) + r"\.[A-Za-z0-9][A-Za-z0-9._-]*", item.name)
    ]
    descriptors: list[dict[str, Any]] = []
    for root in roots:
        pending = [root]
        while pending:
            item = pending.pop()
            resolved = item.resolve(strict=True)
            bundle_root = bundle.bundle_dir.resolve(strict=True)
            if resolved != bundle_root and bundle_root not in resolved.parents:
                _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "payload symlink escapes stage")
            rel = item.relative_to(bundle.bundle_dir).as_posix()
            if item.is_symlink():
                _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "payload symlink is forbidden")
            if item.is_dir():
                content_sha = _R_EMPTY_DIRECTORY_SHA256
                pending.extend(sorted(item.iterdir(), reverse=True))
            else:
                if item.stat().st_nlink > 1:
                    _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "payload hard link is forbidden")
                digest = hashlib.sha256()
                with item.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                content_sha = digest.hexdigest()
            descriptors.append(
                {**allowance, "path": rel, "row_count": 0, "content_sha256": content_sha}
            )
    return descriptors


def _allowance_identity(allowance: dict[str, Any]) -> str:
    return json.dumps(allowance, sort_keys=True, separators=(",", ":"))


def _database_snapshot(bundle: BundleHandle) -> dict[str, Any]:
    schema_rows = [
        tuple(row)
        for row in bundle.con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    ]
    schema_sha = hashlib.sha256(
        json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    tables = [str(row[1]) for row in schema_rows if row[0] == "table"]
    full = {table: _sqlite_content_identity(bundle, table) for table in tables}
    outside: dict[str, tuple[int, str]] = {}
    grouped: dict[str, tuple[str, set[str]]] = {}
    for allowance in (bundle.capability or {}).get("write_allowlist", []):
        if allowance["mutation_class"] != "sqlite_rows":
            continue
        table = str(allowance["table"])
        column = str(allowance["key_scope"]["column"])
        if table in grouped and grouped[table][0] != column:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "table allowances use different key columns")
        grouped.setdefault(table, (column, set()))[1].update(
            str(value) for value in allowance["key_scope"]["values"]
        )
    for table, (column, values) in grouped.items():
        quoted = '"' + column.replace('"', '""') + '"'
        placeholders = ",".join("?" for _ in values)
        outside[table] = _sqlite_content_identity(
            bundle,
            table,
            where_sql=f"{quoted} IS NULL OR {quoted} NOT IN ({placeholders})",
            params=sorted(values),
            required_column=column,
        )
    return {"schema": schema_sha, "tables": full, "outside": outside}


def _payload_snapshot(
    bundle: BundleHandle, *, reject_hardlinks: bool
) -> dict[str, tuple[str, str]]:
    db_rel = normalize_bundle_relative_path(
        bundle.manifest["storage"]["sqlite"]["path"], purpose="bundle SQLite database"
    )
    ignored = {
        STAGE_MARKER,
        "manifest.json",
        db_rel,
        db_rel + "-wal",
        db_rel + "-shm",
        db_rel + "-journal",
    }
    snapshot: dict[str, tuple[str, str]] = {}
    pending = [bundle.bundle_dir]
    while pending:
        parent = pending.pop()
        for item in sorted(parent.iterdir(), reverse=True):
            relative = item.relative_to(bundle.bundle_dir).as_posix()
            if relative in ignored or ".scworkbench-writer.lock" in relative.split("/"):
                continue
            if item.is_symlink():
                snapshot[relative] = ("symlink", "")
            elif item.is_dir():
                snapshot[relative] = ("dir", _R_EMPTY_DIRECTORY_SHA256)
                pending.append(item)
            else:
                if reject_hardlinks and item.stat().st_nlink > 1:
                    _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "payload hard link is forbidden")
                digest = hashlib.sha256()
                with item.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                snapshot[relative] = ("file", digest.hexdigest())
    return snapshot


def _payload_allowance_matches(relative: str, allowance: dict[str, Any]) -> bool:
    prefix = str(allowance["path"]).rstrip("/")
    if relative == prefix or relative.startswith(prefix + "/"):
        return True
    return (
        Path(relative).parent == Path(prefix).parent
        and re.fullmatch(
            re.escape(Path(prefix).name) + r"\.[A-Za-z0-9][A-Za-z0-9._-]*",
            Path(relative).name,
        )
        is not None
    )


def initialize_write_baseline(bundle: BundleHandle) -> None:
    """Capture authoritative pre-operation identities for receipt differencing."""

    baseline: dict[str, Any] = {}
    for allowance in (bundle.capability or {}).get("write_allowlist", []):
        key = _allowance_identity(allowance)
        if key in baseline:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "duplicate write allowance")
        if allowance["mutation_class"] == "sqlite_rows":
            descriptor = _rows_descriptor(bundle, allowance)
            baseline[key] = (descriptor["row_count"], descriptor["content_sha256"])
        else:
            baseline[key] = {
                item["path"]: item["content_sha256"] for item in _payload_entries(bundle, allowance)
            }
    bundle.write_baseline = baseline
    baseline["__database__"] = _database_snapshot(bundle)
    baseline["__payload__"] = _payload_snapshot(bundle, reject_hardlinks=False)


def build_write_manifest(bundle: BundleHandle) -> list[dict[str, Any]]:
    if bundle.access_mode == "read_only":
        return []
    if bundle.con.in_transaction:
        _fail("PYBENCH_RECEIPT_MISMATCH", "cannot issue a receipt with an open transaction")
    if not bundle.authorizer_installed or bundle.write_baseline is None:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "staging enforcement was not initialized")
    before_db = bundle.write_baseline["__database__"]
    after_db = _database_snapshot(bundle)
    if after_db["schema"] != before_db["schema"]:
        _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "SQLite schema changed")
    allowed_tables = {
        str(item["table"])
        for item in (bundle.capability or {}).get("write_allowlist", [])
        if item["mutation_class"] == "sqlite_rows"
    }
    for table in set(before_db["tables"]) | set(after_db["tables"]):
        if before_db["tables"].get(table) == after_db["tables"].get(table):
            continue
        if table not in allowed_tables:
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", f"undeclared SQLite table mutation: {table}")
        if before_db["outside"].get(table) != after_db["outside"].get(table):
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", f"sibling rows changed in {table}")
    result: list[dict[str, Any]] = []
    for allowance in (bundle.capability or {}).get("write_allowlist", []):
        baseline = bundle.write_baseline[_allowance_identity(allowance)]
        if allowance["mutation_class"] == "sqlite_rows":
            descriptor = _rows_descriptor(bundle, allowance)
            current = (descriptor["row_count"], descriptor["content_sha256"])
            if current != baseline:
                result.append(descriptor)
    before_payload = bundle.write_baseline["__payload__"]
    after_payload = _payload_snapshot(bundle, reject_hardlinks=True)
    for relative in sorted(set(before_payload) | set(after_payload)):
        if before_payload.get(relative) == after_payload.get(relative):
            continue
        if after_payload.get(relative, (None,))[0] == "symlink":
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "payload symlink mutation is forbidden")
        matches = [
            allowance
            for allowance in (bundle.capability or {}).get("write_allowlist", [])
            if allowance["mutation_class"] == "payload_tree"
            and _payload_allowance_matches(relative, allowance)
        ]
        if len(matches) != 1:
            _fail("PYBENCH_OUT_OF_SCOPE_MUTATION", "undeclared or ambiguous payload mutation")
        current = after_payload.get(relative)
        result.append(
            {
                **matches[0],
                "path": relative,
                "row_count": 0,
                "content_sha256": (
                    current[1]
                    if current is not None
                    else "71557d1c8bac9bbe3cbec8d00bb223a2f372279827064095447e569fbf5a760a"
                ),
            }
        )
    return result
