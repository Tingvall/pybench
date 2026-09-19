from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v3 import validate_manifest_v3
from pybench.protocol_v4 import validate_receipt_v4, validate_request_v4

FIXTURE = (
    Path(__file__).parents[1] / "protocol" / "fixtures" / "operation-pack-v3" / "phase4e-sparse-knn"
)


def _fixture():
    request = json.loads((FIXTURE / "request.json").read_text(encoding="utf-8"))
    receipt = json.loads((FIXTURE / "receipt.json").read_text(encoding="utf-8"))
    manifest = validate_manifest_v3(FIXTURE / "operation-pack.json", expected_backend=None)
    authentication = request["operation_authentication"]
    discovery = SimpleNamespace(
        manifest=manifest,
        identity={
            "pack_identity": authentication["pack_identity"],
            "manifest_identity": authentication["manifest_identity"],
            "operation_definition_identities": {
                authentication["name"]: authentication["operation_definition_identity"]
            },
        },
        manifest_source_identity=authentication["manifest_source_identity"],
        distribution_identity=authentication["distribution_identity"],
        entry_point_identity=authentication["entry_point"]["identity"],
    )
    return request, receipt, discovery


def _reidentify(value, field):
    previous = value.pop(field)
    value[field] = _identity(previous["scheme"], value, previous.get("scope"))


def test_r_phase4e_fixture_is_accepted_byte_for_byte_by_python():
    request, receipt, discovery = _fixture()
    assert (
        validate_request_v4(request, discovery)["request_identity"] == request["request_identity"]
    )
    assert (
        validate_receipt_v4(receipt, request, discovery)["receipt_identity"]
        == receipt["receipt_identity"]
    )


def test_phase4e_request_rejects_typed_space_and_sparse_grant_mutations():
    request, _, discovery = _fixture()

    stale_axis = copy.deepcopy(request)
    stale_axis["typed_space_grants"][0]["cells_axis"]["count"] += 1
    with pytest.raises(OperationPackError) as caught:
        validate_request_v4(stale_axis, discovery)
    assert caught.value.code == "PYBENCH_CAPABILITY_MISMATCH"

    stale_sparse = copy.deepcopy(request)
    sparse = stale_sparse["outputs"][0]["descriptor"]["sparse_membership_grant"]
    sparse["candidate_count"] += 1
    with pytest.raises(OperationPackError) as caught:
        validate_request_v4(stale_sparse, discovery)
    assert caught.value.code == "PYBENCH_CAPABILITY_MISMATCH"


def test_phase4e_receipt_rejects_self_consistent_cross_output_disagreement():
    request, receipt, discovery = _fixture()
    changed = copy.deepcopy(receipt)
    output = next(item for item in changed["outputs"] if item["name"] == "neighbor_distance")
    descriptor = output["descriptor"]
    observation = descriptor["observed_sparse_membership"]
    observation["edge_ledger"][0]["distance"] += 0.5
    _reidentify(observation, "observation_identity")
    _reidentify(descriptor, "observed_descriptor_identity")
    _reidentify(changed, "receipt_identity")

    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v4(changed, request, discovery)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"


def test_phase4e_receipt_rejects_typed_space_usage_mutation():
    request, receipt, discovery = _fixture()
    changed = copy.deepcopy(receipt)
    usage = changed["typed_space_usage"][0]
    usage["coverage_status"] = "incomplete"
    _reidentify(usage, "usage_identity")
    _reidentify(changed, "receipt_identity")

    with pytest.raises(OperationPackError) as caught:
        validate_receipt_v4(changed, request, discovery)
    assert caught.value.code == "PYBENCH_RECEIPT_MISMATCH"
