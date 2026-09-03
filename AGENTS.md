# Football-Bot Agent Instructions

These instructions apply to the entire repository.

## Read before changing code

0. **Read `docs/SPEC_CORRECTIONS_AND_DEVIATIONS.md` first — before the
   specification.** It records where the specification is wrong or
   under-specified, what was deliberately extended beyond it, and what must not
   be "fixed". Several entries exist because the specification as written
   produced a defect. This applies to anyone *revising the specification* as
   much as to anyone implementing it. `tests/test_spec_corrections.py` enforces
   that this file exists and stays referenced.
1. **For pull request 12, read `docs/PR12_BLOCKER_RESOLUTION_HANDOFF.md` completely.** It is the
   binding independent-review remediation contract. Every `BR-*` item requires behavioral and
   production evidence; earlier `PASSED` labels do not override it.
2. Read `docs/PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md` completely. It is the authoritative
   contract for the clock, 88+ sleeve, event ledger, trade-high, export, latency, frontend, testing,
   production-verification, review, and rollback work.
3. Read `docs/DUAL_SLEEVE_IMPLEMENTATION_CHECKLIST.md` and
   `docs/DUAL_SLEEVE_CHANGELOG.md`. Update them with exact actions and evidence as work proceeds;
   do not rely on conversational memory.
4. The README describes the currently deployed behavior. Where its expected-expiration 88-window
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

- For PR 12, continue the existing branch `cursor/production-integrity-clock-export-aaf8` and the
  existing draft PR; do not open a replacement PR or force-push. Keep the commit boundaries in the
  blocker-resolution handoff. For later work, use the branch/commit boundaries prescribed by the
  applicable specification.
- Before each implementation section, mark its checklist item in progress and record the exact plan
  and acceptance test. Afterward, record files changed, tests, results, limitations, and rollback.
- Keep unrelated refactors and formatting out of the PR. Preserve user changes in a dirty worktree.
- Make failures, stale data, missing coverage, and disconnections visible; do not swallow them.
- Do not deploy or merge the implementation PR until the requested independent final review passes.

## Engineering change log

`docs/ENGINEERING_CHANGE_LOG.md` is the record the original architect reads to
understand what changed and why **without** reading the implementation history.
Commit messages are not a substitute: they are per-commit, they are not indexed
by day, and they do not carry validation results or outstanding risk.

This is distinct from `docs/DUAL_SLEEVE_CHANGELOG.md`, which is the scoped
implementation record for the PR 12/13 dual-sleeve contract and stays as it is.
Work inside that contract still updates that file; **all** code change,
including that work, also appends here.

**Every change to `app/`, `static/`, `Dockerfile`, `railway.json`, or any
strategy parameter default appends an entry before that work is handed back.**
Documentation-only edits and test-only edits that accompany a logged change are
covered by that change's entry and need no separate one.

Rules:

- **Append, never rewrite.** Past entries are a record of what was believed at
  the time. If an entry turns out to be wrong, add a dated follow-up entry that
  corrects it and say which entry it corrects. Do not edit history.
- **Newest first.** Newest day at the top; newest change first within a day.
- **One entry per logical change**, not per commit and not per file. A change
  spanning three files is one entry; three unrelated fixes in one commit are
  three entries.
- **Identify entries as `CHG-YYYY-MM-DD-NNN`**, numbered within the day in the
  order the work was done, so `-001` is the first change of that day.
- **Cite evidence, not adjectives.** "Improved latency" is not an entry.
  "Arrival p95 7,018 ms against a 250 ms threshold, n=34" is. Quote real
  measurements with their sample size. If a number came from production, say so.
- **Record what you checked and did *not* change.** A defect you investigated
  and disproved is worth more than silence, because it stops the next person
  re-investigating it. See CHG-2026-09-03-006 for the shape.
- **State residual risk honestly.** An entry with an empty risk section had
  better deserve it.

Each entry uses these headings, in this order:

```markdown
### CHG-YYYY-MM-DD-NNN — <imperative summary>

**Commit:** <sha(s)>
**Components:** <files and modules touched>

**Observed / original behaviour.** What was seen, with measurements, and what
the code did before.

**Root cause.** The actual technical cause. If the change is a design gap
rather than a defect, say so explicitly.

**Why necessary.** What breaks, stays unmeasurable, or stays wrong without it.

**Exact change.** What was implemented, specifically enough to review without
the diff.

**Before / after.** Concrete expected behaviour on both sides, ideally the same
input evaluated against both.

**Reasoning and trade-offs.** Alternatives considered and why they were
rejected. Record the option you did not take.

**Validation.** Tests added and what they assert; suite result; any validation
against real or production data, with numbers.

**Risks / limitations.** Side effects, data that stays wrong, and what the
change does not fix.

**Follow-up.** Remaining work, or "None."
```

Start each day's section with the branch, base commit, commit list, diff
totals, suite counts before and after, and current deployment status.

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
