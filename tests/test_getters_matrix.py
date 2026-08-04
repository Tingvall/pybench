from pybench import getters


def test_matrix_getter_chunks_large_cell_selection(monkeypatch):
    monkeypatch.setenv("PYBENCH_MATRIX_READ_CHUNK_CELLS", "4")

    calls = []

    def fake_read_chunk(bundle, matrix_relative, feature_idx, col_idx):
        import numpy as np
        from scipy import sparse as sps

        calls.append((matrix_relative, feature_idx, list(col_idx)))
        data = np.arange(3 * len(col_idx), dtype=float).reshape(3, len(col_idx))
        return sps.csc_matrix(data)

    monkeypatch.setattr(getters, "_read_matrix_chunk_subprocess", fake_read_chunk)
    monkeypatch.setattr(
        getters, "_matrix_relative_path", lambda bundle, matrix_id: "mat/fake"
    )
    monkeypatch.setattr(getters, "feature_ids", lambda bundle, matrix_id: ["f1", "f2", "f3"])
    monkeypatch.setattr(
        getters,
        "cells",
        lambda bundle, cell_set="all", dataset_id=None: [f"c{i}" for i in range(10)],
    )

    out, feats, use_cells = getters.matrix(
        bundle=object(),
        matrix_id="ATAC_counts",
        cells_order=[f"c{i}" for i in range(10)],
    )

    assert out.shape == (3, 10)
    assert feats == ["f1", "f2", "f3"]
    assert use_cells == [f"c{i}" for i in range(10)]
    assert [cols for _path, _rows, cols in calls] == [
        [0, 1, 2, 3],
        [4, 5, 6, 7],
        [8, 9],
    ]
