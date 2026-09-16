from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .operation_packs import OperationPackError, canonical_sha256

_NAME = re.compile(r"^set:[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SOURCE = {"label", "created_by", "input_artifact_id", "input_run_id", "input_snapshot_id"}


def _invalid(message: str) -> None:
    raise OperationPackError("PYBENCH_OUTPUT_INVALID", message, phase="audit")


def _identity(scheme: str, scope: str, value: Any) -> dict[str, str]:
    return {"scheme": scheme, "scope": scope, "sha256": canonical_sha256(value)}


def normalize_cell_set_family(
    value: Mapping[str, Any], cell_order: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "sets", "mode", "source"}:
        _invalid("Cell-set-family admission payload has an invalid outer shape")
    if value["kind"] != "cell_set_family_admission":
        _invalid("Cell-set-family kind is invalid")
    if value["mode"] not in ("add", "replace"):
        _invalid("Cell-set-family mode must be add or replace")
    source = value["source"]
    if not isinstance(source, Mapping) or set(source) != _SOURCE:
        _invalid("Cell-set-family source must contain exactly the declared fields")
    if any(item is not None and not isinstance(item, str) for item in source.values()):
        _invalid("Cell-set-family source fields must be strings or null")
    if not source["label"]:
        _invalid("Cell-set-family source label must be nonempty")
    if bool(source["input_run_id"]) != bool(source["input_snapshot_id"]):
        _invalid("Cell-set-family run and snapshot provenance must occur together")
    sets = value["sets"]
    if not isinstance(sets, list):
        _invalid("Cell-set-family sets must be a JSON array")
    if (
        not isinstance(cell_order, (list, tuple))
        or any(not isinstance(cell, str) or not cell for cell in cell_order)
        or len(cell_order) != len(set(cell_order))
    ):
        _invalid("Authenticated bundle cell order is invalid")
    universe = set(cell_order)
    normalized_sets = []
    for item in sets:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "cells",
            "description",
            "selection_rule",
        }:
            _invalid("Cell-set-family member has an invalid shape")
        name = item["name"]
        if not isinstance(name, str):
            _invalid("Cell-set-family name must be a string")
        name = name if name.startswith("set:") else f"set:{name}"
        if not _NAME.fullmatch(name):
            _invalid("Cell-set-family name is invalid")
        cells = item["cells"]
        if (
            not isinstance(cells, list)
            or not cells
            or any(not isinstance(cell, str) or not cell for cell in cells)
            or len(cells) != len(set(cells))
            or not set(cells) <= universe
        ):
            _invalid("Cell-set-family members must be unique nonempty bundle cells")
        if (
            not isinstance(item["description"], str)
            or not isinstance(item["selection_rule"], str)
            or not item["selection_rule"]
        ):
            _invalid("Cell-set-family description or selection rule is invalid")
        selected = set(cells)
        normalized_sets.append(
            {
                "name": name,
                "cells": [cell for cell in cell_order if cell in selected],
                "description": item["description"],
                "selection_rule": item["selection_rule"],
            }
        )
    names = [item["name"] for item in normalized_sets]
    if len(names) != len(set(names)):
        _invalid("Cell-set-family repeats a normalized set name")
    normalized_sets.sort(key=lambda item: item["name"].encode("utf-8"))
    payload = {
        "kind": "cell_set_family_admission",
        "sets": normalized_sets,
        "mode": value["mode"],
        "source": {
            key: source[key]
            for key in (
                "label",
                "created_by",
                "input_artifact_id",
                "input_run_id",
                "input_snapshot_id",
            )
        },
    }
    evidence = {
        "family_identity": _identity(
            "scworkbench.cell_set.family_admission.v1", "normalized family payload", payload
        ),
        "member_identities": [
            {
                "name": item["name"],
                "membership_identity": _identity(
                    "scworkbench.cell_set.membership.v1", "ordered exact membership", item["cells"]
                ),
            }
            for item in normalized_sets
        ],
        "source_identity": _identity(
            "scworkbench.cell_set.family_source.v1", "normalized source metadata", payload["source"]
        ),
    }
    return payload, evidence
