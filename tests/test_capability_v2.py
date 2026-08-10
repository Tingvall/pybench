from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from capability_helpers import (
    payload_allowance,
    raw_write_bundle,
    sqlite_allowance,
    v2_request,
)
from pybench.artifacts_payload import (
    publish_artifact_model_payload,
    write_artifact_cell_vector_payload,
    write_artifact_path_2d_family_payload,
    write_artifact_relation_family_payload,
    write_artifact_table_payload,
)
from pybench.bundle import BundleError, close_bundle, open_bundle
from pybench.capability import (
    STAGE_MARKER,
    CapabilityError,
    authorize_payload,
    build_write_manifest,
    record_write,
    sqlite_rows_content_identity,
)
from pybench.ops import (
    palantir_fit,
    palantir_project,
    peakvi_fit,
    peakvi_project,
    scvi_train,
)
from pybench.ops.py_protocol_hello import _run as run_hello
from pybench.protocol import (
    PATH_CONTRACT,
    ProtocolError,
    open_bundle_for_request,
    validate_receipt,
    validate_request,
    write_receipt,
)
from pybench.spaces_write import add_space

REPO = Path(__file__).resolve().parents[1]
FORGE_PROTOCOL = Path("/home/dirk/Projects/lymphocyte_trajectory/scworkbench/inst/protocol")


def _init_bundle(root: Path) -> Path:
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db" / "app.sqlite")
    con.executescript(
        """
        CREATE TABLE cells(cell_id TEXT PRIMARY KEY, dataset_id TEXT);
        CREATE TABLE cell_set_membership(cell_set TEXT, cell_id TEXT);
        CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE artifact_labels(
          artifact_id TEXT, cell_id TEXT, label TEXT, value_type TEXT, value_num REAL,
          PRIMARY KEY(artifact_id, cell_id)
        );
        CREATE TABLE artifact_model_ref(
          artifact_id TEXT PRIMARY KEY, engine TEXT, model_space_id TEXT,
          in_space_id TEXT, model_type TEXT, model_path TEXT
        );
        CREATE TABLE artifact_relation_rows(
          artifact_id TEXT, relation_key TEXT, cell_id TEXT,
          weight REAL, value REAL, distance REAL
        );
        CREATE TABLE artifact_curves_2d(
          artifact_id TEXT, lineage_id TEXT, point INTEGER, x REAL, y REAL
        );
        CREATE TABLE artifact_tables(
          artifact_id TEXT, table_type TEXT, entity_kind TEXT, meta_json TEXT
        );
        CREATE TABLE artifact_table_rows(
          artifact_id TEXT, row_idx INTEGER, row_json TEXT
        );
        CREATE TABLE spaces(
          space_id TEXT PRIMARY KEY, source TEXT, dims INTEGER,
          storage TEXT, description TEXT
        );
        CREATE TABLE space_payloads(
          space_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL,
          n_cells INTEGER NOT NULL, n_dims INTEGER NOT NULL, cell_ids_sha256 TEXT NOT NULL,
          dim_labels_json TEXT NOT NULL, dtype TEXT NOT NULL, endianness TEXT NOT NULL,
          chunk_rows INTEGER NOT NULL, chunk_dims INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
          content_identity_scheme TEXT NOT NULL, provenance_identity TEXT, created_at TEXT NOT NULL
        );
        INSERT INTO cells VALUES ('c1','d'),('c2','d');
        INSERT INTO cell_set_membership VALUES ('core','c1'),('core','c2');
        INSERT INTO artifacts VALUES
          ('labels','cell_vector'),('model','model_ref'),('relations','relation_family'),
          ('curves','path_2d_family'),('summary','obs_table');
        """
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    (root / "artifact_models").mkdir()
    return root


def _allowance(
    *, output_name: str = "vector", output_id: str = "labels", table: str = "artifact_labels"
) -> dict:
    return {
        "output_name": output_name,
        "output_id": output_id,
        "output_kind": "cell_vector",
        "mutation_class": "sqlite_rows",
        "table": table,
        "key_scope": {"column": "artifact_id", "values": [output_id]},
    }


def _request(root: Path, *, mode: str = "staging_write") -> dict:
    allowlist = [_allowance()] if mode == "staging_write" else []
    outputs = {"vector": {"artifact_id": "labels", "type": "cell_vector"}} if allowlist else {}
    capability = {
        "capability_id": "cap_test",
        "mode": mode,
        "session_id": "session_test",
        "run_id": "run_test",
        "source_attestation": {
            "scheme": "scworkbench-scientific-state-v1",
            "content_sha256": "1" * 64,
        },
        "staging_identity": {
            "root_sha256": "2" * 64,
            "checkpoint_generation": "g_test",
        },
        "write_allowlist": allowlist,
    }
    request = {
        "message_type": "request",
        "protocol_version": "pybench-op-v2",
        "bundle_dir": str(root),
        "path_contract": dict(PATH_CONTRACT),
        "op_name": "protocol_hello",
        "run": {"session_id": "session_test", "run_id": "run_test"},
        "execution_capability": capability,
        "inputs": {"cell_set": "core"},
        "params": {"n_preview": 2},
        "outputs": outputs,
    }
    if mode == "staging_write":
        marker = {
            "schema": "scworkbench.pybench.stage.v2",
            "protocol_version": "pybench-op-v2",
            "capability_id": capability["capability_id"],
            "session_id": capability["session_id"],
            "run_id": capability["run_id"],
            "source_attestation": capability["source_attestation"],
            "staging_identity": capability["staging_identity"],
        }
        (root / STAGE_MARKER).write_text(json.dumps(marker))
    return request


def _inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_schema_and_all_fixtures_are_byte_identical_to_forge():
    assert FORGE_PROTOCOL.is_dir(), "Forge authoritative protocol directory is required"
    local_files = sorted(
        path.relative_to(REPO / "protocol") for path in (REPO / "protocol").rglob("*.json")
    )
    forge_files = sorted(
        path.relative_to(FORGE_PROTOCOL) for path in FORGE_PROTOCOL.rglob("*.json")
    )
    assert local_files == forge_files
    for relative in local_files:
        assert (REPO / "protocol" / relative).read_bytes() == (
            FORGE_PROTOCOL / relative
        ).read_bytes()
    assert (
        hashlib.sha256((REPO / "protocol/pybench-op-v2.schema.json").read_bytes()).hexdigest()
        == "d5e80b58e4f18b37d0e906e7dac127fd17fbb88ea56dd83926406691303b655b"
    )


def test_invalid_v1_missing_and_unknown_capability_fields_are_rejected(tmp_path: Path):
    root = _init_bundle(tmp_path / "bundle")
    request = _request(root)
    v1 = json.loads((REPO / "protocol/fixtures/invalid/v1_request.json").read_text())
    with pytest.raises(ProtocolError, match="writable v1 is disabled"):
        validate_request(v1)
    missing = copy.deepcopy(request)
    missing["execution_capability"].pop("source_attestation")
    with pytest.raises(ProtocolError, match="fields mismatch"):
        validate_request(missing)
    unknown = copy.deepcopy(request)
    unknown["execution_capability"]["ambient_write"] = True
    with pytest.raises(ProtocolError, match="unknown"):
        validate_request(unknown)


def test_direct_staging_open_requires_validated_request(tmp_path: Path):
    root = _init_bundle(tmp_path / "source")
    with pytest.raises(BundleError, match="validated v2 request"):
        open_bundle(root, access_mode="staging_write", capability={"write_allowlist": []})


def test_read_only_rejects_live_regular_sqlite_sidecar_before_open(tmp_path: Path):
    root = _init_bundle(tmp_path / "source")
    (root / "db/app.sqlite-wal").write_bytes(b"residual WAL")
    before = _inventory(root)
    with pytest.raises(BundleError, match="live SQLite sidecar"):
        open_bundle_for_request(_request(root, mode="read_only"))
    assert _inventory(root) == before


def test_sqlite_guard_blocks_null_pragmas_disable_and_tracks_raw_transactions(tmp_path: Path):
    root = _init_bundle(tmp_path / "stage")
    setup = sqlite3.connect(root / "db/app.sqlite")
    setup.execute("CREATE UNIQUE INDEX artifact_labels_unique_label ON artifact_labels(label)")
    setup.executemany(
        "INSERT INTO artifact_labels VALUES(?,?,?,?,?)",
        [
            ("sibling", "c1", "sibling-x", "categorical", None),
            (None, "c2", "null-x", "categorical", None),
        ],
    )
    setup.commit()
    setup.close()
    bundle = open_bundle_for_request(_request(root))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authorizer is locked"):
            bundle.con.set_authorizer(None)
        for statement in (
            "PRAGMA user_version=77",
            "PRAGMA journal_mode=WAL",
            "ANALYZE",
            "REINDEX",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                bundle.con.execute(statement)
        for statement in (
            "INSERT INTO artifact_labels VALUES(NULL,'c1','x','categorical',NULL)",
            "REPLACE INTO artifact_labels VALUES(NULL,'c1','x','categorical',NULL)",
            "DELETE FROM artifact_labels WHERE artifact_id IS NULL",
            "DELETE FROM artifact_labels WHERE artifact_id='sibling'",
            "REPLACE INTO artifact_labels VALUES('labels','c9','sibling-x','categorical',NULL)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="OUT_OF_SCOPE"):
                bundle.con.execute(statement)

        bundle.con.rollback()
        bundle.con.execute("SAVEPOINT transient")
        bundle.con.execute(
            "INSERT INTO artifact_labels VALUES('labels','c1','x','categorical',NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="OUT_OF_SCOPE"):
            bundle.con.execute(
                "UPDATE artifact_labels SET artifact_id=NULL WHERE artifact_id='labels'"
            )
        bundle.con.execute("ROLLBACK TO transient")
        bundle.con.execute("RELEASE transient")
        assert build_write_manifest(bundle) == []

        bundle.con.execute(
            "INSERT INTO artifact_labels VALUES('labels','c1','x','categorical',NULL)"
        )
        with pytest.raises(CapabilityError, match="open transaction"):
            build_write_manifest(bundle)
        bundle.con.commit()
        manifest = build_write_manifest(bundle)
        assert [(item["table"], item["row_count"]) for item in manifest] == [("artifact_labels", 1)]
    finally:
        close_bundle(bundle)


def test_manifest_audit_detects_alternate_connection_and_payload_sibling(tmp_path: Path):
    root = _init_bundle(tmp_path / "stage")
    request = _request(root)
    bundle = open_bundle_for_request(request)
    try:
        other = sqlite3.connect(root / "db/app.sqlite")
        other.execute("UPDATE cells SET dataset_id='escaped' WHERE cell_id='c1'")
        other.commit()
        other.close()
        with pytest.raises(CapabilityError, match="undeclared SQLite table"):
            build_write_manifest(bundle)
    finally:
        close_bundle(bundle)

    root = _init_bundle(tmp_path / "payload_stage")
    request = _request(root)
    request["outputs"] = {"model": {"artifact_id": "model", "type": "model_ref"}}
    request["execution_capability"]["write_allowlist"] = [
        {
            "output_name": "model",
            "output_id": "model",
            "output_kind": "model_ref",
            "mutation_class": "payload_tree",
            "path": "artifact_models/model",
        }
    ]
    (root / STAGE_MARKER).write_text(
        json.dumps(
            {
                "schema": "scworkbench.pybench.stage.v2",
                "protocol_version": "pybench-op-v2",
                "capability_id": request["execution_capability"]["capability_id"],
                "session_id": request["execution_capability"]["session_id"],
                "run_id": request["execution_capability"]["run_id"],
                "source_attestation": request["execution_capability"]["source_attestation"],
                "staging_identity": request["execution_capability"]["staging_identity"],
            }
        )
    )
    bundle = open_bundle_for_request(request)
    try:
        (root / "artifact_models/sibling").write_text("escaped")
        with pytest.raises(CapabilityError, match="undeclared or ambiguous payload"):
            build_write_manifest(bundle)
    finally:
        close_bundle(bundle)


def test_read_only_open_has_no_sidecars_and_every_shared_writer_fails_pre_mutation(tmp_path: Path):
    root = _init_bundle(tmp_path / "source")
    request = _request(root, mode="read_only")
    before = _inventory(root)
    bundle = open_bundle_for_request(request)
    try:
        assert bundle.con.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 2
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            write_artifact_cell_vector_payload(bundle, "labels", ["c1"], [1.0])
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            write_artifact_relation_family_payload(bundle, "relations", ["b"], ["c1"])
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            write_artifact_path_2d_family_payload(bundle, "curves", ["b"], [1], [0.0], [0.0])
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            write_artifact_table_payload(
                bundle,
                "summary",
                artifact_type="obs_table",
                table_rows=[{"x": 1}],
            )
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            publish_artifact_model_payload(
                bundle,
                "model",
                engine="mock",
                model_space_id="space",
                model_path="artifact_models/model",
                writer=lambda path: path.mkdir(),
                expected_type="directory",
            )
        with pytest.raises(CapabilityError, match="PYBENCH_SOURCE_WRITE_ATTEMPT"):
            add_space(bundle, "new_space", [[0.0, 1.0], [1.0, 0.0]], cell_id=["c1", "c2"])
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            bundle.con.execute("DELETE FROM cells")
    finally:
        close_bundle(bundle)
    assert _inventory(root) == before
    assert not list(root.rglob("*-wal"))
    assert not list(root.rglob("*-shm"))


def test_hello_uses_capability_writer_and_receipt_matches_independent_rows(tmp_path: Path):
    root = _init_bundle(tmp_path / "transition")
    request = _request(root)
    receipt = run_hello(request)
    validate_receipt(receipt, request)
    assert receipt["capability_echo"] == request["execution_capability"]
    assert len(receipt["write_manifest"]) == 1
    descriptor = receipt["write_manifest"][0]
    assert descriptor["row_count"] == 2
    assert descriptor["content_sha256"] == (
        "6afd2e473a461f2aa6705171b9e284748dc1178a314a68e3aecb1c916f8a3ce8"
    )


def test_receipt_publication_is_atomic_on_rename_failure(tmp_path: Path, monkeypatch):
    root = _init_bundle(tmp_path / "transition")
    request = _request(root)
    receipt = run_hello(request)
    destination = tmp_path / "receipt.json"

    def fail_replace(_source, _destination):
        raise OSError("injected receipt rename failure")

    monkeypatch.setattr("pybench.protocol.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_receipt(destination, receipt, request=request)
    assert not destination.exists()
    assert not list(tmp_path.glob(".receipt.json.pybench-*.tmp"))


def test_sqlite_row_identity_is_stable_for_float_null_text_blob_and_shared_table(
    tmp_path: Path,
):
    root = _init_bundle(tmp_path / "numeric_identity")
    connection = sqlite3.connect(root / "db/app.sqlite")
    connection.execute(
        """
        CREATE TABLE numeric_rows(
          output_id TEXT, text_value TEXT, real_value REAL,
          int_value INTEGER, null_value TEXT, blob_value BLOB
        )
        """
    )
    connection.executemany(
        "INSERT INTO numeric_rows VALUES(?,?,?,?,?,?)",
        [
            (
                "out_a",
                "quote ' snowman ☃\nline",
                0.1,
                9007199254740991,
                None,
                b"\x00\xff",
            ),
            ("out_a", "tiny", 1.2345678901234567, -7, None, b"abc"),
            ("out_b", "other", -98765.43210987654, 42, None, b""),
        ],
    )
    connection.commit()
    connection.close()
    allowances = [
        sqlite_allowance(name, name, "obs_table", "numeric_rows", "output_id")
        for name in ("out_a", "out_b")
    ]
    bundle = raw_write_bundle(root, allowances, enforce=True)
    try:
        for allowance in allowances:
            record_write(bundle, allowance)
        manifest = build_write_manifest(bundle)
        assert manifest == []
        assert [
            (
                output_id,
                *sqlite_rows_content_identity(bundle, "numeric_rows", "output_id", [output_id]),
            )
            for output_id in ("out_a", "out_b")
        ] == [
            (
                "out_a",
                2,
                "d3a0ff82b436b6e208f53cfb77ba390ea1eb02abd4ca7e289419681b86fe7d9a",
            ),
            (
                "out_b",
                1,
                "d7f3b14cb6ddc68826f833b5975ba6af640003dd923f0f3539465424908569d1",
            ),
        ]
        assert sqlite_rows_content_identity(bundle, "numeric_rows", "output_id", ["out_a"])[0] == 2
    finally:
        close_bundle(bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_id", "wrong"),
        ("session_id", "wrong"),
        ("run_id", "wrong"),
        (
            "source_attestation",
            {"scheme": "scworkbench-scientific-state-v1", "content_sha256": "3" * 64},
        ),
        ("staging_identity", {"root_sha256": "4" * 64, "checkpoint_generation": "stale"}),
    ],
)
def test_stage_marker_binding_tamper_is_rejected_before_writable_open(
    tmp_path: Path, field: str, value
):
    root = _init_bundle(tmp_path / "transition")
    request = _request(root)
    marker = json.loads((root / STAGE_MARKER).read_text())
    marker[field] = value
    (root / STAGE_MARKER).write_text(json.dumps(marker))
    before = _inventory(root)
    with pytest.raises(CapabilityError, match="PYBENCH_STALE_STAGING_IDENTITY"):
        open_bundle_for_request(request)
    assert _inventory(root) == before


def test_undeclared_table_and_payload_symlink_escape_fail_before_mutation(tmp_path: Path):
    root = _init_bundle(tmp_path / "transition")
    request = _request(root)
    bundle = open_bundle_for_request(request)
    try:
        with pytest.raises(CapabilityError, match="OUT_OF_SCOPE"):
            write_artifact_cell_vector_payload(bundle, "sibling", ["c1"], [1.0])
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            bundle.con.execute("DELETE FROM cells")
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            bundle.con.execute("CREATE TABLE escaped_schema(x TEXT)")
        with pytest.raises(sqlite3.IntegrityError, match="OUT_OF_SCOPE"):
            bundle.con.execute(
                "INSERT INTO artifact_labels VALUES ('sibling','c1','x','categorical',NULL)"
            )
    finally:
        close_bundle(bundle)

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "artifact_models").rmdir()
    (root / "artifact_models").symlink_to(outside, target_is_directory=True)
    model_allow = {
        "output_name": "model",
        "output_id": "model",
        "output_kind": "model_ref",
        "mutation_class": "payload_tree",
        "path": "artifact_models/model",
    }
    request["outputs"] = {"model": {"artifact_id": "model", "type": "model_ref"}}
    request["execution_capability"]["write_allowlist"] = [
        {
            "output_name": "model",
            "output_id": "model",
            "output_kind": "model_ref",
            "mutation_class": "sqlite_rows",
            "table": "artifact_model_ref",
            "key_scope": {"column": "artifact_id", "values": ["model"]},
        },
        model_allow,
    ]
    request["execution_capability"]["capability_id"] = "cap_model"
    marker = json.loads((root / STAGE_MARKER).read_text())
    marker["capability_id"] = "cap_model"
    (root / STAGE_MARKER).write_text(json.dumps(marker))
    with pytest.raises(Exception, match="SYMLINK_ESCAPE"):
        open_bundle_for_request(request)
    assert not any(outside.iterdir())


def test_model_writer_failure_removes_private_temporary_tree(tmp_path: Path):
    root = _init_bundle(tmp_path / "transition")
    request = _request(root)
    request["outputs"] = {"model": {"artifact_id": "model", "type": "model_ref"}}
    request["execution_capability"]["write_allowlist"] = [
        {
            "output_name": "model",
            "output_id": "model",
            "output_kind": "model_ref",
            "mutation_class": "sqlite_rows",
            "table": "artifact_model_ref",
            "key_scope": {"column": "artifact_id", "values": ["model"]},
        },
        {
            "output_name": "model",
            "output_id": "model",
            "output_kind": "model_ref",
            "mutation_class": "payload_tree",
            "path": "artifact_models/model",
        },
    ]
    marker = json.loads((root / STAGE_MARKER).read_text())
    (root / STAGE_MARKER).write_text(json.dumps(marker))
    bundle = open_bundle_for_request(request)

    def fail_after_partial_write(path: Path) -> None:
        path.mkdir()
        (path / "partial").write_text("partial")
        (path.parent / "model.sidecar").write_text("serializer sibling")
        raise RuntimeError("injected model failure")

    try:
        with pytest.raises(CapabilityError, match="OUT_OF_SCOPE"):
            authorize_payload(bundle, "artifact_models/model_evil", "model")
        assert (
            authorize_payload(bundle, "artifact_models/model.pkl", "model")["path"]
            == "artifact_models/model"
        )
        with pytest.raises(RuntimeError, match="injected"):
            publish_artifact_model_payload(
                bundle,
                "model",
                engine="mock",
                model_space_id="space",
                model_path="artifact_models/model",
                writer=fail_after_partial_write,
                expected_type="directory",
            )
        assert not list((root / "artifact_models").iterdir())
        assert bundle.con.execute("SELECT COUNT(*) FROM artifact_model_ref").fetchone()[0] == 0

        outside = tmp_path / "outside-model.bin"
        outside.write_bytes(b"outside")

        def hardlink_writer(path: Path) -> None:
            path.mkdir()
            os.link(outside, path / "linked.bin")

        with pytest.raises(Exception, match="HARDLINK_FORBIDDEN"):
            publish_artifact_model_payload(
                bundle,
                "model",
                engine="mock",
                model_space_id="space",
                model_path="artifact_models/model",
                writer=hardlink_writer,
                expected_type="directory",
            )
        assert outside.read_bytes() == b"outside"
        assert not list((root / "artifact_models").iterdir())
    finally:
        close_bundle(bundle)


@pytest.mark.parametrize(
    ("module", "op_name", "outputs"),
    [
        (
            scvi_train,
            "scvi_train",
            {
                "model": {"artifact_id": "model", "type": "model_ref"},
                "fit_summary": {"artifact_id": "summary", "type": "obs_table"},
                "space_registry": {"artifact_id": "latent", "type": "space"},
            },
        ),
        (
            peakvi_fit,
            "peakvi_fit",
            {
                "model": {"artifact_id": "model", "type": "model_ref"},
                "space_registry": {"artifact_id": "latent", "type": "space"},
            },
        ),
        (
            peakvi_project,
            "peakvi_project",
            {"space_registry": {"artifact_id": "latent", "type": "space"}},
        ),
        (
            palantir_fit,
            "palantir_fit",
            {
                "pseudotime": {"artifact_id": "labels", "type": "cell_vector"},
                "branch_membership": {
                    "artifact_id": "relations",
                    "type": "relation_family",
                },
                "branch_summary": {"artifact_id": "summary", "type": "obs_table"},
                "model": {"artifact_id": "model", "type": "model_ref"},
            },
        ),
        (
            palantir_project,
            "palantir_project",
            {"curves": {"artifact_id": "curves", "type": "path_2d_family"}},
        ),
    ],
)
def test_named_ops_mock_only_training_and_use_real_v2_writers(
    tmp_path: Path, monkeypatch, module, op_name: str, outputs: dict
):
    root = _init_bundle(tmp_path / op_name)
    allowances = []
    for name, spec in outputs.items():
        output_id = spec["artifact_id"]
        kind = spec["type"]
        if kind == "space":
            allowances.extend(
                [
                    sqlite_allowance(name, output_id, kind, "spaces", "space_id"),
                    sqlite_allowance(name, output_id, kind, "space_payloads", "space_id"),
                    payload_allowance(name, output_id, kind, "space_payloads"),
                ]
            )
        elif kind == "model_ref":
            allowances.extend(
                [
                    sqlite_allowance(name, output_id, kind, "artifact_model_ref", "artifact_id"),
                    payload_allowance(name, output_id, kind, f"artifact_models/{output_id}"),
                ]
            )
        elif kind == "obs_table":
            allowances.extend(
                [
                    sqlite_allowance(name, output_id, kind, "artifact_tables", "artifact_id"),
                    sqlite_allowance(
                        name,
                        output_id,
                        kind,
                        "artifact_table_rows",
                        "artifact_id",
                    ),
                ]
            )
        else:
            table = {
                "cell_vector": "artifact_labels",
                "relation_family": "artifact_relation_rows",
                "path_2d_family": "artifact_curves_2d",
            }[kind]
            allowances.append(sqlite_allowance(name, output_id, kind, table, "artifact_id"))
    params = (
        {"output_space_id": "latent"}
        if any(spec["type"] == "space" for spec in outputs.values())
        else {}
    )
    request = v2_request(
        root,
        op_name=op_name,
        inputs={"cell_set": "core"},
        params=params,
        outputs=outputs,
        allowances=allowances,
        run_id=f"run_{op_name}",
    )
    monkeypatch.setenv("PYBENCH_TEST_MOCK_TRAINING", "1")
    receipt = module._run(request)
    validate_receipt(receipt, request)
    assert receipt["status"] == "ok"
    assert receipt["write_manifest"]
    assert all(item["content_sha256"] != "0" * 64 for item in receipt["write_manifest"])
    if any(spec["type"] == "model_ref" for spec in outputs.values()):
        expected_engine = {
            "scvi_train": "scvi",
            "peakvi_fit": "peakvi",
            "palantir_fit": "palantir",
        }[op_name]
        connection = sqlite3.connect(root / "db/app.sqlite")
        row = connection.execute(
            "SELECT engine, model_type FROM artifact_model_ref WHERE artifact_id='model'"
        ).fetchone()
        connection.close()
        assert row == (expected_engine, expected_engine)
