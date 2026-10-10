"""Capability-bound graph partition reader over the existing private broker."""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .graph import identity
from .graph_parts import COMPONENTS, partition_chain_seed, partition_chain_update
from .operation_packs import OperationPackError, canonical_json, protocol_asset
from .operation_packs_v2 import _identity
from .protocol_v3 import _safe_root
from .protocol_v4 import _strict_json_file, _wait_for

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = json.loads(protocol_asset("graph-v1.schema.json").read_text())
_PARTITION_VALIDATOR = Draft202012Validator(
    {
        "$defs": _SCHEMA["$defs"],
        "$ref": "#/$defs/component_partition",
    }
)


def _fail(reason: str) -> None:
    raise OperationPackError("PYBENCH_GRAPH_CAPABILITY_MISMATCH", reason, phase="graph_read")


class GraphBrokerClient:
    """One-shot ordered read, with no SQL or source path exposed to handlers."""

    def __init__(
        self,
        grant: Mapping[str, Any],
        stage_root: str | os.PathLike[str],
        *,
        transition_identity: Mapping[str, Any],
        authorization_token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._grant = copy.deepcopy(dict(grant))
        self._transition_identity = copy.deepcopy(dict(transition_identity))
        root = _safe_root(Path(stage_root))
        self._broker = root / "broker"
        if self._broker.is_symlink() or not self._broker.is_dir():
            _fail("graph broker spool is unavailable")
        self._token = authorization_token or os.environ.get(
            "PYBENCH_OPERATION_PACK_BROKER_TOKEN", ""
        )
        if not _HEX64.fullmatch(self._token):
            _fail("graph broker token is absent or malformed")
        raw_timeout = os.environ.get("PYBENCH_OPERATION_PACK_BROKER_TIMEOUT_SECONDS", "300")
        try:
            self._timeout = float(raw_timeout) if timeout is None else float(timeout)
        except (ValueError, TypeError):
            _fail("graph broker timeout is malformed")
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            _fail("graph broker timeout must be positive")
        self._counts = {component: 0 for component in COMPONENTS}
        self._partitions = {component: 0 for component in COMPONENTS}
        self._chains = {component: partition_chain_seed(component) for component in COMPONENTS}
        self._started = False
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    def _call(self, component: str, first_ordinal: int) -> dict[str, Any]:
        request = {
            "schema": "scworkbench.graph.broker_read_request.v1",
            "input_name": self._grant["input_name"],
            "component": component,
            "first_ordinal": str(first_ordinal),
            "read_grant_identity": self._grant["grant_identity"],
            "transition_identity": self._transition_identity,
            "authorization_token": self._token,
        }
        request["call_identity"] = _identity(
            "scworkbench.graph.broker_read_request.v1",
            request,
            "call excluding call_identity",
        )
        pending = self._broker / "request.json.pending"
        target = self._broker / "request.json"
        response = self._broker / "response"
        if pending.exists() or target.exists() or response.exists():
            _fail("graph broker is not ready")
        encoded = canonical_json(request).encode("utf-8")
        if len(encoded) > 16 * 1024:
            _fail("graph broker control request exceeds bounded size")
        try:
            pending.write_bytes(encoded)
            os.replace(pending, target)
            _wait_for(response.exists, self._timeout, "Timed out waiting for graph response")
            if response.is_symlink() or not response.is_dir():
                _fail("graph broker response directory is unsafe")
            files = list(response.iterdir())
            if len(files) != 1 or files[0].name != "manifest.json" or files[0].is_symlink():
                _fail("graph broker response contains undeclared files")
            manifest_path = files[0]
            if not manifest_path.is_file() or manifest_path.stat().st_size > (
                int(self._grant["partition_limits"]["max_decoded_bytes"]) + 8192
            ):
                _fail("graph broker response exceeds granted byte limit")
            value = _strict_json_file(
                manifest_path, "PYBENCH_GRAPH_CAPABILITY_MISMATCH", "graph_read"
            )
            claimed = value.get("response_identity")
            if not isinstance(claimed, dict) or claimed != _identity(
                "scworkbench.graph.broker_read_response.v1",
                {key: child for key, child in value.items() if key != "response_identity"},
                "response excluding response_identity",
            ):
                _fail("graph broker response identity differs")
            if (
                set(value)
                != {"schema", "partition", "complete", "call_identity", "response_identity"}
                or value["schema"] != "scworkbench.graph.broker_read_response.v1"
                or (value["call_identity"] != request["call_identity"])
            ):
                _fail("graph broker response does not match the call")
            acknowledgement = {
                "schema": "scworkbench.graph.broker_read_acknowledgement.v1",
                "call_identity": request["call_identity"],
                "response_identity": value["response_identity"],
                "status": "consumed",
                "authorization_token": self._token,
            }
            acknowledgement["acknowledgement_identity"] = _identity(
                "scworkbench.graph.broker_read_acknowledgement.v1",
                acknowledgement,
                "acknowledgement excluding acknowledgement_identity",
            )
            ack_pending = self._broker / "acknowledgement.json.pending"
            ack = self._broker / "acknowledgement.json"
            ack_pending.write_text(canonical_json(acknowledgement), encoding="utf-8")
            os.replace(ack_pending, ack)
            _wait_for(
                lambda: not response.exists() and not ack.exists() and not target.exists(),
                self._timeout,
                "Timed out waiting for graph acknowledgement",
            )
            return value
        finally:
            pending.unlink(missing_ok=True)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self._started:
            _fail("graph read grant is single-use")
        self._started = True
        for component in COMPONENTS:
            granted = int(self._grant["component_counts"][component])
            while self._counts[component] < granted:
                response = self._call(component, self._counts[component])
                part = response["partition"]
                if response["complete"] is not False or part is None:
                    _fail("graph broker ended before granted component closure")
                errors = list(_PARTITION_VALIDATOR.iter_errors(part))
                if errors:
                    _fail(f"graph broker partition schema differs: {errors[0].message}")
                expected_id = identity(
                    "scworkbench.graph.partition.v1",
                    {key: child for key, child in part.items() if key != "partition_identity"},
                )
                decoded_size = len(
                    canonical_json(
                        {key: child for key, child in part.items() if key != "partition_identity"}
                    ).encode("utf-8")
                )
                count = int(part["row_count"])
                if (
                    part["partition_identity"] != expected_id
                    or part["component"] != component
                    or part["first_ordinal"] != str(self._counts[component])
                    or count < 1
                    or count != len(part["rows"])
                    or count > int(self._grant["partition_limits"]["max_rows"])
                    or decoded_size > int(self._grant["partition_limits"]["max_decoded_bytes"])
                    or self._counts[component] + count > granted
                ):
                    _fail("graph partition is stale, malformed, or out of order")
                self._counts[component] += count
                self._partitions[component] += 1
                self._chains[component] = partition_chain_update(
                    component,
                    self._partitions[component],
                    self._chains[component],
                    part["partition_identity"],
                )
                yield copy.deepcopy(part)
        final = self._call("end", 0)
        if final["partition"] is not None or final["complete"] is not True:
            _fail("graph broker did not retire the complete read")
        self._complete = True

    def usage(self) -> dict[str, Any]:
        if not self._complete:
            _fail("graph read grant was not completely consumed")
        return {
            "input_name": self._grant["input_name"],
            "grant_identity": self._grant["grant_identity"],
            "component_observations": {
                component: {
                    "rows": str(self._counts[component]),
                    "partitions": str(self._partitions[component]),
                    "chain_identity": self._chains[component],
                }
                for component in COMPONENTS
            },
            "content_identity": self._grant["content_identity"],
            "binding_identity": self._grant["binding_identity"],
            "descriptor_identity": self._grant["descriptor_identity"],
            "selected_binding_ids": self._grant["selected_binding_ids"],
            "complete": True,
        }
