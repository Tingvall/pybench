from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from external_contracts import HISTORICAL_FORGE_SOURCE_ENV, configured_directory
from pybench.feature_set_registry import strict_snapshot_json, validate_snapshot
from pybench.operation_packs import OperationPackError, canonical_sha256
from pybench.operation_packs_v2 import (
    _identity,
    activate_operation_pack_v2,
    discover_operation_pack_v2,
    unload_operation_pack_v2,
    validate_manifest_v2,
)
from pybench.protocol_v3 import execute_operation_pack_v3, validate_receipt_v3, validate_request_v3
from v3_helpers import fixture_inputs, install_reference_fixture, make_request

ROOT = Path(__file__).parents[1]
SHARED_SHA = {
    "operation-pack-v2.schema.json": "e4ca57a6a40d87a62865aae43597646f5b6a3f5ca15dec65b89d163033acd8ec",
    "operation-pack-transition-request-v2.schema.json": "700665a2ba03a69c6df75f9640bf09b9be86b443c61060ffdf0381dcc24bcae5",
    "operation-pack-transition-receipt-v2.schema.json": "3e9f07c342fa929806e3747f36ef59f43838921b98907cf5d4ceeede1a1f0a15",
    "fixtures/operation-pack-v2/identities.json": "36f5f5c73b561420477a9f46c6a0fcad7e2121d85349859ceb3310db8342b52e",
}
LEDGER = json.loads((ROOT / "protocol/fixtures/operation-pack-v2/identities.json").read_text())
OP = "scwbpyv2reference::registry_probe"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    manifest = install_reference_fixture(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v2(manifest)
    registration = activate_operation_pack_v2(discovery)
    yield discovery, registration
    unload_operation_pack_v2("scwbpyv2reference")


def _inputs():
    inputs = fixture_inputs()
    inputs["matrix"].update(
        dimensions=[3, 2],
        feature_id=["feature_c", "feature_a", "feature_b"],
        values=[[1, 2], [3, 4], [5, 6]],
    )
    return inputs


def _case(name):
    item = next(item for item in LEDGER["feature_set_registry_examples"] if item["name"] == name)
    params = {
        "zero_selected_sets": {"namespace": "ns_missing"},
        "one_selected_set": {"namespace": "ns_alpha"},
        "many_selected_sets": {},
    }[name]
    return copy.deepcopy(item["snapshot"]), params


def _request(discovery, stage, name="many_selected_sets"):
    snapshot, params = _case(name)
    return make_request(
        discovery, stage, OP, _inputs(), params, registry_snapshots={"feature_sets": snapshot}
    )


def _snapshot_files(stage):
    return {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}


def test_shared_assets_match_forge_bytes_and_digests():
    forge = configured_directory(
        HISTORICAL_FORGE_SOURCE_ENV, purpose="historical Phase 3B Forge parity"
    )
    for relative, digest in SHARED_SHA.items():
        pybench = (ROOT / "protocol" / relative).read_bytes()
        assert pybench == (forge / "inst/protocol" / relative).read_bytes()
        assert hashlib.sha256(pybench).hexdigest() == digest


@pytest.mark.parametrize("name", ["zero_selected_sets", "one_selected_set", "many_selected_sets"])
def test_forge_snapshot_identities_are_exact(name):
    fixture = next(x for x in LEDGER["feature_set_registry_examples"] if x["name"] == name)
    snapshot = validate_snapshot(fixture["snapshot"])
    assert (
        _identity("scworkbench.feature_order.v1", snapshot["feature_universe"])
        == fixture["feature_order_identity"]
    )
    assert (
        _identity(
            "scworkbench.feature_set_registry.selection.v1",
            snapshot["selection"],
            "normalized selector binding",
        )
        == fixture["selection_identity"]
    )
    assert (
        _identity(
            "scworkbench.feature_set_registry.snapshot.v1",
            snapshot,
            "canonical matrix-bound registry snapshot",
        )
        == fixture["content_identity"]
    )
    assert (
        _identity(
            "scworkbench.feature_set_registry.source.v1",
            {key: snapshot[key] for key in ("matrix_id", "feature_universe", "sets")},
            "ordered registry source state",
        )
        == fixture["source_identity"]
    )
    assert (
        _identity(
            "scworkbench.provenance.ancestry.v1",
            {
                "matrix_input": "matrix",
                "matrix_content_identity": fixture["matrix_content_identity"],
                "selection_identity": fixture["selection_identity"],
            },
            "registry snapshot ancestry",
        )
        == fixture["ancestry_identity"]
    )


@pytest.mark.parametrize("name", ["zero_selected_sets", "one_selected_set", "many_selected_sets"])
def test_registry_read_executes_once_with_complete_receipt(installed, tmp_path, name):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage, name)
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    validate_receipt_v3(receipt, request, stage)
    assert len(receipt["registry_reads"]) == 1
    assert receipt["registry_reads"][0]["descriptor"]["use_count"] == 1
    assert (
        receipt["registry_reads"][0]["descriptor"]["content_identity"]
        == request["registry_reads"][0]["descriptor"]["content_identity"]
    )
    assert len(receipt["outputs"]) == 1
    assert receipt["mutation_manifest"]["entry_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["sets"][0]["memberships"][0].update(rank=2),
        lambda s: s["sets"][0]["memberships"][0].update(feature_id="foreign"),
        lambda s: s["sets"][0]["memberships"][1].update(feature_id="feature_c"),
        lambda s: s["sets"][0].update(namespace="wrong"),
        lambda s: s["sets"][0].update(matrix_id="other"),
        lambda s: s["sets"][0].update(source_id=42),
        lambda s: s["sets"].append(copy.deepcopy(s["sets"][0])),
        lambda s: s.update(feature_universe=["feature_a", "feature_b", "feature_c"]),
    ],
)
def test_malformed_registry_snapshot_rejected_before_dispatch(installed, tmp_path, mutation):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage, "one_selected_set")
    path = stage / "registry_reads/feature_sets/payload.json"
    snapshot = json.loads(path.read_text())
    mutation(snapshot)
    path.write_text(json.dumps(snapshot))
    before = _snapshot_files(stage)
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack_v3(request, discovery, stage)
    assert caught.value.code == "PYBENCH_INPUT_IDENTITY_MISMATCH"
    assert _snapshot_files(stage) == before


@pytest.mark.parametrize("mode", ["missing", "duplicate", "argument", "extra"])
def test_registry_reader_capability_is_closed(installed, tmp_path, monkeypatch, mode):
    discovery, registration = installed
    original = registration.handlers[OP]

    def handler(readers, writers, params, context):
        if mode == "missing":
            readers["matrix"]()
            writers.write_terminal_primitive(
                "summary", {"kind": "obs_table", "dimensions": [1, 1], "rows": [{"set_count": 0}]}
            )
        elif mode == "duplicate":
            readers["feature_sets"]()
            readers["feature_sets"]()
        elif mode == "argument":
            readers["feature_sets"]("unexpected")
        else:
            readers["undeclared"]()
        if mode == "missing":
            return
        original(readers, writers, params, context)

    monkeypatch.setitem(registration.handlers, OP, handler)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    before = _snapshot_files(stage)
    with pytest.raises((OperationPackError, KeyError)):
        execute_operation_pack_v3(request, discovery, stage)
    assert _snapshot_files(stage) == before


def test_private_reader_returns_a_copy(installed, tmp_path, monkeypatch):
    discovery, registration = installed

    def handler(readers, writers, params, context):
        first = readers["feature_sets"]()
        first["sets"].clear()
        readers["matrix"]()
        writers.write_terminal_primitive(
            "summary", {"kind": "obs_table", "dimensions": [1, 1], "rows": [{"set_count": 0}]}
        )

    monkeypatch.setitem(registration.handlers, OP, handler)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    assert receipt["registry_reads"][0]["descriptor"]["use_count"] == 1
    assert (
        len(json.loads((stage / "registry_reads/feature_sets/payload.json").read_text())["sets"])
        == 2
    )


def test_registry_payload_tampering_and_interrupted_stage_roll_back(
    installed, tmp_path, monkeypatch
):
    discovery, registration = installed
    original = registration.handlers[OP]
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    before = _snapshot_files(stage)

    def tamper(readers, writers, params, context):
        original(readers, writers, params, context)
        (stage / "registry_reads/feature_sets/payload.json").write_text("{}")

    monkeypatch.setitem(registration.handlers, OP, tamper)
    with pytest.raises(OperationPackError):
        execute_operation_pack_v3(request, discovery, stage)
    assert _snapshot_files(stage) == before


def test_forged_receipt_registry_observation_is_rejected(installed, tmp_path):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    forged = copy.deepcopy(receipt)
    forged["registry_reads"][0]["descriptor"]["content_identity"]["sha256"] = "0" * 64
    descriptor = forged["registry_reads"][0]["descriptor"]
    descriptor["observed_descriptor_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in descriptor.items() if key != "observed_descriptor_identity"}
    )
    forged["receipt_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in forged.items() if key != "receipt_identity"}
    )
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v3(forged, request, stage)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(OperationPackError):
        strict_snapshot_json(b'{"schema":"a","schema":"b"}')


def test_interrupted_dispatch_rolls_back_written_output(installed, tmp_path):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    before = _snapshot_files(stage)

    def interrupt(event):
        if event == "after_handler":
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        execute_operation_pack_v3(request, discovery, stage, fault_hook=interrupt)
    assert _snapshot_files(stage) == before


@pytest.mark.parametrize("mode", ["missing", "duplicate", "extra"])
def test_receipt_registry_read_closure_is_exact(installed, tmp_path, mode):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    changed = copy.deepcopy(receipt)
    records = changed["registry_reads"]
    if mode == "missing":
        records.clear()
    elif mode == "duplicate":
        records.append(copy.deepcopy(records[0]))
    else:
        extra = copy.deepcopy(records[0])
        extra["name"] = "extra"
        records.append(extra)
    changed["receipt_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in changed.items() if key != "receipt_identity"}
    )
    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v3(changed, request, stage)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"


def test_selector_parameter_binding_must_match_snapshot(installed, tmp_path):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage, "one_selected_set")
    request["params"]["namespace"] = "ns_beta"
    request["request_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in request.items() if key != "request_identity"}
    )
    before = _snapshot_files(stage)
    with pytest.raises(OperationPackError) as caught:
        execute_operation_pack_v3(request, discovery, stage)
    assert caught.value.code == "PYBENCH_INPUT_IDENTITY_MISMATCH"
    assert _snapshot_files(stage) == before


def test_rehashed_wrong_matrix_grant_is_rejected(installed, tmp_path):
    discovery, _ = installed
    stage = tmp_path / "stage"
    stage.mkdir()
    request = _request(discovery, stage)
    read = request["registry_reads"][0]["descriptor"]
    read["matrix_content_identity"]["sha256"] = "0" * 64
    read["descriptor_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in read.items() if key != "descriptor_identity"}
    )
    capability = request["execution_capability"]
    capability["reader_grants"][-1]["descriptor_identity"] = read["descriptor_identity"]
    capability["capability_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in capability.items() if key != "capability_identity"}
    )
    request["request_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in request.items() if key != "request_identity"}
    )
    with pytest.raises(OperationPackError) as caught:
        validate_request_v3(request, discovery)
    assert caught.value.code == "PYBENCH_INPUT_IDENTITY_MISMATCH"


def test_manifest_registry_selector_type_and_name_collisions(installed):
    discovery, _ = installed
    manifest = copy.deepcopy(discovery.manifest)
    operation = next(item for item in manifest["operations"] if item["qualified_name"] == OP)
    operation["registry_reads"][0]["namespace_parameter"] = "namespaces"
    with pytest.raises(OperationPackError):
        validate_manifest_v2(manifest)
    operation["registry_reads"][0]["namespace_parameter"] = "namespace"
    operation["registry_reads"][0]["name"] = "matrix"
    with pytest.raises(OperationPackError):
        validate_manifest_v2(manifest)
