from __future__ import annotations

from pathlib import Path

import pytest

from external_contracts import (
    GOLDEN_BUNDLE_ENV,
    GOLDEN_BUNDLE_SHA256_ENV,
    SCWORKBENCH_PROTOCOL_ENV,
    configured_directory,
    directory_identity,
    golden_bundle_directory,
)


def test_directory_identity_is_order_stable_and_content_sensitive(tmp_path: Path):
    root = tmp_path / "fixture"
    (root / "nested").mkdir(parents=True)
    (root / "z.txt").write_text("z\n")
    (root / "nested/a.txt").write_text("a\n")
    first = directory_identity(root)
    assert len(first) == 64
    assert first == directory_identity(root)
    (root / "nested/a.txt").write_text("changed\n")
    assert directory_identity(root) != first


def test_missing_external_directory_skips_with_variable_name(monkeypatch):
    monkeypatch.delenv(SCWORKBENCH_PROTOCOL_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match=SCWORKBENCH_PROTOCOL_ENV):
        configured_directory(SCWORKBENCH_PROTOCOL_ENV, purpose="test contract")


def test_invalid_external_directory_fails_without_fallback(tmp_path: Path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setenv(SCWORKBENCH_PROTOCOL_ENV, str(missing))
    with pytest.raises(pytest.fail.Exception, match=SCWORKBENCH_PROTOCOL_ENV):
        configured_directory(SCWORKBENCH_PROTOCOL_ENV, purpose="test contract")


def test_golden_bundle_requires_matching_immutable_identity(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "db").mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}\n")
    (bundle / "db/app.sqlite").write_bytes(b"fixture")
    monkeypatch.setenv(GOLDEN_BUNDLE_ENV, str(bundle))
    monkeypatch.delenv(GOLDEN_BUNDLE_SHA256_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception, match=GOLDEN_BUNDLE_SHA256_ENV):
        golden_bundle_directory()
    monkeypatch.setenv(GOLDEN_BUNDLE_SHA256_ENV, "0" * 64)
    with pytest.raises(pytest.fail.Exception, match="mismatch"):
        golden_bundle_directory()
    expected = directory_identity(bundle)
    monkeypatch.setenv(GOLDEN_BUNDLE_SHA256_ENV, expected)
    assert golden_bundle_directory() == bundle.resolve()
