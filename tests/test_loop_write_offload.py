"""B8: the writes that were corrupting every timestamp.

Three defects shared one shape -- a synchronous SQLite commit on the asyncio
event loop, taken per event rather than per batch:

* `GoalLatencyObserver._record_provider_events` re-wrote every already-seen
  fingerprint on every ~250 ms poll (SELECT + UPDATE + COMMIT each, under the
  writer lock), which drove the measured poll interval to 5.7 s p50 / 30 s max
  on 2026-09-04 against a 250 ms target.
* `Engine._finalize_signal_path` inserted up to 4,000 `bid_path_samples` rows
  plus a commit inline, from inside the WebSocket handler.
* `PaperDesk._observe_executable_high` committed on every new executable high
  (covered in `tests/test_trade_highs.py` and `tests/test_bid_path.py`).
"""
import asyncio
import tempfile
import threading
import unittest
from collections import deque
from unittest.mock import patch

from app import engine as engine_module
from app import store
from app.books import Book
from app.goal_latency import GoalLatencyObserver


def live_book(yes=None):
    book = Book()
    book.yes_bids = dict(yes or {})
    book.ok = True
    return book


class StoreFixture:
    def start_store(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None


class SignalPathOffLoopTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        self.start_store()
        self.errors = []
        eng = engine_module.Engine.__new__(engine_module.Engine)
        eng._signal_paths = deque()
        eng.errors = deque(maxlen=50)
        eng._last_error_key = None
        eng._last_error_ts = 0.0
        eng.meta = {"KXGAME-YES": {"event": "KXGAME"}}
        eng.books = {}
        eng._record_error = lambda kind, exc: self.errors.append((kind, str(exc)))
        eng.signal_path_fault = None
        eng._signal_path_failed_owners = set()
        eng._path_write_tasks = set()
        self.engine = eng

    def signal(self):
        return store.insert_signal({
            "ts_ms": 1, "local_ts": 1000.0, "market": "KXGAME-YES", "event": "KXGAME",
            "series": "KXGAME", "dir": 1, "dl": 1.0, "levels": 5, "size": 200.0,
            "ref": 40.0, "ext": 60.0, "outcome": "unconfirmed", "detail": {},
            "forward_path_started_ts": 1000.0,
        })

    def watch(self, sid, rows=1):
        watch = {
            "signal_id": sid, "market": "KXGAME-YES", "event": "KXGAME",
            "side": "yes", "strategy": "price_only_late_score",
            "anchor_ts": 1000.0, "expires_at": 1000.0 + 30.0,
            "outcome": "unconfirmed", "last": None, "rows": [], "dropped": 0,
            "total": 0,
        }
        for index in range(rows):
            watch["total"] += 1
            watch["rows"].append({
                "kind": "decline", "trade_id": None, "signal_id": sid,
                "event": "KXGAME", "market": "KXGAME-YES", "side": "yes",
                "strategy": "price_only_late_score", "anchor_ts": 1000.0,
                "dt_ms": float(index * 100), "bid": 80.0 + index, "bid_size": 50.0,
                "exec_px": None, "qty": None,
                "sample_seq": watch["total"], "availability": "quote", "terminal": 0,
            })
        return watch

    def test_finalization_never_writes_on_the_event_loop_thread(self):
        """The whole point of B8b: no `bid_path_samples` commit inside the
        WebSocket handler's own thread."""
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=3))
        write_threads = []
        real = store.finalize_signal_path

        def watched(*args, **kwargs):
            write_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        async def scenario():
            with patch("app.engine.store.finalize_signal_path", side_effect=watched):
                # `on_book` -> `_expire_signal_paths` is synchronous and runs on
                # the loop; the write it triggers must not.
                self.engine._expire_signal_paths(2000.0)
                self.assertEqual(len(self.engine._signal_paths), 1,
                                 "the watch must stay owned while in flight")
                self.assertTrue(self.engine._signal_paths[0]["in_flight"])
                self.assertEqual(write_threads, [], "the write ran inline")
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if not self.engine._signal_paths:
                        break
            return threading.get_ident()

        loop_thread = asyncio.run(scenario())

        self.assertEqual(len(write_threads), 1)
        self.assertNotEqual(write_threads[0], loop_thread,
                            "finalization committed on the event-loop thread")
        self.assertEqual(self.engine._signal_paths, deque(),
                         "a successful finalization must release the watch")
        rows = store.q(
            "SELECT sample_seq FROM bid_path_samples WHERE signal_id=?"
            " ORDER BY sample_seq", (sid,))
        self.assertEqual([row["sample_seq"] for row in rows], [1, 2, 3])
        self.assertIsNotNone(
            store.q("SELECT forward_path_finalized FROM signals WHERE id=?",
                    (sid,))[0]["forward_path_finalized"])

    def test_an_in_flight_watch_is_never_finalized_twice(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=2))
        calls = []
        real = store.finalize_signal_path

        def slow(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        async def scenario():
            with patch("app.engine.store.finalize_signal_path", side_effect=slow):
                for _ in range(5):
                    self.engine._expire_signal_paths(2000.0)
                    self.engine._evict_signal_paths()
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if not self.engine._signal_paths:
                        break

        with patch("app.engine.config.SIGNAL_PATH_MAX_TRACKED", 0):
            asyncio.run(scenario())

        self.assertEqual(len(calls), 1, "the watch was finalized more than once")

    def test_a_failed_off_loop_finalization_keeps_the_watch_and_latches(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=2))

        async def scenario():
            with patch("app.engine.store.finalize_signal_path",
                       side_effect=OSError("disk full")):
                self.engine._expire_signal_paths(2000.0)
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if not self.engine._signal_paths[0].get("in_flight"):
                        break

        asyncio.run(scenario())

        self.assertEqual(len(self.engine._signal_paths), 1)
        self.assertEqual(self.engine.signal_path_fault, "signal_path_persistence_failed")
        self.assertIn(sid, self.engine._signal_path_failed_owners)
        self.assertEqual(self.engine._signal_paths[0]["rows"][0]["sample_seq"], 1)
        # And the same owner recovers on a later attempt.
        self.engine._expire_signal_paths(2001.0)
        self.assertEqual(len(self.engine._signal_paths), 0)
        self.assertIsNone(self.engine.signal_path_fault)

    def test_a_long_watch_flushes_incrementally_instead_of_at_the_end(self):
        """The incremental flush existed but had no caller, so a 300 s window
        landed as one 4,000-row insert inside the WebSocket handler."""
        from app.paper import BID_PATH_FLUSH_EVERY

        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=0))
        watch = self.engine._signal_paths[0]
        book = live_book()
        self.engine.books["KXGAME-YES"] = book

        for tick in range(BID_PATH_FLUSH_EVERY + 10):
            book.yes_bids = {20.0 + tick: 100.0}
            self.engine._record_signal_paths("KXGAME-YES", book, 1000.0 + tick * 0.01)

        durable = store.q(
            "SELECT COUNT(*) AS n FROM bid_path_samples WHERE signal_id=?", (sid,))
        self.assertEqual(durable[0]["n"], BID_PATH_FLUSH_EVERY,
                         "the buffer was not flushed at the batch boundary")
        self.assertEqual(len(watch["rows"]), 10, "flushed rows must leave the buffer")
        self.assertEqual(watch["total"], BID_PATH_FLUSH_EVERY + 10)


class ProviderEventRefreshTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.start_store()

    def payload(self, description="Corner"):
        return {
            "milestone_id": "M",
            "details": {
                "home_significant_events": [
                    {"event_type": "corner", "description": description,
                     "time": "88'", "occurence_ts": 900.0},
                ],
            },
        }

    def observer(self):
        observer = GoalLatencyObserver(object(), lambda: {"E"}, lambda *_a: [])
        observer.milestones["E"] = "M"
        observer.events_by_milestone["M"] = "E"
        return observer

    def timing(self, received):
        return {"started_wall": received - 0.05, "received_wall": received,
                "received_mono": received, "response_ms": 50.0}

    async def poll(self, observer, received):
        await observer._record_provider_events(
            "E", "M", self.payload(), None, self.timing(received),
        )

    async def test_a_repeated_poll_writes_nothing_until_the_flush(self):
        observer = self.observer()
        await self.poll(observer, 1000.0)
        inserted = store.q("SELECT id, first_observed_ts, last_observed_ts"
                           " FROM provider_match_events")
        self.assertEqual(len(inserted), 1, "a new fingerprint must insert at once")

        with patch("app.goal_latency.store.upsert_provider_event") as upsert:
            for step in range(1, 41):          # 40 polls at the 250 ms cadence
                await self.poll(observer, 1000.0 + step * 0.25)
            upsert.assert_not_called()

        self.assertEqual(len(observer.pending_refreshes), 1)
        row = store.q("SELECT last_observed_ts FROM provider_match_events")[0]
        self.assertEqual(row["last_observed_ts"], 1000.0,
                         "a repeated sighting must not commit per poll")

        written = await observer._flush_provider_refreshes(force=True)

        self.assertEqual(written, 1)
        self.assertEqual(observer.pending_refreshes, {})
        stored = store.q(
            "SELECT first_observed_ts, last_observed_ts, poll_started_ts,"
            " previous_poll_ts, response_ms FROM provider_match_events")[0]
        self.assertEqual(stored["last_observed_ts"], 1010.0,
                         "the flush must persist the newest observation time")
        self.assertEqual(stored["first_observed_ts"], 1000.0,
                         "the original occurrence must not move")
        self.assertEqual(stored["poll_started_ts"], 1009.95)

    async def test_the_flush_respects_its_interval_and_forces_on_drop(self):
        observer = self.observer()
        await self.poll(observer, 1000.0)
        await self.poll(observer, 1000.25)
        observer.last_refresh_flush = 1000.0

        with patch("app.goal_latency.config.PROVIDER_EVENT_FLUSH_S", 60.0):
            self.assertEqual(
                await observer._flush_provider_refreshes(now=1030.0), 0,
                "flushed before the interval elapsed")
            self.assertEqual(await observer._flush_provider_refreshes(now=1070.0), 1)

        await self.poll(observer, 1100.0)
        observer.event_tickers = lambda: set()        # the match leaves the window
        await observer._resolve_new_events()

        self.assertEqual(observer.pending_refreshes, {})
        self.assertEqual(
            store.q("SELECT last_observed_ts FROM provider_match_events")[0]
            ["last_observed_ts"], 1100.0,
            "a dropped event must flush its buffered observation time",
        )

    async def test_a_failed_flush_keeps_the_buffer_for_the_next_attempt(self):
        observer = self.observer()
        await self.poll(observer, 1000.0)
        await self.poll(observer, 1000.25)

        with patch("app.goal_latency.store.refresh_provider_events",
                   side_effect=OSError("disk full")):
            self.assertEqual(await observer._flush_provider_refreshes(force=True), 0)

        self.assertEqual(len(observer.pending_refreshes), 1)
        self.assertIn("provider refresh", observer.last_error)
        self.assertEqual(await observer._flush_provider_refreshes(force=True), 1)
        self.assertEqual(observer.pending_refreshes, {})

    async def test_the_batched_refresh_is_mode_scoped(self):
        observer = self.observer()
        await self.poll(observer, 1000.0)
        store.set_mode("demo")
        demo_id, is_new = store.upsert_provider_event({
            "observed_ts": 500.0, "poll_started_ts": 499.9, "previous_poll_ts": None,
            "response_ms": 10.0, "event": "E", "milestone_id": "M",
            "fingerprint": next(iter(observer.seen_fingerprints["E"])),
            "canonical_type": "provider.corner", "normalized_event": {},
            "raw_payload": {},
        })
        self.assertTrue(is_new)

        store.set_mode("live")
        await self.poll(observer, 1002.0)
        await observer._flush_provider_refreshes(force=True)

        rows = {row["id"]: row for row in store.q(
            "SELECT id, mode, last_observed_ts FROM provider_match_events")}
        self.assertEqual(rows[demo_id]["last_observed_ts"], 500.0,
                         "a live refresh reached a demo row")


if __name__ == "__main__":
    unittest.main()
