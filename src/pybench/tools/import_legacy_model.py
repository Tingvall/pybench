from __future__ import annotations

import argparse
import json

from pybench.bundle import close_bundle, open_bundle
from pybench.legacy_import import import_legacy_model


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly import one operator-selected legacy model into a bundle"
    )
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()

    bundle = open_bundle(args.bundle_dir)
    try:
        result = import_legacy_model(bundle, args.artifact_id, args.source)
    finally:
        close_bundle(bundle)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
