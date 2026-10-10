"""Language-neutral graph value validation and identity construction.

This module owns no bundle state or source authority. Bound graph execution must
compare these identities with independent core-issued source and seal records.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import struct
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .operation_packs import OperationPackError, canonical_json

CONTENT_SCHEME = "scworkbench.graph.content.canonical-json.v1"
BINDING_SCHEME = "scworkbench.graph.axis-bindings.canonical-json.v1"
DESCRIPTOR_SCHEME = "scworkbench.graph.retained-descriptor.canonical-json.v1"
AXIS_CONTENT_SCHEME = "scworkbench.graph.source-axis-content.canonical-json.v1"
SNAPSHOT_SCHEME = "scworkbench.provenance.snapshot_head.v1"
_SCOPES = ("graph", "node", "edge", "incidence")
_TYPES = ("bool", "utf8", "int64_decimal_string", "float64_le_hex")
_INT64 = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_OMITTED = object()
_SOURCE_AXIS_PROJECTION_FIELDS = (
    "input_name",
    "source_binding_id",
    "axis_kind",
    "source_ref",
    "source_snapshot_identity",
    "axis_content_identity",
    "axis_order_identity",
    "axis_count",
)


def _fail(reason: str) -> None:
    raise OperationPackError("PYBENCH_GRAPH_INVALID", reason, phase="graph_validation")


def _object(value: Any, fields: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(f"{where} must have exactly {sorted(fields)}")
    return value


def _array(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{where} must be an array")
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(f"{where} must be a nonempty UTF-8 identifier without NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{where} must contain valid Unicode scalar values")
    if len(encoded) > 256:
        _fail(f"{where} exceeds 256 UTF-8 bytes")
    return value


def _field_name(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("attribute name must be a nonempty UTF-8 string without NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("attribute name contains invalid Unicode")
    return value


def _identity(value: Any, scheme: str, where: str) -> Mapping[str, str]:
    value = _object(value, {"scheme", "sha256"}, where)
    if value["scheme"] != scheme or not isinstance(value["sha256"], str):
        _fail(f"{where} has an unsupported identity scheme")
    if not _HEX64.fullmatch(value["sha256"]):
        _fail(f"{where} has an invalid SHA-256 value")
    return value


def canonical_bytes(parts: Any) -> Iterator[bytes]:
    """Emit canonical JSON v1 tokens without collecting declared array iterators.

    An iterable which is not a mapping/string/scalar is treated as a JSON array.
    Production callers must supply replayable, bounded component cursors and
    validate their complete closure separately before publication.
    """

    if isinstance(parts, Mapping):
        if not all(isinstance(key, str) for key in parts):
            _fail("canonical object keys must be strings")
        yield b"{"
        for index, key in enumerate(sorted(parts, key=lambda item: item.encode("utf-8"))):
            if index:
                yield b","
            yield canonical_json(key).encode("utf-8")
            yield b":"
            yield from canonical_bytes(parts[key])
        yield b"}"
    elif isinstance(parts, str) and len(parts) > 8192:
        yield b'"'
        for start in range(0, len(parts), 8192):
            yield canonical_json(parts[start : start + 8192]).encode("utf-8")[1:-1]
        yield b'"'
    elif isinstance(parts, Iterable) and not isinstance(parts, (str, bytes, bytearray)):
        yield b"["
        for index, item in enumerate(parts):
            if index:
                yield b","
            yield from canonical_bytes(item)
        yield b"]"
    else:
        yield canonical_json(parts).encode("utf-8")


def canonical_sha256_stream(parts: Any) -> str:
    digest = hashlib.sha256()
    for token in canonical_bytes(parts):
        digest.update(token)
    return digest.hexdigest()


def canonical_sha256_size_stream(parts: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for token in canonical_bytes(parts):
        digest.update(token)
        size += len(token)
    return digest.hexdigest(), size


def graph_source_axis_projection(authority: Mapping[str, Any]) -> dict[str, Any]:
    """Expose binding-value fields, never the issued authority/certificate object."""

    return {key: copy.deepcopy(authority[key]) for key in _SOURCE_AXIS_PROJECTION_FIELDS}


def graph_binding_from_source(
    source_axis: Mapping[str, Any],
    binding_id: str,
    mapping: list[dict[str, str]],
) -> dict[str, Any]:
    """Construct a binding value; the staged writer still authenticates its source."""

    source = _object(source_axis, set(_SOURCE_AXIS_PROJECTION_FIELDS), "graph source axis")
    _id(binding_id, "binding_id")
    _array(mapping, "binding mapping")
    axis_count = source["axis_count"]
    if (
        not isinstance(axis_count, str)
        or not axis_count.isascii()
        or not axis_count.isdecimal()
        or len(axis_count) > 19
        or (len(axis_count) > 1 and axis_count.startswith("0"))
        or int(axis_count) > 9223372036854775807
    ):
        _fail("source axis count must be a nonnegative int64 decimal string")
    if len(mapping) > int(axis_count):
        _fail("binding mapping exceeds the issued source-axis count")
    return {
        "binding_id": binding_id,
        "axis_kind": source["axis_kind"],
        "source_ref": copy.deepcopy(source["source_ref"]),
        "source_snapshot_identity": copy.deepcopy(source["source_snapshot_identity"]),
        "axis_content_identity": copy.deepcopy(source["axis_content_identity"]),
        "axis_order_identity": copy.deepcopy(source["axis_order_identity"]),
        "mapping": copy.deepcopy(mapping),
    }


def identity(scheme: str, value: Any) -> dict[str, str]:
    return {"scheme": scheme, "sha256": canonical_sha256_stream(value)}


def _attribute_value(kind: str, nullable: bool, value: Any, where: str) -> None:
    if value is None:
        if not nullable:
            _fail(f"{where} is null but not nullable")
        return
    if kind == "bool":
        if not isinstance(value, bool):
            _fail(f"{where} must be boolean")
    elif kind == "utf8":
        if not isinstance(value, str) or "\x00" in value:
            _fail(f"{where} must be a UTF-8 string without NUL")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail(f"{where} contains invalid Unicode")
        if len(encoded) > 65536:
            _fail(f"{where} exceeds 65536 UTF-8 bytes")
    elif kind == "int64_decimal_string":
        if not isinstance(value, str) or not _INT64.fullmatch(value):
            _fail(f"{where} must be a canonical decimal int64 string")
        if not -(1 << 63) <= int(value) < (1 << 63):
            _fail(f"{where} is outside signed int64 range")
    elif kind == "float64_le_hex":
        if not isinstance(value, str) or not _HEX16.fullmatch(value):
            _fail(f"{where} must be lowercase little-endian float64 hex")
        if not math.isfinite(struct.unpack("<d", bytes.fromhex(value))[0]):
            _fail(f"{where} must be finite")


def validate_content(content: Any) -> dict[str, Any]:
    """Validate the frozen small-object graph projection before identity use."""

    value = _object(
        content,
        {"schema", "direction_mode", "nodes", "edges", "incidences", "attributes"},
        "graph content",
    )
    if value["schema"] != "scworkbench.graph.content.v1":
        _fail("unsupported graph content schema")
    mode = value["direction_mode"]
    if mode not in {"directed", "undirected", "mixed"}:
        _fail("invalid direction_mode")
    nodes = _array(value["nodes"], "nodes")
    edges = _array(value["edges"], "edges")
    incidences = _array(value["incidences"], "incidences")
    node_ids: set[str] = set()
    for node in nodes:
        node = _object(node, {"node_id"}, "node")
        node_id = _id(node["node_id"], "node_id")
        if node_id in node_ids:
            _fail("duplicate node_id")
        node_ids.add(node_id)
    edge_ids: set[str] = set()
    edge_records: list[Mapping[str, Any]] = []
    for edge in edges:
        edge = _object(edge, {"edge_id", "kind", "direction"}, "edge")
        edge_id = _id(edge["edge_id"], "edge_id")
        if edge_id in edge_ids:
            _fail("duplicate edge_id")
        edge_ids.add(edge_id)
        if edge["kind"] not in {"ordinary", "hyper"}:
            _fail("invalid edge kind")
        if edge["direction"] not in {"directed", "undirected"}:
            _fail("invalid edge direction")
        if mode != "mixed" and edge["direction"] != mode:
            _fail("edge direction conflicts with direction_mode")
        edge_records.append(edge)
    incidence_ids: set[str] = set()
    cursor = 0
    for edge in edge_records:
        roles: list[str] = []
        ordinal = 0
        while cursor < len(incidences):
            incidence = _object(
                incidences[cursor],
                {"incidence_id", "edge_id", "node_id", "role", "ordinal"},
                "incidence",
            )
            if incidence["edge_id"] != edge["edge_id"]:
                break
            occurrence = _id(incidence["incidence_id"], "incidence_id")
            if occurrence in incidence_ids:
                _fail("duplicate incidence_id")
            incidence_ids.add(occurrence)
            if incidence["node_id"] not in node_ids:
                _fail("dangling incidence node_id")
            observed_ordinal = incidence["ordinal"]
            if (
                type(observed_ordinal) not in (int, float)
                or (type(observed_ordinal) is float and not math.isfinite(observed_ordinal))
                or observed_ordinal != ordinal
            ):
                _fail("incidence ordinals must be contiguous and zero-based per edge")
            if ordinal > 999_999_999_999_999:
                _fail("incidence ordinal exceeds exact contiguous canonical JSON domain")
            roles.append(incidence["role"])
            ordinal += 1
            cursor += 1
        if edge["direction"] == "directed":
            if edge["kind"] == "ordinary" and roles != ["tail", "head"]:
                _fail("ordinary directed edge requires tail then head")
            if edge["kind"] == "hyper" and ("tail" not in roles or "head" not in roles):
                _fail("directed hyperedge requires tail and head")
            if any(role not in {"tail", "head"} for role in roles):
                _fail("invalid directed incidence role")
        else:
            if edge["kind"] == "ordinary" and roles != ["member", "member"]:
                _fail("ordinary undirected edge requires two members")
            if edge["kind"] == "hyper" and (not roles or any(role != "member" for role in roles)):
                _fail("undirected hyperedge requires members")
    if cursor != len(incidences):
        _fail("incidences must follow edge order and ordinal")
    attributes = _object(value["attributes"], set(_SCOPES), "attributes")
    counts = {"graph": 1, "node": len(nodes), "edge": len(edges), "incidence": len(incidences)}
    for scope in _SCOPES:
        names: set[str] = set()
        for field in _array(attributes[scope], f"attributes.{scope}"):
            field = _object(field, {"name", "type", "nullable", "values"}, "attribute field")
            name = _field_name(field["name"])
            if name in names:
                _fail(f"duplicate {scope} attribute field")
            names.add(name)
            kind = field["type"]
            if kind not in _TYPES or type(field["nullable"]) is not bool:
                _fail("invalid attribute type or nullability")
            values = _array(field["values"], "attribute values")
            if len(values) != counts[scope]:
                _fail(f"{scope} attribute length mismatch")
            for index, item in enumerate(values):
                _attribute_value(kind, field["nullable"], item, f"{scope}.{name}[{index}]")
    return dict(value)


def content_identity(content: Any) -> dict[str, str]:
    return identity(CONTENT_SCHEME, validate_content(content))


def validate_bindings(content: Any, bindings: Any) -> list[dict[str, Any]]:
    """Validate shape/mapping closure; source authority remains core-owned."""

    content = validate_content(content)
    records = _array(bindings, "bindings")
    node_order = {row["node_id"]: index for index, row in enumerate(content["nodes"])}
    seen: set[str] = set()
    for record in records:
        record = _object(
            record,
            {
                "binding_id",
                "axis_kind",
                "source_ref",
                "source_snapshot_identity",
                "axis_content_identity",
                "axis_order_identity",
                "mapping",
            },
            "binding",
        )
        binding_id = _id(record["binding_id"], "binding_id")
        if binding_id in seen:
            _fail("duplicate binding_id")
        seen.add(binding_id)
        kind = record["axis_kind"]
        if kind not in {"cell", "feature"}:
            _fail("invalid axis_kind")
        source = _object(record["source_ref"], {"kind", "source_id"}, "source_ref")
        if source["kind"] not in (
            {"matrix", "retained_cell_axis"} if kind == "cell" else {"matrix"}
        ):
            _fail("source_ref kind is incompatible with axis_kind")
        _id(source["source_id"], "source_id")
        _identity(record["source_snapshot_identity"], SNAPSHOT_SCHEME, "source_snapshot_identity")
        _identity(record["axis_content_identity"], AXIS_CONTENT_SCHEME, "axis_content_identity")
        order_scheme = (
            "scworkbench.cell_order.v1" if kind == "cell" else "scworkbench.feature_order.v1"
        )
        _identity(record["axis_order_identity"], order_scheme, "axis_order_identity")
        mapped_nodes: set[str] = set()
        mapped_axis: set[str] = set()
        previous = -1
        for pair in _array(record["mapping"], "binding mapping"):
            pair = _object(pair, {"node_id", "axis_id"}, "binding mapping row")
            node = _id(pair["node_id"], "mapping node_id")
            axis = _id(pair["axis_id"], "mapping axis_id")
            if node not in node_order:
                _fail("mapping references an unknown graph node")
            if node in mapped_nodes or axis in mapped_axis:
                _fail("binding mapping must be injective")
            if node_order[node] <= previous:
                _fail("binding mapping must follow graph node order")
            mapped_nodes.add(node)
            mapped_axis.add(axis)
            previous = node_order[node]
    return [dict(record) for record in records]


@dataclass(frozen=True)
class GraphIdentities:
    content: dict[str, str]
    bindings: dict[str, str]
    descriptor: dict[str, str]


class GraphPrimitive:
    """Bounded in-memory graph convenience value, independent of bundle state."""

    __slots__ = ("_content", "_bindings", "_identities", "_size_bytes")

    def __init__(
        self,
        content: Any,
        bindings: Any = _OMITTED,
        *,
        max_collect_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if type(max_collect_bytes) is not int or max_collect_bytes <= 0:
            _fail("max_collect_bytes must be a positive integer resource budget")
        validated = validate_content(content)
        normalized = [] if bindings is _OMITTED else validate_bindings(validated, bindings)
        size = sum(len(canonical_json(value).encode("utf-8")) for value in (validated, normalized))
        if size > max_collect_bytes:
            raise OperationPackError(
                "PYBENCH_GRAPH_RESOURCE_LIMIT",
                "graph collection exceeds the admitted materialization budget",
                phase="graph_collection",
            )
        self._content = copy.deepcopy(validated)
        self._bindings = copy.deepcopy(normalized)
        self._size_bytes = size
        self._identities = graph_identities(self._content, self._bindings)

    @property
    def content(self) -> dict[str, Any]:
        return copy.deepcopy(self._content)

    @property
    def bindings(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._bindings)

    @property
    def identities(self) -> GraphIdentities:
        return copy.deepcopy(self._identities)


def graph_identities(content: Any, bindings: Any = _OMITTED) -> GraphIdentities:
    """Build the three closed logical identities, not an authority attestation."""

    validated = validate_content(content)
    normalized = [] if bindings is _OMITTED else validate_bindings(validated, bindings)
    content_id = identity(CONTENT_SCHEME, validated)
    binding_id = identity(
        BINDING_SCHEME,
        {
            "schema": "scworkbench.graph.axis-bindings.v1",
            "graph_content_identity": content_id,
            "bindings": normalized,
        },
    )
    descriptor_id = identity(
        DESCRIPTOR_SCHEME,
        {
            "schema": "scworkbench.graph.retained-descriptor.v1",
            "content_identity": content_id,
            "binding_identity": binding_id,
            "binding_count": len(normalized),
        },
    )
    return GraphIdentities(content_id, binding_id, descriptor_id)


def axis_identities(
    axis_kind: str,
    source_ref: Mapping[str, str],
    snapshot_head: Mapping[str, Any],
    ordered_axis_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Recompute source-linked digests for independent comparison with core records."""

    if axis_kind not in {"cell", "feature"}:
        _fail("invalid axis kind")
    _object(source_ref, {"kind", "source_id"}, "source_ref")
    if source_ref["kind"] not in (
        {"matrix", "retained_cell_axis"} if axis_kind == "cell" else {"matrix"}
    ):
        _fail("source kind is incompatible with axis kind")
    _id(source_ref["source_id"], "source_id")
    _object(
        snapshot_head,
        {
            "snapshot_id",
            "schema_version",
            "head_run_ids",
            "n_runs",
            "n_artifacts",
            "commit_id",
            "replay_status",
        },
        "snapshot head",
    )
    seen: set[str] = set()
    for item in _array(ordered_axis_ids, "ordered source axis"):
        _id(item, "source axis ID")
        if item in seen:
            _fail("duplicate source axis ID")
        seen.add(item)
    axis_sorted = sorted(ordered_axis_ids, key=lambda item: item.encode("utf-8"))
    return {
        "source_snapshot_identity": identity(SNAPSHOT_SCHEME, snapshot_head),
        "axis_content_identity": identity(
            AXIS_CONTENT_SCHEME,
            {
                "schema": "scworkbench.graph.source-axis-content.v1",
                "axis_kind": axis_kind,
                "source_ref": source_ref,
                "axis_ids": axis_sorted,
            },
        ),
        "axis_order_identity": identity(
            "scworkbench.cell_order.v1" if axis_kind == "cell" else "scworkbench.feature_order.v1",
            ordered_axis_ids,
        ),
    }
