# Example configurations

Reference configs for two common shapes. They are sanitized illustrations, not dumps from design partners. Copy a file to `.github/checkowners.yml` and edit it. CheckOwners only loads that path (or `CHECKOWNERS_CONFIG`).

| Directory | When to start from it |
|---|---|
| [`solo-maintainer/`](solo-maintainer/) | One or two humans, no org teams, no GitHub API. Matches a personal repo. |
| [`org-with-teams/`](org-with-teams/) | An organization that collapses owners to `@org/team`, with a shorter recency half-life. |

Neither sample `CODEOWNERS` lives at a location GitHub (or CheckOwners) auto-detects at the repository root, so they will not be picked up as this project's ownership file.

[demo.gif](demo.gif) is a `checkowners --offline drift` run on a sanitized tree: `/payments/` is declared `@platform`, and history belongs to `@alice` and `@bob`. [pr-comment.png](pr-comment.png) is the pull-request comment for that run. [sample-CODEOWNERS](sample-CODEOWNERS) is what `generate --force` wrote for the same tree. [sample-output.md](sample-output.md) is trimmed `analyze` and `drift` JSON from this repository.
