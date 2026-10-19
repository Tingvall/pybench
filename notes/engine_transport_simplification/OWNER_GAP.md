# Stage-1 scratch ownership source audit

Status: **STOP disk-backed ordinary v3/v4 snapshots**. Source-only finding, not an implementation approval or runtime qualification.

## Exact pins

- PyBench frozen peer: `0ef3c56f9d300e7813ac5a555e6d2ac7cb64a57e`.
- R source inspected: `06d70aea0886f585a80d24e42feee89b54f96f24` in `engine-simplification-wal-fixture-forge-20261019`.
- Sable's separate source assessment inspected R `eb3791c22585f82371a6607aac0b7cd03ffa33c2`: `/tmp/scwb-sable-python-owned-scratch-design-eb3791c-20261019/assessment.json`, SHA-256 `e0fe352683a4d6735acaac1efb04d305989729919764739d06fb047733154e09`. It found a potentially feasible narrow lifecycle addition, **not** existing complete ownership.
- Amended stage-1 design plan: `b8e88d37bd2e661394d8d2c84e63631831165b10`, `PLAN.md` SHA-256 `0a796ff786bd2380bbd10d639e58a18c9515f44c2de18e85fa0eb024fdc65da1`. Sable cleared design prerequisites, not implementation or selection.

## Existing ownership and missing link

- R `R/operation_pack_v2_transition.R:27-35,1529-1544` creates `bundle/operation_pack_v2/.pending/<transition>` and on ordinary unwind unlinks that stage and an unpublished final record. This **discards** a failed private stage; it does not restore its prior bytes in place.
- R `R/operation_pack_python_v3.R:282-310` launches supervised Python with a process-local environment, kills/waits Python and broker, and removes `stage/broker`. It does not assign a Python `TMPDIR` or own a snapshot backup. `R/operation_pack_python_v3.R:368-402` routes executions without supervised reads through `.wb_operation_pack_v2_python_run`; `R/operation_pack_python_v2.R:24-42,514-532` uses `processx::run` without an execution-specific scratch environment. Discovery also uses the shared runner, so changing its default environment would broaden behavior.
- PyBench `src/pybench/protocol_v5.py:131-160` creates the graph disk snapshot with `tempfile.mkdtemp(prefix="pybench-v5-stage-")` and no `dir`, then closes it only from Python. A Python hard exit cannot execute that close. The ambient temp directory is outside R's known stage cleanup. Existing graph defaults are not changed by this audit.
- R `R/operation_pack_v2_transition.R:1429-1453` deletes directory children of `.pending` on writable recovery; the recipient assertion at lines 1456+ rejects pending directories on read-only open. `R/bundle_access.R:87-90` invokes recovery on writable open. A uniquely R-created scratch sibling **inside** `.pending`, but outside the scanned stage, could use this machinery only after explicit ownership, cleanup and live/stale-owner safety are proved. Current on.exit deletes only `<transition>`, and merely placing a sibling there leaves normal-exit residue. The separate clone cleanup in `R/pybench_transition.R:455-525` belongs to a different transition path.

## Smallest unresolved lifecycle contract

Before PyBench stage-1 implementation, Forge/R would need to demonstrate a private, uniquely named, exclusively created, mode-0700 scratch sibling under the existing disposable `.pending` parent. R must register cleanup immediately, set standard `TMPDIR` only for the exact Python execution child in **both** supervised and unsupervised launch branches, and verify Python's backup actually remains inside that owned root (no ambient fallback). Scratch must be absent before final stage audit/publication. This is a candidate shape, not an authorized code change or new wire/path authority.

Ordinary R `on.exit` cannot handle R hard death. Existing pending recovery must first establish exclusive ownership and actual child/broker liveness or reaping; `cleanup_tree=TRUE`, stale path age or PID alone is not proof. Recovery must not delete an active transition or unrelated sibling. Nonfollowing root/parent checks, outside sentinels, collision and replacement cases, exact scratch/stage inventory, and S0 logical/allocated backup disk pressure are required. The R parent continues to own failure/discard/publication; neither Python receipt nor the backup becomes source-state authority. No new protocol, identity scheme, durable registry or graph-helper default is proposed.

Minimum focused proof: success, pre-handler failure, handler/audit/receipt failure, launch failure, timeout and child hard exit **after backup and stage mutation** through both launch branches; then R hard death during mkdir/copy/handler/audit followed by read-only refusal and safe writable orphan recovery. Verify stage/scratch absence after owned cleanup, no committed source/recipient mutation, no final record, no outside-sentinel change, and no active-owner deletion. Until this owner/death/recovery proof and Sable/Forge selection, retain the ordinary in-memory snapshot. Stages 2/3 of `PLAN.md` remain unapproved.

No R or Python execution, tests, probes, installation, SIF, or runtime/source edit was performed for this audit. Existing PyBench 751-pass/7-skip qualification and later 17-vector/9-file-hash/mixed results remain attributed only to their executed older R pins.
