from __future__ import annotations

import copy
import csv
import shutil
import uuid
from pathlib import Path

from pybench.operation_packs_v2 import _identity, distribution_identity_v2


def install_reference_fixture(tmp_path: Path, monkeypatch):
    source = Path(__file__).parent / "fixtures/operation-pack-v2-python-reference"
    site = tmp_path / "site"
    package = site / "scwbpyv2reference"
    shutil.copytree(source / "src/scwbpyv2reference", package)
    shutil.copy2(source / "operation-pack.json", package / "operation-pack.json")
    dist_info = site / "scwb_v2_python_reference-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: scwb-v2-python-reference\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: pybench-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[scworkbench.operation_packs.v2]\nreference = scwbpyv2reference.pack:register\n",
        encoding="utf-8",
    )
    files = sorted(
        path.relative_to(site).as_posix()
        for path in site.rglob("*")
        if path.is_file() and path.name != "RECORD"
    )
    with (dist_info / "RECORD").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for relative in files:
            writer.writerow([relative, "", ""])
        writer.writerow([(dist_info / "RECORD").relative_to(site).as_posix(), "", ""])
    monkeypatch.syspath_prepend(str(site))
    return package / "operation-pack.json"


def fixture_inputs(provider: str = "packaging") -> dict[str, dict]:
    cell = ["cell_1"]
    feature = ["feature_1"]
    return {
        "matrix": {
            "kind": "matrix",
            "dimensions": [1, 1],
            "cell_id": cell,
            "feature_id": feature,
            "values": [[1.0]],
            "storage_profile": "matrix_ordinary_v1",
            "meta": {"genome_provider": provider},
        },
        "space": {
            "kind": "space",
            "dimensions": [1, 1],
            "cell_id": cell,
            "values": [[1.0]],
            "storage_profile": "space_dense_v1",
        },
        "space_2d": {
            "kind": "space_2d",
            "dimensions": [1, 2],
            "cell_id": cell,
            "values": [[0.0, 1.0]],
            "storage_profile": "space_2d_dense_v1",
        },
        "model_ref": {
            "kind": "model_ref",
            "dimensions": [1],
            "engine": "synthetic",
            "model_space_id": "space",
            "in_space_id": "matrix",
            "payload_tree": {"model.txt": "input"},
            "storage_profile": "model_ref_contained_tree_v1",
        },
        "cell_vector": {
            "kind": "cell_vector",
            "dimensions": [1],
            "cell_id": cell,
            "value": ["a"],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
        "cell_vector_family": {
            "kind": "cell_vector_family",
            "dimensions": [1, 3],
            "cell_id": cell,
            "key": ["k"],
            "value": [1.0],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
        "relation_family": {
            "kind": "relation_family",
            "dimensions": [1, 3],
            "cell_id": cell,
            "key": ["k"],
            "value": [1.0],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
        "feature_table": {
            "kind": "feature_table",
            "dimensions": [1, 1],
            "feature_id": feature,
            "rows": [{"feature_id": "feature_1"}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
        "obs_table": {
            "kind": "obs_table",
            "dimensions": [1, 1],
            "rows": [{"value": 1}],
            "storage_profile": "terminal_primitive_v1",
            "meta": {},
        },
        "path_2d_family": {
            "kind": "path_2d_family",
            "dimensions": [1, 4],
            "key": ["p"],
            "point": [1],
            "x": [0.0],
            "y": [1.0],
            "storage_profile": "path_2d_family_v1",
            "meta": {},
        },
        "space_path_family": {
            "kind": "space_path_family",
            "dimensions": [1, 5],
            "space_id": ["space"],
            "path_id": ["p"],
            "point": [1],
            "dim": [1],
            "value": [0.0],
            "storage_profile": "space_path_family_v1",
            "meta": {},
        },
    }


def _reader(kind: str) -> str:
    return {
        "matrix": "read_matrix",
        "space": "read_space",
        "space_2d": "read_space_2d",
        "model_ref": "read_model_ref",
        "path_2d_family": "read_path_2d_family",
        "space_path_family": "read_space_path_family",
    }.get(kind, "read_terminal_primitive")


def _input_descriptor(name: str, value: dict, declaration: dict) -> dict:
    kind = declaration["kind"]
    descriptor = {
        "kind": kind,
        "storage_profile": value["storage_profile"],
        "reader": _reader(kind),
        "dimensions": value["dimensions"],
        "alignment": declaration["alignment"],
        "content_identity": _identity(f"scworkbench.{kind}.content.v1", value),
        "ancestry_identity": _identity(
            "scworkbench.provenance.ancestry.v1", {"input": name, "value": value}
        ),
        "payload_manifest_identity": _identity(
            "scworkbench.payload.manifest.v2", {"input": name, "value": value}
        ),
    }
    if kind in {
        "matrix",
        "space",
        "space_2d",
        "cell_vector",
        "cell_vector_family",
        "relation_family",
    }:
        descriptor["cell_order_identity"] = _identity("scworkbench.cell_order.v1", value["cell_id"])
    if kind in {"matrix", "feature_table"}:
        descriptor["feature_order_identity"] = _identity(
            "scworkbench.feature_order.v1", value["feature_id"]
        )
    if kind in {
        "cell_vector",
        "cell_vector_family",
        "relation_family",
        "feature_table",
        "obs_table",
        "path_2d_family",
        "space_path_family",
    }:
        descriptor["semantic_metadata_identity"] = _identity(
            "scworkbench.operation_pack.semantic_metadata.v2", value.get("meta", {})
        )
    if kind == "path_2d_family":
        descriptor["row_order_identity"] = _identity(
            "scworkbench.row_order.v1", [value["key"], value["point"]]
        )
    if kind == "space_path_family":
        descriptor["row_order_identity"] = _identity(
            "scworkbench.row_order.v1",
            [value[key] for key in ("space_id", "path_id", "point", "dim")],
        )
        descriptor["linked_space_identity"] = _identity(
            "scworkbench.space.content.v1", sorted(set(value["space_id"]))
        )
    if kind == "model_ref":
        descriptor["model_linkage_identity"] = _identity("scworkbench.model_linkage.v1", value)
        descriptor["contained_payload_identity"] = _identity(
            "scworkbench.operation_pack.contained_payload.v2", value
        )
    descriptor["descriptor_identity"] = _identity(
        "scworkbench.operation_pack.input_descriptor.v2",
        descriptor,
        "descriptor excluding descriptor_identity",
    )
    return descriptor


def _writer(output: dict) -> str:
    if output["category"] == "core_registry_request":
        return "request_cell_set_admission"
    return {
        "matrix": "write_matrix",
        "space": "write_space",
        "space_2d": "write_space_2d",
        "model_ref": "write_model_ref",
        "path_2d_family": "write_path_2d_family",
        "space_path_family": "write_space_path_family",
    }.get(output["kind"], "write_terminal_primitive")


def _semantic(output: dict) -> dict:
    kind = output["kind"]
    return {
        "matrix": {"value_domain": "real", "finite_values": True, "sparse_allowed": True},
        "space": {"finite_values": True, "minimum_dimensions": 1},
        "space_2d": {"finite_values": True, "coordinate_dimensions": 2},
        "model_ref": {"engine": "synthetic", "contained": True, "no_symlinks": True},
        "cell_vector": {
            "value_kind": output.get("value_kind", "categorical"),
            "missing_values": "forbidden",
        },
        "cell_vector_family": {
            "value_kind": output.get("value_kind", "continuous"),
            "missing_values": "forbidden",
        },
        "relation_family": {"value_kind": "continuous", "missing_values": "forbidden"},
        "feature_table": {
            "table_type": output.get("table_type", "generic"),
            "row_order": "canonical",
        },
        "obs_table": {"table_type": output.get("table_type", "generic"), "row_order": "canonical"},
        "path_2d_family": {"finite_coordinates": True, "key_fields": ["key", "point"]},
        "space_path_family": {
            "finite_coordinates": True,
            "key_fields": ["space_id", "path_id", "point", "dim"],
        },
        "cell_set_admission": {
            "members_unique": True,
            "members_in_universe": True,
            "order": "core_cell_order",
        },
    }[kind]


def _expected_dimensions(kind: str) -> list[int]:
    return {
        "matrix": [1, 1],
        "space": [1, 1],
        "space_2d": [1, 2],
        "model_ref": [1],
        "cell_vector": [1],
        "cell_vector_family": [1, 3],
        "relation_family": [1, 3],
        "feature_table": [1, 1],
        "obs_table": [1, 1],
        "path_2d_family": [1, 4],
        "space_path_family": [1, 5],
        "cell_set_admission": [1],
    }[kind]


def _output_descriptor(
    output: dict, input_names: list[str], cells: list[str], features: list[str]
) -> dict:
    kind = output["kind"]
    alignment = (
        "cell_and_feature_ordered"
        if kind == "matrix"
        else "cell_ordered"
        if kind
        in {
            "space",
            "space_2d",
            "cell_vector",
            "cell_vector_family",
            "relation_family",
            "cell_set_admission",
        }
        else "feature_ordered"
        if kind == "feature_table"
        else "model_linked"
        if kind == "model_ref"
        else "canonical_row_ordered"
        if kind == "path_2d_family"
        else "associated_space"
        if kind == "space_path_family"
        else "none"
    )
    descriptor = {
        "category": output["category"],
        "kind": kind,
        "storage_profile": output["storage_profile"],
        "writer": _writer(output),
        "expected_dimensions": _expected_dimensions(kind),
        "expected_alignment": alignment,
        "expected_ancestry_identity": _identity(
            "scworkbench.provenance.ancestry.v1",
            {"output": output["name"], "inputs": input_names},
        ),
        "expected_model_linkage": {"mode": "none"},
        "semantic_constraints": _semantic(output),
    }
    if output["category"] != "core_registry_request":
        descriptor["relative_root"] = f"outputs/{output['name']}"
    if kind in {
        "matrix",
        "space",
        "space_2d",
        "cell_vector",
        "cell_vector_family",
        "relation_family",
        "cell_set_admission",
    }:
        descriptor["expected_cell_order_identity"] = _identity("scworkbench.cell_order.v1", cells)
    if kind in {"matrix", "feature_table"}:
        descriptor["expected_feature_order_identity"] = _identity(
            "scworkbench.feature_order.v1", features
        )
    if kind in {"path_2d_family", "space_path_family"}:
        row_value = [["p"], [1]] if kind == "path_2d_family" else [["space_file"], ["p"], [1], [1]]
        descriptor["expected_row_order_identity"] = _identity("scworkbench.row_order.v1", row_value)
    if kind == "space_path_family":
        descriptor["expected_linked_space_identity"] = _identity(
            "scworkbench.space.content.v1", ["space_file"]
        )
    if kind == "model_ref":
        descriptor["contained_payload_requirements"] = {
            "contained": True,
            "no_symlinks": True,
            "manifest_scheme": "scworkbench.operation_pack.payload_manifest.v2",
        }
    descriptor["grant_identity"] = _identity(
        "scworkbench.operation_pack.output_grant.v2",
        descriptor,
        "descriptor excluding grant_identity",
    )
    return descriptor


def make_request(discovery, stage: Path, operation_name: str, inputs: dict[str, dict]):
    token = uuid.uuid4().hex
    operation = next(
        item
        for item in discovery.manifest["operations"]
        if item["qualified_name"] == operation_name
    )
    selected_inputs = {
        declaration["name"]: inputs[declaration["name"]] for declaration in operation["inputs"]
    }
    input_records = []
    for declaration in operation["inputs"]:
        name = declaration["name"]
        value = selected_inputs[name]
        path = stage / "inputs" / name / "payload.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from pybench.operation_packs import canonical_json

        path.write_text(canonical_json(value), encoding="utf-8")
        input_records.append(
            {
                "name": name,
                "reference": f"{declaration['kind']}:{name}",
                "descriptor": _input_descriptor(name, value, declaration),
            }
        )
    first = next(iter(selected_inputs.values()))
    cells = first.get("cell_id", ["cell_1"])
    features = first.get("feature_id", ["feature_1"])
    output_records = [
        {
            "name": output["name"],
            "allocated_id": f"allocated_{output['name']}",
            "descriptor": _output_descriptor(output, list(selected_inputs), cells, features),
        }
        for output in operation["outputs"]
    ]
    transition = {
        "transition_id": f"transition_{token}",
        "session_id": f"session_{token}",
        "run_id": f"run_{token}",
        "checkpoint_generation": f"generation_{token}",
        "source_state_identity": _identity(
            "scworkbench.scientific_state.v1", {"fixture": "python-v2"}
        ),
    }
    transition["transition_identity"] = _identity(
        "scworkbench.operation_pack.transition.v2",
        transition,
        "transition excluding transition_identity",
    )
    declared = operation["dependencies"]
    selected = declared
    dynamic = []
    dispositions = {}
    if declared:
        dependency = declared[0]
        provider = selected_inputs[dependency["input_name"]]["meta"][dependency["metadata_field"]]
        import importlib.metadata as metadata

        distribution = metadata.distribution(provider)
        evidence = distribution_identity_v2(distribution)
        input_record = next(
            item for item in input_records if item["name"] == dependency["input_name"]
        )
        resolution = {
            "requirement_id": dependency["requirement_id"],
            "selector_identity": _identity(
                "scworkbench.operation_pack.dynamic_dependency_selector.v2", dependency
            ),
            "input_name": dependency["input_name"],
            "input_identity": input_record["descriptor"]["content_identity"],
            "metadata_field": dependency["metadata_field"],
            "ecosystem": "Python",
            "provider_name": provider,
            "package_name_scheme": "python_distribution_name_v1",
            "probe": "python_import_spec_v1",
            "disposition": "required",
            "resolved_package": {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "artifact_identity": evidence,
                "probe_evidence_identity": _identity(
                    "scworkbench.python_import_spec.v1", {"import_name": provider}
                ),
            },
            "status": "available",
        }
        resolution["resolution_identity"] = _identity(
            "scworkbench.operation_pack.dynamic_dependency_resolution.v2",
            resolution,
            "resolution excluding resolution_identity",
        )
        dynamic = [resolution]
        dispositions[dependency["requirement_id"]] = resolution
    declared_identity = _identity("scworkbench.operation_pack.dependencies.v2", declared)
    selected_identity = _identity("scworkbench.operation_pack.dependencies.v2", selected)
    preflight_identity = _identity(
        "scworkbench.operation_pack.preflight.v2",
        {
            "declared": declared,
            "selected": selected,
            "dispositions": dispositions,
            "dynamic": dynamic,
            "network_policy": "deny",
        },
    )
    identity = discovery.identity
    request = {
        "schema": "scworkbench.operation_pack.transition_request.v2",
        "message_type": "request",
        "process_protocol_version": "pybench-op-v3",
        "pack_abi_version": "2",
        "canonical_json_scheme": "scworkbench.operation_pack.canonical_json.v1",
        "transition": transition,
        "operation_authentication": {
            "name": operation_name,
            "pack_identity": identity["pack_identity"],
            "manifest_identity": identity["manifest_identity"],
            "manifest_source_identity": discovery.manifest_source_identity,
            "distribution_identity": discovery.distribution_identity,
            "operation_definition_identity": identity["operation_definition_identities"][
                operation_name
            ],
            "entry_point": {
                "value": discovery.manifest["entry_point"],
                "identity": discovery.entry_point_identity,
            },
        },
        "engine_contract": {
            "implementation": "scworkbench",
            "implementation_version": "phase2b-fixture",
            "scientific_state_scheme": "scworkbench.scientific_state.v1",
            "scientific_state_schema_identity": _identity(
                "scworkbench.scientific_state.schema.v1", "1"
            ),
            "reader_api": "scworkbench.operation_pack.typed_readers.v2",
            "writer_api": "scworkbench.operation_pack.staged_writers.v2",
        },
        "dependency_resolution": {
            "declared_requirement_set_identity": declared_identity,
            "selected_requirement_set_identity": selected_identity,
            "dynamic_resolutions": dynamic,
            "preflight_report_identity": preflight_identity,
            "network_policy": "deny",
        },
        "inputs": input_records,
        "params": {},
        "outputs": output_records,
    }
    capability = {
        "capability_id": f"capability_{token}",
        "mode": "private_staging_write",
        "transition_identity": transition["transition_identity"],
        "session_id": transition["session_id"],
        "run_id": transition["run_id"],
        "checkpoint_generation": transition["checkpoint_generation"],
        "source_state_identity": transition["source_state_identity"],
        "staging_root_identity": _identity(
            "scworkbench.operation_pack.staging_root.v1", {"stage": "private"}
        ),
        "lease_id": f"lease_{token}",
        "pack_identity": identity["pack_identity"],
        "distribution_identity": discovery.distribution_identity,
        "manifest_source_identity": discovery.manifest_source_identity,
        "operation_definition_identity": identity["operation_definition_identities"][
            operation_name
        ],
        "entry_point_identity": discovery.entry_point_identity,
        "selected_requirement_set_identity": selected_identity,
        "preflight_report_identity": preflight_identity,
        "reader_grants": [
            {
                "input_name": item["name"],
                "reader": item["descriptor"]["reader"],
                "descriptor_identity": item["descriptor"]["descriptor_identity"],
            }
            for item in input_records
        ],
        "writer_grants": [
            {
                "output_name": item["name"],
                "writer": item["descriptor"]["writer"],
                "grant_identity": item["descriptor"]["grant_identity"],
            }
            for item in output_records
        ],
        "single_use": True,
    }
    capability["capability_identity"] = _identity(
        "scworkbench.operation_pack.capability.v2",
        capability,
        "capability excluding capability_identity",
    )
    request["execution_capability"] = capability
    request["request_identity"] = _identity(
        "scworkbench.operation_pack.transition_request.v2",
        request,
        "request excluding request_identity",
    )
    return copy.deepcopy(request)


def deep_copy(value):
    return copy.deepcopy(value)
