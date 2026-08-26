from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .artifacts_payload import (
    write_artifact_cell_vector_payload,
    write_artifact_table_payload,
)
from .bundle import BundleHandle, close_bundle
from .capability import build_write_manifest
from .protocol import (
    PROTOCOL_VERSION,
    ok_receipt,
    open_bundle_for_request,
    validate_receipt,
    validate_request,
)

ENTRY_POINT_GROUP = "scworkbench.operation_packs.v1"
MANIFEST_SCHEMA_VERSION = "scworkbench.operation_pack.v1"
RUNTIME_SCHEMA_VERSION = "scworkbench.operation_pack_runtime.python.v1"
INSTALLED_CONTENT_SCHEME = "python-installed-distribution-tree-sha256-v1"
DEPENDENCY_SET_SCHEME = "python-distribution-set-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_VALUE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?$")
_PRIMITIVES = {
    "matrix",
    "space",
    "space_2d",
    "cell_vector",
    "cell_vector_family",
    "relation_family",
    "feature_table",
    "obs_table",
    "model_ref",
    "path_2d_family",
    "space_path_family",
}
_RESERVED_NAMESPACES = {"scworkbench", "pybench", "core", "wb", "scwb"}


class OperationPackError(RuntimeError):
    """Stable operation-pack failure carrying a machine-readable code."""

    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {"status": "error", "code": self.code, "message": str(self), **self.details}


def _fail(code: str, message: str, **details: Any) -> None:
    raise OperationPackError(code, message, **details)


def protocol_asset(name: str) -> Path:
    path = Path(__file__).with_name("protocol_assets") / name
    if not path.is_file():
        path = Path(__file__).parents[2] / "protocol" / name
    if not path.is_file():
        _fail("OPERATION_PACK_PROTOCOL_ASSET_MISSING", f"packaged protocol asset is absent: {name}")
    return path


def canonical_json(value: Any) -> str:
    """Canonical JSON: recursive key order, stable arrays, compact UTF-8."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _fail("OPERATION_PACK_MANIFEST_INVALID", f"value is not canonical JSON: {exc}")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve(strict=True)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail("OPERATION_PACK_MANIFEST_NOT_FOUND", "manifest must be a regular file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("OPERATION_PACK_MANIFEST_INVALID", f"manifest is unreadable: {exc}")
    if not isinstance(manifest, dict):
        _fail("OPERATION_PACK_MANIFEST_INVALID", "manifest must be a JSON object")
    return manifest


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(protocol_asset("operation-pack-v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _validate_operation(operation: dict[str, Any], namespace: str) -> None:
    name = str(operation.get("name", ""))
    if not name.startswith(namespace + "::"):
        _fail("OPERATION_PACK_NAMESPACE_MISMATCH", f"operation {name!r} has wrong namespace")
    required = operation.get("requires", {})
    optional = operation.get("optional_requires", {})
    inputs = operation.get("inputs", {})
    produces = operation.get("produces", {})
    if not all(isinstance(value, dict) for value in (required, optional, inputs, produces)):
        _fail("OPERATION_PACK_DEFINITION_INVALID", f"operation {name!r} maps are malformed")
    if set(required) & set(optional) or set(inputs) != set(required) | set(optional):
        _fail("OPERATION_PACK_DEFINITION_INVALID", f"operation {name!r} input maps disagree")
    for input_name, kind in {**required, **optional}.items():
        if kind not in _PRIMITIVES:
            _fail("OPERATION_PACK_PRIMITIVE_UNSUPPORTED", f"unknown input primitive {kind!r}")
        item = inputs[input_name]
        if not isinstance(item, dict) or item.get("kind") != kind:
            _fail("OPERATION_PACK_DEFINITION_INVALID", f"input {input_name!r} kind disagrees")
        if item.get("required") is not (input_name in required):
            _fail(
                "OPERATION_PACK_DEFINITION_INVALID", f"input {input_name!r} required flag disagrees"
            )
    if not produces or any(kind not in _PRIMITIVES for kind in produces.values()):
        _fail("OPERATION_PACK_PRIMITIVE_UNSUPPORTED", f"operation {name!r} has unknown output")
    interface = operation.get(
        "interface_produces", {key: {"kind": val} for key, val in produces.items()}
    )
    replay = operation.get("replay_equivalence", {})
    if set(interface) != set(produces) or set(replay) != set(produces):
        _fail("OPERATION_PACK_OUTPUT_INVALID", f"operation {name!r} output maps disagree")
    if any(interface[key].get("kind") != value for key, value in produces.items()):
        _fail("OPERATION_PACK_OUTPUT_INVALID", f"operation {name!r} interface kinds disagree")
    promotable = operation.get("promotable", [])
    if len(promotable) != len(set(promotable)) or not set(promotable) <= set(produces):
        _fail(
            "OPERATION_PACK_PROMOTABLE_INVALID", f"operation {name!r} promotable outputs disagree"
        )
    params = operation.get("params", [])
    param_names = [item.get("name") for item in params if isinstance(item, dict)]
    if len(param_names) != len(params) or len(param_names) != len(set(param_names)):
        _fail("OPERATION_PACK_PARAMETER_INVALID", f"operation {name!r} parameters are malformed")
    if set(param_names) & set(inputs):
        _fail(
            "OPERATION_PACK_PARAMETER_INVALID", f"operation {name!r} parameters collide with inputs"
        )


def validate_manifest(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    manifest = (
        load_manifest(manifest_or_path)
        if isinstance(manifest_or_path, (str, os.PathLike))
        else json.loads(json.dumps(dict(manifest_or_path), allow_nan=False))
    )
    backend = manifest.get("backend")
    if backend not in {"R", "Python"}:
        _fail("OPERATION_PACK_BACKEND_UNSUPPORTED", f"unsupported backend: {backend!r}")
    if backend != "Python":
        _fail("OPERATION_PACK_BACKEND_MISMATCH", "PyBench executes only Python packs")
    errors = sorted(_schema_validator().iter_errors(manifest), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        if list(first.path) == ["backend"]:
            _fail("OPERATION_PACK_BACKEND_UNSUPPORTED", first.message)
        _fail("OPERATION_PACK_MANIFEST_INVALID", first.message, path=list(first.path))
    namespace = manifest["namespace"]
    if namespace in _RESERVED_NAMESPACES:
        _fail("OPERATION_PACK_NAMESPACE_RESERVED", f"namespace {namespace!r} is reserved")
    try:
        Version(manifest["package"]["version"])
    except InvalidVersion:
        _fail("OPERATION_PACK_MANIFEST_INVALID", "package.version is not valid PEP 440")
    names: list[str] = []
    for operation in manifest["operations"]:
        _validate_operation(operation, namespace)
        names.append(operation["name"])
    if len(names) != len(set(names)):
        _fail("OPERATION_PACK_OPERATION_DUPLICATE", "manifest duplicates operation names")
    return manifest


def operation_pack_identity(
    manifest_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_or_path)
    hashes = {
        operation["name"]: canonical_sha256(operation) for operation in manifest["operations"]
    }
    identity: dict[str, Any] = {
        "schema_version": manifest["schema_version"],
        "protocol_version": manifest["protocol_version"],
        "manifest_sha256": canonical_sha256(manifest),
        "namespace": manifest["namespace"],
        "package": manifest["package"],
        "backend": manifest["backend"],
        "compatibility": manifest["compatibility"],
        "entry_point": manifest["entry_point"],
        "operation_definition_sha256": hashes,
        "distribution": {
            "declared_name": manifest["package"]["name"],
            "normalized_name": canonicalize_name(manifest["package"]["name"]),
            "version": manifest["package"]["version"],
        },
        "entry_point_group": ENTRY_POINT_GROUP,
    }
    identity["pack_sha256"] = canonical_sha256(identity)
    return identity


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _distribution_content_identity(distribution: metadata.Distribution) -> dict[str, str]:
    entries: list[tuple[str, str]] = []
    for item in sorted(distribution.files or [], key=lambda value: str(value)):
        relative = str(item).replace("\\", "/")
        if relative.endswith((".pyc", ".pyo")) or "__pycache__" in relative.split("/"):
            continue
        path = Path(distribution.locate_file(item))
        if path.is_symlink() or not path.is_file():
            continue
        entries.append((relative, _hash_file(path)))
    if not entries:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH", "distribution has no auditable files"
        )
    framed = bytearray(b"python-installed-distribution-tree-sha256-v1\n")
    for relative, digest in entries:
        framed.extend(f"{len(relative.encode('utf-8'))}:{relative}{digest}\n".encode("utf-8"))
    return {
        "kind": "installed_distribution",
        "scheme": INSTALLED_CONTENT_SCHEME,
        "sha256": hashlib.sha256(framed).hexdigest(),
    }


def _distribution_build_metadata(distribution: metadata.Distribution) -> dict[str, Any]:
    files = {str(item).replace("\\", "/"): item for item in distribution.files or []}

    def text_suffix(suffix: str) -> str | None:
        matches = [item for name, item in files.items() if name.endswith(suffix)]
        if len(matches) != 1:
            return None
        path = Path(distribution.locate_file(matches[0]))
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None

    direct_url = text_suffix(".dist-info/direct_url.json")
    try:
        parsed_direct_url = json.loads(direct_url) if direct_url else None
    except (json.JSONDecodeError, TypeError):
        parsed_direct_url = None
    if not isinstance(parsed_direct_url, dict):
        parsed_direct_url = None
    revision = _distribution_bound_source_revision(distribution, parsed_direct_url)
    return {
        "installer": text_suffix(".dist-info/INSTALLER"),
        "wheel_metadata": text_suffix(".dist-info/WHEEL"),
        "direct_url": parsed_direct_url,
        "source_revision": revision,
    }


@dataclass(frozen=True)
class PackDiscovery:
    manifest: dict[str, Any]
    identity: dict[str, Any]
    distribution: metadata.Distribution
    entry_point: metadata.EntryPoint
    artifact_identity: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "pybench.operation_pack_discovery.v1",
            "backend": "Python",
            "manifest_identity": self.identity,
            "distribution": {
                "declared_name": self.manifest["package"]["name"],
                "normalized_name": canonicalize_name(self.manifest["package"]["name"]),
                "version": self.manifest["package"]["version"],
                "metadata_name": self.distribution.metadata["Name"],
                "metadata_normalized_name": canonicalize_name(self.distribution.metadata["Name"]),
                "metadata_version": self.distribution.version,
                "installed_location": str(Path(self.distribution.locate_file("")).resolve()),
                "artifact_identity": self.artifact_identity,
                "build_metadata": _distribution_build_metadata(self.distribution),
            },
            "entry_point": {
                "group": self.entry_point.group,
                "name": self.entry_point.name,
                "value": self.entry_point.value,
            },
        }


def discover_operation_pack(manifest_path: str | os.PathLike[str]) -> PackDiscovery:
    manifest = validate_manifest(manifest_path)
    identity = operation_pack_identity(manifest)
    declared = manifest["package"]
    try:
        distribution = metadata.distribution(declared["name"])
    except metadata.PackageNotFoundError:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_UNAVAILABLE",
            f"distribution {declared['name']!r} is absent",
        )
    metadata_name = distribution.metadata.get("Name", "")
    if canonicalize_name(metadata_name) != canonicalize_name(declared["name"]):
        _fail("OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH", "distribution metadata name differs")
    try:
        versions_match = Version(distribution.version) == Version(declared["version"])
    except InvalidVersion:
        versions_match = False
    if not versions_match:
        _fail("OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH", "distribution version differs")
    candidates = [
        entry
        for entry in distribution.entry_points
        if entry.group == ENTRY_POINT_GROUP and entry.name == manifest["entry_point"]
    ]
    if not candidates:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_UNAVAILABLE",
            "declared distribution has no matching entry point",
        )
    if len(candidates) != 1:
        _fail(
            "OPERATION_PACK_ENTRY_POINT_AMBIGUOUS",
            "declared distribution has duplicate entry points",
        )
    entry = candidates[0]
    if not _ENTRY_VALUE.fullmatch(entry.value):
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "installed entry-point target is malformed")
    return PackDiscovery(
        manifest, identity, distribution, entry, _distribution_content_identity(distribution)
    )


@dataclass(frozen=True)
class OperationPackHandlers:
    handlers: Mapping[str, Callable[["OperationPackContext"], Any]]


def operation_pack_handlers(
    handlers: Mapping[str, Callable[["OperationPackContext"], Any]],
) -> OperationPackHandlers:
    if not isinstance(handlers, Mapping):
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "handler bindings must be a mapping")
    return OperationPackHandlers(dict(handlers))


def activate_operation_pack(discovery: PackDiscovery) -> OperationPackHandlers:
    target = discovery.entry_point.load()
    if not callable(target):
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "entry point is not callable")
    try:
        registration = target()
    except Exception as exc:
        _fail("OPERATION_PACK_ENTRY_POINT_FAILED", f"entry point failed: {exc}")
    if not isinstance(registration, OperationPackHandlers):
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "entry point returned an invalid registration")
    expected = {item["name"] for item in discovery.manifest["operations"]}
    if set(registration.handlers) != expected:
        _fail("OPERATION_PACK_ENTRY_POINT_INVALID", "handler names differ from manifest operations")
    for name, handler in registration.handlers.items():
        if not callable(handler):
            _fail("OPERATION_PACK_ENTRY_POINT_INVALID", f"handler {name!r} is not callable")
        parameters = list(inspect.signature(handler).parameters.values())
        if len(parameters) != 1 or parameters[0].kind not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            _fail("OPERATION_PACK_ENTRY_POINT_INVALID", f"handler {name!r} must accept one context")
    return registration


def _dependency_attestation() -> dict[str, Any]:
    rows = sorted(
        (
            {
                "name": dist.metadata.get("Name", ""),
                "normalized_name": canonicalize_name(dist.metadata.get("Name", "")),
                "version": dist.version,
            }
            for dist in metadata.distributions()
            if dist.metadata.get("Name")
        ),
        key=lambda row: (row["normalized_name"], row["version"], row["name"]),
    )
    return {
        "scheme": DEPENDENCY_SET_SCHEME,
        "sha256": canonical_sha256(rows),
        "distributions": rows,
    }


def _distribution_bound_source_revision(
    distribution: metadata.Distribution,
    direct_url: Mapping[str, Any] | None,
    *,
    module_path: Path | None = None,
) -> str | None:
    """Return only source identity bound to this installed distribution."""
    if not isinstance(direct_url, Mapping):
        return None

    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, Mapping):
        vcs = vcs_info.get("vcs")
        commit_id = vcs_info.get("commit_id")
        if (
            isinstance(vcs, str)
            and vcs.strip()
            and isinstance(commit_id, str)
            and commit_id.strip()
        ):
            return commit_id.strip()
        return None

    dir_info = direct_url.get("dir_info")
    if not isinstance(dir_info, Mapping) or dir_info.get("editable") is not True:
        return None
    url = direct_url.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        origin = Path(unquote(parsed.path)).resolve(strict=True)
        loaded_module = (module_path or Path(__file__)).resolve(strict=True)
        loaded_module.relative_to(origin)
        project = tomllib.loads((origin / "pyproject.toml").read_text(encoding="utf-8"))
        project_name = project["project"]["name"]
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return None
    distribution_name = distribution.metadata.get("Name", "")
    if canonicalize_name(str(project_name)) != canonicalize_name(distribution_name):
        return None
    try:
        top_level = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=origin,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if Path(top_level).resolve(strict=True) != origin:
            return None
        revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=origin,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return revision or None


def _pybench_revision(distribution: metadata.Distribution) -> str | None:
    build_metadata = _distribution_build_metadata(distribution)
    revision = build_metadata.get("source_revision")
    return revision if isinstance(revision, str) and revision else None


def runtime_attestation(
    discovery: PackDiscovery, *, seed: Any = None, seed_declared: bool = False
) -> dict[str, Any]:
    try:
        pybench_dist = metadata.distribution("pybench")
        pybench_name = pybench_dist.metadata.get("Name", "pybench")
        pybench_version = pybench_dist.version
    except metadata.PackageNotFoundError:
        pybench_dist = None
        pybench_name, pybench_version = "pybench", "unknown"
    return {
        "schema": RUNTIME_SCHEMA_VERSION,
        "backend": "Python",
        "distribution": {
            "declared_name": discovery.manifest["package"]["name"],
            "normalized_name": canonicalize_name(discovery.manifest["package"]["name"]),
            "version": discovery.manifest["package"]["version"],
            "metadata_name": discovery.distribution.metadata["Name"],
            "metadata_normalized_name": canonicalize_name(discovery.distribution.metadata["Name"]),
            "metadata_version": discovery.distribution.version,
            "artifact_identity": discovery.artifact_identity,
        },
        "entry_point": {
            "group": discovery.entry_point.group,
            "name": discovery.entry_point.name,
            "value": discovery.entry_point.value,
        },
        "interpreter": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "abi": sysconfig.get_config_var("SOABI") or "unknown",
            "platform": sysconfig.get_platform(),
            "executable": str(Path(sys.executable).resolve()),
            "executable_sha256": _hash_file(Path(sys.executable).resolve()),
        },
        "dependencies": _dependency_attestation(),
        "pybench": {
            "protocol": PROTOCOL_VERSION,
            "distribution_name": pybench_name,
            "distribution_version": pybench_version,
            "source_revision": _pybench_revision(pybench_dist) if pybench_dist else None,
        },
        "device": {
            "type": "cpu",
            "descriptor": platform.processor() or platform.machine() or "CPU",
        },
        "seed": {
            "status": "declared" if seed_declared else "not_declared",
            "value": seed if seed_declared else None,
        },
    }


@dataclass(frozen=True)
class CellVector:
    cell_id: tuple[str, ...]
    value: tuple[float, ...]
    value_type: str = "continuous"


class OperationPackContext:
    """The only bundle-facing API supplied to a trusted pack handler."""

    def __init__(self, bundle: BundleHandle, request: dict[str, Any], operation: dict[str, Any]):
        self.__bundle = bundle
        self.__request = request
        self.__operation = operation
        self.__written: dict[str, dict[str, Any]] = {}

    @property
    def params(self) -> Mapping[str, Any]:
        return dict(self.__request["params"])

    def parameter(self, name: str) -> Any:
        if name not in self.__request["params"]:
            _fail("OPERATION_PACK_PARAMETER_INVALID", f"parameter {name!r} is absent")
        return self.__request["params"][name]

    def read_cell_vector(self, input_name: str) -> CellVector:
        if self.__operation["requires"].get(input_name) != "cell_vector":
            _fail(
                "OPERATION_PACK_INPUT_INVALID",
                f"input {input_name!r} is not a required cell_vector",
            )
        spec = self.__request["inputs"].get(input_name)
        if (
            not isinstance(spec, dict)
            or set(spec) != {"artifact_id", "type"}
            or spec["type"] != "cell_vector"
        ):
            _fail("OPERATION_PACK_INPUT_INVALID", f"input {input_name!r} binding is malformed")
        artifact_id = str(spec["artifact_id"])
        row = self.__bundle.con.execute(
            "SELECT type FROM artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None or row[0] != "cell_vector":
            _fail(
                "OPERATION_PACK_INPUT_INVALID",
                f"input artifact {artifact_id!r} is not a cell_vector",
            )
        rows = self.__bundle.con.execute(
            "SELECT a.cell_id,a.value_type,a.value_num FROM artifact_labels a "
            "JOIN cells c ON c.cell_id=a.cell_id WHERE a.artifact_id=? ORDER BY c.rowid",
            (artifact_id,),
        ).fetchall()
        if not rows or any(row[1] != "continuous" or row[2] is None for row in rows):
            _fail("OPERATION_PACK_INPUT_INVALID", "cell_vector must contain continuous values")
        values = tuple(float(row[2]) for row in rows)
        if any(not __import__("math").isfinite(value) for value in values):
            _fail("OPERATION_PACK_INPUT_INVALID", "cell_vector contains non-finite values")
        return CellVector(tuple(str(row[0]) for row in rows), values)

    def write_cell_vector(
        self, output_name: str, cell_id: Sequence[str], value: Sequence[float]
    ) -> dict[str, Any]:
        self.__assert_output(output_name, "cell_vector")
        spec = self.__request["outputs"][output_name]
        result = write_artifact_cell_vector_payload(
            self.__bundle, spec["artifact_id"], cell_id, value, "continuous"
        )
        self.__written[output_name] = result
        return result

    def write_obs_table(
        self,
        output_name: str,
        rows: Sequence[dict[str, Any]],
        *,
        table_type: str,
        entity_kind: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.__assert_output(output_name, "obs_table")
        spec = self.__request["outputs"][output_name]
        result = write_artifact_table_payload(
            self.__bundle,
            spec["artifact_id"],
            artifact_type="obs_table",
            table_rows=rows,
            table_type=table_type,
            entity_kind=entity_kind,
            meta=meta,
        )
        self.__written[output_name] = result
        return result

    def __assert_output(self, name: str, kind: str) -> None:
        if name in self.__written:
            _fail("OPERATION_PACK_OUTPUT_INVALID", f"output {name!r} was already written")
        if self.__operation["produces"].get(name) != kind:
            _fail("OPERATION_PACK_OUTPUT_INVALID", f"output {name!r} is not declared as {kind}")
        spec = self.__request["outputs"].get(name)
        if not isinstance(spec, dict) or spec.get("type") != kind:
            _fail("OPERATION_PACK_OUTPUT_INVALID", f"output {name!r} binding is malformed")

    def output_receipts(self) -> dict[str, dict[str, Any]]:
        if set(self.__written) != set(self.__operation["produces"]):
            _fail(
                "OPERATION_PACK_OUTPUT_INVALID",
                "handler did not write every declared output exactly once",
            )
        return dict(self.__written)


class _StageRollback:
    def __init__(self, bundle: BundleHandle):
        self.root = bundle.bundle_dir
        self.temp = Path(tempfile.mkdtemp(prefix="pybench-pack-rollback-"))
        self.database_rel = Path(bundle.manifest["storage"]["sqlite"]["path"])
        if bundle.con.in_transaction:
            bundle.con.commit()
        shutil.copy2(self.root / self.database_rel, self.temp / "database.sqlite")
        self.payload_roots = sorted(
            {
                str(item["path"]).split("/", 1)[0]
                for item in (bundle.capability or {}).get("write_allowlist", [])
                if item["mutation_class"] == "payload_tree"
            }
        )
        for relative in self.payload_roots:
            source = self.root / relative
            if source.exists() or source.is_symlink():
                shutil.copytree(
                    source, self.temp / relative, symlinks=True
                ) if source.is_dir() else shutil.copy2(
                    source, self.temp / relative, follow_symlinks=False
                )

    def restore(self) -> None:
        destination = self.root / self.database_rel
        shutil.copy2(self.temp / "database.sqlite", destination)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        for relative in self.payload_roots:
            destination_root = self.root / relative
            if destination_root.is_dir() and not destination_root.is_symlink():
                shutil.rmtree(destination_root)
            else:
                destination_root.unlink(missing_ok=True)
            backup = self.temp / relative
            if backup.exists() or backup.is_symlink():
                shutil.copytree(
                    backup, destination_root, symlinks=True
                ) if backup.is_dir() else shutil.copy2(
                    backup, destination_root, follow_symlinks=False
                )

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)


def _validate_execution_bindings(
    request: dict[str, Any], discovery: PackDiscovery
) -> dict[str, Any]:
    validate_request(request)
    operations = {item["name"]: item for item in discovery.manifest["operations"]}
    operation = operations.get(request["op_name"])
    if operation is None:
        _fail("OPERATION_PACK_OPERATION_UNAVAILABLE", "requested operation is absent from manifest")
    input_names = set(request["inputs"])
    required_inputs = set(operation["requires"])
    declared_inputs = required_inputs | set(operation.get("optional_requires", {}))
    if not required_inputs <= input_names or not input_names <= declared_inputs:
        _fail("OPERATION_PACK_INPUT_INVALID", "request inputs differ from manifest requirements")
    declared_params = {item["name"]: item for item in operation["params"]}
    if set(request["params"]) != set(declared_params):
        _fail("OPERATION_PACK_PARAMETER_INVALID", "request parameters differ from manifest")
    for name, value in request["params"].items():
        spec = declared_params[name]
        if spec["type"] in {"dbl", "number"}:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not __import__("math").isfinite(float(value))
            ):
                _fail(
                    "OPERATION_PACK_PARAMETER_INVALID", f"parameter {name!r} must be finite numeric"
                )
            if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
                _fail("OPERATION_PACK_PARAMETER_INVALID", f"parameter {name!r} is out of range")
    if {name: spec["type"] for name, spec in request["outputs"].items()} != operation["produces"]:
        _fail("OPERATION_PACK_OUTPUT_INVALID", "request outputs differ from manifest")
    return operation


def execute_operation_pack(
    request: dict[str, Any],
    manifest_path: str | os.PathLike[str],
    *,
    expected_pack_sha256: str,
    expected_definition_sha256: str,
    expected_distribution_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for label, value in (
        ("pack", expected_pack_sha256),
        ("definition", expected_definition_sha256),
        ("distribution", expected_distribution_sha256),
    ):
        if not _SHA256.fullmatch(value):
            _fail("OPERATION_PACK_IDENTITY_MISMATCH", f"expected {label} identity is malformed")
    discovery = discover_operation_pack(manifest_path)
    if discovery.identity["pack_sha256"] != expected_pack_sha256:
        _fail("OPERATION_PACK_IDENTITY_MISMATCH", "pack identity differs from expected identity")
    definition = discovery.identity["operation_definition_sha256"].get(request.get("op_name"))
    if definition != expected_definition_sha256:
        _fail(
            "OPERATION_PACK_DEFINITION_IDENTITY_MISMATCH", "operation definition identity differs"
        )
    if discovery.artifact_identity["sha256"] != expected_distribution_sha256:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "installed distribution contents changed",
        )
    operation = _validate_execution_bindings(request, discovery)
    registration = activate_operation_pack(discovery)
    if _distribution_content_identity(discovery.distribution) != discovery.artifact_identity:
        _fail(
            "OPERATION_PACK_DISTRIBUTION_IDENTITY_MISMATCH",
            "installed distribution contents changed during activation",
        )
    seed_parameter = (
        discovery.manifest["operations"][0].get("determinism", {}).get("seed_parameter")
    )
    attestation = runtime_attestation(
        discovery,
        seed=request["params"].get(seed_parameter) if seed_parameter else None,
        seed_declared=bool(seed_parameter),
    )
    bundle = open_bundle_for_request(request)
    rollback = _StageRollback(bundle)
    success = False
    try:
        context = OperationPackContext(bundle, request, operation)
        registration.handlers[request["op_name"]](context)
        receipt = ok_receipt(
            op_name=request["op_name"],
            run_id=request["run"]["run_id"],
            outputs=context.output_receipts(),
            request=request,
            bundle=bundle,
        )
        validate_receipt(receipt, request)
        if receipt["write_manifest"] != build_write_manifest(bundle):
            _fail(
                "PYBENCH_RECEIPT_MISMATCH",
                "receipt mutation descriptors differ from independent stage audit",
            )
        success = True
        return receipt, attestation
    finally:
        close_bundle(bundle)
        if not success:
            rollback.restore()
        rollback.close()
