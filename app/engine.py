"""Engine: discovery -> subscriptions -> books/recorder -> detector -> paper desk."""
import asyncio
import json
import re
import time
from collections import deque
from datetime import datetime, timezone

from . import config, store
from .books import Book
from .detector import Detector
from .goal_latency import GoalLatencyObserver
from .kalshi import KalshiClient, KalshiWS
from .late_score_sleeve import PriceOnlyLateScoreSleeve
from .match_clock import MatchClockGate, MatchClockTracker, unusable_stamp
from .paper import PaperDesk
from .recorder import RawRecorder


GATE_A_STRATEGY = "gate_a"
PRICE_ONLY_STRATEGY = "price_only_late_score"
# Research observations only.  Never traded, never confirmed, and excluded from
# every sleeve aggregate and kill-condition count.
SUBTHRESHOLD_STRATEGY = "subthreshold_observer"


def market_game_title(market):
    """Derive an additive matchup label from provider text without changing it."""
    title = (market.get("title") or market.get("subtitle") or "").strip()
    if " vs " in title.lower():
        return re.sub(r"\s+Winner\?\s*$", "", title, flags=re.IGNORECASE)
    rules = market.get("rules_secondary") or market.get("rules_primary") or ""
    match = re.search(
        r"(?:refers to|wins) the (.+?\s+vs\s+.+?) professional .*?soccer game",
        rules,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None



def _clock_coverage_check(coverage):
    """Clock coverage as a runtime health check.

    A watched match with no mapping, a stale or missing clock, or accumulated
    88-gate misses means the price-only study is not collecting, which is a
    runtime fault and not merely evidence that is still gathering.
    """
    watched = int(coverage.get("watched") or 0)
    mapped = int(coverage.get("mapped") or 0)
    present = int(coverage.get("clock_present") or 0)
    fresh = int(coverage.get("clock_fresh") or 0)
    stale = int(coverage.get("clock_stale") or 0)
    misses = int(
        coverage.get("clock_gate_candidate_misses_total")
        or coverage.get("clock_gate_candidate_misses") or 0
    )
    faults = list(coverage.get("faults") or [])
    mapping_errors = list(coverage.get("mapping_errors") or [])
    events = coverage.get("events")
    problems = []
    if events is None:
        # Legacy count-only coverage.
        if watched and not mapped:
            problems.append("no watched match is mapped to a live clock")
        if stale:
            problems.append(f"{stale} clock(s) stale")
        if mapping_errors:
            problems.append(f"{len(mapping_errors)} mapping error(s)")
        if faults and not problems:
            problems.append(f"{len(faults)} clock fault(s)")
    else:
        # Health is decided from per-event CURRENT state.  Count arithmetic
        # reported a mapped pre-match fixture as a missing clock, and a single
        # cumulative miss as a permanent fault that recovery could never clear.
        if watched and not mapped:
            problems.append("no watched match is mapped to a live clock")
        reasons = {}
        for row in events:
            if row.get("state") == "fault" and row.get("current_fault"):
                reasons.setdefault(row["current_fault"], 0)
                reasons[row["current_fault"]] += 1
        for reason, count in sorted(reasons.items()):
            problems.append(f"{count} clock {reason.replace('_', ' ')}")
    # Cumulative evidence is reported but never blocks recovery.
    notes = []
    if misses:
        notes.append(f"{misses} 88-gate candidate miss(es) recorded")
    waiting = int(coverage.get("clock_waiting") or 0)
    if waiting and not problems:
        notes.append(f"{waiting} match(es) waiting for kickoff")
    status = "; ".join(problems + notes) if (problems or notes) else "observing"
    return {
        "healthy": not problems,
        "status": status,
        "watched": watched, "mapped": mapped, "clock_present": present,
        "clock_fresh": fresh, "clock_stale": stale,
        "clock_waiting": waiting,
        "clock_gate_candidate_misses": misses,
        "clock_gate_candidate_misses_total": misses,
        "faults": len(faults), "mapping_errors": len(mapping_errors),
        "events": events or [],
    }


class Engine:
    def __init__(self, queue):
        self.q = queue
        self._signal_paths = deque()
        # Current fault, latched until a finalization actually succeeds.  A
        # historical entry in `errors` decays; this does not.
        self.signal_path_fault = None
        self._signal_path_failed_owners = set()
        self.errors = deque(maxlen=50)
        self._last_error_key = None
        self._last_error_ts = 0.0
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
        self.detector = Detector(subthreshold_sink=self.record_subthreshold)
        self.late_score_sleeve = PriceOnlyLateScoreSleeve()
        self.desk = PaperDesk(
            self.broadcast, self.on_paper_entry_result, error_result=self._record_error,
        )
        self.recorder = RawRecorder(self.on_recorder_error)
        self.books = {}
        self.meta = {}                 # ticker -> {event, series, title, close_time}
        self.fee_schedules = {}        # series -> (fee_type, fee_multiplier)
        self.event_markets = {}        # event -> [tickers]
        self.watched_events = set()    # current discovery window only
        self.prices = {}               # ticker -> {last,bid,ask,spark:deque,dirty}
        self.pending = []              # candidates awaiting sibling confirmation
        self.last_entry_ms = {}        # (strategy, ticker) -> exchange signal timestamp
        self.feed_lag = deque(maxlen=600)
        self.market_observations = {}  # event -> recent locally timestamped price changes
        self._last_market_state = {}   # (kind, ticker) -> tuple, suppress unchanged frames
        self.goal_latency = None
        self.clock_tracker = MatchClockTracker()
        self.ws = None
        self.ws_state = "init"
        self.started = time.time()
        self.n_trades = 0
        self.n_foreign = 0
        self.demo_status = ""
        if self.cred_error:
            self._record_error("credentials", self.cred_error)

    # ---------- plumbing ----------
    def broadcast(self, msg):
        try:
            self.q.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def _record_error(self, component, error):
        now = time.time()
        message = str(error)
        key = component, message
        # Repeating network failures are represented without flooding the panel or DB.
        if key == self._last_error_key and now - self._last_error_ts < 30.0:
            return
        self._last_error_key = key
        self._last_error_ts = now
        row = {
            "ts": now,
            "component": component,
            "message": message,
        }
        self.errors.append(row)
        try:
            store.log_event("error", f"{component}: {message}")
        except Exception:
            pass
        self.broadcast({"type": "system_error", "error": row})

    def on_recorder_error(self, error):
        text = f"raw recorder unhealthy: {error}"
        self._record_error("recorder", error)
        try:
            store.log_event("recorder", text)
        except Exception:
            pass
        self.broadcast({"type": "log", "text": text})

    def on_ws_state(self, state):
        self.ws_state = state
        if str(state).startswith("disconnected"):
            self._record_error("websocket", state)

    def register_market(self, ticker, event, series, title, close_time,
                        fee_type="quadratic", fee_multiplier=1.0, leg_title=None,
                        game_title=None):
        self.meta[ticker] = {"event": event, "series": series, "title": title,
                             "close_time": close_time, "fee_type": fee_type,
                             "fee_multiplier": fee_multiplier,
                             "leg_title": leg_title, "game_title": game_title}
        self.event_markets.setdefault(event, [])
        if ticker not in self.event_markets[event]:
            self.event_markets[event].append(ticker)
        store.upsert_market(
            ticker, event, series, title, close_time, "open", game_title, leg_title,
        )

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

    def is_sleeve_window(self, ticker):
        """Approximate minute 88 from scheduled expiration, without live scores."""
        if self.mode == "demo":
            return True
        m = self.meta.get(ticker)
        if not m or not m.get("close_time"):
            return False
        ct = parse_iso(m["close_time"])
        if ct is None:
            return False
        until_expiry = ct - time.time()
        return (-config.SLEEVE_AFTER_EXPIRY_MIN * 60.0 <= until_expiry <=
                config.SLEEVE_START_BEFORE_EXPIRY_MIN * 60.0)

    def _observe_sleeve(self, ticker, observed_ms):
        if config.PRICE_ONLY_SLEEVE_MODE not in {"enforce", "parallel"}:
            return
        m = self.meta.get(ticker)
        if not m:
            return
        event = m["event"]
        self.late_score_sleeve.observe(
            event, self.event_markets.get(event, ()), self.meta, self.books, observed_ms,
            changed_ticker=ticker,
        )

    def price_state(self, ticker):
        if ticker not in self.prices:
            self.prices[ticker] = {"last": None, "bid": None, "ask": None,
                                   "spark": deque(maxlen=180), "dirty": False}
        return self.prices[ticker]

    def _record_market_observation(self, ticker, kind, wall, mono):
        """Keep an in-memory arrival timeline; never calls strategy code or SQLite."""
        meta = self.meta.get(ticker)
        if not meta:
            return
        price = self.price_state(ticker)
        state = ((price.get("bid"), price.get("ask")) if kind == "book"
                 else (price.get("last"),))
        key = kind, ticker
        previous = self._last_market_state.get(key)
        self._last_market_state[key] = state
        if previous == state:
            return
        # An initial order-book snapshot is baseline, not a market movement.
        # The first observed trade is a genuine timestamped market event.
        if previous is None and kind == "book":
            return
        event = meta["event"]
        observations = self.market_observations.setdefault(event, deque(maxlen=4000))
        observations.append({
            "wall": wall,
            "mono": mono,
            "kind": kind,
            "ticker": ticker,
            "bid": price.get("bid"),
            "ask": price.get("ask"),
            "last": price.get("last"),
        })

    def market_window(self, event, anchor_mono, before_s, after_s):
        lower, upper = anchor_mono - before_s, anchor_mono + after_s
        return [row for row in self.market_observations.get(event, ())
                if lower <= row["mono"] <= upper]

    # ---------- ws routing ----------
    def handle_ws(self, msg, wall, mono):
        t = msg.get("type")
        body = msg.get("msg") or {}
        if t == "orderbook_gap":
            tickers = body.get("market_tickers") or list(self.books)
            self.desk.invalidate_books(tickers)
            for tk in tickers:
                book = self.books.get(tk)
                if book is not None:
                    book.ok = False
            store.log_event(
                "book",
                f"sequence gap sid={body.get('sid')} expected={body.get('expected')} "
                f"received={body.get('received')}; awaiting fresh snapshots",
            )
            return
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
            self.desk.apply_book_snapshot(ticker, b)
            self.on_book(ticker)
            self._record_market_observation(ticker, "book", wall, mono)
        elif t == "orderbook_delta":
            b = self.books.setdefault(ticker, Book())
            if not b.apply_delta(body, msg.get("seq"), sequence_validated=True):
                self.desk.invalidate_books([ticker])
                if self.ws:
                    asyncio.get_event_loop().create_task(self.ws.request_snapshot(ticker))
            else:
                self.desk.apply_book_delta(ticker, body, msg.get("seq"))
                self.on_book(ticker)
                self._record_market_observation(ticker, "book", wall, mono)
        elif t == "trade":
            ts_ms = body.get("ts_ms") or (body.get("ts", 0) * 1000)
            px = float(body.get("yes_price_dollars", 0)) * 100
            sz = float(body.get("count_fp") or 0)
            lag = wall * 1000 - ts_ms
            self.feed_lag.append(lag)
            if self.n_trades % 20 == 0:
                store.add_latency("feed_lag", lag)
            self.process_trade(ticker, int(ts_ms), px, sz, body.get("taker_side"), wall)
            self._record_market_observation(ticker, "trade", wall, mono)
        elif t == "market_lifecycle_v2":
            res = body.get("settled_result") or body.get("result")
            if res in ("yes", "no"):
                self.desk.settle_market(ticker, res)

    def _watch_signal_forward(self, sid, cand, outcome):
        """Track the held-side price for a bounded window after any signal.

        Without this a declined signal is a dead record: the study can see that
        the sleeve said no, but never whether saying no was right.  Every
        signal, accepted or declined, becomes a labelled observation.
        """
        if not config.SIGNAL_PATH_WINDOW_S or sid is None:
            return
        ticker = cand.get("ticker")
        meta = self.meta.get(ticker, {})
        now = cand.get("local_ts") or time.time()
        side = "yes" if cand.get("dir", 1) >= 0 else "no"
        self._signal_paths.append({
            "signal_id": sid, "market": ticker, "event": meta.get("event", "?"),
            "side": side, "strategy": cand.get("strategy") or "detector",
            "anchor_ts": now, "expires_at": now + config.SIGNAL_PATH_WINDOW_S,
            "outcome": outcome, "last": None, "rows": [], "dropped": 0, "total": 0,
        })
        self._evict_signal_paths()

    def _record_signal_paths(self, ticker, book, now):
        for watch in self._signal_paths:
            if watch.get("retry_only"):
                continue
            if watch["market"] != ticker:
                continue
            try:
                ladder = book.bid_ladder(watch["side"])
            except Exception:
                ladder = []
            if ladder:
                bid, bid_size = ladder[0]
                availability = "quote"
            else:
                # A no-ladder observation is evidence, not a hole.  Skipping it
                # let the summary bridge an outage the decline never traded
                # through.  One gap row per outage; repeats are suppressed by
                # the unchanged-signature check below.
                bid, bid_size = None, None
                availability = "gap"
            signature = (bid, bid_size, availability)
            if signature == watch["last"]:
                continue
            if availability == "gap" and watch["last"] is None:
                # Never open a path with a gap: there is no availability to end.
                continue
            watch["last"] = signature
            # One slot is reserved so a terminal/final row always fits.
            if watch.get("total", 0) >= store.BID_PATH_MAX_SAMPLES - 1:
                watch["dropped"] += 1
                continue
            watch["total"] = watch.get("total", 0) + 1
            watch["rows"].append({
                "kind": "decline", "trade_id": None, "signal_id": watch["signal_id"],
                "event": watch["event"], "market": ticker, "side": watch["side"],
                "strategy": watch["strategy"], "anchor_ts": watch["anchor_ts"],
                "dt_ms": round((now - watch["anchor_ts"]) * 1000.0, 1),
                "bid": bid, "bid_size": bid_size, "exec_px": None, "qty": None,
                # Sequence keys make a retry exactly-once under the partial
                # unique index; without them a retry duplicated every row.
                "sample_seq": watch["total"], "availability": availability,
                "terminal": 0,
            })

    def _mark_signal_path_failure(self, signal_id, exc):
        self._signal_path_failed_owners.add(signal_id)
        self.signal_path_fault = "signal_path_persistence_failed"
        self._record_error("signal_path", exc)

    def _mark_signal_path_success(self, signal_id):
        self._signal_path_failed_owners.discard(signal_id)
        if not self._signal_path_failed_owners:
            self.signal_path_fault = None

    def _flush_signal_path(self, watch, final=False):
        """Incremental flush. Returns True when the buffer is durable."""
        if final:
            return self._finalize_signal_path(watch)
        if not watch["rows"]:
            return True
        rows = list(watch["rows"])
        try:
            written = store.insert_bid_path(rows)
        except Exception as exc:
            # Keep the rows and the owning watch. A sequence conflict is a
            # current health fault until this exact owner later commits.
            self._mark_signal_path_failure(watch["signal_id"], exc)
            return False
        if isinstance(written, int) and written < len(rows):
            exc = RuntimeError(
                f"signal {watch['signal_id']}: short path persistence "
                f"{written}/{len(rows)}"
            )
            self._mark_signal_path_failure(watch["signal_id"], exc)
            return False
        watch["rows"] = watch["rows"][len(rows):]
        self._mark_signal_path_success(watch["signal_id"])
        return True

    def _finalize_signal_path(self, watch, incomplete_reason=None):
        """Persist remaining rows, summary and the durable finalized marker.

        One transaction.  The caller releases the watch only when this returns
        True, so a failed write always leaves an owner to retry.  Recovery
        metadata belongs to the watch owner as well: a failed first attempt
        must not lose the reason when the same watch retries later.
        """
        if incomplete_reason is None:
            incomplete_reason = watch.get("incomplete_reason")
        try:
            store.finalize_signal_path(
                watch["signal_id"],
                path_rows=list(watch["rows"]),
                truncated=bool(watch.get("dropped")),
                dropped_samples=watch.get("dropped", 0),
                incomplete_reason=incomplete_reason,
            )
        except Exception as exc:
            self._mark_signal_path_failure(watch["signal_id"], exc)
            return False
        watch["rows"] = []
        self._mark_signal_path_success(watch["signal_id"])
        return True

    def _release_finalized(self, index=0):
        """Finalize the watch at `index`, popping it only after it commits."""
        watch = self._signal_paths[index]
        if not self._finalize_signal_path(watch):
            return False
        del self._signal_paths[index]
        return True

    def _expire_signal_paths(self, now):
        # Peek, finalize, then pop.  popleft() before persisting destroyed the
        # only owner of the buffered rows when the write failed.
        while self._signal_paths and self._signal_paths[0]["expires_at"] <= now:
            if not self._release_finalized(0):
                return

    def _evict_signal_paths(self):
        """Drop the oldest watches over the tracking cap, ownership-safely."""
        while len(self._signal_paths) > config.SIGNAL_PATH_MAX_TRACKED:
            if not self._release_finalized(0):
                return

    def rebuild_signal_paths(self):
        """Finalize watches whose process died inside the observation window.

        Their in-memory tail is unrecoverable, so the path is labelled
        incomplete rather than presented as a complete forward observation.
        """
        rebuilt = 0
        for row in store.unfinalized_signal_paths():
            watch = {
                "signal_id": row["id"], "rows": [], "dropped": 0,
                "market": row.get("market"), "event": row.get("event"),
                "expires_at": 0.0, "retry_only": True,
                "incomplete_reason": "in_memory_tail_lost_on_restart",
            }
            if self._finalize_signal_path(watch):
                rebuilt += 1
            else:
                # Startup failure must retain an owned retry object.  A local
                # dictionary that falls out of scope cannot ever recover.
                self._signal_paths.append(watch)
        return rebuilt

    def on_book(self, ticker, synthetic=False):
        b = self.books.get(ticker)
        if not b:
            return
        ps = self.price_state(ticker)
        ps["bid"], ps["ask"] = b.best_yes_bid(), b.best_yes_ask()
        ps["dirty"] = True
        now = time.time()
        self._observe_sleeve(ticker, now * 1000.0)
        self._record_signal_paths(ticker, b, now)
        self._expire_signal_paths(now)
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
        self._observe_sleeve(ticker, wall * 1000.0)
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

    def record_subthreshold(self, observation):
        """Persist one near-miss burst as a research observation.

        Deliberately leaner than `record_signal`: no sibling confirmation, no
        strategy dispatch, no dashboard broadcast, no forward-path watch, and
        no clock-gate miss accounting.  A near miss is evidence about where the
        detector thresholds sit, not a signal, and it must not consume a
        trading budget or move a health indicator.  The clock is stamped
        read-only, because whether a near miss happened at minute 88 is the
        first thing any re-fit will ask.
        """
        ticker = observation["ticker"]
        meta = self.meta.get(ticker, {})
        event = meta.get("event", "?")
        try:
            stamp = self.clock_tracker.stamp(event, observation.get("local_ts"))
        except Exception:
            stamp = None
        try:
            store.insert_signal({
                "ts_ms": observation["ts_ms"], "local_ts": observation["local_ts"],
                "market": ticker, "event": event, "series": meta.get("series", "?"),
                "dir": observation["dir"], "dl": observation["dl"],
                "levels": observation["levels"], "size": observation["size"],
                "ref": observation["ref"], "ext": observation["ext"],
                "conf_lag_ms": None, "late": self.is_late(ticker),
                "outcome": "subthreshold",
                "detail": {
                    "strategy": SUBTHRESHOLD_STRATEGY,
                    "below": observation.get("below") or [],
                    "trading_floor": {
                        "dl_min": config.DL_MIN,
                        "levels_min": config.LEVELS_MIN,
                        "size_min": config.SIZE_MIN,
                    },
                },
                "match_clock_snapshot": stamp,
                # No forward path: these are numerous by design, and each watch
                # costs a tracking slot and up to BID_PATH_MAX_SAMPLES rows.
                "forward_path_started_ts": None,
            })
        except Exception as exc:
            self._record_error("subthreshold", exc)

    def record_signal(self, cand, lag, outcome, announce=True):
        m = self.meta.get(cand["ticker"], {})
        event = m.get("event", "?")
        signal_ts = cand.get("local_ts")
        if self.mode == "demo" and event not in self.clock_tracker.latest:
            stamp = unusable_stamp(
                event, signal_ts, "demo_mode_no_match_clock",
                gate_outcome="clock_demo", source="demo_replay",
            )
        else:
            stamp = self.clock_tracker.stamp(event, signal_ts)
            if not stamp.get("usable_for_88_gate"):
                reason = stamp.get("unusable_reason")
                if reason in {
                    "unmapped", "stale", "malformed", "missing_clock", "unpersisted",
                }:
                    # Count it as cumulative evidence only.  Latching it as a
                    # current fault made the banner unrecoverable after one miss.
                    self.clock_tracker.clock_gate_candidate_misses += 1
                    self._record_error(
                        "match_clock",
                        f"{event}: clock stamp unusable ({reason})",
                    )
        sid = store.insert_signal({
            "ts_ms": cand["ts_ms"], "local_ts": cand["local_ts"], "market": cand["ticker"],
            "event": event, "series": m.get("series", "?"),
            "dir": cand["dir"], "dl": cand["dl"], "levels": cand["levels"],
            "size": cand["size"], "ref": cand["ref"], "ext": cand["ext"],
            "conf_lag_ms": lag, "late": self.is_late(cand["ticker"]), "outcome": outcome,
            "detail": cand.get("detail") or {},
            "match_clock_snapshot": stamp,
            "forward_path_started_ts": (
                cand.get("local_ts") or time.time()
                if config.SIGNAL_PATH_WINDOW_S else None
            ),
        })
        age = stamp.get("age_ms")
        if isinstance(age, (int, float)):
            store.add_latency("match_clock_age_ms", age)
        self._watch_signal_forward(sid, cand, outcome)
        if announce:
            self._announce_signal(sid, cand, lag, outcome)
        return sid

    def _announce_signal(self, sid, cand, lag, outcome):
        m = self.meta.get(cand["ticker"], {})
        self.broadcast({"type": "signal", "signal": {
            "id": sid, "market": cand["ticker"], "series": m.get("series", "?"),
            "dir": cand["dir"], "dl": cand["dl"], "levels": cand["levels"],
            "size": cand["size"], "ref": cand["ref"], "ext": cand["ext"],
            "conf_lag_ms": lag, "outcome": outcome, "ts": time.time(),
            "strategy": cand.get("strategy")}})
        icon = {"filled": "🎯", "rejected_cap": "🧢", "unconfirmed": "👻"}.get(outcome, "•")
        store.log_event("signal", f"{icon} {outcome.upper()} {cand['ticker']} "
                                  f"dl={cand['dl']} lv={cand['levels']} "
                                  f"conf={f'{lag:+.0f}ms' if lag is not None else '—'}")

    @staticmethod
    def _strategy_candidate(cand, strategy, sleeve=None):
        tagged = dict(cand)
        tagged["strategy"] = strategy
        tagged["detail"] = {"strategy": strategy}
        if sleeve is not None:
            tagged["sleeve"] = dict(sleeve)
            tagged["detail"]["sleeve"] = dict(sleeve)
        return tagged

    def _is_strategy_locked(self, cand):
        entries = getattr(self, "last_entry_ms", None)
        if entries is None:
            self.last_entry_ms = entries = {}
        key = (cand.get("strategy") or GATE_A_STRATEGY, cand["ticker"])
        previous = entries.get(key, -1e18)
        return cand["ts_ms"] - previous < config.LOCKOUT_S * 1000.0

    def _remember_fill(self, cand):
        if not hasattr(self, "last_entry_ms"):
            self.last_entry_ms = {}
        key = (cand.get("strategy") or GATE_A_STRATEGY, cand["ticker"])
        self.last_entry_ms[key] = cand["ts_ms"]

    def _execute_strategy_candidate(self, cand, lag, decision_start):
        if self._is_strategy_locked(cand):
            self.record_signal(cand, lag, "strategy_lockout")
            return "strategy_lockout"
        m = self.meta.get(cand["ticker"], {"event": "?", "series": "?"})
        if config.PAPER_EXECUTION_V2:
            sid = self.record_signal(cand, lag, "queued")
            self.desk.queue_enter(sid, cand, m)
            store.add_latency("decision_ms", (time.monotonic() - decision_start) * 1000)
            return "queued"
        book = self.books.get(cand["ticker"])
        sid = self.record_signal(cand, lag, "executing", announce=False)
        try:
            outcome = self.desk.try_enter(sid, cand, m, book)
            detail = dict(cand.get("detail") or {})
        except Exception as exc:
            outcome = "execution_error"
            detail = dict(cand.get("detail") or {})
            detail["error"] = f"{type(exc).__name__}: {exc}"
        store.add_latency("decision_ms", (time.monotonic() - decision_start) * 1000)
        store.update_signal_outcome(sid, outcome, detail)
        self._announce_signal(sid, cand, lag, outcome)
        if outcome == "filled":
            self._remember_fill(cand)
        return outcome

    def _run_gate_a(self, cand, lag, decision_start):
        tagged = self._strategy_candidate(cand, GATE_A_STRATEGY)
        if config.LATE_ONLY and not self.is_late(tagged["ticker"]):
            self.record_signal(tagged, lag, "not_late")
            return "not_late"
        return self._execute_strategy_candidate(tagged, lag, decision_start)

    def _clock_gate_for(self, ticker, signal_ts):
        """Evaluate the persisted live clock; expected expiration is not consulted."""
        event = self.meta.get(ticker, {}).get("event", "?")
        stamp = self.clock_tracker.stamp(event, signal_ts)
        return stamp, MatchClockGate(stamp).evaluate()

    def _run_price_only(self, cand, lag, decision_start):
        stamp, gate = self._clock_gate_for(cand["ticker"], cand.get("local_ts"))
        if not gate["accepted"]:
            sleeve = {
                "strategy": "price_only_late_score_v1",
                "feed_independent": True,
                "decision": gate["outcome"],
                "match_clock_gate": {
                    "outcome": gate["outcome"],
                    "provider_clock": gate.get("provider_clock"),
                    "provider_minute": gate.get("provider_minute"),
                    "provider_period": gate.get("provider_period"),
                    "provider_status": gate.get("provider_status"),
                    "age_ms": gate.get("age_ms"),
                    "observation_id": gate.get("observation_id"),
                    "source": gate.get("source"),
                },
            }
            tagged = self._strategy_candidate(cand, PRICE_ONLY_STRATEGY, sleeve)
            self.record_signal(tagged, lag, f"sleeve_{gate['outcome']}")
            return f"sleeve_{gate['outcome']}"
        m = self.meta.get(cand["ticker"], {"event": "?", "series": "?"})
        event = m.get("event", "?")
        decision = self.late_score_sleeve.classify(
            cand,
            event,
            self.event_markets.get(event, ()),
            self.meta,
            self.books,
            cand.get("local_ts", time.time()) * 1000.0,
        )
        sleeve = dict(
            decision.detail,
            decision=decision.reason,
            match_clock_gate={
                "outcome": gate["outcome"],
                "provider_clock": gate.get("provider_clock"),
                "provider_minute": gate.get("provider_minute"),
                "provider_period": gate.get("provider_period"),
                "provider_status": gate.get("provider_status"),
                "age_ms": gate.get("age_ms"),
                "observation_id": gate.get("observation_id"),
                "source": gate.get("source"),
            },
        )
        tagged = self._strategy_candidate(cand, PRICE_ONLY_STRATEGY, sleeve)
        if not decision.accepted:
            self.record_signal(tagged, lag, f"sleeve_{decision.reason}")
            return f"sleeve_{decision.reason}"
        return self._execute_strategy_candidate(tagged, lag, decision_start)

    def act_on_signal(self, cand, lag):
        """Dispatch one confirmed episode to independently simulated strategies."""
        decision_start = time.monotonic()
        mode = config.PRICE_ONLY_SLEEVE_MODE
        outcomes = {}
        if mode in {"off", "parallel"}:
            outcomes[GATE_A_STRATEGY] = self._run_gate_a(cand, lag, decision_start)
        if mode in {"enforce", "parallel"}:
            outcomes[PRICE_ONLY_STRATEGY] = self._run_price_only(
                cand, lag, decision_start,
            )
        return outcomes

    def on_paper_entry_result(self, signal_id, cand, outcome, detail):
        """Finalize a queued paper signal after its simulated arrival."""
        if outcome == "filled":
            self._remember_fill(cand)
        self.broadcast({
            "type": "signal_update",
            "signal": {"id": signal_id, "outcome": outcome, "detail": detail},
        })
        store.log_event(
            "signal", f"{outcome.upper()} {cand['ticker']} after "
            f"{detail.get('paper_latency_ms', 0):.1f}ms paper latency "
            f"[{cand.get('strategy') or GATE_A_STRATEGY}]",
        )

    # ---------- background tasks ----------
    async def _fee_schedule(self, series):
        if series in self.fee_schedules:
            return self.fee_schedules[series]
        try:
            response = await self.client.get(f"/series/{series}")
            metadata = response.get("series") or {}
            fee_type = metadata.get("fee_type")
            multiplier = metadata.get("fee_multiplier")
            if fee_type is None or multiplier is None:
                return None, None
            schedule = fee_type, float(multiplier)
            self.fee_schedules[series] = schedule
            return schedule
        except (RuntimeError, TypeError, ValueError) as exc:
            self._record_error("fee_schedule", exc)
            return None, None

    async def discovery_task(self):
        while True:
            try:
                want = set()
                now = time.time()
                for series in config.SOCCER_SERIES:
                    try:
                        resp = await self.client.get("/markets", series_ticker=series,
                                                     status="open", limit=1000)
                    except Exception as exc:
                        self._record_error(f"discovery:{series}", exc)
                        continue
                    fee_type, fee_multiplier = await self._fee_schedule(series)
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
                                                 mkt.get("expected_expiration_time") or mkt.get("close_time"),
                                                 fee_type, fee_multiplier,
                                                 mkt.get("yes_sub_title") or
                                                 mkt.get("expiration_value"),
                                                 market_game_title(mkt))
                            want.add(tk)
                if self.ws:
                    await self.ws.set_markets(want)
                self.watched_events = {
                    self.meta[t]["event"] for t in want if t in self.meta
                }
                self.broadcast({"type": "log", "text":
                                f"discovery: watching {len(want)} markets "
                                f"({len(set(self.meta[t]['event'] for t in want if t in self.meta))} matches)"})
            except Exception as e:
                self._record_error("discovery", e)
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
                except Exception as exc:
                    self._record_error(f"settlement:{tk}", exc)

    async def periodic_task(self):
        last_stats = 0.0
        while True:
            await asyncio.sleep(config.BROADCAST_COALESCE_MS / 1000.0)
            self.desk.check_timeouts()
            # expire stale pendings
            now = time.time()
            # A near miss on a market that then goes quiet would otherwise sit
            # held until its next trade, which may never come before the match
            # ends.  Flushing on the same clock bounds that wait.
            self.detector.flush_subthreshold(now * 1000.0)
            # Also retries startup watches when no new book frame arrives.
            self._expire_signal_paths(now)
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
                # The event-clustered bootstrap in store.stats() is computed off
                # the event loop; running it inline here stalled live collection
                # and every dashboard request for its whole duration every 5s.
                stats = await store.read(store.stats)
                self.broadcast({"type": "stats", "stats": stats,
                                "status": self.status()})

    async def paper_execution_task(self):
        """Low-jitter clock for opt-in paper order arrivals."""
        delay = max(config.PAPER_EXECUTION_POLL_MS, 1.0) / 1000.0
        next_due = time.monotonic()
        while True:
            started = time.monotonic()
            store.add_latency("scheduler_lag_ms", max(0.0, (started - next_due) * 1000.0))
            try:
                self.desk.process_pending(self.books)
            except Exception as exc:
                self._record_error("paper_execution", exc)
                store.log_event("paper", f"execution adapter error: {exc!r}")
                self.broadcast({"type": "log", "text": f"paper execution error: {exc!r}"})
            next_due = started + delay
            await asyncio.sleep(max(next_due - time.monotonic(), 0.0))

    def status(self):
        lat = sorted(self.feed_lag)
        recorder = self.recorder.status()
        goal = (self.goal_latency.status() if self.goal_latency else {"enabled": False})
        tracker = getattr(self, "clock_tracker", None)
        watched = getattr(self, "watched_events", set())
        clock_coverage = tracker.coverage(watched) if tracker else {
            "watched": 0, "mapped": 0, "clock_present": 0, "clock_fresh": 0,
            "clock_stale": 0, "clock_gate_candidate_misses": 0,
            "faults": [], "mapping_errors": [],
        }
        database = store.database_health()
        ws_healthy = self.ws_state == "demo" or str(self.ws_state).startswith("connected")
        poll_age = (
            time.time() - goal["last_poll_ts"]
            if goal.get("enabled") and goal.get("last_poll_ts") else None
        )
        poll_stale = (
            poll_age is not None and goal.get("mapped_matches", 0) > 0 and
            poll_age > max(5.0, config.GOAL_LATENCY_POLL_MS / 1000.0 * 10.0)
        )
        goal_healthy = (
            not goal.get("enabled") or
            (not goal.get("last_error") and not poll_stale)
        )
        recent_cutoff = time.time() - 300.0
        recent_errors = [row for row in self.errors if row["ts"] >= recent_cutoff]
        execution_errors = [row for row in recent_errors
                            if row["component"].startswith("paper")]
        try:
            latency_readiness = store.latency_readiness()
        except Exception:
            latency_readiness = {}
        k4 = latency_readiness.get("order_arrival_ms") or {"state": "COLLECTING"}
        k4_blocking = k4.get("state") in {"BREACH", "INVALID"}
        checks = {
            "websocket": {"healthy": ws_healthy, "status": self.ws_state},
            "recorder": {"healthy": bool(recorder.get("healthy")),
                         "status": "recording" if recorder.get("healthy") else "error"},
            "match_event_feed": {
                "healthy": goal_healthy,
                "status": (
                    "diagnostic_disabled" if not goal.get("enabled") else
                    "error" if goal.get("last_error") else
                    "stale" if poll_stale else
                    "mapping_matches" if not goal.get("mapped_matches") else
                    "observing"
                ),
                "last_poll_ts": goal.get("last_poll_ts"),
                "last_response_ms": goal.get("last_response_ms"),
                "target_poll_ms": goal.get("poll_ms"),
                "mapped_matches": goal.get("mapped_matches", 0),
                "last_error": goal.get("last_error"),
            },
            "signal_path_persistence": {
                "healthy": not getattr(self, "signal_path_fault", None),
                "status": getattr(self, "signal_path_fault", None) or "persisted",
                "watches_open": len(getattr(self, "_signal_paths", ())),
            },
            "paper_execution": {
                "healthy": not execution_errors,
                "status": "ready" if not execution_errors else "recent_error",
                "pending": len(self.desk.pending_entries) + len(self.desk.pending_exits),
            },
            "database": database,
            "credentials": {
                "healthy": not bool(self.cred_error),
                "status": "configured" if not self.cred_error else "error",
            },
            "recent_backend_faults": {
                "healthy": not recent_errors,
                "status": "clear" if not recent_errors else f"{len(recent_errors)} recent",
            },
            "match_clock": _clock_coverage_check(clock_coverage),
            "latency_evidence": {
                "healthy": not k4_blocking,
                "status": k4.get("state") or "COLLECTING",
                "p95_ms": k4.get("p95"),
                "n": k4.get("n"),
                "threshold_ms": k4.get("threshold_ms"),
            },
        }
        runtime_ok = all(
            check["healthy"] for name, check in checks.items() if name != "latency_evidence"
        )
        system_healthy = runtime_ok and not k4_blocking
        k4_state = k4.get("state") or "COLLECTING"
        if not runtime_ok:
            banner = "attention_required"
            banner_text = "Attention required"
        elif k4_blocking:
            banner = "latency_breach"
            banner_text = "Runtime healthy · execution latency breached"
        elif k4_state in {"COLLECTING", "STALE"}:
            banner = "evidence_not_ready"
            banner_text = "Runtime healthy · paper evidence not ready"
        else:
            banner = "all_systems_good"
            banner_text = "All systems good"
        return {"mode": self.mode, "ws": self.ws_state, "uptime_s": int(time.time() - self.started),
                "markets": len(self.meta), "matches": len(self.event_markets),
                "trades_seen": self.n_trades, "recorded": self.recorder.total,
                "recorder": recorder,
                "kill": self.desk.kill, "open_positions": len(self.desk.positions),
                "paper_pending": len(self.desk.pending_entries) + len(self.desk.pending_exits),
                "demo": self.demo_status, "cred_error": self.cred_error,
                "foreign_dropped": self.n_foreign,
                "price_only_sleeve": config.PRICE_ONLY_SLEEVE_MODE,
                "goal_latency": goal,
                "clock_coverage": clock_coverage,
                "latency_readiness": latency_readiness,
                "health": {
                    "ok": system_healthy,
                    "runtime_ok": runtime_ok,
                    "banner": banner,
                    "banner_text": banner_text,
                    "checks": checks,
                    "recent_errors": list(reversed(recent_errors[-20:])),
                },
                "feed_lag_p50": round(lat[len(lat) // 2], 1) if lat else None,
                "feed_lag_p95": round(lat[int(0.95 * len(lat))], 1) if len(lat) > 20 else None}

    async def start(self):
        store.set_mode(self.mode)
        if self.mode == "live":
            # Mode-scoped queries isolate live evidence; deleting demo/legacy
            # rows here would make the explicit all-mode archival export lie.
            if config.PAPER_EXECUTION_V2:
                self.desk.restore_open_positions(store.load_open_paper_positions())
            # Signal collection is independent of realistic paper execution.
            # Always reconcile started-but-unfinalized forward watches.
            rebuilt = self.rebuild_signal_paths()
            if rebuilt:
                store.log_event(
                    "paper",
                    f"rebuilt {rebuilt} unfinalized signal forward path(s) "
                    "as incomplete after restart",
                )
            if config.PAPER_EXECUTION_V2:
                for pos in self.desk.positions.values():
                    self._remember_fill({
                        "strategy": pos.strategy,
                        "ticker": pos.market,
                        "ts_ms": pos.entry_ts * 1000.0,
                    })
            self.ws = KalshiWS(self.handle_ws, self.on_ws_state)
            asyncio.create_task(self.ws.run())
            asyncio.create_task(self.discovery_task())
            asyncio.create_task(self.settle_poll_task())
            if config.GOAL_LATENCY_OBSERVER:
                self.goal_latency = GoalLatencyObserver(
                    self.client,
                    lambda: self.watched_events,
                    self.market_window,
                    clock_tracker=self.clock_tracker,
                )
                asyncio.create_task(self.goal_latency.run())
            store.log_event("sys", "engine started in LIVE mode")
        else:
            rebuilt = self.rebuild_signal_paths()
            if rebuilt:
                store.log_event(
                    "paper",
                    f"rebuilt {rebuilt} unfinalized signal forward path(s) "
                    "as incomplete after restart",
                )
            from .replay import DemoReplay
            asyncio.create_task(DemoReplay(self).run())
            self.ws_state = "demo"
            store.log_event("sys", "engine started in DEMO mode (replaying real Madrid tapes)")
        if config.PAPER_EXECUTION_V2:
            asyncio.create_task(self.paper_execution_task())
        asyncio.create_task(self.periodic_task())
