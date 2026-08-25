"""Paper executor: fills simulated IOC orders against the LIVE recorded book.

This is the module that answers the question backtests cannot: what depth
actually rests at the moment our order would arrive. Fees use the verified
official formula. No real orders are ever sent."""
import math
import time

from . import config, store


def fee_dollars(contracts, price_c):
    p = price_c / 100.0
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


class Position:
    def __init__(self, tid, signal_id, market, event, series, d, side, entry_px, size, ref, ext):
        self.tid = tid
        self.signal_id = signal_id
        self.market = market
        self.event = event
        self.series = series
        self.dir = d
        self.side = side              # 'yes' or 'no' (what we bought)
        self.entry_px = entry_px      # in side-space cents
        self.size = size
        self.ref = ref                # signal reference (yes-space)
        self.ext = ext
        self.entry_ts = time.time()
        self.mae = 0.0                # max adverse excursion (side-space cents)
        self.best_bid = entry_px
        self.shadow_stop_hit_px = None

    def unrealized(self, bid):
        return (bid - self.entry_px) * self.size / 100.0


class PaperDesk:
    def __init__(self, broadcast):
        self.positions = {}           # tid -> Position
        self.broadcast = broadcast
        self.kill = False

    def try_enter(self, signal_id, sig, meta, book):
        """IOC against the current book ladder, hard price cap. Returns outcome str."""
        if self.kill:
            return "killed"
        if book is None or not book.ok:
            return "no_book"
        side = "yes" if sig["dir"] > 0 else "no"
        ladder = book.ask_ladder(side)
        want_usd = config.NOTIONAL_USD
        size, cost, vwap_num = 0.0, 0.0, 0.0
        for px, avail in ladder:
            if px > config.PRICE_CAP:
                break
            take = min(avail, (want_usd - cost) / (px / 100.0))
            if take <= 0:
                break
            size += take
            cost += take * px / 100.0
            vwap_num += take * px
            if cost >= want_usd - 0.01:
                break
        if size < 1:
            return "rejected_cap"
        entry_px = vwap_num / size
        tid = store.insert_trade({
            "signal_id": signal_id, "market": sig["ticker"], "event": meta["event"],
            "series": meta["series"], "dir": sig["dir"], "side": side,
            "entry_ts": time.time(), "entry_px": round(entry_px, 2), "size": round(size, 1),
            "cap": config.PRICE_CAP, "notional": want_usd,
            "book_at_entry": book.snapshot_dict(),
        })
        pos = Position(tid, signal_id, sig["ticker"], meta["event"], meta["series"],
                       sig["dir"], side, entry_px, size, sig["ref"], sig["ext"])
        self.positions[tid] = pos
        self.broadcast({"type": "trade_open", "trade": self.pos_dict(pos)})
        store.log_event("trade", f"OPEN {side.upper()} {sig['ticker']} {size:.0f}@{entry_px:.1f}")
        return "filled"

    def pos_dict(self, pos, bid=None):
        return {"id": pos.tid, "market": pos.market, "event": pos.event, "series": pos.series,
                "side": pos.side, "dir": pos.dir, "entry_px": round(pos.entry_px, 2),
                "size": round(pos.size, 1), "entry_ts": pos.entry_ts,
                "bid": bid, "upnl": round(pos.unrealized(bid), 2) if bid is not None else None}

    def on_book(self, ticker, book):
        """Mark open positions on this market; handle target exits + shadow metrics."""
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            bid = book.best_yes_bid() if pos.side == "yes" else book.best_no_bid()
            if bid is None:
                continue
            pos.best_bid = bid
            adverse = pos.entry_px - bid
            if adverse > pos.mae:
                pos.mae = adverse
            # shadow stop (recorded, not acted on unless USE_STOP)
            ext_side = pos.ext if pos.side == "yes" else 100 - pos.ext
            ref_side = pos.ref if pos.side == "yes" else 100 - pos.ref
            stop_lvl = ext_side - config.STOP_FRAC * (ext_side - ref_side)
            if bid <= stop_lvl and pos.shadow_stop_hit_px is None:
                pos.shadow_stop_hit_px = bid
                if config.USE_STOP:
                    self.close(pos, bid, "stop")
                    continue
            if bid >= config.TARGET:
                self.close(pos, config.TARGET, "target")

    def check_timeouts(self):
        now = time.time()
        for pos in list(self.positions.values()):
            if now - pos.entry_ts > config.TIMEOUT_S:
                self.close(pos, pos.best_bid, "timeout")

    def settle_market(self, ticker, result):
        """result: 'yes'|'no' in market (YES) space."""
        for pos in [p for p in self.positions.values() if p.market == ticker]:
            won = (result == "yes") == (pos.side == "yes")
            self.close(pos, 100.0 if won else 0.0, "settle")

    def close(self, pos, exit_px, reason):
        exit_px = min(max(exit_px, 0.0), 100.0)
        gross = (exit_px - pos.entry_px) * pos.size / 100.0
        fees = fee_dollars(pos.size, pos.entry_px)
        if reason != "settle" and config.FEE_EXIT_TAKER:
            fees += fee_dollars(pos.size, exit_px)
        net = gross - fees
        store.close_trade(pos.tid, round(exit_px, 2), reason, round(gross, 2),
                          round(fees, 2), round(net, 2), round(pos.mae, 2),
                          pos.shadow_stop_hit_px)
        self.positions.pop(pos.tid, None)
        self.broadcast({"type": "trade_close", "trade": {
            "id": pos.tid, "market": pos.market, "series": pos.series, "side": pos.side,
            "entry_px": round(pos.entry_px, 2), "exit_px": round(exit_px, 2),
            "size": round(pos.size, 1), "reason": reason, "net": round(net, 2)}})
        store.log_event("trade", f"CLOSE {pos.market} {reason} net ${net:+.2f}")

    def flatten_all(self, reason="flatten"):
        for pos in list(self.positions.values()):
            self.close(pos, pos.best_bid, reason)
