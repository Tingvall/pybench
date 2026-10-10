"""ABI4/process5 graph extensions to the existing pack reader/writer surface."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .graph import GraphPrimitive
from .graph_broker import GraphBrokerClient
from .graph_parts import GraphPartsWriter, _json
from .operation_packs import OperationPackError, canonical_json
from .operation_packs_v2 import _identity
from .protocol_v3 import _Write
from .protocol_v4 import V4Readers, V4Writers, validate_request_v4


def _fail(reason: str) -> None:
    raise OperationPackError("PYBENCH_GRAPH_CAPABILITY_MISMATCH", reason, phase="execution")


def validate_request_v5(request: Mapping[str, Any], discovery: Any) -> dict[str, Any]:
    return validate_request_v4(request, discovery, _graph_abi4=True)


def execute_operation_pack_v5(
    request: Mapping[str, Any],
    discovery: Any,
    stage_root: str | Path,
    *,
    fault_hook: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .protocol_v4 import execute_operation_pack_v4

    return execute_operation_pack_v4(
        request, discovery, stage_root, fault_hook=fault_hook, _graph_abi4=True
    )


def validate_receipt_v5(
    receipt: Mapping[str, Any],
    request: Mapping[str, Any],
    discovery: Any,
    stage_root: str | Path | None = None,
) -> dict[str, Any]:
    from .protocol_v4 import validate_receipt_v4

    return validate_receipt_v4(receipt, request, discovery, stage_root, _graph_abi4=True)


def validate_python_runtime_sidecar_v5(
    sidecar: Mapping[str, Any],
    request: Mapping[str, Any],
    discovery: Any,
) -> dict[str, Any]:
    from .protocol_v4 import validate_python_runtime_sidecar_v4

    return validate_python_runtime_sidecar_v4(sidecar, request, discovery, _graph_abi4=True)


def error_receipt_v5(request: Mapping[str, Any], error: BaseException) -> dict[str, Any]:
    from .protocol_v4 import error_receipt_v4

    value = error_receipt_v4(request, error)
    value["schema"] = "scworkbench.operation_pack.transition_receipt.v4"
    value["process_protocol_version"] = "pybench-op-v5"
    value["pack_abi_version"] = "4"
    value.pop("receipt_identity")
    value["receipt_identity"] = _identity(
        value["schema"], value, "receipt excluding receipt_identity"
    )
    return value


def _fingerprint_tree(root: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _fail("private stage contains a symlink")
        if not path.is_file():
            if not path.is_dir():
                _fail("private stage contains an unsupported object")
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result[path.relative_to(root).as_posix()] = (path.stat().st_size, digest.hexdigest())
    return result


class DiskStageSnapshot:
    """Disk-backed rollback baseline for large private staging generations."""

    def __init__(self, root: Path):
        self.root = root
        self.before = _fingerprint_tree(root)
        self._temp = Path(tempfile.mkdtemp(prefix="pybench-v5-stage-"))
        self._backup = self._temp / "stage"
        try:
            shutil.copytree(root, self._backup)
        except BaseException:
            shutil.rmtree(self._temp)
            raise

    def after(self) -> dict[str, tuple[int, str]]:
        return _fingerprint_tree(self.root)

    def restore(self) -> None:
        for child in self.root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        for child in self._backup.iterdir():
            target = self.root / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copyfile(child, target)

    def close(self) -> None:
        shutil.rmtree(self._temp)


def staged_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def observed_graph_descriptor(observation: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "kind": "graph",
        "storage_profile": "graph_sqlite_v1",
        "writer": "write_graph",
        "content_identity": observation["content_identity"],
        "binding_identity": observation["binding_identity"],
        "retained_descriptor_identity": observation["descriptor_identity"],
        "component_counts": observation["component_counts"],
        "sealed_output_identity": observation["sealed_output_identity"],
    }
    value["observed_descriptor_identity"] = _identity(
        "scworkbench.operation_pack.observed_output_descriptor.v4",
        value,
        "descriptor excluding observed_descriptor_identity",
    )
    return value


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("graph stage repeats a JSON object key")
        result[key] = value
    return result


def audit_graph_stage(
    grant: Mapping[str, Any],
    stage_root: Path,
    observation: Mapping[str, Any],
    *,
    transition_identity: Mapping[str, Any],
    input_kinds: Mapping[str, str],
    output_names: list[str],
) -> dict[str, Any]:
    """Boundedly revalidate physical parts and regenerate exact logical roots."""

    name = grant["output_name"]
    output = stage_root / "outputs" / name
    parts = output / "graph.parts.ndjson"
    descriptor_path = output / "payload.json"
    if (
        output.is_symlink()
        or parts.is_symlink()
        or descriptor_path.is_symlink()
        or not parts.is_file()
        or not descriptor_path.is_file()
        or {path.name for path in output.iterdir()} != {"graph.parts.ndjson", "payload.json"}
    ):
        _fail("graph stage has missing or undeclared payload paths")
    if descriptor_path.stat().st_size > 64 * 1024:
        _fail("graph staged descriptor is not compact")
    try:
        descriptor = json.loads(
            descriptor_path.read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise OperationPackError(
            "PYBENCH_GRAPH_CAPABILITY_MISMATCH",
            "graph staged descriptor is invalid",
            phase="audit",
        ) from exc
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {"schema", "output_name", "parts_file", "observation"}
        or descriptor["schema"] != "scworkbench.graph.staged_payload.v1"
        or descriptor["output_name"] != name
        or descriptor["parts_file"] != "graph.parts.ndjson"
        or descriptor["observation"] != observation
    ):
        _fail("graph staged descriptor differs from declared observation")
    maximum_line = int(grant["resource_budgets"]["partition_limits"]["max_decoded_bytes"]) + 4096
    with tempfile.TemporaryDirectory(prefix="pybench-v5-graph-audit-") as temporary:
        writer = GraphPartsWriter(
            grant,
            temporary,
            transition_identity=transition_identity,
            input_kinds=input_kinds,
            output_names=output_names,
        )
        offsets = {
            component: 0
            for component in (
                "header",
                "nodes",
                "edges",
                "incidences",
                "attribute_fields",
                "attribute_values",
                "bindings",
                "binding_mappings",
            )
        }
        try:
            with parts.open("rb") as handle:
                while True:
                    line = handle.readline(maximum_line + 1)
                    if not line:
                        break
                    if len(line) > maximum_line or not line.endswith(b"\n"):
                        _fail("graph staged partition line exceeds bounded length")
                    try:
                        part = json.loads(line, object_pairs_hook=_strict_pairs)
                    except (UnicodeError, ValueError) as exc:
                        raise OperationPackError(
                            "PYBENCH_GRAPH_CAPABILITY_MISMATCH",
                            "graph staged partition is invalid JSON",
                            phase="audit",
                        ) from exc
                    if (
                        not isinstance(part, dict)
                        or set(part)
                        != {
                            "schema",
                            "component",
                            "first_ordinal",
                            "row_count",
                            "rows",
                            "partition_identity",
                        }
                        or line != (_json(part) + "\n").encode("utf-8")
                    ):
                        _fail("graph staged partition is not the declared canonical line")
                    component = part["component"]
                    if component not in offsets or part["first_ordinal"] != str(offsets[component]):
                        _fail("graph staged partition offset differs")
                    if part["row_count"] != str(len(part["rows"])):
                        _fail("graph staged partition count differs")
                    from .graph import identity

                    expected_id = identity(
                        "scworkbench.graph.partition.v1",
                        {key: value for key, value in part.items() if key != "partition_identity"},
                    )
                    if part["partition_identity"] != expected_id:
                        _fail("graph staged partition identity differs")
                    writer.write_part(component, part["rows"])
                    offsets[component] += len(part["rows"])
            regenerated = writer.finish()
            if regenerated != observation:
                _fail("graph staged observation differs from actual rows")
            return regenerated
        except BaseException:
            writer.abort()
            raise


class V5Readers(V4Readers):
    def __init__(
        self,
        values: Mapping[str, dict[str, Any]],
        graph_clients: Mapping[str, GraphBrokerClient],
        **kwargs: Any,
    ):
        super().__init__(values, **kwargs)
        self._graph_clients = dict(graph_clients)
        self._graph_calls: set[str] = set()

    def read_graph_parts(self, name: str) -> Iterator[dict[str, Any]]:
        client = self._graph_clients.get(name)
        if client is None or name in self._graph_calls:
            _fail("graph reader is undeclared or already used")
        self._graph_calls.add(name)
        return iter(client)

    def __getitem__(self, key: str):
        if key in self._graph_clients:
            return lambda: self.read_graph_parts(key)
        return super().__getitem__(key)

    def __iter__(self):
        return iter((*super().__iter__(), *self._graph_clients))

    def __len__(self):
        return super().__len__() + len(self._graph_clients)

    def graph_read_usage(self) -> list[dict[str, Any]]:
        return [self._graph_clients[name].usage() for name in self._graph_clients]


class _GraphHandle:
    __slots__ = ("_owner", "_name", "_writer", "_finished")

    def __init__(self, owner: V5Writers, name: str, writer: GraphPartsWriter):
        self._owner = owner
        self._name = name
        self._writer = writer
        self._finished = False

    def write_part(self, component: str, rows: list[dict[str, Any]]) -> None:
        if self._finished:
            _fail("graph handle is already finished")
        self._writer.write_part(component, rows)

    def finish(self) -> dict[str, Any]:
        if self._finished:
            _fail("graph handle is already finished")
        observation = self._writer.finish()
        self._finished = True
        self._owner._register_graph(self._name, observation)
        return copy.deepcopy(observation)

    def abort(self) -> None:
        if not self._finished:
            self._writer.abort()
            self._finished = True


def _graph_rows(graph: GraphPrimitive) -> Iterator[tuple[str, dict[str, Any]]]:
    content = graph._content
    yield "header", {"schema": content["schema"], "direction_mode": content["direction_mode"]}
    for component in ("nodes", "edges", "incidences"):
        for row in content[component]:
            yield component, row
    for scope in ("graph", "node", "edge", "incidence"):
        for ordinal, field in enumerate(content["attributes"][scope]):
            yield (
                "attribute_fields",
                {
                    "scope": scope,
                    "field_ordinal": str(ordinal),
                    "name": field["name"],
                    "type": field["type"],
                    "nullable": field["nullable"],
                },
            )
    for scope in ("graph", "node", "edge", "incidence"):
        for field_ordinal, field in enumerate(content["attributes"][scope]):
            for entity_ordinal, value in enumerate(field["values"]):
                yield (
                    "attribute_values",
                    {
                        "scope": scope,
                        "field_ordinal": str(field_ordinal),
                        "entity_ordinal": str(entity_ordinal),
                        "value": value,
                    },
                )
    for binding_ordinal, binding in enumerate(graph._bindings):
        yield (
            "bindings",
            {
                "binding_ordinal": str(binding_ordinal),
                "binding_id": binding["binding_id"],
                "axis_kind": binding["axis_kind"],
                "source_ref": binding["source_ref"],
                "source_snapshot_identity": binding["source_snapshot_identity"],
                "axis_content_identity": binding["axis_content_identity"],
                "axis_order_identity": binding["axis_order_identity"],
            },
        )
    for binding in graph._bindings:
        for mapping_ordinal, pair in enumerate(binding["mapping"]):
            yield (
                "binding_mappings",
                {
                    "binding_id": binding["binding_id"],
                    "mapping_ordinal": str(mapping_ordinal),
                    "node_id": pair["node_id"],
                    "axis_id": pair["axis_id"],
                },
            )


class V5Writers(V4Writers):
    def __init__(
        self,
        request: Mapping[str, Any],
        stage_root: Path,
        cell_order: list[str],
        inputs: Mapping[str, dict[str, Any]],
    ):
        super().__init__(request, stage_root, cell_order, inputs)
        self._transition_identity = request["transition"]["transition_identity"]
        self._graph_grants = {
            grant["output_name"]: grant for grant in request.get("graph_write_grants", [])
        }
        self._input_kinds = {item["name"]: item["descriptor"]["kind"] for item in request["inputs"]}
        self._graph_handles: dict[str, _GraphHandle] = {}

    def open_graph(self, name: str, header: Mapping[str, Any]) -> _GraphHandle:
        if self._consumed or name in self._graph_handles or name in self._writes:
            _fail("graph output was already opened or consumed")
        if name not in self._graph_grants or self._grants[name]["descriptor"]["kind"] != "graph":
            _fail("graph output is undeclared")
        writer = GraphPartsWriter(
            self._graph_grants[name],
            self._stage_root,
            transition_identity=self._transition_identity,
            input_kinds=self._input_kinds,
            output_names=list(self._graph_grants),
        )
        handle = _GraphHandle(self, name, writer)
        self._graph_handles[name] = handle
        handle.write_part("header", [dict(header)])
        return handle

    def _register_graph(self, name: str, observation: dict[str, Any]) -> None:
        output = self._stage_root / "outputs" / name
        descriptor_path = output / "payload.json"
        parts_path = output / "graph.parts.ndjson"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor["observation"] != observation:
            _fail("graph staged descriptor differs from writer receipt")
        self._writes[name] = _Write(
            descriptor,
            descriptor_path,
            {"graph_observation": observation},
            "write_graph",
            [parts_path, descriptor_path],
        )

    def write_graph(self, name: str, graph: GraphPrimitive) -> dict[str, Any]:
        if not isinstance(graph, GraphPrimitive):
            _fail("write_graph requires a bounded GraphPrimitive")
        if name not in self._graph_grants:
            _fail("graph output is undeclared")
        if graph._size_bytes > int(
            self._graph_grants[name]["resource_budgets"]["max_collect_bytes"]
        ):
            raise OperationPackError(
                "PYBENCH_GRAPH_RESOURCE_LIMIT",
                "materialized graph exceeds issued collection budget",
                phase="graph_collection",
            )
        header = {
            "schema": graph._content["schema"],
            "direction_mode": graph._content["direction_mode"],
        }
        handle = self.open_graph(name, header)
        limits = self._graph_grants[name]["resource_budgets"]["partition_limits"]
        max_rows = int(limits["max_rows"])
        max_bytes = int(limits["max_decoded_bytes"])
        component = "header"
        batch: list[dict[str, Any]] = []
        try:
            for next_component, row in _graph_rows(graph):
                if next_component == "header":
                    continue
                candidate = batch + [row]
                estimated = len(canonical_json(candidate).encode("utf-8")) + 512
                if batch and (
                    next_component != component
                    or len(candidate) > max_rows
                    or estimated > max_bytes
                ):
                    handle.write_part(component, batch)
                    batch = []
                    candidate = [row]
                if len(canonical_json(candidate).encode("utf-8")) + 512 > max_bytes:
                    _fail("single graph row exceeds granted partition budget")
                component = next_component
                batch = candidate
            if batch:
                handle.write_part(component, batch)
            return handle.finish()
        except BaseException:
            handle.abort()
            raise

    def graph_write_observations(self) -> list[dict[str, Any]]:
        return [
            self._writes[name].semantic_metadata["graph_observation"]
            for name in self._graph_grants
            if name in self._writes
        ]
