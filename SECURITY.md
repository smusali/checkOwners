# Security Policy

## Supported versions

Only the latest release published on [PyPI](https://pypi.org/project/checkowners/) receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/smusali/checkowners/security/advisories/new). Do not open a public issue for security reports, and do not disclose the report before a fix is published. You can expect an initial response within 3 days.

## Design notes relevant to security

- What the tool reads, what leaves the machine, what the cache holds, and how to delete it are in [docs/PRIVACY.md](docs/PRIVACY.md), including the data-flow diagram and threat model.
- CheckOwners never reads a GitHub token from its config file: `github.token` in `.github/checkowners.yml` is rejected at load time because that file is committed to git. The only supported source is the `GITHUB_TOKEN` environment variable.
- The state directory (`~/.checkowners/` by default) contains an ownership map derived from git history. Email addresses in state, the graph cache, and `handles.json` are stored as tokens, not raw addresses. `@handles` are kept. Point `CHECKOWNERS_STATE_DIR` at an ephemeral location in CI, and avoid committing it (a `.checkowners/` entry ships in this repo's `.gitignore` as a guard). The composite Action already does this (`${{ runner.temp }}/checkowners-state`); CLI and hand-rolled CI must set it themselves. `checkowners cache purge` deletes that directory's contents, including `handles.json`. State, graph, and handle files are written via a temp file and `replace`, under an advisory lock.
- Core inference makes no network calls. Network access is limited to the optional GitHub API features (`github` extra). `--offline` is the explicit form of that boundary: the process does not open a network connection, and review evidence and team verification are unavailable.
- The composite GitHub Action installs the `checkowners` version that matches its own tag from a committed wheel, and installs third-party dependencies from `requirements.lock` with `--require-hashes`. Trusted Publishing and Sigstore attestations on the way out of PyPI are not discarded on the way into consumer CI: a tag pin no longer silently tracks "latest."
- Third-party GitHub Actions used by this repository and by the composite Action are pinned to full 40-character commit SHAs with a version comment. Dependabot (`github-actions`) keeps those pins current.
- `install_spec` is constrained to a local extras allowlist for this repository's own dogfood workflow. Interpolating untrusted data into that input is remote code execution; downstream callers must omit it.
