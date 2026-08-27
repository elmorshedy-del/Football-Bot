import json
import tempfile
import unittest
from unittest.mock import patch

from app import store


class PaperExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_patch = patch("app.store.config.DATA_DIR", self.tempdir.name)
        self.data_patch.start()
        store.init()
        store.set_mode("live")

    def tearDown(self):
        store._conn.close()
        store._conn = None
        self.data_patch.stop()
        self.tempdir.cleanup()

    def insert_signal(self):
        return store.insert_signal({
            "ts_ms": 1,
            "local_ts": 1.0,
            "market": "T",
            "event": "E",
            "series": "S",
            "dir": 1,
            "dl": 10.0,
            "levels": 2,
            "size": 3.0,
            "ref": 40.0,
            "ext": 60.0,
            "late": True,
            "outcome": "queued",
        })

    def test_open_and_close_persist_each_level_and_progress_atomically(self):
        signal_id = self.insert_signal()
        trade = {
            "signal_id": signal_id,
            "market": "T",
            "event": "E",
            "series": "S",
            "dir": 1,
            "side": "yes",
            "entry_ts": 2.0,
            "entry_px": 45.0,
            "size": 2.0,
            "cap": 50.0,
            "notional": 0.9,
            "book_at_entry": {},
        }
        trade_id = store.open_paper_trade(
            trade, {"paper_latency_ms": 100.0}, [(45.0, 2.0, 0.04)], 0.04, 100.0,
        )

        store.record_paper_exit(
            trade_id,
            signal_id,
            "yes",
            3.0,
            "target",
            [(60.0, 2.0, 0.04)],
            {
                "remaining": 0.0,
                "realized_gross": 0.30,
                "accrued_fees": 0.08,
                "exit_qty": 2.0,
                "exit_vwap_num": 120.0,
            },
            100.0,
            {
                "exit_px": 60.0,
                "gross": 0.30,
                "fees": 0.08,
                "net": 0.22,
                "mae": 0.0,
                "shadow_stop_px": None,
            },
        )

        self.assertEqual(store.q("SELECT outcome FROM signals"), [{"outcome": "filled"}])
        self.assertEqual(store.q(
            "SELECT status,remaining,net FROM trades"
        ), [{"status": "closed", "remaining": 0.0, "net": 0.22}])
        self.assertEqual(store.q(
            "SELECT leg,price,quantity,fee FROM paper_fills ORDER BY id"
        ), [
            {"leg": "entry", "price": 45.0, "quantity": 2.0, "fee": 0.04},
            {"leg": "exit", "price": 60.0, "quantity": 2.0, "fee": 0.04},
        ])

    def test_non_fill_signal_and_latency_commit_together(self):
        signal_id = self.insert_signal()

        store.finish_paper_signal(
            signal_id, "no_book", {"paper_latency_ms": 150.0}, 150.0,
        )

        self.assertEqual(store.q("SELECT outcome FROM signals"), [{"outcome": "no_book"}])
        self.assertEqual(store.q(
            "SELECT kind,ms FROM latency"
        ), [{"kind": "paper_entry", "ms": 150.0}])

    def test_open_position_loader_includes_signal_and_fill_state(self):
        signal_id = self.insert_signal()
        trade_id = store.open_paper_trade({
            "signal_id": signal_id,
            "market": "T",
            "event": "E",
            "series": "S",
            "dir": 1,
            "side": "yes",
            "entry_ts": 2.0,
            "entry_px": 45.0,
            "size": 2.0,
            "cap": 50.0,
            "notional": 0.9,
            "book_at_entry": {},
        }, {"paper_latency_ms": 100.0}, [(45.0, 2.0, 0.04)], 0.04, 100.0)

        rows = store.load_open_paper_positions()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], trade_id)
        self.assertEqual(rows[0]["remaining"], 2.0)
        self.assertEqual(rows[0]["entry_fees"], 0.04)
        self.assertEqual(rows[0]["ref"], 40.0)

    def test_k1_validates_fill_against_recorded_arrival_depth(self):
        trade = {
            "side": "yes",
            "cap": 50.0,
            "size": 2.0,
            "entry_px": 45.0,
            "notional": 0.9,
            "book_at_entry": json.dumps({
                "no_bids": [[55.0, 2.0]],
                "fill_levels": [[45.0, 2.0]],
            }),
        }

        self.assertTrue(store._paper_fill_integrity(trade))
        trade["size"] = 3.0
        self.assertFalse(store._paper_fill_integrity(trade))

    def test_k2_cannot_pass_before_fifty_confirmed_signals(self):
        for index in range(5):
            store.ex(
                "INSERT INTO signals(event,outcome,mode) VALUES(?,?,?)",
                (f"E{index}", "filled", "live"),
            )
            store.ex(
                "INSERT INTO trades(event,series,status,net,mode) VALUES(?,?,?,?,?)",
                (f"E{index}", "S", "closed", 10.0, "live"),
            )

        gate = store.stats()["kill"]["k2_ci"]

        self.assertEqual(gate["n_signals"], 5)
        self.assertEqual(gate["ci"], [10.0, 10.0])
        self.assertEqual(gate["status"], "COLLECTING")

    def test_k4_prefers_total_order_arrival_latency(self):
        store.add_latency("feed_lag", 10.0)
        store.add_latency("order_arrival", 220.0)

        gate = store.stats()["kill"]

        self.assertEqual(gate["k4_latency_source"], "order_arrival")
        self.assertEqual(gate["k4_latency_p95_ms"], 220.0)


if __name__ == "__main__":
    unittest.main()
