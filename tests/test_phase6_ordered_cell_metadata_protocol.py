"""Cross-language Phase 6 metadata identities; no Python scVI runtime claim."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pybench.operation_packs import OperationPackError
from pybench.operation_packs_v2 import _identity, protocol_asset
from pybench.protocol_v4 import _validate_typed_slice_grants, execute_operation_pack_v4

FIXTURE = "fixtures/operation-pack-v3/phase6-ordered-cell-metadata-identities.json"
MANIFEST_SCHEMA = "operation-pack-v3.schema.json"
REQUEST_SCHEMA = "operation-pack-transition-request-v3.schema.json"
RECEIPT_SCHEMA = "operation-pack-transition-receipt-v3.schema.json"


def _load(name: str) -> dict:
    return json.loads(protocol_asset(name).read_text(encoding="utf-8"))


def _definition(schema: dict, name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]})


def test_ordered_cell_metadata_protocol_schemas_and_declaration_are_closed():
    manifest = _load(MANIFEST_SCHEMA)
    request = _load(REQUEST_SCHEMA)
    receipt = _load(RECEIPT_SCHEMA)
    for schema in (manifest, request, receipt):
        Draft202012Validator.check_schema(schema)

    declaration = {
        "name": "cell_metadata",
        "category": "authenticated_core_registry_snapshot",
        "kind": "ordered_cell_metadata",
        "reader": "read_ordered_cell_metadata",
        "matrix_input": "matrix",
        "field_parameters": [
            {"parameter": "batch_key", "role": "categorical", "cardinality": "zero_or_one"},
            {
                "parameter": "continuous_covariates",
                "role": "continuous",
                "cardinality": "zero_or_more",
            },
        ],
        "row_order": "matrix_effective_cell_order_v1",
        "missing_value_policy": "preserve_null_v1",
        "call_discipline": "single_use",
    }
    validator = _definition(manifest, "registry_read")
    assert not list(validator.iter_errors(declaration))
    for mutation in (
        {"kind": "untyped_cell_metadata"},
        {"host_path": "/private/cells.tsv"},
        {"missing_value_policy": "drop_nulls"},
    ):
        changed = {**declaration, **mutation}
        assert list(validator.iter_errors(changed))

    assert (
        manifest["$defs"]["typed_slice_read"]["properties"]["value_policy"]["const"]
        == "nonnegative_integer_v1"
    )
    assert (
        request["$defs"]["typed_slice_grant"]["properties"]["value_policy"]["const"]
        == "nonnegative_integer_v1"
    )


def test_ordered_cell_metadata_fixture_identities_match_python_canonical_json():
    fixture = _load(FIXTURE)
    assert set(fixture) == {"snapshot", "content_identity", "source_identity"}
    snapshot = fixture["snapshot"]
    assert snapshot["cell_id"] == ["cell_2", "cell_10"]
    assert [field["value_type"] for field in snapshot["fields"]] == [
        "categorical",
        "continuous",
    ]
    assert snapshot["fields"][0]["values"] == ["b", None]
    assert snapshot["fields"][1]["values"] == [1.5, 2.25]
    assert snapshot["cell_order_identity"] == _identity(
        "scworkbench.cell_order.v1", snapshot["cell_id"]
    )
    assert fixture["content_identity"] == _identity(
        "scworkbench.ordered_cell_metadata.snapshot.v1",
        snapshot,
        "canonical ordered cell metadata",
    )
    assert fixture["source_identity"] == _identity(
        "scworkbench.ordered_cell_metadata.source.v1",
        {
            "matrix_id": snapshot["matrix_id"],
            "cell_order_identity": snapshot["cell_order_identity"],
            "fields": snapshot["fields"],
        },
        "ordered cell metadata source",
    )
    for field, replacement in (
        ("cell_id", ["cell_10", "cell_2"]),
        ("fields", list(reversed(snapshot["fields"]))),
    ):
        changed = copy.deepcopy(snapshot)
        changed[field] = replacement
        assert (
            _identity(
                "scworkbench.ordered_cell_metadata.snapshot.v1",
                changed,
                "canonical ordered cell metadata",
            )
            != fixture["content_identity"]
        )


@pytest.mark.parametrize(
    ("grant_key", "grant", "message"),
    [
        (
            "registry_reads",
            {"name": "cell_metadata", "descriptor": {"kind": "ordered_cell_metadata"}},
            "ordered cell-metadata authority",
        ),
        (
            "typed_slice_grants",
            {"name": "counts", "value_policy": "nonnegative_integer_v1"},
            "nonnegative-integer typed-slice authority",
        ),
    ],
)
def test_unsupported_phase6_grant_rejects_before_staging(
    tmp_path: Path, grant_key: str, grant: dict, message: str
):
    stage = tmp_path / "never-created"
    request = {
        "schema": "scworkbench.operation_pack.transition_request.v3",
        "pack_abi_version": "3",
        "process_protocol_version": "pybench-op-v4",
        grant_key: [grant],
    }
    with pytest.raises(OperationPackError, match=message) as caught:
        execute_operation_pack_v4(request, object(), stage)  # type: ignore[arg-type]
    assert caught.value.code == "OPERATION_PACK_PROTOCOL_INCOMPATIBLE"
    assert caught.value.details["phase"] == "staging"
    assert not stage.exists()


def test_unsupported_phase6_manifest_slice_policy_fails_even_if_grant_omits_it():
    operation = {
        "typed_slice_reads": [{"name": "counts", "value_policy": "nonnegative_integer_v1"}]
    }
    with pytest.raises(OperationPackError, match="nonnegative-integer typed-slice authority"):
        _validate_typed_slice_grants({"typed_slice_grants": []}, operation)
