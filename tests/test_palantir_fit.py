from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from capability_helpers import payload_allowance, sqlite_allowance, v2_request
from pybench.ops import palantir_fit


def test_palantir_fit_persists_relative_contained_model_path(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db" / "app.sqlite")
    con.executescript(
        """
        CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE artifact_model_ref (
          artifact_id TEXT PRIMARY KEY, engine TEXT, model_space_id TEXT,
          in_space_id TEXT, model_type TEXT, model_path TEXT
        );
        INSERT INTO artifacts VALUES ('palantir_model', 'model_ref');
        """
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    (root / "artifact_models").mkdir()

    cells = ["c1", "c2", "c3"]
    fit_df = pd.DataFrame({"cell_id": cells, "d1": [0.0, 1.0, 2.0], "d2": [2.0, 1.0, 0.0]})
    result = SimpleNamespace(
        pseudotime=pd.Series([0.0, 0.5, 1.0], index=cells),
        branch_probs=pd.DataFrame(index=cells),
        entropy=None,
    )
    monkeypatch.setattr(palantir_fit, "get_cells", lambda *_args, **_kwargs: cells)
    monkeypatch.setattr(palantir_fit, "get_space", lambda *_args, **_kwargs: fit_df)
    monkeypatch.setattr(
        palantir_fit.palantir.utils,
        "run_diffusion_maps",
        lambda *_args, **_kwargs: np.ones((3, 2)),
    )
    monkeypatch.setattr(
        palantir_fit.palantir.utils,
        "determine_multiscale_space",
        lambda *_args, **_kwargs: np.ones((3, 2)),
    )
    monkeypatch.setattr(
        palantir_fit.palantir.core,
        "run_palantir",
        lambda *_args, **_kwargs: result,
    )
    outputs = {"model": {"artifact_id": "palantir_model", "type": "model_ref"}}
    request = v2_request(
        root,
        op_name="palantir_fit",
        run_id="fit-relative",
        inputs={"fit_space": "fit", "cell_ids": cells, "root_cell": "c1"},
        params={},
        outputs=outputs,
        allowances=[
            sqlite_allowance(
                "model", "palantir_model", "model_ref", "artifact_model_ref", "artifact_id"
            ),
            payload_allowance(
                "model", "palantir_model", "model_ref", "artifact_models/palantir_model"
            ),
        ],
    )
    receipt = palantir_fit._run(request)
    assert receipt["status"] == "ok"
    con = sqlite3.connect(root / "db" / "app.sqlite")
    try:
        stored = con.execute("SELECT model_path FROM artifact_model_ref").fetchone()[0]
    finally:
        con.close()
    assert stored == "artifact_models/palantir_model.pkl"
    assert not Path(stored).is_absolute()
    assert (root / stored).is_file()
