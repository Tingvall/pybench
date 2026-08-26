from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from capability_helpers import payload_allowance, sqlite_allowance, v2_request
from pybench.operation_packs import (
    ENTRY_POINT_GROUP,
    OperationPackError,
    OperationPackHandlers,
    _distribution_bound_source_revision,
    _distribution_build_metadata,
    activate_operation_pack,
    canonical_sha256,
    discover_operation_pack,
    execute_operation_pack,
    operation_pack_handlers,
    operation_pack_identity,
    runtime_attestation,
    validate_manifest,
)
from pybench.protocol import validate_receipt

REPO = Path(__file__).parents[1]
FIXTURES = REPO / "protocol/fixtures/operation-pack-v1"
MANIFEST = FIXTURES / "valid/python-hyphen.json"


def _init_stage(root: Path) -> dict:
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db/app.sqlite")
    con.executescript(
        """
        CREATE TABLE cells(cell_id TEXT PRIMARY KEY);
        CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY, type TEXT NOT NULL, created_at TEXT);
        CREATE TABLE artifact_labels(
          artifact_id TEXT, cell_id TEXT, label TEXT, value_type TEXT, value_num REAL,
          PRIMARY KEY(artifact_id,cell_id)
        );
        CREATE TABLE artifact_tables(
          artifact_id TEXT PRIMARY KEY, table_type TEXT, entity_kind TEXT, meta_json TEXT
        );
        CREATE TABLE artifact_table_rows(
          artifact_id TEXT, row_idx INTEGER, row_json TEXT,
          PRIMARY KEY(artifact_id,row_idx)
        );
        INSERT INTO cells VALUES('c1'),('c2'),('c3');
        INSERT INTO artifacts VALUES
          ('input','cell_vector','2026-08-25'),
          ('output','cell_vector','2026-08-25'),
          ('summary','obs_table','2026-08-25');
        INSERT INTO artifact_labels VALUES
          ('input','c1','1','continuous',1.0),
          ('input','c2','2','continuous',2.0),
          ('input','c3','3','continuous',3.0);
        """
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    outputs = {
        "vector": {"artifact_id": "output", "type": "cell_vector"},
        "summary": {"artifact_id": "summary", "type": "obs_table"},
    }
    return v2_request(
        root,
        op_name="scwbpython::affine_cell_vector",
        inputs={"signal": {"artifact_id": "input", "type": "cell_vector"}},
        params={"scale": 2.0},
        outputs=outputs,
        allowances=[
            sqlite_allowance("vector", "output", "cell_vector", "artifact_labels", "artifact_id"),
            sqlite_allowance("summary", "summary", "obs_table", "artifact_tables", "artifact_id"),
            sqlite_allowance(
                "summary", "summary", "obs_table", "artifact_table_rows", "artifact_id"
            ),
        ],
        run_id="run_python_pack",
    )


def _identities():
    discovery = discover_operation_pack(MANIFEST)
    identity = discovery.identity
    return discovery, {
        "expected_pack_sha256": identity["pack_sha256"],
        "expected_definition_sha256": identity["operation_definition_sha256"][
            "scwbpython::affine_cell_vector"
        ],
        "expected_distribution_sha256": discovery.artifact_identity["sha256"],
    }


def test_schema_fixture_inventory_and_frozen_identities_are_exact():
    schema = REPO / "protocol/operation-pack-v1.schema.json"
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == (
        "01248103dc8527062eda8732a2dc8e3ad9227579c457b65b013606e348e8fe5f"
    )
    expected = json.loads((FIXTURES / "canonical-identities.json").read_text())
    for manifest_path in sorted((FIXTURES / "valid").glob("python-*.json")):
        manifest = json.loads(manifest_path.read_text())
        identity = operation_pack_identity(manifest_path)
        frozen = expected["python_reference"]["declared_name_variants"][manifest["package"]["name"]]
        assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == frozen["fixture_sha256"]
        assert identity["manifest_sha256"] == frozen["manifest_sha256"]
        assert identity["pack_sha256"] == frozen["pack_sha256"]
        assert set(identity["operation_definition_sha256"].values()) == {
            expected["python_reference"]["operation_definition_sha256"]
        }


def test_canonical_identity_is_independent_process_deterministic():
    script = (
        "import json; from pybench.operation_packs import operation_pack_identity; "
        f"print(json.dumps(operation_pack_identity({str(MANIFEST)!r}),sort_keys=True,separators=(',',':')))"
    )
    first = subprocess.check_output([sys.executable, "-c", script], text=True)
    second = subprocess.check_output([sys.executable, "-c", script], text=True)
    assert first == second
    assert json.loads(first) == operation_pack_identity(MANIFEST)


def test_real_installed_distribution_discovery_is_inert_and_explicit(monkeypatch):
    loaded = False

    def forbidden_load(_self):
        nonlocal loaded
        loaded = True
        raise AssertionError("structural discovery must not import entry point")

    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden_load)
    discovery = discover_operation_pack(MANIFEST)
    assert not loaded
    assert discovery.distribution.metadata["Name"] == "scworkbench-python-pack"
    assert discovery.entry_point.group == ENTRY_POINT_GROUP
    assert discovery.entry_point.name == "reference-pack"
    assert discovery.entry_point.value == "scwbpython.entrypoint:register"
    assert discovery.artifact_identity["scheme"] == ("python-installed-distribution-tree-sha256-v1")


def test_activation_loads_exact_installed_entry_point_and_handlers():
    discovery = discover_operation_pack(MANIFEST)
    registration = activate_operation_pack(discovery)
    assert isinstance(registration, OperationPackHandlers)
    assert set(registration.handlers) == {"scwbpython::affine_cell_vector"}


def test_runtime_attestation_has_frozen_shape_and_stable_content_identity():
    discovery = discover_operation_pack(MANIFEST)
    first = runtime_attestation(discovery)
    second = runtime_attestation(discovery)
    assert first == second
    assert set(first) == {
        "schema",
        "backend",
        "distribution",
        "entry_point",
        "interpreter",
        "dependencies",
        "pybench",
        "device",
        "seed",
    }
    assert first["schema"] == "scworkbench.operation_pack_runtime.python.v1"
    assert first["distribution"]["artifact_identity"] == discovery.artifact_identity
    assert first["dependencies"]["sha256"] == canonical_sha256(
        first["dependencies"]["distributions"]
    )


class _FakeDistribution:
    version = "0.1.0"

    def __init__(self, root: Path, direct_url: str | None = None):
        self.root = root
        self.metadata = {"Name": "pybench"}
        self.files = []
        if direct_url is not None:
            relative = metadata.PackagePath("pybench-0.1.0.dist-info/direct_url.json")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(direct_url, encoding="utf-8")
            self.files.append(relative)

    def locate_file(self, path):
        return self.root / path


def _git_checkout(path: Path, *, project_name: str = "pybench") -> tuple[Path, str]:
    module = path / "src/pybench/operation_packs.py"
    module.parent.mkdir(parents=True)
    module.write_text("# test module\n", encoding="utf-8")
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "PyBench test"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    return module, revision


def test_source_revision_accepts_distribution_bound_vcs_commit(tmp_path: Path):
    revision = "a" * 40
    distribution = _FakeDistribution(tmp_path)
    assert (
        _distribution_bound_source_revision(
            distribution,
            {
                "url": "https://example.invalid/pybench.git",
                "vcs_info": {"vcs": "git", "commit_id": revision},
            },
        )
        == revision
    )
    assert (
        _distribution_bound_source_revision(
            distribution,
            {"vcs_info": {"vcs": "git"}},
        )
        is None
    )


def test_editable_source_revision_requires_verified_pybench_checkout(tmp_path: Path):
    checkout = tmp_path / "pybench-checkout"
    module, revision = _git_checkout(checkout)
    distribution = _FakeDistribution(tmp_path / "site-packages")
    direct_url = {"url": checkout.as_uri(), "dir_info": {"editable": True}}
    assert (
        _distribution_bound_source_revision(distribution, direct_url, module_path=module)
        == revision
    )

    unrelated_module = tmp_path / "pack/src/pybench/operation_packs.py"
    unrelated_module.parent.mkdir(parents=True)
    unrelated_module.write_text("# unrelated\n", encoding="utf-8")
    assert (
        _distribution_bound_source_revision(distribution, direct_url, module_path=unrelated_module)
        is None
    )


def test_source_revision_rejects_nested_ambient_repo_and_missing_git(tmp_path: Path, monkeypatch):
    outer = tmp_path / "operation-pack"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    subprocess.run(
        ["git", "-C", str(outer), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(outer), "config", "user.name", "Pack test"], check=True)
    (outer / "pack.txt").write_text("unrelated\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(outer), "add", "."], check=True)
    subprocess.run(["git", "-C", str(outer), "commit", "-qm", "pack"], check=True)
    ambient_revision = subprocess.check_output(
        ["git", "-C", str(outer), "rev-parse", "HEAD"], text=True
    ).strip()

    checkout = outer / ".venv/src/pybench"
    module = checkout / "src/pybench/operation_packs.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed below unrelated repository\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "pybench"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    distribution = _FakeDistribution(outer / ".venv/site-packages")
    direct_url = {"url": checkout.as_uri(), "dir_info": {"editable": True}}
    reported = _distribution_bound_source_revision(distribution, direct_url, module_path=module)
    assert reported is None
    assert reported != ambient_revision

    monkeypatch.setattr(
        "pybench.operation_packs.subprocess.check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("git")),
    )
    assert _distribution_bound_source_revision(distribution, direct_url, module_path=module) is None


@pytest.mark.parametrize(
    "direct_url",
    [
        "{malformed",
        json.dumps(
            {
                "url": "file:///moved/or/missing/pybench-0.1.0.whl",
                "archive_info": {"hash": "sha256=" + "b" * 64},
            }
        ),
        json.dumps({"url": "file:///source/archive.tar.gz", "archive_info": {}}),
        json.dumps({"url": "file:///source/tree", "dir_info": {"editable": False}}),
    ],
)
def test_wheel_archive_and_malformed_direct_urls_have_unknown_revision(
    tmp_path: Path, direct_url: str
):
    distribution = _FakeDistribution(tmp_path, direct_url)
    build_metadata = _distribution_build_metadata(distribution)
    assert build_metadata["source_revision"] is None


def test_runtime_attestation_is_cwd_and_enclosing_repo_invariant(tmp_path: Path, monkeypatch):
    discovery = discover_operation_pack(MANIFEST)
    outer = tmp_path / "unrelated-pack"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    subprocess.run(
        ["git", "-C", str(outer), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(outer), "config", "user.name", "Pack test"], check=True)
    (outer / "pack.txt").write_text("unrelated\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(outer), "add", "."], check=True)
    subprocess.run(["git", "-C", str(outer), "commit", "-qm", "pack"], check=True)
    ambient_revision = subprocess.check_output(
        ["git", "-C", str(outer), "rev-parse", "HEAD"], text=True
    ).strip()
    distribution = _FakeDistribution(
        outer / ".venv/site-packages",
        json.dumps(
            {
                "url": "file:///original/pybench-0.1.0-py3-none-any.whl",
                "archive_info": {"hash": "sha256=" + "c" * 64},
            }
        ),
    )
    original_distribution = metadata.distribution
    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda name: distribution if name == "pybench" else original_distribution(name),
    )

    monkeypatch.chdir(outer)
    first = runtime_attestation(discovery)
    monkeypatch.chdir(tmp_path)
    second = runtime_attestation(discovery)
    assert first["pybench"]["source_revision"] is None
    assert second["pybench"]["source_revision"] is None
    assert first["pybench"]["source_revision"] != ambient_revision
    assert first["pybench"] == second["pybench"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(backend="R"), "OPERATION_PACK_BACKEND_MISMATCH"),
        (
            lambda value: value.update(namespace="scworkbench"),
            "OPERATION_PACK_NAMESPACE_RESERVED",
        ),
        (
            lambda value: value["operations"][0]["produces"].update(vector="unknown"),
            "OPERATION_PACK_MANIFEST_INVALID",
        ),
        (
            lambda value: value["operations"].append(copy.deepcopy(value["operations"][0])),
            "OPERATION_PACK_OPERATION_DUPLICATE",
        ),
        (
            lambda value: value.update(entry_point="module:callable"),
            "OPERATION_PACK_MANIFEST_INVALID",
        ),
    ],
)
def test_malformed_manifest_rejections(mutation, code: str):
    value = json.loads(MANIFEST.read_text())
    mutation(value)
    with pytest.raises(OperationPackError) as caught:
        validate_manifest(value)
    assert caught.value.code == code


def test_distribution_missing_version_and_entry_point_failures(monkeypatch):
    original = metadata.distribution

    def missing(_name):
        raise metadata.PackageNotFoundError("missing")

    monkeypatch.setattr(metadata, "distribution", missing)
    with pytest.raises(OperationPackError) as caught:
        discover_operation_pack(MANIFEST)
    assert caught.value.code == "OPERATION_PACK_DISTRIBUTION_UNAVAILABLE"
    monkeypatch.setattr(metadata, "distribution", original)

    real = original("scworkbench-python-pack")

    class WrongVersion:
        metadata = real.metadata
        version = "9.0"
        entry_points = real.entry_points
        files = real.files

        def locate_file(self, path):
            return real.locate_file(path)

    monkeypatch.setattr(metadata, "distribution", lambda _name: WrongVersion())
    with pytest.raises(OperationPackError) as caught:
        discover_operation_pack(MANIFEST)
    assert caught.value.code == "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH"

    class NoEntry(WrongVersion):
        version = real.version
        entry_points = []

    monkeypatch.setattr(metadata, "distribution", lambda _name: NoEntry())
    with pytest.raises(OperationPackError) as caught:
        discover_operation_pack(MANIFEST)
    assert caught.value.code == "OPERATION_PACK_ENTRY_POINT_UNAVAILABLE"

    class DuplicateEntry(NoEntry):
        entry_points = [tuple(real.entry_points)[0], tuple(real.entry_points)[0]]

    monkeypatch.setattr(metadata, "distribution", lambda _name: DuplicateEntry())
    with pytest.raises(OperationPackError) as caught:
        discover_operation_pack(MANIFEST)
    assert caught.value.code == "OPERATION_PACK_ENTRY_POINT_AMBIGUOUS"


def test_handler_registration_rejects_missing_extra_noncallable_and_signature(monkeypatch):
    discovery = discover_operation_pack(MANIFEST)
    entry = discovery.entry_point
    for registration in (
        operation_pack_handlers({}),
        operation_pack_handlers(
            {"scwbpython::affine_cell_vector": lambda context: None, "x::y": lambda context: None}
        ),
        operation_pack_handlers({"scwbpython::affine_cell_vector": 3}),
        operation_pack_handlers({"scwbpython::affine_cell_vector": lambda: None}),
    ):
        monkeypatch.setattr(
            metadata.EntryPoint, "load", lambda _self, value=registration: lambda: value
        )
        with pytest.raises(OperationPackError) as caught:
            activate_operation_pack(discovery)
        assert caught.value.code == "OPERATION_PACK_ENTRY_POINT_INVALID"
    monkeypatch.undo()
    assert entry.value == "scwbpython.entrypoint:register"


def test_real_v2_execution_receipt_and_reopen(tmp_path: Path):
    request = _init_stage(tmp_path / "stage")
    discovery, expected = _identities()
    receipt, attestation = execute_operation_pack(request, MANIFEST, **expected)
    validate_receipt(receipt, request)
    assert receipt["status"] == "ok"
    assert set(receipt["outputs"]) == {"vector", "summary"}
    assert {item["table"] for item in receipt["write_manifest"]} == {
        "artifact_labels",
        "artifact_tables",
        "artifact_table_rows",
    }
    con = sqlite3.connect(tmp_path / "stage/db/app.sqlite")
    try:
        values = con.execute(
            "SELECT value_num FROM artifact_labels WHERE artifact_id='output' ORDER BY cell_id"
        ).fetchall()
        summary = json.loads(
            con.execute(
                "SELECT row_json FROM artifact_table_rows WHERE artifact_id='summary'"
            ).fetchone()[0]
        )
    finally:
        con.close()
    assert values == [(2.0,), (4.0,), (6.0,)]
    assert summary["output_mean"] == 4.0
    assert attestation["distribution"]["artifact_identity"] == discovery.artifact_identity


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("expected_pack_sha256", "0" * 64, "OPERATION_PACK_IDENTITY_MISMATCH"),
        (
            "expected_definition_sha256",
            "0" * 64,
            "OPERATION_PACK_DEFINITION_IDENTITY_MISMATCH",
        ),
        (
            "expected_distribution_sha256",
            "0" * 64,
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
        ),
    ],
)
def test_execution_identity_substitution_fails_before_mutation(tmp_path: Path, field, value, code):
    request = _init_stage(tmp_path / field)
    _discovery, expected = _identities()
    expected[field] = value
    before = (tmp_path / field / "db/app.sqlite").read_bytes()
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack(request, MANIFEST, **expected)
    assert caught.value.code == code
    assert (tmp_path / field / "db/app.sqlite").read_bytes() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["inputs"].update(
            extra={"artifact_id": "input", "type": "cell_vector"}
        ),
        lambda request: request["params"].update(extra=1),
        lambda request: request["outputs"].pop("summary"),
        lambda request: request["execution_capability"].update(session_id="stale"),
        lambda request: request["execution_capability"]["staging_identity"].update(
            checkpoint_generation="stale"
        ),
    ],
)
def test_request_capability_and_contract_mismatch_is_pre_mutation(tmp_path: Path, mutate):
    request = _init_stage(tmp_path / "mismatch")
    _discovery, expected = _identities()
    mutate(request)
    before = (tmp_path / "mismatch/db/app.sqlite").read_bytes()
    with pytest.raises(Exception):
        execute_operation_pack(request, MANIFEST, **expected)
    assert (tmp_path / "mismatch/db/app.sqlite").read_bytes() == before


def test_handler_exception_after_write_restores_stage(tmp_path: Path, monkeypatch):
    request = _init_stage(tmp_path / "rollback")
    _discovery, expected = _identities()
    before = (tmp_path / "rollback/db/app.sqlite").read_bytes()

    def fail_after_write(context):
        signal = context.read_cell_vector("signal")
        context.write_cell_vector("vector", signal.cell_id, signal.value)
        raise RuntimeError("injected handler failure")

    registration = operation_pack_handlers({"scwbpython::affine_cell_vector": fail_after_write})
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _self: lambda: registration)
    with pytest.raises(RuntimeError, match="injected handler failure"):
        execute_operation_pack(request, MANIFEST, **expected)
    assert (tmp_path / "rollback/db/app.sqlite").read_bytes() == before


def test_incomplete_output_and_forged_receipt_restore_stage(tmp_path: Path, monkeypatch):
    request = _init_stage(tmp_path / "incomplete")
    _discovery, expected = _identities()
    before = (tmp_path / "incomplete/db/app.sqlite").read_bytes()

    def incomplete(context):
        signal = context.read_cell_vector("signal")
        context.write_cell_vector("vector", signal.cell_id, signal.value)

    registration = operation_pack_handlers({"scwbpython::affine_cell_vector": incomplete})
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda _self: lambda: registration)
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack(request, MANIFEST, **expected)
    assert caught.value.code == "OPERATION_PACK_OUTPUT_INVALID"
    assert (tmp_path / "incomplete/db/app.sqlite").read_bytes() == before

    request = _init_stage(tmp_path / "forged")
    before = (tmp_path / "forged/db/app.sqlite").read_bytes()
    monkeypatch.undo()
    monkeypatch.setattr(
        "pybench.operation_packs.ok_receipt",
        lambda **_kwargs: {
            "message_type": "receipt",
            "protocol_version": "pybench-op-v2",
            "status": "ok",
            "op_name": request["op_name"],
            "run_id": request["run"]["run_id"],
            "capability_echo": request["execution_capability"],
            "outputs": {},
            "write_manifest": [],
        },
    )
    with pytest.raises(Exception):
        execute_operation_pack(request, MANIFEST, **expected)
    assert (tmp_path / "forged/db/app.sqlite").read_bytes() == before


def test_distribution_tampering_during_activation_fails_before_stage_open(
    tmp_path: Path, monkeypatch
):
    request = _init_stage(tmp_path / "tamper")
    _discovery, expected = _identities()
    before = (tmp_path / "tamper/db/app.sqlite").read_bytes()
    module = __import__("pybench.operation_packs", fromlist=["_distribution_content_identity"])
    original = module._distribution_content_identity
    calls = 0

    def drifting(distribution):
        nonlocal calls
        calls += 1
        value = original(distribution)
        if calls >= 2:
            value = {**value, "sha256": "f" * 64}
        return value

    monkeypatch.setattr("pybench.operation_packs._distribution_content_identity", drifting)
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack(request, MANIFEST, **expected)
    assert caught.value.code == "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH"
    assert (tmp_path / "tamper/db/app.sqlite").read_bytes() == before


def test_undeclared_row_payload_schema_and_source_writes_remain_denied(tmp_path: Path):
    request = _init_stage(tmp_path / "denied")
    request["execution_capability"]["write_allowlist"].append(
        payload_allowance("vector", "output", "cell_vector", "declared-output")
    )
    # The shared protocol rejects allowance/output ambiguity and source identity drift;
    # existing capability tests cover the lower-level authorizer and path attacks.
    request["execution_capability"]["write_allowlist"][-1]["output_id"] = "sibling"
    _discovery, expected = _identities()
    with pytest.raises(Exception):
        execute_operation_pack(request, MANIFEST, **expected)


def test_load_order_does_not_change_manifest_identities():
    paths = sorted((FIXTURES / "valid").glob("python-*.json"))
    forward = [operation_pack_identity(path) for path in paths]
    reverse = [operation_pack_identity(path) for path in reversed(paths)]
    assert sorted(item["pack_sha256"] for item in forward) == sorted(
        item["pack_sha256"] for item in reverse
    )


def test_cli_discovery_execution_receipt_and_runtime_sidecar(tmp_path: Path):
    request = _init_stage(tmp_path / "cli-stage")
    discovery, expected = _identities()
    request_path = tmp_path / "request.json"
    receipt_path = tmp_path / "receipt.json"
    runtime_path = tmp_path / "runtime.json"
    discovery_path = tmp_path / "discovery.json"
    request_path.write_text(json.dumps(request))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pybench.operation_pack_cli",
            "discover",
            "--manifest",
            str(MANIFEST),
            "--output",
            str(discovery_path),
        ],
        check=True,
    )
    discovered = json.loads(discovery_path.read_text())
    assert discovered["manifest_identity"] == discovery.identity
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pybench.operation_pack_cli",
            "execute",
            "--manifest",
            str(MANIFEST),
            "--request",
            str(request_path),
            "--receipt",
            str(receipt_path),
            "--runtime-attestation",
            str(runtime_path),
            "--expected-pack-sha256",
            expected["expected_pack_sha256"],
            "--expected-definition-sha256",
            expected["expected_definition_sha256"],
            "--expected-distribution-sha256",
            expected["expected_distribution_sha256"],
        ],
        check=True,
    )
    validate_receipt(json.loads(receipt_path.read_text()), request)
    runtime = json.loads(runtime_path.read_text())
    assert runtime["schema"] == "scworkbench.operation_pack_runtime.python.v1"


def test_killed_subprocess_is_confined_to_disposable_stage_and_retryable(tmp_path: Path):
    stage = tmp_path / "killed-stage"
    request = _init_stage(stage)
    source = tmp_path / "committed-source"
    shutil.copytree(stage, source)
    (source / ".scworkbench-pybench-stage.json").unlink()
    source_before = (source / "db/app.sqlite").read_bytes()
    _discovery, expected = _identities()
    request_path = tmp_path / "killed-request.json"
    signal_path = tmp_path / "write-complete"
    request_path.write_text(json.dumps(request))
    script = tmp_path / "killed_handler.py"
    script.write_text(
        """
import importlib.metadata as metadata
import json
import sys
import time
from pathlib import Path
from pybench import operation_pack_handlers
from pybench.operation_packs import execute_operation_pack

request = json.loads(Path(sys.argv[1]).read_text())
manifest = Path(sys.argv[2])

def handler(context):
    signal = context.read_cell_vector("signal")
    context.write_cell_vector("vector", signal.cell_id, signal.value)
    Path(sys.argv[3]).write_text("written")
    time.sleep(60)

registration = operation_pack_handlers({"scwbpython::affine_cell_vector": handler})
metadata.EntryPoint.load = lambda self: (lambda: registration)
execute_operation_pack(
    request,
    manifest,
    expected_pack_sha256=sys.argv[4],
    expected_definition_sha256=sys.argv[5],
    expected_distribution_sha256=sys.argv[6],
)
"""
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(request_path),
            str(MANIFEST),
            str(signal_path),
            expected["expected_pack_sha256"],
            expected["expected_definition_sha256"],
            expected["expected_distribution_sha256"],
        ]
    )
    deadline = time.monotonic() + 15
    while not signal_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert signal_path.exists()
    process.kill()
    assert process.wait(timeout=5) != 0
    assert (source / "db/app.sqlite").read_bytes() == source_before

    retry = _init_stage(tmp_path / "retry-stage")
    receipt, _attestation = execute_operation_pack(retry, MANIFEST, **expected)
    validate_receipt(receipt, retry)
