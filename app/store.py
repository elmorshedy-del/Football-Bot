"""SQLite persistence + aggregate stats (event-clustered bootstrap, kill conditions)."""
import json
import os
import random
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(
  ticker TEXT PRIMARY KEY, event TEXT, series TEXT, title TEXT,
  close_time TEXT, status TEXT, added_ts REAL);
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
  book_at_entry TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS latency(
  ts REAL, kind TEXT, ms REAL);
CREATE TABLE IF NOT EXISTS eventlog(
  ts REAL, kind TEXT, text TEXT);
"""


_mode = "demo"


def init():
    global _conn
    os.makedirs(config.DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(os.path.join(config.DATA_DIR, "footballbot.db"),
                            check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(SCHEMA)
    # migrate: add mode column to older DBs (persisted on a volume)
    for tbl in ("signals", "trades"):
        try:
            _conn.execute(f"ALTER TABLE {tbl} ADD COLUMN mode TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
    _conn.commit()


def set_mode(m):
    global _mode
    _mode = m


def purge_non_live():
    """On a live boot, drop demo/legacy rows so live stats start clean.
    Live rows (mode='live') are preserved across redeploys; only demo/NULL go."""
    with _lock:
        for tbl in ("signals", "trades"):
            _conn.execute(f"DELETE FROM {tbl} WHERE mode IS NULL OR mode!='live'")
        # drop firehose-era junk: signals on markets we never registered
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


def log_event(kind, text):
    ex("INSERT INTO eventlog(ts,kind,text) VALUES(?,?,?)", (time.time(), kind, text))


def add_latency(kind, ms):
    ex("INSERT INTO latency(ts,kind,ms) VALUES(?,?,?)", (time.time(), kind, ms))


def upsert_market(ticker, event, series, title, close_time, status):
    ex("""INSERT INTO markets(ticker,event,series,title,close_time,status,added_ts)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
          close_time=excluded.close_time, status=excluded.status, title=excluded.title""",
       (ticker, event, series, title, close_time, status, time.time()))


def insert_signal(s):
    cur = ex("""INSERT INTO signals(ts_ms,local_ts,market,event,series,dir,dl,levels,size,
                ref,ext,conf_lag_ms,late,outcome,detail,mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (s["ts_ms"], s["local_ts"], s["market"], s["event"], s["series"], s["dir"],
              s["dl"], s["levels"], s["size"], s["ref"], s["ext"], s.get("conf_lag_ms"),
              1 if s.get("late") else 0, s["outcome"], json.dumps(s.get("detail") or {}), _mode))
    return cur.lastrowid


def update_signal_outcome(signal_id, outcome, detail=None):
    ex("UPDATE signals SET outcome=?, detail=? WHERE id=?",
       (outcome, json.dumps(detail or {}), signal_id))


def insert_trade(t):
    cur = ex("""INSERT INTO trades(signal_id,market,event,series,dir,side,entry_ts,entry_px,
                size,cap,notional,book_at_entry,status,mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
             (t["signal_id"], t["market"], t["event"], t["series"], t["dir"], t["side"],
              t["entry_ts"], t["entry_px"], t["size"], t["cap"], t["notional"],
              json.dumps(t.get("book_at_entry") or {}), _mode))
    return cur.lastrowid


def close_trade(tid, exit_px, reason, gross, fees, net, mae, shadow_stop_px):
    ex("""UPDATE trades SET exit_ts=?, exit_px=?, exit_reason=?, gross=?, fees=?, net=?,
          mae=?, shadow_stop_px=?, status='closed' WHERE id=?""",
       (time.time(), exit_px, reason, gross, fees, net, mae, shadow_stop_px, tid))


def stats():
    closed = q("SELECT * FROM trades WHERE status='closed'")
    open_t = q("SELECT * FROM trades WHERE status='open'")
    sigs = q("SELECT outcome, COUNT(*) n FROM signals GROUP BY outcome")
    n = len(closed)
    net = sum(t["net"] or 0 for t in closed)
    wins = sum(1 for t in closed if (t["net"] or 0) > 0)
    fees = sum(t["fees"] or 0 for t in closed)
    # event-clustered bootstrap CI on net/fill
    ci = None
    by_ev = {}
    for t in closed:
        by_ev.setdefault(t["event"], []).append(t["net"] or 0)
    evs = list(by_ev.values())
    if len(evs) >= 5:
        rnd = random.Random(7)
        means = []
        for _ in range(2000):
            flat = [x for _ in range(len(evs)) for x in rnd.choice(evs)]
            means.append(sum(flat) / len(flat))
        means.sort()
        ci = [round(means[int(0.025 * len(means))], 2), round(means[int(0.975 * len(means))], 2)]
    # per-league
    lg = {}
    for t in closed:
        d = lg.setdefault(t["series"], {"n": 0, "net": 0.0, "wins": 0})
        d["n"] += 1
        d["net"] += t["net"] or 0
        d["wins"] += 1 if (t["net"] or 0) > 0 else 0
    # kill conditions
    lat = q("SELECT ms FROM latency WHERE kind='feed_lag' ORDER BY ts DESC LIMIT 500")
    lat_ms = sorted(x["ms"] for x in lat)
    p95 = lat_ms[int(0.95 * len(lat_ms))] if lat_ms else None
    n_conf = sum(s["n"] for s in sigs if s["outcome"] in
                 ("filled", "rejected_cap", "no_book", "killed"))
    kill = {
        "k1_fill_note": "requires recorded books vs print-model comparison (auto after 25+ signals)",
        "k2_ci": {"n_signals": n_conf, "needed": 50, "ci": ci,
                  "status": ("PASS" if ci and ci[0] > 0 else
                             "FAIL" if ci and ci[1] < 0 else "COLLECTING")},
        "k4_latency_p95_ms": p95,
        "k4_status": "OK" if (p95 is None or p95 < 250) else "BREACH",
    }
    return {"closed": n, "open": len(open_t), "net": round(net, 2),
            "net_per_fill": round(net / n, 2) if n else 0, "win_pct": round(wins / n * 100, 1) if n else 0,
            "fees": round(fees, 2), "ci95": ci, "signals": {s["outcome"]: s["n"] for s in sigs},
            "leagues": lg, "kill": kill}
