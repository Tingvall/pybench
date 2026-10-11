"""Closed graph ABI4 grant and compact observation checks.

These checks authenticate protocol self-identities and declaration linkage.
They do not grant retained-source authority or replace core's staged-row audit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

from .operation_packs import OperationPackError, canonical_json, protocol_asset
from .operation_packs_v2 import _identity

_COMPONENTS = (
    "header",
    "nodes",
    "edges",
    "incidences",
    "attribute_fields",
    "attribute_values",
    "bindings",
    "binding_mappings",
)
_REQUEST = json.loads(
    protocol_asset("operation-pack-transition-request-v4.schema.json").read_text()
)
_RECEIPT = json.loads(
    protocol_asset("operation-pack-transition-receipt-v4.schema.json").read_text()
)


def _fail(reason: str) -> None:
    raise OperationPackError("PYBENCH_GRAPH_CAPABILITY_MISMATCH", reason, phase="staging")


def _shape(value: Any, definition: str, schema: Mapping[str, Any]) -> None:
    validator = Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"})
    errors = list(validator.iter_errors(value))
    if errors:
        _fail(f"{definition} schema mismatch: {errors[0].message}")


def _self_identity(value: Mapping[str, Any], field: str) -> None:
    claimed = value[field]
    payload = {key: child for key, child in value.items() if key != field}
    expected = _identity(claimed["scheme"], payload, claimed.get("scope"))
    if canonical_json(expected) != canonical_json(claimed):
        _fail(f"{field} self-identity differs")


def validate_graph_write_grant(
    grant: Mapping[str, Any],
    *,
    transition_identity: Mapping[str, Any],
    input_kinds: Mapping[str, str],
    output_names: Sequence[str],
) -> None:
    """Check exact issued grant closure before exposing a graph writer."""

    _shape(grant, "graph_write_grant", _REQUEST)
    _self_identity(grant, "grant_identity")
    if grant["output_name"] not in output_names:
        _fail("graph grant names an undeclared output")
    if grant["binding_policy"] == "forbidden" and grant["authorized_source_axes"]:
        _fail("unbound graph grant carries source-axis authority")
    if grant["binding_policy"] == "required" and not grant["authorized_source_axes"]:
        _fail("required bound graph has no source-axis authority")
    seen: set[str] = set()
    for axis in grant["authorized_source_axes"]:
        _self_identity(axis, "authority_identity")
        if int(axis["axis_count"]) > 9223372036854775807:
            _fail("source axis count exceeds signed int64")
        input_kind = input_kinds.get(axis["input_name"])
        if input_kind is None or axis["output_name"] != grant["output_name"]:
            _fail("axis authority names undeclared input or wrong output")
        if canonical_json(axis["transition_identity"]) != canonical_json(transition_identity):
            _fail("axis authority belongs to a different transition")
        source_kind = axis["source_ref"]["kind"]
        if source_kind == "retained_cell_axis" and axis["axis_kind"] != "cell":
            _fail("feature binding cannot claim a retained cell axis")
        if source_kind == "matrix":
            if axis["source_binding_id"] is None:
                if input_kind != "matrix":
                    _fail("direct matrix axis authority requires a matrix input")
            elif input_kind != "graph":
                _fail("selected matrix axis authority requires a graph input")
        elif axis["source_binding_id"] is None:
            if (
                source_kind != "retained_cell_axis"
                or input_kind not in {"cell_vector", "cell_vector_family"}
                or axis["axis_kind"] != "cell"
            ):
                _fail("retained cell axis authority differs from its declared input")
        elif input_kind != "graph":
            _fail("graph source requires an explicit binding selector")
        key = canonical_json(
            {
                "input_name": axis["input_name"],
                "source_binding_id": axis["source_binding_id"],
                "axis_kind": axis["axis_kind"],
                "source_ref": axis["source_ref"],
            }
        )
        if key in seen:
            _fail("duplicate source-axis authority")
        seen.add(key)


def validate_graph_write_observation(
    observation: Mapping[str, Any], grant: Mapping[str, Any]
) -> None:
    """Check receipt shape and grant/count closure; core must reread rows."""

    _shape(observation, "graph_write_observation", _RECEIPT)
    _self_identity(observation, "sealed_output_identity")
    if observation["output_name"] != grant["output_name"] or (
        canonical_json(observation["grant_identity"]) != canonical_json(grant["grant_identity"])
    ):
        _fail("graph receipt is bound to a different output grant")
    for component in _COMPONENTS:
        if (
            observation["component_observations"][component]["rows"]
            != (observation["component_counts"][component])
        ):
            _fail("graph receipt component count differs")
    if observation["component_counts"]["header"] != "1":
        _fail("graph receipt must contain exactly one header")
