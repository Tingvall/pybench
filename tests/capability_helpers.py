from __future__ import annotations

import json
from pathlib import Path

from pybench.bundle import open_bundle
from pybench.capability import STAGE_MARKER
from pybench.protocol import PATH_CONTRACT


def sqlite_allowance(
    output_name: str, output_id: str, output_kind: str, table: str, column: str
) -> dict:
    return {
        "output_name": output_name,
        "output_id": output_id,
        "output_kind": output_kind,
        "mutation_class": "sqlite_rows",
        "table": table,
        "key_scope": {"column": column, "values": [output_id]},
    }


def payload_allowance(output_name: str, output_id: str, output_kind: str, path: str) -> dict:
    return {
        "output_name": output_name,
        "output_id": output_id,
        "output_kind": output_kind,
        "mutation_class": "payload_tree",
        "path": path,
    }


def raw_write_bundle(root: Path, allowances: list[dict] | None = None):
    return open_bundle(
        root,
        access_mode="staging_write",
        capability={"write_allowlist": allowances or []},
    )


def v2_request(
    root: Path,
    *,
    op_name: str,
    inputs: dict,
    params: dict,
    outputs: dict,
    allowances: list[dict],
    run_id: str = "run_test",
) -> dict:
    capability = {
        "capability_id": f"cap_{run_id}",
        "mode": "staging_write",
        "session_id": "session_test",
        "run_id": run_id,
        "source_attestation": {
            "scheme": "scworkbench-scientific-state-v1",
            "content_sha256": "1" * 64,
        },
        "staging_identity": {
            "root_sha256": "2" * 64,
            "checkpoint_generation": "g_test",
        },
        "write_allowlist": allowances,
    }
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
    return {
        "message_type": "request",
        "protocol_version": "pybench-op-v2",
        "bundle_dir": str(root),
        "path_contract": dict(PATH_CONTRACT),
        "op_name": op_name,
        "run": {"session_id": "session_test", "run_id": run_id},
        "execution_capability": capability,
        "inputs": inputs,
        "params": params,
        "outputs": outputs,
    }


def read_only_request(
    root: Path,
    *,
    op_name: str,
    inputs: dict,
    params: dict | None = None,
    outputs: dict | None = None,
    run_id: str = "read_test",
) -> dict:
    capability = {
        "capability_id": f"cap_{run_id}",
        "mode": "read_only",
        "session_id": "session_test",
        "run_id": run_id,
        "source_attestation": {
            "scheme": "scworkbench-scientific-state-v1",
            "content_sha256": "1" * 64,
        },
        "staging_identity": {
            "root_sha256": "2" * 64,
            "checkpoint_generation": "read_only",
        },
        "write_allowlist": [],
    }
    return {
        "message_type": "request",
        "protocol_version": "pybench-op-v2",
        "bundle_dir": str(root),
        "path_contract": dict(PATH_CONTRACT),
        "op_name": op_name,
        "run": {"session_id": "session_test", "run_id": run_id},
        "execution_capability": capability,
        "inputs": inputs,
        "params": params or {},
        "outputs": outputs or {},
    }
