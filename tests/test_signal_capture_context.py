"""B3: a signal row must explain the market it was produced in.

The first live study could not analyse its own funnel.  76% of Gate-A rows
(1,150 of 1,511) ended `unconfirmed` with nothing recorded about the sibling
tape that failed to confirm them, and no declined row said what an entry would
have filled at, so the whole declined population carried no counterfactual.  A
raw L2 replay of 2026-09-04 20:00-22:00 (1.9 M frames, 78 Gate-A candidates)
then showed the entry assumption alone moved the same 29 trades between
-$862.96 and +$550.33, which is only decidable if each row records the book it
saw.

These tests pin what every outcome now carries, and that the two rows
`parallel` mode writes for one episode share a key.
"""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config, store
from app.books import Book
from app.detector import Detector
from app.engine import Engine
from app.match_clock import MatchClockTracker
from app.paper import PaperDesk


def book_with(yes_bids=(), no_bids=()):
    book = Book()
    book.apply_snapshot({
        "yes_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in yes_bids],
        "no_dollars_fp": [[f"{p / 100:.2f}", str(s)] for p, s in no_bids],
    }, 1)
    return book


class CaptureContextTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(config, "DATA_DIR", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        if store._conn is not None:
            store._conn.close()
        store._conn = None
        store.init()
        store.set_mode("live")
        self.addCleanup(self.close_store)

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def make_engine(self):
        engine = Engine.__new__(Engine)
        engine.mode = "live"
        engine.meta = {
            "T": {"event": "E", "series": "S", "close_time": None},
            "D": {"event": "E", "series": "S", "close_time": None},
            "A": {"event": "E", "series": "S", "close_time": None},
        }
        engine.event_markets = {"E": ["T", "D", "A"]}
        engine.books = {
            # 40/45 with 300 resting on the bid and 500 offered at 45.
            "T": book_with(yes_bids=[(40, 300)], no_bids=[(55, 500)]),
            "D": book_with(yes_bids=[(20, 100)], no_bids=[(75, 200)]),
            "A": book_with(yes_bids=[(30, 50)], no_bids=[(65, 400)]),
        }
        engine.prices = {"T": {"last": 44.0}}
        engine.pending = []
        engine._signal_paths = []
        engine.desk = Mock()
        engine.desk.positions = {}
        engine.clock_tracker = MatchClockTracker()
        engine.broadcast = Mock()
        engine._record_error = Mock()
        engine._watch_signal_forward = Mock()
        engine._announce_signal = Mock()
        return engine

    def candidate(self, **over):
        row = {"ticker": "T", "ts_ms": 5_000, "local_ts": 10.0, "dir": 1,
               "dl": 1.0, "signed": 1.0, "levels": 6, "size": 300.0,
               "ref": 40.0, "ext": 55.0,
               "context": {"arrival_wall": 7.25, "proc_wall": 7.5, "backlog": 12}}
        row.update(over)
        return row

    def stored(self, sid):
        row = store.q("SELECT context, episode_id FROM signals WHERE id=?", (sid,))[0]
        return json.loads(row["context"]), row["episode_id"]

    # ---- context on every outcome -------------------------------------

    def test_every_outcome_records_books_spread_fillable_and_load(self):
        engine = self.make_engine()
        engine.desk.positions = {1: object(), 2: object()}
        engine.pending = [object()]
        engine._signal_paths = [object(), object(), object()]

        for outcome in ("filled", "unconfirmed", "rejected_cap",
                        "sleeve_clock_stale", "strategy_lockout"):
            sid = engine.record_signal(self.candidate(), None, outcome,
                                       announce=False)
            context, _episode = self.stored(sid)
            with self.subTest(outcome=outcome):
                # The frame context the platform pass added is preserved.
                self.assertEqual(context["backlog"], 12)
                self.assertAlmostEqual(context["feed_lag_ms"], 2250.0, places=3)
                self.assertEqual(sorted(context["books"]), ["A", "D", "T"])
                own = context["books"]["T"]
                self.assertEqual(own["bid"], 40.0)
                self.assertEqual(own["ask"], 45.0)
                self.assertEqual(own["bid_size"], 300.0)
                self.assertEqual(own["ask_size"], 500.0)
                self.assertEqual(own["last"], 44.0)
                self.assertEqual(own["mid"], 42.5)
                self.assertEqual(context["spread_c"], 5.0)
                self.assertEqual(context["load"], {
                    "open_watches": 3, "open_positions": 2,
                    "pending_candidates": 1,
                })
                self.assertIsNotNone(context["fillable"])

    def test_a_leg_with_no_usable_book_is_null_not_invented(self):
        engine = self.make_engine()
        engine.books["A"].ok = False
        engine.books.pop("D")

        sid = engine.record_signal(self.candidate(), None, "unconfirmed",
                                   announce=False)
        context, _episode = self.stored(sid)

        self.assertIsNone(context["books"]["A"])
        self.assertIsNone(context["books"]["D"])
        self.assertIsNotNone(context["books"]["T"])

    def test_fillable_matches_what_an_entry_would_actually_have_taken(self):
        """The counterfactual has to be the real walk, not an approximation."""
        engine = self.make_engine()
        # A laddered offer so the walk crosses more than one level.
        engine.books["T"] = book_with(
            yes_bids=[(40, 300)],
            no_bids=[(58, 40), (56, 60), (50, 1000)],
        )

        sid = engine.record_signal(self.candidate(), None, "unconfirmed",
                                   announce=False)
        context, _episode = self.stored(sid)
        fillable = context["fillable"]

        desk = PaperDesk(lambda _m: None, realistic=False)
        with patch("app.paper.store.insert_trade", return_value=1), \
                patch("app.paper.store.log_event"):
            outcome = desk.try_enter(
                1, self.candidate(), {"event": "E", "series": "S"},
                engine.books["T"],
            )
        self.assertEqual(outcome, "filled")
        position = desk.positions[1]

        self.assertEqual(fillable["levels"], 3)
        self.assertAlmostEqual(fillable["vwap"], round(position.entry_px, 2),
                               places=2)
        self.assertAlmostEqual(fillable["qty"], round(position.size, 1), places=1)
        self.assertEqual(fillable["price_cap"], config.PRICE_CAP)

    def test_fillable_respects_the_price_cap(self):
        engine = self.make_engine()
        engine.books["T"] = book_with(
            yes_bids=[(40, 300)], no_bids=[(30, 1000)],  # ask 70c, above the cap
        )

        sid = engine.record_signal(self.candidate(), None, "rejected_cap",
                                   announce=False)
        context, _episode = self.stored(sid)

        self.assertEqual(context["fillable"]["qty"], 0.0)
        self.assertIsNone(context["fillable"]["vwap"])
        self.assertEqual(context["fillable"]["levels"], 0)

    # ---- sibling confirmation evidence --------------------------------

    def test_an_unconfirmed_row_records_the_sibling_bursts_it_weighed(self):
        detector = Detector()
        candidate = {"ticker": "T", "ts_ms": 100_000, "signed": 0.9}
        sibling = detector.state("D")
        sibling.big_bursts.append((100_030, +0.7, 4))   # same sign, in window
        sibling.big_bursts.append((100_120, -0.8, 6))   # opposite, out of window

        ok, lag, evidence = detector.confirm(candidate, ["D", "A"])

        self.assertFalse(ok)
        self.assertIsNone(lag)
        self.assertEqual(evidence["siblings"], 2)
        self.assertEqual(evidence["siblings_with_tape"], 1)
        self.assertEqual(evidence["window_ms"], config.CONF_MS)
        recorded = {row["lag_ms"]: row for row in evidence["bursts"]}
        self.assertEqual(sorted(recorded), [30.0, 120.0])
        self.assertTrue(recorded[30.0]["in_window"])
        self.assertFalse(recorded[30.0]["opposite_sign"])
        self.assertEqual(recorded[30.0]["levels"], 4)
        self.assertEqual(recorded[30.0]["sibling_ticker"], "D")
        self.assertFalse(recorded[120.0]["in_window"])
        self.assertTrue(recorded[120.0]["opposite_sign"])

    def test_a_confirmation_still_decides_exactly_as_before(self):
        """Evidence is telemetry; it must not move a single admission."""
        detector = Detector()
        candidate = {"ticker": "T", "ts_ms": 100_000, "signed": 0.9}
        detector.state("D").big_bursts.append((100_010, -0.8, 5))

        ok, lag, _evidence = detector.confirm(candidate, ["D"])

        self.assertTrue(ok)
        self.assertEqual(lag, 10)

    def test_the_evidence_list_is_bounded_in_a_hot_market(self):
        detector = Detector()
        candidate = {"ticker": "T", "ts_ms": 100_000, "signed": 0.9}
        state = detector.state("D")
        for offset in range(-300, 300, 2):
            state.big_bursts.append((100_000 + offset, -0.5, 3))

        _ok, _lag, evidence = detector.confirm(candidate, ["D"])

        self.assertLessEqual(len(evidence["bursts"]), 12)
        self.assertGreater(evidence["bursts_near"], len(evidence["bursts"]))
        self.assertGreater(evidence["bursts_scanned"], evidence["bursts_near"])
        # The nearest bursts are the ones kept.
        self.assertLessEqual(max(abs(r["lag_ms"]) for r in evidence["bursts"]), 12)

    def test_the_engine_persists_confirmation_evidence_on_an_unconfirmed_row(self):
        engine = self.make_engine()
        engine.detector = Detector()
        engine.detector.state("D").big_bursts.append((5_030, +0.7, 4))
        engine.record_subthreshold = Mock()
        engine._observe_sleeve = Mock()
        engine.is_late = Mock(return_value=False)
        engine.n_trades = 0
        engine.pending = [{
            "cand": self.candidate(), "siblings": ["D"],
            "queued_at": 0.0, "deadline": 0.0,
        }]

        engine.process_trade("D", 5_030, 44.0, 10.0, "yes", 7.25)

        row = store.q(
            "SELECT context FROM signals WHERE outcome='unconfirmed'")[0]
        confirmation = json.loads(row["context"])["confirmation"]
        self.assertEqual(confirmation["attempts"], 1)
        self.assertEqual(confirmation["bursts"][0]["sibling_ticker"], "D")
        self.assertEqual(confirmation["bursts"][0]["lag_ms"], 30.0)

    # ---- sub-threshold rows get the cheap subset ----------------------

    def test_a_subthreshold_row_gets_top_of_book_and_spread_only(self):
        engine = self.make_engine()

        engine.record_subthreshold({
            "ticker": "T", "ts_ms": 5_000, "local_ts": 10.0, "dir": 1,
            "dl": 0.5, "levels": 3, "size": 60.0, "ref": 40.0, "ext": 45.0,
            "below": ["size"],
            "context": {"arrival_wall": 6.0, "proc_wall": 6.1, "backlog": 3},
        })

        row = store.q(
            "SELECT context, episode_id FROM signals"
            " WHERE outcome='subthreshold'")[0]
        context = json.loads(row["context"])

        self.assertEqual(context["backlog"], 3)
        self.assertEqual(sorted(context["books"]), ["A", "D", "T"])
        self.assertEqual(context["books"]["T"], {
            "bid": 40.0, "ask": 45.0, "mid": 42.5, "spread_c": 5.0,
        })
        self.assertEqual(context["spread_c"], 5.0)
        # Deliberately absent: these rows are numerous by design.
        self.assertNotIn("fillable", context)
        self.assertNotIn("load", context)
        # A near miss is not an episode.
        self.assertIsNone(row["episode_id"])

    def test_a_capture_failure_never_costs_the_row(self):
        engine = self.make_engine()
        del engine.books

        sid = engine.record_signal(self.candidate(), None, "unconfirmed",
                                   announce=False)
        context, _episode = self.stored(sid)

        self.assertIn("capture_error", context)
        self.assertEqual(context["backlog"], 12)

    # ---- episode identity ---------------------------------------------

    def test_the_parallel_pair_shares_one_stable_episode_id(self):
        engine = self.make_engine()
        cand = self.candidate()
        gate_a = engine._strategy_candidate(cand, "gate_a")
        price_only = engine._strategy_candidate(cand, "price_only_late_score")

        first = engine.record_signal(gate_a, None, "filled", announce=False)
        second = engine.record_signal(price_only, None, "sleeve_clock_stale",
                                      announce=False)

        _c1, episode_one = self.stored(first)
        _c2, episode_two = self.stored(second)
        self.assertEqual(episode_one, "T:5000")
        self.assertEqual(episode_one, episode_two)
        # Stable: re-deriving it from the same candidate gives the same key.
        self.assertEqual(engine.episode_id(cand), episode_one)

    def test_every_episode_outcome_carries_the_key(self):
        engine = self.make_engine()
        for outcome in ("unconfirmed", "confirmed_late", "strategy_lockout",
                        "queued", "sleeve_no_baseline"):
            sid = engine.record_signal(self.candidate(), None, outcome,
                                       announce=False)
            _context, episode = self.stored(sid)
            with self.subTest(outcome=outcome):
                self.assertEqual(episode, "T:5000")

    def test_the_funnel_counts_are_unchanged_by_the_new_column(self):
        """stats() keys on outcome per strategy; episode_id must not touch it."""
        engine = self.make_engine()
        cand = self.candidate()
        engine.record_signal(engine._strategy_candidate(cand, "gate_a"),
                             None, "unconfirmed", announce=False)
        engine.record_signal(
            engine._strategy_candidate(cand, "price_only_late_score"),
            None, "unconfirmed", announce=False)

        stats = store.stats()
        self.assertEqual(stats["signals"]["unconfirmed"], 2)
        self.assertEqual(
            stats["sleeves"]["gate_a"]["signals"]["unconfirmed"], 1)
        self.assertEqual(
            stats["sleeves"]["price_only_late_score"]["signals"]["unconfirmed"], 1)


if __name__ == "__main__":
    unittest.main()
