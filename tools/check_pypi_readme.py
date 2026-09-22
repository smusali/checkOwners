#!/usr/bin/env python3
from __future__ import annotations

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

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
        self.image_srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value and key in {"href", "src"}:
                self.urls.append(value)
            if tag == "img" and key == "src" and value:
                self.image_srcs.append(value)


def _markdown_destinations(text: str) -> list[str]:
    found = [_destination(match) for match in _INLINE.findall(text)]
    found.extend(_destination(match) for match in _REFERENCE.findall(text))
    return found


def _rendered(html: str) -> _HrefCollector:
    collector = _HrefCollector()
    collector.feed(html)
    return collector


def _repo_raw_path(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = unquote(parsed.path)
    prefixes = {
        "raw.githubusercontent.com": "/smusali/checkowners/main/",
        "github.com": "/smusali/checkowners/raw/main/",
    }
    prefix = prefixes.get(host)
    if prefix is None or not path.startswith(prefix):
        return None
    return path[len(prefix) :]


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

    rendered = _rendered(html)
    relative_html = [url for url in rendered.urls if _is_relative(url)]
    if relative_html:
        print("::error::Relative href/src after PyPI render:")
        print("\n".join(f"  {url}" for url in relative_html))
        return 1

    root = _ROOT.resolve()
    missing: list[str] = []
    for src in rendered.image_srcs:
        rel = _repo_raw_path(src)
        if rel is None:
            continue
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            missing.append(src)
    if missing:
        print("::error::README image does not match a file in this tree:")
        print("\n".join(f"  {url}" for url in missing))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
