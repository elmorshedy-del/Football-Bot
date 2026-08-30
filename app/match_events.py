"""Deterministic normalization for match-feed observations.

Raw provider payloads are always retained.  This module adds a small canonical
schema for storage, joins and human-readable audit views without using natural
language interpretation in the trading path.
"""
def _score_key_matches(key, side):
    lowered = key.lower()
    compact = lowered.replace("_", "").replace(".", "")
    tail = lowered.replace("_", ".").split(".")[-1]
    return compact in {f"{side}score", f"score{side}"} or (
        tail == side and "score" in lowered
    )


def _primary_score(signature, side):
    matches = [(key.count("."), key, value) for key, value in signature.items()
               if _score_key_matches(key, side)]
    if not matches:
        return None
    return float(min(matches)[2])


def score_pair(signature):
    return {
        "home": _primary_score(signature, "home"),
        "away": _primary_score(signature, "away"),
    }


def _first_field(value, names):
    wanted = {name.lower().replace("_", "") for name in names}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("_", "")
            if normalized in wanted and isinstance(child, (str, int, float)):
                return child
        for child in value.values():
            found = _first_field(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_field(child, names)
            if found is not None:
                return found
    return None


def _score_text(pair):
    if pair["home"] is None or pair["away"] is None:
        return None
    return f"{int(pair['home'])}–{int(pair['away'])}"


def normalize_match_event(change_kind, before, after, live_data=None):
    """Return a stable canonical event object from numeric score changes."""
    live_data = live_data or {}
    before_pair, after_pair = score_pair(before), score_pair(after)
    home_delta = ((after_pair["home"] or 0.0) - (before_pair["home"] or 0.0)
                  if after_pair["home"] is not None or before_pair["home"] is not None else 0.0)
    away_delta = ((after_pair["away"] or 0.0) - (before_pair["away"] or 0.0)
                  if after_pair["away"] is not None or before_pair["away"] is not None else 0.0)
    if home_delta and not away_delta:
        side = "home"
    elif away_delta and not home_delta:
        side = "away"
    else:
        side = "unknown"

    if change_kind == "goal":
        canonical_type = f"goal_observed.{side}"
        action = f"{side.title()} goal observed" if side != "unknown" else "Goal observed"
    elif change_kind == "score_correction":
        canonical_type = f"score_correction.{side}"
        action = (f"{side.title()} score correction" if side != "unknown"
                  else "Score correction")
    else:
        canonical_type = "score_schema_change.unknown"
        action = "Score schema changed"

    before_text, after_text = _score_text(before_pair), _score_text(after_pair)
    transition = f"{before_text} → {after_text}" if before_text and after_text else None
    human_label = f"{action} · {transition}" if transition else action
    details = live_data.get("details") or {}
    minute = _first_field(details, ("minute", "match_minute", "game_minute"))
    stoppage = _first_field(details, ("stoppage", "stoppage_time", "added_time"))
    period = _first_field(details, ("period", "phase", "half"))
    clock = _first_field(details, ("match_clock", "game_clock", "clock"))
    return {
        "schema": "football.match_event.v1",
        "canonical_type": canonical_type,
        "side": side,
        "score_before": before_pair,
        "score_after": after_pair,
        "score_transition": transition,
        "provider_minute": minute,
        "provider_stoppage": stoppage,
        "provider_period": period,
        "provider_clock": clock,
        "human_label": human_label,
    }


def event_consistency(inferred_state, normalized):
    """Describe logical agreement without claiming that the feed caused a trade."""
    canonical = (normalized or {}).get("canonical_type", "")
    after = (normalized or {}).get("score_after") or {}
    home, away = after.get("home"), after.get("away")
    if canonical.startswith("score_correction"):
        return "correction_or_reversal"
    if not canonical.startswith("goal_observed"):
        return "time_match_only"
    if inferred_state == "equal_score_0" and home is not None and home == away:
        return "equalizer_consistent"
    if inferred_state == "one_goal_lead_+1" and home is not None and away is not None \
            and abs(home - away) == 1:
        return "one_goal_lead_consistent"
    if inferred_state in {"equal_score_0", "one_goal_lead_+1"} and \
            home is not None and away is not None:
        return "state_mismatch"
    return "goal_consistent_state_unknown"
