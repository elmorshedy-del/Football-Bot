#!/usr/bin/env python3
"""Single-event feasibility pilot: can a price-only bot tell that a violent
repricing has exhausted itself, without knowing a goal happened and without a
timer?

The bot in this study sees one thing: the tape of executed trades on a Kalshi
three-way match market, each print carrying a price, a size and a taker side.
It never sees the score, the match minute, or a goal event.

Executable-side reconstruction
------------------------------
Kalshi reports every print as a YES-equivalent price plus the taker's side.
A resting order hit by a taker is therefore observable from the side it sat on:

    taker_side == "no"   a taker bought NO, consuming a resting YES *bid*
                         -> yes_bid  = print price,  no_ask = 1 - print price
    taker_side == "yes"  a taker bought YES, consuming a resting NO *bid*
                         -> yes_ask  = print price,  no_bid = 1 - print price

Both series are *executed* prices on a named side. No midpoint, no last price,
no settlement value is used anywhere, which is the repository's standing
invariant for trade highs and exits.

The Tie leg of a match whose tie is broken late collapses, so the side whose
executable price *rises* is NO. The simulated position is therefore long NO:
entry pays the NO ask, exit receives the NO bid.

Fees use the live quadratic taker schedule from app/paper.py:
    ceil(0.07 * multiplier * contracts * p * (1 - p) * 100) / 100
charged on entry and on exit (FEE_EXIT_TAKER defaults to true).
"""

import argparse
import datetime as dt
import json
import math
import statistics
from pathlib import Path

TICK = 0.01
STALE_MS = 3000.0  # a carried-forward quote older than this is not used


# --------------------------------------------------------------------------
# fees (mirrors app/paper.py fee_dollars)
# --------------------------------------------------------------------------
def fee_dollars(contracts, price, multiplier=1.0):
    p = float(price)
    return math.ceil(0.07 * multiplier * contracts * p * (1 - p) * 100) / 100.0


# --------------------------------------------------------------------------
# tape
# --------------------------------------------------------------------------
class Print:
    __slots__ = ("t", "yes", "side", "size", "yes_bid", "yes_ask",
                 "no_bid", "no_ask", "spread")

    def __init__(self, t, yes, side, size):
        self.t = t
        self.yes = yes
        self.side = side
        self.size = size
        self.yes_bid = self.yes_ask = None
        self.no_bid = self.no_ask = None
        self.spread = None


def load_tape(path):
    out = []
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            t = dt.datetime.fromisoformat(r["created_time"].replace("Z", "+00:00"))
            out.append(Print(t, float(r["yes_price_dollars"]),
                             r["taker_side"], float(r["count_fp"])))
    out.sort(key=lambda p: p.t)
    return out


def annotate(tape):
    """Carry forward the last executed price on each side, with a staleness cap."""
    last_bid = last_ask = None
    last_bid_t = last_ask_t = None
    for p in tape:
        if p.side == "no":
            last_bid, last_bid_t = p.yes, p.t
        else:
            last_ask, last_ask_t = p.yes, p.t
        fresh_bid = (last_bid if last_bid_t is not None and
                     (p.t - last_bid_t).total_seconds() * 1000 <= STALE_MS else None)
        fresh_ask = (last_ask if last_ask_t is not None and
                     (p.t - last_ask_t).total_seconds() * 1000 <= STALE_MS else None)
        p.yes_bid, p.yes_ask = fresh_bid, fresh_ask
        p.no_bid = None if fresh_ask is None else round(1.0 - fresh_ask, 4)
        p.no_ask = None if fresh_bid is None else round(1.0 - fresh_bid, 4)
        if fresh_bid is not None and fresh_ask is not None:
            p.spread = max(TICK, round(fresh_ask - fresh_bid, 4))
    return tape


def secs(a, b):
    return (b - a).total_seconds()


# --------------------------------------------------------------------------
# the held-side executable series the bot actually trades against
# --------------------------------------------------------------------------
def held_series(tape):
    """(time, executable NO bid) — the price an exit would actually receive.

    Only prints that really executed against a resting NO bid qualify, so every
    value here is a trade that happened at that instant.  Carrying a quote
    forward would be fatal in exactly the moment that matters: during the sweep
    the last NO bid seen is tens of cents stale, and pricing an exit against it
    invents a fill nobody could have got.
    """
    return [(p.t, round(1.0 - p.yes, 4)) for p in tape if p.side == "yes"]


def spread_map(tape, series, max_stale_ms=1000.0):
    """Executed spread at each exit observation: this print's NO bid against the
    most recent NO ask, both real fills, dropped when the pair is too stale."""
    out, si = {}, 0
    last_ask_t = last_ask = None
    for p in tape:
        if p.side == "no":
            last_ask, last_ask_t = p.yes, p.t          # yes bid == NO ask side
            continue
        if last_ask_t is not None and \
                (p.t - last_ask_t).total_seconds() * 1000 <= max_stale_ms:
            out[si] = max(TICK, round(p.yes - last_ask, 4))
        si += 1
    return out


def velocity(series, idx, horizon_s):
    """Favourable velocity in cents/sec over a trailing wall-clock horizon,
    using only prints at or before idx."""
    t_now, p_now = series[idx]
    j = idx
    while j > 0 and secs(series[j][0], t_now) < horizon_s:
        j -= 1
    dt_s = secs(series[j][0], t_now)
    if dt_s <= 0:
        return 0.0
    return (p_now - series[j][1]) * 100.0 / dt_s


# --------------------------------------------------------------------------
# baseline: the market's own ordinary regime, measured before the event
# --------------------------------------------------------------------------
def trailing_ref(tape, i, window=20):
    """Median yes price over the prints immediately before i."""
    lo = max(0, i - window)
    if i - lo < 3:
        return None
    return statistics.median(tape[k].yes for k in range(lo, i))


def dislocation(tape, i, window=20):
    """How far this print sits from the tape's own immediately preceding level,
    in dollars.  One print is enough to see a ladder sweep; a velocity measured
    over a whole second is not, because tick quantisation swamps it."""
    ref = trailing_ref(tape, i, window)
    if ref is None:
        return None
    return abs(tape[i].yes - ref)


def displacement(series, idx, horizon_s):
    """Favourable move of the held-side bid over a trailing wall-clock horizon."""
    t_now, p_now = series[idx]
    j = idx
    while j > 0 and secs(series[j][0], t_now) < horizon_s:
        j -= 1
    return p_now - series[j][1]


def burst_ceilings(tape, t_end, window_ms=150.0):
    """The most extreme burst the tape produced before t_end.  Anything the
    quiet market already did is not evidence of a repricing."""
    dev_max = lev_max = 0.0
    size_max = 0.0
    for i in range(len(tape)):
        if tape[i].t >= t_end:
            break
        d = dislocation(tape, i)
        if d is None:
            continue
        b = burst_at(tape, i, window_ms)
        dev_max = max(dev_max, d)
        lev_max = max(lev_max, len({round(q.yes, 2) for q in b}))
        size_max = max(size_max, sum(q.size for q in b))
    return {"dev_max": dev_max, "levels_max": int(lev_max), "size_max": size_max}


def baseline(tape, series, t_end, horizon_s):
    pre = [p for p in tape if p.t < t_end]
    pre_series_idx = [i for i, (t, _) in enumerate(series) if t < t_end]
    span = secs(pre[0].t, t_end) if pre else 0.0
    vels = [velocity(series, i, horizon_s) for i in pre_series_idx[5:]]
    spreads = [p.spread for p in pre if p.spread is not None]
    # inter-print gaps, the update cadence
    gaps = [secs(pre[i - 1].t, pre[i].t) for i in range(1, len(pre))]
    devs = [d for d in (dislocation(tape, i) for i in range(len(pre))) if d is not None]
    disps = [abs(displacement(series, i, horizon_s)) for i in pre_series_idx[5:]]
    return {
        "dev_max": max(devs) if devs else 0.0,
        "dev_p999": _quantile(sorted(devs), 0.999) if devs else 0.0,
        "disp_max": max(disps) if disps else 0.0,
        "span_s": span,
        "prints": len(pre),
        "update_rate_hz": (len(pre) / span) if span > 0 else 0.0,
        "median_gap_ms": (statistics.median(gaps) * 1000) if gaps else None,
        "vel_sd": statistics.pstdev(vels) if len(vels) > 1 else 0.0,
        "vel_p99": _quantile(sorted(abs(v) for v in vels), 0.99) if vels else 0.0,
        "vel_max_fav": max(vels) if vels else 0.0,
        "spread_median": statistics.median(spreads) if spreads else None,
        "spread_p90": _quantile(sorted(spreads), 0.90) if spreads else None,
    }


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


# --------------------------------------------------------------------------
# episode: onset, continuation waves, pauses, stabilisation
# --------------------------------------------------------------------------
def burst_at(tape, i, window_ms=150.0):
    """The prints within window_ms before and including i."""
    lo = i
    while lo > 0 and (tape[i].t - tape[lo - 1].t).total_seconds() * 1000 <= window_ms:
        lo -= 1
    return tape[lo:i + 1]


def find_onset(tape, series, dev_thresh, levels_min, confirm_ms, confirm_move,
               window_ms=150.0):
    """T0 = the first ladder sweep that both dislocates the tape and sticks.

    Two tests, because either alone is fooled.  A single off-level print clears
    a displacement test on its own and the quiet stretch of this tape contains
    several, so a sweep must also eat `levels_min` distinct price levels inside
    one 150 ms burst.  And a burst that dislocates but snaps straight back is a
    blip, not a repricing, so the executable bid on the side that moved must
    still be `confirm_move` away `confirm_ms` later.  Detection therefore lands
    `confirm_ms` after the sweep, which is a real cost and is reported as one.

    Returns (burst_index, detect_time, signature, rejected) where `rejected`
    lists the bursts that cleared the burst test and failed confirmation.
    """
    def bid_at(t):
        lo, hi, out = 0, len(series) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                out = series[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return out

    rejected = []
    for i in range(len(tape)):
        d = dislocation(tape, i)
        if d is None or d < dev_thresh:
            continue
        burst = burst_at(tape, i, window_ms)
        levels = len({round(q.yes, 2) for q in burst})
        if levels < levels_min:
            continue
        t_b = tape[i].t
        before = bid_at(t_b - dt.timedelta(milliseconds=1))
        detect_t = t_b + dt.timedelta(milliseconds=confirm_ms)
        after = bid_at(detect_t)
        if before is None or after is None:
            continue
        sig = {"disloc": d, "levels": levels,
               "size": sum(q.size for q in burst),
               "confirm_move": after - before, "t_burst": t_b}
        if after - before >= confirm_move:      # the NO side rose and held
            return i, detect_t, sig, rejected
        rejected.append(sig)
    return None, None, None, rejected


def abnormal_indices(series, t0_idx, disp_thresh, horizon_s):
    return [i for i in range(t0_idx, len(series))
            if displacement(series, i, horizon_s) >= disp_thresh]


def describe_episode(series, t0_idx, thresh, horizon_s):
    """Segment the post-onset path into continuation waves and pauses.

    T1 is the last instant at which the market moved fast enough in the new
    direction to have qualified as abnormal against its own pre-event
    baseline.  After T1 the tape never again reaches that rate, which is the
    operational meaning of 'returned to its ordinary fluctuation regime'.
    A pause inside the episode is a stretch of ordinary velocity that is
    later followed by another abnormal wave; the end of the episode is a
    stretch that never is.
    """
    hot = abnormal_indices(series, t0_idx, thresh, horizon_s)
    if not hot:
        return None
    t1_idx = hot[-1]
    # group hot indices into waves, split where the quiet gap is >= 1s
    waves, cur = [], [hot[0]]
    for a, b in zip(hot, hot[1:]):
        if secs(series[a][0], series[b][0]) >= 1.0:
            waves.append(cur)
            cur = [b]
        else:
            cur.append(b)
    waves.append(cur)
    pauses = []
    for w_prev, w_next in zip(waves, waves[1:]):
        pauses.append({
            "from": series[w_prev[-1]][0],
            "to": series[w_next[0]][0],
            "secs": secs(series[w_prev[-1]][0], series[w_next[0]][0]),
        })
    return {"t0_idx": t0_idx, "t1_idx": t1_idx, "waves": waves, "pauses": pauses}


# --------------------------------------------------------------------------
# candidate exits, each seeing only what is observable at that instant
# --------------------------------------------------------------------------
def simulate(series, spreads, entry_idx, rule, base):
    """Walk forward from entry, return the index the rule exits at (or None).

    `rule` is (family, params).  No rule may read any value at an index beyond
    the one it is currently standing on.
    """
    fam, prm = rule
    entry_p = series[entry_idx][1]
    run_max = entry_p
    last_max_idx = entry_idx
    armed_velocity = False
    cont_spreads = []

    for i in range(entry_idx + 1, len(series)):
        t, p = series[i]
        run_max = max(run_max, p)
        sp = spreads.get(i)
        if sp is not None:
            cont_spreads.append(sp)

        if fam == "A":  # continuation failure: no net progress over W updates
            w = prm["win"]
            if i - entry_idx >= w and (p - series[i - w][1]) <= 0:
                return i

        elif fam == "B":  # reversal as a fraction of the favourable move
            move = run_max - entry_p
            floor = max(2 * TICK, sp or TICK)
            # Unarmed until the move itself clears the prevailing spread: a
            # fraction of a one-tick move is quote noise, not a reversal.
            if move >= floor:
                give = run_max - p
                if give >= prm["frac"] * move and give >= floor:
                    return i

        elif fam == "C":  # spread blow-out relative to the continuation itself
            if len(cont_spreads) >= 20 and sp is not None:
                med = statistics.median(cont_spreads[:-1])
                if sp >= prm["k"] * med and sp > TICK:
                    return i

        elif fam == "D":  # velocity regime change: impulse -> neutral/negative
            v = velocity(series, i, prm["h"])
            if v >= base["onset_thresh"]:
                armed_velocity = True
            elif armed_velocity and v <= 0:
                return i

        elif fam == "E":  # plateau: updates keep arriving, best bid stops rising
            if p >= run_max - 1e-9:
                last_max_idx = i
            if i - last_max_idx >= prm["updates"]:
                return i

        elif fam == "T":  # diagnostic wall-clock exit
            if secs(series[entry_idx][0], t) >= prm["secs"]:
                return i
    if fam == "T":
        return len(series) - 1
    return None


def evaluate(series, entry_idx, exit_idx, entry_exec, t1_idx, contracts=100.0):
    """Full accounting for one exit, priced on the executable bid."""
    entry_t = series[entry_idx][0]
    exit_t, exit_bid = series[exit_idx]
    path = [p for _, p in series[entry_idx:exit_idx + 1]]
    run_max_at_exit = max(path)
    mfe_episode = max(p for _, p in series[entry_idx:t1_idx + 1])
    mfe_full = max(p for _, p in series[entry_idx:])
    after = [p for _, p in series[exit_idx:]]
    given_back_after = max(after) - exit_bid

    gross = exit_bid - entry_exec
    fees = (fee_dollars(contracts, entry_exec) +
            fee_dollars(contracts, exit_bid)) / contracts
    denom_ep = mfe_episode - entry_exec
    denom_full = mfe_full - entry_exec
    return {
        "entry_exec": entry_exec,
        "exit_exec": exit_bid,
        "gross": gross,
        "fees": fees,
        "net": gross - fees,
        "hold_s": secs(entry_t, exit_t),
        "updates_held": exit_idx - entry_idx,
        "capture_ratio": (gross / denom_full) if denom_full > 0 else None,
        "pct_move_to_T1": (gross / denom_ep) if denom_ep > 0 else None,
        "surrendered_from_peak": run_max_at_exit - exit_bid,
        "max_adverse": entry_exec - min(path),
        "given_back_after_exit": given_back_after,
        "exit_t": exit_t,
    }


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tape", type=Path, help="Kalshi trade JSONL for the Tie leg")
    ap.add_argument("--horizon", type=float, default=1.0,
                    help="velocity horizon in seconds (default 1.0)")
    ap.add_argument("--latency-ms", type=float, default=0.0,
                    help="detection-to-order latency applied to the entry fill")
    ap.add_argument("--cal-secs", type=float, default=300.0,
                    help="length of the quiet calibration stretch (default 300)")
    ap.add_argument("--confirm-ms", type=float, default=500.0,
                    help="how long a sweep must hold before it is believed")
    ap.add_argument("--confirm-mult", type=float, default=5.0,
                    help="confirmation move, as a multiple of the quiet ceiling")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    tape = annotate(load_tape(args.tape))
    series = held_series(tape)
    spread_at = spread_map(tape, series)

    print(f"tape                  {args.tape}")
    print(f"prints                {len(tape)}  "
          f"({tape[0].t.isoformat()} -> {tape[-1].t.isoformat()})")
    print(f"executed NO bids      {len(series)}  (taker_side='yes' prints; "
          f"{len(spread_at)} with a fresh paired ask)")

    # --- calibration -----------------------------------------------------
    # Ceilings come from a stretch that is certainly quiet and strictly before
    # anything this study trades.  `--cal-secs` is the only hand-set number.
    cal_end = tape[0].t + dt.timedelta(seconds=args.cal_secs)
    ceil = burst_ceilings(tape, cal_end)
    base = baseline(tape, series, cal_end, args.horizon)
    dev_thresh = ceil["dev_max"] + TICK
    levels_min = ceil["levels_max"] + 1
    confirm_move = args.confirm_mult * max(base["disp_max"], TICK)
    disp_thresh = base["disp_max"] + TICK
    base["onset_thresh"] = disp_thresh / args.horizon * 100.0  # c/s, for rule D

    print("\n--- calibration: the quiet market's own ceilings ---")
    print(f"window                first {args.cal_secs}s, "
          f"{base['prints']} prints, {base['update_rate_hz']:.2f} prints/s "
          f"(median gap {base['median_gap_ms']:.0f} ms)")
    print(f"spread                median {base['spread_median']*100:.2f}c  "
          f"p90 {base['spread_p90']*100:.2f}c")
    print(f"worst quiet burst     {ceil['dev_max']*100:.0f}c across "
          f"{ceil['levels_max']} levels")
    print(f"quiet {args.horizon:.1f}s move       {base['disp_max']*100:.0f}c max")
    print(f"onset gate            >= {dev_thresh*100:.0f}c dislocation, "
          f">= {levels_min} levels in 150 ms, then >= "
          f"{confirm_move*100:.0f}c still held {args.confirm_ms:.0f} ms later")

    t0_print, detect_t, sig, rejected = find_onset(
        tape, series, dev_thresh, levels_min, args.confirm_ms, confirm_move)
    if t0_print is None:
        raise SystemExit("no confirmed repricing found")
    t0_t = tape[t0_print].t
    t0_idx = max(0, next(i for i, (t, _) in enumerate(series) if t >= t0_t) - 1)
    print(f"rejected before it    {len(rejected)} burst(s) that dislocated but "
          f"did not hold")
    for r in rejected:
        print(f"    {r['t_burst'].strftime('%H:%M:%S')} "
              f"{r['disloc']*100:5.1f}c / {r['levels']:2d} levels -> only "
              f"{r['confirm_move']*100:+5.1f}c held")

    # --- episode -------------------------------------------------------
    ep = describe_episode(series, t0_idx, disp_thresh, args.horizon)
    _, t0_p = series[ep["t0_idx"]]
    t1_t, t1_p = series[ep["t1_idx"]]
    peak_ep = max(p for _, p in series[ep["t0_idx"]:ep["t1_idx"] + 1])
    print("\n--- repricing episode ---")
    print(f"T0                    {t0_t.isoformat()}  NO bid {t0_p:.2f} "
          f"(Tie YES {1-t0_p:.2f})")
    print(f"T1                    {t1_t.isoformat()}  NO bid {t1_p:.2f} "
          f"(Tie YES {1-t1_p:.2f})")
    print(f"duration              {secs(t0_t, t1_t):.1f}s")
    print(f"move over episode     {(peak_ep - t0_p)*100:.1f}c on the executable "
          f"NO bid  ({t0_p:.2f} -> {peak_ep:.2f})")
    print(f"continuation waves    {len(ep['waves'])}")
    for k, w in enumerate(ep["waves"], 1):
        a, b = series[w[0]], series[w[-1]]
        print(f"  wave {k}: {a[0].time()} -> {b[0].time()} "
              f"({secs(a[0], b[0]):5.1f}s, {len(w):4d} abnormal prints, "
              f"bid {a[1]:.2f} -> {b[1]:.2f})")
    if ep["pauses"]:
        pl = [p["secs"] for p in ep["pauses"]]
        print(f"internal pauses       {len(pl)}  "
              f"(median {statistics.median(pl):.1f}s, max {max(pl):.1f}s)")

    # --- entry ---------------------------------------------------------
    # Prints inside one sweep share a timestamp, so their order is not
    # recoverable and no fill inside the burst can be justified from the tape.
    # The entry is therefore the first executed NO ask strictly after it.
    det_t = detect_t + dt.timedelta(milliseconds=args.latency_ms)
    entry_exec = entry_t = None
    for q in tape:
        if q.t > det_t and q.side == "no" and q.no_ask is not None:
            entry_exec, entry_t = q.no_ask, q.t
            break
    if entry_exec is None:
        raise SystemExit("no executable NO ask after detection")
    entry_idx = next(i for i, (t, _) in enumerate(series) if t >= entry_t)
    print("\n--- entry (causal: dislocation alone, no score, no clock) ---")
    print(f"sweep at              {t0_t.isoformat()}  "
          f"({sig['disloc']*100:.0f}c across {sig['levels']} levels, "
          f"{sig['size']:.0f} contracts)")
    print(f"confirmed at          {detect_t.isoformat()}  "
          f"(bid had moved {sig['confirm_move']*100:+.0f}c and held)")
    print(f"entry fill            NO ask {entry_exec:.2f} at "
          f"{entry_t.isoformat()}")
    print(f"                      {secs(t0_t, entry_t)*1000:.0f} ms after the "
          f"sweep, latency budget {args.latency_ms:.0f} ms")

    mfe_full = max(p for _, p in series[entry_idx:])
    print(f"post-entry MFE        NO bid {mfe_full:.2f} "
          f"(+{(mfe_full-entry_exec)*100:.1f}c vs entry)")

    # --- candidate exits ----------------------------------------------
    rules = []
    for w in (10, 25, 50, 100):
        rules.append((f"A  continuation-failure  win={w:3d} upd", ("A", {"win": w})))
    for f in (0.10, 0.20, 0.30, 0.40):
        rules.append((f"B  reversal  {int(f*100):2d}% of move", ("B", {"frac": f})))
    for k in (2, 3):
        rules.append((f"C  spread blow-out  x{k}", ("C", {"k": k})))
    for h in (0.5, 1.0, 2.0):
        rules.append((f"D  velocity regime  h={h:.1f}s", ("D", {"h": h})))
    for n in (25, 50, 100, 200):
        rules.append((f"E  plateau  {n:3d} upd no new high", ("E", {"updates": n})))
    for sec in (1, 2, 5, 10, 20, 30, 60, 120):
        rules.append((f"T  fixed {sec:3d}s (diagnostic)", ("T", {"secs": sec})))
    rules.append(("R  hold to end of tape (ref)", ("T", {"secs": 1e9})))

    rows = []
    for label, rule in rules:
        xi = simulate(series, spread_at, entry_idx, rule, base)
        if xi is None:
            rows.append((label, None))
            continue
        rows.append((label, evaluate(series, entry_idx, xi, entry_exec,
                                     ep["t1_idx"])))

    hdr = (f"{'rule':<34}{'exit':>7}{'hold s':>8}{'upd':>6}{'gross':>8}"
           f"{'net':>8}{'capture':>9}{'peak give':>10}{'left after':>11}")
    print("\n--- candidate exits, priced on the executable NO bid ---")
    print(hdr)
    print("-" * len(hdr))
    for label, m in rows:
        if m is None:
            print(f"{label:<34}{'never':>7}")
            continue
        cr = f"{m['capture_ratio']*100:7.1f}%" if m["capture_ratio"] is not None else "     n/a"
        print(f"{label:<34}{m['exit_exec']:7.2f}{m['hold_s']:8.1f}"
              f"{m['updates_held']:6d}{m['gross']*100:7.1f}c{m['net']*100:7.1f}c"
              f"{cr:>9}{m['surrendered_from_peak']*100:9.1f}c"
              f"{m['given_back_after_exit']*100:10.1f}c")

    print("\n--- what the confirmation delay costs ---")
    print(f"{'confirm':>9}{'detect':>16}{'entry ask':>11}{'MFE after':>11}"
          f"{'false onsets':>14}")
    for cms in (0.0, 150.0, 500.0, 1000.0):
        ip, dt_, sg, rej = find_onset(tape, series, dev_thresh, levels_min,
                                      cms, confirm_move)
        if ip is None:
            print(f"{cms:8.0f}ms   no confirmed onset")
            continue
        ex = next((q.no_ask for q in tape
                   if q.t > dt_ and q.side == "no" and q.no_ask is not None), None)
        ei = next(i for i, (t, _) in enumerate(series) if t >= dt_)
        mfe = max(pp for _, pp in series[ei:])
        print(f"{cms:8.0f}ms{dt_.strftime('%H:%M:%S.%f')[:12]:>16}"
              f"{ex:11.2f}{(mfe-ex)*100:10.1f}c{len(rej):14d}")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "tape": str(args.tape),
            "baseline": {k: v for k, v in base.items()},
            "episode": {
                "T0": t0_t.isoformat(), "T1": t1_t.isoformat(),
                "duration_s": secs(t0_t, t1_t),
                "move_cents": (peak_ep - t0_p) * 100,
                "waves": len(ep["waves"]),
                "pauses_s": [p["secs"] for p in ep["pauses"]],
            },
            "entry": {"exec": entry_exec, "t": series[t0_idx][0].isoformat()},
            "mfe_full": mfe_full,
            "exits": [
                {"rule": label, **({} if m is None else
                 {k: (v.isoformat() if isinstance(v, dt.datetime) else v)
                  for k, v in m.items()})}
                for label, m in rows
            ],
        }, indent=1))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
