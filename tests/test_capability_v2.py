from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from capability_helpers import payload_allowance, sqlite_allowance, v2_request
from pybench.artifacts_payload import (
    publish_artifact_model_payload,
    write_artifact_cell_vector_payload,
    write_artifact_path_2d_family_payload,
    write_artifact_relation_family_payload,
    write_artifact_table_payload,
)
from pybench.bundle import close_bundle
from pybench.capability import STAGE_MARKER, CapabilityError, build_write_manifest
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
)
from pybench.spaces_write import add_space

REPO = Path(__file__).resolve().parents[1]
FORGE_PROTOCOL = Path(
    "/home/dirk/Projects/lymphocyte_trajectory/scworkbench/inst/protocol"
)


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
        CREATE TABLE space_2d(space_id TEXT, cell_id TEXT, x REAL, y REAL);
        CREATE TABLE space_coords(space_id TEXT, cell_id TEXT, dim INTEGER, value REAL);
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
    local_files = sorted(path.relative_to(REPO / "protocol") for path in (REPO / "protocol").rglob("*.json"))
    forge_files = sorted(path.relative_to(FORGE_PROTOCOL) for path in FORGE_PROTOCOL.rglob("*.json"))
    assert local_files == forge_files
    for relative in local_files:
        assert (REPO / "protocol" / relative).read_bytes() == (FORGE_PROTOCOL / relative).read_bytes()
    assert hashlib.sha256((REPO / "protocol/pybench-op-v2.schema.json").read_bytes()).hexdigest() == "d5e80b58e4f18b37d0e906e7dac127fd17fbb88ea56dd83926406691303b655b"


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
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
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
    con = sqlite3.connect(root / "db/app.sqlite")
    rows = con.execute(
        "SELECT artifact_id,cell_id,label,value_num,value_type FROM artifact_labels ORDER BY artifact_id,cell_id,label,value_num,value_type"
    ).fetchall()
    con.close()
    canonical = [
        {
            "artifact_id": row[0],
            "cell_id": row[1],
            "label": row[2],
            "value_num": int(row[3]) if float(row[3]).is_integer() else row[3],
            "value_type": row[4],
        }
        for row in rows
    ]
    expected = hashlib.sha256(
        json.dumps(canonical, separators=(",", ":")).encode()
    ).hexdigest()
    assert descriptor["content_sha256"] == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_id", "wrong"),
        ("session_id", "wrong"),
        ("run_id", "wrong"),
        ("source_attestation", {"scheme": "scworkbench-scientific-state-v1", "content_sha256": "3" * 64}),
        ("staging_identity", {"root_sha256": "4" * 64, "checkpoint_generation": "stale"}),
    ],
)
def test_stage_marker_binding_tamper_is_rejected_before_writable_open(tmp_path: Path, field: str, value):
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
    bundle = open_bundle_for_request(request)
    try:
        with pytest.raises(Exception, match="SYMLINK_ESCAPE"):
            publish_artifact_model_payload(
                bundle,
                "model",
                engine="mock",
                model_space_id="space",
                model_path="artifact_models/model",
                writer=lambda path: path.mkdir(),
                expected_type="directory",
            )
        assert build_write_manifest(bundle) == []
    finally:
        close_bundle(bundle)
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
    (root / "artifact_models").mkdir()
    bundle = open_bundle_for_request(request)

    def fail_after_partial_write(path: Path) -> None:
        path.mkdir()
        (path / "partial").write_text("partial")
        raise RuntimeError("injected model failure")

    try:
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
                    sqlite_allowance(name, output_id, kind, "space_2d", "space_id"),
                ]
            )
        elif kind == "model_ref":
            allowances.extend(
                [
                    sqlite_allowance(
                        name, output_id, kind, "artifact_model_ref", "artifact_id"
                    ),
                    payload_allowance(
                        name, output_id, kind, f"artifact_models/{output_id}"
                    ),
                ]
            )
        elif kind == "obs_table":
            allowances.extend(
                [
                    sqlite_allowance(
                        name, output_id, kind, "artifact_tables", "artifact_id"
                    ),
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
            allowances.append(
                sqlite_allowance(name, output_id, kind, table, "artifact_id")
            )
    params = {"output_space_id": "latent"} if any(
        spec["type"] == "space" for spec in outputs.values()
    ) else {}
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
