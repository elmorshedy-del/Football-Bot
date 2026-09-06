"""The arrival queue is bounded, and what it discards is disclosed.

Production, 2026-09-05: the reader kept up (feed lag p50 63-148 ms) while the
consumer fell behind and the UNBOUNDED queue let it stay behind.  The backlog
reached 896,017 frames and settled near 556,000, `order_arrival_ms` reached p95
2,280,666 ms (38 minutes) and max 2,895,826 ms, and 3.77 GB of an 8 GB limit was
held at 0.12 of 8 vCPU -- so not compute saturation.  Trades 110-114 "entered"
against book state 41-79 minutes old for matches that had already finished and
booked the settlement queued behind them, fabricating about +$281 of paper
profit.

These tests pin the three properties that make the same stall loud instead of
silent: the queue is bounded, every discard is counted and ledgered, and a book
that lost frames stops serving fills until the exchange re-describes it.
"""
import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from app import config
from app import engine as engine_module
from app import store
from app.books import Book
from app.kalshi import KalshiWS


class FakeSocket:
    """`async for`-able stand-in that never ends until told to."""

    def __init__(self, frames, hold=None):
        self.frames = list(frames)
        self.hold = hold
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.frames:
            yield frame
        if self.hold is not None:
            await self.hold

    async def close(self):
        self.closed = True


def book_frame(ticker, seq, ts_ms):
    return json.dumps({
        "type": "orderbook_delta", "sid": 7, "seq": seq,
        "msg": {"market_ticker": ticker, "price_dollars": "0.40",
                "delta_fp": "1", "side": "yes", "ts_ms": ts_ms},
    })


def trade_frame(ticker, ts_ms):
    return json.dumps({
        "type": "trade", "sid": 8,
        "msg": {"market_ticker": ticker, "ts_ms": ts_ms,
                "yes_price_dollars": "0.50", "count_fp": "10",
                "taker_side": "yes"},
    })


class BoundedQueueTests(unittest.IsolatedAsyncioTestCase):
    """The socket client in isolation: bound, drop policy, disclosure."""

    def setUp(self):
        self.events = []
        self.dispatched = []
        self.sent = []

    def make_ws(self, queue_max=4, on_message=None, **overrides):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(
                on_message or (lambda msg, wall, mono, backlog=0:
                               self.dispatched.append(msg)),
                on_feed_event=lambda kind, detail=None:
                    self.events.append((kind, detail)),
            )
        ws.connected = True
        ws._subscribed = {"A", "B"}
        ws._orderbook_sid = 7

        async def send(cmd, params):
            self.sent.append((cmd, params))
            return 1

        ws._send = send
        settings = {"WS_QUEUE_MAX": queue_max, "WS_QUEUE_OVERFLOW_REPORT_S": 1.0}
        settings.update(overrides)
        for name, value in settings.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return ws

    def kinds(self):
        return [kind for kind, _detail in self.events]

    def detail(self, kind):
        return next(detail for name, detail in self.events if name == kind)

    # ------------------------------------------------------------- the bound

    async def test_overflow_drops_the_oldest_frames_and_records_the_loss(self):
        ws = self.make_ws(queue_max=4)
        queue = asyncio.Queue()
        ws._queue = queue
        # Six trade frames into a queue of four: the two oldest must go.
        frames = [trade_frame("A", 1_000 + i * 100) for i in range(6)]

        await ws._read(FakeSocket(frames), queue)

        self.assertEqual(queue.qsize(), 4, "the queue was not bounded")
        kept = [json.loads(entry[0])["msg"]["ts_ms"] for entry in list(queue._queue)]
        self.assertEqual(kept, [1_200, 1_300, 1_400, 1_500],
                         "the NEWEST frames must survive, not the oldest")
        self.assertEqual(ws.queue_dropped_total, 2)
        self.assertEqual(ws.frames_received, 6)

        self.assertIn("queue_overflow", self.kinds())
        detail = self.detail("queue_overflow")
        self.assertEqual(detail["policy"], "oldest")
        self.assertEqual(detail["dropped_total"], ws.queue_dropped_total)
        self.assertEqual(detail["queue_depth"], 4)
        self.assertEqual(detail["queue_max"], 4)
        # The span of exchange time thrown away, from the frames themselves.
        self.assertEqual(detail["exchange_ts_min_ms"], 1_000.0)
        self.assertEqual(detail["exchange_ts_max_ms"], 1_100.0)
        self.assertEqual(detail["exchange_ts_span_ms"], 100.0)

    async def test_queue_max_zero_restores_the_unbounded_queue(self):
        ws = self.make_ws(queue_max=0)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket([trade_frame("A", i) for i in range(50)]), queue)

        self.assertEqual(queue.qsize(), 50)
        self.assertEqual(ws.queue_dropped_total, 0)
        self.assertEqual(ws.queue_overflow_events, 0)
        self.assertEqual(self.events, [])

    async def test_an_unparseable_discard_is_still_counted(self):
        ws = self.make_ws(queue_max=1)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket(["{not json", trade_frame("A", 5)]), queue)

        self.assertEqual(ws.queue_dropped_total, 1)
        self.assertEqual(self.detail("queue_overflow")["unparseable"], 1)

    # ------------------------------------------------- books that lost frames

    async def test_a_dropped_delta_invalidates_its_market_and_asks_for_a_snapshot(self):
        ws = self.make_ws(queue_max=1)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(
            FakeSocket([book_frame("A", 10, 1_000), trade_frame("B", 1_100)]), queue)

        self.assertEqual(ws.queue_dropped_total, 1)
        detail = self.detail("queue_overflow")
        self.assertEqual(detail["invalidated_markets"], ["A"])
        self.assertEqual(detail["book_frames_dropped"], 1)

        # The SAME mechanism a sequence gap uses, so the engine's existing
        # invalidation branch handles it.
        gap = next(msg for msg in self.dispatched if msg["type"] == "orderbook_gap")
        self.assertEqual(gap["msg"]["reason"], "queue_overflow")
        self.assertEqual(gap["msg"]["market_tickers"], ["A"])

        self.assertEqual([cmd for cmd, _ in self.sent], ["update_subscription"])
        params = self.sent[0][1]
        self.assertEqual(params["action"], "get_snapshot")
        self.assertEqual(params["market_tickers"], ["A"])
        self.assertEqual(self.detail("snapshot_requested")["reason"], "queue_overflow")

        # Until that snapshot lands every further delta for A is refused, so a
        # holed book cannot be topped up with frames that assume the hole.
        self.assertFalse(await ws._accept_orderbook_frame(json.loads(
            book_frame("A", 11, 1_200))))
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 12,
            "msg": {"market_ticker": "A"}}))

    async def test_a_dropped_trade_alone_never_invalidates_a_book(self):
        """Trades do not build the book; discarding one is not a book fault."""
        ws = self.make_ws(queue_max=1)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket([trade_frame("A", 1), trade_frame("A", 2)]), queue)

        self.assertEqual(ws.queue_dropped_total, 1)
        self.assertEqual(self.detail("queue_overflow")["invalidated_markets"], [])
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.sent, [])

    async def test_a_book_frame_with_no_readable_market_invalidates_everything(self):
        ws = self.make_ws(queue_max=1)
        queue = asyncio.Queue()
        ws._queue = queue
        anonymous = json.dumps({"type": "orderbook_delta", "sid": 7, "msg": {}})

        await ws._read(FakeSocket([anonymous, trade_frame("A", 2)]), queue)

        gap = next(msg for msg in self.dispatched if msg["type"] == "orderbook_gap")
        self.assertEqual(gap["msg"]["market_tickers"], ["A", "B"])
        self.assertEqual(self.detail("queue_overflow")["unknown_market_frames"], 1)

    # ------------------------------------------------------- emission control

    async def test_overflow_emission_is_rate_limited(self):
        """An overflow storm must not become the bottleneck it reports."""
        ws = self.make_ws(queue_max=1, WS_QUEUE_OVERFLOW_REPORT_S=3600.0)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket([trade_frame("A", i) for i in range(60)]), queue)

        self.assertEqual(ws.queue_dropped_total, 59)
        self.assertEqual(ws.queue_overflow_events, 1,
                         "one summary per interval, not one event per frame")
        self.assertEqual(self.kinds().count("queue_overflow"), 1)
        # The single summary still accounts for every frame that was lost.
        detail = self.detail("queue_overflow")
        self.assertEqual(detail["dropped"], 59)
        self.assertEqual(detail["dropped_total"], 59)

    async def test_a_newly_affected_market_is_disclosed_without_waiting(self):
        """Rate limiting may delay a count; it may never delay an invalidation."""
        ws = self.make_ws(queue_max=1, WS_QUEUE_OVERFLOW_REPORT_S=3600.0)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket([
            book_frame("A", 1, 10), book_frame("A", 2, 20),
            book_frame("B", 3, 30), trade_frame("A", 40),
        ]), queue)

        invalidated = [msg["msg"]["market_tickers"] for msg in self.dispatched
                       if msg["type"] == "orderbook_gap"]
        self.assertEqual(invalidated, [["A"], ["B"]])
        # A repeat drop for a market already invalidated does not re-emit.
        self.assertEqual(ws.queue_overflow_events, 2)
        self.assertEqual(ws.queue_dropped_total, 3)

    # ------------------------------------------------------------ stall guard

    def test_the_stall_guard_forces_exactly_one_reconnect(self):
        ws = self.make_ws(queue_max=100, WS_QUEUE_STALL_DEPTH=50,
                          WS_QUEUE_STALL_S=60.0,
                          WS_RECONNECT_MIN_INTERVAL_S=120.0)

        # Below the high-water mark: nothing is armed.
        self.assertIsNone(ws._stall_reconnect_due(10, 1_000.0))
        self.assertIsNone(ws._stall_since)
        # Above it, the clock starts but does not fire.
        self.assertIsNone(ws._stall_reconnect_due(80, 1_001.0))
        self.assertEqual(ws._stall_since, 1_001.0)
        self.assertIsNone(ws._stall_reconnect_due(80, 1_060.0))
        # Past WS_QUEUE_STALL_S above the mark: fire, once.
        detail = ws._stall_reconnect_due(80, 1_062.0)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["queue_depth"], 80)
        self.assertEqual(detail["high_water"], 50)
        self.assertEqual(detail["held_s"], 61.0)
        self.assertEqual(ws.queue_forced_reconnects, 1)

    def test_a_recovered_queue_disarms_the_stall_guard(self):
        ws = self.make_ws(queue_max=100, WS_QUEUE_STALL_DEPTH=50,
                          WS_QUEUE_STALL_S=60.0)

        self.assertIsNone(ws._stall_reconnect_due(80, 0.0))
        self.assertIsNone(ws._stall_reconnect_due(10, 30.0))    # drained
        self.assertIsNone(ws._stall_reconnect_due(80, 31.0))    # re-armed
        self.assertIsNone(ws._stall_reconnect_due(80, 89.0),
                          "the stall clock did not restart when the queue drained")
        self.assertIsNotNone(ws._stall_reconnect_due(80, 92.0))
        self.assertEqual(ws.queue_forced_reconnects, 1)

    def test_the_minimum_interval_stops_the_guard_thrashing(self):
        ws = self.make_ws(queue_max=100, WS_QUEUE_STALL_DEPTH=50,
                          WS_QUEUE_STALL_S=60.0,
                          WS_RECONNECT_MIN_INTERVAL_S=120.0)

        ws._stall_reconnect_due(80, 0.0)
        self.assertIsNotNone(ws._stall_reconnect_due(80, 61.0))
        # A second stall inside the minimum interval is refused, not queued.
        ws._stall_reconnect_due(80, 62.0)
        self.assertIsNone(ws._stall_reconnect_due(80, 130.0))
        self.assertEqual(ws.queue_forced_reconnects, 1)
        self.assertEqual(ws.queue_forced_reconnects_suppressed, 1)
        # Past the interval it is allowed again.
        ws._stall_reconnect_due(80, 131.0)
        self.assertIsNotNone(ws._stall_reconnect_due(80, 200.0))
        self.assertEqual(ws.queue_forced_reconnects, 2)

    def test_the_stall_guard_is_off_while_the_queue_is_unbounded(self):
        ws = self.make_ws(queue_max=0, WS_QUEUE_STALL_DEPTH=0,
                          WS_QUEUE_STALL_S=60.0)

        self.assertEqual(config.ws_queue_stall_depth(), 0)
        ws._stall_reconnect_due(10 ** 6, 0.0)
        self.assertIsNone(ws._stall_reconnect_due(10 ** 6, 10_000.0))
        self.assertEqual(ws.queue_forced_reconnects, 0)

    async def test_the_watchdog_drains_the_queue_and_closes_the_socket(self):
        ws = self.make_ws(queue_max=100, WS_QUEUE_STALL_DEPTH=2,
                          WS_QUEUE_STALL_S=0.0, WS_QUEUE_STALL_POLL_S=0.0)
        queue = asyncio.Queue()
        ws._queue = queue
        for i in range(5):
            queue.put_nowait((trade_frame("A", i), 1.0, 1.0))
        socket = FakeSocket([])

        await asyncio.wait_for(ws._watch_queue(socket, queue), timeout=5)

        self.assertTrue(socket.closed, "a stalled feed was not reconnected")
        self.assertEqual(queue.qsize(), 0, "the stale backlog was not drained")
        self.assertEqual(ws.frames_discarded, 5)
        detail = self.detail("queue_stall_reconnect")
        self.assertEqual(detail["drained"], 5)
        self.assertEqual(detail["high_water"], 2)

    async def test_a_stalled_queue_forces_the_run_loop_to_reconnect_once(self):
        """End to end: the guard actually rebuilds the connection.

        A reconnect re-subscribes, which yields fresh snapshots -- strictly
        better than a deep queue of stale deltas, and exactly the failure the
        single-coroutine design used to produce by itself.
        """
        def slow_consumer(msg, wall, mono, backlog=0):
            # A consumer that cannot keep up: the production stall was
            # I/O- or lock-bound, at 0.12 of 8 vCPU.
            time.sleep(0.001)

        ws = self.make_ws(queue_max=300, on_message=slow_consumer,
                          WS_QUEUE_STALL_DEPTH=50,
                          WS_QUEUE_STALL_S=0.0, WS_QUEUE_STALL_POLL_S=0.0,
                          WS_QUEUE_OVERFLOW_REPORT_S=3600.0,
                          WS_RECONNECT_MIN_INTERVAL_S=600.0)
        ws.connected = False
        ws._subscribed = set()
        ws._headers = lambda: {}
        sockets = []

        class Connection:
            async def __aenter__(inner):
                inner.socket = FakeSocket(
                    [trade_frame("A", i) for i in range(400)],
                    hold=asyncio.get_running_loop().create_future())
                sockets.append(inner.socket)
                return inner.socket

            async def __aexit__(inner, *_exc):
                return False

        with patch("app.kalshi.websockets.connect",
                   lambda *a, **k: Connection()), \
                patch("app.kalshi.RECONNECT_DELAY_S", 0.0):
            task = asyncio.ensure_future(ws.run())
            for _ in range(400):
                await asyncio.sleep(0.005)
                if ws.connections >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertGreaterEqual(ws.connections, 2,
                                "a stalled queue did not force a reconnect")
        self.assertTrue(sockets[0].closed, "the stalled socket was left open")
        self.assertEqual(ws.queue_forced_reconnects, 1,
                         "the minimum interval did not stop a second reconnect")
        self.assertIn("queue_stall_reconnect", self.kinds())

    # ---------------------------------------------------------------- status

    def test_status_reports_the_bound_and_what_it_cost(self):
        ws = self.make_ws(queue_max=20_000)
        ws.queue_dropped_total = 9
        ws.queue_overflow_events = 2
        ws.queue_forced_reconnects = 1
        status = ws.status()

        self.assertEqual(status["queue_max"], 20_000)
        self.assertEqual(status["queue_drop_policy"], "oldest")
        self.assertEqual(status["queue_dropped_total"], 9)
        self.assertEqual(status["queue_overflow_events"], 2)
        self.assertEqual(status["queue_forced_reconnects"], 1)
        self.assertEqual(status["queue_depth"], status["backlog"])


class DropPolicyTests(unittest.TestCase):
    def test_an_unknown_policy_falls_back_to_oldest_not_to_unbounded(self):
        for value in ("", "newest", "random", "  OLDEST  ", None):
            with self.subTest(value=value), patch.object(
                    config, "WS_QUEUE_DROP_POLICY", value):
                self.assertEqual(config.ws_queue_drop_policy(), "oldest")

    def test_the_stall_depth_defaults_to_half_the_bound(self):
        with patch.object(config, "WS_QUEUE_STALL_DEPTH", 0), \
                patch.object(config, "WS_QUEUE_MAX", 20_000):
            self.assertEqual(config.ws_queue_stall_depth(), 10_000)
        with patch.object(config, "WS_QUEUE_STALL_DEPTH", 25), \
                patch.object(config, "WS_QUEUE_MAX", 20_000):
            self.assertEqual(config.ws_queue_stall_depth(), 25)

    def test_transport_bounds_are_not_strategy_parameters(self):
        """`WS_QUEUE_*` decide which frames are seen, not what is done with
        them, so they must not re-partition the study by moving `config_id`."""
        names = ("WS_QUEUE_MAX", "WS_QUEUE_DROP_POLICY", "WS_QUEUE_STALL_S",
                 "WS_QUEUE_STALL_DEPTH", "WS_QUEUE_OVERFLOW_REPORT_S",
                 "WS_RECONNECT_MIN_INTERVAL_S")
        for name in names:
            self.assertNotIn(name, config.STRATEGY_PARAM_NAMES)
        before = config.config_id()
        with patch.object(config, "WS_QUEUE_MAX", 7), \
                patch.object(config, "WS_QUEUE_STALL_S", 3.0):
            self.assertEqual(config.config_id(), before)

    def test_the_bounds_are_still_recorded_in_the_export_manifest(self):
        """A bundle captured under a bound may contain deliberate holes."""
        from app import exporter

        exported = exporter.non_secret_config()
        self.assertEqual(exported["ws_queue_max"], config.WS_QUEUE_MAX)
        self.assertIn("ws_queue_drop_policy", exported)
        self.assertIn("ws_queue_stall_s", exported)


class OverflowInvalidatesBooksInTheEngineTests(unittest.TestCase):
    """End to end: the discarded delta reaches the engine's existing gap path."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for target in ("app.store.config.DATA_DIR", "app.recorder.config.DATA_DIR"):
            patcher = patch(target, self.dir.name)
            patcher.start()
            self.addCleanup(patcher.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)
        self.engine = engine_module.Engine(asyncio.Queue(maxsize=64))
        self.engine.mode = "live"
        self.engine.register_market("A", "EV", "S", "A", None)
        self.engine.register_market("B", "EV", "S", "B", None)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def live_book(self, ticker):
        book = self.engine.books.setdefault(ticker, Book())
        book.apply_snapshot({"yes_dollars_fp": [["0.40", 500]],
                             "no_dollars_fp": [["0.55", 500]],
                             "ts_ms": 1_000}, 1, arrival_wall=1.0)
        self.engine.desk.apply_book_snapshot(ticker, book)
        return book

    def test_a_market_that_lost_deltas_stops_serving_fills(self):
        book = self.live_book("A")
        other = self.live_book("B")
        self.assertTrue(book.ok)

        self.engine.handle_ws({"type": "orderbook_gap",
                               "msg": {"sid": 7, "reason": "queue_overflow",
                                       "dropped": 12, "market_tickers": ["A"]}},
                              5.0, 5.0, backlog=3)

        self.assertFalse(book.ok, "a book that lost deltas still serves fills")
        self.assertTrue(other.ok, "an unaffected market was invalidated too")

        # Every fill path in the desk and the engine gates on `book.ok`, and
        # only a fresh snapshot clears it -- a queued delta must not.
        book.apply_delta({"price_dollars": "0.41", "delta_fp": "5",
                          "side": "yes", "ts_ms": 2_000}, 14,
                         sequence_validated=True, arrival_wall=6.0)
        self.assertFalse(book.ok, "a queued delta revived an invalidated book")
        shadow = self.engine.desk._shadow_for("gate_a")._books.get("A")
        if shadow is not None:
            self.assertFalse(shadow.ok, "the paper desk's shadow stayed fillable")

        self.engine.handle_ws({"type": "orderbook_snapshot", "seq": 20,
                               "msg": {"market_ticker": "A",
                                       "yes_dollars_fp": [["0.42", 100]],
                                       "no_dollars_fp": [["0.53", 100]],
                                       "ts_ms": 3_000}},
                              7.0, 7.0, backlog=0)
        self.assertTrue(book.ok, "a fresh snapshot did not restore the book")

    def test_the_overflow_is_logged_as_an_overflow_not_as_a_sequence_gap(self):
        self.live_book("A")
        self.engine.handle_ws({"type": "orderbook_gap",
                               "msg": {"sid": 7, "reason": "queue_overflow",
                                       "dropped": 400, "market_tickers": ["A"]}},
                              5.0, 5.0, backlog=3)

        text = store.q("SELECT text FROM eventlog WHERE kind='book'")[-1]["text"]
        self.assertIn("arrival queue overflow", text)
        self.assertIn("dropped=400", text)
        self.assertIn("awaiting fresh snapshots", text)

    def test_a_real_sequence_gap_still_reads_as_a_sequence_gap(self):
        self.live_book("A")
        self.engine.handle_ws({"type": "orderbook_gap",
                               "msg": {"sid": 7, "expected": 11, "received": 40,
                                       "market_tickers": ["A"]}},
                              5.0, 5.0, backlog=0)

        text = store.q("SELECT text FROM eventlog WHERE kind='book'")[-1]["text"]
        self.assertIn("sequence gap sid=7 expected=11 received=40", text)

    def test_status_surfaces_the_queue_counters_beside_the_backlog(self):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(self.engine.handle_ws)
        ws._queue = asyncio.Queue()
        ws.queue_dropped_total = 17
        ws.queue_overflow_events = 3
        ws.queue_forced_reconnects = 1
        self.engine.ws = ws

        status = self.engine.status()

        self.assertIn("feed_backlog", status)
        self.assertEqual(status["queue_depth"], 0)
        self.assertEqual(status["queue_max"], config.WS_QUEUE_MAX)
        self.assertEqual(status["queue_drop_policy"], "oldest")
        self.assertEqual(status["queue_dropped_total"], 17)
        self.assertEqual(status["queue_overflow_events"], 3)
        self.assertEqual(status["queue_forced_reconnects"], 1)

    def test_status_reports_zero_drops_when_there_is_no_socket(self):
        self.engine.ws = None
        status = self.engine.status()

        self.assertEqual(status["queue_dropped_total"], 0)
        self.assertEqual(status["queue_overflow_events"], 0)
        self.assertEqual(status["queue_forced_reconnects"], 0)


if __name__ == "__main__":
    unittest.main()
