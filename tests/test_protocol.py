import pytest

from pybench.ops import py_protocol_hello
from pybench.protocol import PATH_CONTRACT, ProtocolError, validate_receipt, validate_request


def _valid_request():
    return {
        "message_type": "request",
        "protocol_version": "pybench-op-v2",
        "path_contract": dict(PATH_CONTRACT),
        "bundle_dir": "/tmp/bundle",
        "op_name": "demo",
        "run": {"session_id": "session_1", "run_id": "run_1"},
        "execution_capability": {
            "capability_id": "cap_1",
            "mode": "read_only",
            "session_id": "session_1",
            "run_id": "run_1",
            "source_attestation": {
                "scheme": "scworkbench-scientific-state-v1",
                "content_sha256": "1" * 64,
            },
            "staging_identity": {
                "root_sha256": "2" * 64,
                "checkpoint_generation": "read",
            },
            "write_allowlist": [],
        },
        "inputs": {},
        "params": {},
        "outputs": {},
    }


def test_validate_request_rejects_unsupported_protocol_version():
    req = {
        "protocol_version": "pybench-op-v0",
        "bundle_dir": "/tmp/bundle",
        "op_name": "demo",
        "run": {"run_id": "run_1"},
        "inputs": {},
        "params": {},
        "outputs": {},
    }

    with pytest.raises(ProtocolError, match="unsupported protocol_version: 'pybench-op-v0'"):
        validate_request(req)


def test_validate_receipt_rejects_unsupported_protocol_version():
    receipt = {
        "protocol_version": "pybench-op-v0",
        "status": "ok",
        "op_name": "demo",
        "run_id": "run_1",
        "outputs": {},
    }

    with pytest.raises(
        ProtocolError,
        match="unsupported receipt protocol_version: 'pybench-op-v0'",
    ):
        validate_receipt(receipt)


@pytest.mark.parametrize(
    "path_contract",
    [
        None,
        {},
        {**PATH_CONTRACT, "version": "scworkbench-bundle-path-v0"},
        {**PATH_CONTRACT, "external_inputs": "implicit"},
        {**PATH_CONTRACT, "extra": "not-exact"},
    ],
)
def test_validate_request_requires_exact_path_contract(path_contract):
    req = _valid_request()
    if path_contract is None:
        req.pop("path_contract")
    else:
        req["path_contract"] = path_contract
    with pytest.raises(ProtocolError, match="path_contract"):
        validate_request(req)


def test_operation_rejects_path_contract_before_bundle_open(monkeypatch):
    req = _valid_request()
    req.pop("path_contract")
    opened = False

    def forbidden_open(_path):
        nonlocal opened
        opened = True
        raise AssertionError("bundle open must not be reached")

    monkeypatch.setattr(py_protocol_hello, "open_bundle_for_request", forbidden_open)
    with pytest.raises(ProtocolError, match="path_contract"):
        py_protocol_hello._run(req)
    assert opened is False
