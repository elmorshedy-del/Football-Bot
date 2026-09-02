# HISTORICAL — PR 13 evidence index — candidate `0cbf651`

> **Superseded for current hand-back.** Use `docs/evidence/pr13/cb273b4/EVIDENCE_INDEX.md` for the final validated candidate and CI record. The values below are retained as historical evidence.

| Field | Value |
|---|---|
| Repository | `elmorshedy-del/Football-Bot` |
| Pull request | **#13** (must stay draft; **must not merge or deploy**) |
| Candidate head | `0cbf65192f867d7a749d93ddd461002fcc3a5788` |
| Candidate tree | `3b82cf2d479b171aa9d1ec0b6fb8b357df70482a` |
| Base branch / head | `main` / `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| Reviewed head (PR 13 review §0) | `d69f5a4c512fddf55d30cb79079a69e0fa6a1651` |
| Head branch | `claude/binding-handoff-review-0ajqcl` |
| Runtime mode | Paper only |
| Railway `kalchi-kill` | Untouched |
| Date (UTC) | 2026-09-02T00:41:16Z |

## Corrections to the previous evidence (review §1.6)

The reviewer was right on both counts, and both are fixed here.

- **Nonexistent commit.** The checklist, changelog and PR 12 evidence index cited
  `4fbb79d` for the clock work package. That object exists only as an unreferenced
  local artifact of an amend (the original commit included `.venv` and was amended to
  strip it); it is **not** in the PR history, so the documented rollback command could
  not run in a fresh clone. Every citation now reads `c9f490a`.
- **The rollback command was wrong in form as well.** Reverting the work-package
  commits sequentially conflicts, because later packages touch the same code. Verified
  by dry run. The correct command is the range revert below, which applies cleanly.
- **Trailing whitespace** in two PR 12 evidence files failed `git diff --check`. Stripped;
  captured output is now filtered so no assertion or result is altered.
- Evidence is now labelled **PR #13** at the correct candidate head and tree.

## Work packages in this pass

| Commit | Review section | Work |
|---|---|---|
| `6571ecf` | §1.1, §1.2 | One-transaction close; restart resumes at durable max sequence |
| `58860ff` | §1.3 | Signal watch ownership, gaps, finalization marker, restart rebuild |
| `ec780ae` | §1.4 | Mode-scoped APIs, nested queries, path parent authorisation, exports |
| `0cbf651` | §1.5 | Real dashboard browser acceptance; Chromium and strict checks in CI |
| (this) | §1.6 | Evidence corrections, whitespace, regenerated gate |

Earlier packages, unchanged: `c9f490a` (§3-4), `f3d3de0` (§5-6), `00d41f8` (§7),
`9f831c6` (§8.3-8.4), `d69f5a4` (§10).

## Machine-readable artifacts

| Artifact | Path | SHA-256 |
|---|---|---|
| PR 13 baseline red | `docs/evidence/pr13/baseline-d69f5a4/baseline-red-tests.txt` | `f7256b7d7dd84a447a1580048b5e8f77ae7e689cf90df8150c0d3e0ca39ba539` |
| PR 13 validation gate | `docs/evidence/pr13/0cbf651/local-validation-gate.txt` | `7bc6b04d5bc35b7cdd641eaae69c9aad3dd5bfa71dfab878370323adbeaa67e7` |
| PR 12 baseline red | `docs/evidence/pr12/baseline-cd4d36e/baseline-red-tests.txt` | `e5e62bf418984a4ced2cac121ffcb060c2c83899d0aa3d64ccea59e270c8fa9e` |

## Continuous integration

| Field | Value |
|---|---|
| Run | https://github.com/elmorshedy-del/Football-Bot/actions/runs/33576969178 |
| Job | `test` (`100082963919`) |
| Validated head | `3f1f8e369deb9b88a3601a180c3f5bb80910889a` |
| Conclusion | success |
| Chromium | 151.0.7922.34, launched and verified before the suite |

Browser acceptance **actually executed** on the runner. All six
`test_dashboard_browser` tests appear by name in the job log, and the run
contains **zero skip markers** — the failure mode of run `33561327860`, which was
green with three skipped acceptance tests, cannot recur silently.

Two earlier runs on this branch are part of the record and worth reading:

- `33576470763` passed **while its own log contained** `Future exception was never
  retrieved`. The guard at that commit matched only the `Task exception` wording, so
  it reported green over a real leak.
- `33576746007` went **red** once the guard matched both wordings. Root cause was in
  the test file itself: `chromium_path()` entered and immediately exited a
  `sync_playwright()` context manager purely to resolve a path, abandoning the
  driver's `initialize()` future. It surfaced only on hosts without a preinstalled
  binary, which is why CI saw it and local runs did not. The probe is removed in
  `3f1f8e3`.

## Local results

- Full suite **298 tests, OK**, run twice consecutively under
  `python -X dev -W error::RuntimeWarning`.
- `compileall`, `ruff --select E9,F63,F7,F82`, `node --check static/app.js`,
  `git diff --check origin/main...HEAD`: all clean.
- **Zero tracebacks, zero unretrieved task exceptions, zero skipped browser tests.**
- Browser acceptance re-run with `REQUIRE_BROWSER_TESTS=1`: **6 tests, OK**, against the
  shipped dashboard in headless Chromium 141.
- Production-schema copy migrates twice with no loss (`tests/test_production_migration.py`).

## Mutation proofs this pass (all reverted, none committed)

| Invariant removed | Caught by |
|---|---|
| Frontend numeric cache key | `no chart rendered` — real click, real browser |
| Flat `d` across gaps | cross-gap subpath assertion |
| Renamed `.path-error` class | visible-error assertion |

Earlier mutation proofs are recorded in `docs/DUAL_SLEEVE_CHANGELOG.md`.

## Exact rollback command (verified by dry run)

```bash
git revert --no-commit c9f490a^..HEAD && git commit
```

Sequentially reverting individual work packages conflicts and must not be used.
Migrations add only nullable columns and partial indexes; reverting drops no collected
data. **Back up `footballbot.db` first:** reverting `f3d3de0` restores the destructive
`purge_non_live()`, which would delete demo and legacy evidence on the next live boot.

## §11 production evidence — BLOCKED

Unchanged: every item needs an authorised deployment target and a live `ADMIN_TOKEN`.
None has been supplied, and this session has no deploy access. These are required
verifications that have **not** been performed; none may be recorded as `NOT OBSERVED`.

| §11 item | State |
|---|---|
| 1. CI URL, deployment id/service/environment, rollback | BLOCKED (rollback command verified locally) |
| 2. Timestamped JSON from ten production endpoints | BLOCKED |
| 3. Authorised audit bundle <10s, hashes, per-mode counts | BLOCKED (per-mode counts and reconciliation now in the manifest, proven locally) |
| 4. Protected raw-segment Range request | BLOCKED |
| 5. Full export concurrent with reads/writes; p50/p95/max | BLOCKED (250ms bound proven locally) |
| 6. Service restart proving persistence | BLOCKED (restart recovery proven locally) |
| 7. Accepted price-only signals reconciled to persisted 88+ rows | BLOCKED (invariant proven locally) |
| 8. Desktop and 360px screenshots with console/network logs | PARTIAL — rendered 360px assertions automated; screenshots BLOCKED |
| 9. No live-order path | Source/config verified: no live-order endpoint or call in the diff; runtime proof BLOCKED |

## Requested decision

`BLOCKED` — see the §13 hand-back in `docs/DUAL_SLEEVE_CHANGELOG.md`. The implementer
does not self-approve.
