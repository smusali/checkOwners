[![CI](https://github.com/smusali/checkowners/actions/workflows/ci.yml/badge.svg)](https://github.com/smusali/checkowners/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/checkowners.svg)](https://pypi.org/project/checkowners/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/smusali/checkowners/blob/main/LICENSE)

# checkOwners

checkOwners infers who knows each part of a repository from its git history, then
compares that with the CODEOWNERS file you committed.

It reads git locally, sends nothing anywhere without a token, and `--offline` opens no network connection.

> This is a knowledge-risk tool, not a performance-measurement tool. Using it for
> individual evaluation is unsupported and harmful.
## Install

Needs Python 3.11 to 3.15 and Git 2.23.0 or newer, on Linux, macOS, and Windows.

```bash
pip install checkowners
```

The optional `graph` extra adds the graph, topology, and onboarding commands:

```bash
pip install "checkowners[graph]"
```

## Commands

Audit the committed CODEOWNERS against inferred ownership:

```bash
checkowners audit
```

Report where the file and the history disagree:

```bash
checkowners drift
```

Write a CODEOWNERS file from the history:

```bash
checkowners generate
```

Rank paths by knowledge risk:

```bash
checkowners risk
```

Ask who should review a change:

```bash
checkowners suggest --diff main...HEAD
```

Commands, flags, configuration, and exit codes are in the
[usage guide](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md).

## In CI

```yaml
name: checkowners
on: [pull_request]

jobs:
  drift:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: smusali/checkowners@v0
```

A pull-request comment also needs `pull-requests: write`, and a code scanning
upload needs `security-events: write` with `upload_sarif: true`.

## Stability

Before 1.0.0, an incompatible change bumps the minor version and is listed in the
[changelog](https://github.com/smusali/checkowners/blob/main/docs/CHANGELOG.md).

## More

- [Usage](https://github.com/smusali/checkowners/blob/main/docs/USAGE.md)
- [Methodology](https://github.com/smusali/checkowners/blob/main/docs/METHODOLOGY.md)
- [Privacy](https://github.com/smusali/checkowners/blob/main/docs/PRIVACY.md)
- [FAQ](https://github.com/smusali/checkowners/blob/main/docs/FAQ.md)
- [Contributing](https://github.com/smusali/checkowners/blob/main/docs/CONTRIBUTING.md)

## License

MIT
