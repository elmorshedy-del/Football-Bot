"""PR 13 review §1.3: a signal watch must own its path until it is durable.

Expiry and eviction used to `popleft()` before persisting, so a failed write
destroyed the only owner of the buffered rows.
"""
import tempfile
import unittest
from collections import deque
from unittest.mock import patch

from app import engine as engine_module
from app import store
from app.books import Book


def live_book(yes=None, no=None):
    book = Book()
    book.yes_bids = dict(yes or {})
    book.no_bids = dict(no or {})
    book.ok = True
    return book


class SignalPathOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = patch("app.store.config.DATA_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        store._conn = None
        store.init()
        store.set_mode("live")

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
        self.engine = eng

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

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

    def durable(self, sid):
        return store.q(
            "SELECT dt_ms,bid,sample_seq,availability FROM bid_path_samples"
            " WHERE signal_id=? AND kind='decline' ORDER BY sample_seq", (sid,),
        )

    def signal_row(self, sid):
        return store.q("SELECT * FROM signals WHERE id=?", (sid,))[0]

    # ------------------------------------------------------------------ owner

    def test_final_signal_path_failure_keeps_watch_then_retries_exactly_once(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid))

        with patch("app.store._persist_path_in_transaction",
                   side_effect=OSError("disk full")):
            self.engine._expire_signal_paths(2000.0)

        self.assertEqual(
            len(self.engine._signal_paths), 1,
            "the watch was popped even though its final write failed",
        )
        self.assertEqual(self.durable(sid), [], "a partial path was committed")
        self.assertIsNone(
            self.signal_row(sid)["forward_path_finalized"],
            "the signal was marked finalized without durable rows",
        )
        self.assertEqual(
            self.engine.signal_path_fault, "signal_path_persistence_failed",
            "no current health fault was exposed",
        )

        self.engine._expire_signal_paths(2001.0)

        self.assertEqual(len(self.engine._signal_paths), 0, "retry did not release")
        rows = self.durable(sid)
        self.assertEqual([row["sample_seq"] for row in rows], [1])
        self.assertIsNotNone(self.signal_row(sid)["forward_path_finalized"])
        self.assertIsNotNone(self.signal_row(sid)["forward_path_summary"])
        self.assertIsNone(
            self.engine.signal_path_fault,
            "the fault must clear once the retry succeeds",
        )

    def test_eviction_over_max_tracked_also_keeps_the_watch_on_failure(self):
        sid = self.signal()
        with patch("app.engine.config.SIGNAL_PATH_MAX_TRACKED", 0), \
                patch("app.store._persist_path_in_transaction",
                      side_effect=OSError("disk full")):
            self.engine._signal_paths.append(self.watch(sid))
            self.engine._evict_signal_paths()

        self.assertEqual(
            len(self.engine._signal_paths), 1,
            "eviction dropped a watch whose path never persisted",
        )

    def test_retry_never_duplicates_a_decline_row(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=3))

        with patch("app.store._persist_path_in_transaction",
                   side_effect=OSError("disk full")):
            self.engine._expire_signal_paths(2000.0)
        self.engine._expire_signal_paths(2001.0)
        self.engine._signal_paths.append(self.watch(sid, rows=3))
        self.engine._expire_signal_paths(2002.0)

        rows = self.durable(sid)
        self.assertEqual(
            [row["sample_seq"] for row in rows], [1, 2, 3],
            "a retry duplicated decline rows",
        )

    # ------------------------------------------------------------------- gaps

    def test_decline_rows_carry_sequences_and_record_one_gap(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid, rows=0))
        watch = self.engine._signal_paths[0]

        self.engine.books["KXGAME-YES"] = live_book(yes={20.0: 100.0})
        self.engine._record_signal_paths(
            "KXGAME-YES", self.engine.books["KXGAME-YES"], 1000.1)
        empty = live_book()
        for tick in range(4):
            self.engine._record_signal_paths("KXGAME-YES", empty, 1000.2 + tick * 0.1)
        self.engine.books["KXGAME-YES"] = live_book(yes={30.0: 100.0})
        self.engine._record_signal_paths(
            "KXGAME-YES", self.engine.books["KXGAME-YES"], 1001.0)

        availabilities = [row["availability"] for row in watch["rows"]]
        self.assertEqual(
            availabilities, ["quote", "gap", "quote"],
            "a no-ladder run must record exactly one gap and then resume",
        )
        self.assertEqual([row["sample_seq"] for row in watch["rows"]], [1, 2, 3])
        self.assertTrue(all(row["sample_seq"] is not None for row in watch["rows"]))

    # ---------------------------------------------------------------- restart

    def test_unfinalized_watch_is_rebuilt_and_marked_incomplete_on_restart(self):
        sid = self.signal()
        store.insert_bid_path(self.watch(sid, rows=2)["rows"])
        self.assertEqual(len(self.durable(sid)), 2)

        # A restart with no in-memory tail: the watch never finalized.
        pending = store.unfinalized_signal_paths()
        self.assertEqual([row["id"] for row in pending], [sid])

        self.engine.rebuild_signal_paths()

        row = self.signal_row(sid)
        self.assertIsNotNone(
            row["forward_path_finalized"],
            "an unfinalized watch was left unresolved after restart",
        )
        self.assertEqual(
            row["path_incomplete_reason"], "in_memory_tail_lost_on_restart",
            "a rebuilt path must be labelled incomplete, not presented as complete",
        )
        self.assertIsNotNone(row["forward_path_summary"])

    def test_a_cleanly_finalized_signal_is_not_rebuilt(self):
        sid = self.signal()
        self.engine._signal_paths.append(self.watch(sid))
        self.engine._expire_signal_paths(2000.0)

        self.assertEqual(store.unfinalized_signal_paths(), [])
        self.assertIsNone(self.signal_row(sid)["path_incomplete_reason"])


if __name__ == "__main__":
    unittest.main()
