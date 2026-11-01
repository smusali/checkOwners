"""checkOwners: Infer CODEOWNERS from git history with confidence scoring."""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.7.0"

__all__ = ["RepositoryAnalyzer", "__version__"]

if TYPE_CHECKING:
    from checkowners.analyze import RepositoryAnalyzer


def __getattr__(name: str) -> object:
    """Return the named public attribute, importing its module on first use."""
    if name == "RepositoryAnalyzer":
        from checkowners.analyze import RepositoryAnalyzer  # noqa: PLC0415

        return RepositoryAnalyzer
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
