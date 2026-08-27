import unittest

from app.books import Book
from app.sequence import SubscriptionSequenceTracker


class SubscriptionSequenceTrackerTests(unittest.TestCase):
    def test_interleaved_markets_share_one_subscription_sequence(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(7, 100), "ok")  # snapshot: market A
        self.assertEqual(tracker.track(7, 101), "ok")  # snapshot: market B
        self.assertEqual(tracker.track(7, 102), "ok")  # delta: market A

    def test_duplicate_is_dropped_without_advancing(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(3, 10), "ok")
        self.assertEqual(tracker.track(3, 10), "duplicate")
        self.assertEqual(tracker.track(3, 11), "ok")

    def test_forward_gap_and_reset_both_require_recovery(self):
        tracker = SubscriptionSequenceTracker()

        self.assertEqual(tracker.track(4, 20), "ok")
        self.assertEqual(tracker.track(4, 23), "gap")
        self.assertEqual(tracker.track(4, 1), "gap")


class BookSequenceBoundaryTests(unittest.TestCase):
    def test_live_router_can_apply_interleaved_subscription_sequences(self):
        book = Book()
        book.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]]}, 100)

        applied = book.apply_delta(
            {"side": "yes", "price_dollars": "0.40", "delta_fp": "2"},
            102,
            sequence_validated=True,
        )

        self.assertTrue(applied)
        self.assertTrue(book.ok)
        self.assertEqual(book.yes_bids[40.0], 12.0)

    def test_legacy_isolated_book_check_remains_available(self):
        book = Book()
        book.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]]}, 100)

        applied = book.apply_delta(
            {"side": "yes", "price_dollars": "0.40", "delta_fp": "2"}, 102
        )

        self.assertFalse(applied)
        self.assertFalse(book.ok)


if __name__ == "__main__":
    unittest.main()
