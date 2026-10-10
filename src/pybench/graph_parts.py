"""Bounded private-stage graph parts writer for the existing pack executor."""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .graph import (
    BINDING_SCHEME,
    CONTENT_SCHEME,
    DESCRIPTOR_SCHEME,
    _attribute_value,
    canonical_sha256_size_stream,
    identity,
)
from .graph_transport import validate_graph_write_grant, validate_graph_write_observation
from .operation_packs import OperationPackError, protocol_asset
from .operation_packs_v2 import _identity

COMPONENTS = (
    "header",
    "nodes",
    "edges",
    "incidences",
    "attribute_fields",
    "attribute_values",
    "bindings",
    "binding_mappings",
)
_SCOPES = ("graph", "node", "edge", "incidence")
_SCHEMA = json.loads(protocol_asset("graph-v1.schema.json").read_text())
_PARTITION_VALIDATOR = Draft202012Validator(
    {
        "$defs": _SCHEMA["$defs"],
        "$ref": "#/$defs/component_partition",
    }
)


def _fail(reason: str, *, resource: bool = False) -> None:
    raise OperationPackError(
        "PYBENCH_GRAPH_RESOURCE_LIMIT" if resource else "PYBENCH_GRAPH_INVALID",
        reason,
        phase="graph_staging",
    )


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _number(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        _fail("graph budget is not a decimal integer")
    return int(value)


def partition_chain_seed(component: str) -> dict[str, str]:
    if component not in COMPONENTS:
        _fail("unknown graph component")
    return identity(
        "scworkbench.graph.partition_chain.v1",
        {
            "schema": "scworkbench.graph.partition_chain.v1",
            "component": component,
            "partition_count": "0",
            "previous": None,
        },
    )


def partition_chain_update(
    component: str,
    count: int,
    previous: Mapping[str, str],
    partition_identity: Mapping[str, str],
) -> dict[str, str]:
    if component not in COMPONENTS or type(count) is not int or count < 1:
        _fail("invalid graph partition chain update")
    return identity(
        "scworkbench.graph.partition_chain.v1",
        {
            "schema": "scworkbench.graph.partition_chain.v1",
            "component": component,
            "partition_count": str(count),
            "previous": previous,
            "partition_identity": partition_identity,
        },
    )


class GraphPartsWriter:
    """Invocation-scoped ordered writer; pack code receives this, not its paths."""

    def __init__(
        self,
        grant: Mapping[str, Any],
        stage_root: str | os.PathLike[str],
        *,
        transition_identity: Mapping[str, Any],
        input_kinds: Mapping[str, str],
        output_names: Sequence[str],
    ) -> None:
        validate_graph_write_grant(
            grant,
            transition_identity=transition_identity,
            input_kinds=input_kinds,
            output_names=output_names,
        )
        root = Path(stage_root)
        if root.is_symlink() or not root.is_dir():
            _fail("private staging root is unavailable")
        output_parent = root / "outputs"
        if output_parent.is_symlink():
            _fail("private output parent is a symlink")
        created_parent = not output_parent.exists()
        created_dir = False
        parts = None
        db = None
        try:
            output_parent.mkdir(exist_ok=True)
            self._dir = output_parent / grant["output_name"]
            self._dir.mkdir(mode=0o700)
            created_dir = True
            self._parts_path = self._dir / "graph.parts.ndjson"
            self._descriptor_path = self._dir / "payload.json"
            self._scratch_path = self._dir / ".graph-index.sqlite"
            parts = self._parts_path.open("x", encoding="utf-8")
            db = sqlite3.connect(self._scratch_path)
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA cache_size=-1024")
            db.execute(
                "CREATE TABLE rows (component TEXT NOT NULL, global_ordinal INTEGER NOT NULL, scope TEXT, field_ordinal_num INTEGER, entity_ordinal_num INTEGER, binding_id TEXT, mapping_ordinal_num INTEGER, row_json TEXT NOT NULL, PRIMARY KEY(component, global_ordinal)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE INDEX attribute_coordinates ON rows(component,scope,field_ordinal_num,entity_ordinal_num)"
            )
            db.execute(
                "CREATE INDEX binding_coordinates ON rows(component,binding_id,mapping_ordinal_num)"
            )
            db.execute(
                "CREATE TABLE node_ids (node_id TEXT PRIMARY KEY, node_ordinal INTEGER NOT NULL) WITHOUT ROWID"
            )
            db.execute("CREATE TABLE edge_ids (edge_id TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute("CREATE TABLE incidence_ids (incidence_id TEXT PRIMARY KEY) WITHOUT ROWID")
            db.execute(
                "CREATE TABLE binding_ids (binding_id TEXT PRIMARY KEY, binding_ordinal INTEGER NOT NULL) WITHOUT ROWID"
            )
            db.execute(
                "CREATE TABLE field_names (scope TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(scope,name)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE TABLE mapping_nodes (binding_id TEXT NOT NULL, node_id TEXT NOT NULL, PRIMARY KEY(binding_id,node_id)) WITHOUT ROWID"
            )
            db.execute(
                "CREATE TABLE mapping_axes (binding_id TEXT NOT NULL, axis_id TEXT NOT NULL, PRIMARY KEY(binding_id,axis_id)) WITHOUT ROWID"
            )
            self._parts = parts
            self._db = db
            self._grant = dict(grant)
            self._counts = {name: 0 for name in COMPONENTS}
            self._partition_counts = {name: 0 for name in COMPONENTS}
            self._chains = {name: partition_chain_seed(name) for name in COMPONENTS}
            self._last_component = 0
            self._stage_bytes = 0
            self._closed = False
            self._failed = False
        except BaseException:
            if db is not None:
                db.close()
            if parts is not None:
                parts.close()
            if created_dir:
                shutil.rmtree(self._dir)
            if created_parent and output_parent.is_dir():
                output_parent.rmdir()
            raise

    def _rows(self, component: str) -> Iterator[dict[str, Any]]:
        cursor = self._db.execute(
            "SELECT row_json FROM rows WHERE component=? ORDER BY global_ordinal", (component,)
        )
        for (raw,) in cursor:
            yield json.loads(raw)

    def write_part(self, component: str, rows: list[dict[str, Any]]) -> None:
        if self._closed or self._failed:
            _fail("graph writer is closed")
        try:
            self._write_part(component, rows)
        except BaseException:
            self._failed = True
            raise

    def _write_part(self, component: str, rows: list[dict[str, Any]]) -> None:
        if component not in COMPONENTS or COMPONENTS.index(component) < self._last_component:
            _fail("graph components must arrive in fixed order")
        if not isinstance(rows, list) or not rows:
            _fail("graph partition must be a nonempty array")
        if component == "header" and (self._counts["header"] or len(rows) != 1):
            _fail("graph requires exactly one header row")
        if not self._counts["header"] and component != "header":
            _fail("graph header must precede all other components")
        limits = self._grant["resource_budgets"]["partition_limits"]
        if len(rows) > _number(limits["max_rows"]):
            _fail("graph partition row budget exceeded", resource=True)
        preimage = {
            "schema": "scworkbench.graph.component_partition.v1",
            "component": component,
            "first_ordinal": str(self._counts[component]),
            "row_count": str(len(rows)),
            "rows": rows,
        }
        decoded = _json(preimage).encode("utf-8")
        if len(decoded) > _number(limits["max_decoded_bytes"]):
            _fail("graph partition byte budget exceeded", resource=True)
        partition = dict(preimage)
        partition["partition_identity"] = identity("scworkbench.graph.partition.v1", preimage)
        errors = list(_PARTITION_VALIDATOR.iter_errors(partition))
        if errors:
            _fail(f"invalid graph partition: {errors[0].message}")
        stage_line = (_json(partition) + "\n").encode("utf-8")
        if self._stage_bytes + len(stage_line) > _number(
            self._grant["resource_budgets"]["max_staged_bytes"]
        ):
            _fail("graph staging byte budget exceeded", resource=True)
        self._parts.write(stage_line.decode("utf-8"))
        self._parts.flush()
        try:
            self._db.executemany(
                "INSERT INTO rows(component,global_ordinal,scope,field_ordinal_num,entity_ordinal_num,binding_id,mapping_ordinal_num,row_json) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        component,
                        self._counts[component] + index,
                        row.get("scope"),
                        int(row["field_ordinal"]) if "field_ordinal" in row else None,
                        int(row["entity_ordinal"]) if "entity_ordinal" in row else None,
                        row.get("binding_id"),
                        int(row["mapping_ordinal"]) if "mapping_ordinal" in row else None,
                        _json(row),
                    )
                    for index, row in enumerate(rows)
                ],
            )
            unique_key = {
                "edges": ("edge_ids", "edge_id"),
                "incidences": ("incidence_ids", "incidence_id"),
                "bindings": ("binding_ids", "binding_id"),
            }.get(component)
            if component == "nodes":
                self._db.executemany(
                    "INSERT INTO node_ids(node_id,node_ordinal) VALUES(?,?)",
                    (
                        (row["node_id"], self._counts[component] + index)
                        for index, row in enumerate(rows)
                    ),
                )
            if unique_key:
                table, field = unique_key
                if component == "bindings":
                    self._db.executemany(
                        "INSERT INTO binding_ids(binding_id,binding_ordinal) VALUES(?,?)",
                        (
                            (row["binding_id"], self._counts[component] + index)
                            for index, row in enumerate(rows)
                        ),
                    )
                else:
                    self._db.executemany(
                        f"INSERT INTO {table}({field}) VALUES(?)",
                        ((row[field],) for row in rows),
                    )
            if component == "attribute_fields":
                self._db.executemany(
                    "INSERT INTO field_names(scope,name) VALUES(?,?)",
                    ((row["scope"], row["name"]) for row in rows),
                )
            if component == "binding_mappings":
                self._db.executemany(
                    "INSERT INTO mapping_nodes(binding_id,node_id) VALUES(?,?)",
                    ((row["binding_id"], row["node_id"]) for row in rows),
                )
                self._db.executemany(
                    "INSERT INTO mapping_axes(binding_id,axis_id) VALUES(?,?)",
                    ((row["binding_id"], row["axis_id"]) for row in rows),
                )
        except sqlite3.IntegrityError as exc:
            _fail(f"duplicate graph ID, field or partition ordinal: {exc}")
        self._db.commit()
        self._counts[component] += len(rows)
        self._partition_counts[component] += 1
        self._chains[component] = partition_chain_update(
            component,
            self._partition_counts[component],
            self._chains[component],
            partition["partition_identity"],
        )
        self._stage_bytes += len(stage_line)
        self._last_component = COMPONENTS.index(component)
        limit_by_component = {
            "nodes": "max_nodes",
            "edges": "max_edges",
            "incidences": "max_incidences",
            "attribute_fields": "max_attribute_fields",
        }
        if component in limit_by_component and self._counts[component] > _number(
            self._grant["resource_budgets"][limit_by_component[component]]
        ):
            _fail(f"graph {component} budget exceeded", resource=True)
        if self._scratch_path.stat().st_size > _number(
            self._grant["resource_budgets"]["max_spill_bytes"]
        ):
            _fail("graph spill byte budget exceeded", resource=True)

    def _values(self, scope: str, field_ordinal: str, kind: str, nullable: bool) -> Iterator[Any]:
        cursor = self._db.execute(
            "SELECT row_json FROM rows WHERE component='attribute_values' AND scope=? AND field_ordinal_num=? ORDER BY entity_ordinal_num",
            (scope, int(field_ordinal)),
        )
        for index, (raw,) in enumerate(cursor):
            row = json.loads(raw)
            if row["entity_ordinal"] != str(index):
                _fail("attribute entity ordinals are not dense")
            _attribute_value(kind, nullable, row["value"], f"{scope}.{field_ordinal}[{index}]")
            yield row["value"]

    def _fields(self, scope: str) -> Iterator[dict[str, Any]]:
        cursor = self._db.execute(
            "SELECT row_json FROM rows WHERE component='attribute_fields' AND scope=? ORDER BY field_ordinal_num",
            (scope,),
        )
        for index, (raw,) in enumerate(cursor):
            row = json.loads(raw)
            if row["field_ordinal"] != str(index):
                _fail("attribute field ordinals are not dense")
            yield {
                "name": row["name"],
                "type": row["type"],
                "nullable": row["nullable"],
                "values": self._values(scope, row["field_ordinal"], row["type"], row["nullable"]),
            }

    def _bindings(self) -> Iterator[dict[str, Any]]:
        for row in self._rows("bindings"):
            yield {
                "binding_id": row["binding_id"],
                "axis_kind": row["axis_kind"],
                "source_ref": row["source_ref"],
                "source_snapshot_identity": row["source_snapshot_identity"],
                "axis_content_identity": row["axis_content_identity"],
                "axis_order_identity": row["axis_order_identity"],
                "mapping": self._mapping_for(row["binding_id"]),
            }

    def _mapping_for(self, binding_id: str) -> Iterator[dict[str, str]]:
        cursor = self._db.execute(
            "SELECT row_json FROM rows WHERE component='binding_mappings' AND binding_id=? ORDER BY mapping_ordinal_num",
            (binding_id,),
        )
        for index, (raw,) in enumerate(cursor):
            row = json.loads(raw)
            if row["mapping_ordinal"] != str(index):
                _fail("binding mapping ordinals are not dense")
            yield {"node_id": row["node_id"], "axis_id": row["axis_id"]}

    def _validate_topology(self, direction_mode: str) -> None:
        incidence_rows = iter(self._rows("incidences"))
        current = next(incidence_rows, None)
        for edge in self._rows("edges"):
            if direction_mode != "mixed" and edge["direction"] != direction_mode:
                _fail("edge direction conflicts with graph mode")
            count = tails = heads = 0
            ordinary_roles: list[str] = []
            while current is not None and current["edge_id"] == edge["edge_id"]:
                ordinal = current["ordinal"]
                if (
                    type(ordinal) not in (int, float)
                    or (type(ordinal) is float and not math.isfinite(ordinal))
                    or ordinal != count
                ):
                    _fail("incidence ordinals are not dense within edge")
                if (
                    self._db.execute(
                        "SELECT 1 FROM node_ids WHERE node_id=?", (current["node_id"],)
                    ).fetchone()
                    is None
                ):
                    _fail("dangling incidence node")
                role = current["role"]
                if edge["direction"] == "directed":
                    if role == "tail":
                        tails += 1
                    elif role == "head":
                        heads += 1
                    else:
                        _fail("invalid directed incidence role")
                elif role != "member":
                    _fail("invalid undirected incidence role")
                if edge["kind"] == "ordinary":
                    if count >= 2:
                        _fail("ordinary edge has more than two incidences")
                    ordinary_roles.append(role)
                count += 1
                current = next(incidence_rows, None)
            if edge["kind"] == "ordinary":
                expected = (
                    ["tail", "head"] if edge["direction"] == "directed" else ["member", "member"]
                )
                if ordinary_roles != expected:
                    _fail("ordinary edge incidence roles are incomplete")
            elif not count or (edge["direction"] == "directed" and (not tails or not heads)):
                _fail("hyperedge incidence roles are incomplete")
        if current is not None:
            _fail("incidences are not ordered by declared edges")

    def _validate_attributes(self) -> None:
        counts = {
            "graph": 1,
            "node": self._counts["nodes"],
            "edge": self._counts["edges"],
            "incidence": self._counts["incidences"],
        }
        last_scope = 0
        next_field = {scope: 0 for scope in _SCOPES}
        for row in self._rows("attribute_fields"):
            scope = row["scope"]
            position = _SCOPES.index(scope)
            if position < last_scope or row["field_ordinal"] != str(next_field[scope]):
                _fail("attribute fields are not in dense scope order")
            last_scope = position
            next_field[scope] += 1
        previous_coordinate = (-1, -1, -1)
        for row in self._rows("attribute_values"):
            coordinate = (
                _SCOPES.index(row["scope"]),
                int(row["field_ordinal"]),
                int(row["entity_ordinal"]),
            )
            if coordinate <= previous_coordinate:
                _fail("attribute values are not in declared field/entity order")
            previous_coordinate = coordinate
        consumed = 0
        for scope in _SCOPES:
            for field in self._fields(scope):
                length = sum(1 for _ in field["values"])
                if length != counts[scope]:
                    _fail("attribute values do not match entity cardinality")
                consumed += length
        if consumed != self._counts["attribute_values"]:
            _fail("orphan attribute value rows")

    def _validate_bindings(self) -> None:
        authorized = {
            _json(
                {
                    key: axis[key]
                    for key in (
                        "axis_kind",
                        "source_ref",
                        "source_snapshot_identity",
                        "axis_content_identity",
                        "axis_order_identity",
                    )
                }
            ): int(axis["axis_count"])
            for axis in self._grant["authorized_source_axes"]
        }
        previous_coordinate = (-1, -1)
        for row in self._rows("binding_mappings"):
            found = self._db.execute(
                "SELECT binding_ordinal FROM binding_ids WHERE binding_id=?",
                (row["binding_id"],),
            ).fetchone()
            if found is None:
                _fail("binding mapping has no declared binding")
            coordinate = (found[0], int(row["mapping_ordinal"]))
            if coordinate <= previous_coordinate:
                _fail("binding mappings are not in declared binding/ordinal order")
            previous_coordinate = coordinate
        consumed = 0
        for index, row in enumerate(self._rows("bindings")):
            if row["binding_ordinal"] != str(index):
                _fail("binding ordinals are not dense")
            claim = _json(
                {
                    key: row[key]
                    for key in (
                        "axis_kind",
                        "source_ref",
                        "source_snapshot_identity",
                        "axis_content_identity",
                        "axis_order_identity",
                    )
                }
            )
            if claim not in authorized:
                _fail("binding source is not an issued source-axis authority")
            previous_node_ordinal = -1
            mapped = 0
            for mapping in self._mapping_for(row["binding_id"]):
                found = self._db.execute(
                    "SELECT node_ordinal FROM node_ids WHERE node_id=?", (mapping["node_id"],)
                ).fetchone()
                if found is None or found[0] <= previous_node_ordinal:
                    _fail("binding mapping is dangling or out of node order")
                previous_node_ordinal = found[0]
                mapped += 1
                consumed += 1
            if mapped > authorized[claim]:
                _fail("binding mapping exceeds the issued source-axis count")
        if consumed != self._counts["binding_mappings"]:
            _fail("orphan binding mapping rows")
        if self._grant["binding_policy"] == "required" and not self._counts["bindings"]:
            _fail("required graph binding is absent")

    def finish(self) -> dict[str, Any]:
        try:
            return self._finish()
        except BaseException:
            self._failed = True
            raise

    def _finish(self) -> dict[str, Any]:
        if self._closed or self._failed or self._counts["header"] != 1:
            _fail("graph writer is incomplete")
        header = next(self._rows("header"))
        self._validate_topology(header["direction_mode"])
        self._validate_attributes()
        self._validate_bindings()
        content = {
            "schema": header["schema"],
            "direction_mode": header["direction_mode"],
            "nodes": self._rows("nodes"),
            "edges": self._rows("edges"),
            "incidences": self._rows("incidences"),
            "attributes": {scope: self._fields(scope) for scope in _SCOPES},
        }
        content_digest, self._canonical_content_bytes = canonical_sha256_size_stream(content)
        content_id = {"scheme": CONTENT_SCHEME, "sha256": content_digest}
        bindings_id = identity(
            BINDING_SCHEME,
            {
                "schema": "scworkbench.graph.axis-bindings.v1",
                "graph_content_identity": content_id,
                "bindings": self._bindings(),
            },
        )
        descriptor_id = identity(
            DESCRIPTOR_SCHEME,
            {
                "schema": "scworkbench.graph.retained-descriptor.v1",
                "content_identity": content_id,
                "binding_identity": bindings_id,
                "binding_count": self._counts["bindings"],
            },
        )
        observations = {}
        for component in COMPONENTS:
            observations[component] = {
                "rows": str(self._counts[component]),
                "partitions": str(self._partition_counts[component]),
                "chain_identity": self._chains[component],
            }
        result = {
            "output_name": self._grant["output_name"],
            "grant_identity": self._grant["grant_identity"],
            "component_observations": observations,
            "content_identity": content_id,
            "binding_identity": bindings_id,
            "descriptor_identity": descriptor_id,
            "component_counts": {name: str(count) for name, count in self._counts.items()},
            "complete": True,
        }
        result["sealed_output_identity"] = _identity(
            "scworkbench.graph.sealed_output.v1",
            result,
            "seal excluding sealed_output_identity",
        )
        validate_graph_write_observation(result, self._grant)
        self._parts.close()
        self._db.close()
        self._scratch_path.unlink()
        descriptor = {
            "schema": "scworkbench.graph.staged_payload.v1",
            "output_name": self._grant["output_name"],
            "parts_file": "graph.parts.ndjson",
            "observation": result,
        }
        self._descriptor_path.write_text(_json(descriptor) + "\n", encoding="utf-8")
        self._closed = True
        return result

    def abort(self) -> None:
        if not self._closed:
            self._parts.close()
            self._db.close()
            shutil.rmtree(self._dir)
            self._closed = True
