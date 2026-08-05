from __future__ import annotations

import json
import os
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

PATH_CONTRACT_VERSION = "scworkbench-bundle-path-v1"
PathType = Literal["either", "file", "directory"]


@dataclass
class BundleHandle:
    bundle_dir: Path
    manifest: dict[str, Any]
    con: sqlite3.Connection
    access_mode: Literal["read_only", "staging_write"] = "read_only"
    capability: dict[str, Any] | None = None
    write_journal: list[dict[str, Any]] | None = None
    write_baseline: dict[str, Any] | None = None
    authorizer_installed: bool = False


class BundleError(RuntimeError):
    pass


class BundlePathError(BundleError):
    """Stable diagnostic for a rejected portable or contained runtime path."""

    def __init__(self, code: str, path: object, observed: str, purpose: str):
        self.code = code
        self.path_value = path
        self.observed = observed
        self.purpose = purpose
        super().__init__(f"{code}: invalid {purpose} path {path!r} ({observed}).")


def _path_error(code: str, path: object, observed: str, purpose: str) -> BundlePathError:
    return BundlePathError(code, path, observed, purpose)


def normalize_bundle_relative_path(
    path: str | os.PathLike[str], *, purpose: str = "durable portable payload"
) -> str:
    """Validate and normalize one scworkbench-bundle-path-v1 registry value."""

    if not isinstance(path, (str, os.PathLike)):
        raise _path_error("PATH_TYPE_MISMATCH", path, "not a path string", purpose)
    raw = os.fspath(path)
    if not isinstance(raw, str):
        raise _path_error("PATH_TYPE_MISMATCH", path, "not a text path", purpose)
    if not raw:
        raise _path_error("PATH_EMPTY", path, "empty", purpose)

    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise _path_error("PATH_ABSOLUTE", raw, "absolute", purpose)

    parts = normalized.split("/")
    if ".." in parts:
        raise _path_error("PATH_ESCAPE", raw, "parent traversal", purpose)
    parts = [part for part in parts if part not in {"", "."}]
    if not parts:
        raise _path_error("PATH_EMPTY", raw, "empty normalized path", purpose)
    return "/".join(parts)


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _audit_contained_tree(path: Path, root: Path, raw: object, purpose: str) -> None:
    """Inspect links in a payload tree without opening any payload file bytes."""

    pending = [path]
    visited: set[tuple[int, int]] = set()
    while pending:
        current = pending.pop()
        try:
            stat = current.stat()
        except FileNotFoundError:
            raise _path_error("PAYLOAD_MISSING", raw, "missing tree entry", purpose) from None
        inode = (int(stat.st_dev), int(stat.st_ino))
        if inode in visited:
            continue
        visited.add(inode)
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise BundleError(
                f"Unable to inspect contained {purpose} tree {current}: {exc}"
            ) from exc
        for entry in entries:
            entry_path = Path(entry.path)
            if entry.is_symlink():
                try:
                    resolved = entry_path.resolve(strict=False)
                except RuntimeError:
                    raise _path_error(
                        "PATH_SYMLINK_ESCAPE",
                        raw,
                        "payload tree contains an unresolvable symlink",
                        purpose,
                    ) from None
                if not _is_inside(resolved, root):
                    raise _path_error(
                        "PATH_SYMLINK_ESCAPE",
                        raw,
                        "payload tree symlink resolves outside bundle",
                        purpose,
                    )
                if resolved.is_dir():
                    pending.append(resolved)
            elif entry.is_dir(follow_symlinks=False):
                pending.append(entry_path)
            elif entry.stat(follow_symlinks=False).st_nlink > 1:
                raise _path_error(
                    "PATH_HARDLINK_FORBIDDEN",
                    raw,
                    "payload tree contains a multiply-linked file",
                    purpose,
                )


def resolve_bundle_path(
    bundle_or_root: BundleHandle | str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    expected_type: PathType = "either",
    must_exist: bool = True,
    inspect_tree: bool = False,
    purpose: str = "durable portable payload",
) -> Path:
    """Resolve a relative path while preserving scworkbench-bundle-path-v1 semantics.

    Existing ancestors are resolved one at a time, so an escaping symlink is
    rejected even when the requested final leaf has not been created yet.
    ``inspect_tree`` additionally audits nested links without reading file bytes.
    """

    if expected_type not in {"either", "file", "directory"}:
        raise ValueError(f"unsupported expected_type: {expected_type!r}")
    root_value = (
        bundle_or_root.bundle_dir
        if isinstance(bundle_or_root, BundleHandle)
        else Path(bundle_or_root)
    )
    try:
        root = root_value.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise BundleError(f"Bundle directory does not exist: {root_value}") from exc
    if not root.is_dir():
        raise BundleError(f"Bundle root is not a directory: {root}")

    relative = normalize_bundle_relative_path(path, purpose=purpose)
    current = root
    for part in relative.split("/"):
        current = current / part
        if os.path.lexists(current):
            try:
                resolved = current.resolve(strict=True)
            except FileNotFoundError:
                try:
                    resolved = current.resolve(strict=False)
                except RuntimeError:
                    raise _path_error(
                        "PATH_SYMLINK_ESCAPE", path, "unresolvable symlink", purpose
                    ) from None
            except RuntimeError:
                raise _path_error(
                    "PATH_SYMLINK_ESCAPE", path, "unresolvable symlink", purpose
                ) from None
            if not _is_inside(resolved, root):
                raise _path_error(
                    "PATH_SYMLINK_ESCAPE", path, "outside bundle through symlink", purpose
                )
            current = resolved

    present = current.exists()
    if must_exist and not present:
        raise _path_error("PAYLOAD_MISSING", path, "missing", purpose)
    if present:
        resolved = current.resolve(strict=True)
        if not _is_inside(resolved, root):
            raise _path_error("PATH_SYMLINK_ESCAPE", path, "outside bundle", purpose)
        actual = "directory" if resolved.is_dir() else "file"
        if expected_type != "either" and actual != expected_type:
            raise _path_error("PATH_TYPE_MISMATCH", path, actual, purpose)
        if inspect_tree and actual == "directory":
            _audit_contained_tree(resolved, root, path, purpose)
        return resolved

    # All existing ancestors have already been canonicalized and checked.
    if not _is_inside(current, root):
        raise _path_error("PATH_SYMLINK_ESCAPE", path, "outside bundle", purpose)
    return current


def bundle_relative_path(
    bundle_or_root: BundleHandle | str | os.PathLike[str],
    path: str | os.PathLike[str],
    **kwargs: Any,
) -> tuple[str, Path]:
    """Return the normalized portable value and its checked runtime path."""

    relative = normalize_bundle_relative_path(
        path, purpose=kwargs.get("purpose", "durable portable payload")
    )
    return relative, resolve_bundle_path(bundle_or_root, relative, **kwargs)


def resolve_declared_input_path(
    declared_root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    expected_type: PathType = "file",
    purpose: str = "explicit staged input",
) -> Path:
    """Resolve a named runtime input under its explicitly declared staging root."""

    try:
        root = Path(declared_root).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise BundleError(f"Declared input root does not exist: {declared_root}") from exc
    if not root.is_dir():
        raise BundleError(f"Declared input root is not a directory: {root}")

    if not isinstance(path, (str, os.PathLike)):
        raise _path_error("PATH_TYPE_MISMATCH", path, "not a path string", purpose)
    raw = os.fspath(path)
    normalized = raw.replace("\\", "/") if isinstance(raw, str) else raw
    if isinstance(normalized, str) and normalized.startswith("/"):
        lexical = Path(normalized)
        try:
            relative = lexical.relative_to(root).as_posix()
        except ValueError:
            raise _path_error(
                "PATH_ABSOLUTE", path, "outside declared staging root", purpose
            ) from None
    else:
        relative = path
    return resolve_bundle_path(
        root,
        relative,
        expected_type=expected_type,
        purpose=purpose,
    )


_VALIDATED_STAGING_OPEN = object()


class _CapabilityConnection(sqlite3.Connection):
    """Connection whose installed capability guard cannot be casually replaced."""

    _pybench_authorizer_locked = False

    def set_authorizer(self, authorizer_callback):
        if self._pybench_authorizer_locked:
            raise sqlite3.DatabaseError(
                "PYBENCH_OUT_OF_SCOPE_MUTATION: SQLite authorizer is locked"
            )
        return super().set_authorizer(authorizer_callback)

    def _install_locked_authorizer(self, authorizer_callback) -> None:
        super().set_authorizer(authorizer_callback)
        self._pybench_authorizer_locked = True

    def enable_load_extension(self, enabled: bool) -> None:
        if self._pybench_authorizer_locked:
            raise sqlite3.DatabaseError(
                "PYBENCH_OUT_OF_SCOPE_MUTATION: SQLite extensions are disabled"
            )
        super().enable_load_extension(enabled)

    def load_extension(self, path: str, *, entrypoint: str | None = None) -> None:
        if self._pybench_authorizer_locked:
            raise sqlite3.DatabaseError(
                "PYBENCH_OUT_OF_SCOPE_MUTATION: SQLite extensions are disabled"
            )
        if entrypoint is None:
            super().load_extension(path)
        else:  # pragma: no cover - entrypoint is only available on newer Python
            super().load_extension(path, entrypoint=entrypoint)

    def deserialize(self, data: bytes, /, *, name: str = "main") -> None:
        if self._pybench_authorizer_locked:
            raise sqlite3.DatabaseError(
                "PYBENCH_OUT_OF_SCOPE_MUTATION: SQLite deserialize is disabled"
            )
        super().deserialize(data, name=name)


def open_bundle(
    bundle_dir: str | Path,
    *,
    access_mode: Literal["read_only", "staging_write"] = "read_only",
    capability: dict[str, Any] | None = None,
    _staging_token: object | None = None,
) -> BundleHandle:
    try:
        root = Path(bundle_dir).expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise BundleError(f"Bundle directory does not exist: {bundle_dir}") from exc
    if not root.is_dir():
        raise BundleError(f"Bundle directory does not exist: {root}")

    man_path = resolve_bundle_path(
        root,
        "manifest.json",
        expected_type="file",
        purpose="bundle manifest",
    )
    try:
        manifest = json.loads(man_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"Unable to read bundle manifest: {exc}") from exc
    try:
        db_rel = manifest["storage"]["sqlite"]["path"]
    except (KeyError, TypeError) as exc:
        raise BundleError("manifest.json missing storage.sqlite.path") from exc

    db_path = resolve_bundle_path(
        root,
        db_rel,
        expected_type="file",
        purpose="bundle SQLite database",
    )
    # SQLite may automatically open sidecars. Reject an escaping sidecar link
    # before sqlite3 can inspect or read it.
    db_relative = normalize_bundle_relative_path(db_rel, purpose="bundle SQLite database")
    sidecars = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecars.append(
            resolve_bundle_path(
                root,
                db_relative + suffix,
                expected_type="file",
                must_exist=False,
                purpose="bundle SQLite sidecar",
            )
        )
    if access_mode not in {"read_only", "staging_write"}:
        raise BundleError(f"unsupported bundle access mode: {access_mode!r}")
    if access_mode == "read_only" and any(path.exists() for path in sidecars):
        raise BundleError(
            "PYBENCH_SOURCE_WRITE_ATTEMPT: read-only bundle has a live SQLite sidecar"
        )
    if access_mode == "staging_write" and (
        capability is None or _staging_token is not _VALIDATED_STAGING_OPEN
    ):
        raise BundleError(
            "PYBENCH_CAPABILITY_MISMATCH: staging_write requires a validated v2 request"
        )
    if access_mode == "read_only":
        con = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            factory=_CapabilityConnection,
        )
        con.execute("PRAGMA query_only = ON")
    else:
        con = sqlite3.connect(str(db_path), factory=_CapabilityConnection)
    con.row_factory = sqlite3.Row
    return BundleHandle(
        bundle_dir=root,
        manifest=manifest,
        con=con,
        access_mode=access_mode,
        capability=deepcopy(capability),
        write_journal=[],
        write_baseline={},
    )


def close_bundle(bundle: BundleHandle) -> None:
    bundle.con.close()
