#!/usr/bin/env bash
set -euo pipefail
set -C

repo=/home/dirk/Projects/.pybench-worktrees/engine-transport-s1-stream-precode-quill-20261019
base=/home/dirk/Projects/.pybench-worktrees/engine-simplification-quill-20261018
python=/home/dirk/Projects/pybench/.venv/bin/python
parent=/tmp/scwb-engine-simplification-forge-20261018
work="$parent/quill_s1_baseline_retry01"
logs="$parent/quill_s1_supervisor_retry01"
bootstrap="$parent/quill_s1_bootstrap_retry01.log"

if [[ ! -d "$parent" || -e "$work" || -e "$logs" || -e "$bootstrap" ]]; then
  printf '%s\n' 'S1 baseline requires fresh owned work, supervisor, and bootstrap paths.' >&2
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$base/src:$base/tests" \
  nohup setsid "$python" "$repo/tests/perf/transport_s1_supervisor.py" \
  --log-root "$logs" --work-root "$work" --baseline-root "$base" -- \
  "$python" "$repo/tests/perf/transport_s1_runner.py" \
  --phase baseline --baseline-root "$base" --protocols v3,v4 \
  --sizes-mib 64,256 --repetitions 3 --work-root "$work" \
  --ledger "$work/baseline.json" --raw "$work/baseline.jsonl" \
  >"$bootstrap" 2>&1 </dev/null &

printf 'S1 baseline supervisor PID: %s\n' "$!"
printf 'Supervisor status: %s/status.json\n' "$logs"
