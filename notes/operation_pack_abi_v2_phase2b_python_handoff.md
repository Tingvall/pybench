# READY FOR PHASE 2C

## Revisions and scope

- PyBench base: `d2e0587950973cf7eeacfc46b2829397cf84f079`
- Branch: `feat/operation-pack-abi-v2-phase2b-python-quill-20260914`
- Implementation commit: `52e31384cb2d5ecc69367aeac3cb216696ca1922`
- Compatible scworkbench/Forge checkpoint: `8f61007d1b67f213eb06d1f6944ca33a22fd3227`
- Accepted Phase 1 specification: `eb5334a7bec73490ccfbf106f83ba91b1ab39fb8`
- Coordination authorization: `7ebbc8f7f32055d89af86b2eb38566b3e7e29284`

Work was confined to the isolated PyBench worktree. scworkbench, scientific
operations, bundles, publication state, containers, and remotes were not
modified. No push, merge, tag, SIF build, or release publication was performed.

## Protocol identities

The following files are byte-identical to the Forge checkpoint:

- `operation-pack-v2.schema.json`: `8343f8ea40ce3cc57027b0810c4d05b4df00196edd544361d15dc1fd59fe96aa`
- `operation-pack-transition-request-v2.schema.json`: `81bf35b2827778f149aee3e3ca2e921332f22415a290b5e9c704af37b7e1aed2`
- `operation-pack-transition-receipt-v2.schema.json`: `2cd5e6192daa799bd4562dd1f0d4761eb1a7e8847ff048b3880783a0db0fba1b`
- `fixtures/operation-pack-v2/identities.json`: `4f617d4824c9956c854a13687e71e24ba7a99c17d7d3067cc611918383956bac`

PyBench adds one closed, Python-only sidecar schema without changing the shared
receipt: `pybench-op-v3-python-runtime.schema.json` at
`713a34375b5b0b9168bcc3d477425e298ca0293e8c7d87e24d8f826a663e87d8`.
The shared runtime attestation remains language-neutral. The sidecar records the
installed distribution, interpreter, backend, seed, and device.

Observed canonical identities exactly match Forge:

- Manifest 1: `921228acccc7d7e5cd851fbe24678c9a9698dad617cd9d865aa332f79f21f36a`
- `scwbatac::chromvar`: `5072fbdffb10673ee89e38c433ebb04d956d5930b56d11ddafc086cffdb6647e`
- Manifest 2: `348c724e1494ba31470145dd115879bc9ae13b1c2a82e7189d8e92099f816d83`
- `scwbexample::normalize`: `22e2d011c8ae32a25f0f68a9d5cc0eac9872ff2b78bb40cee68845772bca8bdf`
- `scwbexample::cluster`: `d6953d8277da79b5c63d65d8406cc747e6a3c4bfbb793aa6d49169a2d14dbc9e`

Existing operation-pack v1 files and `pybench-op-v2` behavior are unchanged.
The base commit had updated the v1 canonical identity ledger without updating
its file-digest assertion; the stale expected test hash was corrected to the
unchanged committed ledger digest `5d7dc0f143e8dfee15f4c8b75e741770d818ea3a20dbeb991db2a9277239a1d9`.

## Public Python contract

Entry-point group: `scworkbench.operation_packs.v2`.
Process transport: exact `pybench-op-v3`.
Installed CLI: `pybench-operation-pack-v3`.

Public interfaces:

- `discover_operation_pack_v2(path)`: inert strict parse, schema/semantic
  admission, canonical identity, installed manifest/distribution/entry-point
  authentication; no import or registry mutation.
- `activate_operation_pack_v2(discovery)`: repeat authentication, load the one
  declared callable, validate the complete private handler candidate, and
  atomically publish process-local registrations.
- `operation_pack_handlers_v2(mapping)`: construct exact v2 handler bindings.
- `active_operation_packs_v2()` / `unload_operation_pack_v2(namespace)`:
  inspect or remove process-local registrations.
- `validate_request_v3()` / `validate_receipt_v3()`: validate shared documents,
  self-identities, authentication, grants, and exact closure.
- `execute_operation_pack_v3()`: run one active handler against the private
  capability stage and return the shared receipt plus Python runtime sidecar.

Handlers have the exact signature `handler(readers, writers, params, context)`.
Readers cover all 11 accepted kinds through seven named reader methods. Writers
cover all eight sanctioned methods: matrix, space, 2D space, contained model,
terminal primitive, 2D path family, space path family, and cell-set admission.
The synthetic fixture exercises all 15 output tuples and all 10 storage
profiles in one mixed operation, plus a dynamic-provider operation.

## Capability, audit, and rollback design

The request contains the core-issued private capability. Python validates its
session, run, generation, source state, lease, pack/distribution/source/
definition/entry-point identities, selected dependencies, preflight report,
reader grants, writer grants, and self-identity. Capability use is process-local
and single-use. The frozen schema has no clock timestamp: expired authority is
represented by stale lease/generation/session/run/source/capability binding.

Handlers receive no bundle path, SQLite connection, registry/provenance handle,
or final publication path. Inputs are projected typed payloads. Outputs are
limited to granted relative roots. The executor re-reads staged bytes after the
handler, verifies type-specific dimensions/order/linkage/containment and
semantic identities, audits changed files and created directories, constructs
the mutation manifest, and validates the complete receipt. Python never claims
publication or promotion.

Handled exceptions, interrupts, output faults, receipt faults, symlinks, and
undeclared mutation restore the exact stage preimage. A hard-killed subprocess
cannot run Python cleanup; it emits no receipt and no output when terminated
before its writer, after which Forge/core owns private-generation rollback.

## Acceptance-to-test mapping

`tests/test_operation_pack_v2.py` contains 55 passing focused cases:

- shared schema bytes, v2 ledger, independent-process identity: protocol parity;
- 66 invalid input tuples, 345 invalid output tuples, 44 input and 105 output
  required-field removals: exact outer-schema tuple profile;
- all nine frozen semantic recipes, duplicate raw keys, embedded schema faults:
  ordered manifest admission;
- inert discovery, source/distribution/entry-point reauthentication, duplicate
  entry points, atomic multi-operation faults: installed-pack trust boundary;
- all readers, writers, profiles, mixed output closure, runtime sidecar:
  synthetic success lifecycle;
- missing, malformed, unavailable, and changed dynamic providers: dependency
  preflight before analytical handler dispatch;
- stale/mismatched and replayed capabilities, runtime faults and interrupts:
  lease and single-use confinement;
- symlinks, input writes, empty directories, sibling files, and post-writer
  payload rewrites: independent mutation audit and exact rollback;
- incomplete, duplicate, extra, malformed, and identity-mismatched receipts:
  receipt closure;
- v1/v2/v3 confusion: strict dispatch separation;
- subprocess success, duplicate-key request, timeout, and hard exit: installed
  CLI boundary;
- Forge R validation of generated Python success and error receipts: shared
  schema interpretation without Python reinterpretation.

The shared schemas contain 24 fixed negative recipes in addition to exhaustive
cross-products. All fixed recipes are executed directly or subsumed by the
explicit exhaustive tuple/removal tests.

## Qualification results

Focused contract:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_operation_pack_v2.py
55 passed in 16.71s
```

Complete PyBench suite:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q
206 passed, 3 skipped in 44.20s
```

The three skips are existing optional-environment tests. Ruff and repository
checks:

```text
.venv/bin/python -m ruff check .
All checks passed!
.venv/bin/python -m ruff format --check .
51 files already formatted
git diff --check
[exit 0]
```

With `SOURCE_DATE_EPOCH=1789344000`, two independent builds compared
byte-for-byte. PyBench wheel SHA-256 was
`96049d6311c8a02073f3c75cf8252adc3bc81dfe66f383d6fe28a14227102dd4`;
the implementation-tree sdist SHA-256 was
`d6acd27cca0923c0cbf417a406b67ade601ba78a0a390013e3cc47f4894f86b8`.
The synthetic fixture wheel and sdist were also deterministic at
`9c3333a98a02af58be9e11556f0b1a5dfec908e73ded5cdeb0cedb7d8ed7fbb8`
and `0a8f7d9b772fde43bfaad3d3640e71723799176ad1adb5cce57a41e4088433b8`.

A fresh Python 3.11 environment installed both wheels with `--no-index
--no-deps`, using a copied offline closure of the already qualified dependency
environment. The installed PyBench module resolved from the fresh wheel;
explicit reference-pack discovery, activation, and handler invocation passed;
`pybench-operation-pack-v3 --help` passed; and `pip check` reported `No broken
requirements found.` An initial `/usr` system-package attempt was rejected by
qualification because it exposed `jsonschema 3.2.0` and missing dependencies;
it was not used as acceptance evidence.

Final archive scans covered both wheels and sdists: no absolute members, parent
traversal, `.git` content, private keys, credential patterns, or personal host
paths were found. All 24 repository JSON files parsed, the repository has zero
CSV files, local Markdown links passed, and all four shared files compared
byte-for-byte to Forge. No fullstack, SIF, scientific lifecycle, or network
installation was run, as prohibited or unnecessary for Phase 2B.

## Changed-file inventory

- `README.md`, `docs/operation_pack_abi_v2.md`, `pyproject.toml`
- `protocol/operation-pack-v2.schema.json`
- `protocol/operation-pack-transition-request-v2.schema.json`
- `protocol/operation-pack-transition-receipt-v2.schema.json`
- `protocol/fixtures/operation-pack-v2/identities.json`
- `protocol/pybench-op-v3-python-runtime.schema.json`
- `src/pybench/__init__.py`
- `src/pybench/operation_packs_v2.py`
- `src/pybench/protocol_v3.py`
- `src/pybench/operation_pack_v3_cli.py`
- four files under `tests/fixtures/operation-pack-v2-python-reference/`
- `tests/test_operation_pack_v2.py`, `tests/v3_helpers.py`
- `tests/test_operation_packs.py` (stale base digest assertion only)
- this handoff under `notes/`

## Compatibility matrix and Phase 2C order

- ABI v1 + `pybench-op-v2`: unchanged and passing.
- ABI v2 Python + `pybench-op-v3`: implemented and passing synthetic reference
  qualification.
- ABI v2 R/core at Forge `8f61007...`: shared schema, identity, and receipt
  validation compatible.
- Production scientific v2 packs: none; no scientific readiness claim.
- Durable publication/reopen/promotion: remains Forge/core-owned and must be
  exercised only in Phase 2C composite integration.

Phase 2C should first integrate Forge checkpoint `8f61007...`, then this PyBench
implementation commit `52e3138...`, retain byte-identical shared assets, wire
the core subprocess invocation to `pybench-operation-pack-v3`, and run the real
core transition lifecycle through publication/reopen. Only after composite
request/receipt/runtime-sidecar reconciliation should Forge remove or relocate
`py_protocol_hello`. Do not reinterpret the Python sidecar as a shared receipt
extension and do not move publication authority into PyBench.

Residual limitation: process-local capability replay detection cannot survive
replacement of the Python process; durable replay prevention remains the
core-owned transition journal and lease/generation validation. This is the
accepted ownership split, not a protocol mismatch.
