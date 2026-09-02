# Implementor 2 handoff — production integrity

- Status: **backend commits 1–4 done locally; frontend commit 5 half-started; nothing pushed; no implementation PR.**
- Do not merge until independent final review (`docs/PRODUCTION_INTEGRITY_IMPLEMENTATION_SPEC.md` §§11–12).
- Paper-only. Do not change Gate A. Do not touch Railway service `kalchi-kill`.

Read first: `AGENTS.md`, then the specification in full.

## 1. Where the work is

| Item | State |
|---|---|
| Spec / plan PR 11 | Already on `main` ancestry via `origin/codex/production-integrity-plan` (`46aa34c`, `db96583`) |
| Implementation branch | `cursor/production-integrity-clock-export-aaf8` (Cloud Agent cannot use the spec name `codex/production-integrity-clock-export`; keep this name and explain it in the PR) |
| Tracks | `origin/codex/production-integrity-plan` |
| HEAD | `d828357` *Split reliable audit and raw exports* |
| Pushed? | **No.** Branch is 4 implementation commits ahead of the plan branch and has never been published |
| Implementation PR | **Does not exist.** Create one draft PR after finishing or after pushing this snapshot |
| Local suite at commit 4 | 145 tests, ruff `E9,F63,F7,F82` clean |

Working tree after this handoff commit may still contain incomplete frontend files. Treat them as scaffolding for spec commit 5, not as that commit.

## 2. Done — spec commits 1–4 (committed)

These four commits are individually reviewable and the full unittest suite passed after commit 4.

### Commit 1 — `d0a02aa` Persist match clocks and canonical provider events

Additive only. Gate A and price-only admission unchanged in this commit.

- New `app/match_clock.py`: parse current clock from `details.time` → `match_clock`/`game_clock`/`clock` → clock portion of `status_text`. Never from `last_play` or significant-event times.
- New `match_clock_observations` table; insert on clock/period/status change only.
- Immutable `signals.match_clock_snapshot` JSON schema `football.match_clock_stamp.v1` on **every** new signal (including declines). Unusable stamps are complete with `unusable_reason`.
- Canonical `provider_match_events` ledger with fingerprint de-dupe.
- APIs: `GET /api/match-clocks`, `GET /api/provider-events`. Trades join stamp as `match_clock`.
- Config: `MATCH_CLOCK_MAX_AGE_MS` default 2500.
- Demo has no live clocks; `app/replay.py` injects a synthetic 90+5 stamp.

### Commit 2 — `89d15d0` Gate paper 88+ sleeve on persisted live clock

- `engine._run_price_only` uses `MatchClockGate` only. Expected expiration cannot open the sleeve.
- Distinct outcomes: `clock_88_plus`, `clock_pre_88`, `clock_unmapped`, `clock_missing`, `clock_malformed`, `clock_stale`, `clock_not_live`, `clock_final`, `clock_suspended`, `clock_abandoned`, `clock_first_half`, `clock_half_time`, `clock_pre_match`, `clock_period_unusable`.
- Price-only declines persist as `sleeve_<outcome>` (e.g. `sleeve_clock_pre_88`).
- AST/import allowlist test: classifier and paper desk cannot import score/event/narrative fields.
- Al-Hazm 90+5 replay reaches the price classifier instead of `sleeve_outside_window`.

**Gate contract (do not weaken):** mapping + live status + fresh age + second-half (or equivalent) + minute ≥ 88. `90′` and `90+N′` stay eligible while the market is open.

### Commit 3 — `f669841` Record executable trade highs and latency readiness

- `trades.max_executable_bid`, `max_executable_bid_ts`, `mfe_c`. Held-side **best bid only**. Equal high keeps first timestamp. Settlement/ask/mid/last cannot update.
- API derives `high_after_entry_s`.
- Per-kind latency query (no global `LIMIT 1000`). Canonical kinds: `feed_ingress_ms`, `decision_ms`, `paper_entry_ms`, `order_arrival_ms`, `paper_exit_ms`, `match_response_ms`, `match_clock_age_ms`, `scheduler_lag_ms`. Legacy aliases still query.
- Readiness states: `PASS`, `BREACH`, `COLLECTING`, `STALE`, `INVALID`. K4 threshold remains 250 ms; min samples 20.
- `/api/status` `health.ok` is false on K4 `BREACH`/`INVALID`. Banner:
  - `all_systems_good` / “All systems good”
  - `evidence_not_ready` / “Runtime healthy · paper evidence not ready”
  - `latency_breach` / “Runtime healthy · execution latency breached”
  - `attention_required`
- Header UI still **ignores** this banner (see §3). Backend is done.

### Commit 4 — `d828357` Split reliable audit and raw exports

Backend complete:

| Product | Behavior |
|---|---|
| `POST /api/export/prepare?scope=audit` (default) | Tables + snapshot + schema + hashes + **raw inventory only**. No segment bodies. |
| `POST /api/export/prepare?scope=full` | Audit + ZIP64 `STORED` copy of gzip segments; SHA-256 in one pass; progress + cancel |
| Concurrent jobs | Many audit jobs OK; **one** full job at a time (second full prepare returns the active full job) |
| Status | `queued\|preparing\|ready\|error\|expired\|cancelled` plus processed/total bytes and segments |
| `POST /api/export/jobs/{id}/cancel` | Sets `cancel_requested`; worker raises `exporter.ExportCancelled` |
| `GET /api/export/jobs/{id}/download` | Native `FileResponse`; job-scoped HttpOnly cookie **or** admin header; Range supported |
| `GET /api/export/raw` | Inventory + cookie `footballbot_export_raw` |
| `GET /api/export/raw/{name}` | Protected segment; `safe_raw_segment_path` rejects traversal; Range `bytes=start-end` |
| TTL / lease | 3600 s; file not deleted while `leases > 0` |
| `GET /api/export` | Compatibility full bundle (blocking `to_thread`) |

`build_study_bundle(..., scope="audit"|"full", progress=, cancel_check=)`. Manifest includes `scope`, `include_raw`. Secrets never serialized.

## 3. Half completed — spec commit 5 (frontend)

**Do not treat the current `static/*` edits as commit 5.** Markup and labels were started; behavior was not wired. Finish this as one reviewable commit: `Expose clock, high, event, latency, and export audit UI`.

### Already in the dirty / scaffolding tree

`static/index.html`

- Header button is “Download audit data” with `data-export-scope="audit"`.
- System tab: `#clock-coverage-panel`, `#clock-coverage`, `#clock-faults`.
- `#latency-table` next to `#latency-chart`.
- `#export-panel` with audit / full / cancel buttons, `#export-progress`, `#export-error`, `#raw-segment-list`.
- Full button has **no** `data-export-trigger`; cancel is `hidden`.

`static/style.css`

- Layout classes for export actions, raw rows, clock faults, `.loss-path`, `.trade-high`, `.clock-stamp`, 360 px breakpoint.
- A nested `@media (max-width: 360px)` had briefly unclosed the 420 px block and dropped `.equity-chart` min-heights. That parse break was repaired in the worktree. Keep both 420 px and 360 px closed, and keep `.equity-chart` rules.

`static/app.js` — **labels only**

- `state.clocks` exists but `refreshAll()` never fetches `/api/match-clocks`.
- `filters.gate` exists but `filterMarkup` / `passesFilters` / reset do not use it. Contract test still lists `query, strategy, match, result, association, period` — add `gate` to the test when you add `data-filter-field="gate"`.
- Clock-gate outcome labels, association aliases (`temporally_associated`, `no_nearby_same_match_event`), `clockGateLabels`, `latencyLabels`, `humanClockGate()` exist.
- Nothing renders clock stamps, highs, gate chips, coverage, latency table, or dual downloads.

### Still using old behavior (must replace)

`renderHealth()` still computes local `allHealthy` and hardcodes `ALL SYSTEMS GOOD`. Spec: use `status.health.banner` / `banner_text`. Keep the literal `ALL SYSTEMS GOOD` in JS for `tests/test_frontend_contract.py` when banner is `all_systems_good`. While collecting/stale show “Runtime healthy · paper evidence not ready”, never “all good”.

`downloadExport()` still:

- `POST /api/export/prepare` **without** `?scope=`
- polls only `status === "preparing"` (misses `queued`)
- ignores progress fields
- clears admin token on **any** error (spec: clear only on 401; 5xx/timeout must stay)
- has no AbortController / per-request timeout
- no cancel POST
- no raw-segment listing
- `finally` restores “Download study data” / “Prepare study data” even though the buttons were renamed

`tradeCard()` still shows entry → exit only. Missing without opening raw JSON:

- match date/time, persisted clock (`88′` / `90+5′`), age, precision
- max executable bid, MFE, UTC high time, `high_after_entry_s`
- nearby canonical event **or** explicit “No nearby same-match event”
- losing trades: visual **entry → high → exit** (`.loss-path` CSS is unused)

`signalCard()` does not show `match_clock` or the exact 88-gate outcome. Use `signal.match_clock` and/or `signal.trigger.price_only_inference.match_clock_gate.outcome`. Outcomes like `sleeve_clock_pre_88` map to `clock_pre_88`.

`renderLatency()` still maps legacy keys `order_arrival`, `paper_entry`, `feed_lag`, `goal_provider_response` and **filters out `n === 0`**, so COLLECTING kinds vanish. Render every canonical kind with n / p50 / p95 / max / age / threshold / state.

`eventAssociationBlock` uses `matched.association` (`unmatched`, `nearby_goal`, …). Spec labels also live on `matched.event_association` (`temporally_associated`, `state_consistent`, `state_mismatch`, `no_nearby_same_match_event`). Show both facts: strategy reason vs nearby event. Never `caused_by`.

No `renderClockCoverage()`. Data is already on `GET /api/status` → `clock_coverage` (`watched`, `mapped`, `clock_present`, `clock_fresh`, `clock_gate_candidate_misses`, `faults`, `mapping_errors`) and `GET /api/match-clocks`.

### Frontend acceptance (spec §8) checklist for commit 5

- [ ] Closed trade card: human match/contract, sleeve, match time, clock stamp, trigger, entry/exit/qty/gross/fees/net, high/MFE/high time/seconds after entry, nearby event or “No nearby same-match event”, expandable raw IDs
- [ ] Loss cards prioritize entry → high → exit
- [ ] Every signal: immutable clock stamp + exact 88-gate outcome; accepted/declined counts match `/api/stats`
- [ ] Filters: sleeve, match, 88-gate, event association, profitable/loss, period
- [ ] Clock coverage panel
- [ ] Latency panel for every metric
- [ ] Separate audit / full / raw-segment downloads; progress; visible errors; native `<a>` download; **no** `response.blob()`
- [ ] Poll with timeout/abort; do not wipe admin token on transient 5xx
- [ ] Usable at 360 px: wrap, no ellipsis (already forbidden), download controls full width
- [ ] Update `tests/test_frontend_contract.py` for new ids (`clock-coverage`, `export-audit-button`, `latency-table`, `data-export-scope`, `scope=audit`, cancel path). Keep `ALL SYSTEMS GOOD` and no `await response.blob()`.

### Suggested JS wiring (do not invent APIs)

```
GET  /api/status            health.banner, banner_text, runtime_ok, clock_coverage, latency_readiness
GET  /api/trades            closed[].match_clock, max_executable_bid, max_executable_bid_ts, mfe_c, high_after_entry_s, matched_event
GET  /api/signals           match_clock, trigger.price_only_inference.match_clock_gate, matched_event
GET  /api/latency           {kind: {n,p50,p95,max,invalid,age_s,threshold_ms,state,hist}}
GET  /api/match-clocks      {coverage, observations}
GET  /api/provider-events   canonical ledger (optional extra event list)
POST /api/export/prepare?scope=audit|full
GET  /api/export/jobs/{id}
POST /api/export/jobs/{id}/cancel
GET  /api/export/jobs/{id}/download   cookie path /api/export/jobs/{id}
GET  /api/export/raw
GET  /api/export/raw/{name}           cookie path /api/export/raw
```

Poll loop must accept `queued` **and** `preparing`. Cookie is job-scoped; native download relies on it being set on prepare.

Direct function tests pass `range_header=None` because FastAPI `Header()` is otherwise the default object. `_ranged_file_response` already treats non-str as no Range.

## 4. Not started

### Spec commit 6 — `Document production evidence and rollback`

Checklist + changelog only. Do not mix with code.

Update:

- `docs/DUAL_SLEEVE_IMPLEMENTATION_CHECKLIST.md` — production-integrity items are **not** listed yet (file still describes the older dual-sleeve work; §10.7 is `IN PROGRESS` from the previous PR).
- `docs/DUAL_SLEEVE_CHANGELOG.md` — no entry for commits 1–4.
- Rollback text already in spec §13: revert the merge commit; do not drop additive columns; cancel export workers first; disable price-only via `PRICE_ONLY_SLEEVE_MODE` if needed; Gate A stays.

### Implementation PR

Not created. After commit 6 (or after pushing 1–4 + this handoff):

- `ManagePullRequest` `create_pr`, `draft=true`, `base_branch=main` unless the owner says otherwise. Head is this cursor branch.
- State that the spec asked for `codex/production-integrity-clock-export` and Cloud Agent required `cursor/production-integrity-clock-export-aaf8`.
- Do not merge. Subscribe CI; attach §11 evidence.

### Spec §11 production verification

Cannot be finished without production `ADMIN_TOKEN` / deploy. Local items you can still attach:

1. Full suite, `compileall`, ruff, `node --check static/app.js`, `git diff --check`, lockfile audit
2. Deterministic Al-Hazm replay (already a unit test; re-run and quote output)
3–7. Production snapshots, audit ZIP < 10 s, range download, 360 px screenshots, paper-only + untouched `kalchi-kill` — **blocked until deploy**

### Original extra ask: download study data, test, design simulator, architect notes

**Not started.** After the UI can actually download an audit bundle:

1. Pull a production audit ZIP (admin). If production is unreachable, use a local demo DB + a couple of `feed-*.jsonl.gz` fixtures.
2. Recompute from exported tables: p50/p95 per kind, `mfe_c`, `high_after_entry_s`, sleeve counts vs `/api/stats`.
3. Write simulator notes (see §6) in this file or `docs/PRICE_ONLY_BACKTEST_HANDOFF.md` — do not silently change the live strategy.
4. Remaining bot ideas go to the architect; do not ship them in this PR.

## 5. How to continue (ordered)

1. Keep branch `cursor/production-integrity-clock-export-aaf8`. Do not rewrite commits 1–4 unless review finds a defect.
2. Finish **commit 5** only: `static/index.html`, `static/app.js`, `static/style.css`, `tests/test_frontend_contract.py`. Run:

```bash
python -X dev -m unittest discover -s tests -v
python -m compileall -q app tests
ruff check --select E9,F63,F7,F82 app tests
node --check static/app.js
git diff --check
```

3. **Commit 6** changelog/checklist/rollback. No code.
4. `git push -u origin cursor/production-integrity-clock-export-aaf8` (retry with backoff on network errors).
5. Open **one draft** implementation PR. Quote this handoff and the spec. Commits 1–4 + 5 + 6 should be the reviewable stack.
6. If authorized: production §11. If not: list blockers in the PR (no `ADMIN_TOKEN`, no deploy).
7. Simulator / architect notes after a real audit bundle exists.

## 6. Simulator design (for architect review — not implemented)

Purpose: replay **raw** `feed-*.jsonl.gz` plus SQLite clocks/events against frozen paper desks, so 88+ fills can be studied without the live process.

Inputs (already in the export):

- `database/footballbot-snapshot.db` and `tables/*.csv|jsonl`
- `raw/feed-YYYYMMDD-HH.jsonl.gz` (full scope only; audit has inventory)
- `manifest.json` hashes and `configuration` allowlist
- `docs/PRICE_ONLY_BACKTEST_HANDOFF.md`

Proposed stages (keep Gate A and price-only as **two independent** desks, same as live):

1. **Clock reconstruction** from `match_clock_observations`, not from expected expiration.
2. **Book reconstruction** from recorder frames (orderbook snapshot/delta) at local receipt `lt`.
3. **Decision replay** calling existing `PriceOnlyLateScoreSleeve.classify` and Gate A detector with the same frozen config.
4. **Paper fill model** using recorded executable bids and configured `PAPER_ENTRY_LATENCY_MS` / exit latency — never live orders.
5. **Report** per-signal clock stamp, gate outcome, fill/no-fill, high, MFE, fees, vs production rows (diff, do not overwrite).

Out of scope for this PR: changing sleeve thresholds from simulator output.

## 7. Known pitfalls

- **Missing clock vs not-live:** evaluate missing clock **before** status, otherwise unmapped/missing stamps fail as the wrong outcome. Covered by tests; do not reorder.
- **`MatchClockGate` mapped flag:** infer mapped from `unusable_reason != "unmapped"`; a default `mapped=True` hid unmapped failures.
- **Demo clocks:** inject 90+5 in replay; do not pretend wall time is minute 88.
- **Export worker vs event loop:** `started.wait()` in tests blocked asyncio; poll with `await asyncio.sleep`. Status must stay responsive during full prepare (test exists).
- **Cookie path** is `/api/export/jobs/{job_id}`, not `/api/export/jobs`.
- **Do not** default prepare to `full`; browser default is audit so Railway does not sit on a multi-GB zip again.
- Historical rows without stamps/highs stay `null` with reason; never backfill minutes or highs.
- `sleeve_outside_window` based on expected expiration must not appear on **new** price-only records after the clock gate. Old rows remain as historical evidence.

## 8. Ideas for a better bot (architect only — not this PR)

These are diagnostic findings, not license to change Gate A or ship a new strategy:

1. **Clock-calibrated 88+** is the actual study. Expected-expiration ± minutes was why Al-Hazm 90+5 never entered the price sleeve (expiration ~3820 s after the signal).
2. **K4 250 ms** is still the promotion bar. Health must not say all-good while p95 is seconds. Collecting ≠ pass.
3. **High vs exit** on losers is the missed-profit question; MFE is `max(0, high − entry)` but the raw high stays visible when below entry.
4. **Event association is not a trigger.** Sparse events are expected. Do not train on “nearby goal” as causality.
5. **Export product split** is operational integrity: an audit ZIP that prepares in seconds is the study path; raw segments are optional range downloads.
6. Possible later research (separate PR, after replay exists): stoppage-aware sizing, fee-aware scratch vs trail using recorded MFE, league-specific clock mapping quality, scheduler-lag as a kill for evidence collection. None of this belongs in commits 5–6.

## 9. Invariants (copy from spec; do not bargain)

- Paper only; no live-order endpoint.
- Gate A detection/entry/exit/sizing/fees/lockout/settlement unchanged.
- Price-only 88+ gate is clock-only (period, minute, stoppage, status, freshness). No score/scorer/goal/penalty/VAR/correction/narrative/canonical event fields.
- No 88+ paper fill without a fresh persisted usable stamp.
- Executable held-side bid for highs.
- Additive SQLite migrations; rollback does not drop new tables/columns.
- One revertible PR; several reviewable commits.
- No credentials in logs, exports, URLs, screenshots.
