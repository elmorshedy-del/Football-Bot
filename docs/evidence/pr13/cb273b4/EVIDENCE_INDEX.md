# PR 13 final evidence index — validated candidate `cb273b4`

| Field | Value |
|---|---|
| Repository | `elmorshedy-del/Football-Bot` |
| Pull request | **#13** — draft, open, unmerged; do not deploy from this hand-back |
| Validated candidate head | `cb273b465f6a086d910366056a17987db377d0cf` |
| Validated candidate tree | `f24518e4b7b07c50611a5cc3df2ce586fd3ef4b8` |
| Base branch / head | `main` / `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| Reviewed head | `d69f5a4c512fddf55d30cb79079a69e0fa6a1651` |
| Head branch | `claude/binding-handoff-review-0ajqcl` |
| Runtime mode | Paper only |
| Railway `kalchi-kill` | Untouched |

## Machine-readable artifacts

| Artifact | Path | SHA-256 |
|---|---|---|
| Final CI validation gate | `docs/evidence/pr13/cb273b4/ci-validation-gate.txt` | `342e9632916278ebc479010d26ff19c1d5d44af287c843c5b14c9847d578ae33` |
| PR 13 baseline red | `docs/evidence/pr13/baseline-d69f5a4/baseline-red-tests.txt` | `f7256b7d7dd84a447a1580048b5e8f77ae7e689cf90df8150c0d3e0ca39ba539` |
| Historical local gate for candidate 0cbf651 | `docs/evidence/pr13/0cbf651/local-validation-gate.txt` | `7bc6b04d5bc35b7cdd641eaae69c9aad3dd5bfa71dfab878370323adbeaa67e7` |

The old `0cbf651` local gate remains historical evidence only. It is **not** the final candidate record.
Its hash above is recomputed from the checked-in bytes in this branch.

## Continuous integration

| Field | Value |
|---|---|
| Run | https://github.com/elmorshedy-del/Football-Bot/actions/runs/33581738642 |
| Job | `test` (`100097262004`) |
| Validated head | `cb273b465f6a086d910366056a17987db377d0cf` |
| Conclusion | **success** |
| Chromium | 151.0.7922.34; launched before tests |
| Strict suite | **331 tests, OK** |
| Browser/async guard rerun | **331 tests, OK** |
| Static gates | compileall, Ruff fatal selection, Node syntax, diff whitespace/conflict checks — all passed |

Browser acceptance executed on the runner with `REQUIRE_BROWSER_TESTS=1`; the guard found no browser-skip marker and no unretrieved Task/Future exception.

## Hand-back status

All implementation/local/CI remediation requested by the PR 13 follow-up review is represented by the validated candidate and its green run above. Production section 11 remains **BLOCKED** because no authorised deployment target/live `ADMIN_TOKEN` was supplied. BR-07 remains an independent-review requirement and is not self-approved here.

This evidence refresh is documentation-only. The validated runtime candidate is the exact clean head `cb273b465f6a086d910366056a17987db377d0cf` that run `33581738642` tested.
