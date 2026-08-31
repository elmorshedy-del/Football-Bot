import unittest
from pathlib import Path
from unittest.mock import patch

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
