from __future__ import annotations

import json
import pickle
import sqlite3
from pathlib import Path

import pytest

from pybench.artifacts_payload import (
    publish_artifact_model_payload,
    write_artifact_model_ref_payload,
)
from pybench.bundle import BundlePathError, close_bundle, open_bundle, resolve_bundle_path
from pybench.getters import matrix_dir
from pybench.legacy_import import import_legacy_model


def _init_bundle(root: Path) -> Path:
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db" / "app.sqlite")
    try:
        con.executescript(
            """
            CREATE TABLE artifacts (
              artifact_id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              provenance_json TEXT
            );
            CREATE TABLE artifact_model_ref (
              artifact_id TEXT PRIMARY KEY,
              engine TEXT,
              model_space_id TEXT,
              in_space_id TEXT,
              model_type TEXT,
              model_path TEXT
            );
            CREATE TABLE matrices (
              matrix_id TEXT PRIMARY KEY,
              layer TEXT,
              feature_type TEXT,
              n_features INTEGER,
              n_cells INTEGER,
              storage TEXT,
              path TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO artifacts(artifact_id, type, provenance_json) VALUES ('model_art', 'model_ref', '{}')"
        )
        con.commit()
    finally:
        con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    return root


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "PATH_EMPTY"),
        (".", "PATH_EMPTY"),
        ("../payload", "PATH_ESCAPE"),
        ("safe\\..\\payload", "PATH_ESCAPE"),
        ("/tmp/payload", "PATH_ABSOLUTE"),
        ("C:\\payload\\model", "PATH_ABSOLUTE"),
        ("\\\\server\\share\\model", "PATH_ABSOLUTE"),
    ],
)
def test_canonical_resolver_stable_lexical_diagnostics(tmp_path: Path, value: str, code: str):
    root = tmp_path / "bundle"
    root.mkdir()
    with pytest.raises(BundlePathError) as error:
        resolve_bundle_path(root, value, must_exist=False)
    assert error.value.code == code


def test_canonical_resolver_distinguishes_missing_type_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "payload.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BundlePathError) as missing:
        resolve_bundle_path(root, "missing.bin", expected_type="file")
    assert missing.value.code == "PAYLOAD_MISSING"
    with pytest.raises(BundlePathError) as wrong_type:
        resolve_bundle_path(root, "payload.txt", expected_type="directory")
    assert wrong_type.value.code == "PATH_TYPE_MISMATCH"
    with pytest.raises(BundlePathError) as escape:
        resolve_bundle_path(root, "nested/not-created.bin", must_exist=False)
    assert escape.value.code == "PATH_SYMLINK_ESCAPE"


@pytest.mark.parametrize("db_path", ["../outside.sqlite", "/tmp/outside.sqlite", "C:\\outside.sqlite", "\\\\host\\share\\outside.sqlite"])
def test_open_bundle_rejects_sqlite_traversal_and_absolute_before_connect(
    tmp_path: Path, monkeypatch, db_path: str
):
    root = _init_bundle(tmp_path / "bundle")
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": db_path}}})
    )
    connected = False

    def forbidden_connect(*_args, **_kwargs):
        nonlocal connected
        connected = True
        raise AssertionError("SQLite connect must not be reached")

    monkeypatch.setattr("pybench.bundle.sqlite3.connect", forbidden_connect)
    with pytest.raises(BundlePathError):
        open_bundle(root)
    assert connected is False


def test_open_bundle_rejects_manifest_and_sqlite_symlink_before_payload_read(
    tmp_path: Path, monkeypatch
):
    root = _init_bundle(tmp_path / "bundle")
    outside_manifest = tmp_path / "outside-manifest.json"
    outside_manifest.write_text("not json and must not be read")
    (root / "manifest.json").unlink()
    (root / "manifest.json").symlink_to(outside_manifest)
    with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
        open_bundle(root)

    root = _init_bundle(tmp_path / "bundle-sqlite")
    outside_db = tmp_path / "outside.sqlite"
    outside_db.write_bytes(b"not sqlite and must not be opened")
    (root / "db" / "app.sqlite").unlink()
    (root / "db" / "app.sqlite").symlink_to(outside_db)
    connected = False

    def forbidden_connect(*_args, **_kwargs):
        nonlocal connected
        connected = True
        raise AssertionError("SQLite connect must not be reached")

    monkeypatch.setattr("pybench.bundle.sqlite3.connect", forbidden_connect)
    with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
        open_bundle(root)
    assert connected is False


def test_open_bundle_rejects_sqlite_sidecar_symlink_before_connect(tmp_path: Path, monkeypatch):
    root = _init_bundle(tmp_path / "bundle")
    outside_wal = tmp_path / "outside.sqlite-wal"
    outside_wal.write_bytes(b"must not be read")
    (root / "db" / "app.sqlite-wal").symlink_to(outside_wal)
    connected = False

    def forbidden_connect(*_args, **_kwargs):
        nonlocal connected
        connected = True
        raise AssertionError("SQLite connect must not be reached")

    monkeypatch.setattr("pybench.bundle.sqlite3.connect", forbidden_connect)
    with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
        open_bundle(root)
    assert connected is False


def test_matrix_registry_path_and_nested_tree_links_are_rejected(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    b = open_bundle(root)
    try:
        b.con.execute(
            "INSERT INTO matrices(matrix_id, path) VALUES ('m1', '../outside-matrix')"
        )
        b.con.commit()
        with pytest.raises(BundlePathError, match="PATH_ESCAPE"):
            matrix_dir(b, "m1")

        b.con.execute("UPDATE matrices SET path='mat/m1' WHERE matrix_id='m1'")
        b.con.commit()
        (root / "mat" / "m1").mkdir(parents=True)
        outside = tmp_path / "outside-data"
        outside.write_bytes(b"external")
        (root / "mat" / "m1" / "data.bin").symlink_to(outside)
        with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
            matrix_dir(b, "m1")
    finally:
        close_bundle(b)


def test_model_writer_stores_only_relative_contained_paths(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    (root / "artifact_models").mkdir()
    (root / "artifact_models" / "model.pkl").write_bytes(b"model")
    b = open_bundle(root)
    try:
        result = write_artifact_model_ref_payload(
            b,
            "model_art",
            engine="test",
            model_space_id="space",
            model_path="artifact_models/model.pkl",
        )
        stored = b.con.execute(
            "SELECT model_path FROM artifact_model_ref WHERE artifact_id='model_art'"
        ).fetchone()[0]
        assert stored == "artifact_models/model.pkl"
        assert result["type"] == "model_ref"
        assert not Path(stored).is_absolute()

        with pytest.raises(BundlePathError, match="PATH_ABSOLUTE"):
            write_artifact_model_ref_payload(
                b,
                "model_art",
                engine="test",
                model_space_id="space",
                model_path=str(root / "artifact_models" / "model.pkl"),
            )
    finally:
        close_bundle(b)


def test_publication_failure_removes_registry_and_partial_destination(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    b = open_bundle(root)
    try:
        b.con.execute(
            """
            CREATE TRIGGER reject_model_publish BEFORE INSERT ON artifact_model_ref
            BEGIN SELECT RAISE(ABORT, 'forced publication failure'); END
            """
        )
        b.con.commit()

        def writer(path: Path) -> None:
            path.mkdir()
            (path / "state.bin").write_bytes(b"partial")

        with pytest.raises(sqlite3.IntegrityError, match="forced publication failure"):
            publish_artifact_model_payload(
                b,
                "model_art",
                engine="test",
                model_space_id="space",
                model_path="artifact_models/model_art",
                expected_type="directory",
                writer=writer,
            )
        assert not (root / "artifact_models" / "model_art").exists()
        assert not list((root / "artifact_models").glob(".pybench-stage-*"))
        assert b.con.execute("SELECT COUNT(*) FROM artifact_model_ref").fetchone()[0] == 0
    finally:
        close_bundle(b)


def test_publication_rejects_top_level_and_nested_symlink_escape(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _init_bundle(tmp_path / "bundle")
    (root / "artifact_models").symlink_to(outside, target_is_directory=True)
    b = open_bundle(root)
    try:
        with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
            publish_artifact_model_payload(
                b,
                "model_art",
                engine="test",
                model_space_id="space",
                model_path="artifact_models/model_art",
                expected_type="directory",
                writer=lambda path: path.mkdir(),
            )
    finally:
        close_bundle(b)

    root = _init_bundle(tmp_path / "bundle-nested")
    b = open_bundle(root)
    try:
        outside_file = tmp_path / "outside-state.bin"
        outside_file.write_bytes(b"external")

        def nested_writer(path: Path) -> None:
            path.mkdir(parents=True)
            (path / "state.bin").symlink_to(outside_file)

        with pytest.raises(BundlePathError, match="PATH_SYMLINK_ESCAPE"):
            publish_artifact_model_payload(
                b,
                "model_art",
                engine="test",
                model_space_id="space",
                model_path="artifact_models/model_art",
                expected_type="directory",
                writer=nested_writer,
            )
        assert not (root / "artifact_models" / "model_art").exists()
        assert not list((root / "artifact_models").glob(".pybench-stage-*"))
    finally:
        close_bundle(b)


def test_recipient_path_inspection_never_deserializes_adversarial_model(
    tmp_path: Path, monkeypatch
):
    root = _init_bundle(tmp_path / "bundle")
    model = root / "artifact_models" / "adversarial.pkl"
    model.parent.mkdir()
    model.write_bytes(b"cos\nsystem\n(S'touch /tmp/pybench-should-not-run'\ntR.")

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("recipient inspection must not deserialize model bytes")

    monkeypatch.setattr(pickle, "load", forbidden_load)
    b = open_bundle(root)
    try:
        resolved = resolve_bundle_path(
            b, "artifact_models/adversarial.pkl", purpose="recipient model inspection"
        )
        assert resolved == model
    finally:
        close_bundle(b)


def test_explicit_legacy_import_copies_and_records_external_provenance(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    external = tmp_path / "operator-selected.pkl"
    external.write_bytes(b"legacy-model")
    b = open_bundle(root)
    try:
        b.con.execute(
            """
            INSERT INTO artifact_model_ref(
              artifact_id, engine, model_space_id, in_space_id, model_type, model_path
            ) VALUES ('model_art', 'legacy', 'space', 'space', 'legacy_fit', ?)
            """,
            (str(external),),
        )
        b.con.commit()
        result = import_legacy_model(b, "model_art", external)
        stored = b.con.execute(
            "SELECT model_path FROM artifact_model_ref WHERE artifact_id='model_art'"
        ).fetchone()[0]
        provenance = json.loads(
            b.con.execute(
                "SELECT provenance_json FROM artifacts WHERE artifact_id='model_art'"
            ).fetchone()[0]
        )
        assert stored.startswith("artifact_models/imported/model_art-")
        assert not Path(stored).is_absolute()
        assert (root / stored).read_bytes() == b"legacy-model"
        record = provenance["external_model_imports"][-1]
        assert record["external_source"] == str(external)
        assert record["sha256"] == result["source_sha256"]
        assert record["contained_model_path"] == stored
    finally:
        close_bundle(b)


def test_failed_legacy_import_rolls_back_registry_and_destination(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    external = tmp_path / "operator-selected.pkl"
    external.write_bytes(b"legacy-model")
    b = open_bundle(root)
    try:
        b.con.execute(
            """
            INSERT INTO artifact_model_ref(
              artifact_id, engine, model_space_id, in_space_id, model_type, model_path
            ) VALUES ('model_art', 'legacy', 'space', 'space', 'legacy_fit', ?)
            """,
            (str(external),),
        )
        b.con.execute(
            """
            CREATE TRIGGER reject_import_provenance
            BEFORE UPDATE OF provenance_json ON artifacts
            BEGIN SELECT RAISE(ABORT, 'forced provenance failure'); END
            """
        )
        b.con.commit()
        with pytest.raises(sqlite3.IntegrityError, match="forced provenance failure"):
            import_legacy_model(b, "model_art", external)
        stored = b.con.execute(
            "SELECT model_path FROM artifact_model_ref WHERE artifact_id='model_art'"
        ).fetchone()[0]
        assert stored == str(external)
        imported = root / "artifact_models" / "imported"
        assert not imported.exists() or not list(imported.iterdir())
        assert not list(root.glob("**/.pybench-stage-*"))
    finally:
        close_bundle(b)
