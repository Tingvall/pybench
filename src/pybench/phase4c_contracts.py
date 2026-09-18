from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

from .operation_packs import OperationPackError
from .operation_packs_v2 import _identity


def _fail(code: str, message: str, phase: str) -> None:
    raise OperationPackError(code, message, phase=phase)


def plain(identity: Mapping[str, Any]) -> dict[str, str]:
    return {"scheme": identity["scheme"], "sha256": identity["sha256"]}


def assert_self_identity(value: Mapping[str, Any], field: str, code: str, phase: str) -> None:
    identity = value.get(field)
    payload = {key: child for key, child in value.items() if key != field}
    if (
        not isinstance(identity, Mapping)
        or identity.get("sha256") != _identity(identity.get("scheme", ""), payload)["sha256"]
    ):
        _fail(code, f"Self identity mismatch at {field}", phase)


def validate_parameter_axis_grants(
    request: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    declarations = operation.get("parameter_axes", [])
    grants = request.get("parameter_axis_grants", [])
    if [item["name"] for item in grants] != [item["name"] for item in declarations]:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Parameter-axis grant closure differs from the manifest",
            "staging",
        )
    slices = {item["name"]: item for item in request.get("typed_slice_grants", [])}
    result: dict[str, dict[str, Any]] = {}
    for declaration, grant in zip(declarations, grants, strict=True):
        assert_self_identity(grant, "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
        source_decl = declaration["maximum"]
        slice_grant = slices.get(source_decl["typed_slice_read"])
        if slice_grant is None:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Parameter-axis slice grant is absent", "staging")
        maximum = min(
            slice_grant["cells_axis"]["count"] + source_decl["cell_offset"],
            slice_grant["features_axis"]["count"] + source_decl["feature_offset"],
        )
        value = request["params"].get(declaration["parameter"])
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Parameter-axis value is not an integer",
                "staging",
            )
        labels = [f"{declaration['labels']['prefix']}{index}" for index in range(1, value + 1)]
        expected = {
            "schema": "scworkbench.operation_pack.parameter_axis_grant.v1",
            "name": declaration["name"],
            "parameter": declaration["parameter"],
            "value": value,
            "minimum": declaration["minimum"],
            "maximum": maximum,
            "labels": labels,
            "source": {
                "typed_slice_grant_identity": plain(slice_grant["grant_identity"]),
                "cell_axis_count": slice_grant["cells_axis"]["count"],
                "feature_axis_count": slice_grant["features_axis"]["count"],
            },
            "axis_order_identity": _identity("scworkbench.dimension_order.v1", labels),
        }
        if (
            value < declaration["minimum"]
            or value > maximum
            or any(grant.get(key) != child for key, child in expected.items())
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Parameter-axis grant differs from normalized parameters or typed axes",
                "staging",
            )
        result[grant["name"]] = copy.deepcopy(grant)
    return result


def validate_publication_grant(
    grant: Mapping[str, Any],
    policy: Mapping[str, Any],
    request: Mapping[str, Any],
    output: Mapping[str, Any],
) -> None:
    assert_self_identity(
        grant, "publication_grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging"
    )
    params = request["params"]
    expected_mode = "replace" if grant["preimage"]["state"] == "present" else "add"
    expected_conflict = (
        "replace_exact_preimage_v1" if expected_mode == "replace" else "require_absent_v1"
    )
    closure = grant["closure_outputs"]
    if (
        grant["schema"] != "scworkbench.operation_pack.materialization_publication_grant.v1"
        or grant["output"] != policy["output"]
        or grant["target_kind"] != policy["target_kind"]
        or grant["target_id"] != params.get(policy["target_id_parameter"])
        or grant["target_id"] != output["allocated_id"]
        or grant["mode"] != expected_mode
        or grant["conflict_policy"] != expected_conflict
        or closure != sorted(closure)
        or output["name"] not in closure
        or (expected_mode == "replace" and params.get(policy["overwrite_parameter"]) is not True)
        or grant["preimage"].get("transition_source_state_identity")
        != request["transition"]["source_state_identity"]
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Materialization publication grant differs", "staging")


def validate_fragment_grant(
    grant: Mapping[str, Any], declaration: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    assert_self_identity(grant, "grant_identity", "PYBENCH_CAPABILITY_MISMATCH", "staging")
    schedule = grant["source_fragment_schedule"]
    assert_self_identity(
        schedule, "schedule_identity", "PYBENCH_INPUT_IDENTITY_MISMATCH", "resolution"
    )
    slices = {item["name"]: item for item in request.get("typed_slice_grants", [])}
    source = slices.get(declaration["source_typed_slice_read"])
    if source is None:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH", "Matrix fragment typed-slice grant is absent", "staging"
        )
    fragments = schedule["fragments"]
    if schedule["source_fragment_manifest_identity"] != _identity(
        "scworkbench.operation_pack.source_fragment_manifest.v1", fragments
    ):
        _fail(
            "PYBENCH_INPUT_IDENTITY_MISMATCH",
            "Source fragment manifest identity differs",
            "resolution",
        )
    expected_start = 1
    for ordinal, fragment in enumerate(fragments, start=1):
        if (
            fragment["ordinal"] != ordinal
            or fragment["cell_start"] != expected_start
            or fragment["cell_count"] < 1
            or fragment["cell_start"] + fragment["cell_count"] - 1 > source["cells_axis"]["count"]
        ):
            _fail(
                "PYBENCH_INPUT_IDENTITY_MISMATCH",
                "Source fragment schedule is invalid",
                "resolution",
            )
        expected_start += fragment["cell_count"]
    if (
        not fragments
        or expected_start != source["cells_axis"]["count"] + 1
        or grant["source_input"] != declaration["source_input"]
        or grant["typed_slice_grant_identity"] != plain(source["grant_identity"])
        or grant["expected_dimensions"]
        != [source["features_axis"]["count"], source["cells_axis"]["count"]]
        or grant["feature_order_identity"] != source["features_axis"]["order_identity"]
        or grant["cell_order_identity"] != source["cells_axis"]["order_identity"]
        or schedule["input_descriptor_identity"] != source["input_descriptor_identity"]
        or schedule["matrix_content_identity"] != source["input_content_identity"]
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Matrix fragment grant differs", "staging")


def validate_axis_bound_outputs(
    operation: Mapping[str, Any],
    request: Mapping[str, Any],
    writes: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    axes = {item["name"]: item for item in request.get("parameter_axis_grants", [])}
    slices = {item["name"]: item for item in request.get("typed_slice_grants", [])}
    observations: dict[str, dict[str, Any]] = {}
    for policy in operation.get("dimension_policies", []):
        kind = policy.get("policy")
        if kind not in {
            "cell_by_parameter_axis_v1",
            "feature_parameter_axis_cartesian_v1",
            "one_per_parameter_axis_v1",
            "singleton_parameter_echo_v1",
        }:
            continue
        axis = axes[
            policy["parameter_axis"] if "parameter_axis" in policy else policy["dimension_axis"]
        ]
        labels = axis["labels"]
        value = writes[policy["output"]].value
        valid = True
        observed: dict[str, Any]
        if kind == "cell_by_parameter_axis_v1":
            source = slices[policy["cell_axis"]["typed_slice_read"]]
            cells = list(inputs[source["input_name"]]["cell_id"])
            valid = (
                value.get("kind") == "space"
                and value.get("dimensions") == [len(cells), len(labels)]
                and value.get("cell_id") == cells
                and value.get("dimension_id") == labels
                and finite_matrix(value.get("values"), len(cells), len(labels))
            )
            observed = {
                "dimensions": [len(cells), len(labels)],
                "cell_order_identity": _identity("scworkbench.cell_order.v1", cells),
                "dimension_order_identity": _identity("scworkbench.dimension_order.v1", labels),
            }
        elif kind == "feature_parameter_axis_cartesian_v1":
            source = slices[policy["feature_axis"]["typed_slice_read"]]
            features = list(inputs[source["input_name"]]["feature_id"])
            rows = value.get("rows", [])
            expected = [(label, feature) for label in labels for feature in features]
            observed = [
                (row.get(policy["axis_label_field"]), row.get(policy["feature_field"]))
                for row in rows
            ]
            valid = (
                value.get("kind") == "feature_table"
                and observed == expected
                and isinstance(value.get("dimensions"), list)
                and len(value["dimensions"]) == 2
                and value["dimensions"][0] == len(rows)
            )
            valid &= [row.get(policy["axis_index_field"]) for row in rows] == [
                index for index in range(1, len(labels) + 1) for _ in features
            ]
            observed = {
                "dimensions": copy.deepcopy(value.get("dimensions")),
                "row_order_identity": _identity(
                    "scworkbench.row_order.v1",
                    [{"label": label, "feature_id": feature} for label, feature in expected],
                ),
            }
        elif kind == "one_per_parameter_axis_v1":
            rows = value.get("rows", [])
            valid = (
                value.get("kind") == "obs_table"
                and isinstance(value.get("dimensions"), list)
                and len(value["dimensions"]) == 2
                and value["dimensions"][0] == len(rows)
            )
            valid &= [row.get(policy["axis_label_field"]) for row in rows] == labels
            valid &= [row.get(policy["axis_index_field"]) for row in rows] == list(
                range(1, len(labels) + 1)
            )
            observed = {
                "dimensions": copy.deepcopy(value.get("dimensions")),
                "row_order_identity": _identity("scworkbench.row_order.v1", labels),
            }
        else:
            rows = value.get("rows", [])
            valid = (
                value.get("kind") == "obs_table"
                and len(rows) == 1
                and rows[0].get(policy["count_field"]) == len(labels)
                and isinstance(value.get("dimensions"), list)
                and len(value["dimensions"]) == 2
                and value["dimensions"][0] == 1
            )
            observed = {
                "dimensions": copy.deepcopy(value.get("dimensions")),
                "echoed_value": len(labels),
            }
        if not valid:
            _fail(
                "PYBENCH_OUTPUT_INVALID",
                f"Output {policy['output']!r} violates its parameter axis",
                "audit",
            )
        observed["parameter_axis_grant_identity"] = plain(axis["grant_identity"])
        observations[policy["output"]] = observed
    return observations


def publication_observations(
    request: Mapping[str, Any], outputs: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_name = {item["name"]: item for item in outputs}
    result: list[dict[str, Any]] = []
    for output in request["outputs"]:
        grant = output["descriptor"].get("publication_grant")
        if grant is None:
            continue
        closure = [
            {
                "name": name,
                "observed_descriptor_identity": plain(
                    by_name[name]["descriptor"]["observed_descriptor_identity"]
                ),
            }
            for name in grant["closure_outputs"]
        ]
        observed = {
            "schema": "scworkbench.operation_pack.observed_materialization_publication.v1",
            "publication_grant_identity": plain(grant["publication_grant_identity"]),
            "mode": grant["mode"],
            "preimage_identity": _identity(
                "scworkbench.operation_pack.materialization_preimage.v1",
                grant["preimage"],
            ),
            "output_observed_descriptor_identity": plain(
                by_name[grant["output"]]["descriptor"]["observed_descriptor_identity"]
            ),
            "closure_output_identities": closure,
            "status": "validated_not_published",
        }
        observed["observation_identity"] = _identity(
            observed["schema"], observed, "observation excluding observation_identity"
        )
        result.append(observed)
    return result


def fragment_payload_identity(path: Path, relative: str) -> dict[str, str]:
    return _identity(
        "scworkbench.operation_pack.payload_manifest.v2",
        {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        "matrix fragment payload tree",
    )


def matrix_slice_content_identity(
    values: list[list[float]], feature_ids: list[str], cell_ids: list[str]
) -> dict[str, str]:
    return _identity(
        "scworkbench.operation_pack.matrix_slice.content.v1",
        {
            "schema": "scworkbench.operation_pack.matrix_slice.content.v1",
            "kind": "matrix",
            "dimensions": [len(feature_ids), len(cell_ids)],
            "feature_id": feature_ids,
            "cell_id": cell_ids,
            "entries": [
                {"row": row + 1, "column": column + 1, "value": value}
                for column in range(len(cell_ids))
                for row in range(len(feature_ids))
                if (value := values[row][column]) != 0
            ],
        },
    )


def finite_matrix(values: Any, rows: int, columns: int) -> bool:
    return (
        isinstance(values, list)
        and len(values) == rows
        and all(isinstance(row, list) and len(row) == columns for row in values)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for row in values
            for value in row
        )
    )
