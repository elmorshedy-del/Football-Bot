"""In-memory order books built from orderbook_snapshot + orderbook_delta.

Kalshi book semantics: `yes` side = resting YES bids, `no` side = resting NO
bids. A NO bid at price q is an offer to sell YES at (100 - q). All prices
kept internally in cents (float, supports sub-cent)."""


class Book:
    __slots__ = ("yes_bids", "no_bids", "last_seq", "ok", "ts_ms")

    def __init__(self):
        self.yes_bids = {}   # price_c -> size
        self.no_bids = {}
        self.last_seq = None
        self.ok = False
        self.ts_ms = 0

    def apply_snapshot(self, msg, seq):
        self.yes_bids = {float(p) * 100: float(s) for p, s in (msg.get("yes_dollars_fp") or [])}
        self.no_bids = {float(p) * 100: float(s) for p, s in (msg.get("no_dollars_fp") or [])}
        self.last_seq = seq
        self.ok = True

    def apply_delta(self, msg, seq, sequence_validated=False):
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
        return True

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
