from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .operation_packs import OperationPackError, canonical_json
from .operation_packs_v2 import _identity


def _invalid(message: str) -> None:
    raise OperationPackError("PYBENCH_INPUT_IDENTITY_MISMATCH", message, phase="resolution")


def _exact(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _invalid(f"Authenticated core {label} has an invalid shape")
    return value


def _strings(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        _invalid(f"Authenticated core {label} must contain unique nonempty strings")
    return value


def _integer(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value == int(value)
        and 0 <= value < 2**31
    )


def _plain(value: Mapping[str, Any]) -> dict[str, str]:
    return {"scheme": value["scheme"], "sha256": value["sha256"]}


def _strict_json(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _invalid(f"Authenticated core snapshot repeats object key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except OperationPackError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        _invalid(f"Authenticated core snapshot is unreadable: {exc}")
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        _invalid("Authenticated core snapshot is not canonical JSON")
    return value


def _payload_identity(raw: bytes, name: str) -> dict[str, str]:
    return _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {
            "path": f"registry_reads/{name}/payload.json",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "registry read payload manifest",
    )


def _validate_descriptor_identity(descriptor: Mapping[str, Any]) -> None:
    expected = _identity(
        "scworkbench.operation_pack.registry_read_descriptor.v3",
        {key: value for key, value in descriptor.items() if key != "descriptor_identity"},
        "descriptor excluding descriptor_identity",
    )
    if descriptor.get("descriptor_identity") != expected:
        _invalid("Authenticated core descriptor identity differs")


def _validate_session_cell_axis(
    snapshot: dict[str, Any],
    name: str,
    descriptor: Mapping[str, Any],
    declaration: Mapping[str, Any],
    request: Mapping[str, Any],
    raw: bytes,
) -> dict[str, Any]:
    _exact(
        snapshot,
        {
            "schema",
            "selection_policy",
            "coverage_policy",
            "session_id",
            "run_id",
            "checkpoint_generation",
            "transition_source_state_identity",
            "cell_count",
            "cell_id",
            "cell_order_identity",
        },
        "session-cell snapshot",
    )
    cells = _strings(snapshot["cell_id"], "session-cell axis")
    transition = request["transition"]
    expected_order = _identity("scworkbench.cell_order.v1", cells)
    if (
        snapshot["schema"] != "scworkbench.session_cell_axis.snapshot.v1"
        or snapshot["selection_policy"] != declaration["selection_policy"]
        or snapshot["coverage_policy"] != declaration["coverage_policy"]
        or snapshot["session_id"] != transition["session_id"]
        or snapshot["run_id"] != transition["run_id"]
        or snapshot["checkpoint_generation"] != transition["checkpoint_generation"]
        or snapshot["transition_source_state_identity"] != transition["source_state_identity"]
        or not _integer(snapshot["cell_count"])
        or snapshot["cell_count"] != len(cells)
        or snapshot["cell_order_identity"] != expected_order
    ):
        _invalid("Session-cell snapshot differs from its transition or declaration")
    selection_identity = _identity(
        "scworkbench.session_cell_axis.selection.v1",
        {
            "selection_policy": snapshot["selection_policy"],
            "coverage_policy": snapshot["coverage_policy"],
            "cell_id": cells,
        },
        "ordered effective-session selection",
    )
    content_identity = _identity(
        snapshot["schema"], snapshot, "canonical session cell-axis snapshot"
    )
    source_identity = _identity(
        "scworkbench.session_cell_axis.source.v1",
        {
            "session_id": snapshot["session_id"],
            "run_id": snapshot["run_id"],
            "checkpoint_generation": snapshot["checkpoint_generation"],
            "transition_source_state_identity": snapshot["transition_source_state_identity"],
            "cell_order_identity": snapshot["cell_order_identity"],
        },
        "session cell-axis source state",
    )
    ancestry_identity = _identity(
        "scworkbench.provenance.ancestry.v1",
        {
            "source_identity": source_identity,
            "selection_identity": selection_identity,
        },
        "session cell-axis ancestry",
    )
    expected = {
        "category": declaration["category"],
        "kind": declaration["kind"],
        "reader": declaration["reader"],
        "selection_policy": declaration["selection_policy"],
        "coverage_policy": declaration["coverage_policy"],
        "session_id": snapshot["session_id"],
        "run_id": snapshot["run_id"],
        "checkpoint_generation": snapshot["checkpoint_generation"],
        "transition_source_state_identity": snapshot["transition_source_state_identity"],
        "dimensions": [len(cells)],
        "cell_order_identity": expected_order,
        "selection_identity": selection_identity,
        "content_identity": content_identity,
        "source_identity": source_identity,
        "ancestry_identity": ancestry_identity,
        "payload_manifest_identity": _payload_identity(raw, name),
    }
    for key, value in expected.items():
        if descriptor.get(key) != value:
            _invalid(f"Session-cell descriptor {key} differs from its grant")
    _validate_descriptor_identity(descriptor)
    return snapshot


def _validate_matrix_feature_annotations(
    snapshot: dict[str, Any],
    name: str,
    descriptor: Mapping[str, Any],
    declaration: Mapping[str, Any],
    request: Mapping[str, Any],
    raw: bytes,
) -> dict[str, Any]:
    _exact(
        snapshot,
        {
            "schema",
            "matrix_input",
            "matrix_id",
            "input_descriptor_identity",
            "input_content_identity",
            "feature_count",
            "feature_order_identity",
            "fields",
            "rows",
        },
        "matrix-feature-annotation snapshot",
    )
    input_descriptors = {item["name"]: item["descriptor"] for item in request["inputs"]}
    matrix_input = declaration["matrix_input"]
    matrix = input_descriptors.get(matrix_input)
    if matrix is None:
        _invalid("Matrix-feature annotation input is absent")
    fields = _strings(snapshot["fields"], "annotation fields")
    rows = snapshot["rows"]
    if not isinstance(rows, list):
        _invalid("Matrix-feature annotation rows are invalid")
    feature_ids: list[str] = []
    for row in rows:
        _exact(row, {"feature_id", "values"}, "matrix-feature annotation row")
        feature_id = row["feature_id"]
        if not isinstance(feature_id, str) or not feature_id:
            _invalid("Matrix-feature annotation feature identifier is invalid")
        _exact(row["values"], set(fields), "matrix-feature annotation values")
        if any(
            value is not None and not isinstance(value, str) for value in row["values"].values()
        ):
            _invalid("Matrix-feature annotation value is invalid")
        feature_ids.append(feature_id)
    expected_order = _identity("scworkbench.feature_order.v1", feature_ids)
    if (
        snapshot["schema"] != "scworkbench.matrix_feature_annotations.snapshot.v1"
        or snapshot["matrix_input"] != matrix_input
        or not isinstance(snapshot["matrix_id"], str)
        or not snapshot["matrix_id"]
        or snapshot["input_descriptor_identity"] != _plain(matrix["descriptor_identity"])
        or snapshot["input_content_identity"] != matrix["content_identity"]
        or snapshot["feature_order_identity"] != matrix["feature_order_identity"]
        or not _integer(snapshot["feature_count"])
        or snapshot["feature_count"] != len(rows)
        or len(feature_ids) != len(set(feature_ids))
        or snapshot["feature_order_identity"] != expected_order
        or fields != declaration["fields"]
        or fields != ["feature_type"]
    ):
        _invalid("Matrix-feature annotation snapshot differs from its matrix or declaration")
    source_identity = _identity(
        "scworkbench.matrix_feature_annotations.source.v1",
        {
            "matrix_id": snapshot["matrix_id"],
            "fields": fields,
            "rows": rows,
        },
        "ordered matrix feature annotations",
    )
    content_identity = _identity(
        snapshot["schema"], snapshot, "canonical matrix feature annotations"
    )
    ancestry_identity = _identity(
        "scworkbench.provenance.ancestry.v1",
        {
            "input_descriptor_identity": snapshot["input_descriptor_identity"],
            "input_content_identity": snapshot["input_content_identity"],
            "source_identity": source_identity,
        },
        "matrix feature annotation ancestry",
    )
    expected = {
        "category": declaration["category"],
        "kind": declaration["kind"],
        "reader": declaration["reader"],
        "matrix_input": matrix_input,
        "fields": fields,
        "row_order": declaration["row_order"],
        "missing_annotation_policy": declaration["missing_annotation_policy"],
        "matrix_id": snapshot["matrix_id"],
        "dimensions": [len(rows), len(fields)],
        "input_descriptor_identity": snapshot["input_descriptor_identity"],
        "input_content_identity": snapshot["input_content_identity"],
        "feature_order_identity": expected_order,
        "content_identity": content_identity,
        "source_identity": source_identity,
        "ancestry_identity": ancestry_identity,
        "payload_manifest_identity": _payload_identity(raw, name),
    }
    for key, value in expected.items():
        if descriptor.get(key) != value:
            _invalid(f"Matrix-feature annotation descriptor {key} differs from its grant")
    _validate_descriptor_identity(descriptor)
    return snapshot


def validate_authenticated_core_read(
    raw: bytes,
    name: str,
    descriptor: Mapping[str, Any],
    declaration: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = _strict_json(raw)
    kind = declaration.get("kind")
    if kind == "session_cell_axis":
        return _validate_session_cell_axis(snapshot, name, descriptor, declaration, request, raw)
    if kind == "matrix_feature_annotations":
        return _validate_matrix_feature_annotations(
            snapshot, name, descriptor, declaration, request, raw
        )
    _invalid("Authenticated core reader kind is unsupported")
