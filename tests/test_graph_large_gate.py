"""Opt-in large graph gate; routine suites do not write a 512 MiB payload."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("PYBENCH_GRAPH_LARGE_GATE") != "1",
    reason="set PYBENCH_GRAPH_LARGE_GATE=1 for the beyond-RAM qualification",
)
def test_streamed_hypergraph_exceeds_declared_memory_by_eightfold(tmp_path):
    worker = Path(__file__).with_name("large_graph_gate_worker.py")
    result = subprocess.run(
        [sys.executable, str(worker), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=900,
    )
    observed = json.loads(result.stdout)
    print(json.dumps(observed, sort_keys=True))
    assert observed["canonical_content_bytes"] >= 8 * observed["memory_budget_bytes"]
    assert observed["peak_rss_bytes"] <= observed["memory_budget_bytes"]
    assert observed["counts"]["nodes"] == "24576"
    assert observed["counts"]["incidences"] == "24576"
    assert observed["counts"]["binding_mappings"] == "24576"
    assert observed["content_identity"]["scheme"] == ("scworkbench.graph.content.canonical-json.v1")
