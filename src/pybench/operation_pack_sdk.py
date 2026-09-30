"""Installed, inert authoring scaffold for the supported simple ABI-v3 subset.

Generation confers no execution authority. Authentication, staging and receipts
remain in the existing operation-pack discovery and protocol-v4 implementation.
"""

from __future__ import annotations

import json
import keyword
import re
import shutil
import tempfile
from pathlib import Path

from .operation_packs_v2 import _RESERVED_NAMESPACES
from .operation_packs_v3 import validate_manifest_v3

SDK_VERSION = "1"


def matrix_cell_sum_manifest(distribution: str, namespace: str) -> dict:
    """Return a plain manifest; the existing validator remains authoritative."""
    operation = {
        "name": "matrix_cell_sum",
        "qualified_name": f"{namespace}::matrix_cell_sum",
        "title": "Matrix cell sum",
        "description": "Finite-only sums over authenticated bounded cell partitions.",
        "runner": ".matrix_cell_sum",
        "inputs": [
            {
                "name": "matrix",
                "kind": "matrix",
                "required": True,
                "alignment": "cell_and_feature_ordered",
            }
        ],
        "params_schema": {
            "type": "object",
            "properties": {"chunk_size": {"type": "integer", "minimum": 1, "default": 2}},
            "required": ["chunk_size"],
            "additionalProperties": False,
        },
        "typed_slice_reads": [
            {
                "name": "matrix_chunks",
                "input": "matrix",
                "kind": "matrix",
                "reader": "read_typed_slices",
                "supported_axes": ["cells"],
                "chunk_axis": "cells",
                "chunk_size_parameter": "chunk_size",
                "call_discipline": "ordered_partition_exactly_once",
                "empty_selection_policy": "reject",
                "duplicate_policy": "reject",
                "selection": {
                    "cells": {"policy": "effective_input_axis_v1"},
                    "features": {"policy": "effective_input_axis_v1"},
                },
            }
        ],
        "outputs": [
            {
                "name": "sums",
                "category": "terminal_artifact",
                "kind": "cell_vector",
                "storage_profile": "terminal_primitive_v1",
                "allocated_id_key": "sums_id",
                "required": True,
                "promotion_group": "sums",
                "value_kind": "continuous",
            },
            {
                "name": "summary",
                "category": "terminal_artifact",
                "kind": "obs_table",
                "storage_profile": "terminal_primitive_v1",
                "allocated_id_key": "summary_id",
                "required": True,
                "promotion_group": "sums",
                "table_type": "sdk_matrix_cell_sum_summary",
            },
        ],
        "dependencies": [],
        "dimension_policies": [{"output": "summary", "policy": "data_dependent_table_v1"}],
        "retained_parameters": [
            {
                "name": "chunk_size",
                "source": "effective_parameter_value",
                "parameter": "chunk_size",
            },
            {
                "name": "finite_policy",
                "source": "output_meta",
                "output": "sums",
                "field": "finite_policy",
            },
        ],
        "promotion_closure": {"sums": ["summary", "sums"], "summary": ["summary", "sums"]},
        "replay_equivalence": {"sums": "exact_canonical_value", "summary": "exact_canonical_value"},
    }
    return validate_manifest_v3(
        {
            "schema_version": "scworkbench.operation_pack.v3",
            "pack_abi_version": "3",
            "identity_scheme": "scworkbench.operation_pack.canonical_json.v1",
            "namespace": namespace,
            "package": {"name": distribution, "version": "0.1.0"},
            "backend": "Python",
            "compatibility": {
                "scworkbench": {"minimum": "0.1.0", "maximum_exclusive": "0.2.0"},
                "pack_abi": {"minimum": "3", "maximum": "3"},
                "process_protocol": {"minimum": "pybench-op-v4", "maximum": "pybench-op-v4"},
            },
            "entry_point": f"scworkbench.operation_packs.v3:pack={namespace}.pack:register",
            "operations": [operation],
        },
        expected_backend="Python",
    )


_RUNNER = '''"""Reference calculation; all storage access uses invocation-scoped readers."""
import math

from pybench import partition_axis, operation_pack_handlers_v3


def matrix_cell_sum(readers, writers, params, context):
    grant = context["typed_slice_reads"]["matrix_chunks"]
    cells, features = grant["cells"], grant["features"]
    sums, rows = [], []
    for index, selected in enumerate(partition_axis(cells, params["chunk_size"]), 1):
        block = readers["matrix_chunks"](features=features, cells=selected)
        if block["cell_id"] != selected or block["feature_id"] != features:
            raise ValueError("Unexpected authenticated block axes")
        if any(not math.isfinite(value) for row in block["values"] for value in row):
            raise ValueError("matrix_cell_sum requires finite values")
        values = [sum(row[column] for row in block["values"])
                  for column in range(len(selected))]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("matrix_cell_sum produced a nonfinite sum")
        sums.extend(values)
        rows.append({"chunk_index": index, "cell_count": len(selected),
                     "feature_count": len(features), "first_cell": selected[0],
                     "last_cell": selected[-1], "sum_total": sum(values)})
    writers.write_terminal_primitive("sums", {
        "kind": "cell_vector", "dimensions": [len(cells)], "cell_id": cells,
        "value": sums, "storage_profile": "terminal_primitive_v1",
        "meta": {"finite_policy": "error"}})
    writers.write_terminal_primitive("summary", {
        "kind": "obs_table", "dimensions": [len(rows), 6], "rows": rows,
        "storage_profile": "terminal_primitive_v1", "meta": {}})


def register():
    return operation_pack_handlers_v3({"__NAMESPACE__::matrix_cell_sum": matrix_cell_sum})
'''

_METHOD_TEST = '''"""Method-only tests: fake readers do not establish engine conformance."""
import json
from pathlib import Path
import unittest

from __NAMESPACE__.pack import matrix_cell_sum


class MethodTest(unittest.TestCase):
    def test_nonlexicographic_cells_and_partial_blocks(self):
        fixture = json.loads((Path(__file__).parent / "fixture.json").read_text())
        cells, features, values = fixture["cells"], fixture["features"], fixture["values"]
        index = {cell: i for i, cell in enumerate(cells)}
        for size in (1, 2, 3):
            outputs, calls = {}, []

            def reader(*, features, cells):
                calls.append(list(cells))
                return {"cell_id": cells, "feature_id": features,
                        "values": [[row[index[cell]] for cell in cells] for row in values]}

            class Writer:
                def write_terminal_primitive(self, name, value):
                    outputs[name] = value

            matrix_cell_sum({"matrix_chunks": reader}, Writer(), {"chunk_size": size},
                           {"typed_slice_reads": {"matrix_chunks": {
                               "cells": cells, "features": features}}})
            self.assertEqual(outputs["sums"]["value"], fixture["expected_sums"])
            self.assertEqual(outputs["sums"]["cell_id"], cells)
            self.assertEqual([cell for block in calls for cell in block], cells)
            self.assertLessEqual(max(map(len, calls)), size)


if __name__ == "__main__":
    unittest.main()
'''


def scaffold_operation_pack(
    path: str | Path, distribution: str, namespace: str, *, author: str, email: str
) -> Path:
    """Create one inert MIT-licensed Python pack; refuse overwrite and traversal.

    ``namespace`` is also the importable module name. ``distribution`` is the
    package-manager name. Metadata is explicit; this helper installs nothing.
    Only the existing simple matrix_cell_sum ABI-v3 profile is generated.
    """
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]*", namespace)
        or keyword.iskeyword(namespace)
        or namespace in _RESERVED_NAMESPACES
    ):
        raise ValueError("namespace must be a nonreserved lowercase Python identifier")
    if not isinstance(distribution, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", distribution):
        raise ValueError("distribution must be a lowercase package name")
    if (
        not isinstance(author, str)
        or not author.strip()
        or not isinstance(email, str)
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
    ):
        raise ValueError("explicit author and email metadata are required")
    destination = Path(path).expanduser()
    if ".." in destination.parts or destination.exists() or destination.is_symlink():
        raise ValueError("destination must be new and must not contain traversal")
    parent = destination.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("destination parent must be a directory")
    manifest = matrix_cell_sum_manifest(distribution, namespace)
    temporary = Path(tempfile.mkdtemp(prefix=".pybench-sdk-", dir=parent))
    try:
        module = temporary / "src" / namespace
        module.mkdir(parents=True)
        (module / "__init__.py").write_text('"""Installation and import are inert."""\n')
        (module / "pack.py").write_text(_RUNNER.replace("__NAMESPACE__", namespace))
        tests = temporary / "tests"
        tests.mkdir()
        (tests / "test_method.py").write_text(_METHOD_TEST.replace("__NAMESPACE__", namespace))
        (tests / "fixture.json").write_text(
            json.dumps(
                {
                    "features": ["gene_2", "gene_10", "gene_1"],
                    "source_cells": ["cell_2", "cell_10", "cell_1", "cell_3", "cell_7", "cell_4"],
                    "cells": ["cell_2", "cell_1", "cell_3", "cell_7", "cell_4"],
                    "values": [[1, 2, 0, 4, 5], [2, 3, 0, 5, 6], [3, 4, 0, 6, 7]],
                    "expected_sums": [6, 9, 0, 15, 18],
                },
                indent=2,
            )
            + "\n"
        )
        (temporary / "operation-pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # JSON strings are valid TOML basic strings and escape author-provided text.
        quote = json.dumps
        (temporary / "pyproject.toml").write_text(f'''[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"
[project]
name = {quote(distribution)}
version = "0.1.0"
description = "Synthetic finite-only matrix cell sum operation pack"
requires-python = ">=3.11"
license = {{text = "MIT"}}
authors = [{{name = {quote(author)}, email = {quote(email)}}}]
readme = "README.md"
dependencies = ["pybench==0.1.0"]
[project.entry-points."scworkbench.operation_packs.v3"]
pack = "{namespace}.pack:register"
[tool.hatch.build.targets.wheel]
packages = ["src/{namespace}"]
[tool.hatch.build.targets.wheel.force-include]
"operation-pack.json" = "{namespace}/operation-pack.json"
''')
        (temporary / "LICENSE").write_text(
            f"MIT License\n\nCopyright (c) {author}\n\n"
            + "Permission is hereby granted, free of charge, to any person obtaining a copy\n"
            "of this software and associated documentation files (the Software), to deal\n"
            "in the Software without restriction, including without limitation the rights\n"
            "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
            "copies of the Software, and to permit persons to whom the Software is\n"
            "furnished to do so, subject to the following conditions:\n\n"
            "The above copyright notice and this permission notice shall be included in all\n"
            "copies or substantial portions of the Software.\n\n"
            "THE SOFTWARE IS PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n"
            "IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
            "FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
            "AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
            "LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
            "OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
            "SOFTWARE.\n"
        )
        (temporary / "README.md").write_text(f'''# {distribution}

One synthetic Python ABI-v3 operation: `{namespace}::matrix_cell_sum`.
Declare typed IO, request the supplied matrix_chunks reader, compute finite sums,
and emit two primitives. Core owns authentication, alignment and atomic publication.

Static check (does not import pack code):
```python
from pybench import validate_manifest_v3
validate_manifest_v3("operation-pack.json", expected_backend="Python")
```
Method-only test: `PYTHONPATH=src python -m unittest discover -s tests -v`.
This checks the calculation with fake readers; it does not authenticate a source
or prove engine conformance. Installed registered-dispatch and lifecycle checks
must additionally use the scworkbench SDK contract driver and tutorial.
Build with `python -m build`; install the wheel into your explicit isolated
environment together with the accepted PyBench wheel. Installation is inert.
Explicit trusted discovery and activation:
```python
from pybench import discover_operation_pack_v3, activate_operation_pack_v3
from importlib.resources import files
pack = discover_operation_pack_v3(files("{namespace}") / "operation-pack.json")
activate_operation_pack_v3(pack)
```
The scworkbench client executes the qualified name through `wb_session_run_op`
with its configured PyBench interpreter; do not fabricate a request or receipt.
Use the installed scworkbench SDK tutorial for bundle assembly, registered
execution, promotion, relocation and recipient validation.

This example accepts finite values only, sums every effective feature in exact
cell order, and uses `chunk_size` (default 2). Both sums and per-chunk summary
belong to one required promotion closure. Chunked IO does not guarantee bounded
memory for a different algorithm. Advanced R-only capabilities are not supported
by this scaffold. Hashes authenticate bytes; trusted Python code is not sandboxed.
Scientific validation, numerical stability and resource requirements belong to you.
''')
        temporary.rename(parent / destination.name)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return parent / destination.name
