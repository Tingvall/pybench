# Python operation-pack ABI v2

This document describes PyBench's synthetic reference implementation of the
scworkbench operation-pack ABI v2. It does not announce an official scientific
pack. scworkbench remains authoritative for schemas, scientific state,
allocation, publication, promotion, provenance, and replay decisions.

## Discovery and activation

A Python v2 distribution packages exactly one `operation-pack.json` and exactly
one matching entry point in `scworkbench.operation_packs.v2`. The manifest's
entry-point value has the form:

```text
scworkbench.operation_packs.v2:name=package.module:register
```

`discover_operation_pack_v2(path)` strictly parses and validates the installed
manifest, authenticates its distribution tree and entry-point metadata, and
computes canonical v2 identities. It does not import pack code or mutate the
active registry. `activate_operation_pack_v2(discovery)` repeats installed
source authentication, imports only the named callable, validates a complete
private candidate registry, and publishes it atomically in the current process.

The entry point returns `operation_pack_handlers_v2({...})`. Every manifest
operation has exactly one callable with this signature:

```python
def handler(readers, writers, params, context): ...
```

No v1 entry point can register through the v2 path, and no v2 request is
silently downgraded to `pybench-op-v2`.

## V3 transport

`pybench-op-v3` accepts the shared transition-request v2 document plus the
core-created private stage. It validates the request, exact pack identity,
typed input and output closure, dependency preflight, and single-use capability
before dispatch. The capability lease has no clock field in the frozen shared
schema: stale/expired authority is represented by a mismatched lease,
checkpoint generation, session, run, source state, or capability identity.

Readers are named callables for the declared input kinds:

```text
read_matrix, read_space, read_space_2d, read_model_ref,
read_terminal_primitive, read_path_2d_family, read_space_path_family
```

The writer object exposes only the granted methods:

```text
write_matrix, write_space, write_space_2d, write_model_ref,
write_terminal_primitive, write_path_2d_family, write_space_path_family,
request_cell_set_admission
```

The implementation supports every input kind, output tuple, and storage profile
accepted by the shared schemas. It independently verifies dimensions,
cell/feature/row order, semantic metadata, linked-space and model containment
evidence where those values are derivable from the projected payload. Ancestry
and allocation remain authenticated core-issued descriptor fields.

Writers create only canonical `payload.json` files below their granted relative
roots. Before returning, PyBench re-reads those bytes, checks output semantics,
audits every changed file and created directory, and validates a complete
receipt. Exceptions, invalid output closure, receipt faults, symlinks, and
undeclared mutations restore the exact pre-dispatch stage. A hard-killed Python
process cannot perform in-process cleanup; the core-owned subprocess boundary
therefore rejects the missing receipt and deletes the private generation.
Python never publishes or promotes results.

## Runtime evidence

The shared transition receipt contains the closed, language-neutral
`scworkbench.operation_pack.runtime.v2` attestation. PyBench also emits a
separate document validated by
`protocol/pybench-op-v3-python-runtime.schema.json`. That sidecar records:

- installed distribution identity;
- Python implementation, version, ABI, platform, and executable hash;
- backend;
- declared or undeclared seed status;
- CPU device descriptor.

Neither request, receipt, discovery record, nor sidecar contains an absolute
host path. Required and selected dependencies are probed without installation
or network access. Dynamic Python providers come only from authenticated input
metadata and must match the core-resolved dependency record before dispatch.

## CLI

The installed entry point is `pybench-operation-pack-v3`. It requires
`--manifest`, `--request`, `--stage-root`, `--receipt`, and `--python-runtime`.
Success writes the shared receipt and Python sidecar atomically. A handled
failure writes a schema-valid error receipt when request bindings are available.
Publication remains a separate scworkbench action.
