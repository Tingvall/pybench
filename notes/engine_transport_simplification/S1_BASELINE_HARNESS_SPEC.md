# S1 baseline harness specification

Status: harness source prepared but unexecuted. No fixture bytes, trial,
Python test, or candidate runtime implementation exists yet. Runtime peer is
the clean PyBench `0ef3c56f9d300e7813ac5a555e6d2ac7cb64a57e`.
`S1_STREAM_PRECODE_BUDGET.md` defines the acceptance gates; this note fixes
the harness interface and future commands, not measured results.

## Harness files and fixed workload

- `tests/perf/transport_s1_trial.py`: one fresh child process. It receives
  `--protocol v3|v4`, `--stage PATH`, `--source-root PATH`, `--case-id`, and an installed local
  reference-pack distribution. It explicitly discovers/activates the pack,
  loads a deterministic request, invokes the ordinary public PyBench
  `execute_operation_pack_v3` or `execute_operation_pack_v4` once, and reports
  success/error code, output/receipt/sidecar SHA-256, `resource.RUSAGE_SELF`
  peak RSS in KiB, elapsed monotonic time, child-process rejection, and stage
  poststate fingerprint as one JSON object. Any spawned child disqualifies
  the trial rather than being omitted from the process RSS number.
- `tests/perf/transport_s1_runner.py`: outside the measured child. It creates
  four immutable deterministic fixture sources once, prepares the local
  reference-pack installation from an owned Git archive before timing, checks free disk/cgroup state,
  copies exactly one fixture into one owned `TemporaryDirectory`, launches
  the child in a fresh interpreter with the selected baseline or candidate
  source root on `PYTHONPATH`, records its JSON and parent-observed wall
  time, and removes that stage before the next trial. It writes raw
  JSONL plus a baseline ledger with literal frozen thresholds. It never
  retains a per-trial stage payload or changes the child invocation between
  baseline and candidate.
- `tests/perf/transport_s1_supervisor.py` and the two
  `tests/perf/launch_transport_s1_*.sh` scripts: detached launch and external
  owner of full runner stdout/stderr, numeric status, and pre/post source,
  installed-code and fixture guards. The runner retains full per-install and
  per-trial stdout/stderr and numeric timeout/exit status, including setup
  failure evidence.

Use the existing local v2 and v3 Python reference-pack fixture manifests and
their `all_contracts` operations, with the same minimal declared inputs and
outputs for every run within each protocol. Keep the inert file outside
declared writer paths, named `inert/payload.bin`; it must be unchanged by the
handler and present in both `S0` and `S1`. Use a deterministic byte generator
with fixed seed and a recorded SHA-256, not sparse/all-zero or compressible
content. Generate 64 MiB and 256 MiB immutable sources once. The exact
request, pack manifest, fixture generator source, and protocol assets must be
hashed into the baseline ledger before any candidate code is written. The
trial must assert output/receipt/sidecar equivalence and no handler side
effects on the inert payload; otherwise the workload is invalid.

The child should use the ordinary public execution function and local
installed reference distribution, not an internal `_snapshot` microbench.
Only the baseline harness imports test fixture helpers; the reference pack
itself remains independently installed into the owned work root. Pip reads
the Git-derived copy, never the source worktree or main venv. No network or install step belongs
inside measured child time. Each child's `PYTHONPATH` must point to the
explicitly selected source root's `src` and `tests`, never a different
editable installation. Parent and child assert and report actual PyBench
import paths; actual installed dependency code/native files are hashed before
and after, not merely METADATA/RECORD. Do not use a no-restore flag, infer R
parent ownership, or change public staging paths.

## Held command lines

Run only after root releases the CPU lane. The working directory for all
commands is the clean isolated PyBench worktree at
`/home/dirk/Projects/.pybench-worktrees/engine-transport-s1-stream-precode-quill-20261019`.
The interpreter is the PyBench main `.venv` used for the recorded full-suite
qualification, not the separate `/tmp/quill-engine-simplification-venv` used
for R fixture/broker work. It is not modified. Record its executable SHA-256,
installed distribution versions, METADATA and RECORD digests. The work root
is fresh and owned by this trial; never reuse a shared temporary root.

```bash
cd /home/dirk/Projects/.pybench-worktrees/engine-transport-s1-stream-precode-quill-20261019
bash tests/perf/launch_transport_s1_baseline.sh
```

The baseline command must run against unchanged `0ef3c56` runtime files.
Its ledger must include four numeric absolute RSS ceilings in KiB and four
numeric elapsed ceilings in seconds, using the formulas in the pre-code
budget. Stop before runtime coding if any ceiling is unachievable or if disk
headroom is under 2 GiB. Preserve only the immutable fixtures, ledger,
JSONL, and small diagnostic logs between phases.

After an implementation commit, and only after the baseline ledger is
accepted, the runner's fixed paired mode is:

```bash
cd /home/dirk/Projects/.pybench-worktrees/engine-transport-s1-stream-precode-quill-20261019
bash tests/perf/launch_transport_s1_paired.sh
```

The runner must reject changed fixture/request/pack/protocol/interpreter
digests, missing baseline literals, missing child JSON, nonzero unexpected
exit, residual stage directories, or a byte mismatch. It must record each
natural-cache observation but must not label a fresh process as cold cache.
No artificial warm-up or favorable-subset rerun is part of this plan.

## Before running the harness

Freeze the reference operation/request shape from the existing fixtures and
verify that both protocols accept one unchanged inert stage file without
changing the declared output closure. That preflight is deferred until the
CPU lane is free. If either fixture cannot satisfy the ordinary public
execution contract, revise the harness workload and get the fixed request
reviewed before taking the baseline. Do not adjust the workload after
measuring candidate results.
