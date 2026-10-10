"""Subprocess worker for the opt-in beyond-RAM graph staging gate."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pybench.graph_parts as graph_parts
from pybench.graph_parts import GraphPartsWriter
from pybench.operation_packs_v2 import _identity
from test_graph_transport import _grant, _transition

NODE_COUNT = int(os.environ.get("PYBENCH_GRAPH_GATE_NODES", "24576"))


class InjectedLateWriteFault(RuntimeError):
    pass


def peak_rss_bytes() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Linux VmHWM is unavailable")


def stage_disk_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main(root: Path) -> None:
    transition = _transition()
    grant = _grant(transition)
    axis = grant["authorized_source_axes"][0]
    axis["axis_count"] = str(NODE_COUNT)
    axis.pop("authority_identity")
    axis["authority_identity"] = _identity(
        "scworkbench.operation_pack.graph_source_axis_authority.v1",
        axis,
        "authority excluding authority_identity",
    )
    budgets = grant["resource_budgets"]
    budgets.update(
        {
            "max_nodes": str(NODE_COUNT),
            "max_edges": "2",
            "max_incidences": str(NODE_COUNT),
            "max_attribute_fields": "2",
            "max_staged_bytes": str(3 * 1024**3),
            "max_spill_bytes": str(3 * 1024**3),
            "max_published_bytes": str(3 * 1024**3),
            "max_wal_bytes": str(3 * 1024**3),
            "max_memory_bytes": str(192 * 1024**2),
            "max_collect_bytes": str(1024**2),
            "partition_limits": {"max_rows": "128", "max_decoded_bytes": str(1024**2)},
        }
    )
    grant.pop("grant_identity")
    grant["grant_identity"] = _identity(
        "scworkbench.operation_pack.graph_write_grant.v1",
        grant,
        "grant excluding grant_identity",
    )
    baseline_rss = peak_rss_bytes()
    checkpoints = {"baseline": baseline_rss}
    disk_checkpoints = {"baseline": stage_disk_bytes(root)}
    writer = GraphPartsWriter(
        grant,
        root,
        transition_identity=transition,
        input_kinds={"source": "graph"},
        output_names=["graph"],
    )
    try:
        writer.write_part(
            "header",
            [
                {
                    "schema": "scworkbench.graph.content.v1",
                    "direction_mode": "directed",
                }
            ],
        )
        for start in range(0, NODE_COUNT, 128):
            writer.write_part(
                "nodes",
                [{"node_id": f"n{index}"} for index in range(start, min(start + 128, NODE_COUNT))],
            )
        checkpoints["nodes"] = peak_rss_bytes()
        disk_checkpoints["nodes"] = stage_disk_bytes(root)
        writer.write_part(
            "edges",
            [
                {
                    "edge_id": "hyper0",
                    "kind": "hyper",
                    "direction": "directed",
                }
            ],
        )
        for start in range(0, NODE_COUNT, 128):
            writer.write_part(
                "incidences",
                [
                    {
                        "incidence_id": f"i{index}",
                        "edge_id": "hyper0",
                        "node_id": f"n{index}",
                        "ordinal": index,
                        "role": "tail" if index < NODE_COUNT // 2 else "head",
                    }
                    for index in range(start, min(start + 128, NODE_COUNT))
                ],
            )
        checkpoints["incidences"] = peak_rss_bytes()
        disk_checkpoints["incidences"] = stage_disk_bytes(root)
        writer.write_part(
            "attribute_fields",
            [
                {
                    "scope": "node",
                    "field_ordinal": "0",
                    "name": "long_label",
                    "type": "utf8",
                    "nullable": False,
                }
            ],
        )
        label = "x" * (64 * 1024)
        for start in range(0, NODE_COUNT, 15):
            writer.write_part(
                "attribute_values",
                [
                    {
                        "scope": "node",
                        "field_ordinal": "0",
                        "entity_ordinal": str(index),
                        "value": label,
                    }
                    for index in range(start, min(start + 15, NODE_COUNT))
                ],
            )
        checkpoints["attributes"] = peak_rss_bytes()
        disk_checkpoints["attributes"] = stage_disk_bytes(root)
        axis = grant["authorized_source_axes"][0]
        writer.write_part(
            "bindings",
            [
                {
                    "binding_ordinal": "0",
                    "binding_id": "b0",
                    **{
                        key: axis[key]
                        for key in (
                            "axis_kind",
                            "source_ref",
                            "source_snapshot_identity",
                            "axis_content_identity",
                            "axis_order_identity",
                        )
                    },
                }
            ],
        )
        for start in range(0, NODE_COUNT, 128):
            writer.write_part(
                "binding_mappings",
                [
                    {
                        "binding_id": "b0",
                        "mapping_ordinal": str(index),
                        "node_id": f"n{index}",
                        "axis_id": f"c{index}",
                    }
                    for index in range(start, min(start + 128, NODE_COUNT))
                ],
            )
        checkpoints["mappings"] = peak_rss_bytes()
        disk_checkpoints["mappings"] = stage_disk_bytes(root)
        if os.environ.get("PYBENCH_GRAPH_GATE_FAULT") == "late_write":
            raise InjectedLateWriteFault("injected late graph write fault")
        for method_name in ("_validate_topology", "_validate_attributes", "_validate_bindings"):
            original = getattr(writer, method_name)

            def tracked(*args, _original=original, _name=method_name):
                result = _original(*args)
                checkpoints[_name] = peak_rss_bytes()
                disk_checkpoints[_name] = stage_disk_bytes(root)
                return result

            setattr(writer, method_name, tracked)
        original_hash = graph_parts.canonical_sha256_size_stream
        original_identity = graph_parts.identity

        def tracked_hash(parts):
            result = original_hash(parts)
            checkpoints["content_hash"] = peak_rss_bytes()
            disk_checkpoints["content_hash"] = stage_disk_bytes(root)
            return result

        def tracked_identity(scheme, value):
            result = original_identity(scheme, value)
            if scheme in (graph_parts.BINDING_SCHEME, graph_parts.DESCRIPTOR_SCHEME):
                checkpoints[scheme] = peak_rss_bytes()
                disk_checkpoints[scheme] = stage_disk_bytes(root)
            return result

        graph_parts.canonical_sha256_size_stream = tracked_hash
        graph_parts.identity = tracked_identity
        observation = writer.finish()
        checkpoints["sealed"] = peak_rss_bytes()
        disk_checkpoints["sealed"] = stage_disk_bytes(root)
    except InjectedLateWriteFault:
        staged_bytes = writer._stage_bytes
        writer.abort()
        assert not (root / "outputs" / "graph").exists()
        print(
            json.dumps(
                {
                    "fault": "late_write",
                    "memory_budget_bytes": int(budgets["max_memory_bytes"]),
                    "peak_rss_bytes": peak_rss_bytes(),
                    "pre_fault_staged_bytes": staged_bytes,
                    "peak_stage_disk_bytes": max(disk_checkpoints.values()),
                    "stage_disk_bytes_after_abort": stage_disk_bytes(root),
                    "rss_checkpoints": checkpoints,
                    "disk_checkpoints": disk_checkpoints,
                },
                sort_keys=True,
            )
        )
        return
    except BaseException:
        writer.abort()
        raise
    parts = root / "outputs" / "graph" / "graph.parts.ndjson"
    print(
        json.dumps(
            {
                "memory_budget_bytes": int(budgets["max_memory_bytes"]),
                "baseline_rss_bytes": baseline_rss,
                "peak_rss_bytes": peak_rss_bytes(),
                "staged_bytes": parts.stat().st_size,
                "peak_stage_disk_bytes": max(disk_checkpoints.values()),
                "stage_disk_bytes_after_finish": stage_disk_bytes(root),
                "rss_checkpoints": checkpoints,
                "disk_checkpoints": disk_checkpoints,
                "canonical_content_bytes": writer._canonical_content_bytes,
                "counts": observation["component_counts"],
                "content_identity": observation["content_identity"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
