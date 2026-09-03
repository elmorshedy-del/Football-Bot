"""SQLite persistence + aggregate stats (event-clustered bootstrap, kill conditions)."""
import asyncio
import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config
from . import match_clock
from . import match_events
from .match_events import normalize_match_event

# The single writer.  Every INSERT/UPDATE/DELETE and the collector's own reads
# go through `_conn` under `_lock`, which serialises writes and preserves the
# careful per-write transactions below.  Dashboard/API reads do NOT take this
# lock: they run on their own read-only connections (see `_reader`) so a slow
# analytics scan can never stall a live collector write, and vice versa.  WAL
# (enabled in `init`) lets those readers see a consistent snapshot concurrently
# with the writer.
_lock = threading.Lock()
_conn = None

# Per-thread read-only connection pool.  API reads are dispatched onto worker
# threads (see `read`) so they never block the event loop; each worker keeps one
# read-only connection, recycled when the database is (re)initialised.
_read_state = threading.local()
_db_generation = 0

# When true on the current thread, `q` uses a read-only connection instead of
# the writer.  Set only inside `read`, so the collector/event-loop path is
# byte-for-byte unchanged and only dispatched API reads are isolated.
_read_ctx = threading.local()

# Deterministic aggregate cache.  `stats` recomputes an event-clustered
# bootstrap over the whole study on every call; the dashboard, the WebSocket
# hello and the 5s broadcast all ask for it, so multiple tabs plus a reconnect
# storm used to recompute the same numbers many times a second.  The cache is
# keyed on the writer's total change count, so it is returned only while the
# underlying rows are unchanged and is invalidated the instant anything is
# written -- it can never serve a stale study.
_stats_cache = {}

# Lightweight read timing so the API layer can report SQLite wait+exec time
# separately from Python transform and serialisation time (see main._perf).
_perf_lock = threading.Lock()
_read_perf = {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "recent_ms": []}

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  ticker TEXT PRIMARY KEY, event TEXT, series TEXT, title TEXT,
  close_time TEXT, status TEXT, added_ts REAL, display_game TEXT, display_leg TEXT);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, local_ts REAL,
  market TEXT, event TEXT, series TEXT, dir INTEGER, dl REAL, levels INTEGER,
  size REAL, ref REAL, ext REAL, conf_lag_ms REAL, late INTEGER,
  outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, market TEXT,
  event TEXT, series TEXT, dir INTEGER, side TEXT,
  entry_ts REAL, entry_px REAL, size REAL, cap REAL, notional REAL,
  exit_ts REAL, exit_px REAL, exit_reason TEXT,
  gross REAL, fees REAL, net REAL, mae REAL, shadow_stop_px REAL,
  book_at_entry TEXT, status TEXT DEFAULT 'open',
  remaining REAL, realized_gross REAL DEFAULT 0, accrued_fees REAL DEFAULT 0,
  exit_qty REAL DEFAULT 0, exit_vwap_num REAL DEFAULT 0,
  fee_type TEXT, fee_multiplier REAL, strategy TEXT,
  max_executable_bid REAL, max_executable_bid_ts REAL, mfe_c REAL);
-- stats() joins every signal to its trade (signals LEFT JOIN trades ON
-- t.signal_id=s.id) and /api/signals and /api/trades look trades up by
-- signal_id.  Without this index that join is O(signals x trades) -- a full
-- trades scan per signal -- so an ordinary stats refresh degraded quadratically
-- as the study grew.  status scoping backs the open/closed splits.
CREATE INDEX IF NOT EXISTS idx_trades_signal_id ON trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS paper_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, signal_id INTEGER,
  ts REAL, leg TEXT, side TEXT, price REAL, quantity REAL, notional REAL,
  fee REAL, reason TEXT, mode TEXT);
-- Open-position recovery and fill integrity sum fees per trade_id; without this
-- each was a full paper_fills scan.
CREATE INDEX IF NOT EXISTS idx_paper_fills_trade ON paper_fills(trade_id);
CREATE TABLE IF NOT EXISTS latency(
  ts REAL, kind TEXT, ms REAL);
-- Readiness summaries and /api/latency both fetch the newest rows per kind
-- (WHERE kind IN (...) ORDER BY ts DESC LIMIT n).  latency grows fast (a
-- feed_lag row per frame), so without this index every summary full-scanned the
-- whole table -- eight scans per status() call, on the event loop.
CREATE INDEX IF NOT EXISTS idx_latency_kind_ts ON latency(kind, ts);
CREATE TABLE IF NOT EXISTS eventlog(
  ts REAL, kind TEXT, text TEXT);
-- Content-addressed strategy configurations.  Every signal and trade carries a
-- config_id, and this table is what makes that id self-describing inside an
-- export: parameters plus the code fingerprint that produced them.  Rows are
-- immutable; a changed threshold or a changed strategy source is a new id, not
-- an edit to an existing one.
CREATE TABLE IF NOT EXISTS config_versions(
  config_id TEXT PRIMARY KEY, first_seen_ts REAL NOT NULL,
  code_fingerprint TEXT NOT NULL, params TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goal_latency_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_ts REAL NOT NULL,
  event TEXT NOT NULL,
  milestone_id TEXT NOT NULL,
  change_kind TEXT NOT NULL,
  live_type TEXT,
  score_before TEXT NOT NULL,
  score_after TEXT NOT NULL,
  previous_poll_ts REAL,
  poll_started_ts REAL NOT NULL,
  response_ms REAL NOT NULL,
  last_book_change_ts REAL,
  last_book_lead_ms REAL,
  last_trade_ts REAL,
  last_trade_lead_ms REAL,
  first_book_after_ts REAL,
  first_book_after_ms REAL,
  first_trade_after_ts REAL,
  first_trade_after_ms REAL,
  canonical_type TEXT,
  canonical_side TEXT,
  normalized_event TEXT,
  detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match_clock_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_ts REAL NOT NULL,
  poll_started_ts REAL NOT NULL,
  previous_poll_ts REAL,
  response_ms REAL NOT NULL,
  event TEXT NOT NULL,
  milestone_id TEXT NOT NULL,
  provider_period TEXT,
  provider_minute INTEGER,
  provider_stoppage INTEGER,
  provider_clock TEXT,
  provider_status TEXT,
  precision TEXT NOT NULL,
  raw_context TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_match_clock_event_ts
  ON match_clock_observations(event, observed_ts);
CREATE TABLE IF NOT EXISTS provider_match_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_ts REAL NOT NULL,
  first_observed_ts REAL NOT NULL,
  last_observed_ts REAL NOT NULL,
  poll_started_ts REAL NOT NULL,
  previous_poll_ts REAL,
  response_ms REAL NOT NULL,
  event TEXT NOT NULL,
  milestone_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  previous_fingerprint TEXT,
  canonical_type TEXT NOT NULL,
  canonical_side TEXT,
  provider_period TEXT,
  provider_minute INTEGER,
  provider_stoppage INTEGER,
  provider_clock TEXT,
  normalized_event TEXT NOT NULL,
  raw_payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_provider_events_event_ts
  ON provider_match_events(event, observed_ts);
-- The mode-scoped unique fingerprint index is created in init() after the
-- `mode` column migration has run, since it references that column.
"""


_mode = "demo"

# Content address of the configuration that produced the rows this process
# writes.  None until `init()` registers one; rows written without it keep a
# NULL config_id rather than borrowing another configuration's identity.
_config_id = None

# Rows written before capture modes existed carry a NULL mode.  Their
# provenance is unknown, not disposable: they are preserved and presented under
# this label, and never silently counted as live.
LEGACY_MODE = "legacy_unknown"


def mode_clause(alias="", selector=None):
    """Return (sql, params) scoping a query to one capture mode.

    `selector` defaults to the active mode.  "all" disables scoping; the
    legacy label maps to SQL NULL, which is never included in live.
    """
    prefix = f"{alias}." if alias else ""
    selector = _mode if selector is None else selector
    if selector == "all":
        return "", ()
    if selector == LEGACY_MODE:
        return f" AND {prefix}mode IS NULL", ()
    return f" AND {prefix}mode=?", (selector,)


def present_mode(value):
    """Label a stored mode for the API/export semantic layer."""
    return value if value else LEGACY_MODE


# The only selectors an API caller may name.  Anything else is refused rather
# than silently widened, so a typo cannot quietly mix evidence modes.
SAFE_MODE_SELECTORS = ("live", "demo", LEGACY_MODE, "all")


def resolve_mode_selector(value=None):
    """Validate a caller-supplied mode selector; default to the active mode."""
    if value is None:
        return _mode
    selector = str(value).strip().lower()
    if selector not in SAFE_MODE_SELECTORS:
        raise ValueError(
            f"unknown mode selector {value!r};"
            f" expected one of {', '.join(SAFE_MODE_SELECTORS)}"
        )
    return selector


def row_exists_in_mode(table, row_id, mode=None):
    """True when `row_id` exists in `table` within the requested mode.

    Path access is authorised through the parent row so a caller scoped to one
    mode cannot fetch another mode's samples by guessing an id.
    """
    if table not in {"trades", "signals"}:
        raise ValueError(f"unsupported parent table {table!r}")
    scope, scope_args = mode_clause(selector=mode)
    return bool(q(
        f"SELECT 1 FROM {table} WHERE id=?{scope} LIMIT 1",
        (row_id, *scope_args),
    ))


def init():
    global _conn
    os.makedirs(config.DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(database_path(), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    # migrate: add mode column to older DBs (persisted on a volume)
    for tbl in ("signals", "trades", "match_clock_observations",
                "provider_match_events", "goal_latency_observations",
                "latency", "paper_fills"):
        try:
            _conn.execute(f"ALTER TABLE {tbl} ADD COLUMN mode TEXT")
        except sqlite3.OperationalError:
            pass  # already exists, or the table is created later in init
    for column, definition in (
        ("remaining", "REAL"),
        ("realized_gross", "REAL DEFAULT 0"),
        ("accrued_fees", "REAL DEFAULT 0"),
        ("exit_qty", "REAL DEFAULT 0"),
        ("exit_vwap_num", "REAL DEFAULT 0"),
        ("fee_type", "TEXT"),
        ("fee_multiplier", "REAL"),
        ("strategy", "TEXT"),
        ("max_executable_bid", "REAL"),
        ("max_executable_bid_ts", "REAL"),
        ("bid_path_summary", "TEXT"),
        ("mfe_c", "REAL"),
    ):
        try:
            _conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass  # already exists
    for column in ("display_game", "display_leg"):
        try:
            _conn.execute(f"ALTER TABLE markets ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass
    # Every new clock row records the exact source behind its stamp.  Legacy
    # rows keep a null source and are presented as legacy_unknown; they are
    # never relabeled as the current provider.
    for column, definition in (
        ("source", "TEXT"),
        ("confirmed_ts", "REAL"),
        ("confirmation_previous_poll_ts", "REAL"),
    ):
        try:
            _conn.execute(
                f"ALTER TABLE match_clock_observations ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError:
            pass  # already exists
    for column, definition in (
        ("canonical_type", "TEXT"),
        ("canonical_side", "TEXT"),
        ("normalized_event", "TEXT"),
    ):
        try:
            _conn.execute(
                f"ALTER TABLE goal_latency_observations ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError:
            pass
    # Normalized provider occurrence. The raw payload is preserved untouched;
    # these columns record what was resolved from it, from where, and why not.
    for column, definition in (
        ("provider_occurrence_ts", "REAL"),
        ("provider_occurrence_source", "TEXT"),
        ("provider_occurrence_unavailable_reason", "TEXT"),
    ):
        try:
            _conn.execute(
                f"ALTER TABLE provider_match_events ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError:
            pass
    for column in ("match_clock_snapshot", "forward_path_summary",
                   "path_incomplete_reason"):
        try:
            _conn.execute(f"ALTER TABLE signals ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass
    # Configuration provenance.  Rows written before this existed keep a NULL
    # config_id: their configuration is unknown, not assumed to be the current
    # one, so they are never pooled into a current-configuration aggregate.
    for table in ("signals", "trades"):
        try:
            _conn.execute(f"ALTER TABLE {table} ADD COLUMN config_id TEXT")
        except sqlite3.OperationalError:
            pass
    # Durable finalization marker for a signal's forward path.  Without it a
    # restart cannot tell a completed watch from one that died mid-window.
    try:
        _conn.execute("ALTER TABLE signals ADD COLUMN forward_path_finalized REAL")
    except sqlite3.OperationalError:
        pass
    # Written in the same signal INSERT transaction when forward-path capture is
    # enabled.  A started-but-unfinalized watch is recoverable even when the
    # process died before its first quote row reached SQLite.
    try:
        _conn.execute("ALTER TABLE signals ADD COLUMN forward_path_started_ts REAL")
    except sqlite3.OperationalError:
        pass
    # Provider duplicate identity is mode-scoped.  The old (event,fingerprint)
    # unique index made a live observation collide with a demo one and silently
    # refresh it instead of recording it.  Replacing the index deletes no row
    # and rewrites no mode; the new index is strictly more permissive, so it
    # cannot fail against data the old one already accepted.
    try:
        _conn.execute("DROP INDEX IF EXISTS idx_provider_events_fingerprint")
        _conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_events_fingerprint_mode
                 ON provider_match_events(
                     event, fingerprint, COALESCE(mode, 'legacy_unknown'))"""
        )
    except sqlite3.OperationalError:
        pass
    _conn.executescript(
        """CREATE TABLE IF NOT EXISTS bid_path_samples(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             kind TEXT NOT NULL,
             trade_id INTEGER,
             signal_id INTEGER,
             event TEXT,
             market TEXT,
             side TEXT,
             strategy TEXT,
             anchor_ts REAL NOT NULL,
             dt_ms REAL NOT NULL,
             bid REAL,
             bid_size REAL,
             exec_px REAL,
             qty REAL,
             mode TEXT);
           CREATE INDEX IF NOT EXISTS idx_bid_path_trade
             ON bid_path_samples(trade_id, dt_ms);
           CREATE INDEX IF NOT EXISTS idx_bid_path_signal
             ON bid_path_samples(signal_id, dt_ms);
           CREATE INDEX IF NOT EXISTS idx_bid_path_kind
             ON bid_path_samples(kind, anchor_ts);"""
    )
    # Availability and sequence metadata. Nullable for backward compatibility:
    # legacy rows keep a null sample_seq and are left untouched by the partial
    # unique indexes below, which make new rows exactly-once under retry.
    for column, definition in (
        ("sample_seq", "INTEGER"),
        ("availability", "TEXT"),
        ("terminal", "INTEGER"),
    ):
        try:
            _conn.execute(
                f"ALTER TABLE bid_path_samples ADD COLUMN {column} {definition}"
            )
        except sqlite3.OperationalError:
            pass  # already exists
    _conn.executescript(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_bid_path_trade_seq
             ON bid_path_samples(trade_id, kind, sample_seq)
           WHERE trade_id IS NOT NULL AND sample_seq IS NOT NULL;
           CREATE UNIQUE INDEX IF NOT EXISTS idx_bid_path_signal_seq
             ON bid_path_samples(signal_id, kind, sample_seq)
           WHERE signal_id IS NOT NULL AND sample_seq IS NOT NULL;"""
    )
    _conn.executescript(
        """CREATE TABLE IF NOT EXISTS match_clock_observations(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             observed_ts REAL NOT NULL,
             poll_started_ts REAL NOT NULL,
             previous_poll_ts REAL,
             response_ms REAL NOT NULL,
             event TEXT NOT NULL,
             milestone_id TEXT NOT NULL,
             provider_period TEXT,
             provider_minute INTEGER,
             provider_stoppage INTEGER,
             provider_clock TEXT,
             provider_status TEXT,
             precision TEXT NOT NULL,
             raw_context TEXT NOT NULL);
           CREATE INDEX IF NOT EXISTS idx_match_clock_event_ts
             ON match_clock_observations(event, observed_ts);
           CREATE TABLE IF NOT EXISTS provider_match_events(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             observed_ts REAL NOT NULL,
             first_observed_ts REAL NOT NULL,
             last_observed_ts REAL NOT NULL,
             poll_started_ts REAL NOT NULL,
             previous_poll_ts REAL,
             response_ms REAL NOT NULL,
             event TEXT NOT NULL,
             milestone_id TEXT NOT NULL,
             fingerprint TEXT NOT NULL,
             previous_fingerprint TEXT,
             canonical_type TEXT NOT NULL,
             canonical_side TEXT,
             provider_period TEXT,
             provider_minute INTEGER,
             provider_stoppage INTEGER,
             provider_clock TEXT,
             normalized_event TEXT NOT NULL,
             raw_payload TEXT NOT NULL);
           CREATE INDEX IF NOT EXISTS idx_provider_events_event_ts
             ON provider_match_events(event, observed_ts);
           CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_events_fingerprint_mode
             ON provider_match_events(
                 event, fingerprint, COALESCE(mode, 'legacy_unknown'));"""
    )
    _conn.commit()
    # A fresh database (new process, or a test's temp dir) invalidates every
    # pooled read connection and every cached aggregate.  Bumping the generation
    # makes each worker lazily reopen its read connection against the new file
    # the next time it reads.
    global _db_generation
    _db_generation += 1
    _stats_cache.clear()
    register_config_version()


def set_mode(m):
    global _mode
    _mode = m


def register_config_version(record=None):
    """Record the active configuration and make it the stamp for new rows.

    Idempotent: re-registering an id keeps its original `first_seen_ts`, so a
    restart under an unchanged configuration does not look like a new one.
    """
    global _config_id
    record = config.config_record() if record is None else record
    _config_id = record["config_id"]
    try:
        ex("""INSERT INTO config_versions(config_id,first_seen_ts,code_fingerprint,params)
              VALUES(?,?,?,?) ON CONFLICT(config_id) DO NOTHING""",
           (_config_id, time.time(), record["code_fingerprint"],
            json.dumps(record["params"], sort_keys=True)))
    except sqlite3.Error:
        # Provenance must never take down collection.  The id is still stamped
        # on rows; only the self-describing record is missing.
        pass
    return _config_id


def current_config_id():
    return _config_id


def purge_non_live():
    """Clear the operator event log on a live boot.

    This used to DELETE every non-live row from the study tables so live stats
    would start clean.  That destroyed demo and legacy evidence permanently,
    and deleted newly written null-mode rows on the next restart.  Isolation is
    now a query concern: every study read scopes to a capture mode (see
    `mode_clause`), so no observation has to be deleted to keep live clean.

    Only the operator event log and firehose-era junk signals -- rows that
    reference no registered market and are not study evidence -- are removed.
    """
    with _lock:
        # Firehose-era junk: signals on markets that were never registered.
        _conn.execute("DELETE FROM signals WHERE event='?'")
        _conn.execute("DELETE FROM eventlog")
        _conn.commit()


def ex(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _reader():
    """Return this thread's read-only connection, opening it if needed.

    Each worker thread keeps its own connection so reads run concurrently under
    WAL without taking the writer's lock.  `isolation_level=None` keeps every
    SELECT in autocommit, so a read never pins an old WAL snapshot or blocks the
    writer's checkpoint; `query_only` makes an accidental write fail loudly
    rather than corrupt the study.
    """
    entry = getattr(_read_state, "entry", None)
    if entry is not None:
        conn, generation = entry
        if generation == _db_generation:
            return conn
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conn = sqlite3.connect(
        database_path(), check_same_thread=False, timeout=30.0, isolation_level=None,
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA query_only=ON")
    _read_state.entry = (conn, _db_generation)
    return conn


def _record_read_perf(elapsed_ms):
    with _perf_lock:
        _read_perf["count"] += 1
        _read_perf["total_ms"] += elapsed_ms
        _read_perf["max_ms"] = max(_read_perf["max_ms"], elapsed_ms)
        recent = _read_perf["recent_ms"]
        recent.append(round(elapsed_ms, 3))
        if len(recent) > 200:
            del recent[0]


def q(sql, args=()):
    """Read rows as dicts.

    On the collector/event-loop path this uses the writer connection under
    `_lock`, exactly as before.  Inside a dispatched API read (`read`), it uses a
    lock-free read-only connection so dashboard scans never contend with live
    writes.
    """
    if getattr(_read_ctx, "enabled", False):
        started = time.perf_counter()
        cur = _reader().execute(sql, args)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        _record_read_perf((time.perf_counter() - started) * 1000.0)
        return rows
    with _lock:
        cur = _conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@contextmanager
def _read_only():
    prev = getattr(_read_ctx, "enabled", False)
    _read_ctx.enabled = True
    try:
        yield
    finally:
        _read_ctx.enabled = prev


async def read(fn, *args, **kwargs):
    """Run a read-only DB function off the event loop on a read connection.

    This is the seam that keeps the async application responsive: heavy dashboard
    queries and aggregates run on a worker thread against a read-only connection,
    so the event loop stays free to serve lightweight endpoints and the live
    WebSocket even while a multi-second scan is in flight.
    """
    def runner():
        with _read_only():
            return fn(*args, **kwargs)

    return await asyncio.to_thread(runner)


def read_perf():
    """Snapshot of read-connection timing for the operational perf endpoint."""
    with _perf_lock:
        count = _read_perf["count"]
        recent = sorted(_read_perf["recent_ms"])
        return {
            "reads": count,
            "avg_ms": round(_read_perf["total_ms"] / count, 3) if count else 0.0,
            "max_ms": round(_read_perf["max_ms"], 3),
            "p95_ms": _percentile(recent, 0.95) if recent else None,
            "recent_sample": len(recent),
        }


def database_health():
    """Return a non-mutating SQLite connectivity check for the status panel."""
    try:
        with _lock:
            if _conn is None:
                return {"healthy": False, "status": "not_initialized"}
            _conn.execute("SELECT 1").fetchone()
        return {"healthy": True, "status": "connected"}
    except Exception as exc:  # noqa: BLE001 - health must report, never mask the API
        return {
            "healthy": False,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def database_path():
    return os.path.join(config.DATA_DIR, "footballbot.db")


def backup_database(path, pages=0, sleep=0.0, progress=None):
    """Create a transactionally consistent SQLite snapshot at ``path``.

    The page copy runs on its own read-only source connection and does NOT hold
    ``_lock``.  Holding it for the whole backup blocked every event-loop caller
    that takes the same lock -- ``database_health()`` and ``stats()`` among them
    -- so a snapshot stalled live collection for its full duration even when the
    backup itself ran in a worker thread.  ``_lock`` is now held only long
    enough to confirm the database is initialised and read its path.
    """
    with _lock:
        if _conn is None:
            raise RuntimeError("database is not initialized")
        source_path = database_path()
    source = sqlite3.connect(source_path)
    try:
        destination = sqlite3.connect(path)
        try:
            source.backup(
                destination, pages=pages, sleep=sleep, progress=progress,
            )
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()


def log_event(kind, text):
    ex("INSERT INTO eventlog(ts,kind,text) VALUES(?,?,?)", (time.time(), kind, text))


LATENCY_KIND_CANONICAL = {
    "feed_lag": "feed_ingress_ms",
    "paper_entry": "paper_entry_ms",
    "order_arrival": "order_arrival_ms",
    "paper_exit": "paper_exit_ms",
}
LATENCY_KIND_ALIASES = {
    "feed_ingress_ms": ("feed_ingress_ms", "feed_lag"),
    "decision_ms": ("decision_ms",),
    "paper_entry_ms": ("paper_entry_ms", "paper_entry"),
    "order_arrival_ms": ("order_arrival_ms", "order_arrival"),
    "paper_exit_ms": ("paper_exit_ms", "paper_exit"),
    "match_response_ms": ("match_response_ms",),
    "match_clock_age_ms": ("match_clock_age_ms",),
    "scheduler_lag_ms": ("scheduler_lag_ms",),
}
LATENCY_KINDS = tuple(LATENCY_KIND_ALIASES)
K4_THRESHOLD_MS = 250.0
LATENCY_MIN_SAMPLES = 20
LATENCY_STALE_AFTER_S = 300.0


def add_latency(kind, ms):
    kind = LATENCY_KIND_CANONICAL.get(kind, kind)
    now = time.time()
    valid = (
        isinstance(ms, (int, float)) and not isinstance(ms, bool)
        and ms == ms and ms not in (float("inf"), float("-inf")) and ms >= 0
    )
    stored_kind = kind if valid else f"{kind}_invalid"
    stored_ms = float(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None
    ex("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
       (now, stored_kind, stored_ms, _mode))


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def latency_kind_summary(kind, limit=500, now=None, threshold_ms=None, mode=None):
    """Summarise one latency kind for a capture mode (active mode by default).

    Readiness must not mix demo or legacy samples into a live judgement: a demo
    replay writes wildly different timings, and a legacy sample has unknown
    provenance.
    """
    now = time.time() if now is None else now
    aliases = LATENCY_KIND_ALIASES.get(kind, (kind,))
    marks = ",".join("?" for _ in aliases)
    scope, scope_args = mode_clause(selector=mode)
    rows = q(
        f"""SELECT ts, ms FROM latency WHERE kind IN ({marks}){scope}
             ORDER BY ts DESC LIMIT ?""",
        (*aliases, *scope_args, limit),
    )
    invalid = q(
        f"""SELECT COUNT(*) AS n FROM latency WHERE kind=?{scope}""",
        (f"{kind}_invalid", *scope_args),
    )[0]["n"]
    values = [row["ms"] for row in rows if isinstance(row.get("ms"), (int, float))]
    latest_ts = rows[0]["ts"] if rows else None
    age_s = (now - latest_ts) if latest_ts is not None else None
    threshold = K4_THRESHOLD_MS if threshold_ms is None and kind == "order_arrival_ms" else threshold_ms
    p95 = _percentile(values, 0.95)
    if invalid and not values:
        state = "INVALID"
    elif not values or len(values) < LATENCY_MIN_SAMPLES:
        state = "COLLECTING"
    elif age_s is not None and age_s > LATENCY_STALE_AFTER_S:
        state = "STALE"
    elif threshold is not None and p95 is not None and p95 >= threshold:
        state = "BREACH"
    else:
        state = "PASS"
    return {
        "kind": kind,
        "n": len(values),
        "p50": _percentile(values, 0.50),
        "p95": p95,
        "max": max(values) if values else None,
        "invalid": invalid,
        "latest_ts": latest_ts,
        "age_s": round(age_s, 3) if age_s is not None else None,
        "threshold_ms": threshold,
        "state": state,
    }


def latency_readiness(limit=500, now=None, mode=None):
    return {
        kind: latency_kind_summary(kind, limit=limit, now=now, mode=mode)
        for kind in LATENCY_KINDS
    }


BID_PATH_MAX_SAMPLES = 4000


class PathSequenceConflict(RuntimeError):
    """A path sequence key already exists with a different durable payload."""


class PathSampleCapExceeded(RuntimeError):
    """Writing this path would exceed the durable 4,000-row invariant."""


def insert_bid_path(rows):
    """Persist one buffered path atomically with strict retry validation.

    A duplicate sequence key is accepted only when every persisted field is
    identical to the durable row.  Conflicting payloads raise a stable error,
    leave the caller's buffer owned, and never turn a short write into success.
    The return value is the number of input rows proven durable (new or exact
    idempotent retries).
    """
    if not rows:
        return 0
    with _lock:
        try:
            pending = _validated_new_path_payloads(rows)
            _enforce_path_caps(pending)
            if pending:
                _conn.executemany(_BID_PATH_INSERT, pending)
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    return len(rows)


_BID_PATH_INSERT = """INSERT INTO bid_path_samples(
       kind,trade_id,signal_id,event,market,side,strategy,
       anchor_ts,dt_ms,bid,bid_size,exec_px,qty,mode,
       sample_seq,availability,terminal)
     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

_BID_PATH_COLUMNS = (
    "dt_ms,bid,bid_size,exec_px,qty,availability,terminal,sample_seq"
)


def _bid_path_payload(rows):
    return [
        (
            row.get("kind"), row.get("trade_id"), row.get("signal_id"),
            row.get("event"), row.get("market"), row.get("side"),
            row.get("strategy"), row.get("anchor_ts"), row.get("dt_ms"),
            row.get("bid"), row.get("bid_size"), row.get("exec_px"),
            row.get("qty"), _mode,
            row.get("sample_seq"),
            row.get("availability") or ("quote" if _is_priced(row.get("bid")) else "gap"),
            1 if row.get("terminal") else 0,
        )
        for row in rows
    ]


_BID_PATH_FIELD_NAMES = (
    "kind", "trade_id", "signal_id", "event", "market", "side", "strategy",
    "anchor_ts", "dt_ms", "bid", "bid_size", "exec_px", "qty", "mode",
    "sample_seq", "availability", "terminal",
)
_BID_PATH_FIELD_SQL = ",".join(_BID_PATH_FIELD_NAMES)


def _payload_map(payload):
    return dict(zip(_BID_PATH_FIELD_NAMES, payload))


def _path_sequence_key(payload):
    row = _payload_map(payload)
    seq = row.get("sample_seq")
    if seq is None:
        return None
    if row.get("trade_id") is not None:
        return ("trade_id", row["trade_id"], row.get("kind"), seq)
    if row.get("signal_id") is not None:
        return ("signal_id", row["signal_id"], row.get("kind"), seq)
    return None


def _durable_payload_for_key(key):
    if key is None:
        return None
    owner_column, owner_id, kind, seq = key
    cursor = _conn.execute(
        f"SELECT {_BID_PATH_FIELD_SQL} FROM bid_path_samples "
        f"WHERE {owner_column}=? AND kind=? AND sample_seq=? LIMIT 1",
        (owner_id, kind, seq),
    )
    return cursor.fetchone()


def _sequence_conflict(key):
    owner_column, owner_id, kind, seq = key
    return PathSequenceConflict(
        f"path_sequence_conflict: {owner_column}={owner_id} kind={kind!r} "
        f"sample_seq={seq}"
    )


def _validated_new_path_payloads(rows):
    """Return only genuinely new rows after proving duplicate keys idempotent."""
    pending = []
    pending_by_key = {}
    for payload in _bid_path_payload(rows or []):
        key = _path_sequence_key(payload)
        if key is None:
            pending.append(payload)
            continue
        durable = _durable_payload_for_key(key)
        if durable is not None:
            if tuple(durable) != tuple(payload):
                raise _sequence_conflict(key)
            continue
        prior = pending_by_key.get(key)
        if prior is not None:
            if tuple(prior) != tuple(payload):
                raise _sequence_conflict(key)
            continue
        pending_by_key[key] = payload
        pending.append(payload)
    return pending


def _enforce_path_caps(payloads):
    """Reject a batch before INSERT when its durable owner would exceed the cap."""
    grouped = {}
    for payload in payloads:
        row = _payload_map(payload)
        if row.get("trade_id") is not None:
            key = ("trade_id", row["trade_id"], None)
        elif row.get("signal_id") is not None:
            # Signal ids also appear on position rows.  A decline watch owns only
            # its own kind, so position history cannot consume the watch's cap.
            key = ("signal_id", row["signal_id"], row.get("kind"))
        else:
            continue
        grouped[key] = grouped.get(key, 0) + 1
    for (owner_column, owner_id, kind), incoming in grouped.items():
        if kind is None:
            current = _conn.execute(
                f"SELECT COUNT(*) FROM bid_path_samples WHERE {owner_column}=?",
                (owner_id,),
            ).fetchone()[0]
        else:
            current = _conn.execute(
                f"SELECT COUNT(*) FROM bid_path_samples WHERE {owner_column}=? AND kind=?",
                (owner_id, kind),
            ).fetchone()[0]
        if current + incoming > BID_PATH_MAX_SAMPLES:
            raise PathSampleCapExceeded(
                f"path_sample_cap_exhausted: {owner_column}={owner_id} "
                f"durable={current} incoming={incoming} cap={BID_PATH_MAX_SAMPLES}"
            )


def _read_rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _persist_path_in_transaction(rows, owner_column, owner_id, extra_where="",
                                 truncated=False, dropped_samples=0):
    """Write buffered path rows and derive the summary from what is persisted.

    Must be called with ``_lock`` held and inside an open transaction.  The
    summary is read back after the inserts so it always describes exactly the
    rows on disk, never the caller's in-memory guess.
    """
    pending = _validated_new_path_payloads(rows or [])
    for payload in pending:
        if _payload_map(payload).get(owner_column) != owner_id:
            raise ValueError(
                f"path owner mismatch: expected {owner_column}={owner_id}"
            )
    _enforce_path_caps(pending)
    if pending:
        _conn.executemany(_BID_PATH_INSERT, pending)
    samples = _read_rows(_conn.execute(
        f"SELECT {_BID_PATH_COLUMNS} FROM bid_path_samples"
        f" WHERE {owner_column}=?{extra_where} ORDER BY dt_ms LIMIT ?",
        (owner_id, BID_PATH_MAX_SAMPLES),
    ))
    return bid_path_summary(
        samples, truncated=truncated, dropped_samples=dropped_samples,
    )


def set_trade_path_summary(trade_id, summary):
    """Persist the derived summary once, at close.

    Recomputing it per dashboard refresh meant one path query per listed trade
    and could return millions of sample rows for a single /api/trades call.
    """
    ex("UPDATE trades SET bid_path_summary=? WHERE id=?",
       (json.dumps(summary, separators=(",", ":")) if summary else None, trade_id))


def set_signal_path_summary(signal_id, summary):
    ex("UPDATE signals SET forward_path_summary=? WHERE id=?",
       (json.dumps(summary, separators=(",", ":")) if summary else None, signal_id))


def finalize_signal_path(signal_id, path_rows=None, truncated=False,
                         dropped_samples=0, incomplete_reason=None, now=None):
    """Persist a watch's remaining rows, summary and finalized marker as one unit.

    The caller must not release the watch until this returns.  Rows and summary
    used to be separate commits with no durable marker at all, so a failure
    between them left a half-written path that no restart could detect.
    """
    with _lock:
        try:
            summary = _persist_path_in_transaction(
                path_rows, "signal_id", signal_id,
                extra_where=" AND kind='decline'",
                truncated=truncated, dropped_samples=dropped_samples,
            )
            _conn.execute(
                """UPDATE signals SET forward_path_summary=?,
                       forward_path_finalized=?, path_incomplete_reason=?
                     WHERE id=?""",
                (json.dumps(summary, separators=(",", ":")) if summary else None,
                 time.time() if now is None else now,
                 incomplete_reason, signal_id),
            )
            _conn.commit()
            return summary
        except Exception:
            _conn.rollback()
            raise


def unfinalized_signal_paths():
    """Forward watches that started durably but never recorded finalization."""
    scope, scope_args = mode_clause("s")
    return q(
        "SELECT s.id, s.local_ts, s.market, s.event FROM signals s"
        f" WHERE s.forward_path_started_ts IS NOT NULL "
        f"AND s.forward_path_finalized IS NULL{scope} ORDER BY s.id",
        scope_args,
    )


def bid_path_for_trade(trade_id, limit=BID_PATH_MAX_SAMPLES, mode=None):
    scope, scope_args = mode_clause(selector=mode)
    return q(
        f"""SELECT dt_ms,bid,bid_size,exec_px,qty,availability,terminal,sample_seq
             FROM bid_path_samples
             WHERE trade_id=?{scope} ORDER BY dt_ms LIMIT ?""",
        (trade_id, *scope_args, limit),
    )


def bid_path_for_signal(signal_id, limit=BID_PATH_MAX_SAMPLES, mode=None):
    scope, scope_args = mode_clause(selector=mode)
    return q(
        f"""SELECT dt_ms,bid,bid_size,exec_px,qty,availability,terminal,sample_seq
             FROM bid_path_samples
             WHERE signal_id=? AND kind='decline'{scope} ORDER BY dt_ms LIMIT ?""",
        (signal_id, *scope_args, limit),
    )


def _is_priced(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _path_segments(rows):
    """Split a path into priced segments separated by quote outages.

    Returns ``(segments, gaps)`` where each segment carries its priced points
    and the dt at which its availability ended, and each gap carries the dt it
    started and the dt a quote resumed (None when it never did).
    """
    segments, gaps, current = [], [], None
    for row in rows:
        dt = row.get("dt_ms")
        if row.get("terminal") or row.get("availability") == "terminal":
            # A terminal is an end timestamp, not a quote and not an outage.
            # It closes the current availability segment without contributing
            # price, travel, peak/trough, or samples_priced.
            if current is not None:
                current["close_dt"] = dt
                current = None
            continue
        if _is_priced(row.get("bid")):
            if current is None:
                current = {"points": [], "close_dt": None}
                segments.append(current)
            current["points"].append(row)
            if gaps and gaps[-1]["end"] is None:
                gaps[-1]["end"] = dt
        else:
            if current is not None:
                current["close_dt"] = dt
                current = None
            # Consecutive unpriced observations are one outage, not many.
            if not gaps or gaps[-1]["end"] is not None:
                gaps.append({"start": dt, "end": None})
    return segments, gaps


def bid_path_summary(samples, truncated=False, dropped_samples=0):
    """Derive the scalars the UI and study need from a stored path.

    Calculations join only consecutive priced observations inside the same
    segment.  Filtering the unpriced rows out and connecting what remained drew
    one straight line across a quote outage: a 90c bid that stopped being
    available at 1000ms was reported as held until the next quote arrived, and
    the jump across the outage was counted as tradeable travel.
    """
    rows = list(samples or [])
    if not rows:
        return None
    segments, gaps = _path_segments(rows)
    points = [row for segment in segments for row in segment["points"]]
    if not points:
        return None

    bids = [row["bid"] for row in points]
    peak, trough = max(bids), min(bids)
    peak_row = next(row for row in points if row["bid"] == peak)
    trough_row = next(row for row in points if row["bid"] == trough)

    # Time the held side spent at or above its own peak is the answer to
    # "could that high actually have been filled".  Availability ends at the
    # gap that closes the segment, so nothing beyond that boundary counts.
    at_peak_ms = 0.0
    travelled = 0.0
    for segment in segments:
        segment_points = segment["points"]
        for left, right in zip(segment_points, segment_points[1:]):
            travelled += abs(right["bid"] - left["bid"])
            if left["bid"] >= peak:
                at_peak_ms += right["dt_ms"] - left["dt_ms"]
        last = segment_points[-1]
        if segment["close_dt"] is not None and last["bid"] >= peak:
            at_peak_ms += segment["close_dt"] - last["dt_ms"]

    gap_duration_ms = sum(
        gap["end"] - gap["start"] for gap in gaps if gap["end"] is not None
    )
    # An outage the path never came back from has no measurable end; report the
    # observed span separately rather than folding it into measured downtime.
    final_dt = rows[-1].get("dt_ms")
    unknown_gap_duration_ms = sum(
        max(0.0, final_dt - gap["start"])
        for gap in gaps
        if gap["end"] is None and _is_priced(final_dt) and _is_priced(gap["start"])
    )
    displacement = abs(points[-1]["bid"] - points[0]["bid"])
    return {
        "samples": len(points),
        "samples_total": len(rows),
        "samples_priced": len(points),
        "segments": len(segments),
        "gap_count": len(gaps),
        "gap_duration_ms": round(gap_duration_ms, 1),
        "unknown_gap_duration_ms": round(unknown_gap_duration_ms, 1),
        "first_bid": points[0]["bid"],
        "last_bid": points[-1]["bid"],
        "peak_bid": peak,
        "peak_dt_ms": peak_row["dt_ms"],
        "peak_bid_size": peak_row.get("bid_size"),
        "peak_exec_px": peak_row.get("exec_px"),
        "ms_at_peak": round(at_peak_ms, 1),
        "trough_bid": trough,
        "trough_dt_ms": trough_row["dt_ms"],
        "path_travelled_c": round(travelled, 2),
        "displacement_c": round(displacement, 2),
        # 1.0 = straight line, near 0 = chopped back and forth for nothing.
        # Undefined rather than 1.0 when no intra-segment travel was observed.
        "path_efficiency": round(displacement / travelled, 4) if travelled > 0 else None,
        "span_ms": round(rows[-1]["dt_ms"] - rows[0]["dt_ms"], 1),
        "truncated": bool(truncated),
        "dropped_samples": int(dropped_samples or 0),
    }


def update_trade_high(tid, bid, ts):
    """Persist a new executable high only when it strictly exceeds the stored high."""
    with _lock:
        row = _conn.execute(
            """SELECT max_executable_bid, entry_px FROM trades WHERE id=?""",
            (tid,),
        ).fetchone()
        if row is None or bid is None:
            return False
        current, entry_px = row
        if current is not None and bid <= current:
            return False
        mfe = max(0.0, float(bid) - float(entry_px or 0.0))
        _conn.execute(
            """UPDATE trades SET max_executable_bid=?, max_executable_bid_ts=?, mfe_c=?
                WHERE id=?""",
            (float(bid), float(ts), mfe, tid),
        )
        _conn.commit()
        return True


def insert_goal_latency(row):
    live_data = ((row.get("detail") or {}).get("live_data") or {})
    normalized = row.get("normalized_event") or normalize_match_event(
        row["change_kind"], row["score_before"], row["score_after"], live_data,
    )
    cur = ex(
        """INSERT INTO goal_latency_observations(
               observed_ts,event,milestone_id,change_kind,live_type,
               score_before,score_after,previous_poll_ts,poll_started_ts,response_ms,
               last_book_change_ts,last_book_lead_ms,last_trade_ts,last_trade_lead_ms,
               canonical_type,canonical_side,normalized_event,detail,mode)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["observed_ts"], row["event"], row["milestone_id"],
            row["change_kind"], row.get("live_type"),
            json.dumps(row["score_before"], separators=(",", ":")),
            json.dumps(row["score_after"], separators=(",", ":")),
            row.get("previous_poll_ts"), row["poll_started_ts"], row["response_ms"],
            row.get("last_book_change_ts"), row.get("last_book_lead_ms"),
            row.get("last_trade_ts"), row.get("last_trade_lead_ms"),
            normalized["canonical_type"], normalized["side"],
            json.dumps(normalized, separators=(",", ":")),
            json.dumps(row.get("detail") or {}, separators=(",", ":")),
            _mode,
        ),
    )
    return cur.lastrowid


def insert_match_clock(row):
    cur = ex(
        """INSERT INTO match_clock_observations(
               observed_ts,poll_started_ts,previous_poll_ts,response_ms,event,milestone_id,
               provider_period,provider_minute,provider_stoppage,provider_clock,
               provider_status,precision,raw_context,mode,source,confirmed_ts,
               confirmation_previous_poll_ts)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["observed_ts"], row["poll_started_ts"], row.get("previous_poll_ts"),
            row["response_ms"], row["event"], row["milestone_id"],
            row.get("provider_period"), row.get("provider_minute"),
            row.get("provider_stoppage"), row.get("provider_clock"),
            row.get("provider_status"), row.get("precision") or "provider_minute_polled",
            json.dumps(row.get("raw_context") or {}, separators=(",", ":")),
            _mode,
            row.get("source") or match_clock.CLOCK_SOURCE,
            row.get("confirmed_ts"),
            row.get("confirmation_previous_poll_ts"),
        ),
    )
    return cur.lastrowid


def latest_match_clock(event):
    rows = q(
        """SELECT * FROM match_clock_observations WHERE event=?
            ORDER BY id DESC LIMIT 1""",
        (event,),
    )
    return rows[0] if rows else None


def upsert_provider_event(row):
    """Insert a new fingerprint or refresh last_observed_ts. History stays append-only."""
    occurrence_ts, occurrence_source, occurrence_reason = (
        match_events.provider_occurrence(row.get("raw_payload"))
    )
    # Duplicate identity is mode-scoped: the same provider fingerprint may be
    # observed once in demo and once in live, and neither may overwrite the
    # other's raw payload or mode.
    scope, scope_args = mode_clause()
    existing = q(
        "SELECT id, first_observed_ts, canonical_type FROM provider_match_events"
        f" WHERE event=? AND fingerprint=?{scope}",
        (row["event"], row["fingerprint"], *scope_args),
    )
    if existing:
        ex(
            """UPDATE provider_match_events
                  SET last_observed_ts=?, poll_started_ts=?, previous_poll_ts=?, response_ms=?
                WHERE id=?""",
            (
                row["observed_ts"], row["poll_started_ts"], row.get("previous_poll_ts"),
                row["response_ms"], existing[0]["id"],
            ),
        )
        return existing[0]["id"], False
    cur = ex(
        """INSERT INTO provider_match_events(
               observed_ts,first_observed_ts,last_observed_ts,poll_started_ts,previous_poll_ts,
               response_ms,event,milestone_id,fingerprint,previous_fingerprint,canonical_type,
               canonical_side,provider_period,provider_minute,provider_stoppage,provider_clock,
               normalized_event,raw_payload,mode,
               provider_occurrence_ts,provider_occurrence_source,
               provider_occurrence_unavailable_reason)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["observed_ts"], row["observed_ts"], row["observed_ts"],
            row["poll_started_ts"], row.get("previous_poll_ts"), row["response_ms"],
            row["event"], row["milestone_id"], row["fingerprint"],
            row.get("previous_fingerprint"), row["canonical_type"],
            row.get("canonical_side"), row.get("provider_period"),
            row.get("provider_minute"), row.get("provider_stoppage"),
            row.get("provider_clock"),
            json.dumps(row.get("normalized_event") or {}, separators=(",", ":")),
            json.dumps(row.get("raw_payload") or {}, separators=(",", ":")),
            _mode,
            occurrence_ts, occurrence_source, occurrence_reason,
        ),
    )
    return cur.lastrowid, True


SUBSTANTIVE_REVISION_TYPES = (
    "goal.observed", "penalty.scored",
)


def previous_substantive_fingerprint(event, mode=None):
    """Newest persisted substantive event for this event and capture mode.

    A correction usually arrives on a later poll than the goal it revises, and
    a restart loses the in-memory link entirely.  Resolving it from durable
    state keeps the revision chain intact across process death.  Corrections
    are excluded so a correction never links to another correction, and the
    lookup is mode- and event-scoped so it can never link across either.
    """
    scope, scope_args = mode_clause(selector=mode)
    marks = ",".join("?" for _ in SUBSTANTIVE_REVISION_TYPES)
    rows = q(
        f"""SELECT fingerprint FROM provider_match_events
             WHERE event=? AND canonical_type IN ({marks}){scope}
             ORDER BY id DESC LIMIT 1""",
        (event, *SUBSTANTIVE_REVISION_TYPES, *scope_args),
    )
    return rows[0]["fingerprint"] if rows else None


def finish_goal_latency(row_id, first_book=None, first_trade=None):
    ex(
        """UPDATE goal_latency_observations
              SET first_book_after_ts=?, first_book_after_ms=?,
                  first_trade_after_ts=?, first_trade_after_ms=?
            WHERE id=?""",
        (
            first_book.get("wall") if first_book else None,
            first_book.get("delta_ms") if first_book else None,
            first_trade.get("wall") if first_trade else None,
            first_trade.get("delta_ms") if first_trade else None,
            row_id,
        ),
    )


def upsert_market(ticker, event, series, title, close_time, status,
                  display_game=None, display_leg=None):
    ex("""INSERT INTO markets(
                ticker,event,series,title,close_time,status,added_ts,display_game,display_leg)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
          close_time=excluded.close_time, status=excluded.status, title=excluded.title,
          display_game=COALESCE(excluded.display_game,markets.display_game),
          display_leg=COALESCE(excluded.display_leg,markets.display_leg)""",
       (ticker, event, series, title, close_time, status, time.time(),
        display_game, display_leg))


def _stamp_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def insert_signal(s):
    cur = ex("""INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,dl,levels,size,
                ref,ext,conf_lag_ms,late,outcome,detail,mode,match_clock_snapshot,
                forward_path_started_ts,config_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (s["ts_ms"], s["local_ts"], s["market"], s["event"], s["series"], s["dir"],
              s["dl"], s["levels"], s["size"], s["ref"], s["ext"], s.get("conf_lag_ms"),
              1 if s.get("late") else 0, s["outcome"], json.dumps(s.get("detail") or {}), _mode,
              _stamp_text(s.get("match_clock_snapshot")),
              s.get("forward_path_started_ts"), _config_id))
    return cur.lastrowid


def update_signal_outcome(signal_id, outcome, detail=None):
    ex("UPDATE signals SET outcome=?, detail=? WHERE id=?",
       (outcome, json.dumps(detail or {}), signal_id))


def finish_paper_signal(signal_id, outcome, detail, latency_ms, order_arrival_ms=None):
    """Atomically finalize a non-fill paper signal and its latency sample."""
    with _lock:
        try:
            _conn.execute("UPDATE signals SET outcome=?, detail=? WHERE id=?",
                          (outcome, json.dumps(detail or {}), signal_id))
            _conn.execute("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
                          (time.time(), "paper_entry_ms", latency_ms, _mode))
            if order_arrival_ms is not None:
                _conn.execute("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
                              (time.time(), "order_arrival_ms", order_arrival_ms, _mode))
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def insert_trade(t):
    cur = ex("""INSERT INTO trades(signal_id,market,event,series,dir,side,entry_ts,entry_px,
                size,cap,notional,book_at_entry,status,mode,strategy,config_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?)""",
             (t["signal_id"], t["market"], t["event"], t["series"], t["dir"], t["side"],
              t["entry_ts"], t["entry_px"], t["size"], t["cap"], t["notional"],
              json.dumps(t.get("book_at_entry") or {}), _mode,
              t.get("strategy") or "gate_a", _config_id))
    return cur.lastrowid


def open_paper_trade(t, detail, fill_levels, entry_fee, latency_ms, order_arrival_ms=None):
    """Atomically open a realistic paper trade, fills, and signal outcome."""
    with _lock:
        try:
            cur = _conn.execute(
                """INSERT INTO trades(signal_id,market,event,series,dir,side,entry_ts,entry_px,
                       size,cap,notional,book_at_entry,status,mode,remaining,realized_gross,
                       accrued_fees,exit_qty,exit_vwap_num,fee_type,fee_multiplier,strategy,
                       config_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, 0, ?, 0, 0, ?, ?,?,?)""",
                (t["signal_id"], t["market"], t["event"], t["series"], t["dir"], t["side"],
                 t["entry_ts"], t["entry_px"], t["size"], t["cap"], t["notional"],
                 json.dumps(t.get("book_at_entry") or {}), _mode, t["size"], entry_fee,
                 t.get("fee_type"), t.get("fee_multiplier"),
                 t.get("strategy") or "gate_a", _config_id),
            )
            trade_id = cur.lastrowid
            _conn.execute("UPDATE signals SET outcome='filled', detail=? WHERE id=?",
                          (json.dumps(detail or {}), t["signal_id"]))
            _conn.execute("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
                          (time.time(), "paper_entry_ms", latency_ms, _mode))
            if order_arrival_ms is not None:
                _conn.execute("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
                              (time.time(), "order_arrival_ms", order_arrival_ms, _mode))
            for price, quantity, fee in fill_levels:
                _conn.execute(
                    """INSERT INTO paper_fills(trade_id,signal_id,ts,leg,side,price,quantity,
                           notional,fee,reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (trade_id, t["signal_id"], t["entry_ts"], "entry", t["side"], price,
                     quantity, price * quantity / 100.0, fee, "entry", _mode),
                )
            _conn.commit()
            return trade_id
        except Exception:
            _conn.rollback()
            raise


def record_paper_exit(tid, signal_id, side, ts, reason, fill_levels, progress,
                      latency_ms, final=None, path_rows=None, truncated=False,
                      dropped_samples=0):
    """Atomically persist exit fills, position progress, and optional close.

    When `final` is supplied this is the trade's last write, so the remaining
    buffered path rows, the terminal row, the derived summary, the final fill
    and the closed-trade fields all commit as ONE transaction.  They used to be
    two: the trade was closed first and the path flushed afterwards, so a failed
    path write left a closed trade, an orphaned buffer and no retry owner.

    Raises on failure with nothing written; the caller must keep owning the
    position.  Retrying is safe because path rows carry a sequence key.
    """
    with _lock:
        try:
            if final is not None:
                summary = _persist_path_in_transaction(
                    path_rows, "trade_id", tid,
                    truncated=truncated, dropped_samples=dropped_samples,
                )
                _conn.execute(
                    "UPDATE trades SET bid_path_summary=? WHERE id=?",
                    (json.dumps(summary, separators=(",", ":")) if summary else None,
                     tid),
                )
            elif path_rows:
                _conn.executemany(_BID_PATH_INSERT, _bid_path_payload(path_rows))
            for price, quantity, fee in fill_levels:
                _conn.execute(
                    """INSERT INTO paper_fills(trade_id,signal_id,ts,leg,side,price,quantity,
                           notional,fee,reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (tid, signal_id, ts, "exit", side, price, quantity,
                     price * quantity / 100.0, fee, reason, _mode),
                )
            fields = [
                progress["remaining"], progress["realized_gross"],
                progress["accrued_fees"], progress["exit_qty"],
                progress["exit_vwap_num"], tid,
            ]
            _conn.execute(
                """UPDATE trades SET remaining=?, realized_gross=?, accrued_fees=?,
                       exit_qty=?, exit_vwap_num=? WHERE id=?""",
                fields,
            )
            _conn.execute("INSERT INTO latency(ts,kind,ms,mode) VALUES(?,?,?,?)",
                          (time.time(), "paper_exit_ms", latency_ms, _mode))
            if final is not None:
                _conn.execute(
                    """UPDATE trades SET exit_ts=?, exit_px=?, exit_reason=?, gross=?, fees=?,
                           net=?, mae=?, shadow_stop_px=?, status='closed' WHERE id=?""",
                    (ts, final["exit_px"], reason, final["gross"], final["fees"],
                     final["net"], final["mae"], final["shadow_stop_px"], tid),
                )
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def load_open_paper_positions():
    """Return durable open-position state for process restart recovery."""
    return q(
        """SELECT t.*, s.ref, s.ext, s.detail AS signal_detail,
                  COALESCE((SELECT SUM(f.fee) FROM paper_fills f
                            WHERE f.trade_id=t.id AND f.leg='entry'), 0) AS entry_fees,
                  -- Durable path state: without it the first post-restart sample
                  -- reuses sequence 1, collides with history, and is silently
                  -- dropped by the partial unique index.
                  COALESCE((SELECT MAX(p.sample_seq) FROM bid_path_samples p
                            WHERE p.trade_id=t.id), 0) AS path_max_seq,
                  COALESCE((SELECT COUNT(*) FROM bid_path_samples p
                            WHERE p.trade_id=t.id), 0) AS path_rows_durable,
                  COALESCE((SELECT MAX(p.terminal) FROM bid_path_samples p
                            WHERE p.trade_id=t.id), 0) AS path_has_terminal
             FROM trades t LEFT JOIN signals s ON s.id=t.signal_id
            WHERE t.status='open' AND t.mode=? ORDER BY t.id""",
        (_mode,),
    )


def close_trade(tid, exit_px, reason, gross, fees, net, mae, shadow_stop_px,
                path_rows=None, truncated=False, dropped_samples=0):
    """Close a simple (non-realistic) trade and its path in ONE transaction.

    Same contract as `record_paper_exit`: path rows, terminal row, summary and
    the closed-trade fields commit together or not at all.
    """
    with _lock:
        try:
            summary = _persist_path_in_transaction(
                path_rows, "trade_id", tid,
                truncated=truncated, dropped_samples=dropped_samples,
            )
            _conn.execute(
                """UPDATE trades SET exit_ts=?, exit_px=?, exit_reason=?, gross=?,
                       fees=?, net=?, mae=?, shadow_stop_px=?, bid_path_summary=?,
                       status='closed' WHERE id=?""",
                (time.time(), exit_px, reason, gross, fees, net, mae, shadow_stop_px,
                 json.dumps(summary, separators=(",", ":")) if summary else None,
                 tid),
            )
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _paper_fill_integrity(trade):
    """Return True/False for trades with a recorded arrival-book fill model."""
    try:
        book = json.loads(trade.get("book_at_entry") or "{}")
        levels = book.get("fill_levels")
        if not isinstance(levels, list) or not levels:
            return None
        source_key = "no_bids" if trade["side"] == "yes" else "yes_bids"
        source = {round(100.0 - float(price), 8): float(size)
                  for price, size in book.get(source_key, [])}
        # The snapshot is capped at a fixed depth, so a fill that walked further
        # than the cap has levels the evidence simply does not cover.  Those are
        # unverifiable, not wrong: reporting them as integrity failures made K1
        # fail hardest on the deepest walks, which are exactly the fills whose
        # realism matters most.  A level beyond the deepest recorded price is
        # therefore treated as missing evidence; a level *inside* the recorded
        # range that the book does not support is still a real inconsistency.
        deepest = max(source, default=None)
        used = {}
        quantity = weighted = notional = 0.0
        for price, size in levels:
            price, size = float(price), float(size)
            if size <= 0 or price <= 0 or price > float(trade["cap"]) + 1e-6:
                return False
            if deepest is None or price > deepest + 1e-6:
                return None
            used[round(price, 8)] = used.get(round(price, 8), 0.0) + size
            if used[round(price, 8)] > source.get(round(price, 8), 0.0) + 1e-6:
                return False
            quantity += size
            weighted += price * size
            notional += price * size / 100.0
        if quantity <= 0 or abs(quantity - float(trade["size"])) > 0.11:
            return False
        if abs(weighted / quantity - float(trade["entry_px"])) > 0.02:
            return False
        return notional <= float(trade["notional"]) + 0.02
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _strategy_key(value):
    """Map durable/legacy strategy labels into the two supported paper sleeves."""
    if value in {"price_only_late_score", "price_only_late_score_v1"}:
        return "price_only_late_score"
    return "gate_a"


def _signal_strategy(row):
    raw = row.get("trade_strategy")
    if not raw:
        if isinstance(row.get("detail"), dict):
            detail = row["detail"]
        else:
            try:
                detail = json.loads(row.get("detail") or "{}")
            except (TypeError, json.JSONDecodeError):
                detail = {}
        sleeve = detail.get("sleeve") if isinstance(detail.get("sleeve"), dict) else {}
        raw = detail.get("strategy") or sleeve.get("strategy")
    return _strategy_key(raw)


def _event_cluster_ci(closed):
    """Deterministic event-clustered bootstrap interval for closed net per fill.

    Each iteration resamples ``len(evs)`` whole event clusters (with replacement)
    and takes the mean net over every fill in the resample.  That mean is
    identically ``sum(chosen cluster sums) / sum(chosen cluster counts)``, so the
    per-cluster (sum, count) is precomputed and the iteration is O(clusters)
    rather than O(fills): the old form rebuilt a flat list of every resampled
    fill 2,000 times, which cost seconds on a full study.  ``rnd.choice`` is
    called the same number of times in the same order over an equal-length
    sequence, so the seeded draw -- and therefore the interval -- is byte-for-byte
    unchanged (see test_event_cluster_ci_matches_reference).
    """
    by_ev = {}
    for t in closed:
        by_ev.setdefault(t["event"], []).append(t["net"] or 0)
    evs = list(by_ev.values())
    ci = None
    if len(evs) >= 5:
        clusters = [(sum(group), len(group)) for group in evs]
        k = len(clusters)
        rnd = random.Random(7)
        means = []
        for _ in range(2000):
            total = 0.0
            count = 0
            for _ in range(k):
                cluster_sum, cluster_n = rnd.choice(clusters)
                total += cluster_sum
                count += cluster_n
            means.append(total / count)
        means.sort()
        ci = [round(means[int(0.025 * len(means))], 2), round(means[int(0.975 * len(means))], 2)]
    return ci


def _latency_evidence(mode=None):
    summary = latency_kind_summary("order_arrival_ms", mode=mode)
    source = "order_arrival_ms"
    if summary["n"] == 0:
        summary = latency_kind_summary("feed_ingress_ms", mode=mode)
        source = "feed_ingress_ms" if summary["n"] else "order_arrival_ms"
    status = summary["state"]
    if status == "PASS":
        legacy = "OK"
    elif status == "BREACH":
        legacy = "BREACH"
    else:
        legacy = status
    return {
        "p95_ms": summary["p95"],
        "source": source,
        "status": legacy,
        "state": summary["state"],
        "n": summary["n"],
        "invalid": summary["invalid"],
        "threshold_ms": summary["threshold_ms"],
        "scope": "shared_execution_adapter",
        "kinds": latency_readiness(),
    }


def _strategy_summary(closed, open_t, signals, latency_evidence):
    n = len(closed)
    gross = sum(t.get("gross") or 0 for t in closed)
    fees = sum(t.get("fees") or 0 for t in closed)
    net = sum(t.get("net") or 0 for t in closed)
    wins = sum(1 for t in closed if (t.get("net") or 0) > 0)
    ci = _event_cluster_ci(closed)
    signal_counts = {}
    for row in signals:
        outcome = row.get("outcome") or "unknown"
        signal_counts[outcome] = signal_counts.get(outcome, 0) + 1
    confirmed_outcomes = {
        "filled", "rejected_cap", "no_book", "killed", "expired", "unsupported_fee",
    }
    n_conf = sum(count for outcome, count in signal_counts.items()
                 if outcome in confirmed_outcomes)
    fill_checks = [(t["id"], _paper_fill_integrity(t)) for t in closed + open_t]
    fill_checks = [(tid, ok) for tid, ok in fill_checks if ok is not None]
    fill_failures = [tid for tid, ok in fill_checks if not ok]
    k1_status = ("COLLECTING" if len(fill_checks) < 25 else
                 "FAIL" if fill_failures else "PASS")
    exits = {}
    for trade in closed:
        reason = trade.get("exit_reason") or "unknown"
        exits[reason] = exits.get(reason, 0) + 1
    open_realized_gross = sum(t.get("realized_gross") or 0 for t in open_t)
    open_accrued_fees = sum(t.get("accrued_fees") or 0 for t in open_t)
    open_remaining = sum(
        t.get("remaining") if t.get("remaining") is not None else (t.get("size") or 0)
        for t in open_t
    )
    evidence = {
        "k1_fill_integrity": {
            "n_fills": len(fill_checks), "needed": 25,
            "failures": fill_failures[:10], "status": k1_status,
        },
        "k2_ci": {
            "n_signals": n_conf, "needed": 50, "ci": ci,
            "status": ("COLLECTING" if n_conf < 50 else
                       "INSUFFICIENT_CLUSTERS" if ci is None else
                       "PASS" if ci[0] > 0 else "FAIL"),
        },
        "k4_latency": dict(latency_evidence),
    }
    return {
        "closed": n,
        "open": len(open_t),
        "gross": round(gross, 2),
        "fees": round(fees, 2),
        "net": round(net, 2),
        "net_per_fill": round(net / n, 2) if n else 0,
        "win_pct": round(wins / n * 100, 1) if n else 0,
        "ci95": ci,
        "exit_reasons": exits,
        "signals": signal_counts,
        "open_remaining_contracts": round(open_remaining, 2),
        "open_partial_realized_gross": round(open_realized_gross, 2),
        "open_accrued_fees": round(open_accrued_fees, 2),
        "open_partial_realized_net": round(open_realized_gross - open_accrued_fees, 2),
        "evidence": evidence,
    }


def stats(mode=None):
    """Aggregate the study for one capture mode (the active mode by default).

    The result is memoised on the writer's total change count (see
    `_stats_cache`): concurrent dashboard/WebSocket callers share one
    computation while the study is unchanged, and any write invalidates it, so
    the numbers are never served stale.  The expensive event-clustered bootstrap
    in `_compute_stats` therefore runs at most once per change, not once per
    poll -- collapsing multi-tab refreshes and reconnect storms onto one pass.
    """
    key = _mode if mode is None else mode
    version = _conn.total_changes if _conn is not None else -1
    cached = _stats_cache.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    result = _compute_stats(mode)
    _stats_cache[key] = (version, result)
    return result


def _compute_stats(mode=None):
    """Aggregate the study for one capture mode (the active mode by default).

    Isolation is enforced here rather than by deleting rows at startup, so demo
    and legacy evidence stays on disk without ever entering live aggregates.
    """
    scope, scope_args = mode_clause(selector=mode)
    trade_scope, trade_args = mode_clause("t", selector=mode)
    signal_scope, signal_args = mode_clause("s", selector=mode)
    closed = q(f"SELECT * FROM trades WHERE status='closed'{scope}", scope_args)
    open_t = q(f"SELECT * FROM trades WHERE status='open'{scope}", scope_args)
    # Sub-threshold rows are research observations about where the detector
    # floor sits, not signals of either sleeve.  `_strategy_key` maps every
    # unrecognised label to gate_a, so leaving them in would inflate the Gate A
    # funnel with bursts that were never eligible to trade.  They are counted
    # separately below instead.
    signal_rows = q(
        "SELECT s.outcome,s.detail,t.strategy AS trade_strategy"
        " FROM signals s LEFT JOIN trades t"
        f" ON t.signal_id=s.id{trade_scope} WHERE s.outcome IS NOT 'subthreshold'"
        f"{signal_scope}",
        (*trade_args, *signal_args),
    )
    subthreshold = q(
        f"SELECT COUNT(*) n FROM signals s WHERE s.outcome='subthreshold'{signal_scope}",
        signal_args,
    )
    latency_evidence = _latency_evidence(mode=mode)
    by_strategy = {
        "gate_a": {"closed": [], "open": [], "signals": []},
        "price_only_late_score": {"closed": [], "open": [], "signals": []},
    }
    for trade in closed:
        by_strategy[_strategy_key(trade.get("strategy"))]["closed"].append(trade)
    for trade in open_t:
        by_strategy[_strategy_key(trade.get("strategy"))]["open"].append(trade)
    for signal in signal_rows:
        by_strategy[_signal_strategy(signal)]["signals"].append(signal)
    sleeves = {
        strategy: _strategy_summary(
            rows["closed"], rows["open"], rows["signals"], latency_evidence,
        )
        for strategy, rows in by_strategy.items()
    }
    combined = _strategy_summary(closed, open_t, signal_rows, latency_evidence)
    # Per-league results retain the legacy combined fields while adding
    # strategy-separated economics for the operator dashboard.
    lg = {}
    for t in closed:
        series = t["series"]
        d = lg.setdefault(series, {
            "series": series,
            "display_name": config.LEAGUE_NAMES.get(series, series),
            "n": 0, "net": 0.0, "gross": 0.0, "fees": 0.0, "wins": 0,
            "sleeves": {
                "gate_a": {"n": 0, "net": 0.0, "gross": 0.0, "fees": 0.0, "wins": 0},
                "price_only_late_score": {
                    "n": 0, "net": 0.0, "gross": 0.0, "fees": 0.0, "wins": 0,
                },
            },
        })
        strategy = _strategy_key(t.get("strategy"))
        net_value, gross_value, fee_value = (
            t.get("net") or 0.0, t.get("gross") or 0.0, t.get("fees") or 0.0,
        )
        won = 1 if net_value > 0 else 0
        for bucket in (d, d["sleeves"][strategy]):
            bucket["n"] += 1
            bucket["net"] += net_value
            bucket["gross"] += gross_value
            bucket["fees"] += fee_value
            bucket["wins"] += won
    for d in lg.values():
        for bucket in (d, *d["sleeves"].values()):
            bucket["net"] = round(bucket["net"], 2)
            bucket["gross"] = round(bucket["gross"], 2)
            bucket["fees"] = round(bucket["fees"], 2)
            bucket["win_pct"] = round(bucket["wins"] / bucket["n"] * 100, 1) \
                if bucket["n"] else 0.0
            bucket["net_per_trade"] = round(bucket["net"] / bucket["n"], 2) \
                if bucket["n"] else 0.0
    evidence = combined["evidence"]
    kill = {
        "k1_fill_integrity": evidence["k1_fill_integrity"],
        "k2_ci": evidence["k2_ci"],
        "k4_latency_p95_ms": latency_evidence["p95_ms"],
        "k4_latency_source": latency_evidence["source"],
        "k4_status": latency_evidence["status"],
    }
    return {
        **{key: combined[key] for key in (
            "closed", "open", "gross", "net", "net_per_fill", "win_pct", "fees",
            "ci95", "signals", "exit_reasons", "open_remaining_contracts",
            "open_partial_realized_gross", "open_accrued_fees",
            "open_partial_realized_net",
        )},
        "combined": combined,
        "sleeves": sleeves,
        "leagues": lg,
        "kill": kill,
        # Reported beside the sleeves, never inside them.
        "subthreshold_observations": subthreshold[0]["n"] if subthreshold else 0,
    }
