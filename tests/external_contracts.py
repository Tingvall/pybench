from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

GOLDEN_BUNDLE_ENV = "PYBENCH_GOLDEN_BUNDLE"
GOLDEN_BUNDLE_SHA256_ENV = "PYBENCH_GOLDEN_BUNDLE_SHA256"
SCWORKBENCH_PROTOCOL_ENV = "PYBENCH_SCWORKBENCH_PROTOCOL_DIR"
DIRECTORY_IDENTITY_SCHEME = "pybench-external-directory-sha256-v1"


def directory_identity(root: Path) -> str:
    root = root.resolve(strict=True)
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            pytest.fail(f"{DIRECTORY_IDENTITY_SCHEME}: symlink is not allowed: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    if not entries:
        pytest.fail(f"{DIRECTORY_IDENTITY_SCHEME}: directory has no regular files: {root}")
    framed = bytearray(f"{DIRECTORY_IDENTITY_SCHEME}\n".encode())
    for relative, digest in entries:
        relative_bytes = relative.encode("utf-8")
        framed.extend(f"{len(relative_bytes)}:".encode())
        framed.extend(relative_bytes)
        framed.extend(f":{digest}\n".encode())
    return hashlib.sha256(framed).hexdigest()


def configured_directory(variable: str, *, purpose: str) -> Path:
    configured = os.environ.get(variable)
    if not configured:
        pytest.skip(f"{purpose} skipped: set {variable} to its external fixture directory")
    path = Path(configured).expanduser()
    if not path.is_dir():
        pytest.fail(f"{purpose}: {variable} does not name an existing directory: {configured}")
    return path.resolve(strict=True)


def golden_bundle_directory() -> Path:
    path = configured_directory(GOLDEN_BUNDLE_ENV, purpose="golden-bundle artifact parity")
    expected = os.environ.get(GOLDEN_BUNDLE_SHA256_ENV, "")
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        pytest.fail(
            f"golden-bundle artifact parity: {GOLDEN_BUNDLE_SHA256_ENV} must be a lowercase "
            "SHA-256 when PYBENCH_GOLDEN_BUNDLE is configured"
        )
    observed = directory_identity(path)
    if observed != expected:
        pytest.fail(
            f"golden-bundle artifact parity: {GOLDEN_BUNDLE_SHA256_ENV} mismatch; "
            f"expected {expected}, observed {observed}"
        )
    for relative in ("manifest.json", "db/app.sqlite"):
        if not (path / relative).is_file():
            pytest.fail(f"golden-bundle artifact parity: fixture is missing {relative}")
    return path


def scworkbench_protocol_directory() -> Path:
    return configured_directory(
        SCWORKBENCH_PROTOCOL_ENV,
        purpose="scworkbench protocol byte-identity contract",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: PYTHONPATH=tests python -m external_contracts DIRECTORY")
    print(f"{DIRECTORY_IDENTITY_SCHEME}:{directory_identity(Path(sys.argv[1]))}")
