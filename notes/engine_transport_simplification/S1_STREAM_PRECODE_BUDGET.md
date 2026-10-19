# S1 streaming comparison: pre-code budget and trial protocol

Status: source-only design. Parent plan `d41ff3c10b7192bd0b371cf12075a28a479bd04c`;
runtime peer `0ef3c56f9d300e7813ac5a555e6d2ac7cb64a57e`. No implementation,
benchmark, protocol change, or performance acceptance is implied here.

## Exact scope and authority

Ordinary protocol v3 and v4 call `_snapshot(root)` before and after execution
(`src/pybench/protocol_v3.py:1447,1555` and
`src/pybench/protocol_v4.py:3304,3533`). Each snapshot maps every regular
stage file to its complete bytes. The `before` map is the recoverable-failure
rollback authority (`_restore`); only changed-path comparison consumes the
post-handler map. `_tree`, output semantic checks, separate output hashes,
mutation manifest, receipt validation, and R's independent stage audit remain
unchanged. Graph ABI4's `DiskStageSnapshot` is a different path and is out of
scope.

The candidate retains `before = _snapshot(root)` and `_restore(root, before)`.
Replace only ordinary v3/v4 `after = _snapshot(root)` plus byte-map difference
with an exact streaming walk of the same sorted `root.rglob("*")`. Keep a set
of observed file keys and a changed-key set. For each regular file, read to
EOF in bounded blocks and compare every byte with a view of the corresponding
`before` payload. A new file is changed but still read to EOF. After the full
walk, add deleted keys from `before`; sort the final changed set as today.
Identical-byte replacement remains unchanged. Do not accept a digest, size,
timestamp, writer record, or output claim as a substitute for byte equality.

Preserve `_snapshot`'s ordinary symlink check and its current handling of
non-regular objects; keep the subsequent independent `_tree` traversal and
its diagnostics. Do not open a FIFO. A mismatch must not stop traversal:
the old after-snapshot reads every later file before changed-set audit, so a
later read/symlink error retains precedence over an earlier changed-path
error. Keep per-file full reads, original path ordering, and exception and
rollback behavior. Do not change any Python/R request, receipt, capability,
identity, source, or publication authority.

Let `B0` and `B1` be total pre/post regular-file bytes and `P` path metadata.
The existing ordinary path performs snapshot-specific reads `B0+B1` and can
hold both byte maps, approximately `B0+B1+P` payload residency. The candidate
still reads `B0+B1` but holds `B0+P+O(chunk)` for those snapshots. Output
parsing and hashing are separate passes and allocations. This is a **partial
peak-RAM reduction**, not bounded memory independent of `B0`, bounded
typed-space support, or a whole-workflow speed claim. The Python public API
still restores the exact pre-handler stage after recoverable errors. R's
eventual pending-stage cleanup is not a discriminator for skipping rollback.
The existing v5 disk backup is not generalized: its ambient temporary owner
can leave residue after hard process death.

## Frozen measurement gate before implementation

Do not run the trial while Forge/Sable own the CPU lane. First freeze the
runtime commit, installed interpreter/dependency versions, machine/cgroup,
fixture generator digest, and exact command/environment in a baseline ledger.
Use the clean `0ef3c56` peer for baseline, then a single isolated child
implementation commit from this documentation branch. No comparison across
changed R source, protocol assets, fixture contents, or workload parameters.

Prepare immutable deterministic source fixtures with 64 MiB and 256 MiB of
inert baseline file bytes and the same small declared operation/output in
each. Choose bytes that do not compress unusually. Copy one fixture into one
trial-owned `TemporaryDirectory` at a time; close and remove that stage before
the next trial. Keep fixture digests, stage/output/receipt fingerprints and
small logs, not a separate 256 MiB payload for every trial. Preflight actual
free space before each copy (at least 2 GiB free, otherwise stop); keep the
immutable fixtures and one active stage within the available disk budget.
Record `B0`, `B1`, file count, tree paths, output/receipt digests and return
code. Do not change the public invocation path or give the candidate a
different stage layout.

Before code, run three ordinary v3 and three ordinary v4 baseline trials per
size, each in a fresh process. Record exact commands, process-tree peak RSS
in KiB, wall time, available cgroup memory, stage disk peak, and raw results.
From these three results, freeze a **literal absolute whole-process RSS
ceiling in KiB** for each size/version in the baseline ledger: baseline median
peak RSS minus `floor(B1_bytes / 2048)`. If that ceiling is nonpositive or below a
measured interpreter/minimal-stage floor, stop and revise the proposal before
code; never leave an unspecified parsed-object allowance. Freeze a literal
elapsed ceiling of 1.10 times baseline median for each size/version as well.
These formulas are predeclared, but their numeric outputs must be recorded
before implementation. Candidate acceptance uses the frozen literals, not a
recomputed favorable baseline. No forced GC or lowered memory admission.

After implementation and lane release, run three fresh-process matched pairs
per size/version in the fixed order baseline/candidate, candidate/baseline,
baseline/candidate. One stage copy is active at any time. Record natural cache
state and run order; do not call fresh processes cold-cache or first-touch
trials without actual authorized kernel cache control. No artificial warm-up
is part of the ordinary path. The candidate must keep exact outputs/receipts
and error codes, leave no durable or temporary residue, use post-read blocks
of at most 1 MiB. The maximum of the three candidate peak-RSS values must
stay below the frozen RSS ceiling; their median elapsed must stay below the
frozen elapsed ceiling in each size/version cohort. Report every raw run and
pair, not only medians. A miss does not authorize a looser gate or a
favorable-subset retake.

## Fixed adversarial matrix for implementation

Use an independent old `_snapshot`/changed-set oracle and compare exact
sorted keys for unchanged, created, same-byte replaced, changed-byte
replaced, truncated, lengthened, deleted, nested and empty files, including
0, 1, `chunk-1`, `chunk`, `chunk+1`, `2*chunk` and `2*chunk+1` byte boundaries.
Exercise first/middle/last-byte mismatches and many files with a mismatch in
the first file. Confirm every later file is still read to EOF.

Inject a later read failure and a later symlink after an earlier mismatch;
assert the original exception precedence. Include an unchanged and an added
FIFO and an empty directory; compare ordinary `_tree` behavior without
opening a FIFO. Exercise output tamper after the sanctioned writer, handler
exception after file creation/replacement/deletion, incomplete or forged
receipt, and a fault after the comparison but before return. For every
recoverable error, assert exact pre-stage bytes and paths after `_restore`,
no sidecar/receipt success, and successful immediate retry. Keep explicit
v3 and v4 cases; graph ABI4 disk snapshot remains untouched. Verify the
independent R audit and publication path only after the narrow Python tests
pass and the controller schedules an integration lane.

## Frozen retry-03 baseline

The clean `0ef3c56f9d300e7813ac5a555e6d2ac7cb64a57e` runtime peer
completed all 12 ordinary v3/v4 baseline trials (three fresh children for
each size/version). The supervisor exited 0 in 53.104249 seconds without
timeout or supervisor error; the runner returned 0 after checking its own
pre/post source, dependency, fixture, and installed-site guards. The outer
supervisor's `pre_post_guard_equal` is false because setup created its owned
site and fixture trees, not because a source guard failed.

Frozen absolute gates, from `baseline.json`:

| Case | Peak RSS ceiling (KiB) | Median execution ceiling (s) |
| --- | ---: | ---: |
| v3, 64 MiB | 198644 | 0.4568472920218483 |
| v3, 256 MiB | 493012 | 0.7190257942886092 |
| v4, 64 MiB | 199352 | 0.21637273904052565 |
| v4, 256 MiB | 494336 | 0.5227733621082735 |

Evidence root: `/tmp/scwb-engine-simplification-forge-20261018/quill_s1_baseline_retry03`.
`baseline.json` SHA-256:
`802d08ba174eb02703ea732122349dc41d6d3e6390a9c11a26dbc441f701e811`.
The 13-line raw ledger (guard plus 12 trials) SHA-256:
`10ecda1d1bd0a9afb0b6967c817d03436bdcc7625686e2d52c9b0ca001b08050`.
The ledger's `guard.paths.fixtures` records each fixture file digest. Request
hashes by case are v3/64 `43c54c8c4b666bcc9d1e274a9893260cb6c7e6602280c39cdbbc7b1738a3435e`,
v3/256 `d88cba0e6447027dda6c1f9e7ba4f441195a72d48496879818858b5efd754ba2`,
v4/64 `c641ef1534168b0945d9923ff6e8653899cc892f8637188eac64b0d64d58e7df`,
and v4/256 `8f425e7bc3839b17ab59c45458ad86e6165c60193000faf12b324b357ee22d73`.
The inert 64 MiB and 256 MiB fixture hashes are respectively
`ad6b2bee16f83bfca68a7d05d707ad654e1eb8fd904e5a5e233319b64f0caa9e`
and `a83f767ef579f51432a96f761f821d2f468594192a36f4f925998874b21c22d4`.
No previous failed attempt's evidence was overwritten.

If the exact old behavior cannot be preserved without a new backup owner,
stop the candidate rather than broadening the transport model.
