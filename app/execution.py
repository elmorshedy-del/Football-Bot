"""Counterfactual paper order book and deterministic depth walking.

The live :class:`app.books.Book` remains an untouched view of Kalshi.  This
module keeps a separate shadow copy so simulated IOC orders cannot reuse the
same displayed liquidity while later exchange deltas continue to advance the
counterfactual book.
"""
from dataclasses import dataclass, field


_EPS = 1e-9


@dataclass
class DepthFill:
    quantity: float = 0.0
    notional: float = 0.0
    vwap: float | None = None
    complete: bool = False
    levels: list[tuple[float, float]] = field(default_factory=list)


class ShadowBook:
    def __init__(self):
        self.yes_bids = {}
        self.no_bids = {}
        self._observed_yes = {}
        self._observed_no = {}
        self.ok = False
        self.seq = None

    @classmethod
    def from_live(cls, book):
        shadow = cls()
        shadow.reset(book)
        return shadow

    def reset(self, book):
        self.yes_bids = dict(book.yes_bids)
        self.no_bids = dict(book.no_bids)
        self._observed_yes = dict(book.yes_bids)
        self._observed_no = dict(book.no_bids)
        self.ok = bool(book.ok)
        self.seq = book.last_seq

    def reconcile(self, book):
        """Apply changes in the live book without restoring simulated fills."""
        self._reconcile_side(self.yes_bids, self._observed_yes, book.yes_bids)
        self._reconcile_side(self.no_bids, self._observed_no, book.no_bids)
        self._observed_yes = dict(book.yes_bids)
        self._observed_no = dict(book.no_bids)
        self.ok = bool(book.ok)
        self.seq = book.last_seq

    @staticmethod
    def _reconcile_side(shadow, observed, current):
        for price in set(observed) | set(current):
            delta = current.get(price, 0.0) - observed.get(price, 0.0)
            new_size = shadow.get(price, 0.0) + delta
            if new_size <= _EPS:
                shadow.pop(price, None)
            else:
                shadow[price] = new_size

    def invalidate(self):
        self.ok = False

    def apply_delta(self, msg, seq=None):
        if not self.ok:
            return
        px = float(msg["price_dollars"]) * 100
        delta = float(msg["delta_fp"])
        side = self.yes_bids if msg.get("side") == "yes" else self.no_bids
        new_size = side.get(px, 0.0) + delta
        if new_size <= _EPS:
            side.pop(px, None)
        else:
            side[px] = new_size
        self.seq = seq if seq is not None else self.seq

    def buy(self, side, notional_usd, price_cap):
        """Buy YES/NO by walking asks, consuming every filled shadow level."""
        source = self.no_bids if side == "yes" else self.yes_bids
        ladder = sorted((100 - price, size, price) for price, size in source.items())
        quantity = cost = weighted = 0.0
        levels = []
        for price, available, source_price in ladder:
            if price > price_cap or price <= 0:
                break
            take = min(available, (notional_usd - cost) / (price / 100.0))
            if take <= _EPS:
                break
            quantity += take
            cost += take * price / 100.0
            weighted += take * price
            levels.append((price, take))
            self._consume(source, source_price, take)
            if cost >= notional_usd - 0.01:
                break
        return DepthFill(
            quantity=quantity,
            notional=cost,
            vwap=weighted / quantity if quantity > _EPS else None,
            complete=cost >= notional_usd - 0.01,
            levels=levels,
        )

    def sell(self, side, quantity, price_floor=0.0):
        """Sell YES/NO by walking bids, preserving an unfilled remainder."""
        source = self.yes_bids if side == "yes" else self.no_bids
        ladder = sorted(source.items(), reverse=True)
        filled = proceeds = weighted = 0.0
        levels = []
        for price, available in ladder:
            if price < price_floor:
                break
            take = min(available, quantity - filled)
            if take <= _EPS:
                break
            filled += take
            proceeds += take * price / 100.0
            weighted += take * price
            levels.append((price, take))
            self._consume(source, price, take)
            if filled >= quantity - _EPS:
                break
        return DepthFill(
            quantity=filled,
            notional=proceeds,
            vwap=weighted / filled if filled > _EPS else None,
            complete=filled >= quantity - _EPS,
            levels=levels,
        )

    @staticmethod
    def _consume(source, price, quantity):
        remaining = source.get(price, 0.0) - quantity
        if remaining <= _EPS:
            source.pop(price, None)
        else:
            source[price] = remaining

    def snapshot_dict(self, depth=8):
        return {
            "yes_bids": sorted(self.yes_bids.items(), reverse=True)[:depth],
            "no_bids": sorted(self.no_bids.items(), reverse=True)[:depth],
            "seq": self.seq,
            "ok": self.ok,
            "shadow": True,
        }


class ShadowBooks:
    def __init__(self):
        self._books = {}

    def ensure(self, ticker, live_book):
        shadow = self._books.get(ticker)
        if shadow is None:
            shadow = ShadowBook.from_live(live_book)
            self._books[ticker] = shadow
        else:
            shadow.reconcile(live_book)
        return shadow

    def reset(self, ticker, live_book):
        self._books[ticker] = ShadowBook.from_live(live_book)

    def apply_delta(self, ticker, msg, seq=None):
        shadow = self._books.get(ticker)
        if shadow is not None:
            shadow.apply_delta(msg, seq)

    def invalidate(self, tickers):
        for ticker in tickers:
            shadow = self._books.get(ticker)
            if shadow is not None:
                shadow.invalidate()
