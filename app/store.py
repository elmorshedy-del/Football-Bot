"""SQLite persistence + aggregate stats (event-clustered bootstrap, kill conditions)."""
import json
import os
import random
import sqlite3
import threading
import time

from . import config
from . import match_clock
from . import match_events
from .match_events import normalize_match_event

_lock = threading.Lock()
_conn = None

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
CREATE TABLE IF NOT EXISTS paper_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, signal_id INTEGER,
  ts REAL, leg TEXT, side TEXT, price REAL, quantity REAL, notional REAL,
  fee REAL, reason TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS latency(
  ts REAL, kind TEXT, ms REAL);
CREATE TABLE IF NOT EXISTS eventlog(
  ts REAL, kind TEXT, text TEXT);
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
    for column in ("match_clock_snapshot", "forward_path_summary"):
        try:
            _conn.execute(f"ALTER TABLE signals ADD COLUMN {column} TEXT")
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


def set_mode(m):
    global _mode
    _mode = m


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


def q(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


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


def insert_bid_path(rows):
    """Persist one buffered path in a single transaction.

    The caller accumulates samples in memory for the life of a position or a
    decline window and flushes once.  Committing per sample would add a
    synchronous fsync to the asyncio hot path for every book update.
    """
    if not rows:
        return 0
    payload = [
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
    with _lock:
        _conn.executemany(
            """INSERT OR IGNORE INTO bid_path_samples(
                   kind,trade_id,signal_id,event,market,side,strategy,
                   anchor_ts,dt_ms,bid,bid_size,exec_px,qty,mode,
                   sample_seq,availability,terminal)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        _conn.commit()
    return len(payload)


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


def bid_path_for_trade(trade_id, limit=BID_PATH_MAX_SAMPLES):
    return q(
        """SELECT dt_ms,bid,bid_size,exec_px,qty,availability,terminal,sample_seq
             FROM bid_path_samples
             WHERE trade_id=? ORDER BY dt_ms LIMIT ?""",
        (trade_id, limit),
    )


def bid_path_for_signal(signal_id, limit=BID_PATH_MAX_SAMPLES):
    return q(
        """SELECT dt_ms,bid,bid_size,exec_px,qty,availability,terminal,sample_seq
             FROM bid_path_samples
             WHERE signal_id=? AND kind='decline' ORDER BY dt_ms LIMIT ?""",
        (signal_id, limit),
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
                ref,ext,conf_lag_ms,late,outcome,detail,mode,match_clock_snapshot)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (s["ts_ms"], s["local_ts"], s["market"], s["event"], s["series"], s["dir"],
              s["dl"], s["levels"], s["size"], s["ref"], s["ext"], s.get("conf_lag_ms"),
              1 if s.get("late") else 0, s["outcome"], json.dumps(s.get("detail") or {}), _mode,
              _stamp_text(s.get("match_clock_snapshot"))))
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
                size,cap,notional,book_at_entry,status,mode,strategy)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?)""",
             (t["signal_id"], t["market"], t["event"], t["series"], t["dir"], t["side"],
              t["entry_ts"], t["entry_px"], t["size"], t["cap"], t["notional"],
              json.dumps(t.get("book_at_entry") or {}), _mode,
              t.get("strategy") or "gate_a"))
    return cur.lastrowid


def open_paper_trade(t, detail, fill_levels, entry_fee, latency_ms, order_arrival_ms=None):
    """Atomically open a realistic paper trade, fills, and signal outcome."""
    with _lock:
        try:
            cur = _conn.execute(
                """INSERT INTO trades(signal_id,market,event,series,dir,side,entry_ts,entry_px,
                       size,cap,notional,book_at_entry,status,mode,remaining,realized_gross,
                       accrued_fees,exit_qty,exit_vwap_num,fee_type,fee_multiplier,strategy)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, 0, ?, 0, 0, ?, ?,?)""",
                (t["signal_id"], t["market"], t["event"], t["series"], t["dir"], t["side"],
                 t["entry_ts"], t["entry_px"], t["size"], t["cap"], t["notional"],
                 json.dumps(t.get("book_at_entry") or {}), _mode, t["size"], entry_fee,
                 t.get("fee_type"), t.get("fee_multiplier"),
                 t.get("strategy") or "gate_a"),
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
                      latency_ms, final=None):
    """Atomically persist exit fills, position progress, and optional close."""
    with _lock:
        try:
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
                            WHERE f.trade_id=t.id AND f.leg='entry'), 0) AS entry_fees
             FROM trades t LEFT JOIN signals s ON s.id=t.signal_id
            WHERE t.status='open' AND t.mode=? ORDER BY t.id""",
        (_mode,),
    )


def close_trade(tid, exit_px, reason, gross, fees, net, mae, shadow_stop_px):
    ex("""UPDATE trades SET exit_ts=?, exit_px=?, exit_reason=?, gross=?, fees=?, net=?,
          mae=?, shadow_stop_px=?, status='closed' WHERE id=?""",
       (time.time(), exit_px, reason, gross, fees, net, mae, shadow_stop_px, tid))


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
        used = {}
        quantity = weighted = notional = 0.0
        for price, size in levels:
            price, size = float(price), float(size)
            if size <= 0 or price <= 0 or price > float(trade["cap"]) + 1e-6:
                return False
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
    """Deterministic event-clustered bootstrap interval for closed net per fill."""
    by_ev = {}
    for t in closed:
        by_ev.setdefault(t["event"], []).append(t["net"] or 0)
    evs = list(by_ev.values())
    ci = None
    if len(evs) >= 5:
        rnd = random.Random(7)
        means = []
        for _ in range(2000):
            flat = [x for _ in range(len(evs)) for x in rnd.choice(evs)]
            means.append(sum(flat) / len(flat))
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

    Isolation is enforced here rather than by deleting rows at startup, so demo
    and legacy evidence stays on disk without ever entering live aggregates.
    """
    scope, scope_args = mode_clause(selector=mode)
    trade_scope, trade_args = mode_clause("t", selector=mode)
    signal_scope, signal_args = mode_clause("s", selector=mode)
    closed = q(f"SELECT * FROM trades WHERE status='closed'{scope}", scope_args)
    open_t = q(f"SELECT * FROM trades WHERE status='open'{scope}", scope_args)
    signal_rows = q(
        "SELECT s.outcome,s.detail,t.strategy AS trade_strategy"
        " FROM signals s LEFT JOIN trades t"
        f" ON t.signal_id=s.id{trade_scope} WHERE 1=1{signal_scope}",
        (*trade_args, *signal_args),
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
    }
