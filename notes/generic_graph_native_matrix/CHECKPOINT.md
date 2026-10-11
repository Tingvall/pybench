# PyBench native matrix graph parity checkpoint

Branch: `feat/generic-graph-native-matrix-python-20261011`.
Parent: `c3dfdbd51d2e996476205e19665348cc271b8b5f`.

## Frozen R source

This is a narrow protocol and fixture parity checkpoint, not whole-runtime
acceptance. Forge's source is dirty checkpoint 330, not historical Git HEAD
`9a11d1fe988780041daa258eff987e9a1c7b2276` alone. The source tuple is:

- 61-file checkpoint ledger SHA-256:
  `235d33841044bd732b932ab756feff67018e29bbba38cf9d61d8ba21831df85e`.
- Two-file runtime addendum SHA-256:
  `1269de7942f38e8d66fe4a0a5259622607beb41c2b859f9811c5de6f71a0e14e`.
- Narrow freeze JSON SHA-256:
  `67c30bff405bf81604484296a571304cca3e0e1aed0225bf5925e20caf0e4c04`.
- Real request/receipt samples: public R test gate 333 against installed
  checkpoint 330; four exact files under `tests/fixtures/graph-native-matrix-330/`.

All nine shared protocol/schema/identity assets are byte-identical to the
freeze. The changed manifest schema has SHA-256
`0c38f16eb811dd40111c0b82ebcb519d9e8bd06bccaaf13f1aa366a94cc6b13f`.
The other eight frozen hashes and all four sample hashes are executable
assertions in `tests/test_graph_native_matrix_parity.py`. The seven R-only SDK
and fixture assets were hash-checked against Forge's freeze, not copied into
PyBench or treated as Python-owned contracts.

## Python boundary

The closed ABI4 manifest now admits `retained_matrix_axis` for a declared
matrix input, with cell/feature axes and a null binding selector. Process-v5
admission matches an R-issued direct matrix axis or a selected graph-input
binding whose retained source is a matrix. The public graph writer stages and
re-audits bound graph payloads from both frozen R grants. PyBench does not
independently authenticate the R fragment-tree.v2 serialization, BPCells
bytes, producer provenance or promotion state. R owns those checks and final
publication. An unpromoted matrix is ineligible before staging; a selected
matrix input descriptor may cover fewer cells than the complete source axis.

## Focused evidence

- `evidence/pytest_focused.log`: 70 passed, exit 0.
- `evidence/ruff_check.log`: all checks passed, exit 0.
- `evidence/ruff_format.log`: 8 files formatted, exit 0.
- `evidence/protocol_generate_check.log`: five generated schemas current, exit 0.
- `evidence/cross_repo_byte_parity.log`: 9/9 byte-identical, exit 0.
- `evidence/forge_16_asset_check.log`: 16/16 Forge assets verified, exit 0.
- `evidence/forge_runtime_addendum_check.log`: 2/2 additional runtime files verified, exit 0.

No full Python suite, wheel qualification, fullstack, SIF, merge or push was
run for this narrow checkpoint. The Forge 330 runtime still has a publication
disk-admission blocker; legacy certificate/DAG memory and final RSS/disk
qualification remain open. This PyBench result must not be attributed as a
whole-candidate acceptance.
