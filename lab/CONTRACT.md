# Shock Lab — build contract

**This file is the single source of truth for every Shock Lab worker.** Read it fully
before writing code. It exists so that no worker has to re-explore `app/` or re-probe the
Kalshi API — everything already established is written down here. If something you need is
missing, add it here in the same change rather than deriving it privately.

Approved plan: `/root/.claude/plans/create-me-a-new-hashed-emerson.md`.

---

## 1. What is being built

A research platform that tests one hypothesis: **a score-state change late in an MLS match
(draw → lead, or lead → draw) forces violent repricing across Kalshi's 3-way match triplet,
and entering into that repricing is profitable.** It must answer two questions with
uncertainty attached: the best minute cutoff, and the best leg configuration.

Not a trading bot. Nothing here places orders, and no module may import or call a Kalshi
order endpoint.

### Hard repo constraints (from `AGENTS.md`)

- **Do not modify** `app/`, `static/`, `Dockerfile`, `railway.json`, or `tests/` other than
  adding new `tests/test_lab_*.py` files.
- **No live-order endpoint** anywhere, ever.
- Never expose credentials in logs, exports, tests, commits, URLs or screenshots.
- Historical data that was not recorded stays `NULL` with an explicit reason. Never
  fabricate it, never impute it, never silently drop it.
- New code lives in `lab/` (Python) and `lab_static/` (browser). These sit outside the
  paths `AGENTS.md` protects.

### Directory ownership

One owner per directory. Do not write outside your own.

| Directory | Owner | Contents |
|---|---|---|
| `lab/ingest/` | Worker A | Kalshi + provider download, Parquet/DuckDB writing |
| `lab/detect/` | Worker B | Clock, state changes, shock detection, per-leg liquidity |
| `lab/engine/` | Worker D | Backtest: fills, fees, exits, sweep, statistics |
| `lab/api/` | Worker E + integrator | FastAPI app, SQL endpoint, Perspective wiring |
| `lab_static/` | Worker C | Board, Shock Card, Explore front end |
| `lab/common/` | integrator only | Shared helpers (below). Propose changes, don't make them. |

---

## 2. Vocabulary — eight words, enforced by test

The entire user-visible surface uses exactly these eight nouns:

> **Game · Leg · Shock · Goal · Entry · Run · Sweep · Clock · Shape**

| Word | Means |
|---|---|
| **Game** | One MLS match. Exactly one Kalshi event, exactly three legs. |
| **Leg** | One of the three contracts: `HOME`, `AWAY`, `TIE`. |
| **Shock** | A violent multi-leg reprice detected from the trade tape. |
| **Goal** | A real score-state change, from the match-event provider. |
| **Entry** | One simulated trade into a Shock. |
| **Run** | One backtest over the frozen Entry inventory with one settings set. |
| **Sweep** | Many Runs across a parameter grid. |
| **Clock** | Exact match time — minute and stoppage. |
| **Shape** | What a Shock's price path looks like in its first moments. See §5A. |

**Banned from every user-visible string** (UI text, API field names, chart labels, error
messages, column headers):

```
episode, sleeve, gate, "Gate A", signal, triplet, displacement, candidate,
milestone, price-only, sub-threshold, normalized reallocation, arm, trail,
oscillation, confirmation window, detector burst
```

Internal variable names may use whatever is clearest; the ban applies at the API boundary
and outward. `tests/test_lab_vocabulary.py` greps `lab_static/` and every JSON response
schema for the ban list and fails the build on a hit.

Rationale: the user explicitly named Football-Bot's terminology sprawl and disconnected tabs
as the thing to avoid. This is not stylistic — it is a stated requirement.

---

## 3. Kalshi API — verified reference

Base: `https://api.elections.kalshi.com/trade-api/v2` · **no authentication required** for
everything below. All of it was probed directly; response shapes are real, not inferred.

Series ticker: **`KXMLSGAME`**. Event ticker: `KXMLSGAME-<YY><MON><DD><HOME><AWAY>`, e.g.
`KXMLSGAME-26SEP05MIAATL`. Market ticker: `<event>-<LEG>` where LEG is a team abbreviation
or `TIE`.

### 3.1 Live / historical split

`GET /historical/cutoff` →

```json
{"market_settled_ts":"2026-07-13T00:00:00Z","trades_created_ts":"2026-07-13T00:00:00Z",
 "orders_updated_ts":"2026-07-13T00:00:00Z","market_positions_last_updated_ts":"2026-07-13T00:00:00Z"}
```

Markets settled before `market_settled_ts` are served by `/historical/markets`; trades
before `trades_created_ts` by `/historical/trades`. **The cutoff moves** — re-read it each
run, never hardcode. Target live window is 3 months.

Ingest must union both tiers. Verified counts as of planning:

| Query | Rows |
|---|---|
| `/historical/markets?series_ticker=KXMLSGAME` | 1617 |
| `/markets?series_ticker=KXMLSGAME&status=settled` | 423 |
| `/markets?series_ticker=KXMLSGAME&status=open` | 93 |
| **unique markets** | **2133** |
| **unique games** | **711** (every one exactly 3 legs; 676 settled) |
| date span | 2025-05-29 → 2026-09-26 |

### 3.2 Markets

`GET /markets?series_ticker=&status=&limit=1000&cursor=` and
`GET /historical/markets?…` — same shape. Cursor pagination: response `cursor` is empty
when exhausted.

Fields that matter (real sample, trimmed):

```json
{"ticker":"KXMLSGAME-26SEP09VANLAG-VAN",
 "event_ticker":"KXMLSGAME-26SEP09VANLAG",
 "custom_strike":{"soccer_team":"b3fea460-61a1-48c7-a27e-9a01c39cd113"},
 "yes_sub_title":"Vancouver","no_sub_title":"Vancouver",
 "result":"yes","status":"finalized",
 "open_time":"2026-08-27T00:39:00Z",
 "close_time":"2026-09-10T05:02:15Z",
 "expected_expiration_time":"2026-09-10T05:30:00Z",
 "occurrence_datetime":"2026-09-10T05:30:00Z",
 "settlement_ts":"2026-09-10T05:03:45.565013Z",
 "volume_fp":"277461.72","open_interest_fp":"207353.53",
 "yes_bid_dollars":"0.0000","yes_ask_dollars":"1.0000",
 "price_level_structure":"linear_cent"}
```

- `custom_strike.soccer_team` is a **Sportradar team UUID**, and it matches the
  `home_team_id`/`away_team_id` on the milestone. This is the reliable market↔team join.
- The `TIE` leg carries `yes_sub_title: "Tie"`.
- **`expected_expiration_time` and `occurrence_datetime` are schedule estimates and are
  NOT match timing.** Verified wrong by 47 minutes on `KXMLSGAME-26SEP05ATXSJ`
  (said 03:30; last print 02:43). `close_time` is padded too. **Never derive match time
  from any of these.** See §5.
- `*_fp` and `*_dollars` fields are **strings**. Parse with `Decimal`, never `float()` on
  the way in.

### 3.3 Fee schedule

`GET /series/KXMLSGAME` → `"fee_type":"quadratic"`, `"fee_multiplier":1`.

Reuse the repo's verified implementation verbatim (`app/paper.py:61`, copied not imported):

```python
fee = math.ceil(0.07 * fee_multiplier * contracts * p * (1 - p) * 100) / 100.0
```

where `p = price_cents / 100`. Supported: `quadratic`, `quadratic_with_maker_fees`. Any
other value must **raise**, never guess.

Consequence worth knowing: fees are minimal at price extremes, which is where this strategy
trades. NO on a 4¢ leg costs ≈0.27¢/contract; YES on an 82¢ leg ≈1.03¢.

### 3.4 Trade tape — the spine

`GET /markets/trades?ticker=&limit=1000&cursor=&min_ts=&max_ts=`
`GET /historical/trades?ticker=&limit=1000&cursor=`

`min_ts`/`max_ts` are **unix seconds** and are **confirmed working** — use them for
incremental pulls. Newest first.

```json
{"trade_id":"0722afe0-2729-84eb-087f-3c14c5f54011",
 "ticker":"KXMLSGAME-26SEP09VANLAG-VAN",
 "created_time":"2026-09-10T04:37:26.96107Z",
 "yes_price_dollars":"0.9900","no_price_dollars":"0.0100",
 "count_fp":"4.60",
 "taker_side":"no","taker_outcome_side":"no","taker_book_side":"ask",
 "is_block_trade":false}
```

- **`created_time` carries microseconds.** This is the highest-resolution historical data
  Kalshi publishes and the entire platform rests on it. **Parse to integer microseconds
  since epoch; never round to seconds, never store as float.**
- `count_fp` is fractional (`"4.60"`) — contracts are not integers. Store as `DECIMAL(18,2)`.
- Keep `taker_side`, `taker_outcome_side`, `taker_book_side`, `is_block_trade`. The
  MIT-licensed reference schema we borrow drops these; we do not.

**Measured volume:** one game (3 legs) = **41,336 prints / 12.58 MB raw JSON**
(304 B/print). All 711 games ≈ **29M rows, ~9 GB raw JSON, ~0.9 GB Parquet**. Backfill is
≈30,000 requests; hold ≈5 req/s and it takes ~2 hours.

### 3.5 Candlesticks — the only historical quote data

`GET /series/KXMLSGAME/markets/{ticker}/candlesticks?start_ts=&end_ts=&period_interval=1`

`period_interval` ∈ {1, 60, 1440} minutes. `start_ts`/`end_ts` unix seconds. For markets
settled before the cutoff use `GET /historical/markets/{ticker}/candlesticks`.

```json
{"end_period_ts":1789005600,
 "price":{"open_dollars":"0.7800","high_dollars":"0.7800","low_dollars":"0.7800",
          "close_dollars":"0.7800","mean_dollars":"0.7800","previous_dollars":"0.7700"},
 "yes_bid":{"open_dollars":"0.7700","high_dollars":"0.7700","low_dollars":"0.7700","close_dollars":"0.7700"},
 "yes_ask":{"open_dollars":"0.7800","high_dollars":"0.7800","low_dollars":"0.7800","close_dollars":"0.7800"},
 "volume_fp":"2.52","open_interest_fp":"45952.09"}
```

A request is **capped at 5000 candles**, so a full match window must be chunked
(4000 minutes per request is a safe stride). The endpoint returns the truncated
set without an error, so an unchunked request silently loses the tail.

`yes_bid`/`yes_ask` are **the only historical record of the spread**, and the per-leg spread
metric depends on them. In minutes with no trade, `price` has only `previous_dollars`.

### 3.6 Match events

Two calls. First map event → milestone:

`GET /milestones?limit=10&related_event_ticker=<event_ticker>`

Filter results to those whose `related_event_tickers` contains your event ticker; take the
first. The milestone `details` carries `home_team_id` / `away_team_id` as Sportradar UUIDs
matching `custom_strike.soccer_team`.

Then fetch state — **repeat the parameter, do not comma-join** (a comma-joined list returns
zero rows; httpx list params produce the right form):

`GET /live_data/batch?milestone_ids=<id>&milestone_ids=<id>&include_player_stats=false`

```json
{"live_datas":[{"milestone_id":"144418c8-…","type":"soccer_tournament_multi_leg",
 "details":{
   "home_same_game_score":3,"away_same_game_score":0,
   "home_significant_events":[
     {"event_type":"score_change","player":"White, Brian","time":"8'"},
     {"event_type":"score_change","player":"Caicedo, Bruno","time":"90+1'"},
     {"event_type":"yellow_card","player":"Diaby, Yadaly","time":"90+3'"}],
   "away_significant_events":[…],
   "period_scores":[{"number":1,"type":"regular_period","home_score":1,"away_score":0}, …],
   "last_play":{"description":"…","occurence_ts":1789014689},
   "half":"FT","status":"closed","status_text":"Full-Time","winner":"home",
   "match_status":"ended","coverage":{…}}}]}
```

**This persists after full-time** — historical games return their complete final event list.

Verified across 29 MLS games: `event_type` ∈ {`score_change`, `yellow_card`, `red_card`}
only. **There is no VAR event type and no penalty event type.** A goal that was overturned
simply never appears. Ground truth for VAR/penalties must come from the provider in §4.

`time` is a display string: `"8'"`, `"90+1'"`. Parse with a copy of
`app/match_clock.py:parse_clock_text()` — it already handles typographic primes and the
period-ordinal trap documented in its comments. **`time` is a label, not a clock** — it is
integer-minute and observed to cluster. The authoritative clock is §5.

### 3.7 Client manners

Copy the retry shape from `app/kalshi.py:107` (do not import): 6 attempts, explicit `429`
handling, exponential backoff 1.0s → ×2 → cap 20s, also retry transport errors. Hold
≈5 req/s globally. Kalshi budgets tokens rather than a fixed RPS, and treats a 429 as a
signal to back off.

---

## 4. Match-event provider (API-Football)

Config: `APIFOOTBALL_KEY` (env). Base `https://v3.football.api-sports.io`, header
`x-apisports-key`. MLS is league **253**. Absent key ⇒ every provider field is `NULL` and
`clock_source` degrades per §5 — the platform must still build, ingest and run.

Two endpoints:

- `GET /fixtures?league=253&season=<yyyy>` → the fixture list, and critically
  `fixture.periods` (§5).
- `GET /fixtures/events?fixture=<id>` → `type` ∈ {Goal, Card, subst, Var} with `detail`
  ∈ {Normal Goal, Own Goal, Penalty, Missed Penalty, Goal cancelled, Penalty confirmed, …},
  plus `time.elapsed` and `time.extra`. VAR events exist from the 2020-21 season onward.

Fixture↔game matching: date window + team names, resolved through a persisted alias table.
**Never fuzzy-match silently** — an unresolved fixture leaves `apifootball_fixture_id` NULL
with a reason, and the game is reported in the health strip.

Free tier is 100 requests/day, so ingest must be incremental and resumable at fixture
granularity.

---

## 5. The Clock — exact, or explicitly not

This is a stated hard requirement: the user asked for an exact clock and refused an
approximate one.

**Kalshi cannot provide it.** `expected_expiration_time` was verified 47 minutes wrong;
`close_time` is padded; markets close early when a winner is declared. Do not use them.

**The exact anchor is `fixture.periods`** from API-Football — `first` and `second` are unix
timestamps of when each half *actually* kicked off:

```python
def match_second(ts_us, periods_first, periods_second):
    t = ts_us / 1_000_000
    if periods_second is None or t < periods_second:
        return t - periods_first                      # first half
    return 45 * 60 + (t - periods_second)             # second half
```

Halftime falls out automatically. Because the referee's clock also does not stop for
in-play stoppages, this *matches* the official running clock rather than approximating it.

Derived: `minute = floor(match_second/60) + 1`; stoppage is `minute - 45` in H1 and
`minute - 90` in H2 when positive. "Minute 88 including 90+X" is then an exact predicate on
`match_second`.

Every game carries `clock_source`:

| Value | Meaning | Allowed in minute-cutoff work |
|---|---|---|
| `exact_periods` | both `periods` timestamps present | **yes** |
| `anchored_fit` | fitted from ordered Shocks vs ordered Goal minutes; ±30 s | **no** (opt-in only) |
| `none` | no provider data | no |

**Clock gate (invariant):** a Run or Sweep filtered on minute **must refuse to execute**
over games whose `clock_source` is not `exact_periods`, unless explicitly overridden, and
every result displays the excluded count. Studies keyed on `t_minus_last_print` need no
provider and are always available.

---

## 5A. Shape — what the move looks like, and what it costs to look

A Shock is not just a size and a time; it has a *shape*, and the user's question
is whether shape separates a real goal from one that is about to be chalked off
by VAR, or a penalty awarded but not yet taken. The existing bot already leans on
one shape fact — a sibling leg confirming with the opposite sign within ±50 ms —
so the platform's job is to **measure** that rather than inherit it as an
assumption.

**The discipline that keeps this honest.** Shape can only inform a decision if it
is observed *before* the decision. Observing costs time, and time costs price.
So Shape is not a separate study from latency — it is the same axis:

> waiting `shape_window_ms` buys you information and costs you entry price

Every Shape feature is computed strictly within `[detect_ts, detect_ts +
shape_window_ms]`, and the engine **must assert** `shape_window_ms <=
decision_ms` for any feature used in an entry rule. A feature measured after the
commit point is hindsight, not a feature. This is invariant 1 (no lookahead)
applied to Shape, and it gets its own test.

### The eight features

Columns on `shocks`. Eight, fixed, each independently meaningful in plain
English. **No classifier, no learned model, no embedding.** The point is that the
user can read any one of these and know what it means.

| Column | Type | Meaning |
|---|---|---|
| `legs_confirming` | UTINYINT | How many legs moved with the expected opposite sign (0–2). The sibling-confirmation idea, measured. |
| `confirm_lag_ms` | DOUBLE | Milliseconds from the first leg's move to the second leg's confirming move. NULL when nothing confirmed. On ATXSJ both legs moved inside one 100 ms bucket. |
| `sum_legs_drift` | DOUBLE | Maximum abs(sum of leg prices − 1) inside the window. How far the triplet stopped being coherent — a market-maker uncertainty proxy. |
| `path_efficiency` | DOUBLE | abs(net move) / sum of abs(tick-to-tick moves) on the target leg. 1.0 is a straight line; low is whipsaw. On ATXSJ, TIE ran 0.30→0.50→0.31→0.53→0.32 — that churn is the signal. |
| `direction_flips` | INTEGER | Sign changes in the target leg's path inside the window. |
| `frac_of_final_move` | DOUBLE | Share of the eventual total move already completed by the end of the window. **Uses post-window data and is therefore DIAGNOSTIC ONLY — it must never appear in an entry rule.** Flagged as such in code. |
| `size_in_window` | DECIMAL(18,2) | Contracts traded across all legs inside the window. Conviction proxy. |
| `taker_imbalance` | DOUBLE | abs(buy − sell) / total on the target leg, 0..1. One-way flow versus two-way disagreement. |

`shape_window_ms` is a parameter (default 500), swept like any other. Features are
recomputed per window rather than stored once, or the sweep is meaningless.

### What gets shown

One view, folded into Board band A — **not** a new page:

1. **Median price path by outcome class**, normalised to the pre-shock price and
   the eventual move, overlaid for `confirmed_goal` / `reversed` / `orphan`, with
   an interquartile band. This answers "what does a real goal look like versus one
   about to be overturned" by eye, with no model to distrust.
2. **The wait-versus-price curve**: for each `shape_window_ms` in the sweep, the
   mean entry price given up against the share of `reversed` Shocks avoided. This
   is the decision the user actually faces, stated as one line.

**Class sizes are displayed next to every Shape chart, always.** With roughly 480
Shocks in the whole dataset the `reversed` class is expected to be only 20–40
cases. That is enough to look at and form a hypothesis; it is **not** enough to
fit a decision rule on. Any Shape-conditioned entry rule must carry the same
event-clustered confidence interval as everything else, and the UI must never
draw a median path without saying how many Shocks are behind it.

### Vocabulary

**Shape** is the ninth permitted word, joining the eight in §2. It is concrete and
self-describing, which is why it earns an exception. No other word is added
without the same justification.

---

## 6. Storage

Parquet on Cloudflare R2, registered as DuckDB views; a materialised DuckDB file on the
Railway volume is the query engine. Reuse the SigV4 approach in `app/archive.py` (hand-
written over stdlib hmac/hashlib, no boto3) rather than adding a dependency.

Env: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`,
`LAB_DATA_DIR` (default `./lab_data`).

### Schema (authoritative DDL)

```sql
CREATE TABLE games (
  event_ticker            VARCHAR PRIMARY KEY,
  game_date               DATE    NOT NULL,
  home_abbr               VARCHAR NOT NULL,
  away_abbr               VARCHAR NOT NULL,
  home_name               VARCHAR,
  away_name               VARCHAR,
  home_team_uuid          VARCHAR,        -- Sportradar, joins custom_strike.soccer_team
  away_team_uuid          VARCHAR,
  milestone_id            VARCHAR,
  apifootball_fixture_id  BIGINT,
  unmatched_reason        VARCHAR,        -- why no fixture id; NULL when matched
  periods_first           BIGINT,         -- unix s, actual H1 kickoff
  periods_second          BIGINT,         -- unix s, actual H2 kickoff
  clock_source            VARCHAR NOT NULL,   -- exact_periods | anchored_fit | none
  close_time              TIMESTAMP,
  settlement_ts           TIMESTAMP,
  last_print_ts_us        BIGINT,         -- last print across all three legs
  final_home              INTEGER,
  final_away              INTEGER,
  winner                  VARCHAR,        -- HOME | AWAY | TIE
  tape_complete           BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE legs (
  ticker          VARCHAR PRIMARY KEY,
  event_ticker    VARCHAR NOT NULL,
  side            VARCHAR NOT NULL,       -- HOME | AWAY | TIE
  team_uuid       VARCHAR,
  result          VARCHAR,                -- yes | no | '' 
  volume          DECIMAL(18,2),
  open_interest   DECIMAL(18,2),
  fee_type        VARCHAR NOT NULL,
  fee_multiplier  DOUBLE  NOT NULL,
  print_count     BIGINT
);

-- ~29M rows. Partitioned month=YYYY-MM, sorted by ts_us within each file.
CREATE TABLE trades (
  trade_id           VARCHAR NOT NULL,
  ticker             VARCHAR NOT NULL,
  event_ticker       VARCHAR NOT NULL,
  ts_us              BIGINT  NOT NULL,    -- MICROSECONDS since epoch. Never rounded.
  yes_price_c        UTINYINT NOT NULL,   -- 1..99
  count              DECIMAL(18,2) NOT NULL,
  taker_side         VARCHAR NOT NULL,    -- yes | no
  taker_outcome_side VARCHAR,
  taker_book_side    VARCHAR,             -- bid | ask
  is_block_trade     BOOLEAN NOT NULL
);

CREATE TABLE candles_1m (
  ticker          VARCHAR NOT NULL,
  end_period_ts   BIGINT  NOT NULL,       -- unix s
  yes_bid_open    UTINYINT, yes_bid_high UTINYINT, yes_bid_low UTINYINT, yes_bid_close UTINYINT,
  yes_ask_open    UTINYINT, yes_ask_high UTINYINT, yes_ask_low UTINYINT, yes_ask_close UTINYINT,
  price_open      UTINYINT, price_high UTINYINT, price_low UTINYINT, price_close UTINYINT,
  price_mean      UTINYINT, price_previous UTINYINT,
  volume          DECIMAL(18,2),
  open_interest   DECIMAL(18,2),
  PRIMARY KEY (ticker, end_period_ts)
);

CREATE TABLE match_events (
  event_id       VARCHAR PRIMARY KEY,     -- deterministic hash, stable across re-ingest
  event_ticker   VARCHAR NOT NULL,
  team_side      VARCHAR,                 -- HOME | AWAY
  source         VARCHAR NOT NULL,        -- kalshi | apifootball
  kind           VARCHAR NOT NULL,        -- goal | own_goal | penalty | missed_penalty
                                          -- | yellow_card | red_card | var
  var_detail     VARCHAR,                 -- 'Goal cancelled' | 'Penalty confirmed' | ...
  player         VARCHAR,
  minute         INTEGER,                 -- label as published
  stoppage       INTEGER,
  match_second   BIGINT,                  -- via the Clock; NULL when clock_source='none'
  sort_order     INTEGER
);

CREATE TABLE state_changes (
  state_change_id VARCHAR PRIMARY KEY,
  event_ticker    VARCHAR NOT NULL,
  ordinal         INTEGER NOT NULL,
  from_state      VARCHAR NOT NULL,       -- DRAW | HOME_LEAD | AWAY_LEAD
  to_state        VARCHAR NOT NULL,
  scoring_side    VARCHAR NOT NULL,       -- HOME | AWAY
  is_equaliser    BOOLEAN NOT NULL,
  is_go_ahead     BOOLEAN NOT NULL,
  minute          INTEGER,
  stoppage        INTEGER,
  match_second    BIGINT
);

CREATE TABLE shocks (
  shock_id          VARCHAR PRIMARY KEY,
  event_ticker      VARCHAR NOT NULL,
  detect_ts_us      BIGINT  NOT NULL,     -- first print of the move
  match_second      BIGINT,               -- NULL unless clock_source='exact_periods'
  t_minus_last_print_s DOUBLE NOT NULL,   -- always available, never provider-dependent
  legs_moved        INTEGER NOT NULL,
  magnitude         DOUBLE  NOT NULL,     -- sum |delta log-odds|
  coherent          BOOLEAN NOT NULL,     -- >=2 legs, opposite signs
  reversed_frac     DOUBLE,
  label             VARCHAR NOT NULL,     -- confirmed_goal | reversed | orphan | unmatched
  state_change_id   VARCHAR,
  sum_legs_at_detect DOUBLE               -- diagnostic only; NOT a strategy
);

CREATE TABLE shock_legs (                 -- per-leg execution quality at shock time
  shock_id        VARCHAR NOT NULL,
  ticker          VARCHAR NOT NULL,
  side            VARCHAR NOT NULL,
  spread_before_c DOUBLE,                 -- from candles_1m
  price_before_c  UTINYINT,
  first_print_c   UTINYINT,               -- NULL if the leg never printed in the window
  first_print_lag_ms DOUBLE,
  prints_1s  INTEGER, prints_3s  INTEGER, prints_10s INTEGER,
  size_1s  DECIMAL(18,2), size_3s DECIMAL(18,2), size_10s DECIMAL(18,2),
  vwap_first_100_c DOUBLE,
  fee_per_contract DOUBLE,
  PRIMARY KEY (shock_id, ticker)
);
```

`entries`, `runs`, `run_configs`, `fills` are owned by Worker D and specified in
`lab/engine/SCHEMA.md`; they must follow the same rules (content-addressed config id,
immutable runs, every fill storing the `trade_id`s it consumed).

**Conventions.** Timestamps ending `_us` are integer microseconds; `_ts`/`_s` are unix
seconds; prices ending `_c` are cents 1..99. Money is `Decimal`, never float, until display.
Every table is rebuildable from R2; the DuckDB file is a cache, never the only copy.

---

## 7. Internal HTTP API

`lab/api/` serves these. Worker C builds the front end against a **mock** implementing
exactly this shape, so the front end never waits on ingest.

```
GET  /api/games?…filters            → paged Game rows + counts
GET  /api/games/{event_ticker}      → Game + legs + events + state changes + shocks
GET  /api/shocks?…filters           → paged Shock rows (+ shock_legs inline)
GET  /api/shocks/{shock_id}/tape?window_s=  → merged 3-leg tape for the Shock Card
GET  /api/entries?run_id=&…filters  → paged Entry rows
GET  /api/facets?…filters           → band-A distributions for the current filter
GET  /api/sweep/{sweep_id}          → heatmap cells: minute × leg config + CI bounds
POST /api/runs                      → start a Run; returns run_id
GET  /api/runs/{run_id}             → status, tearsheet statistics
GET  /api/health                    → coverage, clock_source mix, label agreement
POST /api/sql                       → read-only DuckDB query
```

**Every list endpoint takes the same filter object**, and it is the only filter vocabulary:

```json
{"minute_min": 88, "minute_max": null,
 "state_from": ["DRAW"], "state_to": ["HOME_LEAD","AWAY_LEAD"],
 "label": ["confirmed_goal"], "clock_source": ["exact_periods"],
 "price_min_c": null, "price_max_c": null,
 "date_from": null, "date_to": null,
 "leg_side": null, "event_ticker": null}
```

Responses always include `{"rows": [...], "total": N, "excluded": {"clock": N, "no_tape": N}}`.
**`excluded` is never omitted** — the user must always see what the filter removed.

`/api/sql` is read-only: a `query_only` DuckDB connection, statement timeout, hard row cap,
and it rejects anything that is not a single `SELECT`/`WITH`. It never interpolates user
text into SQL; the query is passed as-is to a read-only connection.

---

## 8. Invariants — each has a test

1. **No lookahead.** No decision may read a print whose `ts_us` ≥ the decision timestamp.
   Asserted in the fill path and unit-tested with a deliberately violating fixture.
2. **Fill traceability.** Every fill stores the `trade_id`s it consumed. A Run where >5% of
   fills cannot be traced **refuses to report P&L at all** — it reports the failure instead.
3. **Clock gate.** Minute-filtered Runs refuse to execute over non-`exact_periods` games;
   excluded counts are always displayed.
4. **Vocabulary gate.** No banned word appears in any user-visible string.
5. **Determinism.** Same inputs + same config ⇒ byte-identical output. File discovery order
   must not matter after sorting by `ts_us`.
6. **Never fabricate.** Missing data is `NULL` with a reason, surfaced in `/api/health`.

---

## 9. Front-end conventions

**No build step.** Plain ES modules, CDN-pinned at an exact version, matching repo
convention (CI runs `node --check`).

| Library | Licence | Use |
|---|---|---|
| `lightweight-charts` (TradingView) | Apache-2.0 | Shock Card price panes. Synced crosshair via `crosshairMove`. **Attribution link to TradingView is required and must be rendered.** |
| `@finos/perspective` + `perspective-workspace` | Apache-2.0 | Explore screen, bound to DuckDB |
| `@observablehq/plot` | ISC | Band-A small multiples |
| `codemirror` 6 | MIT | SQL editor |

Two screens only: **Board** and **Explore**. Data health is a status strip, not a page.

Board layout: pinned filter bar, then band A (what am I looking at) → band B (what would
have happened) → band C (the actual cases). Clicking any mark anywhere adds a chip to the
filter bar and re-filters every band. All filter state lives in the URL — every view is
bookmarkable. Every aggregate number drills to its rows. Keys: `j`/`k` move through rows,
`f` focuses the filter, `?` shows the eight words.

---

## 10. Validation every worker runs before handing back

```bash
python -X dev -m unittest discover -s tests -v     # existing 47 files must stay green
python -m compileall -q lab tests
ruff check --select E9,F63,F7,F82 lab tests
node --check lab_static/*.js
git diff --check
```

Plus your own `tests/test_lab_<area>.py`. A green suite is not sufficient: state what you
verified against real data, with counts.

**Known-answer test** (`KXMLSGAME-26SEP05ATXSJ`, equaliser at 90+7′) — the end-to-end
fixture every layer is checked against:

```
t-300.0s   SJ 0.98   TIE 0.03
t-299.5s   SJ 0.81   TIE 0.30    <- Shock starts here, both legs, same 100ms bucket
t-299.4s   SJ 0.63   TIE 0.50
t-295.8s   SJ 0.26   TIE 0.70
t-273.8s   SJ 0.01   TIE 0.81
```

Final score 1-1; `TIE` settles `yes`. The detector must find the Shock at the first print of
that move, label it `confirmed_goal`, and (given `exact_periods`) place it inside 90+7′.
