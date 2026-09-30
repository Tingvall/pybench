"""Engine-owned qualification; generated authors need only public interfaces."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

from pybench.operation_pack_sdk import matrix_cell_sum_manifest, scaffold_operation_pack
from pybench.operation_packs_v2 import _identity
from pybench.operation_packs_v3 import (
    activate_operation_pack_v3,
    active_operation_packs_v3,
    discover_operation_pack_v3,
    unload_operation_pack_v3,
    validate_manifest_v3,
)
from pybench.protocol_v4 import execute_operation_pack_v4, validate_receipt_v4
from test_operation_pack_v3_successor import _add_typed_slice_contract, _request_v4
from test_operation_pack_v3_typed_slice import TOKEN, _serve


@pytest.fixture
def installed_example(tmp_path, monkeypatch):
    project = scaffold_operation_pack(
        tmp_path / "project with spaces",
        "sdk-sums",
        "sdksums",
        author="SDK Test Author",
        email="sdk@example.org",
    )
    site = tmp_path / "clean-site"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(site),
            str(project),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    before = active_operation_packs_v3()
    module = importlib.import_module("sdksums.pack")
    assert active_operation_packs_v3() == before
    discovery = discover_operation_pack_v3(site / "sdksums/operation-pack.json")
    yield project, module, discovery
    unload_operation_pack_v3("sdksums")
    sys.modules.pop("sdksums.pack", None)
    sys.modules.pop("sdksums", None)


def test_scaffold_plain_validated_manifest_and_no_private_author_access(installed_example):
    project, module, discovery = installed_example
    assert validate_manifest_v3(project / "operation-pack.json") == discovery.manifest
    assert set(module.register().handlers) == {"sdksums::matrix_cell_sum"}
    runner = (project / "src/sdksums/pack.py").read_text()
    assert "scworkbench:::" not in runner
    assert "_identity" not in runner and "sqlite" not in runner
    assert set(activate_operation_pack_v3(discovery).handlers) == {"sdksums::matrix_cell_sum"}


def test_generated_method_command_is_executable(installed_example):
    project, _module, _discovery = installed_example
    env = {**os.environ, "PYTHONPATH": str(project / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Ran 1 test" in result.stderr and "OK" in result.stderr


@pytest.mark.parametrize("chunk_size", [1, 2, 3])
@pytest.mark.parametrize("failure", [None, "before_summary", "after_handler"])
def test_reference_registered_execution_and_complete_receipt(
    installed_example, tmp_path, monkeypatch, chunk_size, failure
):
    _project, module, discovery = installed_example
    registration = activate_operation_pack_v3(discovery)
    if failure == "before_summary":

        def partial_failure(readers, writers, params, context):
            class PartialWriter:
                def write_terminal_primitive(self, name, value):
                    if name == "summary":
                        raise RuntimeError("injected before summary")
                    return writers.write_terminal_primitive(name, value)

            module.matrix_cell_sum(readers, PartialWriter(), params, context)

        monkeypatch.setitem(registration.handlers, "sdksums::matrix_cell_sum", partial_failure)
    stage = tmp_path / "stage"
    stage.mkdir()
    matrix = {
        "kind": "matrix",
        "dimensions": [1, 3],
        "cell_id": ["c2", "c1", "c3"],
        "feature_id": ["feature_1"],
        "values": [[2.0, 1.0, 3.0]],
        "storage_profile": "matrix_ordinary_v1",
        "meta": {},
    }
    request = _request_v4(
        discovery, stage, "sdksums::matrix_cell_sum", {"matrix": matrix}, {"chunk_size": chunk_size}
    )
    # The historical generic fixture builder assumes a one-cell vector. Bind
    # this engine-owned test grant to the actual three-cell source before use.
    descriptor = next(item["descriptor"] for item in request["outputs"] if item["name"] == "sums")
    descriptor["expected_dimensions"] = [3]
    descriptor.pop("grant_identity")
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v3",
        descriptor,
        "descriptor excluding grant_identity",
    )
    request["execution_capability"]["writer_grants"] = [
        {
            "output_name": item["name"],
            "writer": item["descriptor"]["writer"],
            "grant_identity": item["descriptor"]["grant_identity"],
        }
        for item in request["outputs"]
    ]
    grant = _add_typed_slice_contract(request, stage)
    shutil.rmtree(stage / "inputs/matrix")
    (stage / "broker").mkdir()
    monkeypatch.setenv("PYBENCH_OPERATION_PACK_BROKER_TOKEN", TOKEN)
    errors = []
    broker = threading.Thread(
        target=_serve,
        args=(stage, request, grant, (3 + chunk_size - 1) // chunk_size, errors),
        daemon=True,
    )
    broker.start()
    if failure:

        def fault_hook(phase):
            if failure == "after_handler" and phase == "after_handler":
                raise RuntimeError("injected after handler")

        with pytest.raises(RuntimeError, match="injected"):
            execute_operation_pack_v4(request, discovery, stage, fault_hook=fault_hook)
        broker.join(5)
        assert not broker.is_alive() and not errors
        assert not (stage / "outputs").exists()
        assert not (stage / "registry_requests").exists()
        assert not (stage / "broker").exists() or list((stage / "broker").iterdir()) == []
        return
    receipt, _sidecar = execute_operation_pack_v4(request, discovery, stage)
    broker.join(5)
    assert not broker.is_alive() and not errors
    assert validate_receipt_v4(receipt, request, discovery, stage) == receipt
    assert {item["name"] for item in receipt["outputs"]} == {"sums", "summary"}
    usage = receipt["typed_slice_usage"][0]
    assert usage["coverage_status"] == "exact"
    assert usage["call_count"] == (3 + chunk_size - 1) // chunk_size
    output = json.loads((stage / "outputs/sums/payload.json").read_text())
    assert output["cell_id"] == ["c2", "c1", "c3"]
    # The generic transport broker emits 11,21,... within each response.
    expected = [
        11.0 + 10 * column
        for start in range(0, 3, chunk_size)
        for column in range(min(chunk_size, 3 - start))
    ]
    assert output["value"] == expected
    assert output["meta"] == {"finite_policy": "error"}
    summary = json.loads((stage / "outputs/summary/payload.json").read_text())
    assert sum(row["cell_count"] for row in summary["rows"]) == 3


def test_reference_nonlexicographic_subset_zero_column_and_bounded_reads(installed_example):
    _project, module, _discovery = installed_example
    cells = ["cell_2", "cell_1", "cell_3", "cell_7", "cell_4"]
    features = ["gene_2", "gene_10", "gene_1"]
    values = [[1, 2, 0, 4, 5], [2, 3, 0, 5, 6], [3, 4, 0, 6, 7]]
    for size in (1, 2, 3):
        calls, outputs = [], {}

        def reader(*, features, cells):
            calls.append(list(cells))
            selected = [globals_index[cell] for cell in cells]
            return {
                "cell_id": cells,
                "feature_id": features,
                "values": [[row[i] for i in selected] for row in values],
            }

        class Writer:
            def write_terminal_primitive(self, name, value):
                outputs[name] = value

        globals_index = {cell: i for i, cell in enumerate(cells)}
        module.matrix_cell_sum(
            {"matrix_chunks": reader},
            Writer(),
            {"chunk_size": size},
            {"typed_slice_reads": {"matrix_chunks": {"cells": cells, "features": features}}},
        )
        assert [cell for block in calls for cell in block] == cells
        assert max(map(len, calls)) <= size
        assert outputs["sums"]["value"] == [6, 9, 0, 15, 18]
        assert outputs["sums"]["cell_id"] == cells
        assert outputs["summary"]["rows"][-1]["last_cell"] == "cell_4"


def test_reference_nonfinite_rejection_before_any_output(installed_example):
    _project, module, _discovery = installed_example
    outputs = []

    class Writer:
        def write_terminal_primitive(self, name, value):
            outputs.append(name)

    with pytest.raises(ValueError, match="finite"):
        module.matrix_cell_sum(
            {
                "matrix_chunks": lambda **kwargs: {
                    "cell_id": ["c"],
                    "feature_id": ["g"],
                    "values": [[float("nan")]],
                }
            },
            Writer(),
            {"chunk_size": 1},
            {"typed_slice_reads": {"matrix_chunks": {"cells": ["c"], "features": ["g"]}}},
        )
    assert outputs == []


@pytest.mark.parametrize("namespace", ["../bad", "scworkbench", "bad-name", "class", ""])
def test_scaffold_rejects_unsafe_names_without_residue(tmp_path, namespace):
    with pytest.raises(ValueError):
        scaffold_operation_pack(
            tmp_path / "unsafe", "sdk-sums", namespace, author="Author", email="a@example.org"
        )
    assert list(tmp_path.iterdir()) == []


def test_scaffold_collision_traversal_and_generation_failure_are_atomic(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep"
    sentinel.write_text("preserve")
    for path in (existing, tmp_path / "../traversal"):
        with pytest.raises(ValueError):
            scaffold_operation_pack(path, "sdk-sums", "sdksums", author="A", email="a@b.org")
    assert sentinel.read_text() == "preserve"
    from pathlib import Path

    original = Path.write_text

    def fail(self, *args, **kwargs):
        if self.name == "operation-pack.json":
            raise OSError("injected generation failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError, match="generation failure"):
        scaffold_operation_pack(
            tmp_path / "new", "sdk-sums", "sdksums", author="A", email="a@b.org"
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing"]


def test_sdk_manifest_keeps_frozen_protocol_and_simple_authority():
    manifest = matrix_cell_sum_manifest("sdk-sums", "sdksums")
    assert manifest["pack_abi_version"] == "3"
    assert manifest["compatibility"]["process_protocol"]["minimum"] == "pybench-op-v4"
    assert set(manifest["operations"][0]["promotion_closure"]["sums"]) == {"sums", "summary"}
