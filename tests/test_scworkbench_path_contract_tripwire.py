from __future__ import annotations

import re

from external_contracts import scworkbench_source_directory
from pybench.protocol import PATH_CONTRACT


def test_scworkbench_python_path_contract_is_exactly_in_lockstep():
    repo = scworkbench_source_directory()
    protocol_source = (repo / "R" / "python_protocol.R").read_text()
    containment_source = (repo / "R" / "path_containment.R").read_text()
    bridge_source = (repo / "R" / "op_python_bridge.R").read_text()

    version = re.search(
        r'\.wb_bundle_path_contract_version\s*<-\s*function\(\)\s*"([^"]+)"',
        containment_source,
    )
    assert version is not None
    r_contract = {
        "version": version.group(1),
        "durable_paths": re.search(r'durable_paths\s*=\s*"([^"]+)"', protocol_source).group(1),
        "external_inputs": re.search(r'external_inputs\s*=\s*"([^"]+)"', protocol_source).group(1),
        "serialized_models": re.search(r'serialized_models\s*=\s*"([^"]+)"', protocol_source).group(
            1
        ),
    }
    assert r_contract == PATH_CONTRACT
    assert "path_contract = .wb_pyproto_path_contract()" in bridge_source
    assert "PYBENCH_PATH_CONTRACT_VIOLATION" in bridge_source
