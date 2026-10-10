# Generic graph operation packs (ABI4 candidate)

This is the implementation-branch authoring contract. The ABI4/process5
schema and fixtures remain a joint PyBench/scworkbench draft until the R issuer,
broker, importer and publication path pass the same executable fixture.
ABI3/process4 remains unchanged.

The installed copy is available through
`importlib.resources.files("pybench").joinpath("graph_abi4_candidate.md")`.

An installed Python pack declares `schema_version =
"scworkbench.operation_pack.v4"`, `pack_abi_version = "4"`, and
`pybench-op-v5` compatibility. Its distribution exposes exactly one entry point
in `scworkbench.operation_packs.v4`. Installation is inert. The R engine chooses
the installed manifest and explicitly starts the PyBench process with an issued
request, private stage and broker token. PyBench authenticates the installed
distribution, manifest bytes, entry point and capability before invoking a
handler. R owns source authentication, artifact IDs, publication, promotion and
provenance.

## Manifest declarations

A graph input declares `kind: graph`, `storage_profile: graph_sqlite_v1`,
`read_mode: bounded_partitions_v1`, `alignment: none` and a binding policy.
A graph output declares the same kind/profile, a binding policy, resource
budgets and an `axis_sources` array. `axis_sources: []` permits an unbound
graph. A nonempty array must explicitly name a declared source input:

- `retained_input_axis` selects the authenticated cell axis of a declared
  `cell_vector` or `cell_vector_family` input; `axis_kind` is `cell` and
  `source_binding_selector` is `null`.
- `selected_graph_binding` selects an existing graph-input binding by fixed ID
  or by a required nonempty string parameter. The issued graph read grant must
  include that exact binding ID. No axis-only or implicit-first selection is
  allowed.

The R-issued `authorized_source_axes` grant supplies the exact retained source
snapshot, payload closure, axis content/order identities and transition. A pack
cannot make a new authority by self-hashing these fields. Matrix-bound graph
authority is currently fail-closed pending a separate core-authenticated
matrix source certificate; unbound matrix-to-graph output remains supported.

For a declared bound output, the handler receives a deep-copied, path-free
`context["graph_source_axes"][output_name]` list. Each entry contains only
`input_name`, `source_binding_id`, `axis_kind`, `source_ref`,
`source_snapshot_identity`, `axis_content_identity`, `axis_order_identity` and
`axis_count`. Construct a binding value with the public helper, then pass it to
`GraphPrimitive` or emit its binding/mapping rows through `open_graph`:

```python
from pybench import graph_binding_from_source

source = context["graph_source_axes"]["result"][0]
binding = graph_binding_from_source(
    source, "cell_nodes", [{"node_id": "node_1", "axis_id": "cell_1"}],
)
```

The projection is not an authority handle. Editing it cannot change the
writer's independently retained grant; a mismatched emitted binding rejects.

## Handler example

This handler consumes all issued graph parts and emits an unbound copy plus a
one-row summary. It uses only the invocation-scoped public reader/writer
interfaces. No bundle path, SQL handle or artifact registry is exposed.

```python
def copy_graph_with_summary(readers, writers, params, context):
    source = iter(readers.read_graph_parts("source"))
    header = next(source)
    assert header["component"] == "header"
    graph = writers.open_graph("result", header["rows"][0])
    content_components = {
        "nodes", "edges", "incidences", "attribute_fields", "attribute_values",
    }
    try:
        for part in source:
            if part["component"] in content_components:
                graph.write_part(part["component"], part["rows"])
        observation = graph.finish()
    except BaseException:
        graph.abort()
        raise
    writers.write_terminal_primitive("summary", {
        "kind": "obs_table",
        "dimensions": [1, 1],
        "rows": [{"nodes": int(observation["component_counts"]["nodes"])}],
        "storage_profile": "terminal_primitive_v1",
        "meta": {},
    })
```

The R issuer must declare and grant both outputs in one atomic closure. The
reader is single-use and must be consumed through its final broker `end` call;
early termination produces no successful receipt. `write_part` accepts
nonempty bounded row arrays in fixed component order: `header` is supplied by
`open_graph`, then nodes, edges, incidences, attribute fields/values, bindings
and binding mappings. Skipping an empty component is allowed. Every part is
schema-checked, self-hashed, counted and chain-observed. `finish()` validates
topology, attributes, associations and exact canonical identities before
returning a compact observation. Python writes only the private staged
`graph.parts.ndjson` and `payload.json`; R independently audits/imports them
before any durable publication.

For small graphs, `GraphPrimitive(content, bindings=...)` and
`writers.write_graph(name, graph)` offer a bounded convenience path. Use
`open_graph` for large graphs. The optional `pybench.graph_igraph` adapter
accepts only its documented lossless ordinary-edge subset; hyperedges, mixed
direction, unsupported attributes or structures reject instead of being
simplified. Native adapter conversion remains subject to materialization caps.

Resource budgets are decimal byte/row strings in the grant, not graph-validity
ceilings. The private writer enforces part rows/decoded bytes, staged bytes,
spill bytes, node/edge/incidence counts and attribute-field count. A complete
R publication gate must separately enforce published/WAL budgets and rollback
on any source mismatch, invalid receipt, undeclared mutation or fault.
Python counts the exact UTF-8 bytes of both `graph.parts.ndjson` and
`payload.json` against `max_staged_bytes` before sealing. Its discard-only
SQLite scratch uses no rollback journal, a 1 MiB cache, an admitted page-count
cap, and indexed ordered scans. `max_spill_bytes` includes the scratch DB and
live SQLite temp/journal file descriptors, checked before/after part commits
and at bounded scan intervals; missing Linux `/proc/self/fd` fails closed.
Those checkpoints are not a filesystem quota for transient bytes between
checks, so core independently audits the complete staged closure. A failed
part or seal closes and removes its owned scratch immediately. The
`max_memory_bytes` value is a measured process-RSS qualification target, not
an OS limit on arbitrary trusted pack code. `max_published_bytes` and
`max_wal_bytes` are core publication budgets, not Python writer guarantees.
