from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts_payload import PayloadWriteError, publish_artifact_model_payload
from .bundle import BundleHandle


class LegacyModelImportError(RuntimeError):
    pass


def _source_checksum(source: Path) -> str:
    digest = hashlib.sha256()
    if source.is_symlink():
        raise LegacyModelImportError("legacy model import rejects symlink sources")
    if source.is_file():
        digest.update(b"file\0")
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not source.is_dir():
        raise LegacyModelImportError(f"legacy model source is not a file or directory: {source}")

    digest.update(b"directory\0")
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        filenames.sort()
        root_path = Path(root)
        for name in [*dirnames, *filenames]:
            entry = root_path / name
            if entry.is_symlink():
                raise LegacyModelImportError(
                    f"legacy model import rejects symlink tree entry: {entry}"
                )
        for name in filenames:
            entry = root_path / name
            relative = entry.relative_to(source).as_posix().encode()
            digest.update(b"entry\0" + relative + b"\0")
            with entry.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _model_metadata(bundle: BundleHandle, artifact_id: str) -> dict[str, str | None]:
    artifact = bundle.con.execute(
        "SELECT type, provenance_json FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if artifact is None:
        raise LegacyModelImportError(f"Artifact not found: {artifact_id}")
    if str(artifact[0]) != "model_ref":
        raise LegacyModelImportError(f"Artifact {artifact_id} is not type=model_ref")
    row = bundle.con.execute(
        """
        SELECT engine, model_space_id, in_space_id, model_type
        FROM artifact_model_ref WHERE artifact_id = ?
        """,
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise LegacyModelImportError(
            "legacy import requires an existing model_ref row with engine and space metadata"
        )
    return {
        "engine": str(row[0]),
        "model_space_id": str(row[1]),
        "in_space_id": None if row[2] is None else str(row[2]),
        "model_type": None if row[3] is None else str(row[3]),
        "provenance_json": None if artifact[1] is None else str(artifact[1]),
    }


def import_legacy_model(
    bundle: BundleHandle,
    artifact_id: str,
    source: str | os.PathLike[str],
) -> dict[str, Any]:
    """Explicitly copy an operator-selected legacy model into portable storage."""

    selected_source = Path(source).expanduser()
    if selected_source.is_symlink():
        raise LegacyModelImportError("legacy model import rejects symlink sources")
    source_path = selected_source.resolve(strict=True)
    metadata = _model_metadata(bundle, artifact_id)
    checksum = _source_checksum(source_path)
    suffix = "" if source_path.is_dir() else source_path.suffix
    destination = f"artifact_models/imported/{artifact_id}/{checksum[:16]}{suffix}"

    try:
        provenance = json.loads(metadata["provenance_json"] or "{}")
    except json.JSONDecodeError:
        provenance = {"legacy_provenance_raw": metadata["provenance_json"]}
    if not isinstance(provenance, dict):
        provenance = {"legacy_provenance_value": provenance}
    record = {
        "schema": "pybench_legacy_model_import_v1",
        "external_source": str(source_path),
        "sha256": checksum,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "contained_model_path": destination,
    }
    imports = provenance.get("external_model_imports")
    if not isinstance(imports, list):
        imports = []
    provenance["external_model_imports"] = [*imports, record]

    def copy_source(staging: Path) -> None:
        if source_path.is_dir():
            shutil.copytree(source_path, staging)
        else:
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, staging)
        if _source_checksum(staging) != checksum:
            raise LegacyModelImportError("legacy model source changed while it was being imported")

    def record_provenance(con, relative: str) -> None:
        record["contained_model_path"] = relative
        encoded = json.dumps(provenance, ensure_ascii=True, allow_nan=False)
        cur = con.execute(
            "UPDATE artifacts SET provenance_json = ? WHERE artifact_id = ?",
            (encoded, artifact_id),
        )
        if cur.rowcount != 1:
            raise LegacyModelImportError("failed to record legacy model import provenance")

    try:
        receipt = publish_artifact_model_payload(
            bundle,
            artifact_id=artifact_id,
            engine=str(metadata["engine"]),
            model_space_id=str(metadata["model_space_id"]),
            in_space_id=metadata["in_space_id"],
            model_type=metadata["model_type"],
            model_path=destination,
            expected_type="directory" if source_path.is_dir() else "file",
            writer=copy_source,
            transaction_hook=record_provenance,
        )
    except PayloadWriteError as exc:
        raise LegacyModelImportError(str(exc)) from exc
    return {**receipt, "source_sha256": checksum, "provenance": record}
