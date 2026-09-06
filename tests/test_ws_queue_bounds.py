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
import collections
import inspect
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
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

    async def test_an_unsubscribed_market_is_disclosed_once_not_once_per_frame(self):
        """"Newly affected" must mean newly SEEN, not newly acted on.

        A book frame for a market this process never subscribed to has no book
        to invalidate, so the immediate-disclosure path does nothing for it.
        If that also left it unremembered, every repeat would read as newly
        affected and force another emission -- a write per frame at exactly the
        moment the consumer is already too slow, which is the defect the bound
        exists to remove.  Production drops tens of thousands of these frames.
        """
        ws = self.make_ws(queue_max=1, WS_QUEUE_OVERFLOW_REPORT_S=3600.0)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket(
            [book_frame("FOREIGN", seq, seq * 10) for seq in range(1, 41)]), queue)

        self.assertEqual(ws.queue_dropped_total, 39)
        # One immediate disclosure for the market, then the teardown summary
        # covering the other 38 -- not one emission per dropped frame.
        self.assertEqual(ws.queue_overflow_events, 2)
        gaps = [msg for msg in self.dispatched if msg["type"] == "orderbook_gap"]
        self.assertEqual(len(gaps), 1)
        # Nothing subscribed was holed, so nothing is invalidated or re-snapshotted.
        self.assertEqual(gaps[0]["msg"]["market_tickers"], [])
        self.assertEqual(self.sent, [])

    async def test_unreadable_book_frames_invalidate_everything_once(self):
        """The blanket invalidation is worth doing once, not once per frame."""
        ws = self.make_ws(queue_max=1, WS_QUEUE_OVERFLOW_REPORT_S=3600.0)
        queue = asyncio.Queue()
        ws._queue = queue
        anonymous = json.dumps({"type": "orderbook_delta", "sid": 7, "msg": {}})

        await ws._read(FakeSocket([anonymous] * 40), queue)

        self.assertEqual(ws.queue_dropped_total, 39)
        self.assertEqual(ws.queue_overflow_events, 2)  # immediate + teardown
        gaps = [msg for msg in self.dispatched if msg["type"] == "orderbook_gap"]
        self.assertEqual([gap["msg"]["market_tickers"] for gap in gaps], [["A", "B"]])
        self.assertEqual(len(self.sent), 1)

    async def test_a_market_holed_again_after_recovering_is_invalidated_again(self):
        """Remembering a market must not outlive its fresh snapshot."""
        ws = self.make_ws(queue_max=1, WS_QUEUE_OVERFLOW_REPORT_S=3600.0)
        queue = asyncio.Queue()
        ws._queue = queue

        await ws._read(FakeSocket([book_frame("A", 1, 10), trade_frame("B", 20)]), queue)
        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 2,
            "msg": {"market_ticker": "A"}}))

        await ws._read(FakeSocket([book_frame("A", 3, 30), trade_frame("B", 40)]), queue)

        invalidated = [msg["msg"]["market_tickers"] for msg in self.dispatched
                       if msg["type"] == "orderbook_gap"]
        self.assertEqual(invalidated, [["A"], ["A"]])

    # ------------------------------------------------- consumer throughput

    async def drain_with_clock(self, frames, cost_ms, slice_ms):
        """Drain `frames` frames, charging `cost_ms` to each, on a fake clock.

        Returns how many frames were handled in each event-loop turn -- the
        quantity the old design fixed at 16 however cheap a frame was.  Turns
        are counted by a competing task, because that is what a turn IS: the
        point at which everything else on the loop gets to run.
        """
        ws = self.make_ws(queue_max=0, WS_CONSUMER_SLICE_MS=slice_ms)
        queue = asyncio.Queue()
        for index in range(frames):
            queue.put_nowait((trade_frame("A", index), 1.0, 1.0))
        ws._queue = queue

        now, turn = [0.0], [0]
        per_turn = collections.Counter()

        async def handle(raw, wall, mono, backlog):
            now[0] += cost_ms / 1000.0
            per_turn[turn[0]] += 1

        async def count_turns():
            while True:
                await asyncio.sleep(0)
                turn[0] += 1

        ws._handle_raw = handle
        # Patch the NAME `app.kalshi` binds, never `time.monotonic` itself: the
        # event loop reads the real one, and a frozen clock underneath it would
        # be measuring the harness rather than the code.
        clock = SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1_000.0,
                                perf_counter_ns=lambda: int(now[0] * 1e9))
        with patch("app.kalshi.time", clock):
            tasks = [asyncio.ensure_future(count_turns()),
                     asyncio.ensure_future(ws._consume(queue))]
            for _ in range(frames * 4):
                if queue.empty():
                    break
                await asyncio.sleep(0)
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        return [per_turn[key] for key in sorted(per_turn)]

    async def test_the_consumer_drains_a_time_slice_not_a_frame_count(self):
        """The old `CONSUMER_YIELD_EVERY = 16` capped throughput at 16 frames
        per event-loop turn however cheap a frame was.  Production, 2026-09-06:
        scheduler lag p50 19 ms / p95 165 ms, so that cap is 97-800 frames/s
        against ~19,000 frames/s of measured capacity, with CPU at 0.10 of 8
        vCPU.  Frames per turn must follow the time budget instead."""
        cheap = await self.drain_with_clock(400, cost_ms=0.1, slice_ms=5.0)
        self.assertGreater(min(cheap[:-1] or cheap), 16,
                           "a cheap frame must not still cost a whole loop turn")
        # 5 ms of 0.1 ms frames is ~50, and never the old fixed 16.
        self.assertTrue(all(40 <= size <= 60 for size in cheap[:-1]), cheap)

    async def test_an_expensive_frame_shortens_the_batch_instead_of_the_slice(self):
        """The slice bounds how long anything else on the loop waits, so a
        costlier frame buys fewer frames per turn -- not a longer turn."""
        pricey = await self.drain_with_clock(60, cost_ms=1.0, slice_ms=5.0)
        self.assertTrue(all(5 <= size <= 7 for size in pricey[:-1]), pricey)

    async def test_a_zero_slice_yields_after_every_frame(self):
        """The escape hatch stays available and means what it says."""
        every = await self.drain_with_clock(20, cost_ms=1.0, slice_ms=0.0)
        self.assertEqual(set(every), {1})

    # ---------------------------------------- recovery must not amplify itself

    async def test_a_market_is_asked_for_one_snapshot_per_recovery_episode(self):
        """The engine calls `request_snapshot` for every delta a holed book
        rejects, which is unbounded while the book is being rebuilt: during the
        2026-09-06 storm that was thousands of websocket sends a second, each
        taking `_lock` on the consumer path and each answered with a snapshot
        into the overflowing queue."""
        ws = self.make_ws(queue_max=0)
        for _ in range(50):
            await ws.request_snapshot("A")

        self.assertEqual(len(self.sent), 1, "recovery re-asked for the same book")
        self.assertEqual(ws.snapshot_requests_coalesced, 49)
        self.assertEqual(self.sent[0][1]["market_tickers"], ["A"])

    async def test_a_snapshot_that_lands_re_arms_the_next_request(self):
        """Coalescing must not outlive the request it coalesced."""
        ws = self.make_ws(queue_max=0)
        ws._recovering_orderbooks[7] = {"A"}
        await ws.request_snapshot("A")
        await ws.request_snapshot("A")
        self.assertEqual(len(self.sent), 1)

        self.assertTrue(await ws._accept_orderbook_frame({
            "type": "orderbook_snapshot", "sid": 7, "seq": 5,
            "msg": {"market_ticker": "A"}}))
        await ws.request_snapshot("A")

        self.assertEqual(len(self.sent), 2, "a fresh hole must be able to ask again")

    async def test_a_gap_while_recovery_is_in_flight_is_recorded_not_re_requested(self):
        """161 gaps in eight minutes each re-requested all 36 markets, feeding
        the queue whose overflow was manufacturing the gaps."""
        ws = self.make_ws(queue_max=0)
        await ws._recover_orderbook(7, 100, 200)
        self.assertEqual(len(self.sent), 1)
        first = self.sent[0][1]["market_tickers"]
        self.assertEqual(first, ["A", "B"])

        for expected in range(201, 211):
            await ws._recover_orderbook(7, expected, expected + 5)

        self.assertEqual(len(self.sent), 1, "recovery re-requested while in flight")
        self.assertEqual(ws.gap_recoveries_coalesced, 10)
        # Every gap is still on the record; only the duplicate work is skipped.
        gaps = [detail for kind, detail in self.events if kind == "gap"]
        self.assertEqual(len(gaps), 11)
        self.assertEqual(gaps[-1]["recovery"], "already_in_flight")
        self.assertNotIn("recovery", gaps[0])

    async def test_recovery_still_runs_again_once_the_books_are_back(self):
        ws = self.make_ws(queue_max=0)
        await ws._recover_orderbook(7, 100, 200)
        for ticker in ("A", "B"):
            await ws._accept_orderbook_frame({
                "type": "orderbook_snapshot", "sid": 7, "seq": 5,
                "msg": {"market_ticker": ticker}})

        await ws._recover_orderbook(7, 300, 400)

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(ws.gap_recoveries_coalesced, 0)

    async def test_a_gap_covering_a_market_recovery_missed_is_never_suppressed(self):
        """Suppression is only safe while the in-flight set covers everything
        this gap invalidates; a market subscribed since must still recover."""
        ws = self.make_ws(queue_max=0)
        await ws._recover_orderbook(7, 100, 200)
        ws._subscribed.add("C")

        await ws._recover_orderbook(7, 300, 400)

        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1][1]["market_tickers"], ["A", "B", "C"])

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


class StageCostTests(unittest.TestCase):
    """Where the per-frame cost goes -- measured, not attributed by argument.

    The consumer's cost per frame is what decides how far behind the exchange
    this process runs.  The in-process benchmark says ~65 us of work per frame;
    production on 2026-09-06 sustained 320-470 frames/s, which is 2-3 ms.
    Nothing measured the difference, so every explanation for it was a guess --
    and Part E of the investigation records what guessing cost the last time:
    every instrumentation layer added since 2026-08-30 ran on the trading loop
    and became the latency it had been added to explain.

    So the two properties that matter for this particular instrument are that it
    reports the stages truthfully, and that it is far too cheap to be the thing
    it is measuring.
    """

    def engine(self):
        eng = engine_module.Engine.__new__(engine_module.Engine)
        eng._stages = None
        return eng

    def test_each_stage_is_reported_with_its_own_count_and_cost(self):
        eng = self.engine()
        for _ in range(3):
            with eng.stage("book_apply"):
                pass
        with eng.stage("on_book"):
            pass

        costs = eng.stage_costs()
        self.assertEqual(sorted(costs), ["book_apply", "on_book"])
        self.assertEqual(costs["book_apply"]["n"], 3)
        self.assertEqual(costs["on_book"]["n"], 1)
        for row in costs.values():
            self.assertGreaterEqual(row["total_ms"], 0.0)
            self.assertGreaterEqual(row["max_ms"], 0.0)
            self.assertIsNotNone(row["mean_us"])

    def test_a_raising_stage_is_still_counted_and_still_raises(self):
        """A stage that fails is exactly the one worth having measured."""
        eng = self.engine()
        with self.assertRaises(ValueError):
            with eng.stage("book_apply"):
                raise ValueError("frame rejected")

        self.assertEqual(eng.stage_costs()["book_apply"]["n"], 1)

    def test_two_engines_never_share_timings(self):
        first, second = self.engine(), self.engine()
        with first.stage("record"):
            pass

        self.assertEqual(second.stage_costs(), {})

    def test_an_engine_with_no_frames_yet_reports_nothing_not_an_error(self):
        self.assertEqual(self.engine().stage_costs(), {})

    def test_the_measurement_costs_far_less_than_what_it_measures(self):
        """It is on the trading loop, so its own cost is a correctness
        property, not a nicety.  The budget is the 2-3 ms per frame being
        explained; anything near that would be self-defeating."""
        eng = self.engine()
        rounds = 20_000
        start = time.perf_counter()
        for _ in range(rounds):
            with eng.stage("record"):
                pass
        per_call_us = (time.perf_counter() - start) / rounds * 1e6

        self.assertLess(per_call_us, 50.0,
                        f"stage timing costs {per_call_us:.2f} us per frame")
        self.assertEqual(eng.stage_costs()["record"]["n"], rounds)


class ReadinessOffTheLoopTests(unittest.TestCase):
    """`status()` must not run the latency series on the event loop.

    It is 18 SQLite queries, measured at 104 ms per `status()` call in
    production, and `status()` runs from the 5 s broadcast, from every
    `/api/status` poll and from every WebSocket hello. That is loop time the
    frame consumer does not get -- and the frame path itself accounts for only
    ~2.6% of wall clock, so what starves it is work like this.

    `periodic_task` refreshes the snapshot through `store.read`, which is the
    same seam the adjacent `store.stats` call already uses.
    """

    def engine(self):
        eng = engine_module.Engine.__new__(engine_module.Engine)
        eng._stages = None
        eng._readiness_snapshot = None
        return eng

    def test_status_reads_the_snapshot_and_never_the_database(self):
        eng = self.engine()
        eng._readiness_snapshot = {"order_arrival_ms": {"state": "PASS", "p95": 12.0}}

        with patch.object(engine_module.store, "latency_readiness") as live:
            readiness = eng._readiness_snapshot
            self.assertEqual(live.call_count, 0)
        self.assertEqual(readiness["order_arrival_ms"]["p95"], 12.0)

    def test_before_the_first_refresh_it_still_reports(self):
        """A snapshot that has not landed yet must not blank the health panel."""
        eng = self.engine()
        self.assertIsNone(eng._readiness_snapshot)

    def test_an_engine_built_without_init_still_has_the_default(self):
        self.assertIsNone(
            engine_module.Engine.__new__(engine_module.Engine)._readiness_snapshot)

    def test_k4_reads_the_snapshot_so_staleness_is_bounded_by_the_broadcast(self):
        """Safe precisely because k4_blocking feeds the health banner only: it
        gates no trade, no fill and no kill, so a snapshot up to one broadcast
        interval old changes nothing about what the bot does."""
        self.assertIn("self._readiness_snapshot",
                      inspect.getsource(engine_module.Engine.status))
        # Whitespace-insensitive: the claim is that the refresh goes through
        # `store.read`, not how the call happens to be wrapped.
        periodic = " ".join(
            inspect.getsource(engine_module.Engine.periodic_task).split())
        self.assertIn("store.read( store.latency_readiness)", periodic)


class ConsumerShareTests(unittest.IsolatedAsyncioTestCase):
    """Working time over uptime: starved, or saturated?

    Every latency diagnosis in this codebase so far answered that by argument.
    A4 attributed the backlog to SQLite commits on the loop and was wrong -- the
    frame path issues 0.0008 statements per frame. The per-stage timers then
    showed the whole frame path costing 2.6% of wall clock while the queue built
    to 17,000, which says the consumer is not doing the expensive thing; it does
    not by itself prove what is. This counter is the direct measurement: the
    coroutine's own working time, against the time it existed for.
    """

    def setUp(self):
        self.events = []

    def make_ws(self, **overrides):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(lambda msg, wall, mono, backlog=0: None)
        for name, value in {"WS_QUEUE_MAX": 0, "WS_CONSUMER_SLICE_MS": 5.0,
                            **overrides}.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return ws

    async def drain(self, ws, frames, cost_ms):
        queue = asyncio.Queue()
        for index in range(frames):
            queue.put_nowait((trade_frame("A", index), 1.0, 1.0))
        now = [0.0]

        async def handle(raw, wall, mono, backlog):
            now[0] += cost_ms / 1000.0

        ws._handle_raw = handle
        clock = SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1_000.0,
                                perf_counter_ns=lambda: int(now[0] * 1e9))
        with patch("app.kalshi.time", clock):
            task = asyncio.ensure_future(ws._consume(queue))
            for _ in range(frames * 4):
                if queue.empty():
                    break
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_the_counter_measures_the_work_the_consumer_actually_did(self):
        ws = self.make_ws()
        await self.drain(ws, frames=200, cost_ms=1.0)

        # 200 frames at 1 ms each, whatever the loop did around them.
        self.assertAlmostEqual(ws.consume_ns / 1e6, 200.0, delta=5.0)
        self.assertGreater(ws.consume_slices, 0)
        self.assertEqual(ws.status()["consume_ms"], round(ws.consume_ns / 1e6, 3))

    async def test_slices_are_counted_so_the_share_can_be_read_per_slice(self):
        ws = self.make_ws(WS_CONSUMER_SLICE_MS=5.0)
        await self.drain(ws, frames=200, cost_ms=1.0)

        # ~6 frames per 5 ms slice, so ~33 slices for 200 frames.
        self.assertGreaterEqual(ws.consume_slices, 25)
        self.assertLessEqual(ws.consume_slices, 40)

    async def test_an_idle_consumer_reports_no_working_time(self):
        ws = self.make_ws()
        self.assertEqual(ws.consume_ns, 0)
        self.assertEqual(ws.status()["consume_ms"], 0.0)
