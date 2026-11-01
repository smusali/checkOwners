"""The Typer application: the global callback and every command."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Annotated

import typer

from checkowners import __version__
from checkowners.cli import codeowners, ownership, report, risk
from checkowners.cli.common import (
    CliSession,
    bind_session,
    console,
    err_console,
    exit_with,
)
from checkowners.errors import CheckOwnersError
from checkowners.github import (
    clear_api_evidence,
    set_offline,
)
from checkowners.models import (
    RunOptions,
)

app = typer.Typer(
    name="checkowners",
    help="Infer and maintain CODEOWNERS from git history.",
    rich_markup_mode="rich",
    invoke_without_command=True,
)
for _module in (codeowners, ownership, risk, report):
    app.registered_commands.extend(_module.commands.registered_commands)
app.add_typer(codeowners.baseline, name="baseline")
app.add_typer(report.cache, name="cache")
app.add_typer(report.identity, name="identity")


_DIFF_COMMANDS = frozenset(
    {
        "analyze",
        "audit",
        "doctor",
        "drift",
        "expertise",
        "explain",
        "owners",
        "qualified-owners",
        "risk",
        "suggest",
    }
)

_OFFLINE_LINES = (
    "Network access: disabled",
    "Review evidence: unavailable",
    "Team verification: unavailable",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"checkowners {__version__}")
        raise typer.Exit()


@app.callback()
def _app_callback(
    ctx: typer.Context,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Score recency as of this UTC instant (ISO 8601).",
        ),
    ] = None,
    deterministic: Annotated[
        bool,
        typer.Option(
            "--deterministic",
            help=(
                "Documented guarantee: analysis is a pure function of "
                "the repository, commit, and config."
            ),
        ),
    ] = False,
    exit_zero: Annotated[
        bool,
        typer.Option(
            "--exit-zero",
            help="Exit 0 when the only problem is findings.",
        ),
    ] = False,
    fail_on_incomplete: Annotated[
        bool,
        typer.Option(
            "--fail-on-incomplete",
            help="Exit 3 when scored-signal completeness is below 1.",
        ),
    ] = False,
    allow_stale: Annotated[
        bool,
        typer.Option(
            "--allow-stale",
            help="Reuse cached analysis even when its commit is not HEAD.",
        ),
    ] = False,
    max_age: Annotated[
        int | None,
        typer.Option(
            "--max-age",
            min=0,
            help="Refuse cached analysis older than this many seconds. 0 expires immediately.",
        ),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Do not read or write the analysis cache.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print blame cache hits and misses.",
        ),
    ] = False,
    no_incremental: Annotated[
        bool,
        typer.Option(
            "--no-incremental",
            help="Recompute the full lookback window.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Make no network calls. Review evidence and team verification are unavailable.",
        ),
    ] = False,
    redact_emails: Annotated[
        bool,
        typer.Option(
            "--redact-emails",
            help="Replace email addresses in output with stable tokens.",
        ),
    ] = False,
    diff_only: Annotated[
        str | None,
        typer.Option(
            "--diff-only",
            help="Limit read-only analysis to paths changed in SPEC (base...head or a revision).",
        ),
    ] = None,
) -> None:
    """Infer and maintain CODEOWNERS from git history."""
    clear_api_evidence()
    options = RunOptions(
        as_of=as_of,
        deterministic=deterministic,
        exit_zero=exit_zero,
        fail_on_incomplete=fail_on_incomplete,
        allow_stale=allow_stale,
        max_age=max_age,
        no_cache=no_cache,
        verbose=verbose,
        no_incremental=no_incremental,
        offline=offline,
        redact_emails=redact_emails,
        diff_only=diff_only,
    )
    ctx.obj = CliSession(options)
    bind_session(ctx.obj)
    if diff_only is not None and ctx.invoked_subcommand not in _DIFF_COMMANDS:
        raise typer.BadParameter("--diff-only applies to read-only analysis commands")
    set_offline(offline)
    if offline:
        for line in _OFFLINE_LINES:
            err_console.print(line, highlight=False, markup=False)
    if ctx.invoked_subcommand is None:
        ctx.invoke(report.audit)


def _guarded(callback: Callable[..., object]) -> Callable[..., object]:
    """Return `callback` with a raised CheckOwnersError mapped to its exit code."""

    @functools.wraps(callback)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return callback(*args, **kwargs)
        except CheckOwnersError as exc:
            console.print(f"[red]{exc}[/red]")
            exit_with(exc.exit_code)

    return wrapper


for _group in (app, codeowners.baseline, report.cache, report.identity):
    if _group.registered_callback is not None and _group.registered_callback.callback is not None:
        _group.registered_callback.callback = _guarded(_group.registered_callback.callback)
    for _command in _group.registered_commands:
        if _command.callback is not None:
            _command.callback = _guarded(_command.callback)


def main() -> None:
    """Entry point for the checkowners CLI."""
    app()
