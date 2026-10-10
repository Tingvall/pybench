from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.graph import (
    axis_identities,
    canonical_bytes,
    content_identity,
    graph_binding_from_source,
    graph_identities,
    graph_source_axis_projection,
    validate_bindings,
    validate_content,
)
from pybench.operation_packs import OperationPackError, canonical_json

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "graph-v1"
CONTENT = json.loads((FIXTURES / "content-identities.json").read_text())
BINDINGS = json.loads((FIXTURES / "binding-descriptor-identities.json").read_text())
SCHEMA = json.loads((FIXTURES.parents[1] / "graph-v1.schema.json").read_text())


def _definition(name: str):
    return Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{name}"})


def _location(value, pointer: str):
    tokens = [item.replace("~1", "/").replace("~0", "~") for item in pointer.split("/")[1:]]
    current = value
    for token in tokens[:-1]:
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current, tokens[-1]


def _mutate(value, mutation):
    parent, key = _location(value, mutation["path"])
    if mutation["op"] == "copy":
        source, source_key = _location(value, mutation["from"])
        replacement = copy.deepcopy(
            source[int(source_key)] if isinstance(source, list) else source[source_key]
        )
    else:
        replacement = mutation.get("value")
    if mutation["op"] == "remove":
        if isinstance(parent, list):
            parent.pop(int(key))
        else:
            del parent[key]
    elif mutation["op"] in {"add", "copy"}:
        if isinstance(parent, list):
            parent.append(replacement) if key == "-" else parent.insert(int(key), replacement)
        else:
            parent[key] = replacement
    else:
        if isinstance(parent, list):
            parent[int(key)] = replacement
        else:
            parent[key] = replacement


def test_long_streamed_string_matches_canonical_json_across_escape_boundaries():
    value = {"payload": "a" * 8191 + '"\\\n\t' + "é" * 9000 + "\u0001"}
    assert b"".join(canonical_bytes(value)) == canonical_json(value).encode("utf-8")


def test_public_graph_axis_projection_is_path_free_and_not_mutable_authority():
    identity_value = {"scheme": "example", "sha256": "a" * 64}
    authority = {
        "input_name": "labels",
        "source_binding_id": None,
        "axis_kind": "cell",
        "source_ref": {"kind": "retained_cell_axis", "source_id": "axis_1"},
        "source_snapshot_identity": identity_value,
        "axis_content_identity": identity_value,
        "axis_order_identity": identity_value,
        "axis_count": "1",
        "source_payload_closure_identity": identity_value,
        "authority_identity": identity_value,
        "host_path": "/not-exposed",
    }
    source = graph_source_axis_projection(authority)
    assert "host_path" not in source
    assert "authority_identity" not in source
    binding = graph_binding_from_source(
        source,
        "b0",
        [{"node_id": "n0", "axis_id": "cell_1"}],
    )
    source["source_ref"]["source_id"] = "changed"
    assert binding["source_ref"]["source_id"] == "axis_1"
    source["axis_count"] = "00"
    with pytest.raises(OperationPackError, match="nonnegative int64 decimal"):
        graph_binding_from_source(source, "b1", [])


def test_canonical_numeric_ordinal_round_trip_and_invalid_numeric_rejection():
    source = next(
        row["content"]
        for row in CONTENT["positive"]
        if row["id"] == "directed_self_loop_mixed_mode"
    )
    parsed = json.loads(canonical_json(source))
    assert type(parsed["incidences"][1]["ordinal"]) is float
    assert content_identity(parsed) == content_identity(source)
    for invalid in (True, -1, 0.5, float("inf"), 1e20):
        bad = copy.deepcopy(parsed)
        bad["incidences"][1]["ordinal"] = invalid
        with pytest.raises(OperationPackError):
            validate_content(bad)


@pytest.mark.parametrize("case", CONTENT["positive"], ids=lambda case: case["id"])
def test_frozen_content_identity(case):
    Draft202012Validator(SCHEMA).validate(case["content"])
    assert content_identity(case["content"])["sha256"] == case["expected_sha256"]
    canonical = canonical_json(case["content"]).encode("utf-8")
    for width in (1, 2, 7, 64):
        streamed = b"".join(canonical_bytes(case["content"]))
        assert streamed == canonical
        digest = hashlib.sha256()
        for start in range(0, len(streamed), width):
            digest.update(streamed[start : start + width])
        assert digest.hexdigest() == case["expected_sha256"]


@pytest.mark.parametrize(
    "case",
    [item for item in CONTENT["negative"] if "path" in item["mutation"]],
    ids=lambda case: case["id"],
)
def test_self_rehashed_malformed_content_rejects(case):
    original = next(item["content"] for item in CONTENT["positive"] if item["id"] == case["base"])
    altered = copy.deepcopy(original)
    _mutate(altered, case["mutation"])
    with (FIXTURES / "negative-rehash-identities.csv").open(newline="") as handle:
        expected = {
            row["mutation_id"]: row["expected_rehashed_sha256"] for row in csv.DictReader(handle)
        }
    assert (
        hashlib.sha256(canonical_json(altered).encode("utf-8")).hexdigest() == expected[case["id"]]
    )
    with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
        validate_content(altered)


@pytest.mark.parametrize("case", BINDINGS["positive"], ids=lambda case: case["id"])
def test_frozen_binding_and_descriptor_identity(case):
    options = case["native_constructor_options"]
    ids = (
        graph_identities(case["content"], options["associations"])
        if "associations" in options
        else graph_identities(case["content"])
    )
    expected = case["claimed_projections"]
    _definition("binding_preimage").validate(expected["binding_preimage"])
    _definition("descriptor_preimage").validate(expected["descriptor_preimage"])
    assert ids.content == expected["content_identity"]
    assert ids.bindings == expected["binding_identity"]
    assert ids.descriptor == expected["descriptor_identity"]
    assert ids.descriptor == case["independent_output_seal"]
    for binding in options.get("associations", []):
        matches = [
            source
            for source in case["source_authority_records"]
            if source["source_ref"] == binding["source_ref"]
            and source["axis_kind"] == binding["axis_kind"]
            and source["lookup_snapshot_identity"] == binding["source_snapshot_identity"]
        ]
        assert len(matches) == 1
        source = matches[0]
        assert axis_identities(
            source["axis_kind"],
            source["source_ref"],
            source["snapshot_head"],
            source["axis_ids"],
        ) == {
            key: binding[key]
            for key in ("source_snapshot_identity", "axis_content_identity", "axis_order_identity")
        }


def test_native_omission_and_explicit_null_differ():
    empty = CONTENT["positive"][0]["content"]
    assert graph_identities(empty).bindings == graph_identities(empty, []).bindings
    with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
        graph_identities(empty, None)


def test_binding_structure_rejects_foreign_node_and_duplicate_axis():
    case = next(item for item in BINDINGS["positive"] if item["id"] == "declared_mapping_order_a")
    content = case["content"]
    bindings = copy.deepcopy(case["native_constructor_options"]["associations"])
    bindings[0]["mapping"][0]["node_id"] = "foreign"
    with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
        validate_bindings(content, bindings)
    bindings = copy.deepcopy(case["native_constructor_options"]["associations"])
    bindings[0]["mapping"][1]["axis_id"] = bindings[0]["mapping"][0]["axis_id"]
    with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
        validate_bindings(content, bindings)


def test_reordered_mapping_rejects_instead_of_sorting():
    case = next(item for item in BINDINGS["positive"] if item["id"] == "declared_mapping_order_a")
    bindings = copy.deepcopy(case["native_constructor_options"]["associations"])
    bindings[0]["mapping"].reverse()
    with pytest.raises(OperationPackError, match="PYBENCH_GRAPH_INVALID"):
        validate_bindings(case["content"], bindings)


def test_graph_schema_is_closed_and_generated():
    Draft202012Validator.check_schema(SCHEMA)
    invalid = copy.deepcopy(CONTENT["positive"][0]["content"])
    invalid["artifact_id"] = "must_not_enter_content"
    assert not Draft202012Validator(SCHEMA).is_valid(invalid)
    invalid = copy.deepcopy(BINDINGS["positive"][3]["claimed_projections"]["binding_preimage"])
    invalid["bindings"][0]["source_ref"]["host_path"] = "/tmp/forbidden"
    assert not _definition("binding_preimage").is_valid(invalid)
