# CheckOwners

Local-first documentation for inferring CODEOWNERS from git history. The repository is [smusali/checkowners](https://github.com/smusali/checkowners).

Zero telemetry, ever, by default. CheckOwners does not phone home. Network use is opt-in GitHub API access.

## Five-minute audit

Install the core package and audit the repository you are in. Git 2.23.0 or newer is required, and the clone needs full history.

```bash
pip install checkowners
checkowners audit
```

`uvx checkowners audit` runs the same command without a permanent install. With no subcommand, `checkowners` runs `audit`. `checkowners --help` still prints help.

Exit status is the same for every command. The codes are in [Exit codes](reference.md#exit-codes).

## CI setup

The composite Action needs a full clone. A shallow checkout fails before analysis starts.

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

Inputs, outputs, and the comment and SARIF options are in [Action](reference.md#action). Workflow examples stay in [GitHub Actions](USAGE.md#github-actions).

## Generate CODEOWNERS

```bash
checkowners generate
```

The command writes inferred owners into the generated block and leaves a manual block unchanged. Dialects, round-trip checks, and the file layout are in [Generated CODEOWNERS](USAGE.md#generated-codeowners).
