"""Typed tables retain the R contract through public getters and scoped writes."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import subprocess
from pathlib import Path

import pytest

from capability_helpers import raw_write_bundle, sqlite_allowance
from pybench import artifact_table, close_bundle, open_bundle
from pybench.artifacts_payload import PayloadWriteError, write_artifact_table_payload
from pybench.capability import CapabilityError, authorize_sqlite

FORMAT = "scworkbench.table.typed-json.v1"


def storage(names=("value", "label"), types=("double", "character")):
    return {"format": FORMAT, "columns": {"names": list(names), "types": list(types)}}


def fixture_bundle(root, marker=True):
    (root / "db").mkdir(parents=True)
    con = sqlite3.connect(root / "db/app.sqlite")
    con.executescript("""
        CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY,type TEXT,provenance_json TEXT);
        CREATE TABLE artifact_tables(artifact_id TEXT PRIMARY KEY,table_type TEXT,entity_kind TEXT,meta_json TEXT);
        CREATE TABLE artifact_table_rows(artifact_id TEXT,row_idx INTEGER,row_json TEXT,PRIMARY KEY(artifact_id,row_idx));
    """)
    con.execute(
        "INSERT INTO artifacts VALUES(?,?,?)",
        ("table", "obs_table", json.dumps({"table_storage": FORMAT} if marker else {})),
    )
    con.commit()
    con.close()
    (root / "manifest.json").write_text(
        json.dumps({"storage": {"sqlite": {"path": "db/app.sqlite"}}})
    )
    allowances = [
        sqlite_allowance("table", "table", "obs_table", t, "artifact_id")
        for t in ("artifact_tables", "artifact_table_rows")
    ]
    return raw_write_bundle(root, allowances, enforce=True)


def write(bundle, rows, declaration=None, meta=None):
    return write_artifact_table_payload(
        bundle,
        "table",
        artifact_type="obs_table",
        table_rows=rows,
        table_storage=declaration,
        meta=meta,
    )


@pytest.fixture(scope="module")
def r_fixture(tmp_path_factory):
    library = os.environ.get("SCWB_TYPED_R_LIBRARY")
    if not library:
        pytest.skip("SCWB_TYPED_R_LIBRARY required for actual R-to-Python publication fixture")
    root = tmp_path_factory.mktemp("r_typed_fixture")
    script = Path(__file__).parent / "support/typed_table_r_fixture.R"
    result = subprocess.run(
        [
            "Rscript",
            "--vanilla",
            str(script),
            library,
            str(root / "bundle"),
            str(root / "ids.json"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return root / "bundle", json.loads((root / "ids.json").read_text())


def test_public_reader_of_real_r_publication(r_fixture):
    root, ids = r_fixture
    bundle = open_bundle(root)
    try:
        table = artifact_table(bundle, ids["typed"])
        assert table.types == (
            "character",
            "double",
            "integer",
            "logical",
            "character",
            "double",
            "integer",
            "logical",
            "character",
        )
        values = [
            0.0499545077,
            math.nextafter(0.05, -math.inf),
            0.05,
            math.nextafter(0.05, math.inf),
            1e-50,
            1e-300,
            0.0,
            -0.0,
            math.pi,
            float.fromhex("0x1.fffffffffffffp+1023"),
            float.fromhex("0x1p-1022"),
            5e-324,
        ]
        assert [struct.pack("=d", r["value"]) for r in table.rows[:12]] == [
            struct.pack("=d", v) for v in values
        ]
        assert table.rows[12]["value"] is None
        assert math.isnan(table.rows[13]["value"])
        assert table.rows[14]["value"] == math.inf
        assert table.rows[15]["value"] == -math.inf
        assert table.rows[3]["label"] == "雪:-0,}"
        features = artifact_table(bundle, ids["features"])
        assert features.kind == "feature_table" and features.types == table.types
        assert [struct.pack("=d", r["value"]) for r in features.rows[:12]] == [
            struct.pack("=d", v) for v in values
        ]
        assert all(r["missing_double"] is None and r["missing_integer"] is None for r in table.rows)
        empty = artifact_table(bundle, ids["empty"])
        assert empty.rows == [] and empty.columns == table.columns and empty.types == table.types
        projected = artifact_table(bundle, ids["projection"])
        assert projected.types == ("legacy", "legacy")
        assert projected.rows == [
            {"group": "a", "day": "2020-01-01"},
            {"group": "b", "day": "2020-01-02"},
        ]
        legacy = artifact_table(bundle, ids["legacy"])
        assert legacy.storage_format is None and legacy.rows == [{"value": 0.05}]
        assert legacy.meta["meta"]["label"] == "legacy"
    finally:
        close_bundle(bundle)


def test_scoped_writer_roundtrip_empty_types_and_nonfinite(tmp_path):
    bundle = fixture_bundle(tmp_path)
    try:
        rows = [
            {"value": v, "label": s}
            for v, s in [
                (-0.0, "-0"),
                (None, "NA"),
                (math.nan, "NaN"),
                (math.inf, "Inf"),
                (-math.inf, None),
            ]
        ]
        write(bundle, rows, storage())
        actual = artifact_table(bundle, "table")
        assert struct.pack("=d", actual.rows[0]["value"]) == struct.pack("=d", -0.0)
        assert actual.rows[1]["value"] is None and actual.rows[1]["label"] == "NA"
        assert math.isnan(actual.rows[2]["value"])
        assert actual.rows[3]["value"] == math.inf and actual.rows[4]["value"] == -math.inf
        write(bundle, [], storage())
        empty = artifact_table(bundle, "table")
        assert empty.rows == [] and empty.types == ("double", "character")
        with pytest.raises(CapabilityError, match="OUT_OF_SCOPE"):
            authorize_sqlite(bundle, "artifacts", "artifact_id", "table")
    finally:
        close_bundle(bundle)


@pytest.mark.parametrize("bad", [True, 1, "1", [], {}])
def test_bad_double_rolls_back_already_flushed_rows(tmp_path, bad):
    bundle = fixture_bundle(tmp_path)
    try:
        write(bundle, [{"value": 0.0499545077, "label": "original"}], storage())
        before = bundle.con.execute("SELECT row_json FROM artifact_table_rows").fetchall()
        rows = [{"value": 0.1, "label": "valid"}] * 1001 + [{"value": bad, "label": "bad"}]
        with pytest.raises(PayloadWriteError):
            write(bundle, rows, storage())
        assert bundle.con.execute("SELECT row_json FROM artifact_table_rows").fetchall() == before
        assert artifact_table(bundle, "table").rows == [
            {"value": 0.0499545077, "label": "original"}
        ]
    finally:
        close_bundle(bundle)


@pytest.mark.parametrize(
    "names,types",
    [(["x", "x"], ["double", "double"]), (["x"], []), ([""], ["double"]), (["x"], ["unknown"])],
)
def test_bad_declaration_rejected_before_mutation(tmp_path, names, types):
    bundle = fixture_bundle(tmp_path)
    try:
        with pytest.raises(PayloadWriteError):
            write(bundle, [], storage(names, types))
        assert bundle.con.execute("SELECT COUNT(*) FROM artifact_tables").fetchone()[0] == 0
    finally:
        close_bundle(bundle)


def test_marker_required_but_envelope_shaped_legacy_metadata_is_not_typed(tmp_path):
    bundle = fixture_bundle(tmp_path, marker=False)
    try:
        with pytest.raises(PayloadWriteError, match="R-owned"):
            write(bundle, [], storage())
        meta = {"storage_format": FORMAT, "columns": {"names": "x", "types": "double"}, "meta": {}}
        write(bundle, [{"value": "NA"}], meta=meta)
        table = artifact_table(bundle, "table")
        assert (
            table.storage_format is None and table.rows == [{"value": "NA"}] and table.meta == meta
        )
    finally:
        close_bundle(bundle)
