"""Generate a deterministic draft ABI4 mixed-closure interop fixture.

This is development evidence until the R issuer accepts the same tuple.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

from pybench.graph import GraphPrimitive
from pybench.operation_packs_v4 import (
    activate_operation_pack_v4,
    discover_operation_pack_v4,
    operation_pack_handlers_v4,
    unload_operation_pack_v4,
)
from pybench.protocol_v5 import (
    execute_operation_pack_v5,
    validate_python_runtime_sidecar_v5,
    validate_receipt_v5,
    validate_request_v5,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_graph_broker import TOKEN, _serve  # noqa: E402
from test_graph_protocol import _manifest  # noqa: E402
from v3_helpers import fixture_inputs  # noqa: E402
from v4_graph_helpers import graph_source_request  # noqa: E402


class Entry:
    group = "scworkbench.operation_packs.v4"
    name = "fixture"
    value = "scwbgraphfixture.pack:register"

    def load(self):
        return lambda: operation_pack_handlers_v4(
            {item["qualified_name"]: handler for item in _manifest()["operations"]}
        )


class Distribution:
    version = "0.1.0"
    metadata = {"Name": "scwb-graph-fixture"}

    def __init__(self, root):
        from importlib.metadata import PackagePath

        self.root = root
        self.files = [PackagePath("scwbgraphfixture/operation-pack.json")]
        self.entry_points = [Entry()]

    def locate_file(self, path):
        return self.root / path


def handler(readers, writers, params, context):
    assert [part["component"] for part in readers.read_graph_parts("source")] == ["header"]
    assert readers["metadata"]()["kind"] == "obs_table"
    writers.write_graph(
        "result",
        GraphPrimitive(
            {
                "schema": "scworkbench.graph.content.v1",
                "direction_mode": "undirected",
                "nodes": [],
                "edges": [],
                "incidences": [],
                "attributes": {scope: [] for scope in ("graph", "node", "edge", "incidence")},
            }
        ),
    )
    writers.write_terminal_primitive("summary", fixture_inputs()["obs_table"])


def main() -> None:
    target = ROOT / "protocol" / "fixtures" / "graph-v1" / "draft-abi4-mixed-interop.json"
    with tempfile.TemporaryDirectory(prefix="pybench-graph-fixture-") as tmp:
        root = Path(tmp)
        manifest = _manifest()
        installed_fixture_manifest = (
            ROOT
            / "tests/fixtures/operation-pack-v4-graph-python"
            / "src/scwbgraphfixture/operation-pack.json"
        )
        installed_fixture_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path = root / "scwbgraphfixture" / "operation-pack.json"
        path.parent.mkdir()
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        distribution = Distribution(root)
        stage = root / "stage"
        stage.mkdir()
        with (
            patch("pybench.operation_packs_v4.metadata.distribution", return_value=distribution),
            patch("v3_helpers.uuid.uuid4", return_value=uuid.UUID(int=1)),
            patch.dict(os.environ, {"PYBENCH_OPERATION_PACK_BROKER_TOKEN": TOKEN}),
        ):
            discovery = discover_operation_pack_v4(path)
            request = graph_source_request(discovery, stage, operation_index=1)
            validate_request_v5(request, discovery)
            activate_operation_pack_v4(discovery)
            server = threading.Thread(target=_serve, args=(stage,))
            server.start()
            try:
                receipt, runtime = execute_operation_pack_v5(request, discovery, stage)
                validate_receipt_v5(receipt, request, discovery, stage)
                validate_python_runtime_sidecar_v5(runtime, request, discovery)
            finally:
                server.join(timeout=5)
                unload_operation_pack_v4(manifest["namespace"])
            assert not server.is_alive()
            target.write_text(
                json.dumps(
                    {
                        "status": "draft",
                        "manifest": manifest,
                        "request": request,
                        "receipt": receipt,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            print(target)
            print(f"request={request['request_identity']['sha256']}")
            print(f"receipt={receipt['receipt_identity']['sha256']}")


if __name__ == "__main__":
    main()
