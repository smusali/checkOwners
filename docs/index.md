# checkOwners

checkOwners infers who knows each part of a repository from its git history,
then compares that with the CODEOWNERS file you committed. The repository is
[smusali/checkowners](https://github.com/smusali/checkowners).

It reads git locally, sends nothing anywhere without a token, and never phones
home. Network use is opt-in GitHub API access.

## A five-minute audit

Git 2.23.0 or newer is required, and the clone needs full history.

```bash
pip install checkowners
checkowners audit
```

`uvx checkowners audit` runs the same thing without installing. A bare
`checkowners` runs `audit`, and the exit codes are in
[Exit codes](USAGE.md#exit-codes).

## In CI

The composite Action needs a full clone; a shallow checkout fails before
analysis starts.

```yaml
permissions:
  contents: read
steps:
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: smusali/checkowners@v0
    with:
      comment_on_pr: false
```

Inputs, outputs, permissions, and the SARIF upload are in
[GitHub Actions](USAGE.md#github-actions).

## Write CODEOWNERS

```bash
checkowners generate
```

The command fills the generated block and leaves a manual block alone. Where to
go next:

- [Usage](USAGE.md) for every command, every config key, and CI.
- [Methodology](METHODOLOGY.md) for the signals, the formulas, and the limits.
- [Privacy](PRIVACY.md) for what is read, stored, and sent.
- [FAQ](FAQ.md) for tokens, teams, identity, and troubleshooting.
