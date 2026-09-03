import json
import os
import sqlite3
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

    def test_goal_latency_observation_persists_both_sides_of_market_window(self):
        row_id = store.insert_goal_latency({
            "observed_ts": 10.0,
            "event": "E",
            "milestone_id": "M",
            "change_kind": "goal",
            "live_type": "soccer",
            "score_before": {"homeScore": 0.0},
            "score_after": {"homeScore": 1.0},
            "previous_poll_ts": 9.75,
            "poll_started_ts": 9.99,
            "response_ms": 12.5,
            "last_book_change_ts": 9.9,
            "last_book_lead_ms": 100.0,
            "last_trade_ts": 9.8,
            "last_trade_lead_ms": 200.0,
            "detail": {"poll_uncertainty_ms": 250.0},
        })
        store.finish_goal_latency(
            row_id,
            {"wall": 10.1, "delta_ms": 100.0},
            {"wall": 10.2, "delta_ms": 200.0},
        )

        rows = store.q(
            """SELECT change_kind,last_book_lead_ms,first_book_after_ms,
                      first_trade_after_ms FROM goal_latency_observations"""
        )
        self.assertEqual(rows, [{
            "change_kind": "goal",
            "last_book_lead_ms": 100.0,
            "first_book_after_ms": 100.0,
            "first_trade_after_ms": 200.0,
        }])

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
        ), [{"kind": "paper_entry_ms", "ms": 150.0}])

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

    def test_a_walk_past_the_recorded_depth_is_unverifiable_not_failed(self):
        """Production regression: the only two K1 failures were truncations.

        The arrival snapshot is capped at a fixed depth, so trades 39 and 64
        (15 and 14 levels walked, 8 recorded) reported failed fills when the
        evidence simply did not reach that far. Their first eight levels
        matched the book exactly and no level exceeded available depth.
        """
        trade = {
            "side": "yes",
            "cap": 50.0,
            "size": 4.0,
            "entry_px": 46.0,
            "notional": 1.9,
            "book_at_entry": json.dumps({
                # Records depth to 45c only; the fill walked one level past it.
                "no_bids": [[55.0, 2.0]],
                "fill_levels": [[45.0, 2.0], [47.0, 2.0]],
            }),
        }
        self.assertIsNone(store._paper_fill_integrity(trade))

    def test_a_level_inside_the_recorded_range_must_still_be_supported(self):
        """Truncation tolerance must not become a hole in the check."""
        trade = {
            "side": "yes",
            "cap": 50.0,
            "size": 4.0,
            "entry_px": 44.5,
            "notional": 1.8,
            "book_at_entry": json.dumps({
                "no_bids": [[57.0, 2.0], [55.0, 2.0]],
                # 44c sits inside the recorded 43c..45c range but the book
                # holds no depth there, so this is a real inconsistency.
                "fill_levels": [[43.0, 2.0], [44.0, 2.0]],
            }),
        }
        self.assertFalse(store._paper_fill_integrity(trade))

    def test_snapshot_depth_covers_the_walk_it_is_evidence_for(self):
        from app.execution import ShadowBook

        book = ShadowBook.__new__(ShadowBook)
        book.yes_bids = {float(90 - i): 5.0 for i in range(20)}
        book.no_bids = {float(90 - i): 5.0 for i in range(20)}
        book.seq, book.ok = 1, True
        default = book.snapshot_dict()
        self.assertEqual(len(default["no_bids"]), ShadowBook.SNAPSHOT_DEPTH)
        self.assertTrue(default["truncated"])
        deep = book.snapshot_dict(depth=20)
        self.assertEqual(len(deep["no_bids"]), 20)
        self.assertFalse(deep["truncated"])

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

        self.assertEqual(gate["k4_latency_source"], "order_arrival_ms")
        self.assertEqual(gate["k4_latency_p95_ms"], 220.0)

    def test_zero_trade_stats_include_both_sleeves_and_reconcile(self):
        result = store.stats()

        self.assertEqual(result["combined"]["closed"], 0)
        self.assertEqual(result["combined"]["gross"], 0)
        self.assertEqual(result["combined"]["net"], 0)
        self.assertEqual(set(result["sleeves"]), {
            "gate_a", "price_only_late_score",
        })
        self.assertEqual(result["sleeves"]["gate_a"]["open"], 0)
        self.assertEqual(result["sleeves"]["price_only_late_score"]["signals"], {})

    def test_sleeve_economics_signals_exits_and_partial_state_reconcile(self):
        gate_signal = store.insert_signal({
            "ts_ms": 1, "local_ts": 1.0, "market": "G", "event": "EG",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 100,
            "ref": 30, "ext": 60, "late": True, "outcome": "filled",
            "detail": {"strategy": "gate_a"},
        })
        price_signal = store.insert_signal({
            "ts_ms": 2, "local_ts": 2.0, "market": "P", "event": "EP",
            "series": "S", "dir": 1, "dl": 1.0, "levels": 5, "size": 100,
            "ref": 30, "ext": 60, "late": True, "outcome": "filled",
            "detail": {"strategy": "price_only_late_score"},
        })
        store.ex(
            "INSERT INTO signals(event,outcome,detail,mode) VALUES(?,?,?,?)",
            ("EL", "rejected_cap", "{}", "live"),
        )
        store.ex(
            """INSERT INTO trades(signal_id,event,series,status,gross,fees,net,
                       exit_reason,strategy,mode,book_at_entry)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (gate_signal, "EG", "S", "closed", 10.0, 1.0, 9.0,
             "target", None, "live", "{}"),
        )
        store.ex(
            """INSERT INTO trades(signal_id,event,series,status,gross,fees,net,
                       exit_reason,strategy,mode,book_at_entry)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (price_signal, "EP", "S", "closed", 5.0, 2.0, 3.0,
             "sleeve_scratch", "price_only_late_score", "live", "{}"),
        )
        store.ex(
            """INSERT INTO trades(event,series,status,size,remaining,realized_gross,
                       accrued_fees,strategy,mode,book_at_entry)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("EG2", "S", "open", 5.0, 2.0, 0.5, 0.2, None, "live", "{}"),
        )
        store.ex(
            """INSERT INTO trades(event,series,status,size,remaining,realized_gross,
                       accrued_fees,strategy,mode,book_at_entry)
                 VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("EP2", "S", "open", 4.0, 1.0, 0.2, 0.1,
             "price_only_late_score", "live", "{}"),
        )

        result = store.stats()
        gate = result["sleeves"]["gate_a"]
        price = result["sleeves"]["price_only_late_score"]
        combined = result["combined"]

        self.assertEqual((gate["closed"], gate["open"], gate["net"]), (1, 1, 9.0))
        self.assertEqual((price["closed"], price["open"], price["net"]), (1, 1, 3.0))
        for field in ("closed", "open", "gross", "fees", "net",
                      "open_remaining_contracts", "open_partial_realized_gross",
                      "open_accrued_fees", "open_partial_realized_net"):
            self.assertAlmostEqual(combined[field], gate[field] + price[field])
        self.assertEqual(gate["exit_reasons"], {"target": 1})
        self.assertEqual(price["exit_reasons"], {"sleeve_scratch": 1})
        self.assertEqual(gate["signals"], {"filled": 1, "rejected_cap": 1})
        self.assertEqual(price["signals"], {"filled": 1})
        self.assertEqual(gate["evidence"]["k2_ci"]["n_signals"], 2)
        self.assertEqual(price["evidence"]["k2_ci"]["n_signals"], 1)
        self.assertEqual(result["net"], combined["net"])
        league = result["leagues"]["S"]
        self.assertEqual((league["n"], league["net"], league["win_pct"]),
                         (2, 12.0, 100.0))
        self.assertEqual(league["sleeves"]["gate_a"]["net"], 9.0)
        self.assertEqual(league["sleeves"]["price_only_late_score"]["net"], 3.0)


class OldVolumeMigrationTests(unittest.TestCase):
    def test_existing_volume_gets_strategy_and_canonical_event_columns(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = os.path.join(tempdir, "footballbot.db")
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE trades(
                  id INTEGER PRIMARY KEY, signal_id INTEGER, market TEXT,
                  event TEXT, series TEXT, dir INTEGER, side TEXT,
                  entry_ts REAL, entry_px REAL, size REAL, cap REAL, notional REAL,
                  exit_ts REAL, exit_px REAL, exit_reason TEXT, gross REAL, fees REAL,
                  net REAL, mae REAL, shadow_stop_px REAL, book_at_entry TEXT,
                  status TEXT DEFAULT 'open');
                CREATE TABLE goal_latency_observations(
                  id INTEGER PRIMARY KEY, observed_ts REAL NOT NULL, event TEXT NOT NULL,
                  milestone_id TEXT NOT NULL, change_kind TEXT NOT NULL, live_type TEXT,
                  score_before TEXT NOT NULL, score_after TEXT NOT NULL,
                  previous_poll_ts REAL, poll_started_ts REAL NOT NULL, response_ms REAL NOT NULL,
                  last_book_change_ts REAL, last_book_lead_ms REAL, last_trade_ts REAL,
                  last_trade_lead_ms REAL, first_book_after_ts REAL, first_book_after_ms REAL,
                  first_trade_after_ts REAL, first_trade_after_ms REAL, detail TEXT NOT NULL);
                CREATE TABLE markets(
                  ticker TEXT PRIMARY KEY, event TEXT, series TEXT, title TEXT,
                  close_time TEXT, status TEXT, added_ts REAL);
            """)
            conn.close()

            with patch("app.store.config.DATA_DIR", tempdir):
                store.init()
                trade_columns = {row["name"] for row in store.q("PRAGMA table_info(trades)")}
                event_columns = {
                    row["name"] for row in store.q(
                        "PRAGMA table_info(goal_latency_observations)"
                    )
                }
                market_columns = {
                    row["name"] for row in store.q("PRAGMA table_info(markets)")
                }
                store._conn.close()
                store._conn = None

        self.assertIn("strategy", trade_columns)
        self.assertTrue({
            "canonical_type", "canonical_side", "normalized_event",
        }.issubset(event_columns))
        self.assertTrue({"display_game", "display_leg"}.issubset(market_columns))


if __name__ == "__main__":
    unittest.main()
