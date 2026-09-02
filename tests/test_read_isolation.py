"""Serving-layer responsiveness: dashboard reads must not stall the loop.

These tests pin the fix for the production starvation cascade (WebSocket 1006,
"Fetch aborted", "Load failed", HTTP 499): synchronous SQLite reads used to run
directly on the async event loop and under the single writer lock, so one heavy
analytics scan blocked every other request -- including trivial ones such as
/api/config -- and delayed live collection.  The contract now is:

  * a dashboard read never waits on the writer's lock (read/write isolation);
  * a slow read runs off the event loop, so the loop keeps serving;
  * the study aggregate is memoised per write, so a poll storm computes it once;
  * server-side timing is measurable, not merely inferred from the browser.
"""
import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch

from app import main, store


def _slow_read():
    # Stands in for a multi-second SQLite scan: time.sleep releases the GIL just
    # as SQLite's C calls do, so if this ran on the loop the loop would freeze.
    time.sleep(0.4)
    return "done"


class ReadIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        store._conn = None
        store.init()
        store.set_mode("live")

        def _close():
            if store._conn is not None:
                store._conn.close()
                store._conn = None

        self.addCleanup(_close)

    def _seed_signal(self, event="E"):
        return store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "T", "event": event,
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 10.0,
            "ref": 40.0, "ext": 60.0, "outcome": "filled", "detail": {},
        })

    def test_api_read_does_not_wait_on_the_writer_lock(self):
        """A read must complete while the writer lock is held.

        Before the fix q() took `_lock` for every read, so a dashboard scan and a
        live write serialised against each other; holding the writer lock here
        would have deadlocked the read.  It now uses a lock-free read connection.
        """
        self._seed_signal()

        async def _run():
            return await asyncio.wait_for(main.signals(), timeout=5.0)

        store._lock.acquire()
        try:
            rows = asyncio.run(_run())
        finally:
            store._lock.release()

        self.assertEqual({row["event"] for row in rows}, {"E"})

    def test_a_slow_read_does_not_block_the_event_loop(self):
        """While a read runs, the loop must keep making progress."""

        async def _body():
            slow = asyncio.create_task(store.read(_slow_read))
            ticks = 0
            while not slow.done():
                await asyncio.sleep(0.01)
                ticks += 1
            return ticks, await slow

        ticks, result = asyncio.run(_body())
        self.assertEqual(result, "done")
        # If _slow_read had run on the loop, it would have frozen for 0.4s and
        # the counter could not have advanced.  Off the loop, it ticks freely.
        self.assertGreater(ticks, 5)

    def test_read_perf_counts_dispatched_reads(self):
        self._seed_signal()
        before = store.read_perf()["reads"]
        asyncio.run(main.signals())
        after = store.read_perf()
        self.assertGreater(after["reads"], before)
        self.assertGreaterEqual(after["avg_ms"], 0.0)


class StatsCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        store._conn = None
        store.init()
        store.set_mode("live")

        def _close():
            if store._conn is not None:
                store._conn.close()
                store._conn = None

        self.addCleanup(_close)

    def _insert_closed_trade(self, event, net):
        store.ex(
            "INSERT INTO trades(event,series,status,net,mode) VALUES(?,?,?,?,?)",
            (event, "S", "closed", net, "live"),
        )

    def test_repeated_calls_reuse_one_computation_until_a_write(self):
        self._insert_closed_trade("E1", 10.0)
        first = store.stats()
        second = store.stats()
        # Same object: the poll storm shares one bootstrap pass while the study
        # is unchanged, instead of recomputing it every call.
        self.assertIs(first, second)

        self._insert_closed_trade("E2", 5.0)
        third = store.stats()
        self.assertIsNot(third, first, "a write must invalidate the cache")
        self.assertEqual(third["combined"]["closed"], first["combined"]["closed"] + 1)

    def test_a_fresh_database_does_not_serve_a_previous_cache(self):
        self._insert_closed_trade("E1", 10.0)
        self.assertEqual(store.stats()["combined"]["closed"], 1)
        # Re-initialising against a different database file (a new volume, or the
        # next test's temp dir reusing a pooled connection) must drop the cache
        # so a fresh, empty study is never answered with the previous one.
        store._conn.close()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        with patch("app.store.config.DATA_DIR", other.name):
            store.init()
            self.assertEqual(store.stats()["combined"]["closed"], 0)

    def test_cache_matches_a_direct_recompute(self):
        for index in range(6):
            self._insert_closed_trade(f"E{index}", 1.0 + index)
        self.assertEqual(store.stats(), store._compute_stats(None))


class EventClusterCITests(unittest.TestCase):
    """The bootstrap CI is study evidence; the speedup must not move a digit."""

    @staticmethod
    def _reference(closed):
        # The original O(fills-per-iteration) implementation, preserved here so
        # the optimised store version is held to byte-for-byte equality.
        import random
        by_ev = {}
        for t in closed:
            by_ev.setdefault(t["event"], []).append(t["net"] or 0)
        evs = list(by_ev.values())
        if len(evs) < 5:
            return None
        rnd = random.Random(7)
        means = []
        for _ in range(2000):
            flat = [x for _ in range(len(evs)) for x in rnd.choice(evs)]
            means.append(sum(flat) / len(flat))
        means.sort()
        return [round(means[int(0.025 * len(means))], 2),
                round(means[int(0.975 * len(means))], 2)]

    def test_event_cluster_ci_matches_reference(self):
        import random
        rnd = random.Random(99)
        for n_events in (5, 8, 25, 60):
            closed = []
            for e in range(n_events):
                for _ in range(rnd.randint(1, 12)):
                    closed.append({"event": f"E{e}", "net": rnd.uniform(-8, 9)})
            self.assertEqual(
                store._event_cluster_ci(closed), self._reference(closed),
                f"CI diverged for {n_events} events",
            )

    def test_below_five_clusters_has_no_interval(self):
        closed = [{"event": f"E{i}", "net": 1.0} for i in range(4)]
        self.assertIsNone(store._event_cluster_ci(closed))


class TimingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, scope):
        sent = []

        async def app(inner_scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await main._TimingMiddleware(app)(scope, receive, send)
        return sent

    async def test_http_response_is_stamped_and_recorded(self):
        with main._perf_lock:
            main._request_perf.clear()
        sent = await self._drive({"type": "http", "path": "/api/probe", "method": "GET"})
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = dict(start["headers"])
        self.assertIn(b"server-timing", headers)
        self.assertTrue(headers[b"server-timing"].startswith(b"app;dur="))

        report = await main.perf()
        self.assertTrue(any(row["path"] == "/api/probe" for row in report["requests"]))
        self.assertIn("db_reads", report)

    async def test_non_http_scope_passes_through_untouched(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        await main._TimingMiddleware(app)(
            {"type": "lifespan"}, None, None,
        )
        self.assertEqual(seen, ["lifespan"])


if __name__ == "__main__":
    unittest.main()
