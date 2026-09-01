"""Keep the PyPI long description independent of its hosting location."""

import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from readme_renderer.markdown import render

_ROOT = Path(__file__).resolve().parents[1]


def _relative_targets(markdown: str) -> list[str]:
    html = render(markdown)
    assert html is not None
    targets = re.findall(r'(?:href|src)="([^"]*)"', html)
    return [
        target for target in targets if not urlsplit(target).scheme and not target.startswith("#")
    ]


def test_readme_links_work_outside_github() -> None:
    assert _relative_targets((_ROOT / "README.md").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "markdown",
    ["[guide](docs/USAGE.md)", "[guide][ref]\n\n[ref]: docs/USAGE.md", "![badge](badge.svg)"],
)
def test_relative_link_guard(markdown: str) -> None:
    assert _relative_targets(markdown)


def test_documentation_project_url() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["urls"]["Documentation"] == (
        "https://github.com/smusali/checkowners/blob/main/docs/USAGE.md"
    )
