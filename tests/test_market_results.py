"""B6: the settlement result of every watched market must be queryable.

Nothing persisted a market result anywhere a study could read it, so the
declined population -- most of the funnel -- had no outcome label at all and
no precision/recall question about it could be answered.  The settlement poll
also only ever looked at markets with an open paper position, so a market the
bot declined never got a result even when it settled minutes later.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config, store
from app.books import Book
from app.engine import Engine


def book_at(bid, ask, arrival_wall=1.0):
    book = Book()
    book.apply_snapshot({
        "yes_dollars_fp": [[f"{bid / 100:.2f}", "100"]],
        "no_dollars_fp": [[f"{(100 - ask) / 100:.2f}", "100"]],
    }, 1, arrival_wall=arrival_wall)
    return book


class MarketResultStoreTests(unittest.TestCase):
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
        store.upsert_market("T", "EV", "S", "T", None, "open")

    def close_store(self):
        if store._conn is not None:
            store._conn.close()
        store._conn = None

    def test_a_result_is_persisted_with_its_last_quote(self):
        written = store.record_market_result("T", "yes", 1_234.5, 41.0, 46.0)

        self.assertEqual(written, 1)
        row = store.market_result("T")
        self.assertEqual(row["result"], "yes")
        self.assertEqual(row["settled_ts"], 1_234.5)
        self.assertEqual(row["last_yes_bid"], 41.0)
        self.assertEqual(row["last_yes_ask"], 46.0)

    def test_the_first_settlement_time_and_a_known_quote_are_never_degraded(self):
        store.record_market_result("T", "yes", 1_000.0, 41.0, 46.0)
        store.record_market_result("T", "yes", 2_000.0, None, None)

        row = store.market_result("T")
        self.assertEqual(row["settled_ts"], 1_000.0)
        self.assertEqual(row["last_yes_bid"], 41.0)

    def test_a_non_binary_result_writes_nothing(self):
        self.assertEqual(store.record_market_result("T", "void"), 0)
        self.assertIsNone(store.market_result("T")["result"])

    def test_an_unregistered_ticker_does_not_invent_a_market(self):
        self.assertEqual(store.record_market_result("NOPE", "yes"), 0)
        self.assertIsNone(store.market_result("NOPE"))


class EngineSettlementTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        for target in ("app.store.config.DATA_DIR", "app.recorder.config.DATA_DIR"):
            patcher = patch(target, self._dir.name)
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

    def engine(self):
        engine = Engine.__new__(Engine)
        engine.mode = "live"
        engine.meta = {}
        engine.event_markets = {}
        engine.books = {}
        engine.prices = {}
        engine._watched_markets = set()
        engine._settled_markets = set()
        engine._path_write_tasks = set()
        engine._record_error = Mock()
        engine.desk = Mock()
        engine.desk.positions = {}
        return engine

    def test_a_settled_market_with_no_position_still_records_its_result(self):
        engine = self.engine()
        store.upsert_market("T", "EV", "S", "T", None, "open")
        engine.books["T"] = book_at(41.0, 46.0)

        engine._record_market_result("T", "yes", 1_500.0)

        row = store.market_result("T")
        self.assertEqual(row["result"], "yes")
        self.assertEqual(row["settled_ts"], 1_500.0)
        self.assertEqual(row["last_yes_bid"], 41.0)
        self.assertEqual(row["last_yes_ask"], 46.0)
        self.assertEqual(engine.desk.settle_market.call_count, 0)

    def test_the_lifecycle_frame_records_the_result_and_settles_the_desk(self):
        engine = self.engine()
        store.upsert_market("T", "EV", "S", "T", None, "open")
        engine.meta["T"] = {"event": "EV", "series": "S", "close_time": None}
        engine.recorder = Mock()
        engine.feed_backlog = 0
        engine._backlog_tick = 0
        engine.n_foreign = 0

        engine.handle_ws(
            {"type": "market_lifecycle_v2",
             "msg": {"market_ticker": "T", "settled_result": "no"}},
            100.0, 1.0,
        )

        self.assertEqual(store.market_result("T")["result"], "no")
        engine.desk.settle_market.assert_called_once_with("T", "no")

    def test_a_market_with_no_book_records_a_null_quote_not_a_guess(self):
        engine = self.engine()
        store.upsert_market("T", "EV", "S", "T", None, "open")

        engine._record_market_result("T", "yes", 1_500.0)

        row = store.market_result("T")
        self.assertEqual(row["result"], "yes")
        self.assertIsNone(row["last_yes_bid"])
        self.assertIsNone(row["last_yes_ask"])

    # ---- widened poll targets ----------------------------------------

    def test_the_poll_covers_expired_markets_the_bot_never_traded(self):
        engine = self.engine()
        engine._watched_markets = {"HELD", "EXPIRED", "STILL_RUNNING"}
        engine.meta = {
            "HELD": {"close_time": "2026-09-05T10:00:00Z"},
            "EXPIRED": {"close_time": "2026-09-05T10:00:00Z"},
            "STILL_RUNNING": {"close_time": "2126-09-05T10:00:00Z"},
        }
        held = Mock()
        held.market = "HELD"
        engine.desk.positions = {1: held}

        targets = engine._settlement_poll_targets(now=4_000_000_000.0)

        self.assertEqual(sorted(targets), ["EXPIRED", "HELD"])

    def test_a_market_already_settled_is_not_polled_again(self):
        engine = self.engine()
        engine._watched_markets = {"EXPIRED"}
        engine.meta = {"EXPIRED": {"close_time": "2026-09-05T10:00:00Z"}}
        engine._settled_markets = {"EXPIRED"}

        self.assertEqual(engine._settlement_poll_targets(4_000_000_000.0), [])

    def test_an_open_position_is_polled_even_after_its_result_is_recorded(self):
        """A settlement that failed to close a position must keep retrying."""
        engine = self.engine()
        engine._watched_markets = set()
        held = Mock()
        held.market = "HELD"
        engine.desk.positions = {1: held}
        engine._settled_markets = {"HELD"}

        self.assertEqual(engine._settlement_poll_targets(4_000_000_000.0), ["HELD"])

    def test_the_poll_is_bounded_per_cycle(self):
        engine = self.engine()
        engine._watched_markets = {f"M{i}" for i in range(200)}
        engine.meta = {tk: {"close_time": "2026-09-05T10:00:00Z"}
                       for tk in engine._watched_markets}

        targets = engine._settlement_poll_targets(4_000_000_000.0)

        self.assertEqual(len(targets), Engine.SETTLE_POLL_MAX)

    def test_the_socket_settlement_write_does_not_run_on_the_event_loop(self):
        """The platform pass took these writes off the loop; keep them off."""
        engine = self.engine()
        store.upsert_market("T", "EV", "S", "T", None, "open")

        async def scenario():
            with patch("app.engine.store.record_market_result") as direct:
                engine._record_market_result("T", "yes", 1.0)
                direct.assert_not_called()
            await asyncio.gather(*list(engine._path_write_tasks))

        asyncio.run(scenario())
        self.assertEqual(store.market_result("T")["result"], "yes")


if __name__ == "__main__":
    unittest.main()
