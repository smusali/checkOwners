"""Shared fixtures: keep every test away from the real ~/.checkowners."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CHECKOWNERS_STATE_DIR at a per-test directory.

    The state file, graph cache, and email->handle cache all live under the
    state dir; without this fixture any test that exercises them would read
    and pollute the developer's real cache.
    """
    monkeypatch.setenv("CHECKOWNERS_STATE_DIR", str(tmp_path / "checkowners-state"))
