"""Handoff section 7: export preparation must not block collection.

These tests use a real temporary WAL SQLite database and a real page-by-page
backup rather than a mocked sleep, because the defect being fixed is lock
ownership, which a mock cannot reproduce.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import store

# Enough rows that a page-at-a-time backup takes measurable wall time.
_SEED_ROWS = 4000
# The handoff's bound for status/scheduler delay in the test environment.
_MAX_DELAY_MS = 250.0
# Per-page delay applied through SQLite's own progress mechanism, so the copy
# is genuinely slow rather than a mocked sleep around it.
_PAGE_DELAY_S = 0.005


def _slow_page(_status, _remaining, _total):
    time.sleep(_PAGE_DELAY_S)


class BackupContentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")
        for index in range(_SEED_ROWS):
            store.add_latency("order_arrival_ms", float(index % 200))

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def destination(self, name="snapshot.db"):
        return os.path.join(self.tempdir.name, name)

    async def test_slow_backup_does_not_stall_status_reads_or_writes(self):
        """A page-by-page backup must not own the store lock while copying."""
        destination = self.destination()
        max_status_ms = 0.0
        completed = {"status": 0, "signals": 0, "latency": 0}

        backup = asyncio.ensure_future(asyncio.to_thread(
            store.backup_database, destination, 1, 0.0, _slow_page,
        ))
        # Give the worker thread time to enter the copy loop.
        await asyncio.sleep(0.05)

        deadline = time.perf_counter() + 1.5
        while not backup.done() and time.perf_counter() < deadline:
            started = time.perf_counter()
            health = store.database_health()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            max_status_ms = max(max_status_ms, elapsed_ms)
            self.assertTrue(health["healthy"])
            completed["status"] += 1

            store.add_latency("feed_ingress_ms", 12.0)
            completed["latency"] += 1
            store.insert_signal({
                "ts_ms": 1, "local_ts": 1.0, "market": "T", "event": "EV",
                "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 200.0,
                "ref": 40.0, "ext": 60.0, "outcome": "confirmed", "detail": {},
            })
            completed["signals"] += 1
            await asyncio.sleep(0)

        await backup

        self.assertTrue(os.path.isfile(destination))
        for name, count in completed.items():
            self.assertGreater(count, 0, f"no {name} operation completed during backup")
        self.assertLess(
            max_status_ms, _MAX_DELAY_MS,
            f"status read blocked {max_status_ms:.1f}ms behind the backup lock",
        )

    async def test_write_committed_during_backup_survives_and_is_not_lost(self):
        destination = self.destination("during-write.db")
        backup = asyncio.ensure_future(asyncio.to_thread(
            store.backup_database, destination, 1, 0.0, _slow_page,
        ))
        await asyncio.sleep(0.05)

        marker_id = store.insert_signal({
            "ts_ms": 99, "local_ts": 99.0, "market": "MARKER", "event": "EV",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 200.0,
            "ref": 40.0, "ext": 60.0, "outcome": "confirmed", "detail": {},
        })
        await backup

        rows = store.q("SELECT market FROM signals WHERE id=?", (marker_id,))
        self.assertEqual(len(rows), 1, "a write committed during backup was lost")
        self.assertEqual(rows[0]["market"], "MARKER")

    async def test_backup_uses_a_dedicated_connection_not_the_live_one(self):
        """The live connection must stay usable throughout the copy."""
        destination = self.destination("dedicated.db")
        backup = asyncio.ensure_future(asyncio.to_thread(
            store.backup_database, destination, 1, 0.0, _slow_page,
        ))
        await asyncio.sleep(0.05)
        while not backup.done():
            self.assertTrue(store.database_health()["healthy"])
            await asyncio.sleep(0)
        await backup


class LegacyExportEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Section 7.2 item 4: one export implementation, reached through the job."""

    async def test_legacy_get_returns_202_job_and_never_snapshots_inline(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app import main

        # A registered full job makes later prepare calls short-circuit, so the
        # global job table must be restored for other tests in the suite.
        saved_jobs = dict(main._export_jobs)

        def _restore_jobs():
            main._export_jobs.clear()
            main._export_jobs.update(saved_jobs)

        self.addCleanup(_restore_jobs)

        recorder = Mock()
        fake_engine = SimpleNamespace(
            recorder=recorder, mode="live", _record_error=Mock(),
        )
        build = Mock(name="build_study_bundle")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = os.path.join(tmp, "snap.db")
            open(snapshot, "wb").close()
            with patch.object(main, "engine", fake_engine), \
                    patch.object(main.exporter, "raw_feed_paths", return_value=[]), \
                    patch.object(main.exporter, "prepare_database_snapshot",
                                 return_value=snapshot), \
                    patch.object(main.exporter, "build_study_bundle", build):
                started = time.perf_counter()
                response = await main.export_study_data()
                elapsed_ms = (time.perf_counter() - started) * 1000.0

                for task in tuple(main._export_tasks):
                    task.cancel()
                if main._export_tasks:
                    await asyncio.gather(*tuple(main._export_tasks),
                                         return_exceptions=True)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers.get("Deprecation"), "true")
        self.assertIn("/api/export/prepare", response.headers.get("Link", ""))
        self.assertIn("set-cookie", {k.lower() for k in response.headers.keys()})
        body = json.loads(bytes(response.body).decode())
        self.assertTrue(body.get("job_id"))
        self.assertLess(elapsed_ms, 500.0, "legacy export did not return promptly")
        build.assert_not_called()


class ExportTaskFailureTests(unittest.IsolatedAsyncioTestCase):
    """Section 7.2 item 5: no background export exception may go unobserved."""

    async def test_background_task_exception_is_retrieved_and_recorded(self):
        from app import main

        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))

        recorded = []

        async def boom():
            raise RuntimeError("export blew up")

        task = asyncio.ensure_future(boom())
        main._track_export_task(task, job_id="job-1", record=recorded.append)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(task.done())
        self.assertIsNotNone(task.exception())
        self.assertEqual(len(recorded), 1, "the failure was not recorded")
        self.assertNotIn(task, main._export_tasks, "the task was not removed")

        # Drop the last reference and force collection: an unretrieved
        # exception would be reported to the loop handler here.
        del task
        import gc
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual(
            [ctx.get("message") for ctx in unhandled], [],
            "an export task exception was never retrieved",
        )


if __name__ == "__main__":
    unittest.main()
