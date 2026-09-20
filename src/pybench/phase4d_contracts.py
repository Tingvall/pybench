from __future__ import annotations

import copy
import hashlib
import math
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .operation_packs import OperationPackError
from .operation_packs_v2 import _identity


def _fail(code: str, message: str, phase: str) -> None:
    raise OperationPackError(code, message, phase=phase)


def plain(identity: Mapping[str, Any]) -> dict[str, str]:
    return {"scheme": identity["scheme"], "sha256": identity["sha256"]}


def _selected_dimension_count(descriptor: Mapping[str, Any], value: Any) -> int:
    if value is None:
        dimensions = descriptor.get("dimensions", [])
        return dimensions[1] if isinstance(dimensions, list) and len(dimensions) == 2 else 0
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        return len(value)
    return 0


def safe_model_path(
    value: Any,
    *,
    code: str = "PYBENCH_OUT_OF_SCOPE_MUTATION",
    phase: str = "audit",
) -> str:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/"
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(character not in allowed for character in value)
    ):
        _fail(code, "Contained-model path is invalid", phase)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(code, "Contained-model path is not UTF-8", phase)
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        _fail(code, "Contained-model path escapes its root", phase)
    normalized = path.as_posix()
    if normalized != value:
        _fail(code, "Contained-model path is not canonical", phase)
    return normalized


def payload_file_identity(entry: Mapping[str, Any]) -> dict[str, str]:
    return _identity("scworkbench.operation_pack.payload_file.v1", entry)


def tree_payload_identity(manifest: Mapping[str, Any]) -> dict[str, str]:
    return _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {
            "contained_model_tree_manifest_identity": plain(manifest["manifest_identity"]),
            "entry_payload_identities": [
                payload_file_identity(entry) for entry in manifest["entries"]
            ],
        },
    )


def inventory_model_tree(
    root: Path,
    *,
    engine: str,
    model_type: str,
    primary_path: str,
    maximum_file_count: int,
    maximum_total_bytes: int,
    failure_code: str = "PYBENCH_OUTPUT_INVALID",
    phase: str = "audit",
) -> tuple[dict[str, Any], list[Path]]:
    primary_path = safe_model_path(primary_path, code=failure_code, phase=phase)
    if not root.is_dir() or root.is_symlink():
        _fail(failure_code, "Contained-model tree is absent", phase)
    entries: list[dict[str, Any]] = []
    paths: list[Path] = []
    for path in sorted(
        root.rglob("*"), key=lambda child: child.relative_to(root).as_posix().encode()
    ):
        relative = safe_model_path(path.relative_to(root).as_posix())
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            _fail(
                "PYBENCH_OUT_OF_SCOPE_MUTATION",
                f"Contained-model entry {relative!r} is not a private regular file",
                "audit",
            )
        entries.append(
            {
                "path": relative,
                "size": info.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        paths.append(path)
    total_size = sum(entry["size"] for entry in entries)
    if (
        not entries
        or len(entries) > maximum_file_count
        or total_size > maximum_total_bytes
        or any(entry["size"] <= 0 for entry in entries)
        or primary_path not in {entry["path"] for entry in entries}
    ):
        _fail(failure_code, "Contained-model tree is incomplete or exceeds its grant", phase)
    manifest: dict[str, Any] = {
        "schema": "scworkbench.operation_pack.contained_model_tree_manifest.v1",
        "engine": engine,
        "model_type": model_type,
        "primary_path": primary_path,
        "file_count": len(entries),
        "total_size": total_size,
        "entries": entries,
    }
    manifest["manifest_identity"] = _identity(
        manifest["schema"], manifest, "manifest excluding manifest_identity"
    )
    return manifest, paths


def validate_tree_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Contained-model manifest differs", "staging")
    manifest = copy.deepcopy(dict(value))
    identity = manifest.pop("manifest_identity", None)
    expected = _identity(
        "scworkbench.operation_pack.contained_model_tree_manifest.v1",
        manifest,
        "manifest excluding manifest_identity",
    )
    entries = manifest.get("entries")
    entry_paths = []
    if isinstance(entries, list):
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"path", "size", "sha256"}
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("size"), int)
                or isinstance(entry.get("size"), bool)
                or entry["size"] < 1
                or not isinstance(entry.get("sha256"), str)
                or len(entry["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in entry["sha256"])
            ):
                _fail(
                    "PYBENCH_CAPABILITY_MISMATCH",
                    "Contained-model manifest entry differs",
                    "staging",
                )
            entry_paths.append(entry["path"])
    if (
        not isinstance(identity, Mapping)
        or identity != expected
        or manifest.get("schema") != "scworkbench.operation_pack.contained_model_tree_manifest.v1"
        or not isinstance(entries, list)
        or not entries
        or entry_paths != sorted(entry_paths, key=lambda path: path.encode("utf-8"))
        or manifest.get("file_count") != len(entries)
        or manifest.get("total_size") != sum(entry.get("size", -1) for entry in entries)
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Contained-model manifest differs", "staging")
    names: list[str] = []
    for entry in entries:
        name = safe_model_path(
            entry.get("path"), code="PYBENCH_CAPABILITY_MISMATCH", phase="staging"
        )
        names.append(name)
    if len(names) != len(set(names)) or manifest.get("primary_path") not in set(names):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Contained-model path closure differs", "staging")
    return {**manifest, "manifest_identity": dict(identity)}


def validate_private_model_copy(root: Path, grant: Mapping[str, Any]) -> list[Path]:
    expected = validate_tree_manifest(grant["tree_manifest"])
    selected_model = grant.get("selected_model", {})
    observed, paths = inventory_model_tree(
        root,
        engine=grant.get("engine", selected_model.get("engine")),
        model_type=grant.get("model_type", selected_model.get("model_type")),
        primary_path=expected["primary_path"],
        maximum_file_count=expected["file_count"],
        maximum_total_bytes=expected["total_size"],
        failure_code="PYBENCH_INPUT_IDENTITY_MISMATCH",
        phase="resolution",
    )
    if (
        observed != expected
        or tree_payload_identity(observed) != grant["payload_manifest_identity"]
    ):
        _fail("PYBENCH_INPUT_IDENTITY_MISMATCH", "Contained-model bytes differ", "resolution")
    return paths


def validate_contained_model_grants(
    request: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate the additive contained-model read and write capability closure."""

    params = request["params"]
    input_descriptors = {item["name"]: item["descriptor"] for item in request["inputs"]}
    output_grants = {item["name"]: item for item in request["outputs"]}
    output_declarations = {item["name"]: item for item in operation["outputs"]}

    for output_name, output_declaration in output_declarations.items():
        policy = output_declaration.get("contained_model_policy")
        grant_record = output_grants[output_name]
        grant = grant_record["descriptor"].get("contained_model_write_grant")
        if (policy is None) != (grant is None):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model write grant presence differs from the manifest",
                "staging",
            )
        if grant is None:
            continue
        claimed = grant.get("write_grant_identity")
        payload = {key: child for key, child in grant.items() if key != "write_grant_identity"}
        if claimed != _identity(
            "scworkbench.operation_pack.contained_model_write_grant.v1",
            payload,
            "grant excluding write_grant_identity",
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model write grant identity differs",
                "staging",
            )
        source = input_descriptors.get(policy["source_space_input"])
        paired = output_grants.get(policy["paired_space_output"])
        dimension_count = _selected_dimension_count(
            source or {}, params.get(policy["dimensions_parameter"])
        )
        source_binding = grant["source_space"]
        paired_binding = grant["paired_space_output"]
        expected_linkage = grant_record["descriptor"]["expected_model_linkage"]
        valid = (
            grant["schema"] == "scworkbench.operation_pack.contained_model_write_grant.v1"
            and grant["output"] == output_name
            and grant["engine"] == policy["engine"]
            and grant["model_type"] == policy["model_type"]
            and grant["primary_path"] == policy["primary_path"]
            and grant["relative_root"] == f"outputs/{output_name}/model_tree"
            and grant["limits"]
            == {
                "maximum_file_count": policy["maximum_file_count"],
                "maximum_total_bytes": policy["maximum_total_bytes"],
            }
            and isinstance(source, Mapping)
            and source_binding["input_name"] == policy["source_space_input"]
            and source_binding["input_descriptor_identity"] == plain(source["descriptor_identity"])
            and source_binding["content_identity"] == source["content_identity"]
            and source_binding["cell_order_identity"] == source["cell_order_identity"]
            and source_binding["dimensions"] == source["dimensions"]
            and source_binding["selected_dimension_count"] == dimension_count
            and dimension_count >= 1
            and expected_linkage
            == {
                "mode": "input",
                "reference_name": policy["source_space_input"],
                "reference_identity": source_binding["input_descriptor_identity"],
            }
            and isinstance(paired, Mapping)
            and paired_binding["name"] == policy["paired_space_output"]
            and paired_binding["allocated_id"] == paired["allocated_id"]
            and paired_binding["expected_dimensions"] == paired["descriptor"]["expected_dimensions"]
            and paired_binding["expected_dimensions"] == [source_binding["dimensions"][0], 2]
            and paired_binding["expected_cell_order_identity"]
            == paired["descriptor"]["expected_cell_order_identity"]
            and paired_binding["expected_cell_order_identity"]
            == source_binding["cell_order_identity"]
        )
        if not valid:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model write grant differs from its manifest or source space",
                "staging",
            )
        safe_model_path(grant["primary_path"], code="PYBENCH_CAPABILITY_MISMATCH", phase="staging")

    declarations = operation.get("contained_model_reads", [])
    grants = request.get("model_payload_grants", [])
    if [item["name"] for item in grants] != [item["name"] for item in declarations]:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Contained-model read grant closure differs from the manifest",
            "staging",
        )
    validated: list[dict[str, Any]] = []
    for declaration, grant in zip(declarations, grants, strict=True):
        claimed = grant.get("grant_identity")
        payload = {key: child for key, child in grant.items() if key != "grant_identity"}
        if claimed != _identity(
            "scworkbench.operation_pack.contained_model_read_grant.v1",
            payload,
            "grant excluding grant_identity",
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model read grant identity differs",
                "staging",
            )
        model = input_descriptors.get(declaration["input"])
        source = input_descriptors.get(declaration["source_space_input"])
        tree = validate_tree_manifest(grant["tree_manifest"])
        selected_count = _selected_dimension_count(
            source or {}, params.get(declaration["dimensions_parameter"])
        )
        source_binding = grant["source_space_binding"]
        expected_contained = _identity(
            "scworkbench.operation_pack.contained_payload.v2",
            plain(tree["manifest_identity"]),
        )
        expected_content = _identity(
            "scworkbench.model_ref.content.v1",
            {
                "engine": grant["engine"],
                "model_type": grant["model_type"],
                "linkage_identity": grant["model_linkage_identity"],
                "tree_manifest_identity": plain(tree["manifest_identity"]),
            },
        )
        valid = (
            grant["schema"] == "scworkbench.operation_pack.contained_model_read_grant.v1"
            and grant["name"] == declaration["name"]
            and grant["input_name"] == declaration["input"]
            and grant["reader"] == declaration["reader"]
            and grant["engine"] == declaration["engine"] == tree["engine"]
            and grant["model_type"] == declaration["model_type"] == tree["model_type"]
            and grant["access"] == declaration["access"]
            and grant["call_discipline"] == declaration["call_discipline"]
            and grant["relative_root"] == f"model_payload_reads/{grant['name']}/tree"
            and isinstance(model, Mapping)
            and grant["input_descriptor_identity"] == plain(model["descriptor_identity"])
            and grant["model_content_identity"] == model["content_identity"]
            and grant["model_linkage_identity"] == model["model_linkage_identity"]
            and grant["contained_payload_identity"] == model["contained_payload_identity"]
            and grant["contained_payload_identity"] == expected_contained
            and grant["model_content_identity"] == expected_content
            and grant["payload_manifest_identity"] == tree_payload_identity(tree)
            and grant["payload_manifest_identity"]
            == model.get("payload_manifest_identity", grant["payload_manifest_identity"])
            and isinstance(source, Mapping)
            and source_binding["input_name"] == declaration["source_space_input"]
            and source_binding["input_descriptor_identity"] == plain(source["descriptor_identity"])
            and source_binding["selected_dimension_count"] == selected_count
            and selected_count >= 1
        )
        if not valid:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model read grant differs from its manifest or authenticated inputs",
                "staging",
            )
        validated.append(copy.deepcopy(grant))
    return validated


def validate_contained_model_source_axes(
    request: Mapping[str, Any],
    operation: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
) -> None:
    params = request["params"]
    write_grants = {
        item["name"]: item["descriptor"].get("contained_model_write_grant")
        for item in request["outputs"]
    }
    read_grants = {item["name"]: item for item in request.get("model_payload_grants", [])}

    def check(source_name: str, parameter: str, binding: Mapping[str, Any]) -> None:
        source = inputs[source_name]
        dimensions = source.get("dimensions", [])
        width = dimensions[1] if isinstance(dimensions, list) and len(dimensions) == 2 else 0
        labels = source.get("dimension_id")
        if not isinstance(labels, list) or len(labels) != width:
            labels = [f"dim{index}" for index in range(1, width + 1)]
        selected = params.get(parameter)
        selected = list(range(1, width + 1)) if selected is None else list(selected)
        if (
            not selected
            or any(
                not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= width
                for index in selected
            )
            or len(selected) != len(set(selected))
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Contained-model selected dimensions are invalid",
                "resolution",
            )
        selected_labels = [labels[index - 1] for index in selected]
        if binding["selected_dimension_count"] != len(selected_labels) or binding[
            "selected_dimension_order_identity"
        ] != _identity("scworkbench.dimension_order.v1", selected_labels):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Contained-model selected dimension binding differs",
                "staging",
            )

    for declaration in operation["outputs"]:
        policy = declaration.get("contained_model_policy")
        grant = write_grants.get(declaration["name"])
        if policy is not None and grant is not None:
            check(
                policy["source_space_input"],
                policy["dimensions_parameter"],
                grant["source_space"],
            )
    for declaration in operation.get("contained_model_reads", []):
        grant = read_grants[declaration["name"]]
        check(
            declaration["source_space_input"],
            declaration["dimensions_parameter"],
            grant["source_space_binding"],
        )


def validate_space_2d_publication_grant(
    grant: Mapping[str, Any],
    policy: Mapping[str, Any],
    request: Mapping[str, Any],
    output: Mapping[str, Any],
) -> None:
    claimed = grant.get("publication_grant_identity")
    payload = {key: child for key, child in grant.items() if key != "publication_grant_identity"}
    if claimed != _identity(
        "scworkbench.operation_pack.space_2d_publication_grant.v1",
        payload,
        "grant excluding publication_grant_identity",
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Space publication identity differs", "staging")
    inputs = {item["name"]: item["descriptor"] for item in request["inputs"]}
    input_record = inputs.get(policy["cell_axis_input"])
    model_binding = grant["source_model_binding"]
    declared_binding = policy["model_binding"]
    valid_model = model_binding["source"] == declared_binding["source"]
    if declared_binding["source"] == "input":
        model = inputs.get(declared_binding["name"])
        model_payload = next(
            (
                item
                for item in request.get("model_payload_grants", [])
                if item["input_name"] == declared_binding["name"]
            ),
            None,
        )
        valid_model &= (
            isinstance(model, Mapping)
            and isinstance(model_payload, Mapping)
            and model_binding["name"] == declared_binding["name"]
            and model_binding["engine"] == model_payload["engine"]
            and model_binding["model_type"] == model_payload["model_type"]
            and model_binding["model_linkage_identity"] == model["model_linkage_identity"]
            and model_binding["contained_payload_identity"] == model["contained_payload_identity"]
        )
    else:
        model = next(
            (item for item in request["outputs"] if item["name"] == declared_binding["name"]),
            None,
        )
        model_write = (
            model["descriptor"].get("contained_model_write_grant")
            if isinstance(model, Mapping)
            else None
        )
        valid_model &= (
            isinstance(model_write, Mapping)
            and model_binding["name"] == declared_binding["name"]
            and model_binding["allocated_id"] == model["allocated_id"]
            and model_binding["engine"] == model_write["engine"]
            and model_binding["model_type"] == model_write["model_type"]
            and model_binding["source_space_binding_identity"]
            == _identity(
                "scworkbench.operation_pack.model_source_space.v1",
                {
                    key: model_write["source_space"][key]
                    for key in (
                        "input_name",
                        "input_descriptor_identity",
                        "selected_dimension_count",
                        "selected_dimension_order_identity",
                    )
                },
            )
        )
    mode_policy = policy["mode"]
    expected_mode = "add" if mode_policy["policy"] == "add_only_v1" else grant["mode"]
    expected_conflict = {
        "add": "require_absent_v1",
        "selected_cell_merge": "merge_exact_preimage_v1",
        "full_replace": "replace_exact_preimage_v1",
    }.get(grant["mode"])
    overwrite = (
        False
        if mode_policy["policy"] == "add_only_v1"
        else request["params"].get(mode_policy["overwrite_parameter"])
    )
    selected = grant["selected_cells"]
    preimage = grant["preimage"]
    absent_preimage = {
        "state",
        "registry_absence_identity",
        "transition_source_state_identity",
    }
    present_preimage = {
        "state",
        "transition_source_state_identity",
        "registry_identity",
        "content_identity",
        "payload_manifest_identity",
        "provenance_identity",
        "lifecycle_identity",
        "cell_order_identity",
        "coordinate_dimensions",
    }
    preimage_shape_valid = (
        preimage.get("state") == "absent" and set(preimage) == absent_preimage
    ) or (
        preimage.get("state") == "present"
        and set(preimage) == present_preimage
        and preimage.get("coordinate_dimensions") == 2
    )
    valid = (
        grant["schema"] == "scworkbench.operation_pack.space_2d_publication_grant.v1"
        and grant["output"] == policy["output"] == output["name"]
        and grant["target_kind"] == policy["target_kind"] == "space_2d"
        and grant["target_id"] == request["params"].get(policy["target_id_parameter"])
        and grant["target_id"] == output["allocated_id"]
        and grant["mode"] == expected_mode
        and grant["conflict_policy"] == expected_conflict
        and preimage.get("transition_source_state_identity")
        == request["transition"]["source_state_identity"]
        and preimage_shape_valid
        and ((grant["mode"] == "add") == (preimage.get("state") == "absent"))
        and grant["overwrite_existing_cells"] is overwrite
        and isinstance(input_record, Mapping)
        and selected["count"] >= 1
        and selected["count"] == output["descriptor"]["expected_dimensions"][0]
        and selected["order_identity"] == output["descriptor"]["expected_cell_order_identity"]
        and selected["order_identity"] == input_record["cell_order_identity"]
        and grant["closure_outputs"] == sorted(grant["closure_outputs"])
        and output["name"] in grant["closure_outputs"]
        and valid_model
    )
    if not valid:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Space_2d publication grant differs from the manifest or inputs",
            "staging",
        )


def model_read_observations(
    grants: list[Mapping[str, Any]], use_counts: Mapping[str, int]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for grant in grants:
        observed: dict[str, Any] = {
            "schema": "scworkbench.operation_pack.observed_contained_model_read.v1",
            "name": grant["name"],
            "grant_identity": plain(grant["grant_identity"]),
            "input_descriptor_identity": grant["input_descriptor_identity"],
            "model_linkage_identity": grant["model_linkage_identity"],
            "payload_manifest_identity": grant["payload_manifest_identity"],
            "engine": grant["engine"],
            "model_type": grant["model_type"],
            "use_count": use_counts.get(grant["name"], 0),
            "integrity_status": "unchanged",
        }
        observed["observation_identity"] = _identity(
            observed["schema"], observed, "observation excluding observation_identity"
        )
        result.append(observed)
    return result


def validate_space_2d_candidates(request: Mapping[str, Any], writes: Mapping[str, Any]) -> None:
    for output in request["outputs"]:
        grant = output["descriptor"].get("publication_grant")
        if grant is None or grant.get("schema") != (
            "scworkbench.operation_pack.space_2d_publication_grant.v1"
        ):
            continue
        value = writes[output["name"]].value
        cells = value.get("cell_id")
        coordinates = value.get("values")
        valid = (
            value.get("kind") == "space_2d"
            and isinstance(cells, list)
            and cells
            and all(isinstance(cell, str) and cell for cell in cells)
            and len(cells) == len(set(cells))
            and value.get("dimensions") == [len(cells), 2]
            and isinstance(coordinates, list)
            and len(coordinates) == len(cells)
            and all(
                isinstance(row, list)
                and len(row) == 2
                and all(
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and math.isfinite(item)
                    for item in row
                )
                for row in coordinates
            )
            and grant["selected_cells"]["count"] == len(cells)
            and grant["selected_cells"]["order_identity"]
            == _identity("scworkbench.cell_order.v1", cells)
            and grant["selected_cells"]["membership_identity"]
            == _identity("scworkbench.cell_membership.v1", sorted(cells))
        )
        if not valid:
            _fail(
                "PYBENCH_OUTPUT_INVALID",
                "Space_2d candidate differs from its authenticated selection",
                "audit",
            )
