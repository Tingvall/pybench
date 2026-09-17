from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from pybench.operation_packs import OperationPackError, canonical_json
from pybench.operation_packs_v2 import _identity
from pybench.protocol_v4 import (
    TypedSliceBrokerClient,
    _read_inputs_v4,
    _validate_typed_slice_grants,
    partition_axis,
)

TOKEN = "a" * 64


def _axis(stage: Path, name: str, axis: str, ids: list[str]) -> dict:
    scheme = "scworkbench.cell_order.v1" if axis == "cells" else "scworkbench.feature_order.v1"
    value = {
        "schema": "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
        "axis": axis,
        "count": len(ids),
        "ids": ids,
        "order_identity": _identity(scheme, ids),
    }
    value["manifest_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
        value,
        "axis manifest excluding manifest_identity",
    )
    relative = f"typed_slice_axes/{name}/{axis}.json"
    path = stage / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(value).encode()
    path.write_bytes(raw)
    return {
        "axis": axis,
        "count": len(ids),
        "order_identity": value["order_identity"],
        "axis_manifest_identity": value["manifest_identity"],
        "relative_path": relative,
        "payload_manifest_identity": _identity(
            "scworkbench.operation_pack.payload_manifest.v2",
            {"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "registry read payload manifest",
        ),
    }


def _contract(stage: Path, *, discipline: str = "ordered_partition_exactly_once"):
    request = {
        "request_identity": _identity(
            "scworkbench.operation_pack.transition_request.v3",
            {"r": 1},
            "request excluding request_identity",
        ),
        "execution_capability": {
            "capability_identity": _identity("scworkbench.operation_pack.capability.v3", {"c": 1})
        },
        "operation_authentication": {
            "operation_definition_identity": _identity(
                "scworkbench.operation_pack.definition.v3", {"d": 1}
            )
        },
    }
    source = {
        "schema": "scworkbench.operation_pack.typed_slice_source_reference.v1",
        "input_name": "matrix",
        "input_reference": "matrix:m1",
        "source_kind": "bundle_matrix",
        "matrix_id": "m1",
        "input_descriptor_identity": _identity("fixture.descriptor", {"i": 1}),
        "input_content_identity": _identity("fixture.content", {"i": 1}),
        "transition_source_state_identity": _identity("fixture.state", {"i": 1}),
    }
    source["source_reference_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_source_reference.v1",
        source,
        "source reference excluding source_reference_identity",
    )
    grant = {
        "schema": "scworkbench.operation_pack.typed_slice_grant.v1",
        "name": "matrix_chunks",
        "input_name": "matrix",
        "kind": "matrix",
        "reader": "read_typed_slices",
        "input_descriptor_identity": source["input_descriptor_identity"],
        "input_content_identity": source["input_content_identity"],
        "source_reference": source,
        "supported_axes": ["cells"],
        "chunk_axis": "cells",
        "cells_axis": _axis(stage, "matrix_chunks", "cells", ["c2", "c1", "c3"]),
        "features_axis": _axis(stage, "matrix_chunks", "features", ["g2", "g1"]),
        "requested_chunk_size": 2,
        "call_discipline": discipline,
        "empty_selection_policy": "reject",
        "duplicate_policy": "reject",
        "max_rows": 2,
        "max_columns": 2,
        "max_elements": 4,
        "max_control_bytes": 1_000_000,
        "max_response_bytes": 1_000_000,
        "max_outstanding_responses": 1,
    }
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    return request, grant


def _response_axis(axis: str, ids: list[str]) -> bytes:
    scheme = "scworkbench.cell_order.v1" if axis == "cells" else "scworkbench.feature_order.v1"
    value = {
        "schema": "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
        "axis": axis,
        "count": len(ids),
        "ids": ids,
        "order_identity": _identity(scheme, ids),
    }
    value["manifest_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_axis_manifest.v1",
        value,
        "axis manifest excluding manifest_identity",
    )
    return canonical_json(value).encode()


def _serve(stage: Path, request: dict, grant: dict, calls: int, errors: list, *, corrupt=False):
    try:
        broker = stage / "broker"
        broker.mkdir(exist_ok=True)
        for _ in range(calls):
            control_path = broker / "request.json"
            deadline = time.monotonic() + 5
            while not control_path.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("control timeout")
                time.sleep(0.005)
            control = json.loads(control_path.read_text())
            assert control["request_identity"] == request["request_identity"]
            assert control["grant_identity"] == grant["grant_identity"]
            assert control["authorization_token"] == TOKEN
            assert control["control_identity"] == _identity(
                "scworkbench.operation_pack.typed_slice_control_request.v1",
                {key: value for key, value in control.items() if key != "control_identity"},
                "control request excluding control_identity",
            )
            features, cells = control["features"], control["cells"]
            entries = []
            lines = ["%%MatrixMarket matrix coordinate real general"]
            for column, _cell in enumerate(cells, start=1):
                for row, _feature in enumerate(features, start=1):
                    value = float(column * 10 + row)
                    entries.append({"row": row, "column": column, "value": value})
            lines.append(f"{len(features)} {len(cells)} {len(entries)}")
            lines.extend(f"{item['row']} {item['column']} {item['value']}" for item in entries)
            matrix = ("\n".join(lines) + "\n").encode()
            feature_bytes = _response_axis("features", features)
            cell_bytes = _response_axis("cells", cells)
            feature_axis = json.loads(feature_bytes)
            cell_axis = json.loads(cell_bytes)
            semantic = _identity(
                "scworkbench.operation_pack.matrix_slice.content.v1",
                {
                    "schema": "scworkbench.operation_pack.matrix_slice.content.v1",
                    "kind": "matrix",
                    "dimensions": [len(features), len(cells)],
                    "feature_id": features,
                    "cell_id": cells,
                    "entries": entries,
                },
            )
            result = {
                "schema": "scworkbench.operation_pack.typed_slice_result.v1",
                "source_reference_identity": {
                    key: grant["source_reference"]["source_reference_identity"][key]
                    for key in ("scheme", "sha256")
                },
                "grant_identity": {
                    key: grant["grant_identity"][key] for key in ("scheme", "sha256")
                },
                "ordinal": control["ordinal"],
                "feature_count": len(features),
                "feature_order_identity": feature_axis["order_identity"],
                "cell_count": len(cells),
                "cell_order_identity": cell_axis["order_identity"],
                "dimensions": [len(features), len(cells)],
                "semantic_content_identity": semantic,
            }
            result["slice_result_identity"] = _identity(
                "scworkbench.operation_pack.typed_slice_result.v1",
                result,
                "slice result excluding slice_result_identity",
            )
            if corrupt:
                semantic = _identity(
                    "scworkbench.operation_pack.matrix_slice.content.v1", {"bad": 1}
                )
            payloads = {
                "matrix.mtx": matrix,
                "features.json": feature_bytes,
                "cells.json": cell_bytes,
            }
            manifest = {
                "schema": "scworkbench.operation_pack.typed_slice_response.v1",
                "request_identity": request["request_identity"],
                "grant_identity": grant["grant_identity"],
                "source_reference_identity": grant["source_reference"]["source_reference_identity"],
                "ordinal": control["ordinal"],
                "dimensions": result["dimensions"],
                "nonzero_count": len(entries),
                "feature_axis_manifest_identity": feature_axis["manifest_identity"],
                "cell_axis_manifest_identity": cell_axis["manifest_identity"],
                "files": [
                    {
                        "role": role,
                        "path": name,
                        "size": len(payloads[name]),
                        "sha256": hashlib.sha256(payloads[name]).hexdigest(),
                    }
                    for role, name in (
                        ("matrix_market", "matrix.mtx"),
                        ("features_axis", "features.json"),
                        ("cells_axis", "cells.json"),
                    )
                ],
                "semantic_content_identity": semantic,
                "slice_result_identity": result["slice_result_identity"],
            }
            manifest["response_identity"] = _identity(
                "scworkbench.operation_pack.typed_slice_response.v1",
                manifest,
                "response excluding response_identity",
            )
            temporary = broker / "response.pending"
            temporary.mkdir()
            for name, raw in payloads.items():
                (temporary / name).write_bytes(raw)
            (temporary / "manifest.json").write_text(canonical_json(manifest))
            os.replace(temporary, broker / "response")
            if corrupt:
                return
            ack = broker / "acknowledgement.json"
            deadline = time.monotonic() + 5
            while not ack.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("ack timeout")
                time.sleep(0.005)
            acknowledgement = json.loads(ack.read_text())
            assert acknowledgement["request_identity"] == request["request_identity"]
            assert acknowledgement["grant_identity"] == grant["grant_identity"]
            assert acknowledgement["response_identity"] == manifest["response_identity"]
            assert acknowledgement["authorization_token"] == TOKEN
            assert acknowledgement["acknowledgement_identity"] == _identity(
                "scworkbench.operation_pack.typed_slice_acknowledgement.v1",
                {
                    key: value
                    for key, value in acknowledgement.items()
                    if key != "acknowledgement_identity"
                },
                "acknowledgement excluding acknowledgement_identity",
            )
            shutil.rmtree(broker / "response")
            ack.unlink()
            control_path.unlink()
    except BaseException as exc:  # pragma: no cover - surfaced in the parent assertion
        errors.append(exc)


def test_typed_slice_client_is_bounded_ordered_and_transport_neutral(tmp_path):
    request, grant = _contract(tmp_path)
    (tmp_path / "broker").mkdir()
    errors = []
    broker = threading.Thread(
        target=_serve, args=(tmp_path, request, grant, 2, errors), daemon=True
    )
    broker.start()
    client = TypedSliceBrokerClient(request, grant, tmp_path, authorization_token=TOKEN, timeout=5)
    first = client(features=["g2", "g1"], cells=["c2", "c1"])
    second = client(features=["g2", "g1"], cells=["c3"])
    broker.join(5)
    assert not broker.is_alive() and not errors
    assert first["values"] == [[11.0, 21.0], [12.0, 22.0]]
    assert second["cell_id"] == ["c3"]
    usage = client.usage()
    assert usage["coverage_status"] == "exact"
    assert usage["call_count"] == 2
    assert usage["concatenated_chunk_axis_order_identity"] == grant["cells_axis"]["order_identity"]
    assert "scope" not in usage["grant_identity"]
    assert "scope" not in usage["request_identity"]
    assert "scope" not in usage["capability_identity"]
    assert "scope" not in usage["operation_definition_identity"]
    assert all("scope" not in call["grant_identity"] for call in usage["calls"])
    assert all("scope" in call["slice_result_identity"] for call in usage["calls"])
    assert all("scope" in call["call_identity"] for call in usage["calls"])
    assert "scope" in usage["usage_identity"]
    assert not (tmp_path / "inputs/matrix/payload.json").exists()
    assert list((tmp_path / "broker").iterdir()) == []


def test_typed_slice_client_rejects_order_and_semantic_corruption(tmp_path):
    request, grant = _contract(tmp_path)
    (tmp_path / "broker").mkdir()
    client = TypedSliceBrokerClient(request, grant, tmp_path, authorization_token=TOKEN, timeout=2)
    with pytest.raises(OperationPackError, match="ordered exactly-once"):
        client(features=["g2", "g1"], cells=["c1"])
    errors = []
    broker = threading.Thread(
        target=_serve,
        args=(tmp_path, request, grant, 1, errors),
        kwargs={"corrupt": True},
        daemon=True,
    )
    broker.start()
    with pytest.raises(OperationPackError, match="response binding differs"):
        client(features=["g2", "g1"], cells=["c2", "c1"])
    broker.join(5)
    assert not errors


def test_partition_axis_is_pure_order_preserving_and_closed():
    assert partition_axis(["c2", "c1", "c3"], 2) == [["c2", "c1"], ["c3"]]
    assert partition_axis([], 2) == []
    with pytest.raises(OperationPackError):
        partition_axis(["c1", "c1"], 2)
    with pytest.raises(OperationPackError):
        partition_axis(["c1"], True)


def test_brokered_input_rejects_eager_whole_matrix_staging(tmp_path):
    request, grant = _contract(tmp_path)
    request["inputs"] = []
    request["typed_slice_grants"] = [grant]
    eager = tmp_path / "inputs/matrix/payload.json"
    eager.parent.mkdir(parents=True)
    eager.write_text("{}")
    with pytest.raises(OperationPackError, match="eagerly staged"):
        _read_inputs_v4(request, tmp_path)


def test_empty_nonchunk_axis_records_zero_call_usage(tmp_path):
    request, grant = _contract(tmp_path)
    grant["features_axis"] = _axis(tmp_path, "matrix_chunks", "features", [])
    grant["empty_selection_policy"] = "allow_zero_axis"
    grant["max_rows"] = 0
    grant["max_elements"] = 0
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_grant.v1",
        {key: value for key, value in grant.items() if key != "grant_identity"},
        "grant excluding grant_identity",
    )
    (tmp_path / "broker").mkdir()
    usage = TypedSliceBrokerClient(
        request, grant, tmp_path, authorization_token=TOKEN, timeout=2
    ).usage()
    assert usage["coverage_status"] == "empty"
    assert usage["calls"] == []
    assert usage["concatenated_chunk_axis_order_identity"] == _identity(
        "scworkbench.cell_order.v1", []
    )


def test_empty_axis_is_rejected_before_dispatch_when_grant_policy_rejects(tmp_path):
    request, grant = _contract(tmp_path)
    grant["features_axis"] = _axis(tmp_path, "matrix_chunks", "features", [])
    grant["max_rows"] = 0
    grant["max_elements"] = 0
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.typed_slice_grant.v1",
        {key: value for key, value in grant.items() if key != "grant_identity"},
        "grant excluding grant_identity",
    )
    request.update(
        inputs=[
            {
                "name": "matrix",
                "descriptor": {
                    "descriptor_identity": grant["input_descriptor_identity"],
                    "content_identity": grant["input_content_identity"],
                },
            }
        ],
        params={"chunk_size": 2},
        transition={
            "source_state_identity": grant["source_reference"]["transition_source_state_identity"]
        },
        typed_slice_grants=[grant],
    )
    operation = {
        "typed_slice_reads": [
            {
                "name": "matrix_chunks",
                "input": "matrix",
                "kind": "matrix",
                "reader": "read_typed_slices",
                "supported_axes": ["cells"],
                "chunk_axis": "cells",
                "chunk_size_parameter": "chunk_size",
                "call_discipline": "ordered_partition_exactly_once",
                "empty_selection_policy": "reject",
                "duplicate_policy": "reject",
            }
        ]
    }
    with pytest.raises(OperationPackError, match="rejects its empty selected axis"):
        _validate_typed_slice_grants(request, operation)


def test_independent_slices_record_overlap_without_repartition(tmp_path):
    request, grant = _contract(tmp_path, discipline="independent_slices")
    (tmp_path / "broker").mkdir()
    errors = []
    broker = threading.Thread(
        target=_serve, args=(tmp_path, request, grant, 2, errors), daemon=True
    )
    broker.start()
    client = TypedSliceBrokerClient(request, grant, tmp_path, authorization_token=TOKEN, timeout=5)
    client(features=["g2", "g1"], cells=["c2", "c1"])
    client(features=["g2", "g1"], cells=["c1", "c3"])
    broker.join(5)
    assert not broker.is_alive() and not errors
    usage = client.usage()
    assert usage["coverage_status"] == "independent"
    assert usage["unique_chunk_axis_count"] == 3
    assert usage["duplicate_chunk_axis_count"] == 1
