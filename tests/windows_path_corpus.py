"""Corpus of Windows-style path strings and expected match / exclude behaviors.

Windows environments can present paths with:
- Backslashes instead of forward slashes (`src\\components\\Button.tsx`)
- Mixed slashes (`src/components\\Button.tsx`)
- Windows drive letters (`C:\\repo\\src\\main.py`, `D:/workspace/docs/index.md`)
- Extended / UNC path prefixes (`\\\\?\\C:\\repo\\src\\main.py`, `\\\\server\\share\\repo\\src\\main.py`)
- Case folding / case insensitivity (e.g. `SRC/APP.PY` vs `src/app.py`)

This module provides deterministic fixture entries and runner-independent tests
to verify matcher behavior (`patterns.py`), rule evaluation (`match_path`),
and filter exclusions (`paths.exclude` via `analyze._is_excluded`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from checkowners.analyze import _is_excluded
from checkowners.patterns import CodeownersRule, match_path, parse_rules, pattern_matches


def normalize_posix_path(path: str) -> str:
    """Normalize a path (including Windows drive letters, backslashes, UNC) into a repo-relative POSIX path.

    - Strips leading Windows drive letters (e.g. `C:\\...`, `c:/...`)
    - Strips UNC prefixes (e.g. `\\\\?\\C:\\...`, `\\\\server\\share\\...`)
    - Replaces backslashes with forward slashes
    - Strips leading forward slashes
    """
    p = path.strip()
    # Strip verbatim / device prefix \\?\
    if p.startswith(("\\\\?\\", "//?/")):
        p = p[4:]
    # Strip UNC share prefix \\server\share\
    unc_match = re.match(r"^[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/](.*)$", p)
    if unc_match:
        p = unc_match.group(1)
    # Strip drive letter e.g. C: or c:
    drive_match = re.match(r"^[a-zA-Z]:[\\/](.*)$", p)
    if drive_match:
        p = drive_match.group(1)
    # Normalize backslashes to forward slashes
    p = p.replace("\\", "/")
    # Remove leading slashes
    return p.lstrip("/")


@dataclass(frozen=True)
class WindowsPathMatchCase:
    """A test case representing a Windows-shaped path matched against a CODEOWNERS pattern."""

    category: str
    pattern: str
    raw_path: str
    expected_direct: bool
    expected_normalized: bool
    description: str


@dataclass(frozen=True)
class WindowsPathExcludeCase:
    """A test case representing a Windows-shaped path evaluated against paths.exclude patterns."""

    category: str
    patterns: tuple[str, ...]
    raw_path: str
    expected_exclude: bool
    description: str


WINDOWS_PATH_MATCH_CORPUS: tuple[WindowsPathMatchCase, ...] = (
    # 1. Backslashes
    WindowsPathMatchCase(
        category="backslash",
        pattern="src/*",
        raw_path=r"src\app.py",
        expected_direct=False,
        expected_normalized=True,
        description="Single-level directory with backslash separator (direct fails because regex expects '/')",
    ),
    WindowsPathMatchCase(
        category="backslash",
        pattern="src/**",
        raw_path=r"src\components\Button.tsx",
        expected_direct=False,
        expected_normalized=True,
        description="Globstar matching nested backslash path",
    ),
    WindowsPathMatchCase(
        category="backslash",
        pattern="*.py",
        raw_path=r"backend\services\auth.py",
        # Note: Unanchored extension pattern (?:.+/)?([^/]+) translates to matches ending in .py without '/'
        # Because the backslash path contains no forward slashes, regex matches the whole string as the filename.
        expected_direct=True,
        expected_normalized=True,
        description="Floating file extension pattern against nested backslash path",
    ),
    WindowsPathMatchCase(
        category="backslash",
        pattern="/docs/",
        raw_path=r"docs\architecture\ADR.md",
        expected_direct=False,
        expected_normalized=True,
        description="Root anchored directory pattern against backslash path",
    ),
    # 2. Mixed forward and backslashes
    WindowsPathMatchCase(
        category="mixed slash",
        pattern="src/components/*",
        raw_path=r"src/components\Modal.tsx",
        expected_direct=False,
        expected_normalized=True,
        description="Mixed forward and backward slashes in subpath",
    ),
    WindowsPathMatchCase(
        category="mixed slash",
        pattern="packages/**",
        raw_path=r"packages\ui/src\index.ts",
        expected_direct=False,
        expected_normalized=True,
        description="Mixed slashes under packages globstar",
    ),
    # 3. Windows drive letters
    WindowsPathMatchCase(
        category="drive letter",
        pattern="src/*",
        raw_path=r"C:\repo\src\main.py",
        expected_direct=False,
        expected_normalized=False,  # stripped to 'repo/src/main.py', does not match 'src/*'
        description="Drive letter absolute path C:\\repo\\src\\main.py",
    ),
    WindowsPathMatchCase(
        category="drive letter",
        pattern="docs/**",
        raw_path=r"D:\docs\getting-started.md",
        expected_direct=False,
        expected_normalized=True,
        description="Drive letter directly on repo folder D:\\docs\\getting-started.md",
    ),
    WindowsPathMatchCase(
        category="drive letter",
        pattern="*.rs",
        raw_path=r"c:/cargo/src/lib.rs",
        # Forward slashes present; unanchored *.rs matches the trailing /lib.rs segment
        expected_direct=True,
        expected_normalized=True,
        description="Drive letter with forward slashes and extension pattern",
    ),
    # 4. UNC and Extended Path prefixes
    WindowsPathMatchCase(
        category="UNC prefix",
        pattern="api/**",
        raw_path=r"\\server\share\api\v1\routes.py",
        expected_direct=False,
        expected_normalized=True,
        description="UNC share path prefix mapped to repo relative path",
    ),
    WindowsPathMatchCase(
        category="UNC prefix",
        pattern="core/*",
        raw_path=r"\\?\C:\core\engine.cpp",
        expected_direct=False,
        expected_normalized=True,
        description="Extended path prefix \\\\?\\C:\\core\\engine.cpp",
    ),
    # 5. Spaces and special characters with Windows separators
    WindowsPathMatchCase(
        category="spaces and special chars",
        pattern="my folder/*",
        raw_path=r"my folder\document.txt",
        expected_direct=False,
        expected_normalized=True,
        description="Space in directory with backslash separator",
    ),
    WindowsPathMatchCase(
        category="dotfiles with backslash",
        pattern="/.github/workflows/*",
        raw_path=r".github\workflows\ci.yml",
        expected_direct=False,
        expected_normalized=True,
        description="Dotfile directory with backslash separator",
    ),
)


WINDOWS_PATH_EXCLUDE_CORPUS: tuple[WindowsPathExcludeCase, ...] = (
    # fnmatch on Windows handles backslashes natively for path exclusions
    WindowsPathExcludeCase(
        category="paths.exclude backslash",
        patterns=("*.tmp", "build/**", "node_modules/**"),
        raw_path=r"build\output.js",
        expected_exclude=True,
        description="Backslash path inside build directory matches build/**",
    ),
    WindowsPathExcludeCase(
        category="paths.exclude backslash",
        patterns=("*.tmp", "build/**", "node_modules/**"),
        raw_path=r"node_modules\lodash\index.js",
        expected_exclude=True,
        description="Backslash path in node_modules matches node_modules/**",
    ),
    WindowsPathExcludeCase(
        category="paths.exclude backslash",
        patterns=("*.generated.*",),
        raw_path=r"src\schema.generated.ts",
        expected_exclude=True,
        description="Backslash path with .generated. suffix",
    ),
    WindowsPathExcludeCase(
        category="paths.exclude mixed",
        patterns=("dist/**",),
        raw_path=r"dist/esm\bundle.js",
        expected_exclude=True,
        description="Mixed slash path inside dist directory",
    ),
    WindowsPathExcludeCase(
        category="paths.exclude non-matching",
        patterns=("*.lock", "vendor/**"),
        raw_path=r"src\services\payment.py",
        expected_exclude=False,
        description="Standard source file not matching exclude list",
    ),
)
