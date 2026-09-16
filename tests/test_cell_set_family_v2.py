from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pybench.cell_set_family import normalize_cell_set_family
from pybench.operation_packs import OperationPackError, canonical_sha256
from pybench.operation_packs_v2 import (
    activate_operation_pack_v2,
    discover_operation_pack_v2,
    unload_operation_pack_v2,
)
from pybench.protocol_v3 import execute_operation_pack_v3, validate_receipt_v3
from v3_helpers import fixture_inputs, install_reference_fixture, make_request

ROOT = Path(__file__).parents[1]
LEDGER = ROOT / "protocol/fixtures/operation-pack-v2/identities.json"


def _source():
    return {
        "label": "test",
        "created_by": None,
        "input_artifact_id": None,
        "input_run_id": None,
        "input_snapshot_id": None,
    }


def _payload(sets=None, mode="add"):
    return {
        "kind": "cell_set_family_admission",
        "sets": sets or [],
        "mode": mode,
        "source": _source(),
    }


def _inputs():
    inputs = fixture_inputs()
    inputs["matrix"].update(
        dimensions=[1, 4],
        cell_id=["cell_c", "cell_a", "cell_b", "cell_d"],
        feature_id=["feature_1"],
        values=[[1, 2, 3, 4]],
    )
    return inputs


@pytest.fixture
def installed(tmp_path, monkeypatch):
    manifest = install_reference_fixture(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v2(manifest)
    registration = activate_operation_pack_v2(discovery)
    yield discovery, registration
    unload_operation_pack_v2("scwbpyv2reference")


def _execute(installed, tmp_path, payload):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    inputs = _inputs()
    request = make_request(
        discovery, stage, "scwbpyv2reference::family_probe", inputs, {"family": payload}
    )
    receipt, sidecar = execute_operation_pack_v3(request, discovery, stage)
    validate_receipt_v3(receipt, request)
    return stage, request, receipt, sidecar


def test_forge_family_identity_fixture():
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    normalized, evidence = normalize_cell_set_family(
        fixture["input_payload"], fixture["cell_order"]
    )
    assert normalized == fixture["normalized_payload"]
    assert evidence == {
        "family_identity": fixture["family_identity"],
        "member_identities": fixture["member_identities"],
        "source_identity": fixture["source_identity"],
    }


@pytest.mark.parametrize("count", [0, 1, 2])
def test_family_execution_has_complete_receipt_and_canonical_payload(installed, tmp_path, count):
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    payload = copy.deepcopy(fixture["input_payload"])
    payload["sets"] = payload["sets"][:count]
    stage, request, receipt, sidecar = _execute(installed, tmp_path, payload)
    normalized, identities = normalize_cell_set_family(payload, _inputs()["matrix"]["cell_id"])
    actual = json.loads((stage / "registry_requests/family/payload.json").read_text())
    assert actual == normalized
    assert receipt["status"] == "ok"
    assert len(receipt["registry_requests"]) == 1
    assert len(receipt["outputs"]) == 1
    descriptor = receipt["registry_requests"][0]["descriptor"]
    assert descriptor["dimensions"] == [
        count,
        sum(len(item["cells"]) for item in normalized["sets"]),
    ]
    assert descriptor["family_identity"] == identities["family_identity"]
    assert descriptor["member_identities"] == identities["member_identities"]
    assert descriptor["source_identity"] == identities["source_identity"]
    assert descriptor["alignment"] == "family_name_then_core_cell_order"
    assert request["outputs"][0]["descriptor"]["expected_dimensions"] == [0, 0]
    assert "registry_requests" not in sidecar
    assert canonical_sha256(actual) == descriptor["family_identity"]["sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(mode="merge"),
        lambda p: p["sets"][0].update(name="bad/name"),
        lambda p: p["sets"][0].update(cells=["cell_a", "cell_a"]),
        lambda p: p["sets"][0].update(cells=["foreign"]),
        lambda p: p["sets"].append(copy.deepcopy(p["sets"][0])),
        lambda p: p["sets"][0].update(unexpected=True),
        lambda p: p["source"].update(input_run_id="run"),
        lambda p: p["source"].update(label=""),
    ],
)
def test_malformed_family_rolls_back_complete_stage(installed, tmp_path, mutation):
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    payload = copy.deepcopy(fixture["input_payload"])
    mutation(payload)
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::family_probe", _inputs(), {"family": payload}
    )
    before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack_v3(request, discovery, stage)
    assert caught.value.code == "PYBENCH_OUTPUT_INVALID"
    assert {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("mode", ["missing", "duplicate", "extra"])
def test_family_writer_closure_is_write_once(installed, tmp_path, monkeypatch, mode):
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    discovery, registration = installed
    original = registration.handlers["scwbpyv2reference::family_probe"]

    def handler(readers, writers, params, context):
        if mode == "missing":
            readers["matrix"]()
            writers.write_terminal_primitive(
                "summary", {"kind": "obs_table", "dimensions": [1, 1], "rows": [{"value": 1}]}
            )
        else:
            original(readers, writers, params, context)
            writers.request_cell_set_family_admission(
                "family" if mode == "duplicate" else "extra", params["family"]
            )

    monkeypatch.setitem(registration.handlers, "scwbpyv2reference::family_probe", handler)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::family_probe",
        _inputs(),
        {"family": fixture["input_payload"]},
    )
    before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    with pytest.raises(OperationPackError):
        execute_operation_pack_v3(request, discovery, stage)
    assert {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "field", ["family_identity", "member_identities", "source_identity", "dimensions"]
)
def test_family_receipt_tampering_is_rejected(installed, tmp_path, field):
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    stage, request, receipt, _sidecar = _execute(installed, tmp_path, fixture["input_payload"])
    changed = copy.deepcopy(receipt)
    descriptor = changed["registry_requests"][0]["descriptor"]
    descriptor[field] = [] if field in {"member_identities", "dimensions"} else {}
    descriptor["observed_descriptor_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in descriptor.items() if key != "observed_descriptor_identity"}
    )
    changed["receipt_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in changed.items() if key != "receipt_identity"}
    )
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v3(changed, request, stage)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"


def test_family_installed_process_cli(installed, tmp_path):
    fixture = json.loads(LEDGER.read_text())["cell_set_family_examples"][0]
    discovery, _registration = installed
    manifest = tmp_path / "site/scwbpyv2reference/operation-pack.json"
    stage = tmp_path / "cli-stage"
    stage.mkdir()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::family_probe",
        _inputs(),
        {"family": fixture["input_payload"]},
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    runtime_path = tmp_path / "runtime.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(manifest.parents[1]), str(ROOT / "src")))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pybench.operation_pack_v3_cli",
            "--manifest",
            str(manifest),
            "--request",
            str(request_path),
            "--stage-root",
            str(stage),
            "--receipt",
            str(receipt_path),
            "--python-runtime",
            str(runtime_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(receipt_path.read_text())
    validate_receipt_v3(receipt, request)
    assert (
        receipt["registry_requests"][0]["descriptor"]["family_identity"]
        == fixture["family_identity"]
    )
