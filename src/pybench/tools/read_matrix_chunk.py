from __future__ import annotations

import argparse

import numpy as np
from scipy import sparse as sps

from pybench.bundle import resolve_bundle_path


def _load_idx(path: str | None) -> list[int] | None:
    if not path:
        return None
    arr = np.load(path)
    return [int(x) for x in np.asarray(arr, dtype=np.int64).tolist()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Read one BPCells matrix chunk into a safe sparse npz")
    ap.add_argument("--bundle-dir", required=True)
    ap.add_argument("--matrix-path", required=True)
    ap.add_argument("--cols-path", required=True)
    ap.add_argument("--out-path", required=True)
    ap.add_argument("--rows-path")
    args = ap.parse_args()

    import bpcells.experimental as bp  # type: ignore

    rows = _load_idx(args.rows_path)
    cols = _load_idx(args.cols_path)
    if cols is None or not cols:
        raise SystemExit("cols-path must contain at least one column index")

    matrix_dir = resolve_bundle_path(
        args.bundle_dir,
        args.matrix_path,
        expected_type="directory",
        inspect_tree=True,
        purpose="matrix payload",
    )
    mat = bp.DirMatrix(str(matrix_dir))
    key_rows = slice(None) if rows is None else rows
    out = mat[key_rows, cols]
    if sps.issparse(out):
        out = out.tocsc()
    else:
        out = sps.csc_matrix(out)
    sps.save_npz(args.out_path, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
