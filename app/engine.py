"""Engine: discovery -> subscriptions -> books/recorder -> detector -> paper desk."""
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone

from . import config, store
from .books import Book
from .detector import Detector
from .kalshi import KalshiClient, KalshiWS
from .paper import PaperDesk
from .recorder import RawRecorder


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class Engine:
    def __init__(self, queue):
        self.q = queue
        self.mode = config.mode()
        self.cred_error = ""
        try:
            self.client = KalshiClient()
        except Exception as e:
            # Bad/mangled credentials must NOT take down the whole service.
            # Fall back to demo, keep the dashboard up, surface the reason.
            self.cred_error = str(e)
            self.mode = "demo"
            self.client = KalshiClient.__new__(KalshiClient)
            self.client._key = None
            import httpx as _httpx
            self.client._http = _httpx.AsyncClient(base_url=config.KALSHI_REST, timeout=30)
            self.client.n_requests = self.client.n_429 = self.client.n_retries = 0
        self.detector = Detector()
        self.desk = PaperDesk(self.broadcast)
        self.recorder = RawRecorder(self.on_recorder_error)
        self.books = {}
        self.meta = {}                 # ticker -> {event, series, title, close_time}
        self.event_markets = {}        # event -> [tickers]
        self.prices = {}               # ticker -> {last,bid,ask,spark:deque,dirty}
        self.pending = []              # candidates awaiting sibling confirmation
        self.feed_lag = deque(maxlen=600)
        self.ws = None
        self.ws_state = "init"
        self.started = time.time()
        self.n_trades = 0
        self.n_foreign = 0
        self.demo_status = ""

    # ---------- plumbing ----------
    def broadcast(self, msg):
        try:
            self.q.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def on_recorder_error(self, error):
        text = f"raw recorder unhealthy: {error}"
        try:
            store.log_event("recorder", text)
        except Exception:
            pass
        self.broadcast({"type": "log", "text": text})

    def register_market(self, ticker, event, series, title, close_time):
        self.meta[ticker] = {"event": event, "series": series, "title": title,
                             "close_time": close_time}
        self.event_markets.setdefault(event, [])
        if ticker not in self.event_markets[event]:
            self.event_markets[event].append(ticker)
        store.upsert_market(ticker, event, series, title, close_time, "open")

    def siblings(self, ticker):
        m = self.meta.get(ticker)
        if not m:
            return []
        return [t for t in self.event_markets.get(m["event"], []) if t != ticker]

    def is_late(self, ticker):
        """Late-game gate keyed on the SCHEDULED match end (expected_expiration_time).
        Production-legal: known before the match, unlike close_time which Kalshi
        pads days past the game."""
        m = self.meta.get(ticker)
        if self.mode == "demo":
            return True
        if not m or not m.get("close_time"):
            return False
        ct = parse_iso(m["close_time"])
        return ct is not None and (ct - time.time()) <= config.LATE_WINDOW_MIN * 60

    def price_state(self, ticker):
        if ticker not in self.prices:
            self.prices[ticker] = {"last": None, "bid": None, "ask": None,
                                   "spark": deque(maxlen=180), "dirty": False}
        return self.prices[ticker]

    # ---------- ws routing ----------
    def handle_ws(self, msg, wall, mono):
        t = msg.get("type")
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker")
        # defense-in-depth: ignore anything we didn't explicitly subscribe to
        # (a filterless subscription upstream becomes an all-market firehose)
        if ticker and ticker not in self.meta and self.mode == "live":
            self.n_foreign += 1
            return
        if t in ("orderbook_snapshot", "orderbook_delta", "trade", "market_lifecycle_v2"):
            self.recorder.write(msg, wall, mono)
        if t == "orderbook_snapshot":
            b = self.books.setdefault(ticker, Book())
            b.apply_snapshot(body, msg.get("seq"))
            self.on_book(ticker)
        elif t == "orderbook_delta":
            b = self.books.setdefault(ticker, Book())
            if not b.apply_delta(body, msg.get("seq")):
                if self.ws:
                    asyncio.get_event_loop().create_task(self.ws.request_snapshot(ticker))
            else:
                self.on_book(ticker)
        elif t == "trade":
            ts_ms = body.get("ts_ms") or (body.get("ts", 0) * 1000)
            px = float(body.get("yes_price_dollars", 0)) * 100
            sz = float(body.get("count_fp") or 0)
            lag = wall * 1000 - ts_ms
            self.feed_lag.append(lag)
            if self.n_trades % 20 == 0:
                store.add_latency("feed_lag", lag)
            self.process_trade(ticker, int(ts_ms), px, sz, body.get("taker_side"), wall)
        elif t == "market_lifecycle_v2":
            res = body.get("settled_result") or body.get("result")
            if res in ("yes", "no"):
                self.desk.settle_market(ticker, res)

    def on_book(self, ticker, synthetic=False):
        b = self.books.get(ticker)
        if not b:
            return
        ps = self.price_state(ticker)
        ps["bid"], ps["ask"] = b.best_yes_bid(), b.best_yes_ask()
        ps["dirty"] = True
        self.desk.on_book(ticker, b)

    # ---------- signal flow ----------
    def process_trade(self, ticker, ts_ms, px, sz, taker, wall):
        self.n_trades += 1
        ps = self.price_state(ticker)
        ps["last"] = px
        ps["spark"].append([int(wall * 1000), round(px, 1)])
        ps["dirty"] = True
        self.broadcast({"type": "tape", "ticker": ticker, "px": px, "sz": round(sz, 1),
                        "taker": taker, "ts_ms": ts_ms})
        cand = self.detector.on_trade(ticker, ts_ms, px, sz, taker)
        # re-check pending candidates whose siblings include this market
        if self.pending:
            still = []
            for p in self.pending:
                if ticker in p["siblings"]:
                    ok, lag = self.detector.confirm(p["cand"], p["siblings"])
                    if ok:
                        self.act_on_signal(p["cand"], lag)
                        continue
                if time.time() < p["deadline"]:
                    still.append(p)
                else:
                    self.record_signal(p["cand"], None, "unconfirmed")
            self.pending = still
        if cand:
            sibs = self.siblings(ticker)
            ok, lag = self.detector.confirm(cand, sibs)
            if ok:
                self.act_on_signal(cand, lag)
            elif sibs:
                self.pending.append({"cand": cand, "siblings": sibs,
                                     "deadline": time.time() + 0.2})
            else:
                self.record_signal(cand, None, "unconfirmed")

    def record_signal(self, cand, lag, outcome, announce=True):
        m = self.meta.get(cand["ticker"], {})
        sid = store.insert_signal({
            "ts_ms": cand["ts_ms"], "local_ts": cand["local_ts"], "market": cand["ticker"],
            "event": m.get("event", "?"), "series": m.get("series", "?"),
            "dir": cand["dir"], "dl": cand["dl"], "levels": cand["levels"],
            "size": cand["size"], "ref": cand["ref"], "ext": cand["ext"],
            "conf_lag_ms": lag, "late": self.is_late(cand["ticker"]), "outcome": outcome,
            "detail": {},
        })
        if announce:
            self._announce_signal(sid, cand, lag, outcome)
        return sid

    def _announce_signal(self, sid, cand, lag, outcome):
        m = self.meta.get(cand["ticker"], {})
        self.broadcast({"type": "signal", "signal": {
            "id": sid, "market": cand["ticker"], "series": m.get("series", "?"),
            "dir": cand["dir"], "dl": cand["dl"], "levels": cand["levels"],
            "size": cand["size"], "ref": cand["ref"], "ext": cand["ext"],
            "conf_lag_ms": lag, "outcome": outcome, "ts": time.time()}})
        icon = {"filled": "🎯", "rejected_cap": "🧢", "unconfirmed": "👻"}.get(outcome, "•")
        store.log_event("signal", f"{icon} {outcome.upper()} {cand['ticker']} "
                                  f"dl={cand['dl']} lv={cand['levels']} "
                                  f"conf={f'{lag:+.0f}ms' if lag is not None else '—'}")

    def act_on_signal(self, cand, lag):
        decision_start = time.monotonic()
        if config.LATE_ONLY and not self.is_late(cand["ticker"]):
            self.record_signal(cand, lag, "not_late")
            return
        m = self.meta.get(cand["ticker"], {"event": "?", "series": "?"})
        book = self.books.get(cand["ticker"])
        sid = self.record_signal(cand, lag, "executing", announce=False)
        try:
            outcome = self.desk.try_enter(sid, cand, m, book)
            detail = {}
        except Exception as exc:
            outcome = "execution_error"
            detail = {"error": f"{type(exc).__name__}: {exc}"}
        store.add_latency("decision_ms", (time.monotonic() - decision_start) * 1000)
        store.update_signal_outcome(sid, outcome, detail)
        self._announce_signal(sid, cand, lag, outcome)
        if outcome == "filled":
            self.detector.state(cand["ticker"]).last_entry_ms = cand["ts_ms"]

    # ---------- background tasks ----------
    async def discovery_task(self):
        while True:
            try:
                want = set()
                now = time.time()
                for series in config.SOCCER_SERIES:
                    try:
                        resp = await self.client.get("/markets", series_ticker=series,
                                                     status="open", limit=1000)
                    except Exception:
                        continue
                    for mkt in resp.get("markets") or []:
                        # KEY: expected_expiration_time = scheduled match end.
                        # close_time is padded ~3 days past the game — never use it
                        # for match timing (verified Aug 2026, e.g. UCL close_time
                        # = game day + 3 while expected_expiration = final whistle).
                        exp = parse_iso(mkt.get("expected_expiration_time") or "") \
                            or parse_iso(mkt.get("close_time") or "")
                        if exp is None:
                            continue
                        if -config.DROP_AFTER_CLOSE_MIN * 60 < exp - now < \
                                config.SUBSCRIBE_BEFORE_CLOSE_MIN * 60:
                            tk = mkt["ticker"]
                            self.register_market(tk, mkt.get("event_ticker", "?"), series,
                                                 mkt.get("title") or mkt.get("subtitle") or tk,
                                                 mkt.get("expected_expiration_time") or mkt.get("close_time"))
                            want.add(tk)
                if self.ws:
                    await self.ws.set_markets(want)
                self.broadcast({"type": "log", "text":
                                f"discovery: watching {len(want)} markets "
                                f"({len(set(self.meta[t]['event'] for t in want if t in self.meta))} matches)"})
            except Exception as e:
                self.broadcast({"type": "log", "text": f"discovery error: {e!r}"})
            await asyncio.sleep(config.DISCOVERY_INTERVAL_S)

    async def settle_poll_task(self):
        """Fallback settlement detection for open paper positions."""
        while True:
            await asyncio.sleep(30)
            if self.mode != "live":
                continue
            tickers = {p.market for p in self.desk.positions.values()}
            for tk in tickers:
                try:
                    r = await self.client.get(f"/markets/{tk}")
                    mkt = r.get("market") or {}
                    if mkt.get("result") in ("yes", "no"):
                        self.desk.settle_market(tk, mkt["result"])
                except Exception:
                    pass

    async def periodic_task(self):
        last_stats = 0.0
        while True:
            await asyncio.sleep(config.BROADCAST_COALESCE_MS / 1000.0)
            self.desk.check_timeouts()
            # expire stale pendings
            now = time.time()
            for p in [p for p in self.pending if now >= p["deadline"]]:
                self.record_signal(p["cand"], None, "unconfirmed")
            self.pending = [p for p in self.pending if now < p["deadline"]]
            # coalesced price updates
            dirty = []
            for tk, ps in self.prices.items():
                if ps["dirty"]:
                    ps["dirty"] = False
                    m = self.meta.get(tk, {})
                    dirty.append({"ticker": tk, "event": m.get("event"),
                                  "series": m.get("series"), "last": ps["last"],
                                  "bid": ps["bid"], "ask": ps["ask"],
                                  "late": self.is_late(tk)})
            if dirty:
                self.broadcast({"type": "prices", "prices": dirty})
            if now - last_stats > 5:
                last_stats = now
                lat = sorted(self.feed_lag)
                self.broadcast({"type": "stats", "stats": store.stats(),
                                "status": self.status()})

    def status(self):
        lat = sorted(self.feed_lag)
        return {"mode": self.mode, "ws": self.ws_state, "uptime_s": int(time.time() - self.started),
                "markets": len(self.meta), "matches": len(self.event_markets),
                "trades_seen": self.n_trades, "recorded": self.recorder.total,
                "recorder": self.recorder.status(),
                "kill": self.desk.kill, "open_positions": len(self.desk.positions),
                "demo": self.demo_status, "cred_error": self.cred_error,
                "foreign_dropped": self.n_foreign,
                "feed_lag_p50": round(lat[len(lat) // 2], 1) if lat else None,
                "feed_lag_p95": round(lat[int(0.95 * len(lat))], 1) if len(lat) > 20 else None}

    async def start(self):
        store.set_mode(self.mode)
        if self.mode == "live":
            store.purge_non_live()  # clean demo/legacy rows so live P&L starts fresh
            self.ws = KalshiWS(self.handle_ws, lambda s: setattr(self, "ws_state", s))
            asyncio.create_task(self.ws.run())
            asyncio.create_task(self.discovery_task())
            asyncio.create_task(self.settle_poll_task())
            store.log_event("sys", "engine started in LIVE mode")
        else:
            from .replay import DemoReplay
            asyncio.create_task(DemoReplay(self).run())
            self.ws_state = "demo"
            store.log_event("sys", "engine started in DEMO mode (replaying real Madrid tapes)")
        asyncio.create_task(self.periodic_task())
