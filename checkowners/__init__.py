"""checkOwners: Infer CODEOWNERS from git history with confidence scoring."""

__version__ = "0.6.0"

from checkowners.analyze import RepositoryAnalyzer

__all__ = ["RepositoryAnalyzer", "__version__"]
