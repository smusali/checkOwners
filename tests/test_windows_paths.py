"""Cross-platform corpus for Windows-shaped path strings."""

from __future__ import annotations

import pytest

from checkowners.analyze import _is_excluded
from checkowners.patterns import pattern_matches


@pytest.mark.parametrize(
    ("path", "matches_src", "excluded_as_lock"),
    [
        ("src/main.py", True, False),
        (r"src\main.py", False, False),
        (r"src\deep/mixed.lock", False, True),
        (r"C:\repo\src\deps.lock", False, True),
        (r"\\server\share\repo\src\deps.lock", False, True),
        ("SRC/main.py", False, False),
    ],
)
def test_windows_path_corpus(
    path: str,
    matches_src: bool,
    excluded_as_lock: bool,
) -> None:
    """Exercise CODEOWNERS matching and paths.exclude without filesystem access."""
    assert pattern_matches("src/**", path) is matches_src
    assert _is_excluded(path, ("*.lock",)) is excluded_as_lock
