# pybench

Python runtime for `scworkbench` bundles.

Purpose:
- read bundle data directly (SQLite + payload files)
- run Python-native methods (for example `palantir`, `scvi-tools`)
- write primitive payloads directly to the bundle
- return a compact run receipt to the bench (R engine)

`pybench` does **not** own run registry or provenance in v1. The bench remains the authority for those.

## PeakVI Matrix Path Contract

`pybench` includes native Python matrix getters for bundle matrices.

Current native getter support:
- Python resolves `matrix_id` from the bundle registry
- Python reads canonical BPCells matrix payloads directly
- `peakvi_fit` can use native bundle matrix reads when `matrix_chunks_dir` is
  absent

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

## Development (first-time setup)

### Option A: `uv` (recommended)

```bash
cd /path/to/pybench
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .[dev]
```

### Option B: stdlib `venv` + `pip`

```bash
cd /path/to/pybench
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .[dev]
```

Run tests:

```bash
pytest -q
```

## Planned modules

- `pybench.bundle`: bundle handle / open-close
- `pybench.getters`: core read getters (cells, meta, spaces, sets, native matrices)
- `pybench.artifacts_payload`: payload writers for primitive artifacts
- `pybench.receipts`: success/error run receipts
- `pybench.schema`: bundle contract checks

See `docs/pybench_api_inventory_draft.md` in `scworkbench` for the current API inventory draft.
