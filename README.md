# pybench

Python runtime for `scworkbench` bundles.

Purpose:
- read bundle data directly (SQLite + payload files)
- run Python-native methods (for example `palantir`, `scvi-tools`)
- write primitive payloads directly to the bundle
- return a compact run receipt to the bench (R engine)

`pybench` does **not** own run registry or provenance in v1. The bench remains the authority for those.

## Current Native Matrix Support

`pybench` now includes native Python matrix getters for bundle matrices.

Current implications:
- Python resolves `matrix_id` from the bundle registry
- Python reads canonical BPCells matrix payloads directly
- `peakvi_fit` no longer depends on R-side Matrix Market staging

This is the intended contract direction for Python-native matrix consumers in
`pybench`: symbolic refs in, bundle-native reads inside Python.

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
