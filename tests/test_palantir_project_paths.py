from __future__ import annotations

import json
import pickle
import shutil
import sqlite3
from pathlib import Path

import pytest

from pybench.bundle import BundlePathError, close_bundle, open_bundle
from pybench.ops.palantir_project import _load_palantir_model_payload
from pybench.protocol import PATH_CONTRACT, trusted_model_path


def test_palantir_project_loads_moved_contained_model_only_through_trusted_request(
    tmp_path: Path,
):
    source = tmp_path / "source.bundle"
    (source / "db").mkdir(parents=True)
    sqlite3.connect(source / "db" / "app.sqlite").close()
    (source / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    relative = "artifact_models/palantir.pkl"
    (source / "artifact_models").mkdir()
    with (source / relative).open("wb") as handle:
        pickle.dump({"engine": "palantir", "presults": "safe"}, handle)
    moved = tmp_path / "new-parent" / "portable.bundle"
    moved.parent.mkdir()
    shutil.copytree(source, moved)

    request = {
        "protocol_version": "pybench-op-v1",
        "path_contract": dict(PATH_CONTRACT),
        "bundle_dir": str(moved),
        "op_name": "palantir_project",
        "run": {"run_id": "moved-palantir"},
        "inputs": {"model_path": relative, "plot_space": "umap"},
        "params": {},
        "outputs": {},
    }
    bundle = open_bundle(moved)
    try:
        trusted = trusted_model_path(request, bundle, relative, expected_type="file")
        payload = _load_palantir_model_payload(trusted)
        assert payload["presults"] == "safe"
        assert trusted.absolute == (moved / relative).resolve()

        with pytest.raises(BundlePathError, match="PATH_ABSOLUTE"):
            trusted_model_path(
                request, bundle, str(source / relative), expected_type="file"
            )
    finally:
        close_bundle(bundle)
