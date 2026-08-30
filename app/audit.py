"""Presentation-safe audit joins for market triggers and match-feed observations.

The matcher is deliberately descriptive: it finds the nearest observation for
the same stored event identifier inside a fixed window.  It never feeds the
strategy and never upgrades temporal proximity into a causal claim.
"""
import json

from . import config
from .match_events import event_consistency, normalize_match_event


def json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalized_event(row):
    stored = json_object(row.get("normalized_event"))
    if stored:
        return stored
    detail = json_object(row.get("detail"))
    return normalize_match_event(
        row.get("change_kind"),
        json_object(row.get("score_before")),
        json_object(row.get("score_after")),
        detail.get("live_data") or {},
    )


def signal_inferred_state(signal):
    detail = json_object(signal.get("detail"))
    sleeve = json_object(detail.get("sleeve"))
    return sleeve.get("inferred_state") or detail.get("inferred_state")


def signal_strategy(signal):
    detail = json_object(signal.get("detail"))
    sleeve = json_object(detail.get("sleeve"))
    raw = signal.get("strategy") or detail.get("strategy") or sleeve.get("strategy")
    if raw == "price_only_late_score_v1":
        return "price_only_late_score"
    return raw or "detector"


def build_trigger(signal):
    """Return the observed market trigger and frozen thresholds with units."""
    detail = json_object(signal.get("detail"))
    sleeve = json_object(detail.get("sleeve"))
    strategy = signal_strategy(signal)
    return {
        "strategy": strategy,
        "outcome": signal.get("outcome"),
        "observed": {
            "log_odds_displacement": signal.get("dl"),
            "distinct_price_levels": signal.get("levels"),
            "contracts": signal.get("size"),
            "sibling_confirmation_lag_ms": signal.get("conf_lag_ms"),
            "reference_price_c": signal.get("ref"),
            "extreme_price_c": signal.get("ext"),
        },
        "thresholds": {
            "min_log_odds_displacement": config.DL_MIN,
            "min_distinct_price_levels": config.LEVELS_MIN,
            "min_contracts": config.SIZE_MIN,
            "sibling_confirmation_window_ms": config.CONF_MS,
            "price_cap_c": config.PRICE_CAP,
        },
        "price_only_inference": sleeve or None,
    }


def _timing_relation(delta_ms):
    if delta_ms > 0:
        return "market_signal_first"
    if delta_ms < 0:
        return "match_feed_first"
    return "same_recorded_time"


def match_signal_event(signal, observations, window_s=None):
    """Find the nearest same-event feed observation around local signal receipt."""
    window_s = config.EVENT_MATCH_WINDOW_S if window_s is None else float(window_s)
    signal_ts = signal.get("local_ts")
    base = {
        "match_status": "no_nearby_same_match_event",
        "causality": "not_established",
        "interpretation": "Nearest provider observation only; causation is not established.",
        "window_s": window_s,
        "signal_received_ts": signal_ts,
        "event_observed_ts": None,
        "event_minus_signal_ms": None,
        "timing_relation": None,
        "state_consistency": None,
        "observation_id": None,
        "canonical_event": None,
        "provider_poll_uncertainty_ms": None,
        "provider_response_ms": None,
        "raw_provider_payload": None,
    }
    if not isinstance(signal_ts, (int, float)):
        base["match_status"] = "signal_time_missing"
        return base
    event = signal.get("event")
    eligible = []
    for row in observations:
        observed_ts = row.get("observed_ts")
        if row.get("event") != event or not isinstance(observed_ts, (int, float)):
            continue
        delta = observed_ts - signal_ts
        if abs(delta) <= window_s:
            eligible.append((abs(delta), observed_ts, row.get("id") or 0, delta, row))
    if not eligible:
        return base
    _distance, observed_ts, _row_id, delta, row = min(
        eligible, key=lambda item: (item[0], item[1], item[2]),
    )
    normalized = normalized_event(row)
    detail = json_object(row.get("detail"))
    delta_ms = round(delta * 1000.0, 3)
    base.update({
        "match_status": "nearest_same_match_event",
        "event_observed_ts": observed_ts,
        "event_minus_signal_ms": delta_ms,
        "timing_relation": _timing_relation(delta_ms),
        "state_consistency": event_consistency(signal_inferred_state(signal), normalized),
        "observation_id": row.get("id"),
        "canonical_event": normalized,
        "provider_poll_uncertainty_ms": detail.get("poll_uncertainty_ms"),
        "provider_response_ms": row.get("response_ms"),
        "raw_provider_payload": detail.get("live_data"),
    })
    return base


def timing_fields(signal, trade=None):
    detail = json_object(signal.get("detail"))
    trade = trade or {}
    exit_ts = trade.get("exit_ts")
    settled = trade.get("exit_reason") == "settle"
    exchange_signal_ts = (
        signal.get("ts_ms") / 1000.0
        if isinstance(signal.get("ts_ms"), (int, float)) else None
    )
    order_delay = detail.get("order_arrival_ms")
    order_arrival_ts = (
        exchange_signal_ts + order_delay / 1000.0
        if exchange_signal_ts is not None and isinstance(order_delay, (int, float)) else
        trade.get("entry_ts")
    )
    return {
        "exchange_signal_ts": exchange_signal_ts,
        "signal_received_ts": signal.get("local_ts"),
        "paper_order_arrival_ts": order_arrival_ts,
        "paper_order_arrival_delay_ms": order_delay,
        "paper_execution_latency_ms": detail.get("paper_latency_ms"),
        "entry_ts": trade.get("entry_ts"),
        "exit_ts": None if settled else exit_ts,
        "settlement_ts": exit_ts if settled else None,
    }
