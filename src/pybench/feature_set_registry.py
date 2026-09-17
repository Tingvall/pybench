from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from .operation_packs import OperationPackError
from .operation_packs_v2 import _identity

_SNAPSHOT_FIELDS = {"schema", "matrix_id", "feature_universe", "selection", "sets"}
_SELECTION_FIELDS = {"namespaces", "feature_sets", "no_selector_policy", "empty_match_policy"}
_SET_FIELDS = {
    "feature_set",
    "matrix_id",
    "n_features",
    "set_id",
    "namespace",
    "organism",
    "source_id",
    "source_version",
    "origin",
    "recipe_json",
    "is_readonly",
    "created_at",
    "updated_at",
    "created_by",
    "description",
    "memberships",
}
_NULLABLE_METADATA = _SET_FIELDS - {
    "feature_set",
    "matrix_id",
    "n_features",
    "is_readonly",
    "memberships",
}
_SELECTOR_FIELDS = ("namespace_parameter", "namespaces_parameter", "feature_sets_parameter")


def _invalid(message: str) -> None:
    raise OperationPackError("PYBENCH_INPUT_IDENTITY_MISMATCH", message, phase="resolution")


def _exact(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        _invalid(f"Feature-set registry {label} has an invalid shape")


def _strings(value: Any, label: str, *, ordered: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        _invalid(f"Feature-set registry {label} must be a string array")
    if len(value) != len(set(value)) or (ordered and value != sorted(value, key=str.encode)):
        _invalid(f"Feature-set registry {label} is duplicated or unordered")
    return value


def _integer(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value == int(value)
        and -(2**31) <= value < 2**31
    )


def _scoped(scheme: str, scope: str, value: Any) -> dict[str, str]:
    return _identity(scheme, value, scope)


def strict_snapshot_json(raw: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                _invalid("Feature-set registry JSON contains duplicate keys")
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _invalid(f"Feature-set registry JSON is unreadable: {exc}")
    return value


def normalized_selection(binding: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    def selected(field: str, scalar: bool = False) -> list[str]:
        parameter = binding.get(field)
        if parameter is None:
            return []
        raw = params.get(parameter)
        if raw is None:
            return []
        if scalar:
            if not isinstance(raw, str):
                _invalid(f"Registry selector {parameter!r} must be a string")
            values = [raw]
        else:
            if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
                _invalid(f"Registry selector {parameter!r} must be a string array")
            values = raw
        values = [item for item in values if item]
        if len(values) != len(set(values)):
            _invalid(f"Registry selector {parameter!r} contains duplicates")
        return values

    namespaces = selected("namespace_parameter", scalar=True) + selected("namespaces_parameter")
    return {
        "namespaces": sorted(set(namespaces), key=str.encode),
        "feature_sets": sorted(selected("feature_sets_parameter"), key=str.encode),
        "no_selector_policy": binding["no_selector_policy"],
        "empty_match_policy": binding["empty_match_policy"],
    }


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    _exact(snapshot, _SNAPSHOT_FIELDS, "snapshot")
    if snapshot["schema"] != "scworkbench.feature_set_registry.snapshot.v1":
        _invalid("Feature-set registry schema is invalid")
    matrix_id = snapshot["matrix_id"]
    if not isinstance(matrix_id, str) or not matrix_id:
        _invalid("Feature-set registry matrix binding is invalid")
    universe = _strings(snapshot["feature_universe"], "feature universe")
    _exact(snapshot["selection"], _SELECTION_FIELDS, "selection")
    selection = snapshot["selection"]
    namespaces = _strings(selection["namespaces"], "namespaces", ordered=True)
    feature_sets = _strings(selection["feature_sets"], "selected sets", ordered=True)
    if selection["no_selector_policy"] != "all_namespaces" or selection[
        "empty_match_policy"
    ] not in ("allow", "error"):
        _invalid("Feature-set registry selection policy is invalid")
    sets = snapshot["sets"]
    if not isinstance(sets, list):
        _invalid("Feature-set registry sets must be an array")
    seen_names: set[str] = set()
    keys: list[tuple[bytes, bytes]] = []
    for row in sets:
        _exact(row, _SET_FIELDS, "set")
        name = row["feature_set"]
        if not isinstance(name, str) or not name or name in seen_names:
            _invalid("Feature-set registry set names are invalid or duplicated")
        seen_names.add(name)
        if row["matrix_id"] != matrix_id:
            _invalid("Feature-set registry row has a foreign matrix")
        if not _integer(row["n_features"]) or row["n_features"] < 0:
            _invalid("Feature-set registry set size is invalid")
        if not _integer(row["is_readonly"]) or row["is_readonly"] not in (0, 1):
            _invalid("Feature-set registry read-only metadata is invalid")
        if any(
            row[key] is not None and not isinstance(row[key], str) for key in _NULLABLE_METADATA
        ):
            _invalid("Feature-set registry metadata is invalid")
        namespace = row["namespace"] or ""
        if namespaces and namespace not in namespaces:
            _invalid("Feature-set registry row is outside the selected namespace")
        if feature_sets and name not in feature_sets:
            _invalid("Feature-set registry row is outside the selected set names")
        keys.append((namespace.encode("utf-8"), name.encode("utf-8")))
        members = row["memberships"]
        if not isinstance(members, list) or len(members) != row["n_features"]:
            _invalid("Feature-set registry membership count is invalid")
        ids: list[str] = []
        for rank, member in enumerate(members, start=1):
            _exact(member, {"feature_id", "rank"}, "membership")
            if not isinstance(member["feature_id"], str) or not member["feature_id"]:
                _invalid("Feature-set registry membership identifier is invalid")
            if not _integer(member["rank"]) or member["rank"] != rank:
                _invalid("Feature-set registry stored ranks are noncontiguous")
            ids.append(member["feature_id"])
        if len(ids) != len(set(ids)) or not set(ids) <= set(universe):
            _invalid("Feature-set registry membership is duplicated or foreign")
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _invalid("Feature-set registry rows are noncanonical")
    if not sets and selection["empty_match_policy"] == "error":
        _invalid("No feature sets match the authenticated registry selectors")
    return snapshot


def validate_registry_read(
    raw: bytes,
    name: str,
    descriptor: Mapping[str, Any],
    declaration: Mapping[str, Any],
    params: Mapping[str, Any],
    matrix_descriptor: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot = validate_snapshot(strict_snapshot_json(raw))
    binding = {
        key: declaration.get(key)
        for key in (*_SELECTOR_FIELDS, "no_selector_policy", "empty_match_policy")
    }
    selection = normalized_selection(binding, params)
    expected = {
        "selector_binding": binding,
        "matrix_input": declaration["matrix_input"],
        "matrix_content_identity": matrix_descriptor["content_identity"],
        "feature_order_identity": matrix_descriptor["feature_order_identity"],
        "matrix_id": snapshot["matrix_id"],
        "dimensions": [len(snapshot["sets"]), sum(len(x["memberships"]) for x in snapshot["sets"])],
        "selection_identity": _scoped(
            "scworkbench.feature_set_registry.selection.v1",
            "normalized selector binding",
            selection,
        ),
        "content_identity": _scoped(
            "scworkbench.feature_set_registry.snapshot.v1",
            "canonical matrix-bound registry snapshot",
            snapshot,
        ),
        "source_identity": _scoped(
            "scworkbench.feature_set_registry.source.v1",
            "ordered registry source state",
            {key: snapshot[key] for key in ("matrix_id", "feature_universe", "sets")},
        ),
    }
    expected["ancestry_identity"] = _scoped(
        "scworkbench.provenance.ancestry.v1",
        "registry snapshot ancestry",
        {
            "matrix_input": declaration["matrix_input"],
            "matrix_content_identity": matrix_descriptor["content_identity"],
            "selection_identity": expected["selection_identity"],
        },
    )
    manifest = {
        "path": f"registry_reads/{name}/payload.json",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    expected["payload_manifest_identity"] = _scoped(
        "scworkbench.operation_pack.payload_manifest.v2", "registry read payload manifest", manifest
    )
    if snapshot["selection"] != selection:
        _invalid("Feature-set registry selector differs from declared parameters")
    if (
        _identity("scworkbench.feature_order.v1", snapshot["feature_universe"])
        != matrix_descriptor["feature_order_identity"]
    ):
        _invalid("Feature-set registry universe differs from bound matrix")
    for key, value in expected.items():
        if descriptor[key] != value:
            _invalid(f"Feature-set registry {key} differs from its grant")
    if (
        descriptor["category"] != "authenticated_core_registry_snapshot"
        or descriptor["kind"] != "feature_set_registry"
        or descriptor["reader"] != "read_feature_set_registry"
    ):
        _invalid("Feature-set registry grant type is invalid")
    if descriptor["descriptor_identity"] != _scoped(
        "scworkbench.operation_pack.registry_read_descriptor.v2",
        "descriptor excluding descriptor_identity",
        {key: value for key, value in descriptor.items() if key != "descriptor_identity"},
    ):
        _invalid("Feature-set registry descriptor identity is invalid")
    return snapshot
