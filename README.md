# pybench

Python runtime for `scworkbench` bundles.

## Current Pack-Authoring Route

New Python operation packs use the bounded ABI-v3 SDK and `pybench-op-v4`.
Start with the installed [authoring guide](src/pybench/sdk_authoring.md), not
the historical bridge described below. The simple author model is declare
typed inputs/outputs, read issued primitives, compute, and write primitives;
the R engine owns source validation, lineage, receipts, and atomic publication.

```python
from importlib.resources import files
print(files("pybench").joinpath("sdk_authoring.md").read_text())
```

Scaffolding is inert: it installs and activates nothing. Linux atomic
no-replace support is required; unsupported publication fails closed. Installed
pack code and serialized models are trusted, not sandboxed. Check the guide's
policy-level support table: shared schemas do not establish executable Python
support for advanced R grants.

The seven-pack standard profile consists of R packs. Their private Python
computation is not Python-pack activation. A bundle recipient can inspect
promoted output without a pack, but execution/replay needs the compatible
activated pack and runtime. Package versions alone do not identify the exact
installed instance.

The older bridge and pack versions remain explicit compatibility contracts;
they are not templates to copy into a new SDK pack. ABI-v2 packs use
`pybench-op-v3`; ABI-v3 Python packs use `pybench-op-v4`.

The generic graph ABI4/process5 implementation is under joint qualification;
see the [candidate graph authoring contract](docs/graph_abi4_candidate.md).
It does not replace the current ABI-v3 authoring route or establish cluster or
publication readiness on its own.

Purpose:
- read bundle data directly (SQLite + payload files)
- run Python-native methods (for example `palantir`, `scvi-tools`)
- write primitive payloads only inside an engine-owned isolated session stage
- return a compact run receipt to the bench (R engine)

`pybench` does **not** own the run registry or provenance. The R engine remains
the authority and audits every isolated Python transition before publication.

## Historical Engine Bridge: Operation Protocol v2

Writable operation protocol v1 is disabled. `pybench-op-v2` requires an
explicit execution capability bound to one session, preallocated run, source
attestation, staging-root identity, checkpoint generation, and output
allowlist. The canonical schema and fixtures are vendored byte-for-byte under
`protocol/`; their authoritative Forge schema SHA-256 is
`d5e80b58e4f18b37d0e906e7dac127fd17fbb88ea56dd83926406691303b655b`
(scworkbench commit `be9f006675891c65701b0e926ec4dc1c2fa141fe`, with stage-marker follow-up
`19ef32b`).

Bundle opens use one of two modes:

- `read_only` uses SQLite URI `mode=ro` plus `PRAGMA query_only=ON`; shared
  writers fail before mutation and SQLite cannot create WAL/SHM sidecars.
- `staging_write` requires `.scworkbench-pybench-stage.json` to echo the exact
  capability/session/run/source/root/checkpoint binding before the writable
  connection opens. SQLite authorizers deny DDL, undeclared tables, and key
  values outside the allowlist. Payload resolution rejects absolute paths,
  traversal, and symlink escape.

Shared writers journal only successful SQLite and atomic payload publication.
Successful receipts include final row counts and canonical content SHA-256
descriptors for every journaled mutation. The R engine independently derives
the pre/post mutation set and rejects any mismatch before publishing the
transition. Error receipts echo the capability binding but redact the runtime
bundle path.

SQLite row identities use `scworkbench-sqlite-rows-v2`: columns are sorted by
name; values use explicit type tags, `printf('%lld')` integers, big-endian
IEEE-754 binary64 hex for reals, and hexadecimal text/blob bytes. Rows are
sorted by those typed strings, then UTF-8 values are hashed in length-prefixed framing. This keeps numeric,
NULL, text, and blob identities stable across R and Python SQLite versions.
Payload allowances accept the exact declared stem,
its descendants, or a same-parent `.<safe-extension>` file; underscore/dash
sibling IDs are not included.

Tests that need the real subprocess and writer lifecycle without expensive
model training may set `PYBENCH_TEST_MOCK_TRAINING=1`. This hook runs after v2
and stage validation and uses the real space, artifact, model-tree, journal,
and receipt APIs; it replaces only scientific model computation.

## Bundle path and model trust contract

PyBench enforces the engine-owned `scworkbench-bundle-path-v1` contract.
Every `pybench-op-v2` request must contain this exact record:

```json
{
  "version": "scworkbench-bundle-path-v1",
  "durable_paths": "bundle_relative_contained",
  "external_inputs": "explicit_import_only",
  "serialized_models": "trusted_provenance_runtime_or_explicit_opt_in"
}
```

Durable manifest, SQLite, matrix, materialization, space-manifest, and model
paths are nonempty bundle-relative values. Absolute paths, parent traversal,
missing or wrong-type payloads, and top-level or nested symlink escape are
rejected before payload bytes are read. Absolute bundle and explicitly staged
input roots exist only as operation runtime context and are never persisted.

Fit operations stage model output, atomically move it into `artifact_models/`,
and publish only its relative path. Project operations accept that relative
path and resolve it against the exact request bundle; they do not probe an
external path or recover a stale path by basename. Serialized state is loaded
only after the operation request and containment contract have both passed.
Opening or inspecting a bundle never deserializes model bytes.

Legacy absolute model references have one explicit operator route:

```bash
python -m pybench.tools.import_legacy_model \
  --bundle-dir /path/to/bundle \
  --artifact-id model_artifact_id \
  --source /operator/selected/legacy/model
```

The command copies the source into bundle-owned storage, records its external
source and SHA-256 in artifact provenance, and transactionally replaces the
registry value with the contained relative path. Normal opening and project
operations never perform this import.

## PeakVI Matrix Path Contract

`pybench` includes native Python matrix getters for bundle matrices.

Current native getter support:
- Python resolves `matrix_id` from the bundle registry
- Python reads canonical BPCells matrix payloads directly
- Python space getters preserve an explicit requested cell order; when no
  order is supplied they use canonical all-bundle order restricted to cells
  present in the space
- `peakvi_fit` can use native bundle matrix reads when `matrix_chunks_dir` is
  absent

Count ingest requires finite, nonnegative integer-valued data by default and
stores BPCells values as `uint32`. Double-valued counts are accepted only via
the explicit experimental `require_integer=False` ingest option. Python-native
BPCells reads preserve the stored numeric type; the cluster-safe staged Matrix
Market route remains the supported production path for scVI/PeakVI.

Production PeakVI on cluster should use R-staged sparse chunks until native
Python BPCells reads are revalidated on the cluster runtime. Historical context:
native Python reads were pursued after upstream Python BPCells could not handle
double matrices; a fork fixed double-matrix reads locally, but the native/forked
path later hit segmentation faults on Tetralith.

Current PeakVI contract:
- `peakvi_fit` supports staged sparse chunks when `matrix_chunks_dir` is
  supplied; this is the production-safe cluster path.
- `peakvi_fit` supports native bundle matrix reads when `matrix_chunks_dir` is
  absent; this path is local/development capable, not the current cluster
  production default.
- `peakvi_project` currently requires staged chunks.
- Promoting native BPCells reads back to production requires a cluster smoke that
  exercises the forked/double-matrix BPCells path without segmentation faults.

## Development and release checks

The canonical core developer/test profile is Python 3.11 with the `dev` and
`ingest` extras. `ingest` is required because the core suite exercises AnnData
ingest and staged sparse-matrix helpers.

```bash
cd "$(git rev-parse --show-toplevel)"
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,ingest]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

The optional model profiles build on the core profile:

```bash
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python -m pip install -e '.[dev,ingest,scvi]'      # scVI and PeakVI
.venv/bin/python -m pip install -e '.[dev,ingest,palantir]'  # Palantir
```

`ruff check src tests` enforces syntax/import correctness. The project
intentionally does not enforce `E501`: long SQL statements and compact SQLite
fixture DDL are schema contracts where wrapping does not improve reviewability.

Three optional cross-repository tests use explicit external fixture contracts:

- `PYBENCH_GOLDEN_BUNDLE` names the generated scworkbench golden bundle.
  `PYBENCH_GOLDEN_BUNDLE_SHA256` must contain its lowercase
  `pybench-external-directory-sha256-v1` identity.
- `PYBENCH_SCWORKBENCH_PROTOCOL_DIR` names the pinned scworkbench
  `inst/protocol` directory used for byte-identity checks. It must resolve to
  the exact `inst/protocol` child of `PYBENCH_SCWORKBENCH_SOURCE_DIR`.
- `PYBENCH_SCWORKBENCH_SOURCE_DIR` names the pinned scworkbench source checkout
  used by the Python path-contract tripwire. It must contain
  `R/python_protocol.R`, `R/path_containment.R`, and `R/op_python_bridge.R`.

Compute and supply the golden identity, then run the complete core suite:

```bash
export PYBENCH_GOLDEN_BUNDLE="${GOLDEN_BUNDLE_DIR}"
golden_identity="$(PYTHONPATH=tests .venv/bin/python -m external_contracts \
  "$PYBENCH_GOLDEN_BUNDLE")"
export PYBENCH_GOLDEN_BUNDLE_SHA256="${golden_identity#*:}"
export PYBENCH_SCWORKBENCH_PROTOCOL_DIR="${SCWORKBENCH_PROTOCOL_DIR}"
export PYBENCH_SCWORKBENCH_SOURCE_DIR="${SCWORKBENCH_SOURCE_DIR}"
.venv/bin/python -m pytest -q
```

If a path variable is absent, only its external contract test is explicitly
skipped. A configured missing path, malformed or mismatched golden identity, or
protocol inventory/byte mismatch is a test failure. A configured scworkbench
source checkout that lacks any required R source file also fails explicitly.
Tests never search a developer filesystem, infer a sibling checkout, or compare
PyBench protocol files with themselves. Direct, aliased, copied-but-unbound, or
internally symlinked protocol trees are rejected before byte comparison.

Release artifacts must be built from an immutable Git archive extracted away
from any checkout or linked-worktree metadata:

```bash
commit="$(git rev-parse HEAD)"
release_root="$(mktemp -d)"
source_archive="$release_root/pybench-$commit.tar"
git archive --format=tar --prefix="pybench-$commit/" "$commit" > "$source_archive"
sha256sum "$source_archive"
tar -tf "$source_archive"
tar -xf "$source_archive" -C "$release_root"
source_root="$release_root/pybench-$commit"
test ! -e "$source_root/.git"
(cd "$source_root" && python3.11 -m build --outdir "$release_root/dist")
python3.11 -m zipfile -l "$release_root"/dist/*.whl
tar -tzf "$release_root"/dist/*.tar.gz
```

Qualification must retain the archive listings and recursively scan the Git
archive, extracted source, wheel, and sdist for `.git` members, personal
absolute paths, credentials, private keys, and undeclared payloads before an
artifact is accepted.

## Existing ABI-v1/v2 Python Pack Contracts

This section documents earlier runtime seams. New ABI-v3 authoring uses the
SDK guide linked above and `pybench-op-v4`; do not substitute these older CLI
commands for that contract.

PyBench implements the Python-owned operation-pack v1 runtime seam. Installation
is inert. Discovery and activation are explicit trust decisions:

```bash
pybench-operation-pack discover --manifest /installed/pack/operation-pack.json
pybench-operation-pack execute \
  --manifest /installed/pack/operation-pack.json \
  --request request.json --receipt receipt.json \
  --runtime-attestation runtime.json \
  --expected-pack-sha256 PACK_SHA256 \
  --expected-definition-sha256 DEFINITION_SHA256 \
  --expected-distribution-sha256 INSTALLED_CONTENT_SHA256
```

Discovery resolves only `manifest.package.name`, compares distribution names by
PEP 503 normalization and versions by PEP 440 equivalence, and authenticates
exactly one entry point in `scworkbench.operation_packs.v1` whose name equals
`manifest.entry_point`. The supplied manifest must be the distribution's unique
packaged `operation-pack.json`; discovery records only its distribution-relative
path plus raw and canonical SHA-256 identities. It does not import pack code.
Activation loads only that installed metadata target and requires it to return
handler bindings matching the manifest operation names exactly.

The manifest owns scientific definitions. Identity uses
`scworkbench.operation_pack.canonical_json.v1`, whose UTF-8 ordering, escaping,
restricted decimal normalization, defaults, and identity field sets are
specified in `protocol/operation-pack-canonical-json-v1.md`. PyBench implements
that byte contract independently and uses it for manifest, definition, pack,
and dependency-set identities. Python code supplies runtime handlers only.
Handlers receive `OperationPackContext`, with
typed `read_cell_vector()`, `parameter()`, `write_cell_vector()`, and
`write_obs_table()` methods. They receive no public SQLite connection, arbitrary
payload writer, registry API, promoter, custom primitive, or validation hook.
Execution re-resolves and re-hashes the packaged manifest, definitions, pack,
distribution, entry point, interpreter, dependency set, and protocol before
handler registration. It then opens only the engine-created v2 stage, delegates
writes to existing capability-bound writers, builds and validates the ordinary
v2 receipt, and emits the frozen Python runtime attestation sidecar. Discovery
and runtime records contain no absolute host paths.

Installed artifact identity uses
`python-installed-distribution-tree-sha256-v1`: sorted distribution-relative
regular files (excluding bytecode caches) are framed with their individual
SHA-256 values and hashed. This truthfully identifies installed contents when
original wheel bytes are unavailable. Dependency identity uses the canonical
sorted installed distribution set under `python-distribution-set-v1`.

The runtime attestation's nullable `pybench.source_revision` is derived only
from metadata bound to the installed PyBench distribution. A PEP 610 VCS
`commit_id` is accepted directly. For an editable local install, PyBench
verifies that the declared `file://` origin contains the loaded module, names
the PyBench distribution, and is itself the Git root before reading `HEAD`.
Ordinary wheel and archive installs without bound revision evidence report
`null`; enclosing repositories and the current working directory are never
used as source identity. Installed-content, interpreter, dependency, protocol,
pack, and entry-point attestations remain available when the revision is
unknown.

Trusted pack code is not host-sandboxed. Capability enforcement governs
scientific-state mutation, and conformance is not scientific validation. V1
packs cannot add primitives, storage formats, getters, promotion functions, or
provenance/validation hooks. A recipient can inspect retained promoted outputs
without the pack; replay and continuation retain scworkbench's
`OPERATION_PACK_REQUIRED` behavior until the exact compatible pack is installed
and explicitly activated.

Operation-pack ABI v2 is an earlier supported contract with a synthetic Python
reference implementation. The standard profile's Statistics pack uses the R
ABI-v2 runtime; this is not Python scientific-pack qualification. Python v2
packs declare one exact entry point in
`scworkbench.operation_packs.v2`; installation and discovery remain inert, and
activation is explicit and process-local. ABI v2 executes only through
`pybench-op-v3`:

```bash
pybench-operation-pack-v3 \
  --manifest /installed/pack/operation-pack.json \
  --request request.json \
  --stage-root /private/core-created-stage \
  --receipt receipt.json \
  --python-runtime python-runtime.json
```

The three shared v2 manifest/request/receipt schemas under `protocol/` are
byte-identical mirrors of the scworkbench authority. The shared receipt carries
language-neutral runtime evidence. The separate closed
`pybench.operation_pack.python_runtime.v3` sidecar records the installed Python
distribution, interpreter, backend, seed, and device without changing the
shared schema. Python writes only granted private staged outputs and never
publishes bundle state. See `docs/operation_pack_abi_v2.md` for the developer
API and capability matrix. No official scientific v2 pack is provided.

## scVI Fit Summary

`scvi_train` writes a one-row `scvi_fit_summary` `obs_table` alongside its
model reference. It retains the input, architecture, covariate, runtime, and
version fields, and records structured training evidence without parsing logs:

- `final_train_loss` and `final_validation_loss`: final finite values when
  scvi-tools exposes them;
- `*_status`: `available`, `unavailable`, or `extraction_failed`;
- `*_metric` and `*_source`: selected scvi-tools metric key and structured
  source (`model.history` or `model.trainer.callback_metrics`);
- `training_epochs_completed` with status/source fields;
- `early_stopping_outcome`, callback monitor, best score, stopped epoch,
  wait count, patience, and source fields.

The summary uses `training_evidence_schema = "scvi_training_evidence_v1"`.
Missing metrics are normal for runtimes that do not retain a requested metric;
`extraction_failed` means a candidate metric was present but could not be
converted to a finite scalar. Existing summaries remain valid and simply do
not contain the added fields.

## Authoring Boundary

Use the installed SDK guide and generator for new Python packs. Handlers consume
issued typed readers and produce primitives through issued writers. Discovery
and execution authenticate the installed distribution; core owns committed
state and publication. Do not infer support for an advanced R grant from shared
schema parity: the SDK capability matrix records executable Python coverage
and fail-closed unsupported paths.

## License

MIT. See `LICENSE`.
