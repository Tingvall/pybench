# pybench

Python runtime for `scworkbench` bundles.

Purpose:
- read bundle data directly (SQLite + payload files)
- run Python-native methods (for example `palantir`, `scvi-tools`)
- write primitive payloads directly to the bundle
- return a compact run receipt to the bench (R engine)

`pybench` does **not** own run registry or provenance in v1. The bench remains the authority for those.

## Bundle path and model trust contract

PyBench enforces the engine-owned `scworkbench-bundle-path-v1` contract. Every
`pybench-op-v1` request must contain this exact record:

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
cd /home/dirk/Projects/pybench
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,ingest]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

The optional model profiles build on the core profile:

```bash
cd /home/dirk/Projects/pybench
.venv/bin/python -m pip install -e '.[dev,ingest,scvi]'      # scVI and PeakVI
.venv/bin/python -m pip install -e '.[dev,ingest,palantir]'  # Palantir
```

`ruff check src tests` enforces syntax/import correctness. The project
intentionally does not enforce `E501`: long SQL statements and compact SQLite
fixture DDL are schema contracts where wrapping does not improve reviewability.

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

## Planned modules

- `pybench.bundle`: bundle handle / open-close
- `pybench.getters`: core read getters (cells, meta, spaces, sets, native matrices)
- `pybench.artifacts_payload`: payload writers for primitive artifacts
- `pybench.receipts`: success/error run receipts
- `pybench.schema`: bundle contract checks

See `docs/pybench_api_inventory_draft.md` in `scworkbench` for the current API inventory draft.

## License

MIT. See `LICENSE`.
