# CheckOwners roadmap

CheckOwners infers CODEOWNERS from git history and reports when the committed file has drifted from that evidence. This file is a milestone snapshot. The [action-item register](docs/ACTION_ITEMS.md) and the [issue tracker](https://github.com/smusali/checkowners/issues) are the source of truth. New work starts as an issue.

## How to contribute

1. Pick a [good first issue](https://github.com/smusali/checkowners/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) or another labeled issue you can finish.
2. Follow [CONTRIBUTING.md](docs/CONTRIBUTING.md). The fastest local setup is `pip install -e ".[dev]"`.
3. Open a pull request against `main`. The PR template asks for tests, docs, and any model or contract impact.

Usage questions go to [Discussions](https://github.com/smusali/checkowners/discussions/categories/q-a), not the issue tracker.

## Milestones

| Milestone | Theme | Action items |
|---|---|---|
| `0.5.1` | Unbreak identity, Action correctness, and the contributor on-ramp | A1 · A2 · A3 · A4 · B1 · B2 · B3 · B4 · B5 · B6 · B8 · C1 · H5 · K2 · L2 · N1 |
| `0.6.0` | Correctness and trust | B7 · C5 · C6 · C7 · D1 · D2 · D5 · E3 · E4 · E8 · E11 · F1 · F2 · F3 · F5 · F6 · G7 · H1 · H2 · H3 · I1 · I2 · I3 · K1 · L1 · L3 |
| `0.7.0` | Ownership risk and scale | B9 · C2 · C3 · C4 · C8 · D4 · E10 · E12 · F4 · G2 · G3 · G6 · H6 · J1 · J2 · J4 · K3 · K4 · K5 · L4 |
| `0.8.0` | Audit and rigor | D6 · E1 · E2 · E6 · E7 · E9 · H4 · H7 · M1 · M3 |
| `0.9.0` | Research and reporting | C9 · D3 · D7 · G1 · G4 · J3 · K6 · M2 · M4 |
| `1.0.0` | The contract | G5 · K7 · N2 (plus schema freeze, semver policy, published conformance corpus) |

IDs such as `D1` and `N1` are defined in the [action-item register](docs/ACTION_ITEMS.md). Each row there links to its GitHub issue.
