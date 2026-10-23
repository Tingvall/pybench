"""Focused protocol-v2 typed-table probe; no bundle or provenance authority."""

from __future__ import annotations

import argparse
import math
import sys
from typing import Any

from pybench import close_bundle
from pybench.artifacts_payload import write_artifact_table_payload
from pybench.protocol import (
    error_receipt_from_exception,
    load_request,
    ok_receipt,
    open_bundle_for_request,
    write_receipt,
)

VALUES = [
    0.0499545077,
    math.nextafter(0.05, -math.inf),
    0.05,
    math.nextafter(0.05, math.inf),
    1e-50,
    1e-300,
    0.0,
    -0.0,
    math.pi,
    sys.float_info.max,
    sys.float_info.min,
    5e-324,
    None,
    float("nan"),
    float("inf"),
    -float("inf"),
]


def adversarial_rows() -> list[dict[str, Any]]:
    rows = []
    for i, value in enumerate(VALUES):
        rows.append(
            {
                "feature id": f"f{i + 1}",
                "p_val_adj": value,
                "count": None if i % 2 == 0 else i,
                "flag": [True, False, None][i % 3],
                "label": ["NA", "Inf", "雪", "line\nbreak", None][i % 5],
                "missing double": None,
                "missing integer": None,
                "missing logical": None,
                "missing character": None,
            }
        )
    return rows


def benchmark_rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "gene": f"gene_{i}",
            "p_adj": (i % 1000) / 20000.0,
            "logfc": (i % 37 - 18) / 7.0,
            "score": (i % 101) / 13.0,
            "count": i % 500,
            "flag": i % 2 == 0,
            "group": "A" if i % 2 else "B",
            "missing": None if i % 11 == 0 else (i % 23) / 9.0,
        }
        for i in range(n)
    ]


def run(request: dict[str, Any]) -> dict[str, Any]:
    spec = request["outputs"]["table"]
    params = request["params"]
    case = params["case"]
    if case == "benchmark":
        rows = benchmark_rows(int(params["n_rows"]))
    elif case == "empty":
        rows = []
    else:
        rows = adversarial_rows()
    if case == "wrong_order":
        rows[0] = dict(reversed(tuple(rows[0].items())))
    if case == "wrong_type":
        rows[0]["count"] = True
    storage = spec.get("table_storage")
    if case == "wrong_meta":
        storage = {
            "format": storage["format"],
            "columns": {
                "names": list(reversed(storage["columns"]["names"])),
                "types": storage["columns"]["types"],
            },
        }
    bundle = open_bundle_for_request(request)
    try:
        written = write_artifact_table_payload(
            bundle,
            spec["artifact_id"],
            artifact_type=spec["type"],
            table_rows=rows,
            table_type="de_probe",
            entity_kind="feature",
            meta={"source": "pybench"},
            table_storage=storage,
        )
        if case == "forged_schema":
            import json

            row = bundle.con.execute(
                "SELECT meta_json FROM artifact_tables WHERE artifact_id=?",
                (spec["artifact_id"],),
            ).fetchone()
            meta = json.loads(row[0])
            meta["columns"]["types"][0] = "double"
            bundle.con.execute(
                "UPDATE artifact_tables SET meta_json=? WHERE artifact_id=?",
                (json.dumps(meta), spec["artifact_id"]),
            )
            bundle.con.commit()
        return ok_receipt(
            op_name=request["op_name"],
            run_id=request["run"]["run_id"],
            outputs={"table": written},
            warnings=[],
            timings={},
            pybench_version="typed-table-probe",
            request=request,
            bundle=bundle,
        )
    finally:
        close_bundle(bundle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    request = load_request(args.request)
    try:
        receipt = run(request)
    except Exception as error:
        receipt = error_receipt_from_exception(
            op_name=request["op_name"],
            run_id=request["run"]["run_id"],
            exc=error,
            request=request,
        )
        write_receipt(args.receipt, receipt)
        return 1
    write_receipt(args.receipt, receipt, request=request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
