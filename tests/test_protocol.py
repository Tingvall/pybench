import pytest

from pybench.protocol import ProtocolError, validate_receipt, validate_request


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
