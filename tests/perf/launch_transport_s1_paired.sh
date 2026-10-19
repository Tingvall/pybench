#!/usr/bin/env bash
set -euo pipefail
set -C

repo=/home/dirk/Projects/.pybench-worktrees/engine-transport-s1-stream-precode-quill-20261019
base=/home/dirk/Projects/.pybench-worktrees/engine-simplification-quill-20261018
python=/home/dirk/Projects/pybench/.venv/bin/python
parent=/tmp/scwb-engine-simplification-forge-20261018
work="$parent/quill_s1_baseline"
logs="$parent/quill_s1_paired_supervisor"
bootstrap="$parent/quill_s1_paired_bootstrap.log"

if [[ ! -f "$work/baseline.json" || -e "$logs" || -e "$bootstrap" ]]; then
  printf '%s\n' 'S1 paired run needs the frozen baseline and fresh supervisor paths.' >&2
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$base/src:$base/tests" \
  nohup setsid "$python" "$repo/tests/perf/transport_s1_supervisor.py" \
  --log-root "$logs" --work-root "$work" --baseline-root "$base" \
  --candidate-root "$repo" -- \
  "$python" "$repo/tests/perf/transport_s1_runner.py" \
  --phase paired --baseline-root "$base" --candidate-root "$repo" \
  --protocols v3,v4 --sizes-mib 64,256 --repetitions 3 \
  --order baseline,candidate/candidate,baseline/baseline,candidate \
  --work-root "$work" --baseline-ledger "$work/baseline.json" \
  --raw "$work/paired.jsonl" \
  >"$bootstrap" 2>&1 </dev/null &

printf 'S1 paired supervisor PID: %s\n' "$!"
printf 'Supervisor status: %s/status.json\n' "$logs"
