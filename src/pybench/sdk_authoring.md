# Python operation-pack SDK v1

SDK version, pack ABI version, and execution protocol version are different:
this SDK scaffolds ABI-v3 packs executed through `pybench-op-v4`. Existing
ABI-v1/v2 manifests are not upgraded by changing a version label. Scientific
method tests belong to the pack; engine authority and broker tests remain in
PyBench/scworkbench, with separately invoked installed-pack composites.
Generic graph ABI4/process5 support is separate and opt-in; the scaffold's
ABI3 default is unchanged.

The installed public helper is `pybench.scaffold_operation_pack(path,
distribution, namespace, *, author, email) -> pathlib.Path`. The destination must
be new, its parent must exist, and namespace must be a lowercase Python module
identifier outside the reserved engine namespaces. The generated MIT project is
one ABI-v3 Python operation using exactly `pybench-op-v4`. Generation validates
its plain manifest through `validate_manifest_v3` and installs or activates
nothing. Explicit author metadata is required. Existing destinations are refused
atomically, including competitors created during generation. Publication requires
Linux libc, kernel and filesystem support for `renameat2(RENAME_NOREPLACE)`; if
unavailable, generation fails closed and removes only its private staging directory.
There is no replacing-rename fallback. Failed generation likewise removes only
its private temporary project.

```python
from pybench import scaffold_operation_pack
scaffold_operation_pack("my pack", "my-pack", "mypack",
                         author="Actual Maintainer", email="maintainer@example.org")
```

The generated README has structural-validation, build/install, method-test and
explicit trusted activation commands. Importing the project is inert. Its method
test uses fake readers and proves calculation behavior only. Full authentication,
registered dispatch, closure, promotion, relocation and recipient tests belong
to the installed scworkbench SDK tutorial and contract driver. PyBench's own SDK
engine test separately executes the installed handler through the existing
protocol-v4 executor and validates both terminal receipts.

## Invocation-scoped callback surface

Handlers have `handler(readers, writers, params, context)`. Handler return values
do not publish outputs or automatically retain arbitrary parameters. Registration
returns `operation_pack_handlers_v3({qualified_name: handler})` from the declared
distribution entry point. No handler constructs requests, receipts or identities.

`readers[name]()` returns a private primitive copy. Ordinary matrix readers also
accept keyword `features` and `cells` within the authenticated input. A declared
typed-slice reader uses `readers[declaration_name](features=[...], cells=[...])`.
Its context entry at `context['typed_slice_reads'][declaration_name]` contains
`name`, `input_name`, `chunk_axis`, `requested_chunk_size`, `call_discipline`,
ordered `cells`, and ordered `features`. Use public `partition_axis(axis, size)`;
do not infer axes from host files. Exactly-once reads must cover the admitted
schedule, including the final partial block. The broker enforces axes, order,
size, source bindings and call observations.

Registry and artifact-ancestry readers take no arguments and are exactly once.
Contained-model readers take no arguments exactly once and return a checked
private handle with `entries`, `primary_path`, `path(relative)` and
`read_bytes(relative)`. A checked private path is not a committed bundle path.
Deserialization remains trusted code; checksums do not make pickle safe.

Writer methods take `(name, primitive, semantic_metadata=None)`:
`write_matrix`, `write_space`, `write_space_2d`, `write_model_ref`,
`write_terminal_primitive`, `write_path_2d_family`, `write_space_path_family`,
`request_cell_set_admission`, and `request_cell_set_family_admission`.
Registry requests are distinct from primitive kinds. Each name is consumed once
and must match its grant. Contained-model outputs use
`open_model_tree(name)`, checked `path(relative)`/`write_bytes(relative, bytes)`,
and `finish(...)`; matrix fragments use `open_matrix_fragments(name)`,
`write_fragment(...)`, then `finish(...)`. Complete trees, ordered schedules and
linked terminal closure are mandatory. Never retain any issued callback or
handle beyond the invocation.

## Capability support at the SDK checkpoint

Shared schemas are broader than Python execution. The following table records
the inspected implementation, rather than inferring execution from schema bytes.

| Capability | Manifest/request admission | Python execution |
| --- | --- | --- |
| All eleven ordinary primitive reader/writer kinds | accepted lawful tuples | implemented; all-contracts reference test |
| Typed matrix slices, finite-value policy | accepted | bounded broker implemented |
| Session cells and feature annotations | accepted | authenticated read implemented |
| Feature-set registry and cell-set/family requests | accepted | implemented |
| Fixed, parameter and bounded-group family axes | accepted | implemented cardinality checks |
| Parameter/dimension axes, matrix fragments, add/replace publication | accepted | implemented; Phase 4C tests |
| Contained model trees, source linkage v1/v2, sibling closure | accepted | implemented; Phase 4D/G1-G2 tests |
| Artifact ancestry | accepted | implemented |
| Generic graph, ABI4/process5 only | closed graph and mixed-primitive declarations, bounded graph grants, retained cell-vector/family or selected graph-binding source axes | bounded broker reads, private staged graph writes and complete mixed receipts; installed fixture exercises execution; no graph scaffold |
| Selector-bound typed spaces and sparse kNN grants | schemas and receipt parity supported | no typed-space callback/client in executor; unsupported scaffold |
| G4 collections, G5 named axes, G6 display/endpoint groups | shared schemas/parity | no Python runtime callback; unsupported scaffold |
| G3 external-state adoption | shared schemas/parity | no Python adoption runtime; unsupported scaffold |
| G7 cell-vector subset/union seals | shared schemas/parity | no Python seal/declaration runtime; unsupported scaffold |
| Standalone sparse families and sealed relations | manifest and request reject | unsupported |
| Genomic/motif reads and runtime cell-family keys | manifest rejects | unsupported |
| Producer lineage/full typed-space axes/linked-space unions | manifest and request reject | unsupported |
| Ordered cell metadata and nonnegative-integer matrix slices | shared schema accepts; request rejects | unsupported |
| Derived fitted axes/G1 linkage v3 | shared schema accepts; request rejects | unsupported |
| Matrix-trained contained model closure | shared schema accepts; request rejects | unsupported |

The simple scaffold exposes only matrix-cell sums. Unsupported advanced runtime
contracts are not scaffold options; callers must not use schema validation as a
claim of execution qualification. Existing fail-closed guards are in
`validate_manifest_v3`, `validate_request_v4`, and `validate_receipt_v4`.
ABI4 graph dispatch retains those inherited unsupported-grant boundaries;
graph support does not turn a schema-only capability into an executable one.
Callbacks absent from the executor cannot be supplied by author code without
creating unauthorized authority. A capability extension requires a separate
engine decision. No shared protocol file or scientific pack changed for this SDK.

The operation rejects nonfinite input and sums and retains `chunk_size` and
`finite_policy='error'` through manifest-owned retention. Chunked IO bounds each
input block, not total algorithm memory: this example retains one value per cell.
Scientific validity, floating-point error, resources and native dependencies are
the author's responsibility. Installation is not activation; activation is not
scientific qualification; pack-free inspection is not pack-free replay.

## Generic graph ABI4/process5 authoring

The source-tree package at
`tests/fixtures/operation-pack-v4-graph-python/` is the executable,
public-interface reference for this candidate. It declares a v4 manifest and
one distribution entry point, uses `operation_pack_handlers_v4`, and implements
matrix-to-unbound-graph, graph-to-table, graph-plus-metadata, and
graph-plus-retained-cell-vector operations. It imports no bundle, SQL,
registry, request or receipt internals. With Python 3.11+ and `build` and
`hatchling` available in the build environment, run from the PyBench
repository root to build and inspect the installed example explicitly:

```bash
python -m build --wheel --outdir /tmp/pybench-graph-wheels .
python -m build --wheel --outdir /tmp/pybench-graph-wheels tests/fixtures/operation-pack-v4-graph-python
python -m venv /tmp/pybench-graph-env
/tmp/pybench-graph-env/bin/python -m pip install /tmp/pybench-graph-wheels/pybench-*.whl /tmp/pybench-graph-wheels/scwb_graph_fixture-*.whl
/tmp/pybench-graph-env/bin/python -m pip check
```

```python
from importlib.resources import files
from pybench import activate_operation_pack_v4, discover_operation_pack_v4, validate_manifest_v4

manifest = files("scwbgraphfixture").joinpath("operation-pack.json")
validate_manifest_v4(manifest, expected_backend="Python")
pack = discover_operation_pack_v4(manifest)
activate_operation_pack_v4(pack)  # Explicit trust decision; installation is inert.
```

Validation and activation do not execute an operation or grant source access.
Only an R-issued process-v5 private staging capability can invoke a handler.
Handlers read graphs with `readers.read_graph_parts(name)`, write bounded parts
through `writers.open_graph(name, header)` and `write_part`/`finish`, or use
`GraphPrimitive` and `writers.write_graph` under the collection cap. For a
declared retained axis, use `context["graph_source_axes"][output_name]` and
`graph_binding_from_source`; no authority handle or host path is exposed. The
full contract and a public callback example are in `graph_abi4_candidate.md`.
Matrix-to-unbound-graph is supported; matrix-derived bindings remain fail-closed
until core authenticates their source certificate. Python cannot promote,
register or attest a graph independently of scworkbench.

## Authoring and Release Sequence

1. Generate the simple reference and run its method tests unchanged.
2. Declare supported primitive profiles, parameters/defaults, dependencies,
   grants, required terminals, retention, and complete publication closure.
3. Implement only the calculation through supplied callbacks; do not copy
   storage access, request construction, identity hashing, or receipts.
4. Add scientific oracles and malformed-input/schedule/fault cases, then test
   installed registered execution through the R engine's SDK lifecycle.
5. Record source and installed identities, numeric exits, discovered/executed
   cases, skips and limitations before distribution.

Use focused checks while developing; run a coordinated final qualification of
the frozen tuple rather than unrelated full suites after every prose change.
An absent callback or rejected policy is a capability gap, not permission to
provide a fabricated grant. Conflicting installed dependency versions may
require separate environments/processes; unloading code does not retroactively
change a retained run's runtime evidence.
