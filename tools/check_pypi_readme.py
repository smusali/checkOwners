#!/usr/bin/env python3
from __future__ import annotations

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from readme_renderer.markdown import render

_ROOT = Path(__file__).resolve().parents[1]
_INLINE = re.compile(r"\]\(([^)]+)\)")
_REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s+(\S+)", re.MULTILINE)


def _destination(raw: str) -> str:
    return raw.strip().split()[0].strip("<>")


def _is_relative(url: str) -> bool:
    parsed = urlparse(url)
    return not (parsed.scheme or parsed.netloc or url.startswith("#"))


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"href", "src"} and value:
                self.urls.append(value)


def _markdown_destinations(text: str) -> list[str]:
    found = [_destination(match) for match in _INLINE.findall(text)]
    found.extend(_destination(match) for match in _REFERENCE.findall(text))
    return found


def _rendered_urls(html: str) -> list[str]:
    collector = _HrefCollector()
    collector.feed(html)
    return collector.urls


def main() -> int:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    docs_url = project.get("project", {}).get("urls", {}).get("Documentation")
    if not isinstance(docs_url, str) or not docs_url.startswith("https://"):
        print("::error::project.urls.Documentation must be an https URL")
        return 1

    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    relative = [url for url in _markdown_destinations(readme) if _is_relative(url)]
    if relative:
        print("::error::Relative Markdown destinations in README.md:")
        print("\n".join(f"  {url}" for url in relative))
        return 1

    html = render(readme)
    if html is None:
        print("::error::readme_renderer failed to render README.md")
        return 1

    relative_html = [url for url in _rendered_urls(html) if _is_relative(url)]
    if relative_html:
        print("::error::Relative href/src after PyPI render:")
        print("\n".join(f"  {url}" for url in relative_html))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
