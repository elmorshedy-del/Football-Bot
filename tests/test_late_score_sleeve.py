import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.books import Book
from app.late_score_sleeve import (
    PriceOnlyLateScoreSleeve,
    leg_role,
    sleeve_exit_reason,
)
from app.paper import Position, fee_dollars


def quoted_book(mid, spread=2.0):
    book = Book()
    bid, ask = mid - spread / 2.0, mid + spread / 2.0
    book.yes_bids = {bid: 500.0}
    book.no_bids = {100.0 - ask: 500.0}
    book.ok = True
    return book


class PriceOnlyClassifierTests(unittest.TestCase):
    def setUp(self):
        self.event = "E"
        self.tickers = ["E-HOME", "E-TIE", "E-AWAY"]
        self.meta = {
            "E-HOME": {"title": "Home"},
            "E-TIE": {"title": "Tie"},
            "E-AWAY": {"title": "Away"},
        }
        self.sleeve = PriceOnlyLateScoreSleeve()

    def observe(self, mids, ts=0.0):
        books = {ticker: quoted_book(mids[ticker]) for ticker in self.tickers}
        self.assertIsNone(self.sleeve.observe(
            self.event, self.tickers, self.meta, books, ts,
        ))
        return books

    @staticmethod
    def candidate(ticker):
        return {"ticker": ticker, "dir": 1}

    def test_market_identity_finds_draw_without_score_feed(self):
        self.assertEqual(leg_role("MATCH-TIE", "Anything"), "draw")
        self.assertEqual(leg_role("MATCH-H", "Home team"), "team")

    def test_coherent_draw_surge_is_inferred_equalizer(self):
        self.observe({"E-HOME": 25, "E-TIE": 30, "E-AWAY": 45})
        books = {"E-HOME": quoted_book(15), "E-TIE": quoted_book(55),
                 "E-AWAY": quoted_book(30)}

        decision = self.sleeve.classify(
            self.candidate("E-TIE"), self.event, self.tickers,
            self.meta, books, 2000.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.detail["inferred_state"], "equal_score_0")
        self.assertEqual(decision.detail["sibling_explanation"], 1.0)

    def test_coherent_team_surge_is_inferred_one_goal_lead(self):
        self.observe({"E-HOME": 25, "E-TIE": 30, "E-AWAY": 45})
        books = {"E-HOME": quoted_book(50), "E-TIE": quoted_book(20),
                 "E-AWAY": quoted_book(30)}

        decision = self.sleeve.classify(
            self.candidate("E-HOME"), self.event, self.tickers,
            self.meta, books, 2000.0,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.detail["inferred_state"], "one_goal_lead_+1")

    def test_sibling_rise_rejects_single_leg_quote_noise(self):
        self.observe({"E-HOME": 25, "E-TIE": 30, "E-AWAY": 45})
        books = {"E-HOME": quoted_book(50), "E-TIE": quoted_book(35),
                 "E-AWAY": quoted_book(20)}

        decision = self.sleeve.classify(
            self.candidate("E-HOME"), self.event, self.tickers,
            self.meta, books, 2000.0,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "incoherent_sibling_rise")

    def test_fails_closed_without_a_timed_baseline(self):
        books = {ticker: quoted_book(mid) for ticker, mid in zip(
            self.tickers, (50, 20, 30),
        )}
        self.sleeve.observe(
            self.event, self.tickers, self.meta, books, 2000.0,
        )

        decision = self.sleeve.classify(
            self.candidate("E-HOME"), self.event, self.tickers,
            self.meta, books, 2000.0,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "no_baseline")

    def test_price_only_path_does_not_import_match_feed_fields(self):
        root = Path(__file__).resolve().parents[1]
        import ast
        for filename in ("late_score_sleeve.py", "detector.py", "paper.py"):
            source = (root / "app" / filename).read_text()
            for forbidden in (
                "goal_latency", "normalize_match_event", "score_before",
                "score_after", "normalized_event", "live_data",
            ):
                self.assertNotIn(forbidden, source)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn("goal_latency", node.module)
                    self.assertNotIn("match_events", node.module)
                    self.assertNotRegex(node.module or "", r"(^|\.)match_clock$")
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, {
                        "score_before", "score_after", "scorer", "live_data",
                        "normalized_event",
                    })


    def test_engine_price_only_decision_path_reads_no_match_feed_content(self):
        """The orchestrator is the weak link the module-level scan cannot cover.

        engine.py legitimately imports both match_clock and goal_latency, so a
        whole-file scan cannot protect it.  Walk only the functions that make
        the price-only admission decision and prove no score, scorer, event, or
        narrative field is referenced inside them.
        """
        import ast
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "app" / "engine.py").read_text())
        forbidden = {
            "score_before", "score_after", "scorer", "live_data",
            "normalized_event", "canonical_type", "canonical_side",
            "human_label", "provider_description", "goal_latency",
            "normalize_match_event", "change_kind",
        }
        checked = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in {"_run_price_only", "_clock_gate_for"}:
                continue
            checked.append(node.name)
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name):
                    self.assertNotIn(inner.id, forbidden, f"{node.name} reads {inner.id}")
                if isinstance(inner, ast.Attribute):
                    self.assertNotIn(
                        inner.attr, forbidden, f"{node.name} reads .{inner.attr}",
                    )
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    self.assertNotIn(
                        inner.value, forbidden, f"{node.name} names {inner.value!r}",
                    )
        self.assertEqual(sorted(checked), ["_clock_gate_for", "_run_price_only"])


class PriceOnlyExitTests(unittest.TestCase):
    def position(self):
        pos = Position(
            1, 2, "T", "E", "S", 1, "yes", 45.0, 10.0, 25.0, 50.0,
            sleeve={"strategy": "price_only_late_score_v1"},
        )
        pos.entry_fees = 0.0
        return pos

    def test_fee_aware_scratch_arms_only_after_profit(self):
        pos = self.position()

        self.assertIsNone(sleeve_exit_reason(pos, 44.0, 1.0, fee_dollars))
        self.assertIsNone(sleeve_exit_reason(pos, 50.0, 2.0, fee_dollars))
        self.assertEqual(
            sleeve_exit_reason(pos, 47.0, 3.0, fee_dollars),
            "sleeve_scratch",
        )

    def test_trailing_exit_locks_a_larger_move(self):
        pos = self.position()

        self.assertIsNone(sleeve_exit_reason(pos, 44.0, 1.0, fee_dollars))
        self.assertIsNone(sleeve_exit_reason(pos, 52.0, 2.0, fee_dollars))
        self.assertEqual(
            sleeve_exit_reason(pos, 48.0, 3.0, fee_dollars),
            "sleeve_profit_lock",
        )

    def test_reversal_is_measured_from_first_executable_bid_not_entry_ask(self):
        pos = self.position()

        self.assertIsNone(sleeve_exit_reason(pos, 43.5, 1.0, fee_dollars))
        with patch("app.late_score_sleeve.config.SLEEVE_REVERSAL_C", 3.0):
            self.assertEqual(
                sleeve_exit_reason(pos, 40.5, 2.0, fee_dollars),
                "sleeve_reversal",
            )


if __name__ == "__main__":
    unittest.main()


class RejectionEvidenceTests(unittest.TestCase):
    """A refusal must carry the numbers that produced it.

    In the first live study the early refusals returned bare reasons: a
    `wide_spread` row recorded that the book was too wide but never how wide,
    so no later analysis could ask what a different cap would have admitted.
    """

    class FakeBook:
        def __init__(self, bid, ask, ok=True):
            self._bid, self._ask, self.ok = bid, ask, ok

        def best_yes_bid(self):
            return self._bid

        def best_yes_ask(self):
            return self._ask

    def books(self, spreads):
        return {t: self.FakeBook(50 - s / 2.0, 50 + s / 2.0)
                for t, s in spreads.items()}

    def classify(self, books, tickers=None, observe=False):
        sleeve = PriceOnlyLateScoreSleeve()
        tickers = tickers or list(books)
        meta = {t: {"title": "Tie" if t.endswith("TIE") else "Team"}
                for t in tickers}
        if observe:
            # Mark every leg as just seen so the freshness gate passes and the
            # refusal under test is the baseline one.
            sleeve.last_leg_observation["E"].update({t: 1000.0 for t in tickers})
        return sleeve.classify(
            {"ticker": tickers[0], "dir": 1}, "E", tickers, meta, books, 1000.0,
        )

    def test_wide_spread_records_how_wide_and_against_what_limit(self):
        books = self.books({"A": 2.0, "B-TIE": 3.0, "C": 40.0})
        decision = self.classify(books)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "wide_spread")
        self.assertEqual(decision.detail["widest_leg"], "C")
        self.assertEqual(decision.detail["widest_spread_c"], 40.0)
        self.assertEqual(decision.detail["max_spread_c_limit"],
                         config.SLEEVE_MAX_SPREAD_C)
        # Every leg's width is present, not just the offending one.
        self.assertEqual(len(decision.detail["spread_c"]), 3)

    def test_incomplete_book_names_the_leg_that_was_missing(self):
        books = self.books({"A": 2.0, "B-TIE": 2.0, "C": 2.0})
        books["C"] = self.FakeBook(None, None, ok=True)
        decision = self.classify(books)
        self.assertEqual(decision.reason, "incomplete_book")
        self.assertEqual(decision.detail["missing_leg"], "C")

    def test_baseline_refusals_record_the_history_they_lacked(self):
        books = self.books({"A": 2.0, "B-TIE": 2.0, "C": 2.0})
        decision = self.classify(books, observe=True)
        self.assertEqual(decision.reason, "no_baseline")
        self.assertEqual(decision.detail["baseline_rows"], 0)
        self.assertEqual(decision.detail["baseline_eligible"], 0)
        self.assertEqual(decision.detail["baseline_lag_ms"],
                         config.SLEEVE_BASELINE_MS)
        self.assertEqual(decision.detail["max_baseline_age_ms"],
                         config.SLEEVE_MAX_BASELINE_AGE_MS)

    def test_not_triplet_records_the_leg_count(self):
        books = self.books({"A": 2.0, "B-TIE": 2.0})
        decision = self.classify(books)
        self.assertEqual(decision.reason, "not_triplet")
        self.assertEqual(decision.detail["leg_count"], 2)
