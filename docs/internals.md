# Internals

The scoring contract is [Methodology](METHODOLOGY.md). The decisions behind that contract are in [Decisions](decisions.md).

## Pattern semantics

Matching is GitHub CODEOWNERS semantics: the last matching pattern in file order assigns the owners. The implementation is `checkowners.patterns`. `generate` and `validate` accept a dialect. GitHub output is the CODEOWNERS file described in [Generated CODEOWNERS](USAGE.md#generated-codeowners). GitLab writes one section per top-level directory. Bitbucket and Gerrit are accepted names and exit 2 because those forges are not rendered.

## Caching

Analysis state is cached per repository under `~/.checkowners` (override the root with `CHECKOWNERS_STATE_DIR`). A later command reuses that file when its commit is `HEAD`, the scoring config hash matches, and the model ids match. Blame line counts are cached by blob SHA, path, blame flags, ignore-revs, mass-refactor revisions, and `.mailmap`. State, graph, and blame-cache files share a 256 MiB cap and are evicted oldest-first. `cache clear` removes analysis files and keeps handle files. `cache purge` deletes the cache directory. Flags and the on-disk layout are in [Pipeline](USAGE.md#pipeline).

## Benchmark methodology

`benchmarks/harness.py` shells out to `checkowners analyze`. It does not import the scorers. A result records cold runtime, warm runtime, peak resident memory, git command count, GitHub API request count, cache size, and incremental runtime after one new commit. Two runs on the same commit must match the git command count. Pull-request CI runs the synthetic `ci` tier and passes `--counts-only` because a hosted runner does not hold the wall-time band. The procedure, tiers, and recorded figures are in the [benchmark readme](https://github.com/smusali/checkowners/blob/main/benchmarks/README.md).
