"""Prepare a limited screen from the independent two-leg audit bundle.

This adapter does not certify ordinary-period, negative-label or fresh-clock
coverage. It never relabels missing observations as negative examples.
"""
import argparse
import bisect
from collections import defaultdict
import csv
import gzip
import json
from pathlib import Path
import statistics


def prepare(audit_root):
    root = Path(audit_root)
    research = root/"football-research"
    with gzip.open(research/"two_leg_replay_cache.json.gz", "rt") as handle:
        cache = json.load(handle)
    with (research/"two_leg_event_paths.csv").open() as handle:
        legs = list(csv.DictReader(handle))
    with (research/"late_candidates.json").open() as handle:
        cases = {str(c["record_id"]): c for c in json.load(handle)}
    groups = {}
    excluded = []
    truths, coverage = [], []
    qindex = {}
    for market, qs in cache["quotes"].items():
        # A timestamp-normalized book is a diagnostic of the exchange state,
        # not evidence the strategy had already received that book.
        points = {}
        for q in qs:
            age = q.get("exchange_age_ms")
            if q.get("valid") is False or age is None or not -.25 <= age/1000 <= 120:
                continue
            ts = round(q["time"]-age/1000, 3)
            points[ts] = dict(q, exchange_ts=ts)
        ordered = [points[k] for k in sorted(points)]
        qindex[market] = ([q["exchange_ts"] for q in ordered], ordered)

    def quote_at(market, ts):
        times, qs = qindex.get(market, ([], []))
        i = bisect.bisect_right(times, ts)-1
        if i < 0 or ts-times[i] > 1:
            return None
        q = qs[i]
        if q["yes_bid"] > q["yes_ask"] or min(q["yes_bid_size"], q["yes_ask_size"]) < 1:
            return None
        return q

    def onset(leg):
        anchor = float(leg["anchor_ts"])
        market, side = leg["ticker"], leg["held_side"]
        samples = [quote_at(market, anchor-offset) for offset in range(41,61)]
        base = [q[side+"_ask"] for q in samples if q is not None]
        if len(base) < 3:
            return None
        baseline = statistics.median(base)
        for ts in qindex.get(market, ([],[]))[0]:
            if not anchor-45 <= ts <= anchor+20:
                continue
            q = quote_at(market, ts)
            if q is None or q[side+"_ask"] < baseline+10:
                continue
            future = [quote_at(market, ts+step) for step in (.025,.05,.1)]
            if all(p is not None and p[side+"_ask"] >= baseline+10 for p in future):
                return ts
        return None

    for leg in legs:
        group = leg["event"]
        anchor = float(leg["anchor_ts"])
        groups[group] = min(anchor, groups.get(group, anchor))
        case = cases[str(leg["record_id"])]
        before = tuple(map(int, case["score_before"].split("-")))
        after = tuple(map(int, case["score_after"].split("-")))
        if sum(after)-sum(before) != 1 or not ((before[0]==before[1]) != (after[0]==after[1])):
            raise ValueError("Case is not a positive single-goal tie/lead state change")
        complete = leg["price_window_complete"] == "True"
        positive = (complete and leg["large_move"] == "True"
                    and leg["stable_completion"] == "True"
                    and not leg["reversal_record_id"])
        if positive:
            start = onset(leg)
            if start is None:
                excluded.append(dict(record_id=leg["record_id"],market=leg["ticker"],
                                     reason="independent_price_onset_unavailable"))
                continue
            ident = leg["record_id"]+":"+leg["role"]
            truths.append(dict(id=ident,event_id=leg["record_id"],group=group,
                               market=leg["ticker"],side=leg["held_side"],
                               onset_ts=start,start_ts=start-1,
                               end_ts=min(start+30,float(leg["forward_end_ts"])),
                               baseline_c=float(leg["baseline_bid"]),
                               terminal_c=float(leg["peak_bid"])))
        elif complete:
            coverage.append(dict(group=group,market=leg["ticker"],
                                 start_ts=anchor-45,
                                 end_ts=min(anchor+30,float(leg["forward_end_ts"])),
                                 negative_labels_complete=True,
                                 basis="complete case lacks a stable >=15c completed move"))
        else:
            excluded.append(dict(record_id=leg["record_id"],market=leg["ticker"],
                                 reason="incomplete_price_window"))
        # Ordinary-looking controls remain UNCERTAIN because this 500-record
        # observer export is not a certified complete negative event ledger.
        coverage.append(dict(group=group,market=leg["ticker"],start_ts=anchor-150,
                             end_ts=anchor-65,negative_labels_complete=False,
                             basis="uncertified pre-case control"))

    features = []
    for market, candidates in cache["candidates"].items():
        group = market.rsplit("-",1)[0]
        if group not in groups:
            continue
        for c in candidates:
            side = "yes" if c["direction"] == 1 else "no"
            q = quote_at(market, c["time"]+.1)
            features.append(dict(market=market,group=group,received_ts=c["time"],
                                 exchange_ts=c["exchange_time"],log_move=c["dl"],
                                 levels=c["levels"],volume=c["volume"],side=side,
                                 eligible=None,
                                 entry_ask_c=q[side+"_ask"] if q else None))
    return dict(
        metadata=dict(schema="football.threshold_dataset.v1",feature_floor_log=.2,
                      continuous_ordinary_periods_complete=False,
                      negative_labels_complete=False,clock_coverage_complete=False,
                      feature_capture_complete=False,synthetic=False,
                      scope="Conditional case/control screen, not the full late-game population",
                      excluded=excluded,source_case_legs=len(legs),
                      source_complete_price_legs=sum(l["price_window_complete"]=="True" for l in legs),
                      label_definition="Independent score state change; >=15c stable completed move; no known reversal",
                      onset_definition="Fixed +10c ask shift persisting 100ms, independent of searched threshold",
                      quote_cost="Exchange-book diagnostic at receipt+100ms, not a guaranteed fill"),
        groups=[dict(id=k,start_ts=v) for k,v in sorted(groups.items())],
        features=features,truths=truths,coverage=coverage)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-directory",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args(argv)
    data=prepare(args.audit_directory)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(data,separators=(",",":"),allow_nan=False))
    print(json.dumps(dict(groups=len(data["groups"]),features=len(data["features"]),
                          truth_legs=len(data["truths"]),metadata=data["metadata"]),indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
