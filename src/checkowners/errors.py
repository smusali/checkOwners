"""Process exit codes and the exception tiers the CLI maps onto them."""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar


class ExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL = 1
    CONFIG = 2
    FINDINGS = 3
    INTEGRATION = 4


class CheckOwnersError(Exception):
    """Base class for every error that carries its own exit code."""

    exit_code: ClassVar[ExitCode] = ExitCode.INTERNAL


class ConfigError(CheckOwnersError):
    """The configuration, the CODEOWNERS file or an argument is unusable."""

    exit_code: ClassVar[ExitCode] = ExitCode.CONFIG


class FindingsError(CheckOwnersError):
    """The run completed and produced blocking findings."""

    exit_code: ClassVar[ExitCode] = ExitCode.FINDINGS


class IntegrationError(CheckOwnersError):
    """Git, the GitHub API or the environment did not cooperate."""

    exit_code: ClassVar[ExitCode] = ExitCode.INTEGRATION
