"""Presentation-safe audit joins for market triggers and match-feed observations.

The matcher is deliberately descriptive: it finds the nearest observation for
the same stored event identifier inside a fixed window.  It never feeds the
strategy and never upgrades temporal proximity into a causal claim.
"""
import json
from datetime import datetime, timezone

from . import config
from .match_clock import parse_stored_stamp
from .match_events import (
    SUBSTANTIVE_EVENT_TYPES,
    association_class,
    event_consistency,
    normalize_match_event,
)


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
    if stored.get("schema") == "football.provider_match_event.v1":
        return stored
    detail = json_object(row.get("detail"))
    derived = normalize_match_event(
        row.get("change_kind"),
        json_object(row.get("score_before")),
        json_object(row.get("score_after")),
        detail.get("live_data") or {},
    )
    if not stored:
        return derived
    # Re-derive additive presentation fields from the preserved raw payload so
    # historical observations benefit from deterministic parser improvements.
    return {**stored, **{
        key: value for key, value in derived.items()
        if value is not None or stored.get(key) is None
    }}


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


def schedule_window(signal):
    """Expose the frozen expiration-time proxy used by the price-only sleeve."""
    raw = signal.get("expected_expiration_time") or signal.get("close_time")
    signal_ts = signal.get("local_ts")
    expiration_ts = None
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expiration_ts = parsed.timestamp()
        except ValueError:
            pass
    seconds_to_expiration = (
        expiration_ts - signal_ts
        if expiration_ts is not None and isinstance(signal_ts, (int, float)) else None
    )
    inside = (
        -config.SLEEVE_AFTER_EXPIRY_MIN * 60.0 <= seconds_to_expiration <=
        config.SLEEVE_START_BEFORE_EXPIRY_MIN * 60.0
        if seconds_to_expiration is not None else None
    )
    return {
        "source": "market_expected_expiration",
        "assumption": "Schedule proxy only; not a verified live match clock.",
        "expected_expiration_time": raw,
        "expected_expiration_ts": expiration_ts,
        "signal_ts": signal_ts,
        "seconds_to_expected_expiration": (
            round(seconds_to_expiration, 3) if seconds_to_expiration is not None else None
        ),
        "window_start_before_expiration_min": config.SLEEVE_START_BEFORE_EXPIRY_MIN,
        "window_end_after_expiration_min": config.SLEEVE_AFTER_EXPIRY_MIN,
        "inside_configured_window": inside,
    }


def _timing_relation(delta_ms):
    if delta_ms > 0:
        return "market_signal_first"
    if delta_ms < 0:
        return "match_feed_first"
    return "same_recorded_time"


def _association_label(consistency):
    return {
        "equalizer_consistent": "state_consistent",
        "one_goal_lead_consistent": "state_consistent",
        "goal_consistent_state_unknown": "nearby_goal",
        "correction_or_reversal": "nearby_correction",
        "state_mismatch": "state_mismatch",
        "time_match_only": "time_only",
    }.get(consistency, "unmatched")


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
        "provider_occurrence_ts": None,
        "occurrence_minus_signal_ms": None,
        "association": "unmatched",
        "event_association": "no_nearby_same_match_event",
        "raw_provider_payload": None,
        "match_clock": parse_stored_stamp(signal.get("match_clock_snapshot")),
    }
    if not isinstance(signal_ts, (int, float)):
        base["match_status"] = "signal_time_missing"
        return base
    event = signal.get("event")
    eligible = []
    for row in observations:
        observed_ts = row.get("first_observed_ts", row.get("observed_ts"))
        if row.get("event") != event or not isinstance(observed_ts, (int, float)):
            continue
        delta = observed_ts - signal_ts
        if abs(delta) <= window_s:
            canonical = row.get("canonical_type") or ""
            if not canonical and isinstance(row.get("normalized_event"), dict):
                canonical = row["normalized_event"].get("canonical_type") or ""
            priority = 0 if (
                canonical in SUBSTANTIVE_EVENT_TYPES or
                str(canonical).startswith("goal_observed") or
                str(canonical).startswith("score_correction")
            ) else 1
            eligible.append((priority, abs(delta), observed_ts, row.get("id") or 0, delta, row))
    if not eligible:
        return base
    _priority, _distance, observed_ts, _row_id, delta, row = min(
        eligible, key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    normalized = normalized_event(row)
    # goal_latency_observations carry `detail`; provider_match_events carry
    # `raw_payload`.  Reading only `detail` left every canonical provider
    # association with an empty payload, no occurrence time, and no poll
    # uncertainty.
    detail = json_object(row.get("detail"))
    raw_provider = detail.get("live_data") or json_object(row.get("raw_payload")) or {}
    provider_details = raw_provider.get("details") or {}
    last_play = provider_details.get("last_play") or {}
    occurrence_ts = last_play.get("occurence_ts") if isinstance(last_play, dict) else None
    occurrence_delta_ms = (
        round((occurrence_ts - signal_ts) * 1000.0, 3)
        if isinstance(occurrence_ts, (int, float)) else None
    )
    delta_ms = round(delta * 1000.0, 3)
    consistency = event_consistency(signal_inferred_state(signal), normalized)
    base.update({
        "match_status": "nearest_same_match_event",
        "event_observed_ts": observed_ts,
        "event_minus_signal_ms": delta_ms,
        "timing_relation": _timing_relation(delta_ms),
        "state_consistency": consistency,
        "association": _association_label(consistency),
        "event_association": association_class(consistency, True),
        "observation_id": row.get("id"),
        "canonical_event": normalized,
        # goal_latency rows carry a precomputed value in `detail`; provider rows
        # carry the raw previous_poll_ts, so derive it rather than reporting
        # empty polling uncertainty for every canonical provider association.
        "provider_poll_uncertainty_ms": (
            detail.get("poll_uncertainty_ms")
            if detail.get("poll_uncertainty_ms") is not None
            else (
                round((observed_ts - row["previous_poll_ts"]) * 1000.0, 3)
                if isinstance(row.get("previous_poll_ts"), (int, float))
                and isinstance(observed_ts, (int, float)) else None
            )
        ),
        "provider_response_ms": row.get("response_ms"),
        "provider_occurrence_ts": occurrence_ts,
        "occurrence_minus_signal_ms": occurrence_delta_ms,
        "raw_provider_payload": raw_provider,
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
