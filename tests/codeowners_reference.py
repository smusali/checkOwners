"""Reference CODEOWNERS matcher.

Port of the pattern matcher from https://github.com/hmarr/codeowners
(match.go). Used only to label and differential-test the supported glob
language.

MIT License

Copyright (c) 2020 Harry Marr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import re

_META = frozenset(r"\.+*?()|[]{}^$")


def reference_matches(pattern: str, path: str) -> bool:
    """Return whether ``pattern`` matches repo-relative ``path``.

    Empty patterns and patterns containing ``***`` do not match.
    """
    if not pattern or "***" in pattern:
        return False
    if _is_literal(pattern):
        return _literal_match(pattern, path)
    compiled = _build_regex(pattern)
    if compiled is None:
        return False
    return compiled.search(path) is not None


def _is_literal(pattern: str) -> bool:
    return pattern.startswith("/") and not any(char in pattern for char in "*?\\")


def _literal_match(pattern: str, path: str) -> bool:
    prefix = pattern[1:]
    if not prefix:
        return False
    if prefix.endswith("/"):
        return path.startswith(prefix)
    if len(path) == len(prefix):
        return path == prefix
    return len(path) > len(prefix) and path[len(prefix)] == "/" and path.startswith(prefix)


def _quote_meta(char: str) -> str:
    if char in _META:
        return "\\" + char
    return char


def _translate_segment(segment: str, *, is_last: bool) -> str:
    out: list[str] = []
    escaped = False
    for char in segment:
        if escaped:
            out.append(_quote_meta(char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "*":
            out.append(r"[^/]*")
        elif char == "?":
            out.append(r"[^/]")
        else:
            out.append(_quote_meta(char))
    if is_last:
        out.append(r"(?:/.*)?")
    return "".join(out)


def _build_regex(pattern: str) -> re.Pattern[str] | None:
    if not pattern or "***" in pattern:
        return None
    if pattern == "/":
        return re.compile(r"\A\Z")
    segments = pattern.split("/")
    if segments[0] == "":
        segments = segments[1:]
    elif segments[0] != "**" and (len(segments) == 1 or (len(segments) == 2 and segments[1] == "")):
        segments = ["**", *segments]
    if len(segments) > 1 and segments[-1] == "":
        segments[-1] = "**"
    if not segments:
        return re.compile(r"\A\Z")
    last = len(segments) - 1
    need_slash = False
    parts: list[str] = [r"\A"]
    for index, segment in enumerate(segments):
        if segment == "**":
            if index == 0 and index == last:
                parts.append(r".+")
            elif index == 0:
                parts.append(r"(?:.+/)?")
                need_slash = False
            elif index == last:
                parts.append(r"/.*")
            else:
                parts.append(r"(?:/.+)?")
                need_slash = True
            continue
        if segment == "*":
            if need_slash:
                parts.append("/")
            parts.append(r"[^/]+")
            need_slash = True
            continue
        if need_slash:
            parts.append("/")
        parts.append(_translate_segment(segment, is_last=index == last))
        need_slash = True
    parts.append(r"\Z")
    return re.compile("".join(parts))
