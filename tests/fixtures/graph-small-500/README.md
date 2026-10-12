# Small graph R/Python parity fixture

These files are byte-identical copies of the successful scworkbench small
out-of-core lifecycle run in
`notes/generic_graph_implementation/evidence/500_small_authenticated/` on
Forge's `feat/generic-graph-primitive-forge-20261010` candidate. The source
fixture is `notes/generic_graph_implementation/bounded_small_case.json`.
Forge's sealed export ledger is
`notes/generic_graph_implementation/evidence/501_small_authenticated_exports.sha256`
(SHA-256 `f19c67fe8d2d9c08eea20f3e61c3537ac0d95fa03afcc64572ac3c123abaccef`).
The complete R small-lifecycle test passed before this copy; its source and
installed-runtime post-hashes matched their prelaunch ledgers.

`test_graph_small_r_parity.py` pins every copied file's SHA-256. The R-issued
emit request and receipt authenticate the 32-node graph and three retained
bindings. The copy request and receipt authenticate the selected graph read
grant and complete cursor usage. PyBench independently reproduces the exact
content, binding, descriptor, partition-chain, and read-usage identities using
2-row partitions, a 1 MiB writer working allowance, and an 8 KiB collect cap.
The binding and descriptor roots are specific to this R source snapshot; a
fresh bundle run may issue different roots for identical graph content.

This fixture qualifies the tested bounded infrastructure, not above-host-RAM
graphs or a total-process 1 MiB RSS ceiling.
