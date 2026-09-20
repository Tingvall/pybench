from __future__ import annotations

import copy
from typing import Any, Mapping

from .operation_packs import OperationPackError
from .operation_packs_v2 import _identity
from .phase4d_contracts import plain, tree_payload_identity, validate_tree_manifest

SELF_LINKED_READ_SCHEMA = "scworkbench.operation_pack.self_linked_contained_model_read.v1"
SELF_LINKED_GRANT_SCHEMA = "scworkbench.operation_pack.self_linked_contained_model_read_grant.v1"
SELF_LINKED_OBSERVATION_SCHEMA = (
    "scworkbench.operation_pack.observed_self_linked_contained_model_read.v1"
)
MODEL_SOURCE_LINKAGE_SCHEMA = "scworkbench.operation_pack.model_source_linkage.v2"
SIBLING_POLICY_SCHEMA = "scworkbench.operation_pack.contained_model_sibling_closure_policy.v1"
SIBLING_GRANT_SCHEMA = "scworkbench.operation_pack.contained_model_sibling_write_grant.v1"
SIBLING_OBSERVATION_SCHEMA = (
    "scworkbench.operation_pack.contained_model_sibling_tree_observation.v1"
)
MODEL_SIBLING_CLOSURE_SCHEMA = "scworkbench.operation_pack.model_sibling_closure.v1"

MAXIMUM_MODEL_FILE_COUNT = 32
MAXIMUM_MODEL_TOTAL_BYTES = 1_073_741_824


def _fail(code: str, message: str, phase: str) -> None:
    raise OperationPackError(code, message, phase=phase)


def _without(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: copy.deepcopy(child) for key, child in value.items() if key != field}


def _assert_self_identity(
    value: Mapping[str, Any],
    field: str,
    scheme: str,
    scope: str,
    *,
    code: str = "PYBENCH_CAPABILITY_MISMATCH",
    phase: str = "staging",
) -> None:
    if value.get(field) != _identity(scheme, _without(value, field), scope):
        _fail(code, f"{scheme} identity differs", phase)


def validate_model_source_linkage(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the durable, self-contained model/source linkage v2 value."""

    if not isinstance(value, Mapping):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Model source linkage is absent", "staging")
    linkage = copy.deepcopy(dict(value))
    required = {
        "engine",
        "model_type",
        "source_space_id",
        "source_space_content_identity",
        "fitted_cell_order_identity",
        "selected_dimension_count",
        "selected_dimension_order_identity",
        "source_snapshot_id",
        "linkage_identity",
    }
    if (
        set(linkage) != required
        or any(
            not isinstance(linkage.get(field), str) or not linkage[field]
            for field in ("engine", "model_type", "source_space_id", "source_snapshot_id")
        )
        or not isinstance(linkage.get("selected_dimension_count"), int)
        or isinstance(linkage.get("selected_dimension_count"), bool)
        or linkage["selected_dimension_count"] < 1
    ):
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Model source linkage differs", "staging")
    for field in (
        "source_space_content_identity",
        "fitted_cell_order_identity",
        "selected_dimension_order_identity",
    ):
        identity = linkage.get(field)
        if not isinstance(identity, Mapping) or set(identity) != {"scheme", "sha256"}:
            _fail("PYBENCH_CAPABILITY_MISMATCH", "Model source linkage differs", "staging")
    linkage_identity = linkage.get("linkage_identity")
    if not isinstance(linkage_identity, Mapping) or set(linkage_identity) != {
        "scheme",
        "scope",
        "sha256",
    }:
        _fail("PYBENCH_CAPABILITY_MISMATCH", "Model source linkage differs", "staging")
    _assert_self_identity(
        linkage,
        "linkage_identity",
        MODEL_SOURCE_LINKAGE_SCHEMA,
        "linkage excluding linkage_identity",
    )
    return linkage


def self_linked_declaration_identity(declaration: Mapping[str, Any]) -> dict[str, str]:
    return _identity(SELF_LINKED_READ_SCHEMA, declaration)


def validate_self_linked_model_grants(
    request: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Authenticate G1 grants without requiring a caller-supplied source space."""

    declarations = operation.get("self_linked_contained_model_reads", [])
    grants = request.get("self_linked_model_payload_grants", [])
    if [item.get("name") for item in grants] != [item.get("name") for item in declarations]:
        _fail(
            "PYBENCH_CAPABILITY_MISMATCH",
            "Self-linked contained-model read grant closure differs from the manifest",
            "staging",
        )
    inputs = {item["name"]: item["descriptor"] for item in request["inputs"]}
    result: list[dict[str, Any]] = []
    for declaration, grant_value in zip(declarations, grants, strict=True):
        grant = copy.deepcopy(dict(grant_value))
        _assert_self_identity(
            grant,
            "grant_identity",
            SELF_LINKED_GRANT_SCHEMA,
            "grant excluding grant_identity",
        )
        model = inputs.get(declaration["input"])
        tree = validate_tree_manifest(grant["tree_manifest"])
        linkage = validate_model_source_linkage(grant["model_source_linkage"])
        selected = grant["selected_model"]
        expected_contained = _identity(
            "scworkbench.operation_pack.contained_payload.v2",
            plain(tree["manifest_identity"]),
        )
        expected_content = _identity(
            "scworkbench.model_ref.content.v1",
            {
                "engine": selected["engine"],
                "model_type": selected["model_type"],
                "linkage_identity": linkage["linkage_identity"],
                "tree_manifest_identity": plain(tree["manifest_identity"]),
            },
        )
        limits = grant["limits"]
        valid = (
            grant.get("schema") == SELF_LINKED_GRANT_SCHEMA
            and grant.get("name") == declaration["name"]
            and grant.get("input_name") == declaration["input"]
            and grant.get("reader") == declaration["reader"]
            and grant.get("declaration_identity") == self_linked_declaration_identity(declaration)
            and selected in declaration["allowed_models"]
            and linkage["engine"] == selected["engine"] == tree["engine"]
            and linkage["model_type"] == selected["model_type"] == tree["model_type"]
            and isinstance(model, Mapping)
            and model.get("kind") == "model_ref"
            and grant.get("input_descriptor_identity") == plain(model["descriptor_identity"])
            and grant.get("model_content_identity") == model.get("content_identity")
            and grant.get("model_content_identity") == expected_content
            and grant.get("model_source_linkage_identity") == plain(linkage["linkage_identity"])
            and grant.get("model_source_linkage_identity")
            == plain(model.get("model_linkage_identity", {}))
            and grant.get("contained_payload_identity") == model.get("contained_payload_identity")
            and grant.get("contained_payload_identity") == expected_contained
            and grant.get("payload_manifest_identity") == tree_payload_identity(tree)
            and grant.get("payload_manifest_identity")
            == model.get("payload_manifest_identity", grant.get("payload_manifest_identity"))
            and limits
            == {
                "maximum_file_count": declaration["maximum_file_count"],
                "maximum_total_bytes": declaration["maximum_total_bytes"],
            }
            and tree["file_count"] <= limits["maximum_file_count"]
            and tree["total_size"] <= limits["maximum_total_bytes"]
            and grant.get("relative_root") == f"model_payload_reads/{grant['name']}/tree"
            and grant.get("access") == "verified_private_copy_read_only_v1"
            and grant.get("call_discipline") == declaration["call_discipline"]
        )
        if not valid:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Self-linked contained-model grant differs from its manifest or input",
                "staging",
            )
        result.append(grant)
    return result


def self_linked_model_read_observations(
    grants: list[Mapping[str, Any]], use_counts: Mapping[str, int]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for grant in grants:
        tree = grant["tree_manifest"]
        observation: dict[str, Any] = {
            "schema": SELF_LINKED_OBSERVATION_SCHEMA,
            "name": grant["name"],
            "grant_identity": copy.deepcopy(grant["grant_identity"]),
            "input_descriptor_identity": grant["input_descriptor_identity"],
            "model_source_linkage_identity": grant["model_source_linkage_identity"],
            "tree_manifest_identity": copy.deepcopy(tree["manifest_identity"]),
            "payload_manifest_identity": grant["payload_manifest_identity"],
            "engine": grant["selected_model"]["engine"],
            "model_type": grant["selected_model"]["model_type"],
            "use_count": use_counts.get(grant["name"], 0),
            "integrity_status": "unchanged",
        }
        observation["observation_identity"] = _identity(
            SELF_LINKED_OBSERVATION_SCHEMA,
            observation,
            "self_linked_model_read_observation excluding observation_identity",
        )
        result.append(observation)
    return result


def model_sibling_closure_value(
    model_name: str,
    model_grant_identity: Mapping[str, Any],
    linkage_identity: Mapping[str, Any],
    siblings: list[Mapping[str, Any]],
    promotion_group: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "model_output": model_name,
        "model_grant_identity": plain(model_grant_identity),
        "model_source_linkage_identity": plain(linkage_identity),
        "promotion_group": promotion_group,
        "siblings": [
            {
                "name": sibling["name"],
                "grant_identity": plain(sibling["grant_identity"]),
                "replay_policy": sibling["replay_policy"],
            }
            for sibling in siblings
        ],
    }
    value["closure_identity"] = _identity(
        MODEL_SIBLING_CLOSURE_SCHEMA,
        value,
        "model_sibling_closure excluding closure_identity",
    )
    return value


def validate_sibling_model_grants(
    request: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Authenticate G2 model grants and their ordered terminal closure."""

    params = request["params"]
    inputs = {item["name"]: item["descriptor"] for item in request["inputs"]}
    output_grants = {item["name"]: item for item in request["outputs"]}
    output_declarations = {item["name"]: item for item in operation["outputs"]}
    result: list[dict[str, Any]] = []
    for output_name, declaration in output_declarations.items():
        policy = declaration.get("contained_model_sibling_closure_policy")
        descriptor = output_grants[output_name]["descriptor"]
        grant = descriptor.get("contained_model_sibling_write_grant")
        if (policy is None) != (grant is None):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Sibling-bound contained-model grant presence differs",
                "staging",
            )
        if grant is None:
            continue
        _assert_self_identity(
            grant,
            "write_grant_identity",
            SIBLING_GRANT_SCHEMA,
            "grant excluding write_grant_identity",
        )
        source = inputs.get(policy["source_space_input"])
        source_binding = grant["source_space"]
        linkage = validate_model_source_linkage(grant["model_source_linkage"])
        sibling_names = policy["required_sibling_terminals"]
        siblings = grant["required_sibling_terminals"]
        sibling_declarations = [output_declarations.get(name) for name in sibling_names]
        sibling_grants = [output_grants.get(name) for name in sibling_names]
        dimension_parameter = policy["dimensions_parameter"]
        selected = params.get(dimension_parameter) if dimension_parameter is not None else None
        width = source.get("dimensions", [0, 0])[1] if isinstance(source, Mapping) else 0
        selected_count = (
            width if selected is None else len(selected) if isinstance(selected, list) else 0
        )
        base_descriptor = {
            key: copy.deepcopy(child)
            for key, child in descriptor.items()
            if key not in {"grant_identity", "contained_model_sibling_write_grant"}
        }
        base_model_grant_identity = _identity(
            "scworkbench.operation_pack.output_grant.v3",
            base_descriptor,
            "descriptor excluding grant_identity",
        )
        expected_closure = model_sibling_closure_value(
            output_name,
            base_model_grant_identity,
            linkage["linkage_identity"],
            siblings,
            declaration["promotion_group"],
        )
        valid_siblings = (
            [item.get("name") for item in siblings] == sibling_names
            and all(item is not None for item in sibling_declarations)
            and all(item is not None for item in sibling_grants)
        )
        if valid_siblings:
            for sibling, sibling_declaration, sibling_grant in zip(
                siblings, sibling_declarations, sibling_grants, strict=True
            ):
                assert sibling_declaration is not None and sibling_grant is not None
                expected_axis = sibling.get("expected_cell_order_identity_if_cell_indexed")
                requires_cell_axis = sibling_declaration["kind"] in {
                    "cell_vector",
                    "cell_vector_family",
                    "relation_family",
                }
                valid_siblings = valid_siblings and (
                    sibling["allocated_id"] == sibling_grant["allocated_id"]
                    and sibling["kind"] == sibling_declaration["kind"]
                    and sibling["grant_identity"]
                    == plain(sibling_grant["descriptor"]["grant_identity"])
                    and sibling["replay_policy"] == operation["replay_equivalence"][sibling["name"]]
                    and sibling_declaration["required"] is True
                    and sibling_declaration["category"] == "terminal_artifact"
                    and sibling_declaration["promotion_group"] == declaration["promotion_group"]
                    and (
                        expected_axis == source_binding["cell_order_identity"]
                        if requires_cell_axis
                        else expected_axis is None
                    )
                )
        valid = (
            grant.get("schema") == SIBLING_GRANT_SCHEMA
            and grant.get("output") == output_name
            and grant.get("engine") == policy["engine"] == linkage["engine"]
            and grant.get("model_type") == policy["model_type"] == linkage["model_type"]
            and grant.get("primary_path") == policy["primary_path"]
            and grant.get("relative_root") == f"outputs/{output_name}/model_tree"
            and grant.get("limits")
            == {
                "maximum_file_count": policy["maximum_file_count"],
                "maximum_total_bytes": policy["maximum_total_bytes"],
            }
            and isinstance(source, Mapping)
            and source_binding.get("input_name") == policy["source_space_input"]
            and source_binding.get("input_descriptor_identity")
            == plain(source["descriptor_identity"])
            and source_binding.get("content_identity") == source.get("content_identity")
            and source_binding.get("cell_order_identity") == source.get("cell_order_identity")
            and source_binding.get("dimensions") == source.get("dimensions")
            and source_binding.get("selected_dimension_count") == selected_count
            and selected_count >= 1
            and linkage["source_space_content_identity"] == source_binding["content_identity"]
            and linkage["fitted_cell_order_identity"] == source_binding["cell_order_identity"]
            and linkage["selected_dimension_count"] == selected_count
            and linkage["selected_dimension_order_identity"]
            == source_binding["selected_dimension_order_identity"]
            and valid_siblings
            and grant.get("model_sibling_closure_identity")
            == plain(expected_closure["closure_identity"])
        )
        if not valid:
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Sibling-bound contained-model grant differs from manifest or source",
                "staging",
            )
        result.append(copy.deepcopy(dict(grant)))
    return result


def validate_sibling_model_source_axes(
    request: Mapping[str, Any],
    operation: Mapping[str, Any],
    inputs: Mapping[str, Mapping[str, Any]],
) -> None:
    grants = {
        item["name"]: item["descriptor"].get("contained_model_sibling_write_grant")
        for item in request["outputs"]
    }
    for output in operation["outputs"]:
        policy = output.get("contained_model_sibling_closure_policy")
        grant = grants.get(output["name"])
        if policy is None or grant is None:
            continue
        source = inputs[policy["source_space_input"]]
        dimensions = source.get("dimensions", [])
        width = dimensions[1] if isinstance(dimensions, list) and len(dimensions) == 2 else 0
        labels = source.get("dimension_id")
        if not isinstance(labels, list) or len(labels) != width:
            labels = [f"dim{index}" for index in range(1, width + 1)]
        parameter = policy["dimensions_parameter"]
        selected = request["params"].get(parameter) if parameter is not None else None
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
                "Sibling-bound model selected dimensions are invalid",
                "resolution",
            )
        binding = grant["source_space"]
        linkage = grant["model_source_linkage"]
        selected_labels = [labels[index - 1] for index in selected]
        selected_identity = _identity("scworkbench.dimension_order.v1", selected_labels)
        if (
            binding["selected_dimension_count"] != len(selected)
            or binding["selected_dimension_order_identity"] != selected_identity
            or linkage["selected_dimension_count"] != len(selected)
            or linkage["selected_dimension_order_identity"] != selected_identity
            or (
                source.get("space_id") is not None
                and linkage["source_space_id"] != source["space_id"]
            )
        ):
            _fail(
                "PYBENCH_CAPABILITY_MISMATCH",
                "Sibling-bound model source-axis binding differs",
                "staging",
            )


def validate_sibling_model_observations(
    request: Mapping[str, Any], observed: Mapping[str, Mapping[str, Any]]
) -> None:
    """Authenticate the complete G2 receipt closure, including nested evidence."""

    request_outputs = {item["name"]: item for item in request["outputs"]}
    for name, request_output in request_outputs.items():
        grant = request_output["descriptor"].get("contained_model_sibling_write_grant")
        if grant is None:
            continue
        descriptor = observed.get(name)
        if not isinstance(descriptor, Mapping):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Sibling-bound model observation is absent",
                "receipt",
            )
        observation = descriptor.get("contained_model_sibling_tree")
        if not isinstance(observation, Mapping):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Sibling-bound model tree observation is absent",
                "receipt",
            )
        tree = validate_tree_manifest(observation.get("tree_manifest", {}))
        expected_siblings = []
        for sibling in grant["required_sibling_terminals"]:
            sibling_descriptor = observed.get(sibling["name"])
            if not isinstance(sibling_descriptor, Mapping):
                _fail(
                    "PYBENCH_RECEIPT_MISMATCH",
                    "Sibling-bound model terminal observation is absent",
                    "receipt",
                )
            expected_siblings.append(plain(sibling_descriptor["observed_descriptor_identity"]))
        expected = {
            "schema": SIBLING_OBSERVATION_SCHEMA,
            "write_grant_identity": plain(grant["write_grant_identity"]),
            "tree_manifest": tree,
            "tree_manifest_identity": plain(tree["manifest_identity"]),
            "payload_manifest_identity": tree_payload_identity(tree),
            "model_source_linkage_identity": plain(
                grant["model_source_linkage"]["linkage_identity"]
            ),
            "ordered_sibling_observed_descriptor_identities": expected_siblings,
            "model_sibling_closure_identity": grant["model_sibling_closure_identity"],
            "loadability_status": "contained_tree_authenticated",
        }
        expected["observation_identity"] = _identity(
            SIBLING_OBSERVATION_SCHEMA,
            expected,
            "contained_model_sibling_tree_observation excluding observation_identity",
        )
        expected_content = _identity(
            "scworkbench.model_ref.content.v1",
            {
                "engine": grant["engine"],
                "model_type": grant["model_type"],
                "linkage_identity": plain(grant["model_source_linkage"]["linkage_identity"]),
                "tree_manifest_identity": plain(tree["manifest_identity"]),
            },
        )
        if (
            dict(observation) != expected
            or descriptor.get("content_identity") != expected_content
            or descriptor.get("payload_manifest_identity") != tree_payload_identity(tree)
            or descriptor.get("model_linkage_identity")
            != plain(grant["model_source_linkage"]["linkage_identity"])
        ):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "Sibling-bound model receipt closure differs",
                "receipt",
            )
