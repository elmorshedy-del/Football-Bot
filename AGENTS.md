# Football-Bot Agent Instructions

These instructions apply to the entire repository.

## Read before changing code

1. Read `docs/PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md` completely. It is the authoritative
   contract for the clock, 88+ sleeve, event ledger, trade-high, export, latency, frontend, testing,
   production-verification, review, and rollback work.
2. Read `docs/DUAL_SLEEVE_IMPLEMENTATION_CHECKLIST.md` and
   `docs/DUAL_SLEEVE_CHANGELOG.md`. Update them with exact actions and evidence as work proceeds;
   do not rely on conversational memory.
3. The README describes the currently deployed behavior. Where its expected-expiration 88-window
   description conflicts with the production-integrity specification, the specification governs
   the new implementation.

## Hard invariants

- This is paper trading only. Do not add or call a live-order endpoint.
- Do not claim guaranteed profit. Report observed paper evidence, fees, latency, uncertainty, and
  limitations exactly.
- Do not change Gate A detection, confirmation, sizing, entry, exit, fee, lockout, or settlement
  behavior.
- The price-only sleeve may use only the narrow clock gate defined in the specification to establish
  minute 88+. It must not consume scores, goals, penalties, VAR/corrections, scorers, narratives, or
  canonical event labels.
- A price-only paper fill without a fresh persisted 88+ clock stamp is invalid and must fail closed.
  Expected expiration and UTC wall time are diagnostics, never substitutes for match minute.
- Use executable held-side bids for trade highs. Never use midpoint, last price, ask, or settlement.
- Preserve original storage identifiers and raw provider payloads. Human-readable normalization is
  additive and presentation-safe.
- Historical data that was not recorded stays null with an explicit reason; never fabricate it.
- Database migrations must be additive and idempotent. Rollback must not delete newly collected
  observations.
- Never expose credentials in logs, exports, tests, commits, URLs, or screenshots.
- Do not modify or deploy the unrelated Railway service named `kalchi-kill`.

## Working method

- Use implementation branch `codex/production-integrity-clock-export` and one independently
  revertible PR. Keep the commit boundaries prescribed in Section 10 of the specification.
- Before each implementation section, mark its checklist item in progress and record the exact plan
  and acceptance test. Afterward, record files changed, tests, results, limitations, and rollback.
- Keep unrelated refactors and formatting out of the PR. Preserve user changes in a dirty worktree.
- Make failures, stale data, missing coverage, and disconnections visible; do not swallow them.
- Do not deploy or merge the implementation PR until the requested independent final review passes.

## Required validation

Run the specification's targeted tests plus the full CI-equivalent suite:

```bash
python -X dev -m unittest discover -s tests -v
python -m compileall -q app tests
ruff check --select E9,F63,F7,F82 app tests
node --check static/app.js
git diff --check
```

Use dependencies pinned in `requirements-dev.lock`. Also complete every production and rendered
acceptance item in Sections 11 and 12 of the specification. Attach machine-readable evidence to the
implementation PR. A green unit suite alone is not merge approval.

## Handoff

The implementation PR must remain unmerged for the final reviewer. Include the PR number, head SHA,
CI run, deployment ID if a review deployment was authorized, production evidence, known limitations,
and exact rollback command. Any failed reviewer item blocks merge.
