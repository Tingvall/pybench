"""Packaging contract for the accepted Palantir numerical oracle."""

import tomllib
from pathlib import Path


def test_palantir_extra_pins_accepted_oracle_stack() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["palantir"] == [
        "palantir==1.3.6",
        "pandas==2.3.3",
    ]


def test_operation_pack_fixtures_pin_current_pybench() -> None:
    root = Path(__file__).parent / "fixtures"
    projects = sorted(root.glob("operation-pack-*/pyproject.toml"))
    assert projects
    for path in projects:
        project = tomllib.loads(path.read_text())
        assert project["project"]["dependencies"] == ["pybench==0.1.1"]
