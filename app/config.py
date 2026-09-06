"""Football-Bot configuration. Everything overridable via environment variables."""
import hashlib
import json
import os


def _f(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _i(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _b(name, default):
    v = os.environ.get(name, "").lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


# --- Kalshi credentials (absent => DEMO mode) ---
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "")  # PEM string
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
# Bulletproof escape hatch: base64 of the whole .key file (no newline issues).
# Produce with:  base64 -w0 mykey.key   (or: python -c "import base64;print(base64.b64encode(open('mykey.key','rb').read()).decode())")
KALSHI_PRIVATE_KEY_B64 = os.environ.get("KALSHI_PRIVATE_KEY_B64", "")
KALSHI_REST = os.environ.get("KALSHI_REST", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS = os.environ.get("KALSHI_WS", "wss://api.elections.kalshi.com/trade-api/ws/v2")

MODE = os.environ.get("MODE", "auto")  # auto | live | demo
DATA_DIR = os.environ.get("DATA_DIR", "./data")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# --- Strategy (frozen Gate A primary unless overridden) ---
DL_MIN = _f("DL_MIN", 0.8)              # min log-odds displacement of the sweep
LEVELS_MIN = _i("LEVELS_MIN", 5)        # min distinct price levels in the sweep
SIZE_MIN = _f("SIZE_MIN", 200.0)        # min contracts in the sweep
CONF_MS = _i("CONF_MS", 50)             # sibling confirmation window (+- ms)
# How long to hold an unconfirmed candidate waiting for its sibling's frame to
# arrive.  This is transport patience, not a strategy window: `Detector.confirm`
# judges coherence on exchange timestamps (CONF_MS above), so waiting longer
# cannot admit a pair the exchange clock says was too far apart.  It was fixed
# at 200ms against an observed feed lag p95 of 0.9-1.1s, so siblings whose
# exchange stamps were within 50ms of each other were still dropped as
# unconfirmed purely because their frames arrived late.
CONF_WAIT_S = _f("CONF_WAIT_S", 2.0)
# Maximum wall-clock age of a candidate at confirmation time for it to be
# tradeable.  Waiting longer than this still records the confirmation, as
# `confirmed_late`, but does not enter: a coherent pair learned two seconds
# after the fact is evidence about the confirmation rate, not an opportunity,
# and entering on it would deepen an arrival latency that is already the
# study's largest execution problem.  The default preserves the previous
# trading behaviour exactly, so the longer wait is purely additive evidence.
CONF_TRADE_MAX_AGE_S = _f("CONF_TRADE_MAX_AGE_S", 0.2)
CONF_SIGN = _b("CONF_SIGN", True)       # sibling must move opposite sign (validated improvement)
PRICE_CAP = _f("PRICE_CAP", 58.0)       # max price paid (isotonic zero-crossing, late regime)
# Minimum executable price. Below this the entry is refused as `rejected_floor`,
# but the signal and its forward path are still recorded, exactly as
# `rejected_cap` handles the upper bound, so the evidence keeps accruing.
#
# Sub-35c entries were 41% of trades and 73% of all contract exposure, because a
# fixed dollar notional buys contracts as 1/price: $100 is ~727 contracts at
# 13.8c against ~176 at 57c. That bucket lost 22 of 27, and every one of the six
# with a recorded MFE never traded above entry even once. The hypothesis was
# stated from trades 1-61 and then reproduced out of sample on trades 83-89.
# Set to 0 to disable the floor entirely.
PRICE_FLOOR = _f("PRICE_FLOOR", 35.0)
NOTIONAL_USD = _f("NOTIONAL_USD", 100.0)
TARGET = _f("TARGET", 90.0)             # take-profit (YES-space for longs, mirrored for NO)
TIMEOUT_S = _i("TIMEOUT_S", 180)        # max hold before flattening
LOCKOUT_S = _i("LOCKOUT_S", 120)        # per-market re-entry lockout
EPISODE_COOLDOWN_S = _i("EPISODE_COOLDOWN_S", 5)
LATE_ONLY = _b("LATE_ONLY", False)      # if true, only trade within LATE_WINDOW_MIN of scheduled close
LATE_WINDOW_MIN = _i("LATE_WINDOW_MIN", 20)
USE_STOP = _b("USE_STOP", False)        # Gate A: stops off; shadow-stop is always recorded
STOP_FRAC = _f("STOP_FRAC", 0.35)
FEE_EXIT_TAKER = _b("FEE_EXIT_TAKER", True)  # charge taker fee on exits (conservative)

# --- Price-only late-score sleeve (paper-only; no score/event feed) ---
# ``off`` preserves Gate A exactly. ``enforce`` runs only the price-only sleeve.
# ``parallel`` independently paper-trades both Gate A and the price-only sleeve.
_price_only_sleeve_mode = os.environ.get("PRICE_ONLY_SLEEVE_MODE", "off").lower()
PRICE_ONLY_SLEEVE_MODE = (
    _price_only_sleeve_mode
    if _price_only_sleeve_mode in {"off", "enforce", "parallel"}
    else "off"
)
SLEEVE_START_BEFORE_EXPIRY_MIN = _f("SLEEVE_START_BEFORE_EXPIRY_MIN", 2.0)
SLEEVE_AFTER_EXPIRY_MIN = _f("SLEEVE_AFTER_EXPIRY_MIN", 12.0)
SLEEVE_BASELINE_MS = _f("SLEEVE_BASELINE_MS", 1800.0)
SLEEVE_MAX_BASELINE_AGE_MS = _f("SLEEVE_MAX_BASELINE_AGE_MS", 6000.0)
SLEEVE_TRIPLET_FRESH_MS = _f("SLEEVE_TRIPLET_FRESH_MS", 5000.0)
SLEEVE_MAX_SPREAD_C = _f("SLEEVE_MAX_SPREAD_C", 8.0)
SLEEVE_MIN_TEAM_GAIN_PP = _f("SLEEVE_MIN_TEAM_GAIN_PP", 0.15)
SLEEVE_MIN_DRAW_GAIN_PP = _f("SLEEVE_MIN_DRAW_GAIN_PP", 0.15)
SLEEVE_MIN_TEAM_POST = _f("SLEEVE_MIN_TEAM_POST", 0.45)
SLEEVE_MIN_DRAW_POST = _f("SLEEVE_MIN_DRAW_POST", 0.40)
SLEEVE_MAX_SIBLING_RISE_PP = _f("SLEEVE_MAX_SIBLING_RISE_PP", 0.02)
SLEEVE_MIN_EXPLAINED = _f("SLEEVE_MIN_EXPLAINED", 0.85)
SLEEVE_SCRATCH_ARM_C = _f("SLEEVE_SCRATCH_ARM_C", 4.0)
SLEEVE_SCRATCH_BUFFER_C = _f("SLEEVE_SCRATCH_BUFFER_C", 0.5)
SLEEVE_UNKNOWN_FEE_BUFFER_C = _f("SLEEVE_UNKNOWN_FEE_BUFFER_C", 2.0)
SLEEVE_TRAIL_ARM_C = _f("SLEEVE_TRAIL_ARM_C", 6.0)
SLEEVE_TRAIL_MIN_C = _f("SLEEVE_TRAIL_MIN_C", 2.0)
SLEEVE_TRAIL_FRAC = _f("SLEEVE_TRAIL_FRAC", 0.45)
SLEEVE_REVERSAL_C = _f("SLEEVE_REVERSAL_C", 2.0)
SLEEVE_OSCILLATION_WINDOW_S = _f("SLEEVE_OSCILLATION_WINDOW_S", 4.0)
SLEEVE_OSCILLATION_CROSSES = _i("SLEEVE_OSCILLATION_CROSSES", 3)
SLEEVE_MAX_OSCILLATION_EFFICIENCY = _f("SLEEVE_MAX_OSCILLATION_EFFICIENCY", 0.35)
SLEEVE_TIMEOUT_S = _f("SLEEVE_TIMEOUT_S", 30.0)

# --- Sub-threshold research capture (collection only; never trades) ---
# Bursts that clear this floor but not the Gate-A thresholds are recorded with
# outcome ``subthreshold``.  They are never confirmed, never dispatched to a
# sleeve, and never counted toward a kill-condition gate.  They exist so the
# detector thresholds can be re-fitted from the study database instead of only
# by replaying the raw feed.  These knobs are excluded from the strategy
# configuration identity: capturing an observation cannot change a decision.
SUBTHRESHOLD_CAPTURE = _b("SUBTHRESHOLD_CAPTURE", True)
SUBTHRESHOLD_DL_MIN = _f("SUBTHRESHOLD_DL_MIN", 0.3)
SUBTHRESHOLD_LEVELS_MIN = _i("SUBTHRESHOLD_LEVELS_MIN", 3)
SUBTHRESHOLD_SIZE_MIN = _f("SUBTHRESHOLD_SIZE_MIN", 50.0)
# Per-market rate limit.  Bounds the write rate to at most one observation per
# market per interval, whatever the trade rate.
SUBTHRESHOLD_COOLDOWN_S = _f("SUBTHRESHOLD_COOLDOWN_S", 5.0)

# --- Paper execution adapter (off preserves the original paper desk exactly) ---
PAPER_EXECUTION_V2 = _b("PAPER_EXECUTION_V2", False)
PAPER_ENTRY_LATENCY_MS = _f("PAPER_ENTRY_LATENCY_MS", 150.0)
PAPER_EXIT_LATENCY_MS = _f("PAPER_EXIT_LATENCY_MS", 150.0)
PAPER_EXECUTION_POLL_MS = _f("PAPER_EXECUTION_POLL_MS", 5.0)
# Maximum age, in milliseconds, of the order book a paper entry may fill
# against, measured from that book's arrival in this process to the fill.
#
# 0 means RECORD ONLY: the age is measured and persisted on every fill, and no
# entry is ever refused for it, so the default reproduces today's behaviour
# exactly. Above zero, an entry whose book is older than the bound is finalised
# as `stale_book` instead of filling.
#
# This is a strategy parameter, not an observability knob: raising it above
# zero refuses entries, so it changes `config_id` and rows written under
# different bounds must not pool.
#
# Why it exists: a raw L2 replay of 2026-09-04 20:00-22:00 (1.9 M frames, 78
# Gate-A candidates) found the reconstructed book's best ask worse than a real
# same-side executed price for 29 of 29 candidates (median +12c), with the
# process running 5.6 s median behind the exchange across 18 sequence gaps and
# 8 reconnects. An unaged fill price is therefore an unfalsifiable claim.
PAPER_MAX_BOOK_AGE_MS = _f("PAPER_MAX_BOOK_AGE_MS", 0.0)

# --- Read-only Kalshi goal/market latency observer ---
# This never participates in signal generation or paper execution.  It polls
# Kalshi's milestone live-data endpoint and timestamps score changes beside the
# already-received market stream so feed latency can be measured empirically.
GOAL_LATENCY_OBSERVER = _b("GOAL_LATENCY_OBSERVER", True)
GOAL_LATENCY_POLL_MS = _f("GOAL_LATENCY_POLL_MS", 250.0)
GOAL_LATENCY_LOOKBACK_S = _f("GOAL_LATENCY_LOOKBACK_S", 10.0)
GOAL_LATENCY_AFTER_S = _f("GOAL_LATENCY_AFTER_S", 2.0)
# Diagnostic +-seconds for associating a signal with the nearest same-match
# provider event.  An audit-window default, never an entry or exit input, and
# deliberately excluded from `STRATEGY_PARAM_NAMES` (it is listed under
# `exporter._OBSERVABILITY_NAMES` instead), so changing it does not move
# `config_id` and cannot re-partition the study.
#
# Was 20, guessed rather than measured, against a documented 18.635 s
# observation lag on the Al-Shabab case -- about 1.4 s of margin (see
# SPEC_CORRECTIONS C6, which asks for it to be set from data). Measured since:
# provider `occurence_ts` to first observation is p50 15 s, and goal
# observation minus bot entry is typically +12..+50 s, because the Kalshi score
# feed lands 10-40 s after the market moves. At 20 s most genuinely goal-driven
# trades therefore recorded `no_nearby_same_match_event`, which reads as "no
# goal" and is wrong.
#
# 90 covers the measured upper tail with margin. The cost is a looser label:
# association was already only a ground-truth label and never an explanation of
# any individual trade (C6), and a wider window admits more coincidental
# matches. That is the correct direction for a recall-limited measurement --
# the association is reported alongside `state_consistent` / `state_mismatch`,
# which is what separates a real match from a coincidence.
EVENT_MATCH_WINDOW_S = _f("EVENT_MATCH_WINDOW_S", 90.0)

# Forward price window recorded after every signal, accepted or declined, so a
# decline is a labelled observation rather than a dead record.  Collection only:
# nothing in the trading path reads it.
SIGNAL_PATH_WINDOW_S = _f("SIGNAL_PATH_WINDOW_S", 300.0)
SIGNAL_PATH_MAX_TRACKED = _i("SIGNAL_PATH_MAX_TRACKED", 400)
# Path sampling policy.  A flat 4,000-row cap is a budget spent in arrival
# order, so the busiest markets exhausted it first: samples=3999 on every La
# Liga trade and signal in the first live study, which collapsed the intended
# 300 s forward window to 60-130 s exactly where activity was highest and the
# answer mattered most.
#
# Time-based thinning instead: every change for the first PATH_THIN_AFTER_S
# after the anchor, where the reaction being studied happens, then at most one
# row per PATH_THIN_INTERVAL_MS.  A new peak or trough is ALWAYS recorded
# regardless of thinning, so the extremes the exit study reads are never the
# rows that get dropped.  `store.BID_PATH_MAX_SAMPLES` remains the hard
# backstop.
#
# Collection only: nothing in the trading path reads a persisted path, so these
# are deliberately not strategy parameters.
PATH_THIN_AFTER_S = _f("PATH_THIN_AFTER_S", 10.0)
PATH_THIN_INTERVAL_MS = _f("PATH_THIN_INTERVAL_MS", 250.0)
# Maximum age of a persisted match-clock confirmation used by the 88+ gate.
#
# Was 2500 ms, derived as ten 250 ms poll intervals. Live capture measured
# match_clock_age_ms at p50 6099 ms, so the bound rejected the great majority of
# candidates and the sleeve never admitted one: 12 `sleeve_clock_stale` refusals
# and 42 cumulative gate misses, against zero sleeve trades ever.
#
# The bound should be proportional to how fast the underlying signal changes,
# and a provider minute changes once per 60 s. Staleness is also directionally
# safe for this gate: match minute only increases, so a stale reading of minute
# M means the true minute is >= M, and `minute >= threshold` can therefore only
# refuse an eligible candidate, never admit an ineligible one. The residual risk
# it does carry is entering shortly after a final whistle the stale clock has
# not yet reflected, which is what bounds this at ten seconds rather than sixty.
MATCH_CLOCK_MAX_AGE_MS = _f("MATCH_CLOCK_MAX_AGE_MS", 10000.0)
# Match minute at which the price-only sleeve becomes eligible. Previously
# hard-coded as 88 inside the gate. Made configurable so it can be moved
# deliberately, and so it enters the strategy configuration fingerprint.
#
# 80, and deliberately *below* the best estimate of the optimum rather than at
# it. The Polymarket timing study put the shock inflection at minutes 86-90 on
# an inferred clock measured to run about 5 minutes fast, which back-calibrates
# to roughly 81-85 with several minutes of uncertainty either side. A floor set
# inside that band censors the data exactly where the answer lies: at 85 an
# optimum of 82 could never be observed, because nothing below 85 would ever
# fire. A floor at 80 puts the whole plausible band inside the sample, so the
# threshold can be fitted from this venue's own provider clock rather than from
# a cross-venue inference. On the most recent 500 provider observations it
# roughly doubles eligible clock coverage: 61 at minute >= 88, 125 at >= 80.
#
# This admits candidates the study suggests are worse than the latest ones.
# That is the intended cost: every fired trade records its `provider_minute`
# and its forward path, so the minute becomes a measured variable instead of a
# guess. `PRICE_FLOOR` bounds what the sample can cost, since entry price and
# not minute was the dominant loss driver.
SLEEVE_MIN_MINUTE = _i("SLEEVE_MIN_MINUTE", 80)
# How often to re-resolve event -> milestone mappings. This runs on its own
# task: it makes one sequential REST call per unmapped event, and doing that
# inside the clock poll loop blocked the refresh for seconds at a time.
CLOCK_MAPPING_INTERVAL_S = _f("CLOCK_MAPPING_INTERVAL_S", 15.0)

# How often the observer flushes the buffered `last_observed_ts` refreshes for
# provider events it has already recorded.  Writing one per already-seen event
# per poll was the dominant cost of the clock poll (5.7 s p50 against a 250 ms
# target, measured 2026-09-04).  Observability only: it cannot change a
# decision, so it is deliberately not a strategy parameter.
PROVIDER_EVENT_FLUSH_S = _f("PROVIDER_EVENT_FLUSH_S", 60.0)

# --- Raw feed archive (Cloudflare R2) ---------------------------------------
# The Railway volume is finite (4.08 GB, 4.00 GB used on 2026-09-05 with 176
# hourly segments = 2.94 GB) while the raw feed grows at ~300 MB/day.  The
# archive extends the SAME logical timeline onto object storage: a segment is
# uploaded, verified, and only then may its local copy be removed.  These are
# STORAGE knobs.  They are deliberately absent from `STRATEGY_PARAM_NAMES`,
# because moving a recorded file cannot change a trading decision.
RAW_ARCHIVE_ENABLED = _b("RAW_ARCHIVE_ENABLED", False)
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
# Secret.  Never logged, never exported, never returned by an API.
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "football-bot-raw-feed")
# Optional endpoint override.  Empty means the account's R2 endpoint; tests
# point it at a local stub so no test ever needs the network.
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
# A verified segment stays on the volume this long so recent replays are served
# from local disk; older ones are pruned to R2 (they remain downloadable).
RAW_LOCAL_RETENTION_HOURS = _i("RAW_LOCAL_RETENTION_HOURS", 48)
# Emergency floor: below this much free space on DATA_DIR, verified segments are
# pruned oldest-first regardless of retention.  The failure this prevents is
# silent write loss in the study database on a full volume.
RAW_ARCHIVE_MIN_FREE_MB = _i("RAW_ARCHIVE_MIN_FREE_MB", 512)
RAW_ARCHIVE_MAX_ATTEMPTS = _i("RAW_ARCHIVE_MAX_ATTEMPTS", 5)
RAW_ARCHIVE_INTERVAL_S = _f("RAW_ARCHIVE_INTERVAL_S", 60.0)


def r2_endpoint():
    """Base URL of the S3-compatible endpoint, or "" when unconfigured."""
    if R2_ENDPOINT:
        return R2_ENDPOINT.rstrip("/")
    if R2_ACCOUNT_ID:
        return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return ""


def raw_archive_ready():
    """True only when the archive is switched on AND fully credentialled.

    Fail closed: without this the feature is inert -- no uploads, no state
    writes, no deletions -- and the bot behaves exactly as it did before.
    """
    return bool(
        RAW_ARCHIVE_ENABLED and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY
        and R2_BUCKET and r2_endpoint()
    )


# --- WebSocket arrival queue bounds -----------------------------------------
# TRANSPORT knobs.  They are deliberately absent from `STRATEGY_PARAM_NAMES`:
# they decide which frames this process gets to see, not what it does with the
# ones it sees.  Gate A detection, confirmation, sizing, entry, exit, fee,
# lockout and settlement read none of them.
#
# The queue between the socket reader and the frame consumer was unbounded on
# purpose, so its depth would measure the processing backlog.  In production on
# 2026-09-05 that made a consumer stall silent and unbounded instead: the reader
# kept up (feed lag p50 63-148 ms) while the backlog reached 896,017 frames and
# settled near 556,000, `order_arrival_ms` reached p95 2,280,666 ms (38 minutes)
# and max 2,895,826 ms, and the process held 3.77 GB of an 8 GB limit at 0.12 of
# 8 vCPU.  The bot went on "entering" against 77-minute-old book state for
# matches that had already finished and booking the settlement that was queued
# behind it, which fabricated about +$281 of paper profit across trades 110-114.
#
# A bound cannot make a slow consumer fast.  What it can do is convert an
# invisible, unbounded loss of currency into an explicit, counted, ledgered loss
# of frames -- which is the honest failure, and the one the old single-coroutine
# design used to produce naturally when Kalshi dropped a backed-up socket.
WS_QUEUE_MAX = _i("WS_QUEUE_MAX", 20000)
# `oldest` drops from the head of the queue.  For trading, the newest market
# state is the only state worth having: a frame that has already waited behind
# 20,000 others cannot inform a decision, and processing it produces a fill
# price the exchange stopped offering minutes ago.  Anything else is normalised
# to `oldest` (see `ws_queue_drop_policy`) rather than silently disabling the
# bound.
WS_QUEUE_DROP_POLICY = os.environ.get("WS_QUEUE_DROP_POLICY", "oldest")
WS_QUEUE_DROP_POLICIES = ("oldest",)
# One overflow summary per this many seconds, so an overflow storm cannot itself
# become the bottleneck.  A newly affected market is disclosed immediately
# regardless: its book must be invalidated before the consumer can fill from it.
WS_QUEUE_OVERFLOW_REPORT_S = _f("WS_QUEUE_OVERFLOW_REPORT_S", 1.0)
# Stall guard.  A queue held above the high-water mark for this long is a
# consumer that is not going to recover on its own; a reconnect drains it and
# re-subscribes, which yields fresh snapshots.  Fresh snapshots are strictly
# better than a deep queue of stale deltas.
WS_QUEUE_STALL_S = _f("WS_QUEUE_STALL_S", 60.0)
# Depth the stall guard watches.  0 derives it as half of `WS_QUEUE_MAX`, and
# leaves the guard off when the queue is unbounded.
WS_QUEUE_STALL_DEPTH = _i("WS_QUEUE_STALL_DEPTH", 0)
WS_QUEUE_STALL_POLL_S = _f("WS_QUEUE_STALL_POLL_S", 1.0)
# Floor on the interval between two FORCED reconnects, so the guard cannot
# thrash a feed that reconnects into the same backlog.
WS_RECONNECT_MIN_INTERVAL_S = _f("WS_RECONNECT_MIN_INTERVAL_S", 120.0)
# How long the frame consumer may hold the event loop before yielding.  It
# replaces a fixed 16-frames-per-turn yield, which capped throughput at 16
# frames per event-loop turn regardless of how cheap a frame was or how much
# CPU was idle: at the measured scheduler lag (p50 19 ms, p95 165 ms) that is
# 97-800 frames/s, against ~19,000 frames/s of actual capacity.  5 ms is small
# beside that lag, so nothing else on the loop waits materially longer, and it
# is roughly 75 frames at the measured per-frame cost.  0 restores the old
# yield-after-every-frame behaviour.
WS_CONSUMER_SLICE_MS = _f("WS_CONSUMER_SLICE_MS", 5.0)
# Minimum interval before recovery re-asks for a market's snapshot.  Recovery
# used to send one `get_snapshot` per rejected delta and re-request every market
# on every gap; under load that made recovery the largest single load on the
# process it was recovering.
WS_SNAPSHOT_RETRY_S = _f("WS_SNAPSHOT_RETRY_S", 5.0)


def ws_queue_drop_policy():
    """The active drop policy, normalised.  Unknown values mean `oldest`.

    Failing closed here would mean an unbounded queue, which is the defect.
    """
    policy = (WS_QUEUE_DROP_POLICY or "").strip().lower()
    return policy if policy in WS_QUEUE_DROP_POLICIES else "oldest"


def ws_queue_stall_depth():
    """Effective stall high-water mark; 0 disables the guard."""
    if WS_QUEUE_STALL_DEPTH > 0:
        return WS_QUEUE_STALL_DEPTH
    return WS_QUEUE_MAX // 2 if WS_QUEUE_MAX > 0 else 0


# --- Market discovery ---
DISCOVERY_INTERVAL_S = _i("DISCOVERY_INTERVAL_S", 180)
SUBSCRIBE_BEFORE_CLOSE_MIN = _i("SUBSCRIBE_BEFORE_CLOSE_MIN", 150)  # watch markets closing within this
DROP_AFTER_CLOSE_MIN = _i("DROP_AFTER_CLOSE_MIN", 20)
SERIES_FILE = os.environ.get("SERIES_FILE", "")  # optional path to override the soccer series list

# --- Demo replay ---
DEMO_SPEED = _f("DEMO_SPEED", 25.0)     # x real time
DEMO_LOOP = _b("DEMO_LOOP", True)

# --- Dashboard ---
BROADCAST_COALESCE_MS = _i("BROADCAST_COALESCE_MS", 250)

# Soccer game series (from the Aug 2026 universe scan; league -> [series tickers])
SOCCER_SERIES = [
    "KXEPLGAME", "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME",
    "KXMLSGAME", "KXLIGAMXGAME", "KXLEAGUESCUPGAME", "KXBRASILEIROGAME", "KXBRASILEIROBGAME",
    "KXARGPREMDIVGAME", "KXNWSLGAME", "KXUCLGAME", "KXUELGAME", "KXUECLGAME", "KXCLUBFGAME",
    "KXALLSVENSKANGAME", "KXELITESERIENGAME", "KXDENSUPERLIGAGAME", "KXEREDIVISIEGAME",
    "KXLIGAPORTUGALGAME", "KXSCOTTISHPREMGAME", "KXEFLCHAMPIONSHIPGAME", "KXEFLCUPGAME",
    "KXSAUDIPLGAME", "KXCHNSLGAME", "KXJLEAGUEGAME", "KXKLEAGUEGAME", "KXECULPGAME",
    "KXPERLIGA1GAME", "KXCHLLDPGAME", "KXDIMAYORGAME", "KXURYPDGAME", "KXCOPADELREYGAME",
    "KXFACUPGAME", "KXDFBPOKALGAME", "KXCOPPAITALIAGAME", "KXCOUPEDEFRANCEGAME",
    "KXCONMEBOLLIBGAME", "KXCONMEBOLSUDGAME", "KXCONCACAFCCUPGAME", "KXBELGIANPLGAME",
    "KXLIGAEXPGAME", "KXBRASILEIROCGAME", "KXCZEFLGAME", "KXEKSTRAKLASAGAME",
]

LEAGUE_NAMES = {
    "KXEPLGAME": "English Premier League",
    "KXLALIGAGAME": "La Liga",
    "KXBUNDESLIGAGAME": "Bundesliga",
    "KXSERIEAGAME": "Serie A",
    "KXLIGUE1GAME": "Ligue 1",
    "KXMLSGAME": "Major League Soccer",
    "KXLIGAMXGAME": "Liga MX",
    "KXLEAGUESCUPGAME": "Leagues Cup",
    "KXBRASILEIROGAME": "Brazilian Serie A",
    "KXBRASILEIROBGAME": "Brazilian Serie B",
    "KXARGPREMDIVGAME": "Argentine Primera Division",
    "KXNWSLGAME": "National Women's Soccer League",
    "KXUCLGAME": "UEFA Champions League",
    "KXUELGAME": "UEFA Europa League",
    "KXUECLGAME": "UEFA Conference League",
    "KXCLUBFGAME": "FIFA Club World Cup",
    "KXALLSVENSKANGAME": "Allsvenskan",
    "KXELITESERIENGAME": "Eliteserien",
    "KXDENSUPERLIGAGAME": "Danish Superliga",
    "KXEREDIVISIEGAME": "Eredivisie",
    "KXLIGAPORTUGALGAME": "Primeira Liga",
    "KXSCOTTISHPREMGAME": "Scottish Premiership",
    "KXEFLCHAMPIONSHIPGAME": "EFL Championship",
    "KXEFLCUPGAME": "EFL Cup",
    "KXSAUDIPLGAME": "Saudi Pro League",
    "KXCHNSLGAME": "Chinese Super League",
    "KXJLEAGUEGAME": "J1 League",
    "KXKLEAGUEGAME": "K League 1",
    "KXECULPGAME": "Ecuadorian LigaPro",
    "KXPERLIGA1GAME": "Peruvian Liga 1",
    "KXCHLLDPGAME": "Chilean Primera Division",
    "KXDIMAYORGAME": "Colombian Primera A",
    "KXURYPDGAME": "Uruguayan Primera Division",
    "KXCOPADELREYGAME": "Copa del Rey",
    "KXFACUPGAME": "FA Cup",
    "KXDFBPOKALGAME": "DFB-Pokal",
    "KXCOPPAITALIAGAME": "Coppa Italia",
    "KXCOUPEDEFRANCEGAME": "Coupe de France",
    "KXCONMEBOLLIBGAME": "Copa Libertadores",
    "KXCONMEBOLSUDGAME": "Copa Sudamericana",
    "KXCONCACAFCCUPGAME": "CONCACAF Champions Cup",
    "KXBELGIANPLGAME": "Belgian Pro League",
    "KXLIGAEXPGAME": "Liga de Expansion MX",
    "KXBRASILEIROCGAME": "Brazilian Serie C",
    "KXCZEFLGAME": "Czech First League",
    "KXEKSTRAKLASAGAME": "Ekstraklasa",
}

# Per-league Gate A realized edge (cents/contract at 50ms) — dashboard prior; live re-ranks
LEAGUE_PRIOR = {
    "KXLIGAMXGAME": 27, "KXLEAGUESCUPGAME": 27, "KXCLUBFGAME": 18, "KXMLSGAME": 17,
    "KXBRASILEIROGAME": 13, "KXARGPREMDIVGAME": 12, "KXUECLGAME": 14, "KXEPLGAME": 4,
    "KXWCGAME": 0, "KXUCLGAME": 0, "KXUELGAME": 0, "KXCHNSLGAME": 0, "KXDIMAYORGAME": 0,
}


# --- Strategy configuration identity ---------------------------------------
# Every signal and trade is stamped with the identity of the configuration that
# produced it.  Without this, rows from different builds pool into one
# aggregate: the first live study reported a single net of -$609.02 that was
# really a pre-Aug-30 build at -$630.13 plus a later one at +$21.11, and no
# aggregate over that pool answered any question about either.
#
# The identity covers parameters AND code, because a strategy change is just as
# often a code edit as an environment change. `SOCCER_SERIES` is included: the
# traded universe is part of the configuration.  Read-only observability knobs
# (the goal-latency observer, diagnostic windows, forward-path capture) are
# excluded, since changing them cannot change a trading decision.
STRATEGY_PARAM_NAMES = (
    "DL_MIN", "LEVELS_MIN", "SIZE_MIN", "CONF_MS", "CONF_SIGN",
    "PRICE_CAP", "NOTIONAL_USD", "TARGET", "TIMEOUT_S", "LOCKOUT_S",
    "EPISODE_COOLDOWN_S", "LATE_ONLY", "LATE_WINDOW_MIN", "USE_STOP",
    "STOP_FRAC", "FEE_EXIT_TAKER", "PRICE_ONLY_SLEEVE_MODE",
    "SLEEVE_START_BEFORE_EXPIRY_MIN", "SLEEVE_AFTER_EXPIRY_MIN",
    "SLEEVE_BASELINE_MS", "SLEEVE_MAX_BASELINE_AGE_MS",
    "SLEEVE_TRIPLET_FRESH_MS", "SLEEVE_MAX_SPREAD_C",
    "SLEEVE_MIN_TEAM_GAIN_PP", "SLEEVE_MIN_DRAW_GAIN_PP",
    "SLEEVE_MIN_TEAM_POST", "SLEEVE_MIN_DRAW_POST",
    "SLEEVE_MAX_SIBLING_RISE_PP", "SLEEVE_MIN_EXPLAINED",
    "SLEEVE_SCRATCH_ARM_C", "SLEEVE_SCRATCH_BUFFER_C",
    "SLEEVE_UNKNOWN_FEE_BUFFER_C", "SLEEVE_TRAIL_ARM_C", "SLEEVE_TRAIL_MIN_C",
    "SLEEVE_TRAIL_FRAC", "SLEEVE_REVERSAL_C", "SLEEVE_OSCILLATION_WINDOW_S",
    "SLEEVE_OSCILLATION_CROSSES", "SLEEVE_MAX_OSCILLATION_EFFICIENCY",
    "SLEEVE_TIMEOUT_S", "PAPER_EXECUTION_V2", "PAPER_ENTRY_LATENCY_MS",
    "PAPER_EXIT_LATENCY_MS", "PAPER_EXECUTION_POLL_MS",
    "MATCH_CLOCK_MAX_AGE_MS", "SLEEVE_MIN_MINUTE", "PRICE_FLOOR",
    # Above zero this refuses entries (`stale_book`), so it is a strategy
    # parameter and a change to it is a new configuration identity.
    "PAPER_MAX_BOOK_AGE_MS",
    "SOCCER_SERIES",
)

# Source files whose contents decide what is traded and how it is filled.
_STRATEGY_SOURCES = (
    "books.py", "config.py", "detector.py", "engine.py", "execution.py",
    "late_score_sleeve.py", "match_clock.py", "paper.py",
)
_UNREADABLE_SOURCE = "unreadable"


def strategy_params():
    """The parameter set that defines one strategy configuration."""
    return {name.lower(): globals()[name] for name in STRATEGY_PARAM_NAMES}


def _compute_code_fingerprint():
    """Hash the strategy-critical sources so a code edit changes the identity.

    A configuration that is identical in parameters but different in code is a
    different configuration.  Hashing file contents keeps this self-contained:
    no git metadata is needed, and the image need not ship a repository.
    """
    digest = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in _STRATEGY_SOURCES:
        digest.update(name.encode("utf-8"))
        try:
            with open(os.path.join(here, name), "rb") as handle:
                digest.update(handle.read())
        except OSError:
            # Never fail startup over provenance; record the gap instead of
            # claiming a fingerprint that did not read every source.
            digest.update(_UNREADABLE_SOURCE.encode("utf-8"))
    return digest.hexdigest()[:12]


# Sources cannot change under a running process, so this is computed once.
CODE_FINGERPRINT = _compute_code_fingerprint()


def config_id():
    """Stable content address for the active parameters and code."""
    blob = json.dumps(strategy_params(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8"))
    digest.update(CODE_FINGERPRINT.encode("utf-8"))
    return digest.hexdigest()[:16]


def config_record():
    """The full, self-describing record behind `config_id()`."""
    return {
        "config_id": config_id(),
        "code_fingerprint": CODE_FINGERPRINT,
        "params": strategy_params(),
    }


def has_credentials():
    return bool(KALSHI_API_KEY_ID) and bool(
        KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_B64)


def mode():
    if MODE in ("live", "demo"):
        return MODE
    return "live" if has_credentials() else "demo"
