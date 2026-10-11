# Frozen R-issued graph matrix samples

These four request/receipt files were copied byte-for-byte from Forge's public
native-matrix graph lifecycle test, run as gate 333 against the installed source
checkpoint 330. The source checkpoint is a dirty descendant of historical base
`9a11d1fe988780041daa258eff987e9a1c7b2276`, not a commit at 330.

Freeze record: `notes/generic_graph_implementation/evidence/336_shared_protocol_fixture_freeze_330.json`
in the Forge scworkbench implementation worktree, SHA-256
`67c30bff405bf81604484296a571304cca3e0e1aed0225bf5925e20caf0e4c04`.
The 61-file source ledger SHA-256 is
`235d33841044bd732b932ab756feff67018e29bbba38cf9d61d8ba21831df85e`.
Forge's addendum for two previously omitted runtime files has SHA-256
`1269de7942f38e8d66fe4a0a5259622607beb41c2b859f9811c5de6f71a0e14e`.
The producer source tuple is checkpoint 330 plus that addendum, not the
historical Git HEAD alone.

The test copied the exact `request.json` and `receipt.json` files from two
public transitions. No Python test constructs their R-issued authority. Native
matrix axes require an existing promoted matrix and independent R producer
provenance; an unpromoted session matrix remains ineligible before staging.
PyBench treats the R-issued `scworkbench.content.matrix.fragment-tree.v2` hash
as opaque and does not claim to reproduce R serialization or BPCells bytes.
The samples support a narrow protocol parity claim, not whole-runtime or
publication-budget qualification.
