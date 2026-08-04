from __future__ import annotations

import argparse
import time
from typing import Any

from pybench import close_bundle, open_bundle
from pybench.artifacts_payload import (
    write_artifact_cell_vector_payload,
    write_artifact_relation_family_payload,
    write_artifact_table_payload,
)
from pybench.getters import cells as get_cells
from pybench.protocol import (
    error_receipt_from_exception,
    load_request,
    ok_receipt,
    validate_request,
    write_receipt,
)

PYBENCH_VERSION = "0.1.0"


def _run(req: dict[str, Any]) -> dict[str, Any]:
    validate_request(req)
    op_name = str(req["op_name"])
    run_id = str(req["run"]["run_id"])
    t0 = time.time()
    b = open_bundle(req["bundle_dir"])
    try:
        inputs = req.get("inputs", {})
        params = req.get("params", {})
        outputs_spec = req.get("outputs", {})

        cell_set = str(inputs.get("cell_set", "core"))
        all_cells = get_cells(b, cell_set=cell_set)
        n_preview = int(params.get("n_preview", 200))
        use_cells = all_cells[: max(1, min(len(all_cells), n_preview))]

        out_receipts: dict[str, Any] = {}

        # 1) cell_vector: simple continuous index
        if "vector" in outputs_spec:
            spec = outputs_spec["vector"]
            vals = [float(i + 1) for i in range(len(use_cells))]
            r = write_artifact_cell_vector_payload(
                b,
                artifact_id=spec["artifact_id"],
                cell_id=use_cells,
                value=vals,
                value_type="continuous",
            )
            out_receipts["vector"] = r

        # 2) relation_family: one dummy lineage over same cells
        if "membership" in outputs_spec:
            spec = outputs_spec["membership"]
            key = ["root:HELLO::lineage:WORLD"] * len(use_cells)
            r = write_artifact_relation_family_payload(
                b,
                artifact_id=spec["artifact_id"],
                relation_key=key,
                cell_id=use_cells,
                weight=[1.0] * len(use_cells),
            )
            out_receipts["membership"] = r

        # 3) obs_table: one-row summary
        if "summary" in outputs_spec:
            spec = outputs_spec["summary"]
            rows = [
                {
                    "metric": "n_cells_written",
                    "value": float(len(use_cells)),
                    "cell_set": cell_set,
                }
            ]
            r = write_artifact_table_payload(
                b,
                artifact_id=spec["artifact_id"],
                artifact_type=spec.get("type", "obs_table"),
                table_rows=rows,
                table_type="generic",
                entity_kind="run_summary",
                meta={"hello": True},
            )
            out_receipts["summary"] = r

        dt = time.time() - t0
        return ok_receipt(
            op_name=op_name,
            run_id=run_id,
            outputs=out_receipts,
            warnings=[],
            timings={"total_sec": round(dt, 6)},
            pybench_version=PYBENCH_VERSION,
        )
    finally:
        close_bundle(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="pybench protocol hello op (dummy protocol test op)")
    ap.add_argument("--request", required=True, help="Path to pybench request JSON")
    ap.add_argument("--receipt", required=True, help="Path to write pybench receipt JSON")
    args = ap.parse_args()

    req = load_request(args.request)
    try:
        rec = _run(req)
    except Exception as e:  # noqa: BLE001
        rec = error_receipt_from_exception(op_name=str(req.get("op_name", "<unknown>")), run_id=str((req.get("run") or {}).get("run_id", "<unknown>")), exc=e)
        write_receipt(args.receipt, rec, request=None)
        return 1

    write_receipt(args.receipt, rec, request=req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
