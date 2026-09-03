"""Price-only late-score sleeve.

The sleeve never consumes a score or event feed.  It treats the three mutually
exclusive 1X2 contracts as a small state vector and asks whether a confirmed
Gate-A sweep looks like a coherent reallocation of probability toward either:

* a team-win leg (latent one-goal lead), or
* the draw leg (latent equalizer).

Those labels are inferences, not observed scores.  A goal, VAR reversal and
other news can be observationally identical at entry time; the exit policy
therefore reacts to executable-price reversion instead of pretending to know
the cause.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
import re

from . import config


DRAW_WORDS = re.compile(r"(?:^|[-\s—])(tie|draw)(?:$|[-\s—])", re.IGNORECASE)


def leg_role(ticker, title=""):
    """Return ``draw`` or ``team`` using market identity, never match data."""
    suffix = ticker.rsplit("-", 1)[-1].upper()
    if suffix in {"TIE", "DRAW", "X"} or DRAW_WORDS.search(title or ""):
        return "draw"
    return "team"


def normalized_triplet(mids):
    """Normalize 1X2 YES midpoints into a probability simplex."""
    if len(mids) != 3 or any(value is None or value <= 0 for value in mids.values()):
        return None
    total = sum(mids.values())
    if total <= 0:
        return None
    return {ticker: value / total for ticker, value in mids.items()}


@dataclass(frozen=True)
class SleeveDecision:
    accepted: bool
    reason: str
    detail: dict


class PriceOnlyLateScoreSleeve:
    """Track triplet snapshots and classify confirmed sweep candidates."""

    def __init__(self):
        self.history = defaultdict(lambda: deque(maxlen=1200))
        self.last_leg_observation = defaultdict(dict)

    @staticmethod
    def _snapshot(tickers, meta, books, observed_ms, evidence=None):
        """Build the normalized triplet, or explain why it could not be built.

        `evidence` collects the measurements behind a refusal.  Returning bare
        reasons made a rejection unre-decidable: a `wide_spread` row recorded
        that the book was too wide but never how wide, so no later analysis
        could ask what a different cap would have admitted.  Every refusal now
        carries the numbers that produced it.
        """
        evidence = {} if evidence is None else evidence
        evidence["leg_count"] = len(tickers)
        if len(tickers) != 3:
            return None, "not_triplet"
        mids, spreads, roles = {}, {}, {}
        evidence["spread_c"] = spreads
        evidence["mid_c"] = mids
        for ticker in tickers:
            book = books.get(ticker)
            if book is None or not book.ok:
                evidence["missing_leg"] = ticker
                evidence["book_ok"] = None if book is None else bool(book.ok)
                return None, "incomplete_book"
            bid, ask = book.best_yes_bid(), book.best_yes_ask()
            if bid is None or ask is None or ask < bid:
                evidence["missing_leg"] = ticker
                evidence["best_bid"], evidence["best_ask"] = bid, ask
                return None, "incomplete_book"
            spreads[ticker] = ask - bid
            mids[ticker] = (bid + ask) / 2.0
            roles[ticker] = leg_role(ticker, meta.get(ticker, {}).get("title", ""))
        # Widths are measured for every leg before any is judged, so the row
        # shows the whole triplet rather than stopping at the first offender.
        widest = max(spreads, key=spreads.get)
        evidence["widest_leg"] = widest
        evidence["widest_spread_c"] = round(spreads[widest], 3)
        evidence["max_spread_c_limit"] = config.SLEEVE_MAX_SPREAD_C
        if spreads[widest] > config.SLEEVE_MAX_SPREAD_C:
            return None, "wide_spread"
        evidence["roles"] = dict(roles)
        if list(roles.values()).count("draw") != 1:
            return None, "ambiguous_draw_leg"
        probs = normalized_triplet(mids)
        if probs is None:
            return None, "invalid_triplet"
        return {
            "ts_ms": float(observed_ms),
            "mid": mids,
            "q": probs,
            "spread": spreads,
            "role": roles,
        }, None

    def observe(self, event, tickers, meta, books, observed_ms, changed_ticker=None):
        if changed_ticker is None:
            self.last_leg_observation[event].update(
                {ticker: float(observed_ms) for ticker in tickers}
            )
        else:
            self.last_leg_observation[event][changed_ticker] = float(observed_ms)
        snapshot, error = self._snapshot(tickers, meta, books, observed_ms)
        if snapshot is None:
            return error
        rows = self.history[event]
        # Coalesce same-clock observations while retaining every changed state.
        if rows and rows[-1]["ts_ms"] == snapshot["ts_ms"]:
            rows[-1] = snapshot
        elif not rows or rows[-1]["mid"] != snapshot["mid"]:
            rows.append(snapshot)
        cutoff = float(observed_ms) - max(config.SLEEVE_BASELINE_MS * 4.0, 20_000.0)
        while rows and rows[0]["ts_ms"] < cutoff:
            rows.popleft()
        return None

    def classify(self, candidate, event, tickers, meta, books, observed_ms):
        ticker = candidate["ticker"]
        detail = {"strategy": "price_only_late_score_v1", "feed_independent": True}
        if candidate.get("dir") != 1:
            return SleeveDecision(False, "not_rising_leg", detail)

        snapshot, error = self._snapshot(
            tickers, meta, books, observed_ms, evidence=detail,
        )
        if snapshot is None:
            return SleeveDecision(False, error, detail)
        if ticker not in snapshot["q"]:
            return SleeveDecision(False, "target_not_in_triplet", detail)

        leg_ages = {
            leg: float(observed_ms) - self.last_leg_observation[event].get(leg, -1e18)
            for leg in tickers
        }
        detail["triplet_age_ms"] = {
            leg: round(age, 1) if age < 1e12 else None for leg, age in leg_ages.items()
        }
        if any(age > config.SLEEVE_TRIPLET_FRESH_MS for age in leg_ages.values()):
            return SleeveDecision(False, "stale_triplet_leg", detail)

        target_ms = float(observed_ms) - config.SLEEVE_BASELINE_MS
        rows = list(self.history.get(event, ()))
        eligible = [row for row in rows if row["ts_ms"] <= target_ms]
        # Recorded before the refusals below, so a baseline rejection shows how
        # much history existed and how old the best candidate was.  Without it
        # neither the baseline lag nor its max age could be re-fitted.
        detail["baseline_rows"] = len(rows)
        detail["baseline_eligible"] = len(eligible)
        detail["baseline_lag_ms"] = config.SLEEVE_BASELINE_MS
        detail["max_baseline_age_ms"] = config.SLEEVE_MAX_BASELINE_AGE_MS
        detail["oldest_row_age_ms"] = (
            round(float(observed_ms) - rows[0]["ts_ms"], 1) if rows else None
        )
        if not eligible:
            return SleeveDecision(False, "no_baseline", detail)
        baseline = eligible[-1]
        baseline_age_ms = float(observed_ms) - baseline["ts_ms"]
        detail["baseline_age_ms"] = round(baseline_age_ms, 1)
        if baseline_age_ms > config.SLEEVE_MAX_BASELINE_AGE_MS:
            return SleeveDecision(False, "stale_baseline", detail)

        deltas = {leg: snapshot["q"][leg] - baseline["q"][leg] for leg in tickers}
        target_gain = deltas[ticker]
        sibling_deltas = [delta for leg, delta in deltas.items() if leg != ticker]
        negative_outflow = -sum(min(delta, 0.0) for delta in sibling_deltas)
        positive_sibling_flow = sum(max(delta, 0.0) for delta in sibling_deltas)
        gross_sibling_flow = negative_outflow + positive_sibling_flow
        explained = negative_outflow / gross_sibling_flow if gross_sibling_flow > 0 else 0.0
        role = snapshot["role"][ticker]
        min_gain = (config.SLEEVE_MIN_DRAW_GAIN_PP if role == "draw"
                    else config.SLEEVE_MIN_TEAM_GAIN_PP)
        min_post = (config.SLEEVE_MIN_DRAW_POST if role == "draw"
                    else config.SLEEVE_MIN_TEAM_POST)
        detail.update({
            "inferred_state": "equal_score_0" if role == "draw" else "one_goal_lead_+1",
            "leg_role": role,
            "baseline_age_ms": round(baseline_age_ms, 1),
            "baseline_q": {leg: round(value, 6) for leg, value in baseline["q"].items()},
            "current_q": {leg: round(value, 6) for leg, value in snapshot["q"].items()},
            "delta_q": {leg: round(value, 6) for leg, value in deltas.items()},
            "target_gain_pp": round(target_gain, 6),
            "sibling_explanation": round(explained, 6),
            "target_spread_c": round(snapshot["spread"][ticker], 3),
        })

        if target_gain < min_gain:
            return SleeveDecision(False, "insufficient_triplet_shift", detail)
        if snapshot["q"][ticker] < min_post:
            return SleeveDecision(False, "weak_post_state", detail)
        if max(sibling_deltas) > config.SLEEVE_MAX_SIBLING_RISE_PP:
            return SleeveDecision(False, "incoherent_sibling_rise", detail)
        if explained < config.SLEEVE_MIN_EXPLAINED:
            return SleeveDecision(False, "weak_triplet_coherence", detail)
        return SleeveDecision(True, "accepted", detail)


def fee_aware_scratch_price(position, bid, fee_fn):
    """Executable cents needed to cover estimated round-trip taker fees."""
    if position.size <= 0:
        return position.entry_px
    entry_fee_c = position.entry_fees / position.size * 100.0
    exit_fee_c = 0.0
    if config.FEE_EXIT_TAKER:
        try:
            exit_fee_c = fee_fn(
                position.remaining,
                bid,
                position.fee_type,
                position.fee_multiplier,
            ) / position.remaining * 100.0
        except (ValueError, ZeroDivisionError):
            exit_fee_c = config.SLEEVE_UNKNOWN_FEE_BUFFER_C
    return position.entry_px + entry_fee_c + exit_fee_c + config.SLEEVE_SCRATCH_BUFFER_C


def sleeve_exit_reason(position, bid, now, fee_fn):
    """Return a dynamic price-only exit reason, or ``None`` to keep holding."""
    if not position.sleeve:
        return None
    position.bid_path.append((now, bid))
    position.peak_bid = max(position.peak_bid, bid)
    if position.sleeve_anchor_bid is None:
        position.sleeve_anchor_bid = bid
    mfe = position.peak_bid - position.entry_px
    scratch = fee_aware_scratch_price(position, bid, fee_fn)

    # A full collapse is the only price-only evidence of a failed/overturned
    # event.  Exit immediately; a no-loss promise is impossible here.
    if bid <= position.sleeve_anchor_bid - config.SLEEVE_REVERSAL_C:
        return "sleeve_reversal"

    if mfe >= config.SLEEVE_SCRATCH_ARM_C and bid <= scratch:
        return "sleeve_scratch"

    if mfe >= config.SLEEVE_TRAIL_ARM_C:
        drawdown = position.peak_bid - bid
        trail = max(config.SLEEVE_TRAIL_MIN_C, config.SLEEVE_TRAIL_FRAC * mfe)
        if drawdown >= trail and bid >= scratch:
            return "sleeve_profit_lock"

    recent = [(ts, px) for ts, px in position.bid_path
              if ts >= now - config.SLEEVE_OSCILLATION_WINDOW_S]
    if mfe >= config.SLEEVE_SCRATCH_ARM_C and len(recent) >= 4:
        signs = [1 if px >= scratch else -1 for _ts, px in recent]
        crossings = sum(1 for left, right in zip(signs, signs[1:]) if left != right)
        path = sum(abs(right[1] - left[1]) for left, right in zip(recent, recent[1:]))
        displacement = abs(recent[-1][1] - recent[0][1])
        efficiency = displacement / path if path > 0 else 1.0
        if crossings >= config.SLEEVE_OSCILLATION_CROSSES and \
                efficiency <= config.SLEEVE_MAX_OSCILLATION_EFFICIENCY:
            return "sleeve_oscillation"
    return None
