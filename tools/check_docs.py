#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer
import yaml

from checkowners.cli import ExitCode, app
from checkowners.config import _V2_TOP_LEVEL

_ROOT = Path(__file__).resolve().parents[1]
_SITE_PREFIX = "/checkowners/"


def command_names() -> tuple[str, ...]:
    root = typer.main.get_command(app)
    found: list[str] = []

    def walk(node: object) -> None:
        children = getattr(node, "commands", None)
        if not isinstance(children, dict):
            return
        for key, child in children.items():
            if isinstance(key, str):
                found.append(key)
            walk(child)

    root_name = getattr(root, "name", None)
    if isinstance(root_name, str) and root_name:
        found.append(root_name)
    walk(root)
    return tuple(dict.fromkeys(found))


def has_token(text: str, token: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


def _string_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(key for key in value if isinstance(key, str))


def action_names() -> tuple[str, ...]:
    loaded = yaml.safe_load((_ROOT / "action.yml").read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return ()
    return _string_keys(loaded.get("inputs")) + _string_keys(loaded.get("outputs"))


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "link", "area", "base"}:
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _site_path(path: str) -> str | None:
    if path.startswith(_SITE_PREFIX):
        return path.removeprefix(_SITE_PREFIX).lstrip("/")
    if path.startswith("/") and not path.startswith("//"):
        return path.lstrip("/")
    return None


def _local_target(page: Path, href: str, site: Path) -> Path | None:
    parsed = urlparse(href)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc != "smusali.github.io":
            return None
        relative = _site_path(unquote(parsed.path))
        if relative is None:
            return None
        target = site / relative
    elif parsed.scheme:
        return None
    else:
        raw = unquote(parsed.path)
        if not raw:
            return None
        relative = _site_path(raw) if raw.startswith("/") else None
        target = site / relative if relative is not None else (page.parent / raw).resolve()
    if target.is_dir() or target.suffix == "":
        index = target / "index.html"
        html = target.with_suffix(".html")
        if index.is_file():
            return index
        if html.is_file():
            return html
    return target


def missing_links(site: Path) -> tuple[str, ...]:
    missing: list[str] = []
    root = site.resolve()
    for page in sorted(root.rglob("*.html")):
        collector = _HrefCollector()
        collector.feed(page.read_text(encoding="utf-8"))
        label = page.relative_to(root)
        for href in collector.hrefs:
            target = _local_target(page, href, root)
            if target is None:
                continue
            resolved = target.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                missing.append(f"{label} -> {href}")
                continue
            if not resolved.is_file():
                missing.append(f"{label} -> {href}")
    return tuple(missing)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    site = Path(args[0]) if args else Path("site")
    if not site.is_dir():
        print(f"built site not found: {site}", file=sys.stderr)
        return 1
    reference = (site / "reference" / "index.html").read_text(encoding="utf-8")
    cli_page = (site / "cli" / "index.html").read_text(encoding="utf-8")
    problems: list[str] = []
    for name in command_names():
        if not has_token(cli_page, name):
            problems.append(f"CLI reference is missing command {name}")
    for code in ExitCode:
        phrase = f"exit code {code.value}"
        if phrase not in reference:
            problems.append(f"reference is missing {phrase}")
    for name in action_names():
        if not has_token(reference, name):
            problems.append(f"reference is missing Action name {name}")
    for key in sorted(_V2_TOP_LEVEL):
        if not has_token(reference, key):
            problems.append(f"reference is missing configuration key {key}")
    problems.extend(f"broken link {item}" for item in missing_links(site))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
