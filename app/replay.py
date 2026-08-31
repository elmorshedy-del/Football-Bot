"""DEMO mode: replays the real Espanyol vs Real Madrid tapes (Aug 22, 2026 —
the match that started this project) through the full live pipeline, with
synthetic order books labeled as such. No credentials required."""
import asyncio
import gzip
import json
import os
import time
from datetime import datetime, timezone

from . import config, store
from .books import Book
from .match_clock import ParsedClock

EVENT = "KXLALIGAGAME-26AUG22ESPRMA"
MKTS = {
    "KXLALIGAGAME-26AUG22ESPRMA-RMA": ("Real Madrid", "yes"),
    "KXLALIGAGAME-26AUG22ESPRMA-TIE": ("Tie", "no"),
    "KXLALIGAGAME-26AUG22ESPRMA-ESP": ("Espanyol", "no"),
}


def parse_us(s):
    dt = datetime.strptime(s.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S") \
        .replace(tzinfo=timezone.utc)
    frac = s.split(".")[1].rstrip("Z") if "." in s else "0"
    return int(dt.timestamp() * 1_000_000) + int((frac + "000000")[:6])


class DemoReplay:
    def __init__(self, engine):
        self.e = engine
        self.recent = {}   # ticker -> last few prints (robust mid for synth books)
        self.rows = []
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "demo_data")
        for tk in MKTS:
            p_gz = os.path.join(base, tk + ".jsonl.gz")
            p_txt = os.path.join(base, tk + ".jsonl")
            opener, path = (gzip.open, p_gz) if os.path.exists(p_gz) else (open, p_txt)
            with opener(path, "rt") as f:
                for line in f:
                    t = json.loads(line)
                    self.rows.append((parse_us(t["created_time"]), tk,
                                      float(t["yes_price_dollars"]) * 100,
                                      float(t["count_fp"]), t["taker_side"]))
        self.rows.sort()

    def synth_book(self, ticker, last, ts_ms=0, sz=1.0):
        """Synthetic-but-plausible book (labeled DEMO in UI). Anchored on the
        SIZE-WEIGHTED median of the last 2s of prints: big sweeps move it, small
        stale-liquidity cleanup prints cannot — mirroring a real resting book."""
        rec = self.recent.setdefault(ticker, [])
        rec.append((ts_ms, last, max(sz, 0.1)))
        cutoff = ts_ms - 2000
        while rec and rec[0][0] < cutoff:
            rec.pop(0)

        def swmed(rows):
            pts = sorted((p, s) for _, p, s in rows)
            half = sum(s for _, s in pts) / 2.0
            acc = 0.0
            for p, s in pts:
                acc += s
                if acc >= half:
                    return p
            return last

        # momentum-adaptive anchor: a real book reposts FORWARD after a sweep,
        # so when the last 500ms carries real size, anchor there; else calm 2s.
        recent5 = [r for r in rec if r[0] >= ts_ms - 500]
        if sum(r[2] for r in recent5) >= 300:
            mid = swmed(recent5)
        else:
            mid = swmed(rec)
        b = self.e.books.setdefault(ticker, Book())
        b.yes_bids = {max(0.5, mid - 1): 500.0, max(0.5, mid - 4): 900.0,
                      max(0.5, mid - 9): 1600.0}
        b.no_bids = {max(0.5, (100 - mid) - 1): 500.0, max(0.5, (100 - mid) - 4): 900.0,
                     max(0.5, (100 - mid) - 9): 1600.0}
        b.ok = True
        b.last_seq = None
        return b

    async def run(self):
        loop_n = 0
        for tk, (title, _res) in MKTS.items():
            self.e.register_market(tk, EVENT, "KXLALIGAGAME", f"Espanyol vs Real Madrid — {title}",
                                   None, leg_title=title,
                                   game_title="Espanyol vs Real Madrid")
        parsed = ParsedClock(
            provider_period="2nd", provider_minute=90, provider_stoppage=5,
            provider_clock="90+5′", provider_status="live",
            source_field="demo", raw_context={"source": "demo_replay_clock"},
        )
        row = self.e.clock_tracker.ingest_synthetic(
            EVENT, "demo-milestone", parsed, time.time(), "demo_replay_clock",
        )
        if row is not None:
            row_id = store.insert_match_clock(row)
            self.e.clock_tracker.latest[EVENT]["id"] = row_id
        while True:
            loop_n += 1
            self.e.demo_status = f"loop {loop_n} — Espanyol 1-1 Real Madrid, closing minutes"
            self.e.broadcast({"type": "log",
                              "text": f"🎬 DEMO loop {loop_n}: replaying Espanyol–Real Madrid "
                                      f"(real tape, {config.DEMO_SPEED:.0f}x speed) — late goal incoming…"})
            prev_us = None
            for (us, tk, px, sz, taker) in self.rows:
                if prev_us is not None:
                    dt = (us - prev_us) / 1e6 / config.DEMO_SPEED
                    if dt > 0:
                        await asyncio.sleep(min(dt, 2.0))
                prev_us = us
                wall = time.time()
                self.synth_book(tk, px, us // 1000, sz)
                self.e.process_trade(tk, us // 1000, px, sz, taker, wall)
                self.e.on_book(tk)
            # settle at true results
            self.e.broadcast({"type": "log", "text": "🏁 DEMO: full time — Espanyol 1–2 Real Madrid. Settling."})
            for tk, (_t, res) in MKTS.items():
                self.e.desk.settle_market(tk, res)
            # reset detector/book state between loops
            self.e.detector.markets.clear()
            self.e.pending.clear()
            if not config.DEMO_LOOP:
                self.e.demo_status = "demo finished"
                return
            await asyncio.sleep(6)
