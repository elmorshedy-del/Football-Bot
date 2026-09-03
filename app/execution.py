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

    def reset(self, book, preserve_depletion=False):
        if preserve_depletion:
            yes_depletion = self._depletion(self._observed_yes, self.yes_bids)
            no_depletion = self._depletion(self._observed_no, self.no_bids)
            self.yes_bids = self._subtract_depletion(book.yes_bids, yes_depletion)
            self.no_bids = self._subtract_depletion(book.no_bids, no_depletion)
        else:
            self.yes_bids = dict(book.yes_bids)
            self.no_bids = dict(book.no_bids)
        self._observed_yes = dict(book.yes_bids)
        self._observed_no = dict(book.no_bids)
        self.ok = bool(book.ok)
        self.seq = book.last_seq

    @staticmethod
    def _depletion(observed, shadow):
        return {
            price: max(0.0, size - shadow.get(price, 0.0))
            for price, size in observed.items()
            if size - shadow.get(price, 0.0) > _EPS
        }

    @staticmethod
    def _subtract_depletion(observed, depletion):
        return {
            price: available
            for price, size in observed.items()
            if (available := size - depletion.get(price, 0.0)) > _EPS
        }

    def invalidate(self):
        self.ok = False

    def apply_delta(self, msg, seq=None):
        if not self.ok:
            return
        px = float(msg["price_dollars"]) * 100
        delta = float(msg["delta_fp"])
        if msg.get("side") == "yes":
            shadow, observed = self.yes_bids, self._observed_yes
        else:
            shadow, observed = self.no_bids, self._observed_no
        self._apply_size_delta(observed, px, delta)
        self._apply_size_delta(shadow, px, delta)
        self.seq = seq if seq is not None else self.seq

    @staticmethod
    def _apply_size_delta(side, price, delta):
        new_size = side.get(price, 0.0) + delta
        if new_size <= _EPS:
            side.pop(price, None)
        else:
            side[price] = new_size

    def buy(self, side, notional_usd, price_cap, consume=True):
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
            if consume:
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

    def consume_buy(self, side, fill):
        source = self.no_bids if side == "yes" else self.yes_bids
        for price, quantity in fill.levels:
            self._consume(source, 100 - price, quantity)

    def sell(self, side, quantity, price_floor=0.0, consume=True):
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
            if consume:
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

    def consume_sell(self, side, fill):
        source = self.yes_bids if side == "yes" else self.no_bids
        for price, quantity in fill.levels:
            self._consume(source, price, quantity)

    @staticmethod
    def _consume(source, price, quantity):
        remaining = source.get(price, 0.0) - quantity
        if remaining <= _EPS:
            source.pop(price, None)
        else:
            source[price] = remaining

    # Default persisted depth.  Callers recording evidence for a specific fill
    # must pass a depth that covers that fill's walk (see PaperDesk entry).
    SNAPSHOT_DEPTH = 8

    def snapshot_dict(self, depth=None):
        depth = self.SNAPSHOT_DEPTH if depth is None else depth
        yes_bids = sorted(self.yes_bids.items(), reverse=True)
        no_bids = sorted(self.no_bids.items(), reverse=True)
        return {
            "yes_bids": yes_bids[:depth],
            "no_bids": no_bids[:depth],
            "seq": self.seq,
            "ok": self.ok,
            "shadow": True,
            # Records whether anything was cut, so a later integrity check can
            # tell "evidence does not cover this" from "the fill was wrong".
            "depth": depth,
            "truncated": len(yes_bids) > depth or len(no_bids) > depth,
        }


class ShadowBooks:
    def __init__(self):
        self._books = {}

    def ensure(self, ticker, live_book):
        shadow = self._books.get(ticker)
        if shadow is None:
            shadow = ShadowBook.from_live(live_book)
            self._books[ticker] = shadow
        return shadow

    def reset(self, ticker, live_book):
        shadow = self._books.get(ticker)
        if shadow is None:
            self._books[ticker] = ShadowBook.from_live(live_book)
        else:
            shadow.reset(live_book, preserve_depletion=True)

    def apply_delta(self, ticker, msg, seq=None):
        shadow = self._books.get(ticker)
        if shadow is not None:
            shadow.apply_delta(msg, seq)

    def invalidate(self, tickers):
        for ticker in tickers:
            shadow = self._books.get(ticker)
            if shadow is not None:
                shadow.invalidate()
