from __future__ import annotations

import json
import sys
from pathlib import Path

from .operation_packs import canonical_json
from .operation_packs_v3 import OperationPackHandlersV3, discover_operation_pack_v3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    manifest, callable_name = args
    discovery = discover_operation_pack_v3(Path(manifest))
    registration = discovery.entry_point.load()()
    if not isinstance(registration, OperationPackHandlersV3):
        return 3
    finalizer = registration.finalizers.get(callable_name)
    if finalizer is None:
        return 4
    value = json.loads(sys.stdin.read())
    result = finalizer(value["merged_family"], value["metadata"])
    if not isinstance(result, dict):
        return 5
    sys.stdout.write(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
