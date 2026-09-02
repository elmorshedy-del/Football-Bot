# PR 12 evidence index — candidate `9f831c6`

| Field | Value |
|---|---|
| Repository | `elmorshedy-del/Football-Bot` |
| Pull request | #12 (draft; **must not merge**) |
| Candidate head | `9f831c6af3b3366451393421ad10666ae655570a` |
| Candidate tree | `e9515218dda9c574a122bdbb9c526d916213268e` |
| Base branch / head | `main` / `8b6a8a8e736f8eb59cec7383a51b38c857947816` |
| Reviewed head (handoff §0) | `cd4d36e1adeb01d63381fce79b58d6311cfc7b2d` |
| Work branch | `claude/binding-handoff-review-0ajqcl` (based on PR 12 head `5f546fe`) |
| Runtime mode | Paper only |
| Railway `kalchi-kill` | Untouched |
| Date (UTC) | 2026-09-01T21:11:07Z |

## Starting-head note (handoff §0)

The handoff pins the reviewed head at `cd4d36e`. When this pass began, PR 12's head was
`5f546fe` — `cd4d36e` plus the docs-only handoff commit itself. There was no code delta, so
every reviewed finding still applied unchanged. Work is stacked on `5f546fe`.

## Machine-readable artifacts

| Artifact | Path | SHA-256 |
|---|---|---|
| Baseline red tests (`BR-00`) | `docs/evidence/pr12/baseline-cd4d36e/baseline-red-tests.txt` | `aa6ccff69b31073e49e771231bf6aed9b485cc330353c2c32c006d681670f7b6` |
| Local validation gate (`BR-LOCAL`) | `docs/evidence/pr12/9f831c6/local-validation-gate.txt` | `7ff47da6a76d001c5b2f12d4b97c88f1811df295e53b9b79425a9d6f290122e7` |

## Commits by work package

| Commit | Work package | Sections |
|---|---|---|
| `c9f490a` | Clock publication and current health | §3, §4 |
| `f3d3de0` | Evidence modes and provider-event lineage | §5, §6 |
| `00d41f8` | Non-blocking export and captured failures | §7 |
| `9f831c6` | Gap-aware paths and a working chart | §8.3, §8.4, part of §8.2 |

## Local results

- Full suite: **260 tests, OK**, run twice consecutively under
  `python -X dev -W error::RuntimeWarning -m unittest discover -s tests`.
- `compileall`, `ruff check --select E9,F63,F7,F82`, `node --check static/app.js`,
  `git diff --check`: all clean.
- **Zero tracebacks and zero unretrieved task exceptions** in the recorded output. At the reviewed
  head every run printed `Task exception was never retrieved`, which alone failed `BR-03`.
- Browser interaction: 3 tests in real headless Chromium 141 (`tests/test_frontend_path_browser.py`).
  They **skip rather than pass** when Chromium is unavailable.
- Migration from a production-schema copy: `tests/test_production_migration.py`, 5 tests,
  migrated twice, no loss, no duplicate columns or indexes, provenance unchanged (`BR-04`).

## Mutation proofs (all reverted, none committed)

| Invariant removed | Caught by |
|---|---|
| `is_persisted_id()` positive-id check → `return True` | 13 assertions across 4 files |
| `mode_clause()` → no scoping | 3 mode-isolation tests |
| `_finite_timestamp()` validation bypassed | 10 invalid-occurrence shapes |
| `backup_database()` → reviewed-head lock behaviour | `265.3ms not less than 250.0` |
| Frontend numeric cache key + flat `d` across gaps | 3 headless-browser assertions |

## §11 production evidence — BLOCKED

Every item below needs an authorised deploy and a live `ADMIN_TOKEN`. The handoff states not to
deploy until the user authorises a target; no target has been authorised, and this session has no
deploy access. **None of these may be recorded as `NOT OBSERVED`** — they are required
verifications that have not been performed.

| §11 item | State |
|---|---|
| 1. CI run URL, deployment id/service/environment, rollback command | BLOCKED |
| 2. Timestamped JSON snapshots from ten production endpoints | BLOCKED |
| 3. Authorised audit bundle ready in <10s, verified hashes, per-mode counts | BLOCKED |
| 4. Protected raw-segment Range request with bytes/hash verification | BLOCKED |
| 5. Full export concurrent with status reads and writes; p50/p95/max | BLOCKED |
| 6. Service restart proving clocks/events/modes/paths persist | BLOCKED |
| 7. Accepted price-only signals reconciled 100% to persisted 88+ clock rows | BLOCKED |
| 8. Desktop and 360px screenshots with console/network logs | BLOCKED |
| 9. Source/config/runtime proof of no live-order path | Partially local: no live-order endpoint exists in the diff; runtime proof BLOCKED |

## Outstanding implementation — `BR-PATH` §8.2

Recorded in full in `docs/DUAL_SLEEVE_CHANGELOG.md` under work package 4. The transactional
final-close ownership contract is not implemented, and four §8.5 tests are unwritten. **`BR-01`
is therefore not satisfied and PR 12 must stay draft.**

## Requested decision

`BLOCKED` — see `§13` hand-back in the changelog.
