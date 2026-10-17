"""Offline log-move threshold search. No order or deployment APIs.

Python 3.10+, standard library. Independent labels never enter the detector.
Run with --input a canonical dataset, optionally --raw-trades JSONL(.gz).
See research/THRESHOLD_OPTIMIZER.md for the data and scoring contracts.
"""
import argparse
import bisect
from collections import defaultdict, deque
import csv
import datetime as dt
from decimal import Decimal
import gzip
import json
import math
from pathlib import Path
import random
import statistics

# Prespecified comparators, never selected from holdout results. The original
# value is the literal DL_MIN default in app/config.py at main 1132c4b.
BASELINES = {"repository_original_log": .8, "previous_proposed_log": .5}


def clock_eligible(stamp, now, min_minute=80, max_age_s=10):
    """Clock-only gate: never derive current minute from a scorer or goal."""
    if not isinstance(stamp, dict):
        return False
    age = now - float(stamp.get("observed_ts", -1e30))
    return (0 <= age <= max_age_s and stamp.get("status") == "live"
            and stamp.get("period") in (2, "2nd", "second_half")
            and isinstance(stamp.get("minute"), (int, float))
            and stamp["minute"] >= min_minute)


def _logit(cents):
    p = min(max(cents / 100, .0001), .9999)
    return math.log(p / (1-p))


def trade_features(trades, min_log=0):
    """Yield prefix-only features; input order is receipt order, not sorted future tape."""
    tapes = defaultdict(deque)
    last_receipt = -math.inf
    for t in trades:
        market = t["market"]
        ts, received = float(t["exchange_ts"]), float(t["received_ts"])
        price, size = float(t["yes_price_c"]), float(t["size"])
        if not all(math.isfinite(x) for x in (ts, received, price, size)):
            raise ValueError("Non-finite trade field")
        if not 0 <= price <= 100 or size <= 0 or t["taker_side"] not in ("yes", "no"):
            raise ValueError("Invalid trade price, quantity or taker side")
        if received < last_receipt:
            raise ValueError("Raw trades must be supplied in recorded receipt order")
        last_receipt = received
        tape = tapes[market]
        # Match the repository detector's millisecond windows, including its
        # late-exchange-print semantics. Every member has already been received;
        # a newer exchange stamp in this prefix is not future information.
        ms = ts*1000
        tape.append((ms, price, size, t["taker_side"]))
        while tape and tape[0][0] < ms-300_000:
            tape.popleft()
        burst = [x for x in tape if x[0] >= ms-150]
        ref = sorted(x[1] for x in tape if ms-2100 <= x[0] < ms-150)
        if not ref or not burst:
            continue
        reference = ref[len(ref)//2]
        if not .5 <= reference <= 99.5:
            continue
        buy = sum(x[2] for x in burst if x[3] == "yes")
        sell = sum(x[2] for x in burst if x[3] == "no")
        direction = 1 if buy >= sell else -1
        extreme = max(x[1] for x in burst) if direction == 1 else min(x[1] for x in burst)
        displacement = (_logit(extreme)-_logit(reference))*direction
        if displacement >= min_log:
            yield dict(market=market, group=t["group"], exchange_ts=ts,
                       received_ts=received, log_move=displacement,
                       levels=len({round(x[1], 1) for x in burst}),
                       volume=buy+sell, side="yes" if direction == 1 else "no",
                       eligible=t.get("eligible"), reference_yes=reference,
                       extreme_yes=extreme)


def make_alerts(features, threshold, min_levels=5, min_size=200, cooldown_s=120):
    """Replay each threshold causally; a weak early alert may consume lockout."""
    last = {}
    out = []
    for f in sorted(features, key=lambda x: x["received_ts"]):
        if f.get("eligible") is False:
            continue
        if f["log_move"] < threshold or f["levels"] < min_levels or f["volume"] < min_size:
            continue
        timestamp = f["received_ts"]
        if timestamp-last.get(f["market"], -math.inf) < cooldown_s:
            continue
        last[f["market"]] = timestamp
        out.append(f)
    return out


def attach_clocks(features, observations):
    """Join only the last clock observation already received at each decision."""
    groups = defaultdict(list)
    for stamp in observations:
        groups[stamp["group"]].append(stamp)
    for stamps in groups.values():
        stamps.sort(key=lambda s: s["observed_ts"])
    for f in features:
        stamps = groups.get(f["group"], [])
        index = bisect.bisect_right([s["observed_ts"] for s in stamps], f["received_ts"])-1
        stamp = stamps[index] if index >= 0 else None
        yield dict(f, eligible=clock_eligible(stamp, f["received_ts"]),
                   clock_observation_id=stamp.get("id") if stamp else None)


def _ratio(a, b):
    return a/b if b else None


def score_alerts(alerts, truths, coverage, miss_penalty_s=30):
    """One-to-one event detection. Unknown labels never become assumed negatives."""
    by_market = defaultdict(list)
    cov = defaultdict(list)
    by_group = defaultdict(lambda: dict(tp=0, fp=0, fn=0, unknown=0,
                                        delay_total=0., truth_count=0))
    for t in truths:
        by_market[t["market"]].append(t)
        by_group[t["group"]]["truth_count"] += 1
    for c in coverage:
        cov[c["market"]].append(c)
    used, audit, delays, prices = set(), [], [], []
    duplicates = 0
    for a in sorted(alerts, key=lambda x: x["received_ts"]):
        ts, market, group = a["received_ts"], a["market"], a["group"]
        candidates = [t for t in by_market[market]
                      if t["side"] == a["side"] and t["start_ts"] <= ts <= t["end_ts"]]
        unused = [t for t in candidates if t["id"] not in used]
        target = min(unused, key=lambda t: (abs(ts-t["onset_ts"]), t["id"])) if unused else None
        if target:
            if target["group"] != group:
                raise ValueError("Alert/truth match-group mismatch")
            used.add(target["id"])
            status = "tp"
            delay = max(0., ts-target["onset_ts"])
            delays.append(delay)
            by_group[group]["delay_total"] += min(delay, miss_penalty_s)
            if a.get("entry_ask_c") is not None:
                prices.append(a["entry_ask_c"])
        elif candidates:
            status = "fp_duplicate"
            duplicates += 1
        elif any(c.get("negative_labels_complete") is True
                 and c["start_ts"] <= ts <= c["end_ts"] for c in cov[market]):
            status = "fp"
        else:
            status = "unknown"
        by_group[group]["fp" if status.startswith("fp") else status] += 1
        audit.append(dict(market=market, group=group, side=a["side"],
                          received_ts=ts, exchange_ts=a.get("exchange_ts"),
                          status=status, truth_id=target["id"] if target else None,
                          entry_ask_c=a.get("entry_ask_c")))
    for t in truths:
        if t["id"] not in used:
            by_group[t["group"]]["fn"] += 1
            by_group[t["group"]]["delay_total"] += miss_penalty_s
    tp = sum(g["tp"] for g in by_group.values())
    fp = sum(g["fp"] for g in by_group.values())
    fn = sum(g["fn"] for g in by_group.values())
    unknown = sum(g["unknown"] for g in by_group.values())
    n = len(truths)
    return dict(tp=tp, fp=fp, fn=fn, unknown=unknown, duplicates=duplicates,
                precision=_ratio(tp, tp+fp), recall=_ratio(tp, n),
                false_per_true=_ratio(fp, tp),
                median_delay_s=statistics.median(delays) if delays else None,
                penalized_delay_s=_ratio(sum(g["delay_total"] for g in by_group.values()), n),
                median_entry_c=statistics.median(prices) if prices else None,
                truth_legs=n, truth_events=len({t.get("event_id", t["id"]) for t in truths}),
                truth_matches=len({t["group"] for t in truths}),
                unknown_fraction=_ratio(unknown, tp+fp+unknown),
                by_group=dict(by_group), audit=audit)


def split_groups(groups, holdout_fraction=.3):
    if not 0 < holdout_fraction < 1:
        raise ValueError("holdout_fraction must be between zero and one")
    days = sorted({int(g["start_ts"]//86400) for g in groups})
    if len(days) < 2:
        raise ValueError("Need at least two distinct days for an untouched holdout")
    cut = days[max(1, min(len(days)-1, math.ceil(len(days)*(1-holdout_fraction))))]
    train = {g["id"] for g in groups if int(g["start_ts"]//86400) < cut}
    test = {g["id"] for g in groups if g["id"] not in train}
    return train, test


def choose_threshold(rows, min_precision=.9, min_recall=.8):
    eligible = [r for r in rows if (r.get("precision") or 0) >= min_precision
                and (r.get("recall") or 0) >= min_recall
                and r.get("false_per_true") is not None
                and r.get("penalized_delay_s") is not None]
    return min(eligible, key=lambda r: (r["penalized_delay_s"], r["false_per_true"],
               r.get("median_entry_c") if r.get("median_entry_c") is not None else math.inf,
               r["threshold"]), default=None)


def pareto_frontier(rows):
    candidates = [r for r in rows if r.get("false_per_true") is not None
                  and r.get("penalized_delay_s") is not None and r.get("recall") is not None]
    def objectives(r):
        return r["false_per_true"], r["penalized_delay_s"], 1-r["recall"]
    return [r for r in candidates if not any(
        all(x <= y for x, y in zip(objectives(other), objectives(r)))
        and any(x < y for x, y in zip(objectives(other), objectives(r)))
        for other in candidates if other is not r)]


def threshold_grid(start=.2, stop=2, step=.05, feature_floor=0):
    if start < feature_floor-1e-12:
        raise ValueError("Requested threshold is below the input feature-capture floor")
    if step <= 0 or stop < start or start < 0:
        raise ValueError("Invalid threshold range")
    a, b, h = map(lambda x: Decimal(str(x)), (start, stop, step))
    out = []
    while a <= b:
        out.append(float(a))
        a += h
    return out


def recommendation_blockers(meta, train, test, min_precision=.9, min_recall=.8):
    flags = {
        "continuous_ordinary_periods_complete": "continuous_ordinary_periods_incomplete",
        "negative_labels_complete": "negative_labels_incomplete",
        "clock_coverage_complete": "late_clock_coverage_incomplete",
        "feature_capture_complete": "feature_capture_incomplete",
    }
    reasons = [reason for field, reason in flags.items() if meta.get(field) is not True]
    if meta.get("certified_control_market_minutes", 0) < 60:
        reasons.append("fewer_than_60_certified_control_market_minutes")
    if meta.get("synthetic") is True:
        reasons.append("synthetic_data_only")
    if train.get("truth_matches", 0) < 20:
        reasons.append("fewer_than_20_training_positive_matches")
    if test.get("truth_matches", 0) < 10:
        reasons.append("fewer_than_10_holdout_positive_matches")
    if (test.get("precision") or 0) < min_precision:
        reasons.append("holdout_precision_below_target")
    if (test.get("recall") or 0) < min_recall:
        reasons.append("holdout_recall_below_target")
    if train.get("unknown", 0) or test.get("unknown", 0):
        reasons.append("unclassified_alerts_remain")
    return reasons


def cluster_intervals(metrics, repeats=500, seed=17):
    """Resample whole matches, not correlated legs, for descriptive intervals."""
    groups = list(metrics.get("by_group", {}).values())
    if len(groups) < 2:
        return dict(reason="fewer_than_two_match_clusters")
    rng = random.Random(seed)
    values = dict(precision=[], recall=[], penalized_delay_s=[])
    for _ in range(repeats):
        sample = [rng.choice(groups) for _ in groups]
        tp, fp = sum(g["tp"] for g in sample), sum(g["fp"] for g in sample)
        n = sum(g["truth_count"] for g in sample)
        for key, value in [("precision", _ratio(tp, tp+fp)), ("recall", _ratio(tp, n)),
                           ("penalized_delay_s", _ratio(sum(g["delay_total"] for g in sample), n))]:
            if value is not None:
                values[key].append(value)
    out = dict(method="whole-match percentile bootstrap", repeats=repeats, match_clusters=len(groups))
    for key, vals in values.items():
        vals.sort()
        out[key] = [vals[int(.025*(len(vals)-1))], vals[int(.975*(len(vals)-1))]] if vals else None
    return out


def read_raw_trades(paths, allowed_groups):
    """Read canonical or original recorder JSONL without loading whole archives."""
    for path in paths:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt") as handle:
            for line in handle:
                frame = json.loads(line)
                if "exchange_ts" in frame:
                    if frame["group"] in allowed_groups:
                        yield frame
                    continue
                packet = frame.get("m") or {}
                if packet.get("type") != "trade":
                    continue
                msg = packet.get("msg") or {}
                market = msg.get("market_ticker", "")
                group = market.rsplit("-", 1)[0]
                if group not in allowed_groups:
                    continue
                received = frame.get("at", frame.get("lt"))
                if received is None:
                    raise ValueError("Raw frame lacks original receipt time")
                yield dict(market=market, group=group, received_ts=float(received),
                           exchange_ts=float(msg["ts_ms"])/1000 if msg.get("ts_ms") is not None else float(msg["ts"]),
                           yes_price_c=float(msg["yes_price_dollars"])*100,
                           size=float(msg["count_fp"]), taker_side=msg["taker_side"],
                           eligible=None)


def validate_dataset(data):
    groups = data["groups"]
    identifiers = [g["id"] for g in groups]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicate match-group IDs")
    known = set(identifiers)
    truths = data["truths"]
    if len({t["id"] for t in truths}) != len(truths):
        raise ValueError("Duplicate truth IDs")
    for t in truths:
        if t["group"] not in known or t["side"] not in ("yes", "no"):
            raise ValueError("Invalid truth group or held side")
        if not t["start_ts"] <= t["onset_ts"] <= t["end_ts"]:
            raise ValueError("Invalid truth association interval")
    for f in data.get("features", []):
        if f["group"] not in known or f["side"] not in ("yes", "no"):
            raise ValueError("Invalid feature group or side")
        for field in ("received_ts", "exchange_ts", "log_move", "levels", "volume"):
            if not math.isfinite(float(f[field])):
                raise ValueError("Non-finite feature field")
    for c in data["coverage"]:
        if c["group"] not in known or c["start_ts"] > c["end_ts"]:
            raise ValueError("Invalid coverage interval")


def certified_control_minutes(coverage, truths):
    """Union certified coverage per market; subtract target-association windows."""
    intervals = defaultdict(list)
    holes = defaultdict(list)
    for c in coverage:
        if c.get("negative_labels_complete") is True:
            intervals[c["market"]].append((c["start_ts"], c["end_ts"]))
    for t in truths:
        holes[t["market"]].append((t["start_ts"], t["end_ts"]))
    def union(xs):
        merged = []
        for start, end in sorted(xs):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return merged
    seconds = 0.
    for market, xs in intervals.items():
        for start, end in union(xs):
            remaining = [(start, end)]
            for lo, hi in union(holes[market]):
                pieces = []
                for a, b in remaining:
                    if hi <= a or lo >= b:
                        pieces.append((a, b))
                    else:
                        if a < lo:
                            pieces.append((a, lo))
                        if hi < b:
                            pieces.append((hi, b))
                remaining = pieces
            seconds += sum(b-a for a,b in remaining)
    return seconds/60


def run_search(data, grid, min_precision=.9, min_recall=.8, holdout_fraction=.3,
               cooldown_s=120, min_levels=5, min_size=200):
    validate_dataset(data)
    grid = sorted(set(grid) | set(BASELINES.values()))
    if grid and min(grid) < data["metadata"].get("feature_floor_log", 0)-1e-12:
        raise ValueError("Grid falls below feature capture floor")
    train_groups, test_groups = split_groups(data["groups"], holdout_fraction)
    def subset(name, groups):
        return [x for x in data[name] if x["group"] in groups]
    training = {name: subset(name, train_groups) for name in ("features", "truths", "coverage")}
    holdout = {name: subset(name, test_groups) for name in ("features", "truths", "coverage")}
    rows = []
    for threshold in grid:
        alerts = make_alerts(training["features"], threshold, min_levels, min_size, cooldown_s)
        metrics = score_alerts(alerts, training["truths"], training["coverage"])
        rows.append(dict(threshold=threshold, **metrics))
    chosen = choose_threshold(rows, min_precision, min_recall)
    # Freeze before touching holdout outcomes. Never tune on a holdout grid.
    frozen = dict(threshold=chosen["threshold"], training_metrics=chosen) if chosen else None
    test = {}
    if frozen:
        alerts = make_alerts(holdout["features"], frozen["threshold"], min_levels, min_size, cooldown_s)
        test = score_alerts(alerts, holdout["truths"], holdout["coverage"])
        test["cluster_intervals"] = cluster_intervals(test)
    baselines = {}
    for name, threshold in BASELINES.items():
        alerts = make_alerts(holdout["features"], threshold, min_levels, min_size, cooldown_s)
        baselines[name] = dict(threshold=threshold,
            training_metrics=next(r for r in rows if r["threshold"] == threshold),
            holdout_metrics=score_alerts(alerts, holdout["truths"], holdout["coverage"]),
            sibling_confirmation=False, entry_price_gates=False,
            interpretation="Prespecified ungated log baseline; not the full deployed strategy")
    meta = dict(data["metadata"])
    meta["certified_control_market_minutes"] = certified_control_minutes(data["coverage"], data["truths"])
    if any(f.get("eligible") is None for f in data["features"]):
        meta["clock_coverage_complete"] = False
    reasons = recommendation_blockers(meta, chosen or {}, test, min_precision, min_recall)
    if not frozen:
        reasons.append("no_training_threshold_meets_quality_targets")
    recommended = frozen["threshold"] if frozen and not reasons else None
    return dict(recommended_threshold=recommended, frozen_training_candidate=frozen,
                holdout_metrics=test, blockers=reasons, data_coverage=meta, training_grid=rows,
                baseline_comparison=baselines,
                training_frontier=pareto_frontier(rows),
                split=dict(training_groups=sorted(train_groups), holdout_groups=sorted(test_groups)),
                policy=dict(min_precision=min_precision, min_recall=min_recall,
                            miss_penalty_s=30, cooldown_s=cooldown_s, min_levels=min_levels,
                            min_size=min_size, optimization="earliest subject to quality; frontier retained",
                            warning="A grid winner is not proof of a global optimum or trading profitability"))


def _compact(metrics):
    return {k: v for k, v in metrics.items() if k not in ("audit", "by_group")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw-trades", type=Path, nargs="+")
    parser.add_argument("--start", type=float, default=.2)
    parser.add_argument("--stop", type=float, default=2)
    parser.add_argument("--step", type=float, default=.05)
    parser.add_argument("--min-precision", type=float, default=.9)
    parser.add_argument("--min-recall", type=float, default=.8)
    parser.add_argument("--holdout-fraction", type=float, default=.3)
    parser.add_argument("--cooldown-s", type=float, default=120)
    args = parser.parse_args(argv)
    if not 0 < args.min_precision <= 1 or not 0 < args.min_recall <= 1:
        parser.error("Quality targets must be in (0, 1]")
    if args.cooldown_s < 0:
        parser.error("Cooldown cannot be negative")
    data = json.loads(args.input.read_text())
    if args.raw_trades:
        capture_floor = min(args.start, *BASELINES.values())
        data["features"] = list(trade_features(
            read_raw_trades(args.raw_trades, {g["id"] for g in data["groups"]}), min_log=capture_floor))
        data["metadata"]["feature_floor_log"] = capture_floor
    if data.get("clock_observations"):
        data["features"] = list(attach_clocks(data["features"], data["clock_observations"]))
    grid = threshold_grid(args.start, args.stop, args.step,
                          data["metadata"].get("feature_floor_log", 0))
    result = run_search(data, grid, args.min_precision, args.min_recall,
                        args.holdout_fraction, args.cooldown_s)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/"result.json").write_text(json.dumps(result, indent=2, allow_nan=False))
    rows = [_compact(r) for r in result["training_grid"]]
    if rows:
        with (args.out/"training_grid.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (args.out/"training_frontier.json").write_text(
        json.dumps([_compact(r) for r in result["training_frontier"]], indent=2, allow_nan=False))
    print(json.dumps(dict(recommended_threshold=result["recommended_threshold"],
                          frozen_training_candidate=result["frozen_training_candidate"]["threshold"]
                          if result["frozen_training_candidate"] else None,
                          blockers=result["blockers"],
                          holdout_metrics=_compact(result["holdout_metrics"]),
                          evaluated_thresholds=len(result["training_grid"])), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
