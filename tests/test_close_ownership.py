"""PR 13 review §1.1 and §1.2: closing a trade must be one owned transaction.

Every test here drives the real PaperDesk against a real temporary SQLite
database. A close is only complete when the path, its summary, the final fill
and the closed-trade fields all commit together; until then the position stays
owned and retryable.
"""
import tempfile
import unittest
from unittest.mock import patch

from app import paper, store
from app.books import Book


def live_book(yes=None, no=None):
    book = Book()
    book.yes_bids = dict(yes or {})
    book.no_bids = dict(no or {})
    book.ok = True
    return book


class CloseOwnershipTests(unittest.TestCase):
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
        self.broadcasts = []
        self.desk = paper.PaperDesk.__new__(paper.PaperDesk)
        self.desk.realistic = False
        self.desk.positions = {}
        self.desk.pending_entries = []
        self.desk.pending_exits = {}
        self.desk.kill = False
        self.desk.shadows = None
        self.desk._report_error = lambda kind, exc: self.errors.append((kind, str(exc)))
        self.desk._safe_log = lambda *a, **k: None
        self.desk.broadcast = lambda payload: self.broadcasts.append(payload)
        self.desk._safe_broadcast = self.desk.broadcast

    def tearDown(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def open_trade(self, entry_px=50.0, size=10.0):
        signal_id = store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "KXGAME-YES", "event": "KXGAME",
            "series": "KXGAME", "dir": 1, "dl": 1.0, "levels": 5, "size": size,
            "ref": 40.0, "ext": 60.0, "outcome": "confirmed", "detail": {},
        })
        tid = store.open_paper_trade(
            {
                "signal_id": signal_id, "market": "KXGAME-YES", "event": "KXGAME",
                "series": "KXGAME", "dir": 1, "side": "yes", "entry_ts": 1000.0,
                "entry_px": entry_px, "size": size, "cap": 100.0, "notional": 5.0,
                "book_at_entry": {}, "strategy": "price_only_late_score",
            },
            {}, [(entry_px, size, 0.1)], 0.1, 12.0, order_arrival_ms=30.0,
        )
        pos = paper.Position(
            tid, signal_id, "KXGAME-YES", "KXGAME", "KXGAME", 1, "yes",
            entry_px, size, 40.0, 60.0, "quadratic", 1.0, {},
            "price_only_late_score",
        )
        pos.entry_ts = 1000.0
        pos.remaining = size
        pos.entry_fees = 0.1
        self.desk.positions[tid] = pos
        return pos

    def trade_row(self, tid):
        return store.q("SELECT * FROM trades WHERE id=?", (tid,))[0]

    def path_rows(self, tid):
        return store.q(
            "SELECT dt_ms,bid,sample_seq,terminal FROM bid_path_samples"
            " WHERE trade_id=? ORDER BY sample_seq", (tid,),
        )

    # ------------------------------------------------------------------ §1.1

    def test_final_trade_path_failure_keeps_position_then_retries_exactly_once(self):
        pos = self.open_trade()
        self.desk._record_exec_path(
            pos, live_book(no={45.0: 100.0}), 55.0, 1001.0,
        )
        self.assertTrue(pos.exec_path, "fixture must buffer at least one row")

        with patch("app.paper.store.insert_bid_path",
                   side_effect=OSError("disk full")), \
                patch("app.store.insert_bid_path", side_effect=OSError("disk full")), \
                patch("app.store._persist_path_in_transaction",
                      side_effect=OSError("disk full")):
            self.desk.close(pos, 60.0, "target")

        trade = self.trade_row(pos.tid)
        self.assertEqual(
            trade["status"], "open",
            "the trade was closed even though its final path never persisted",
        )
        self.assertIn(pos.tid, self.desk.positions,
                      "the position lost its retry owner")
        self.assertTrue(pos.exec_path, "buffered rows were dropped on failure")
        self.assertEqual(
            [event for event in self.broadcasts if event.get("type") == "trade_close"],
            [], "a close was broadcast for a transaction that rolled back",
        )
        self.assertEqual(self.path_rows(pos.tid), [], "a partial path was committed")

        # Retry: the same sequence keys must produce exactly one of everything.
        self.desk.close(pos, 60.0, "target")

        trade = self.trade_row(pos.tid)
        self.assertEqual(trade["status"], "closed")
        self.assertNotIn(pos.tid, self.desk.positions)
        rows = self.path_rows(pos.tid)
        self.assertEqual(
            [row["terminal"] for row in rows].count(1), 1,
            "retry must leave exactly one terminal row",
        )
        self.assertEqual(
            len({row["sample_seq"] for row in rows}), len(rows),
            "retry duplicated path rows",
        )
        self.assertIsNotNone(trade["bid_path_summary"], "no summary was persisted")
        exits = store.q(
            "SELECT COUNT(*) AS n FROM paper_fills WHERE trade_id=? AND leg='exit'",
            (pos.tid,),
        )[0]["n"]
        self.assertLessEqual(exits, 1, "retry duplicated the final fill")

    def test_a_committed_close_writes_path_summary_and_closed_fields_together(self):
        pos = self.open_trade()
        self.desk._record_exec_path(pos, live_book(no={45.0: 100.0}), 55.0, 1001.0)
        self.desk.close(pos, 60.0, "target")

        trade = self.trade_row(pos.tid)
        rows = self.path_rows(pos.tid)
        self.assertEqual(trade["status"], "closed")
        self.assertIsNotNone(trade["bid_path_summary"])
        self.assertEqual([row["terminal"] for row in rows].count(1), 1)
        self.assertTrue(all(row["sample_seq"] is not None for row in rows))

    def test_uncommitted_final_close_restores_open_trade_after_restart(self):
        pos = self.open_trade()
        self.desk._record_exec_path(pos, live_book(no={45.0: 100.0}), 55.0, 1001.0)

        with patch("app.store._persist_path_in_transaction",
                   side_effect=OSError("disk full")):
            self.desk.close(pos, 60.0, "target")

        # Restart: a fresh desk rehydrates from durable state only.
        restarted = paper.PaperDesk.__new__(paper.PaperDesk)
        restarted.realistic = True
        restarted.positions = {}
        restarted.pending_exits = {}
        restarted._safe_log = lambda *a, **k: None
        restarted.restore_open_positions(store.load_open_paper_positions())

        self.assertIn(
            pos.tid, restarted.positions,
            "an uncommitted close did not restore as an open, retryable trade",
        )

    # ------------------------------------------------------------------ §1.2

    def test_restored_position_resumes_at_durable_max_sequence(self):
        pos = self.open_trade()
        self.desk._record_exec_path(pos, live_book(no={49.0: 100.0}), 51.0, 1001.0)
        self.desk._record_exec_path(pos, live_book(no={48.0: 100.0}), 52.0, 1002.0)
        self.desk._flush_exec_path(pos)
        durable = self.path_rows(pos.tid)
        self.assertEqual([row["sample_seq"] for row in durable], [1, 2])

        restarted = paper.PaperDesk.__new__(paper.PaperDesk)
        restarted.realistic = True
        restarted.positions = {}
        restarted.pending_exits = {}
        restarted._safe_log = lambda *a, **k: None
        restarted._report_error = lambda *a, **k: None
        restarted.restore_open_positions(store.load_open_paper_positions())
        restored = restarted.positions[pos.tid]

        self.assertEqual(
            restored.exec_path_total, 2,
            "durable path length was not restored, so the next sample reuses a key",
        )

        restarted._record_exec_path(
            restored, live_book(no={40.0: 100.0}), 60.0, 1003.0,
        )
        self.assertEqual(
            [row["sample_seq"] for row in restored.exec_path], [3],
            "the first post-restart sample must continue at durable max + 1",
        )

        restarted._flush_exec_path(restored)
        seqs = [row["sample_seq"] for row in self.path_rows(pos.tid)]
        bids = [row["bid"] for row in self.path_rows(pos.tid)]
        self.assertEqual(seqs, [1, 2, 3], "the post-restart quote was silently dropped")
        self.assertIn(60.0, bids, "the new observation never became durable")

    def test_realistic_exit_rolls_back_path_fill_and_close_as_one_unit(self):
        """The final fill must not survive a failed path write."""
        pos = self.open_trade()
        progress = {"remaining": 0.0, "realized_gross": 1.0, "accrued_fees": 0.2,
                    "exit_qty": 10.0, "exit_vwap_num": 600.0}
        final = {"exit_px": 60.0, "gross": 1.0, "fees": 0.2, "net": 0.8,
                 "mae": 0.0, "shadow_stop_px": None}

        with patch("app.store._persist_path_in_transaction",
                   side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.record_paper_exit(
                    pos.tid, pos.signal_id, "yes", 1002.0, "target",
                    [(60.0, 10.0, 0.1)], progress, 5.0, final,
                    path_rows=[], truncated=False, dropped_samples=0,
                )

        trade = self.trade_row(pos.tid)
        self.assertEqual(trade["status"], "open", "the close survived a rolled-back path")
        self.assertIsNone(trade["exit_ts"])
        self.assertIsNone(trade["bid_path_summary"])
        self.assertEqual(
            store.q("SELECT COUNT(*) AS n FROM paper_fills WHERE trade_id=?"
                    " AND leg='exit'", (pos.tid,))[0]["n"],
            0, "the exit fill committed without its path",
        )

    def test_settlement_rolls_back_and_keeps_the_position_owned(self):
        pos = self.open_trade()
        self.desk.realistic = True
        self.desk.shadows = None
        self.desk._record_exec_path(pos, live_book(no={45.0: 100.0}), 55.0, 1001.0)

        with patch("app.store._persist_path_in_transaction",
                   side_effect=OSError("disk full")):
            self.desk.settle_market("KXGAME-YES", "yes")

        trade = self.trade_row(pos.tid)
        self.assertEqual(trade["status"], "open", "settlement half-committed")
        self.assertIn(pos.tid, self.desk.positions, "settlement dropped its owner")
        self.assertIn("paper_settle", [kind for kind, _ in self.errors])

        # Retry settles cleanly and exactly once.
        self.desk.settle_market("KXGAME-YES", "yes")
        trade = self.trade_row(pos.tid)
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(
            [row["terminal"] for row in self.path_rows(pos.tid)].count(1), 1,
            "retry left more than one terminal row",
        )

    def test_conflicting_trade_sequence_rolls_back_close_and_keeps_position(self):
        """A same-key/different-payload retry is corruption, never success."""
        pos = self.open_trade()
        self.desk._record_exec_path(pos, live_book(no={49.0: 100.0}), 51.0, 1001.0)
        self.desk._flush_exec_path(pos)
        self.assertEqual(len(self.path_rows(pos.tid)), 1)

        pos.exec_path = [{
            "kind": "position", "trade_id": pos.tid, "signal_id": pos.signal_id,
            "event": pos.event, "market": pos.market, "side": pos.side,
            "strategy": pos.strategy, "anchor_ts": pos.entry_ts, "dt_ms": 99.0,
            "bid": 99.0, "bid_size": 1.0, "exec_px": 99.0, "qty": 1.0,
            "sample_seq": 1, "availability": "quote", "terminal": 0,
        }]
        before = list(pos.exec_path)

        self.assertFalse(self.desk.close(pos, 60.0, "target"))
        self.assertEqual(self.trade_row(pos.tid)["status"], "open")
        self.assertIn(pos.tid, self.desk.positions)
        self.assertEqual(pos.exec_path, before, "the conflicting buffer lost ownership")
        self.assertEqual(len(self.path_rows(pos.tid)), 1, "conflict committed a partial close")
        self.assertTrue(any("path_sequence_conflict" in message for _, message in self.errors))


if __name__ == "__main__":
    unittest.main()
