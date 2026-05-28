from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class BundleHandle:
    bundle_dir: Path
    manifest: dict[str, Any]
    con: sqlite3.Connection


class BundleError(RuntimeError):
    pass


def open_bundle(bundle_dir: str | Path) -> BundleHandle:
    root = Path(bundle_dir).expanduser().resolve()
    if not root.is_dir():
        raise BundleError(f"Bundle directory does not exist: {root}")

    man_path = root / "manifest.json"
    if not man_path.exists():
        raise BundleError(f"Missing manifest.json in bundle: {root}")

    manifest = json.loads(man_path.read_text())
    try:
        db_rel = manifest["storage"]["sqlite"]["path"]
    except Exception as e:  # noqa: BLE001
        raise BundleError("manifest.json missing storage.sqlite.path") from e

    db_path = (root / db_rel).resolve()
    if not db_path.exists():
        raise BundleError(f"SQLite DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return BundleHandle(bundle_dir=root, manifest=manifest, con=con)


def close_bundle(bundle: BundleHandle) -> None:
    bundle.con.close()
