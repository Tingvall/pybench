# Python operation-pack SDK v1

The installed public helper is `pybench.scaffold_operation_pack(path,
distribution, namespace, *, author, email) -> pathlib.Path`. The destination must
be new, its parent must exist, and namespace must be a lowercase Python module
identifier outside the reserved engine namespaces. The generated MIT project is
one ABI-v3 Python operation using exactly `pybench-op-v4`. Generation validates
its plain manifest through `validate_manifest_v3` and installs or activates
nothing. Explicit author metadata is required. Existing destinations are refused;
failed generation removes only its private temporary project.

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
Callbacks absent from the executor cannot be supplied by author code without
creating unauthorized authority. A capability extension requires a separate
engine decision. No shared protocol file or scientific pack changed for this SDK.

The operation rejects nonfinite input and sums and retains `chunk_size` and
`finite_policy='error'` through manifest-owned retention. Chunked IO bounds each
input block, not total algorithm memory: this example retains one value per cell.
Scientific validity, floating-point error, resources and native dependencies are
the author's responsibility. Installation is not activation; activation is not
scientific qualification; pack-free inspection is not pack-free replay.
