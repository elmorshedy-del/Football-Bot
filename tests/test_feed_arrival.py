"""B1/B2: arrival stamping, backlog measurement and the feed-health ledger.

Local receipt stamps used to be taken when the consumer dequeued a frame, so
during a backlog every derived timestamp was wrong by however long the frame
had waited -- 5-28 s p50 per minute at the measured 2026-09-04 20:47-21:05 peak
of 64.7k frames/min -- and nothing recorded that a backlog existed at all.
"""
import asyncio
import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import engine as engine_module
from app import store
from app.kalshi import KalshiWS, _backlog_call_style
from app.recorder import MARKER_TYPE, RawRecorder


class FakeSocket:
    """Minimal `async for`-able stand-in for a websockets connection."""

    def __init__(self, frames, hold=None):
        self.frames = list(frames)
        self.hold = hold

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self.frames:
            yield frame
        if self.hold is not None:
            await self.hold


class ArrivalStampTests(unittest.IsolatedAsyncioTestCase):
    def make_ws(self, on_message, on_feed_event=None):
        with patch("app.kalshi._load_private_key", return_value=None):
            ws = KalshiWS(on_message, on_feed_event=on_feed_event)
        ws.connected = True
        ws._subscribed = {"A"}
        return ws

    async def test_reader_stamps_receipt_and_consumer_reports_the_backlog(self):
        """The stamp must be receipt time, not the time the handler ran."""
        seen = []

        def slow(msg, wall, mono, backlog=0):
            time.sleep(0.012)
            seen.append((msg, wall, mono, backlog))

        ws = self.make_ws(slow)
        frames = [json.dumps({"type": "trade", "msg": {"market_ticker": "A"}, "n": i})
                  for i in range(5)]
        queue = asyncio.Queue()
        ws._queue = queue
        await ws._read(FakeSocket(frames), queue)

        # Every frame was received before any was processed: that is the
        # condition the arrival stamp exists to make visible.
        self.assertEqual(queue.qsize(), 5)
        self.assertEqual(ws.backlog, 5)
        arrivals = [entry[1] for entry in list(queue._queue)]

        consumer = asyncio.ensure_future(ws._consume(queue))
        while len(seen) < 5:
            await asyncio.sleep(0.005)
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

        self.assertEqual([entry[0]["n"] for entry in seen], [0, 1, 2, 3, 4])
        self.assertEqual([entry[1] for entry in seen], arrivals)
        self.assertEqual([entry[3] for entry in seen], [4, 3, 2, 1, 0])
        # Processing the last frame happened well after it arrived.
        self.assertGreater(time.time() - arrivals[-1], 0.03)
        self.assertEqual(ws.max_backlog, 4)

    async def test_a_three_argument_callback_still_works(self):
        """`tests/test_sequence.py` and any legacy caller pass three args."""
        forwarded = []
        ws = self.make_ws(lambda *args: forwarded.append(args))
        queue = asyncio.Queue()
        ws._queue = queue
        await ws._read(FakeSocket([json.dumps({"type": "trade"})]), queue)
        consumer = asyncio.ensure_future(ws._consume(queue))
        while not forwarded:
            await asyncio.sleep(0.005)
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        self.assertEqual(len(forwarded[0]), 4)
        self.assertEqual(forwarded[0][0], {"type": "trade"})

    def test_call_style_detection_covers_every_shape(self):
        self.assertEqual(_backlog_call_style(lambda *args: None), "positional")
        self.assertEqual(_backlog_call_style(lambda m, w, mo: None), "none")
        self.assertEqual(_backlog_call_style(lambda m, w, mo, backlog=0: None), "keyword")
        self.assertEqual(_backlog_call_style(lambda m, w, mo, **kw: None), "keyword")
        self.assertEqual(_backlog_call_style(lambda m, w, mo, b: None), "positional")


class RecorderFrameTests(unittest.TestCase):
    def rows(self, directory):
        raw = os.path.join(directory, "raw")
        name = sorted(os.listdir(raw))[0]
        with gzip.open(os.path.join(raw, name), "rt") as source:
            return [json.loads(line) for line in source]

    def test_arrival_processing_and_backlog_are_all_recorded(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            recorder = RawRecorder()
            recorder.write({"type": "trade"}, 200.0, 20.0,
                           arrival_wall=100.0, arrival_mono=10.0, backlog=42)
            recorder.close()
            row = self.rows(directory)[0]

        self.assertEqual(row["lt"], 200.0)
        self.assertEqual(row["lm"], 20.0)
        self.assertEqual(row["at"], 100.0)
        self.assertEqual(row["am"], 10.0)
        self.assertEqual(row["bl"], 42)
        self.assertEqual(row["m"], {"type": "trade"})

    def test_a_legacy_three_argument_write_keeps_the_old_layout(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            recorder = RawRecorder()
            recorder.write({"type": "trade"}, 100.0, 50.0)
            recorder.close()
            row = self.rows(directory)[0]

        self.assertEqual(sorted(row), ["lm", "lt", "m"])

    def test_a_marker_is_self_describing_in_the_stream(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            recorder = RawRecorder()
            recorder.write_marker("gap", {"sid": 7, "expected": 11}, 5.0, 1.0)
            recorder.write({"type": "trade"}, 6.0, 2.0)
            recorder.close()
            rows = self.rows(directory)

        self.assertEqual(rows[0]["m"]["type"], MARKER_TYPE)
        self.assertEqual(rows[0]["m"]["kind"], "gap")
        self.assertEqual(rows[0]["m"]["detail"], {"sid": 7, "expected": 11})
        self.assertEqual(rows[0]["lt"], 5.0)
        self.assertEqual(recorder.markers, 1)
        # `total` stays a count of exchange frames; markers are counted apart.
        self.assertEqual(recorder.total, 1)
        self.assertEqual(rows[1]["m"]["type"], "trade")

    def test_a_ledger_failure_never_fails_the_recorder(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.recorder.config.DATA_DIR", directory):
            def explode(*_args):
                raise RuntimeError("ledger down")

            recorder = RawRecorder(on_event=explode)
            recorder._hour = "20260101-00"
            recorder._fh = gzip.open(
                os.path.join(directory, "raw", "feed-20260101-00.jsonl.gz"), "at")
            with patch("app.recorder.time.strftime", return_value="20260101-01"):
                recorder.write({"type": "trade"}, 1.0, 1.0)
            recorder.close()

        self.assertTrue(recorder.healthy)
        self.assertEqual(recorder.event_failures, 1)


class EngineArrivalWiringTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        recorder_patch = patch("app.recorder.config.DATA_DIR", self.dir.name)
        recorder_patch.start()
        self.addCleanup(recorder_patch.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

        self.engine = engine_module.Engine(asyncio.Queue(maxsize=64))
        self.engine.mode = "live"
        self.engine.register_market("A", "EV", "S", "A", None)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def trade_frame(self, ts_ms):
        return {"type": "trade", "msg": {
            "market_ticker": "A", "ts_ms": ts_ms,
            "yes_price_dollars": "0.50", "count_fp": "10", "taker_side": "yes",
        }}

    def test_feed_lag_uses_arrival_and_is_not_committed_per_trade(self):
        with patch("app.engine.store.add_latency") as add_latency:
            for i in range(60):
                # Arrival 2 s after the exchange stamp, processing 3 s later
                # still: only the arrival gap is the feed lag.
                self.engine.handle_ws(self.trade_frame(1000 + i), 3.0 + i, 1.0 + i,
                                      backlog=i)
            add_latency.assert_not_called()
            self.engine._flush_feed_latency()

        kinds = {call.args[0]: call.args[1] for call in add_latency.call_args_list}
        self.assertEqual(sorted(kinds), ["backlog_frames", "feed_lag"])
        self.assertEqual(kinds["backlog_frames"], 59)
        # arrival_wall*1000 - ts_ms for the median sample.
        self.assertAlmostEqual(kinds["feed_lag"], (3.0 + 30) * 1000 - 1030, places=3)
        self.assertEqual(len(self.engine.feed_lag), 60)
        self.assertEqual(self.engine.status()["feed_backlog"], 59)

    def test_the_recorded_frame_carries_arrival_processing_and_backlog(self):
        self.engine.handle_ws(self.trade_frame(1000), 111.0, 11.0, backlog=7)
        self.engine.recorder.close()
        raw = os.path.join(self.dir.name, "raw")
        with gzip.open(os.path.join(raw, sorted(os.listdir(raw))[0]), "rt") as source:
            row = json.loads(source.readline())

        self.assertEqual(row["at"], 111.0)
        self.assertEqual(row["am"], 11.0)
        self.assertEqual(row["bl"], 7)
        self.assertGreater(row["lt"], row["at"], "processing must be stamped later")

    def test_every_signal_outcome_records_its_capture_context(self):
        cand = {
            "ticker": "A", "ts_ms": 5_000, "dir": 1, "dl": 1.0, "levels": 5,
            "size": 300.0, "ref": 40.0, "ext": 60.0, "local_ts": 10.0,
            "context": {"arrival_wall": 7.25, "proc_wall": 7.5, "backlog": 12},
        }
        for outcome in ("unconfirmed", "confirmed_late", "rejected_cap"):
            sid = self.engine.record_signal(dict(cand), None, outcome, announce=False)
            row = store.q("SELECT outcome, context FROM signals WHERE id=?", (sid,))[0]
            context = json.loads(row["context"])
            with self.subTest(outcome=outcome):
                self.assertEqual(row["outcome"], outcome)
                self.assertEqual(context["backlog"], 12)
                self.assertAlmostEqual(context["feed_lag_ms"], 2250.0, places=3)
                self.assertAlmostEqual(context["proc_lag_ms"], 250.0, places=3)

    def test_a_subthreshold_row_records_the_frame_it_came_from(self):
        self.engine.record_subthreshold({
            "ticker": "A", "ts_ms": 5_000, "dir": 1, "dl": 0.5, "levels": 3,
            "size": 40.0, "ref": 40.0, "ext": 45.0, "local_ts": 10.0,
            "below": ["size"],
            "context": {"arrival_wall": 6.0, "proc_wall": 6.1, "backlog": 3},
        })
        row = store.q(
            "SELECT context FROM signals WHERE outcome='subthreshold'")[0]
        context = json.loads(row["context"])

        self.assertEqual(context["backlog"], 3)
        self.assertAlmostEqual(context["feed_lag_ms"], 1000.0, places=3)

    def test_the_detector_carries_the_frame_context_onto_the_candidate(self):
        """A held near miss must report the frame it happened on, not the one
        that flushed it."""
        detector = self.engine.detector
        for i in range(6):
            detector.on_trade("A", 100_000 - 2000 + i * 10, 20.0, 1.0, "yes",
                              context={"arrival_wall": 1.0, "proc_wall": 1.0,
                                       "backlog": 0})
        candidate = None
        for i in range(8):
            candidate = detector.on_trade(
                "A", 100_000 + i, 40.0 + i, 400.0, "yes",
                context={"arrival_wall": 9.0, "proc_wall": 9.1, "backlog": 4},
            ) or candidate

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["context"]["backlog"], 4)
        self.assertEqual(candidate["context"]["arrival_wall"], 9.0)


class FeedEventLedgerTests(unittest.TestCase):
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

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def ledger(self):
        return store.q("SELECT kind, detail, mode FROM feed_events ORDER BY id")

    def test_a_ledger_event_reaches_both_the_table_and_the_raw_stream(self):
        self.engine.on_feed_event("gap", {"sid": 7, "expected": 11, "received": 41})
        self.engine.recorder.close()

        rows = self.ledger()
        self.assertEqual([row["kind"] for row in rows], ["gap"])
        self.assertEqual(json.loads(rows[0]["detail"])["received"], 41)
        self.assertEqual(rows[0]["mode"], "live")

        raw = os.path.join(self.dir.name, "raw")
        with gzip.open(os.path.join(raw, sorted(os.listdir(raw))[0]), "rt") as source:
            frame = json.loads(source.readline())
        self.assertEqual(frame["m"]["type"], MARKER_TYPE)
        self.assertEqual(frame["m"]["kind"], "gap")

    def test_a_disconnect_and_a_gap_are_both_recorded_from_the_socket(self):
        async def scenario():
            with patch("app.kalshi._load_private_key", return_value=None):
                ws = KalshiWS(self.engine.handle_ws, self.engine.on_ws_state,
                              self.engine.on_ws_feed_event)
            ws.connected = True
            ws._subscribed = {"A", "B"}
            ws._orderbook_sid = 7
            sent = []

            async def send(cmd, params):
                sent.append((cmd, params))
                return 1

            ws._send = send
            await ws._accept_orderbook_frame({
                "type": "orderbook_snapshot", "sid": 7, "seq": 100,
                "msg": {"market_ticker": "A"},
            })
            await ws._accept_orderbook_frame({
                "type": "orderbook_delta", "sid": 7, "seq": 140,
                "msg": {"market_ticker": "A"},
            })
            ws._queue = asyncio.Queue()
            for _ in range(3):
                ws._queue.put_nowait(("{}", 1.0, 1.0))
            discarded = ws._discard_backlog()
            ws._emit("disconnected", {"error": "ConnectionClosedError",
                                      "backlog_discarded": discarded})
            # Let the ledger writes dispatched to worker threads land.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(self.ledger()) >= 3:
                    break
            return sent

        sent = asyncio.run(scenario())

        rows = self.ledger()
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds, ["gap", "snapshot_requested", "disconnected"])
        gap = json.loads(rows[0]["detail"])
        self.assertEqual((gap["expected"], gap["received"]), (101, 140))
        self.assertEqual(gap["invalidated"], 2)
        self.assertEqual(json.loads(rows[2]["detail"])["backlog_discarded"], 3)
        self.assertEqual([cmd for cmd, _ in sent], ["update_subscription"])

    def test_discovery_records_markets_added_and_dropped(self):
        self.engine._watched_markets = {"A", "B"}
        self.engine.on_feed_event("market_dropped", {"count": 1, "markets": ["B"]})
        rows = self.ledger()
        self.assertEqual(rows[0]["kind"], "market_dropped")
        self.assertEqual(json.loads(rows[0]["detail"])["markets"], ["B"])

    def test_a_ledger_write_failure_is_reported_not_swallowed(self):
        with patch("app.engine.store.insert_feed_event",
                   side_effect=OSError("disk full")):
            self.engine.on_feed_event("connected", {"connection": 1})

        self.assertEqual(self.engine._feed_event_failures, 1)
        self.assertTrue(any(row["component"] == "feed_event"
                            for row in self.engine.errors))

    def test_the_ledger_write_does_not_run_on_the_event_loop_thread(self):
        threads = []

        def record(*args, **kwargs):
            threads.append(threading.get_ident())

        async def scenario():
            with patch("app.engine.store.insert_feed_event", side_effect=record):
                self.engine.on_feed_event("connected", {"connection": 1})
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if threads:
                        break
            return threading.get_ident()

        loop_thread = asyncio.run(scenario())

        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], loop_thread)


if __name__ == "__main__":
    unittest.main()
