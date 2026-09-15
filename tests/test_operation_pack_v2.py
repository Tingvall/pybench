from __future__ import annotations

import copy
import hashlib
import importlib.metadata as metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError, canonical_sha256
from pybench.operation_packs_v2 import (
    ENTRY_POINT_GROUP_V2,
    activate_operation_pack_v2,
    active_operation_packs_v2,
    discover_operation_pack_v2,
    manifest_identities_v2,
    parse_manifest_v2_bytes,
    unload_operation_pack_v2,
    validate_manifest_v2,
)
from pybench.protocol import validate_request
from pybench.protocol_v3 import (
    V3Readers,
    _audit_chunk_calls,
    error_receipt_v3,
    execute_operation_pack_v3,
    preflight_v3,
    validate_python_runtime_sidecar_v3,
    validate_receipt_v3,
    validate_request_v3,
)
from v3_helpers import deep_copy, fixture_inputs, install_reference_fixture, make_request

REPO = Path(__file__).parents[1]
FORGE = Path(
    "/home/dirk/Projects/.scworkbench-worktrees/core-to-packs-phase3a-statistics-forge-20260914"
)
SHARED = (
    "operation-pack-v2.schema.json",
    "operation-pack-transition-request-v2.schema.json",
    "operation-pack-transition-receipt-v2.schema.json",
)
STATISTICS_MANIFEST = Path(
    "/home/dirk/Projects/operation_packs/scwbStatistics/inst/scworkbench/operation-pack.json"
)
PYTHON_RUNTIME_SCHEMA_SHA256 = "59fbaf6456d010bf777323e113ea0e36f94c5b41e75657148ae8cd0aeb9b0b7d"
STATISTICS_DEFINITIONS = {
    "scwbstatistics::de_wilcox": "dca55697427cede0ef77a2d29399b5db6ddd163111c6b886d158ec356a5583d6",
    "scwbstatistics::markers_topn": "c2c82d51918368a6a1da8a7fda2c95720265a89e8161745e5e1c95b93e881d9b",
    "scwbstatistics::cell_vector_assoc": "8c29eda950bffd0d9bad3a5f0d12bd2562fad5e9c51d8bec6a740a5902a37978",
    "scwbstatistics::cell_vector_association": "3325465e1afcb766abb03810d35c166920db98bcf94a2f593b54e8d25b9f44df",
    "scwbstatistics::cell_vector_compare": "0d6e62442271c553ac803d89296040878c2c3bd143b1eb7b91a4a0711289f6a7",
    "scwbstatistics::feature_trend_spearman": "e6c6ebc993c3e119c407bf88f3b76ab43bbf280a27de9ef65d9136f0f43112d4",
}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    manifest = install_reference_fixture(tmp_path, monkeypatch)
    discovery = discover_operation_pack_v2(manifest)
    yield manifest, discovery
    unload_operation_pack_v2("scwbpyv2reference")


def _expect(code, phase, function, *args, **kwargs):
    with pytest.raises(OperationPackError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    assert caught.value.details.get("phase") == phase


def _pointer(value, pointer):
    current = value
    if not pointer:
        return current
    for part in pointer.strip("/").split("/"):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _replace_pointer(value, pointer, replacement=None, *, remove=False):
    parts = pointer.strip("/").split("/")
    current = value
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = int(parts[-1]) if isinstance(current, list) else parts[-1]
    if remove:
        current.pop(final)
    else:
        current[final] = replacement


def _stage_snapshot(stage):
    return {
        path.relative_to(stage).as_posix(): path.read_bytes()
        for path in stage.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _five_feature_inputs():
    inputs = fixture_inputs()
    inputs["matrix"].update(
        dimensions=[5, 2],
        cell_id=["c1", "c2"],
        feature_id=["g1", "g2", "g3", "g4", "g5"],
        values=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
    )
    return inputs


def _resign_sidecar(sidecar, *, observation=True):
    if observation and "chunk_observation" in sidecar:
        observed = sidecar["chunk_observation"]
        observed["observation_identity"] = {
            "scheme": "pybench.operation_pack.chunk_observation.v1",
            "scope": "chunk observation excluding observation_identity",
            "sha256": canonical_sha256(
                {key: value for key, value in observed.items() if key != "observation_identity"}
            ),
        }
    sidecar["runtime_identity"] = {
        "scheme": "pybench.operation_pack.python_runtime.v3",
        "scope": "python runtime sidecar excluding runtime_identity",
        "sha256": canonical_sha256(
            {key: value for key, value in sidecar.items() if key != "runtime_identity"}
        ),
    }
    return sidecar


def test_shared_protocol_bytes_and_frozen_canonical_identities_match_forge():
    ledger = json.loads((REPO / "protocol/fixtures/operation-pack-v2/identities.json").read_text())
    for name in SHARED:
        ours = (REPO / "protocol" / name).read_bytes()
        theirs = (FORGE / "inst/protocol" / name).read_bytes()
        assert ours == theirs
        assert hashlib.sha256(ours).hexdigest() == ledger["files"][name]
    for relative in (
        "operation-pack-canonical-json-v1.md",
        "fixtures/operation-pack-canonical-json-v1/cases.json",
        "fixtures/operation-pack-v2/identities.json",
    ):
        assert (REPO / "protocol" / relative).read_bytes() == (
            FORGE / "inst/protocol" / relative
        ).read_bytes()
    schema = json.loads((REPO / "protocol/operation-pack-v2.schema.json").read_text())
    for example, expected in zip(schema["examples"], ledger["manifest_examples"], strict=True):
        identities = manifest_identities_v2(example)
        assert identities["manifest_sha256"] == expected["manifest_sha256"]
        assert identities["operation_definition_sha256"] == expected["definitions"]
    for filename, key, expected in (
        (
            "operation-pack-transition-request-v2.schema.json",
            "request_identity",
            ledger["request_examples"],
        ),
        (
            "operation-pack-transition-receipt-v2.schema.json",
            "receipt_identity",
            ledger["receipt_examples"],
        ),
    ):
        schema = json.loads((REPO / "protocol" / filename).read_text())
        assert [
            canonical_sha256({name: child for name, child in example.items() if name != key})
            for example in schema["examples"]
        ] == expected


def test_python_runtime_sidecar_schema_has_amended_identity():
    payload = (REPO / "protocol/pybench-op-v3-python-runtime.schema.json").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == PYTHON_RUNTIME_SCHEMA_SHA256


def test_statistics_manifest_semantics_and_canonical_identities_match_forge():
    assert hashlib.sha256(STATISTICS_MANIFEST.read_bytes()).hexdigest() == (
        "14b4610595e255f199c58748e387981b294825a145a3ffffa2e0c5e309c175ad"
    )
    manifest = parse_manifest_v2_bytes(STATISTICS_MANIFEST.read_bytes())
    validate_manifest_v2(manifest, expected_backend=None)
    identities = manifest_identities_v2(manifest)
    assert identities["manifest_sha256"] == (
        "96f4fbe8f12d1842e7d57da142fb4942dd5b023cce02b7f1f5f9bbc90e26d0cb"
    )
    assert identities["operation_definition_sha256"] == STATISTICS_DEFINITIONS


def _reference_manifest():
    return json.loads(
        (REPO / "tests/fixtures/operation-pack-v2-python-reference/operation-pack.json").read_text()
    )


def _public_type_conflict(operation):
    operation["public_interface"] = {
        "intent": ["conformance"],
        "inputs": [],
        "parameters": [{"name": "features", "type": "string", "help": "Feature sequence."}],
        "outputs": [
            {"name": output["name"], "description": "Conformance output."}
            for output in operation["outputs"]
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda op: op["parameter_bindings"].append(
            {"parameter": "features", "policy": "cell_ids_or_set_v1", "input": "matrix"}
        ),
        lambda op: op["parameter_bindings"][0].update(input="space"),
        lambda op: op["parameter_bindings"][0].update(parameter="chunk_size"),
        lambda op: op["dimension_policies"][0].update(output="matrix_ordinary"),
        lambda op: op["chunking"].update(input="missing"),
        lambda op: op["chunking"].update(sequence_parameter="p_adjust_method"),
        lambda op: op["chunking"].update(size_parameter="features"),
        lambda op: op["chunking"].update(output="obs_table"),
        lambda op: op["chunking"].update(adjust_parameter="features"),
        _public_type_conflict,
        lambda op: op["retained_parameters"][0].update(output="missing"),
        lambda op: op["retained_parameters"][1].update(input="missing"),
        lambda op: op["retained_parameters"][2].update(parameter="missing"),
        lambda op: op["retained_parameters"].append(
            {"name": "feature_rows", "source": "output_meta", "output": "feature_table"}
        ),
    ],
)
def test_manifest_owned_declarations_reject_semantic_mismatch(mutation):
    manifest = _reference_manifest()
    mutation(manifest["operations"][0])
    _expect(
        "OPERATION_PACK_MANIFEST_SEMANTICS_INVALID",
        "semantic_admission",
        validate_manifest_v2,
        manifest,
        expected_backend=None,
    )


def test_manifest_owned_declarations_are_namespace_neutral():
    manifest = _reference_manifest()
    manifest["namespace"] = "anotherpack"
    for operation in manifest["operations"]:
        operation["qualified_name"] = f"anotherpack::{operation['name']}"
    validate_manifest_v2(manifest, expected_backend=None)


def test_manifest_outer_schema_exhaustive_tuple_and_required_field_contract():
    schema = json.loads((REPO / "protocol/operation-pack-v2.schema.json").read_text())
    validator = Draft202012Validator(schema)
    inputs = schema["$defs"]["input"]["oneOf"]
    outputs = schema["$defs"]["output"]["oneOf"]
    input_tuples = {
        (branch["properties"]["kind"]["const"], branch["properties"]["alignment"]["const"])
        for branch in inputs
    }
    input_kinds = {item[0] for item in input_tuples}
    alignments = {item[1] for item in input_tuples}
    invalid_inputs = 0
    input_validator = validator.evolve(schema=schema["$defs"]["input"])
    for kind in input_kinds:
        for alignment in alignments:
            candidate = {"name": "input", "kind": kind, "required": True, "alignment": alignment}
            valid = input_validator.is_valid(candidate)
            assert valid == ((kind, alignment) in input_tuples)
            invalid_inputs += not valid

    output_tuples = {
        (
            branch["properties"]["category"]["const"],
            branch["properties"]["kind"]["const"],
            branch["properties"]["storage_profile"]["const"],
        )
        for branch in outputs
    }
    categories = {item[0] for item in output_tuples}
    kinds = {item[1] for item in output_tuples}
    profiles = {item[2] for item in output_tuples}
    invalid_outputs = 0
    output_validator = validator.evolve(schema=schema["$defs"]["output"])
    for category in categories:
        for kind in kinds:
            for profile in profiles:
                candidate = {
                    "name": "output",
                    "category": category,
                    "kind": kind,
                    "storage_profile": profile,
                    "allocated_id_key": "output_id",
                    "required": True,
                    "promotion_group": "group",
                }
                if category == "materialization":
                    candidate["materialized_by_terminals"] = ["terminal"]
                if kind in {"cell_vector", "cell_vector_family"}:
                    candidate["value_kind"] = "continuous"
                if kind in {"feature_table", "obs_table"}:
                    candidate["table_type"] = "fixture"
                valid = output_validator.is_valid(candidate)
                assert valid == ((category, kind, profile) in output_tuples)
                invalid_outputs += not valid
    assert (invalid_inputs, invalid_outputs) == (66, 345)

    input_removals = 0
    for branch in inputs:
        candidate = {
            "name": "input",
            "kind": branch["properties"]["kind"]["const"],
            "required": True,
            "alignment": branch["properties"]["alignment"]["const"],
        }
        for field in branch["required"]:
            assert not input_validator.is_valid({k: v for k, v in candidate.items() if k != field})
            input_removals += 1
    output_removals = 0
    for branch in outputs:
        candidate = {
            "name": "output",
            "category": branch["properties"]["category"]["const"],
            "kind": branch["properties"]["kind"]["const"],
            "storage_profile": branch["properties"]["storage_profile"]["const"],
            "allocated_id_key": "output_id",
            "required": True,
            "promotion_group": "group",
        }
        if candidate["category"] == "materialization":
            candidate["materialized_by_terminals"] = ["terminal"]
        if candidate["kind"] in {"cell_vector", "cell_vector_family"}:
            candidate["value_kind"] = "continuous"
        if candidate["kind"] in {"feature_table", "obs_table"}:
            candidate["table_type"] = "fixture"
        for field in branch["required"]:
            assert not output_validator.is_valid(
                {key: child for key, child in candidate.items() if key != field}
            )
            output_removals += 1
    assert (input_removals, output_removals) == (44, 105)


def test_all_fixed_shared_schema_negative_fixtures_are_rejected():
    for filename in SHARED:
        schema = json.loads((REPO / "protocol" / filename).read_text())
        validator = Draft202012Validator(schema)
        for recipe in schema["x-negative-fixtures"]:
            if recipe["action"].startswith("exhaustive_"):
                continue
            value = deep_copy(schema["examples"][recipe["base_example"]])
            action = recipe["action"]
            if action == "remove":
                _replace_pointer(value, recipe["pointer"], remove=True)
            elif action in {"add", "replace"}:
                if action == "add":
                    parent, name = recipe["pointer"].rsplit("/", 1)
                    _pointer(value, parent)[name] = recipe["value"]
                else:
                    _replace_pointer(value, recipe["pointer"], recipe["value"])
            elif action == "duplicate":
                parent, _index = recipe["pointer"].rsplit("/", 1)
                target = _pointer(value, parent)
                target.append(deep_copy(_pointer(value, recipe["pointer"])))
            elif action == "replace_fields":
                target = _pointer(value, recipe["pointer"])
                target.update(recipe["fields"])
            elif action == "add_outputs_from_success":
                success = schema["examples"][0]
                value["outputs"] = deep_copy(success["outputs"])
            else:
                raise AssertionError(f"Unhandled frozen fixture action: {action}")
            assert not validator.is_valid(value), f"accepted {filename}:{recipe['id']}"


def test_canonical_identity_is_independent_process_deterministic():
    script = (
        "import json; from pathlib import Path; "
        "from pybench.operation_packs_v2 import manifest_identities_v2; "
        "x=json.loads(Path('protocol/operation-pack-v2.schema.json').read_text())['examples'][1]; "
        "print(json.dumps(manifest_identities_v2(x),sort_keys=True,separators=(',',':')))"
    )
    environment = {**__import__("os").environ, "PYTHONPATH": str(REPO / "src")}
    first = subprocess.check_output([sys.executable, "-c", script], cwd=REPO, env=environment)
    second = subprocess.check_output([sys.executable, "-c", script], cwd=REPO, env=environment)
    assert first == second


def test_strict_duplicate_key_and_parameter_schema_profile_rejection():
    _expect(
        "OPERATION_PACK_MANIFEST_PARSE_INVALID",
        "parse",
        parse_manifest_v2_bytes,
        b'{"schema_version":"x","schema_version":"y"}',
    )
    schema = json.loads((REPO / "protocol/operation-pack-v2.schema.json").read_text())
    manifest = deep_copy(schema["examples"][1])
    manifest["operations"][0]["params_schema"]["patternProperties"] = {}
    _expect(
        "OPERATION_PACK_PARAM_SCHEMA_PROFILE_UNSUPPORTED",
        "parameter_schema",
        validate_manifest_v2,
        manifest,
        expected_backend=None,
    )
    manifest = deep_copy(schema["examples"][1])
    manifest["operations"][0]["params_schema"]["properties"]["scale_factor"]["default"] = -1
    _expect(
        "OPERATION_PACK_PARAM_SCHEMA_INVALID",
        "parameter_schema",
        validate_manifest_v2,
        manifest,
        expected_backend=None,
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda m: m["operations"][0].update(qualified_name="wrong::normalize"),
            "OPERATION_PACK_MANIFEST_QUALIFIED_NAME_MISMATCH",
        ),
        (
            lambda m: m["operations"].append(copy.deepcopy(m["operations"][0])),
            "OPERATION_PACK_MANIFEST_OPERATION_DUPLICATE",
        ),
        (
            lambda m: m["operations"][0]["inputs"].append(
                copy.deepcopy(m["operations"][0]["inputs"][0])
            ),
            "OPERATION_PACK_MANIFEST_INPUT_DUPLICATE",
        ),
        (
            lambda m: m["operations"][0]["outputs"].append(
                copy.deepcopy(m["operations"][0]["outputs"][0])
            ),
            "OPERATION_PACK_MANIFEST_OUTPUT_DUPLICATE",
        ),
        (
            lambda m: m["operations"][0]["outputs"][1].update(allocated_id_key="matrix_id"),
            "OPERATION_PACK_MANIFEST_ALLOCATION_KEY_DUPLICATE",
        ),
        (
            lambda m: m["operations"][0]["dependencies"].extend(
                [
                    {
                        "requirement_id": "dup",
                        "kind": "device_runtime",
                        "name": "cuda",
                        "disposition": "required",
                        "probe": "core_device_availability_v1",
                    },
                    {
                        "requirement_id": "dup",
                        "kind": "device_runtime",
                        "name": "cpu",
                        "disposition": "required",
                        "probe": "core_device_availability_v1",
                    },
                ]
            ),
            "OPERATION_PACK_MANIFEST_DEFINITION_KEY_DUPLICATE",
        ),
        (
            lambda m: m["operations"][0]["promotion_closure"].update(summary=["summary"]),
            "OPERATION_PACK_MANIFEST_PROMOTION_CLOSURE_INVALID",
        ),
        (
            lambda m: m["operations"][0]["replay_equivalence"].update(
                normalized="loadable_reference"
            ),
            "OPERATION_PACK_MANIFEST_REPLAY_EQUIVALENCE_INVALID",
        ),
        (
            lambda m: (
                m.update(namespace="scworkbench"),
                [
                    operation.update(qualified_name=f"scworkbench::{operation['name']}")
                    for operation in m["operations"]
                ],
            ),
            "OPERATION_PACK_NAMESPACE_RESERVED",
        ),
    ],
)
def test_nine_ordered_semantic_admission_failures(mutation, code):
    schema = json.loads((REPO / "protocol/operation-pack-v2.schema.json").read_text())
    manifest = deep_copy(schema["examples"][1])
    mutation(manifest)
    _expect(code, "semantic_admission", validate_manifest_v2, manifest, expected_backend=None)


def test_frozen_semantic_negative_ledger_codes_and_ordering():
    schema = json.loads((REPO / "protocol/operation-pack-v2.schema.json").read_text())
    assert len(schema["x-semantic-negative-fixtures"]) == 9
    for recipe in schema["x-semantic-negative-fixtures"]:
        manifest = deep_copy(schema["examples"][recipe["base_example"]])
        if recipe["mutation"] == "replace":
            _replace_pointer(manifest, recipe["pointer"], recipe["value"])
        elif recipe["mutation"] == "append_modified_copy":
            parent, _index = recipe["pointer"].rsplit("/", 1)
            copied = deep_copy(_pointer(manifest, recipe["pointer"]))
            copied.update(recipe["changes"])
            _pointer(manifest, parent).append(copied)
        else:
            raise AssertionError(f"Unhandled semantic mutation: {recipe['mutation']}")
        _expect(
            recipe["expected_code"],
            recipe["expected_stage"],
            validate_manifest_v2,
            manifest,
            expected_backend=None,
        )


def test_discovery_is_inert_and_v2_entry_point_is_distinct(installed, monkeypatch):
    manifest, discovery = installed
    loaded = False

    def forbidden(_self):
        nonlocal loaded
        loaded = True
        raise AssertionError("discovery imported an entry point")

    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden)
    observed = discover_operation_pack_v2(manifest)
    assert not loaded
    assert observed.entry_point.group == ENTRY_POINT_GROUP_V2
    assert observed.entry_point.group != "scworkbench.operation_packs.v1"
    assert observed.public_record()["distribution"]["artifact_identity"]["sha256"]
    assert "/" not in observed.identity["manifest_relative_path"].lstrip("scwbpyv2reference/")
    assert discovery.identity == observed.identity


def test_atomic_multi_operation_activation_and_exact_preimage(installed):
    _manifest, discovery = installed
    before = active_operation_packs_v2()
    for boundary in ("catalog_before_pack", "catalog_pack_installed", "catalog_before_commit"):

        def fault(event, target=boundary):
            if event == target:
                raise RuntimeError(event)

        _expect(
            "OPERATION_PACK_CATALOG_PUBLICATION_FAILED",
            "catalog_publication",
            activate_operation_pack_v2,
            discovery,
            fault_hook=fault,
        )
        assert active_operation_packs_v2() == before
    registration = activate_operation_pack_v2(discovery)
    assert set(registration.handlers) == {
        "scwbpyv2reference::all_contracts",
        "scwbpyv2reference::chunk_probe",
        "scwbpyv2reference::dynamic_provider",
    }
    assert set(active_operation_packs_v2()) == {"scwbpyv2reference"}


@pytest.mark.parametrize("target", ["manifest", "distribution", "entry_point"])
def test_activation_reauthenticates_installed_sources_before_registration(installed, target):
    manifest, discovery = installed
    before = active_operation_packs_v2()
    if target == "manifest":
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif target == "distribution":
        pack = manifest.parent / "pack.py"
        pack.write_text(pack.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    else:
        entry_points = next(manifest.parents[1].glob("*.dist-info/entry_points.txt"))
        entry_points.write_text(
            "[scworkbench.operation_packs.v2]\nwrong = scwbpyv2reference.pack:register\n",
            encoding="utf-8",
        )
    with pytest.raises(OperationPackError):
        activate_operation_pack_v2(discovery)
    assert active_operation_packs_v2() == before


def test_discovery_rejects_noninstalled_manifest_and_duplicate_entry_point(installed, tmp_path):
    manifest, _discovery = installed
    copied = tmp_path / "copied.json"
    copied.write_bytes(manifest.read_bytes())
    _expect(
        "OPERATION_PACK_MANIFEST_SOURCE_REQUIRED",
        "authentication",
        discover_operation_pack_v2,
        copied,
    )
    entry_points = next(manifest.parents[1].glob("*.dist-info/entry_points.txt"))
    line = "reference = scwbpyv2reference.pack:register\n"
    entry_points.write_text(
        "[scworkbench.operation_packs.v2]\n" + line + line,
        encoding="utf-8",
    )
    _expect(
        "OPERATION_PACK_ENTRY_POINT_AMBIGUOUS",
        "authentication",
        discover_operation_pack_v2,
        manifest,
    )


def test_synthetic_pack_all_readers_writers_profiles_and_receipt(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::all_contracts",
        fixture_inputs(),
    )
    receipt, sidecar = execute_operation_pack_v3(request, discovery, stage)
    validate_receipt_v3(receipt, request)
    assert receipt["status"] == "ok"
    assert len(receipt["outputs"]) == 14
    assert len(receipt["registry_requests"]) == 1
    assert len(receipt["mutation_manifest"]) == 4
    assert receipt["mutation_manifest"]["entry_count"] == 15
    feature_grant = next(item for item in request["outputs"] if item["name"] == "feature_table")
    feature_output = next(item for item in receipt["outputs"] if item["name"] == "feature_table")
    assert feature_grant["descriptor"]["expected_dimensions"] == [0, 0]
    assert feature_output["descriptor"]["dimensions"] == [1, 1]
    assert {item["descriptor"]["storage_profile"] for item in request["outputs"]} == {
        "matrix_ordinary_v1",
        "matrix_backed_chunked_v1",
        "space_dense_v1",
        "space_file_backed_hdf5_v1",
        "space_2d_dense_v1",
        "model_ref_contained_tree_v1",
        "terminal_primitive_v1",
        "path_2d_family_v1",
        "space_path_family_v1",
        "cell_set_admission_v1",
    }
    runtime_schema = json.loads(
        (REPO / "protocol/pybench-op-v3-python-runtime.schema.json").read_text()
    )
    Draft202012Validator(runtime_schema).validate(sidecar)
    assert sidecar["runtime_identity"]["sha256"] == canonical_sha256(
        {key: value for key, value in sidecar.items() if key != "runtime_identity"}
    )
    assert not any(key in receipt for key in ("published", "publication", "promoted"))


def test_typed_matrix_reader_enforces_exact_chunk_schedule():
    matrix = {
        "kind": "matrix",
        "feature_id": ["g1", "g2", "g3", "g4", "g5"],
        "cell_id": ["c1", "c2"],
        "dimensions": [5, 2],
        "values": [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
    }
    operation = {
        "chunking": {
            "axis": "features",
            "input": "matrix",
            "sequence_parameter": "features",
            "size_parameter": "chunk_size",
        }
    }
    params = {"features": matrix["feature_id"], "chunk_size": 2}
    readers = V3Readers({"matrix": matrix})
    assert readers["matrix"](features=["g1", "g2"])["dimensions"] == [2, 2]
    readers["matrix"](features=["g3", "g4"])
    readers["matrix"](features=["g5"])
    audit_request = {
        "request_identity": {"sha256": "request"},
        "execution_capability": {"capability_identity": {"sha256": "capability"}},
        "operation_authentication": {"operation_definition_identity": {"sha256": "definition"}},
        "inputs": [
            {
                "name": "matrix",
                "descriptor": {"descriptor_identity": {"sha256": "descriptor"}},
            }
        ],
    }
    observation = _audit_chunk_calls(operation, params, readers, {"matrix": matrix}, audit_request)
    assert observation["observed_batch_sizes"] == [2, 2, 1]
    assert [call["features"] for call in readers.calls] == [
        ["g1", "g2"],
        ["g3", "g4"],
        ["g5"],
    ]
    _expect(
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
        "execution",
        V3Readers({"matrix": matrix})["matrix"],
        features=["not_granted"],
    )


@pytest.mark.parametrize(
    "chunk_size,expected",
    [(2, [["g1", "g2"], ["g3", "g4"], ["g5"]]), (5, [["g1", "g2", "g3", "g4", "g5"]])],
)
def test_chunk_observation_sidecar_records_actual_reader_calls(
    installed, tmp_path, chunk_size, expected
):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / f"chunk-{chunk_size}"
    stage.mkdir()
    inputs = _five_feature_inputs()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::chunk_probe",
        inputs,
        {"chunk_size": chunk_size},
    )
    receipt, sidecar = execute_operation_pack_v3(request, discovery, stage)
    observed = sidecar["chunk_observation"]
    assert observed["observed_batches"] == expected
    assert observed["observed_batch_sizes"] == [len(batch) for batch in expected]
    assert observed["observed_call_count"] == len(expected)
    assert observed["requested_chunk_size"] == chunk_size
    assert observed["request_identity"] == request["request_identity"]
    assert observed["capability_identity"] == request["execution_capability"]["capability_identity"]
    validate_python_runtime_sidecar_v3(sidecar, request, discovery, {"matrix": inputs["matrix"]})
    assert (
        canonical_sha256(
            {key: value for key, value in observed.items() if key != "observation_identity"}
        )
        == observed["observation_identity"]["sha256"]
    )
    assert (
        canonical_sha256(
            {key: value for key, value in sidecar.items() if key != "runtime_identity"}
        )
        == sidecar["runtime_identity"]["sha256"]
    )
    assert "chunk_observation" not in receipt
    validate_receipt_v3(receipt, request)
    payload = json.loads((stage / "outputs/feature_table/payload.json").read_text())
    assert payload["feature_id"] == inputs["matrix"]["feature_id"]


@pytest.mark.parametrize(
    "batches",
    [
        [["g3", "g4"], ["g1", "g2"], ["g5"]],
        [["g1", "g2"], ["g1", "g2"], ["g5"]],
        [["g1", "g2"], ["g3", "g4"]],
        [[], ["g1", "g2"], ["g3", "g4"], ["g5"]],
        [["g1", "g2", "g3"], ["g4", "g5"]],
        [["g1", "g2"], ["g3", "g4"], ["g5"], ["g5"]],
    ],
    ids=["reordered", "repeated", "missing", "empty", "oversized", "extra"],
)
def test_invalid_chunk_calls_fail_without_receipt_or_stage_residue(
    installed, tmp_path, monkeypatch, batches
):
    _manifest, discovery = installed
    registration = activate_operation_pack_v2(discovery)

    def invalid_chunk_probe(readers, writers, params, context):
        del params, context
        for batch in batches:
            readers["matrix"](features=batch)
        features = ["g1", "g2", "g3", "g4", "g5"]
        writers.write_terminal_primitive(
            "feature_table",
            {
                "kind": "feature_table",
                "dimensions": [5, 1],
                "feature_id": features,
                "rows": [{"feature_id": feature} for feature in features],
            },
        )

    monkeypatch.setitem(
        registration.handlers, "scwbpyv2reference::chunk_probe", invalid_chunk_probe
    )
    stage = tmp_path / "invalid-chunk"
    stage.mkdir()
    inputs = _five_feature_inputs()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::chunk_probe",
        inputs,
        {"chunk_size": 2},
    )
    before = _stage_snapshot(stage)
    _expect(
        "PYBENCH_CAPABILITY_MISMATCH",
        "execution",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert _stage_snapshot(stage) == before


@pytest.fixture
def chunk_execution(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "chunk-execution"
    stage.mkdir()
    inputs = _five_feature_inputs()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::chunk_probe",
        inputs,
        {"chunk_size": 2},
    )
    receipt, sidecar = execute_operation_pack_v3(request, discovery, stage)
    return discovery, request, inputs, receipt, sidecar


def _set_observation_value(sidecar, field, value):
    sidecar["chunk_observation"][field] = value


@pytest.mark.parametrize(
    "mutation,resign_observation",
    [
        (lambda s: s["distribution_identity"].update(sha256="0" * 64), True),
        (lambda s: s["chunk_observation"]["request_identity"].update(sha256="0" * 64), True),
        (
            lambda s: s["chunk_observation"]["capability_identity"].update(sha256="0" * 64),
            True,
        ),
        (
            lambda s: s["chunk_observation"]["operation_definition_identity"].update(
                sha256="0" * 64
            ),
            True,
        ),
        (
            lambda s: s["chunk_observation"]["input_descriptor_identity"].update(sha256="0" * 64),
            True,
        ),
        (lambda s: _set_observation_value(s, "input_name", "other"), True),
        (lambda s: _set_observation_value(s, "axis", "cells"), True),
        (lambda s: _set_observation_value(s, "sequence_parameter", "other"), True),
        (lambda s: _set_observation_value(s, "size_parameter", "other"), True),
        (lambda s: _set_observation_value(s, "requested_chunk_size", 3), True),
        (lambda s: _set_observation_value(s, "observed_call_count", 4), True),
        (
            lambda s: _set_observation_value(
                s, "observed_batches", [["g3", "g4"], ["g1", "g2"], ["g5"]]
            ),
            True,
        ),
        (lambda s: _set_observation_value(s, "observed_batch_sizes", [2, 1, 2]), True),
        (
            lambda s: s["chunk_observation"]["observation_identity"].update(sha256="0" * 64),
            False,
        ),
    ],
    ids=[
        "distribution",
        "request",
        "capability",
        "operation-definition",
        "input-descriptor",
        "input-name",
        "axis",
        "sequence-parameter",
        "size-parameter",
        "requested-size",
        "call-count",
        "batches",
        "batch-sizes",
        "observation-identity",
    ],
)
def test_chunk_observation_tampering_is_rejected(chunk_execution, mutation, resign_observation):
    discovery, request, inputs, _receipt, sidecar = chunk_execution
    tampered = deep_copy(sidecar)
    mutation(tampered)
    _resign_sidecar(tampered, observation=resign_observation)
    _expect(
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
        validate_python_runtime_sidecar_v3,
        tampered,
        request,
        discovery,
        {"matrix": inputs["matrix"]},
    )


def test_chunk_sidecar_cannot_cross_requests(chunk_execution, tmp_path):
    discovery, _request, inputs, _receipt, sidecar = chunk_execution
    other_stage = tmp_path / "other-request"
    other_stage.mkdir()
    other_request = make_request(
        discovery,
        other_stage,
        "scwbpyv2reference::chunk_probe",
        inputs,
        {"chunk_size": 2},
    )
    _expect(
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
        validate_python_runtime_sidecar_v3,
        sidecar,
        other_request,
        discovery,
        {"matrix": inputs["matrix"]},
    )


def test_chunk_sidecar_rejects_substituted_input_projection(chunk_execution):
    discovery, request, inputs, _receipt, sidecar = chunk_execution
    substituted = deep_copy(inputs["matrix"])
    substituted["values"][0][0] = -999
    _expect(
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
        validate_python_runtime_sidecar_v3,
        sidecar,
        request,
        discovery,
        {"matrix": substituted},
    )


def test_chunk_observation_presence_is_bound_to_manifest(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    chunk_stage = tmp_path / "chunk-present"
    chunk_stage.mkdir()
    chunk_inputs = _five_feature_inputs()
    chunk_request = make_request(
        discovery,
        chunk_stage,
        "scwbpyv2reference::chunk_probe",
        chunk_inputs,
        {"chunk_size": 2},
    )
    _receipt, chunk_sidecar = execute_operation_pack_v3(chunk_request, discovery, chunk_stage)
    missing = deep_copy(chunk_sidecar)
    missing.pop("chunk_observation")
    _resign_sidecar(missing)
    _expect(
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
        validate_python_runtime_sidecar_v3,
        missing,
        chunk_request,
        discovery,
        {"matrix": chunk_inputs["matrix"]},
    )

    plain_stage = tmp_path / "plain"
    plain_stage.mkdir()
    plain_inputs = fixture_inputs()
    plain_request = make_request(
        discovery,
        plain_stage,
        "scwbpyv2reference::dynamic_provider",
        plain_inputs,
    )
    _receipt, plain_sidecar = execute_operation_pack_v3(plain_request, discovery, plain_stage)
    assert "chunk_observation" not in plain_sidecar
    validate_python_runtime_sidecar_v3(
        plain_sidecar, plain_request, discovery, {"matrix": plain_inputs["matrix"]}
    )
    smuggled = deep_copy(plain_sidecar)
    smuggled["chunk_observation"] = deep_copy(chunk_sidecar["chunk_observation"])
    _resign_sidecar(smuggled)
    _expect(
        "PYBENCH_RUNTIME_ATTESTATION_MISMATCH",
        "runtime_attestation",
        validate_python_runtime_sidecar_v3,
        smuggled,
        plain_request,
        discovery,
        {"matrix": plain_inputs["matrix"]},
    )


def test_chunk_schedule_mismatch_rolls_back_before_receipt(installed, tmp_path, monkeypatch):
    _manifest, discovery = installed
    registration = activate_operation_pack_v2(discovery)
    original = registration.handlers["scwbpyv2reference::all_contracts"]

    def duplicate_read(readers, writers, params, context):
        original(readers, writers, params, context)
        readers["matrix"](features=params["features"])

    monkeypatch.setitem(registration.handlers, "scwbpyv2reference::all_contracts", duplicate_read)
    stage = tmp_path / "chunk-mismatch"
    stage.mkdir()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::all_contracts",
        fixture_inputs(),
    )
    before = _stage_snapshot(stage)
    _expect(
        "PYBENCH_CAPABILITY_MISMATCH",
        "execution",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert _stage_snapshot(stage) == before


def test_dynamic_dependency_resolution_and_changed_provider_fail_before_dispatch(
    installed, tmp_path
):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "dynamic"
    stage.mkdir()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::dynamic_provider",
        fixture_inputs(),
    )
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    dynamic = receipt["runtime_attestation"]["dynamic_resolutions"]
    assert len(dynamic) == 1 and dynamic[0]["provider_name"] == "packaging"

    changed_stage = tmp_path / "changed"
    changed_stage.mkdir()
    changed = make_request(
        discovery,
        changed_stage,
        "scwbpyv2reference::dynamic_provider",
        fixture_inputs(),
    )
    path = changed_stage / "inputs/matrix/payload.json"
    value = json.loads(path.read_text())
    value["meta"]["genome_provider"] = "jsonschema"
    from pybench.operation_packs import canonical_json

    path.write_text(canonical_json(value))
    before = {
        p.relative_to(changed_stage): p.read_bytes()
        for p in changed_stage.rglob("*")
        if p.is_file()
    }
    _expect(
        "PYBENCH_INPUT_IDENTITY_MISMATCH",
        "resolution",
        execute_operation_pack_v3,
        changed,
        discovery,
        changed_stage,
    )
    after = {
        p.relative_to(changed_stage): p.read_bytes()
        for p in changed_stage.rglob("*")
        if p.is_file()
    }
    assert before == after


@pytest.mark.parametrize(
    ("provider", "code", "phase"),
    [
        ("", "OPERATION_PACK_DYNAMIC_PROVIDER_INVALID", "preflight"),
        ("../packaging", "OPERATION_PACK_DYNAMIC_PROVIDER_INVALID", "preflight"),
        (
            "definitely-missing-python-distribution",
            "OPERATION_PACK_DEPENDENCY_MISSING",
            "preflight",
        ),
        ("pandas", "OPERATION_PACK_DYNAMIC_PROVIDER_CHANGED", "dispatch"),
    ],
)
def test_dynamic_provider_faults_precede_dispatch(installed, tmp_path, provider, code, phase):
    _manifest, discovery = installed
    stage = tmp_path / "dynamic-preflight"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    operation = discovery.manifest["operations"][1]
    payload = deep_copy(fixture_inputs()["matrix"])
    payload["meta"]["genome_provider"] = provider
    _expect(
        code,
        phase,
        preflight_v3,
        operation,
        {"matrix": payload},
        {},
        request,
    )


@pytest.mark.parametrize(
    "field",
    [
        "manifest_identity",
        "distribution_identity",
        "manifest_source_identity",
        "operation_definition_identity",
    ],
)
def test_wrong_operation_authentication_fails_before_dispatch(installed, tmp_path, field):
    _manifest, discovery = installed
    stage = tmp_path / field
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    before = _stage_snapshot(stage)
    request["operation_authentication"][field]["sha256"] = "0" * 64
    request["request_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in request.items() if key != "request_identity"}
    )
    _expect(
        "PYBENCH_CAPABILITY_MISMATCH",
        "staging",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert _stage_snapshot(stage) == before


def test_wrong_entry_point_authentication_fails_before_dispatch(installed, tmp_path):
    _manifest, discovery = installed
    stage = tmp_path / "entry"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    request["operation_authentication"]["entry_point"]["identity"]["sha256"] = "0" * 64
    request["request_identity"]["sha256"] = canonical_sha256(
        {key: value for key, value in request.items() if key != "request_identity"}
    )
    _expect(
        "PYBENCH_CAPABILITY_MISMATCH",
        "staging",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )


def test_capability_is_process_local_single_use(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "single-use"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    execute_operation_pack_v3(request, discovery, stage)
    before = _stage_snapshot(stage)
    _expect(
        "PYBENCH_CAPABILITY_MISMATCH",
        "staging",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert _stage_snapshot(stage) == before


@pytest.mark.parametrize("boundary", ["before_input_read", "before_dispatch", "after_handler"])
@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_fault_and_interruption_boundaries_restore_exact_stage(
    installed, tmp_path, boundary, exception
):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / f"{boundary}-{exception.__name__}"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    before = _stage_snapshot(stage)

    def fail(event):
        if event == boundary:
            raise exception(event)

    with pytest.raises(exception):
        execute_operation_pack_v3(request, discovery, stage, fault_hook=fail)
    assert _stage_snapshot(stage) == before


@pytest.mark.parametrize("mutation", ["payload", "empty_directory", "input_file"])
def test_independent_audit_rejects_post_writer_and_directory_mutations(
    installed, tmp_path, monkeypatch, mutation
):
    _manifest, discovery = installed
    registration = activate_operation_pack_v2(discovery)
    original = registration.handlers["scwbpyv2reference::dynamic_provider"]
    stage = tmp_path / mutation
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    before = _stage_snapshot(stage)

    def malicious(readers, writers, params, context):
        original(readers, writers, params, context)
        if mutation == "payload":
            (stage / "outputs/provider/payload.json").write_text("{}", encoding="utf-8")
        elif mutation == "empty_directory":
            (stage / "outputs/provider/undeclared").mkdir()
        else:
            (stage / "inputs/matrix/undeclared.txt").write_text("bad", encoding="utf-8")

    monkeypatch.setitem(registration.handlers, "scwbpyv2reference::dynamic_provider", malicious)
    _expect(
        "PYBENCH_OUT_OF_SCOPE_MUTATION",
        "audit",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert _stage_snapshot(stage) == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["execution_capability"].update(run_id="wrong"),
        lambda r: r["execution_capability"].update(checkpoint_generation="stale"),
        lambda r: r["execution_capability"].update(lease_id="changed"),
        lambda r: r["operation_authentication"].update(name="wrong::operation"),
        lambda r: r["outputs"].pop(),
    ],
)
def test_malformed_replayed_or_mismatched_capabilities_fail_without_residue(
    installed, tmp_path, mutation
):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "bad"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    mutation(request)
    before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    with pytest.raises(OperationPackError):
        execute_operation_pack_v3(request, discovery, stage)
    after = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    assert before == after


def test_handler_exception_partial_output_and_undeclared_mutation_roll_back(
    installed, tmp_path, monkeypatch
):
    _manifest, discovery = installed
    registration = activate_operation_pack_v2(discovery)
    original = registration.handlers["scwbpyv2reference::dynamic_provider"]
    stage = tmp_path / "rollback"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}

    def partial(readers, writers, params, context):
        original(readers, writers, params, context)
        (stage / "sibling.txt").write_text("undeclared")

    monkeypatch.setitem(registration.handlers, "scwbpyv2reference::dynamic_provider", partial)
    _expect(
        "PYBENCH_OUT_OF_SCOPE_MUTATION",
        "audit",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    after = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    assert before == after


def test_path_symlink_and_output_alignment_fail_with_zero_output_residue(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "escape"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (stage / "outputs").symlink_to(outside, target_is_directory=True)
    _expect(
        "PYBENCH_OUT_OF_SCOPE_MUTATION",
        "audit",
        execute_operation_pack_v3,
        request,
        discovery,
        stage,
    )
    assert not list(outside.iterdir())


def test_receipt_duplicate_extra_incomplete_and_identity_mismatch_are_rejected():
    request_schema = json.loads(
        (REPO / "protocol/operation-pack-transition-request-v2.schema.json").read_text()
    )
    receipt_schema = json.loads(
        (REPO / "protocol/operation-pack-transition-receipt-v2.schema.json").read_text()
    )
    request = request_schema["examples"][0]
    base = receipt_schema["examples"][0]
    validate_receipt_v3(base, request)
    for mutate in (
        lambda r: r["outputs"].pop(),
        lambda r: r["outputs"].append(copy.deepcopy(r["outputs"][0])),
        lambda r: r.update(extra=True),
        lambda r: r["request_identity"].update(sha256="0" * 64),
    ):
        value = deep_copy(base)
        mutate(value)
        value["receipt_identity"] = {
            "scheme": "scworkbench.operation_pack.transition_receipt.v2",
            "scope": "receipt excluding receipt_identity",
            "sha256": canonical_sha256(
                {key: child for key, child in value.items() if key != "receipt_identity"}
            ),
        }
        with pytest.raises(OperationPackError):
            validate_receipt_v3(value, request)


def test_v1_v2_v3_paths_reject_cross_protocol_confusion(installed):
    _manifest, discovery = installed
    request_schema = json.loads(
        (REPO / "protocol/operation-pack-transition-request-v2.schema.json").read_text()
    )
    v3 = request_schema["examples"][0]
    with pytest.raises(Exception):
        validate_request(v3)
    v2 = {
        "message_type": "request",
        "protocol_version": "pybench-op-v2",
    }
    _expect(
        "OPERATION_PACK_PROTOCOL_INCOMPATIBLE",
        "staging",
        validate_request_v3,
        v2,
        discovery,
    )


def test_forge_core_schema_validates_python_receipt_without_reinterpretation(installed, tmp_path):
    _manifest, discovery = installed
    activate_operation_pack_v2(discovery)
    stage = tmp_path / "forge"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    receipt, _ = execute_operation_pack_v3(request, discovery, stage)
    error = error_receipt_v3(
        request,
        OperationPackError("PYBENCH_PROCESS_FAILED", "synthetic", phase="execution"),
    )
    validate_receipt_v3(error, request)
    receipt_path = tmp_path / "receipts.json"
    receipt_path.write_text(json.dumps([receipt, error]), encoding="utf-8")
    expression = (
        'devtools::load_all(".", quiet=TRUE); '
        f'xs <- jsonlite::read_json("{receipt_path}", simplifyVector=FALSE); '
        'for (x in xs) getFromNamespace(".wb_operation_pack_v2_schema_assert", "scworkbench")'
        '(x, "receipt", "PYBENCH_RECEIPT_MISMATCH"); cat("FORGE_RECEIPTS_SCHEMA_PASS\\n")'
    )
    result = subprocess.run(
        ["Rscript", "--vanilla", "-e", expression],
        cwd=FORGE,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORGE_RECEIPTS_SCHEMA_PASS" in result.stdout


def _cli_environment(manifest):
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(manifest.parents[1]), str(REPO / "src"))),
    }


def _cli_command(manifest, request_path, stage, receipt, runtime):
    return [
        sys.executable,
        "-m",
        "pybench.operation_pack_v3_cli",
        "--manifest",
        str(manifest),
        "--request",
        str(request_path),
        "--stage-root",
        str(stage),
        "--receipt",
        str(receipt),
        "--python-runtime",
        str(runtime),
    ]


def test_v3_cli_installed_process_success_and_duplicate_key_rejection(installed, tmp_path):
    manifest, discovery = installed
    stage = tmp_path / "cli-success"
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    runtime = tmp_path / "runtime.json"
    result = subprocess.run(
        _cli_command(manifest, request_path, stage, receipt, runtime),
        env=_cli_environment(manifest),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    validate_receipt_v3(json.loads(receipt.read_text()), request)
    assert json.loads(runtime.read_text())["schema"] == "pybench.operation_pack.python_runtime.v3"

    malformed = tmp_path / "duplicate.json"
    malformed.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    bad_receipt = tmp_path / "bad-receipt.json"
    result = subprocess.run(
        _cli_command(manifest, malformed, stage, bad_receipt, tmp_path / "bad-runtime.json"),
        env=_cli_environment(manifest),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert not bad_receipt.exists()


def test_v3_cli_emits_validated_chunk_observation(installed, tmp_path):
    manifest, discovery = installed
    stage = tmp_path / "cli-chunk"
    stage.mkdir()
    inputs = _five_feature_inputs()
    request = make_request(
        discovery,
        stage,
        "scwbpyv2reference::chunk_probe",
        inputs,
        {"chunk_size": 2},
    )
    request_path = tmp_path / "chunk-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    receipt_path = tmp_path / "chunk-receipt.json"
    runtime_path = tmp_path / "chunk-runtime.json"
    result = subprocess.run(
        _cli_command(manifest, request_path, stage, receipt_path, runtime_path),
        env=_cli_environment(manifest),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(receipt_path.read_text())
    sidecar = json.loads(runtime_path.read_text())
    validate_receipt_v3(receipt, request)
    validate_python_runtime_sidecar_v3(sidecar, request, discovery, {"matrix": inputs["matrix"]})
    assert sidecar["chunk_observation"]["observed_batches"] == [
        ["g1", "g2"],
        ["g3", "g4"],
        ["g5"],
    ]


@pytest.mark.parametrize("mode", ["timeout", "exit"])
def test_v3_cli_process_timeout_and_hard_exit_leave_no_python_outputs(installed, tmp_path, mode):
    manifest, _initial = installed
    pack = manifest.parent / "pack.py"
    source = pack.read_text(encoding="utf-8")
    statement = "import time; time.sleep(30)" if mode == "timeout" else "import os; os._exit(17)"
    source = source.replace(
        "def _dynamic_provider(readers, writers, params, context):\n",
        f"def _dynamic_provider(readers, writers, params, context):\n    {statement}\n",
    )
    pack.write_text(source, encoding="utf-8")
    discovery = discover_operation_pack_v2(manifest)
    stage = tmp_path / mode
    stage.mkdir()
    request = make_request(
        discovery, stage, "scwbpyv2reference::dynamic_provider", fixture_inputs()
    )
    request_path = tmp_path / f"{mode}-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    command = _cli_command(
        manifest,
        request_path,
        stage,
        tmp_path / f"{mode}-receipt.json",
        tmp_path / f"{mode}-runtime.json",
    )
    if mode == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(command, env=_cli_environment(manifest), timeout=0.5)
    else:
        result = subprocess.run(command, env=_cli_environment(manifest), timeout=30)
        assert result.returncode == 17
    assert not (stage / "outputs").exists()
    assert not (stage / "registry_requests").exists()
