"""In-memory order books built from orderbook_snapshot + orderbook_delta.

Kalshi book semantics: `yes` side = resting YES bids, `no` side = resting NO
bids. A NO bid at price q is an offer to sell YES at (100 - q). All prices
kept internally in cents (float, supports sub-cent)."""
import time


class Book:
    __slots__ = ("yes_bids", "no_bids", "last_seq", "ok", "ts_ms",
                 "last_exchange_ts_ms", "last_arrival_wall")

    def __init__(self):
        self.yes_bids = {}   # price_c -> size
        self.no_bids = {}
        self.last_seq = None
        self.ok = False
        self.ts_ms = 0
        # When the exchange stamped the newest update this book carries, and
        # when this process received it.  A fill taken from a 16 s-old book is
        # a different claim from one taken at the touch, and until these
        # existed the difference was unrecorded: a raw L2 replay of
        # 2026-09-04 20:00-22:00 found the reconstructed best ask worse than a
        # real same-side executed price for 29 of 29 candidates (median +12c)
        # while the process ran 5.6 s median behind the exchange.
        self.last_exchange_ts_ms = None
        self.last_arrival_wall = None

    def apply_snapshot(self, msg, seq, arrival_wall=None):
        self.yes_bids = {float(p) * 100: float(s) for p, s in (msg.get("yes_dollars_fp") or [])}
        self.no_bids = {float(p) * 100: float(s) for p, s in (msg.get("no_dollars_fp") or [])}
        self.last_seq = seq
        self.ok = True
        self._stamp(msg, arrival_wall)

    def apply_delta(self, msg, seq, sequence_validated=False, arrival_wall=None):
        """Apply one delta after optional subscription-level validation.

        Kalshi sequences the entire WebSocket subscription, not each ticker.
        Live routing therefore validates sequence numbers once per ``sid`` and
        passes ``sequence_validated=True``.  The legacy per-book check remains
        available for isolated callers and old replay fixtures.
        """
        if (not sequence_validated and self.last_seq is not None and seq is not None
                and seq != self.last_seq + 1):
            self.ok = False
            return False
        self.last_seq = seq if seq is not None else self.last_seq
        px = float(msg["price_dollars"]) * 100
        d = float(msg["delta_fp"])
        side = self.yes_bids if msg.get("side") == "yes" else self.no_bids
        nv = side.get(px, 0.0) + d
        if nv <= 1e-9:
            side.pop(px, None)
        else:
            side[px] = nv
        self.ts_ms = msg.get("ts_ms") or self.ts_ms
        self._stamp(msg, arrival_wall)
        return True

    def _stamp(self, msg, arrival_wall):
        """Record the age inputs.  Never invents an exchange stamp.

        `arrival_wall` is the reader's receipt stamp when the caller has one;
        it falls back to now so an isolated caller still produces a usable age
        rather than a null.  A frame with no `ts_ms` leaves the exchange stamp
        as it was: a missing provider timestamp stays missing.
        """
        exchange = msg.get("ts_ms") if isinstance(msg, dict) else None
        if isinstance(exchange, (int, float)) and not isinstance(exchange, bool):
            self.last_exchange_ts_ms = float(exchange)
        self.last_arrival_wall = time.time() if arrival_wall is None else float(arrival_wall)

    # --- views (YES space) ---
    def best_yes_bid(self):
        return max(self.yes_bids) if self.yes_bids else None

    def best_yes_ask(self):
        return 100 - max(self.no_bids) if self.no_bids else None

    def best_no_bid(self):
        return max(self.no_bids) if self.no_bids else None

    def best_no_ask(self):
        return 100 - max(self.yes_bids) if self.yes_bids else None

    def ask_ladder(self, side):
        """Ladder to BUY `side` ('yes'|'no'): [(price_of_side_c, size)] ascending."""
        src = self.no_bids if side == "yes" else self.yes_bids
        return sorted(((100 - p, s) for p, s in src.items()))

    def bid_ladder(self, side):
        """Ladder to SELL `side`: [(price_of_side_c, size)] descending."""
        src = self.yes_bids if side == "yes" else self.no_bids
        return sorted(src.items(), reverse=True)

    def snapshot_dict(self, depth=8):
        return {"yes_bids": sorted(self.yes_bids.items(), reverse=True)[:depth],
                "no_bids": sorted(self.no_bids.items(), reverse=True)[:depth],
                "seq": self.last_seq, "ok": self.ok}
