#!/usr/bin/env python3
"""Calibrates "how many dollars at API prices 1 % of the limit is worth" and computes my share.

Principle (no curve fitting):
  growth of the limit percentage over an interval = rate × my local spend + spend outside this Mac,
  and spend outside this Mac is never negative. So the rate is not taken "on average"
  (that would mean counting all spend as mine) but as a lower bound: the largest rate
  at which my spend never explains more than the limit actually grew.
  It is correct if on at least some intervals the account was used only from this Mac.
  Everything above "rate × my spend" is spend outside Claude Code on this Mac
  (chat, voice, phone, other people).

Dollars are computed at API prices with token-type weights as in the price list: a check against
5-hour limit hits showed that exactly these weights give the smallest spread.
"""
import bisect
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

Q = 0.03        # fraction of pairs allowed to violate the bound (rounding and sampling noise)
TOL = 0.5       # the percentage arrives as an integer
MIN_USD = {"7d": 60.0, "5h": 15.0, "fable": 30.0}
SPAN = {"5h": 5 * 3600, "7d": 7 * 86400}
LOOKBACK = 35 * 86400
# A reading that falls by more than this many points inside one window is an unscheduled reset (14.11.2026:
# Claude's weekly limit fell from 81 to 0 with the same reset time). Claude's readings never fall otherwise.
# Antigravity's settle by up to 2 points after use, so its block passes its own value (antigravity.DROP_PTS).
RESET_DROP = 1.5
K_DEFAULT = 2.4  # how many times faster a Fable dollar uses up the shared limit than an Opus dollar
# least squares counts use outside this Mac (chat, Cowork, other devices) as mine, so it drifts high
# on accounts with much outside use (23.11.2026, a Max 5x account: 7.6 against 2.0 from its own Fable window)
K_FIT_RANGE = (1.0, 6.0)
K_GUARD = (0.5, 10.0)  # the other estimates are not biased that way: this only guards against broken input


class Spend:
    """Cumulative account spend per model family: answers "how much between t0 and t1" quickly.
    `fam` maps a model id to a family; Opus and Fable get their own columns, everything else is "other"."""

    def __init__(self, con, acc, fam=core.family):
        self.t, self.o, self.f, self.x = [], [], [], []
        o = f = x = 0.0
        for ts, model, c in con.execute(
                "SELECT ts,model,cost FROM rows WHERE acc=? AND cost IS NOT NULL ORDER BY ts", (acc,)):
            f_ = fam(model)
            if f_ == "opus":
                o += c
            elif f_ == "fable":
                f += c
            else:
                x += c
            self.t.append(ts); self.o.append(o); self.f.append(f); self.x.append(x)

    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return (self.o[i], self.f[i], self.x[i]) if i >= 0 else (0.0, 0.0, 0.0)

    def between(self, t0, t1):
        a, b = self.at(t0), self.at(t1)
        return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def weighted(d, k):
    return d[0] + k * d[1] + d[2]


def load_points(con, acc, win, t0):
    """Window samples plus exact points from limit hits (100 % at the hit, 0 % at the start of the 5-hour window)."""
    # samples without a reset time are skipped: between two such samples the window could have changed unnoticed
    pts = [(ts, pct, ra) for ts, pct, ra in con.execute(
        "SELECT ts,pct,resets_at FROM samples WHERE acc=? AND win=? AND ts>=? AND resets_at IS NOT NULL ORDER BY ts",
        (acc, win, t0))]
    kinds = {"5h": ("five_hour",), "7d": ("seven_day",), "fable": ("seven_day_overage_included",)}.get(win, ())
    for kind in kinds:
        for ts, ra in con.execute(
                "SELECT min(ts),resets_at FROM hits WHERE acc=? AND kind=? AND ts>=? GROUP BY resets_at", (acc, kind, t0)):
            if ra:
                ra = (ra + 30) // 60 * 60
                pts.append((ts, 100.0, ra))
                if win == "5h":
                    pts.append((ra - SPAN["5h"], 0.0, ra))
    pts.sort()
    return pts


def segments(pts, drop=RESET_DROP):
    """Split into segments of one window without resets; keep only points where the percentage changed."""
    segs, cur = [], []
    for p in pts:
        if cur and (p[1] < cur[-1][1] - drop or abs(p[2] - cur[-1][2]) > 120):
            segs.append(cur); cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    thin = []
    for seg in segs:
        keep = [seg[0]]
        for a, b in zip(seg, seg[1:]):
            if b[1] != keep[-1][1]:
                if a is not keep[-1]:
                    keep.append(a)  # last point with the old value: the boundary of the increase
                keep.append(b)
        if seg[-1] is not keep[-1]:
            keep.append(seg[-1])
        thin.append(keep)
    return thin


def seg_pairs(spend, seg):
    cum = [spend.at(ts) for ts, _, _ in seg]
    out = []
    for i in range(len(seg)):
        for j in range(i + 1, len(seg)):
            out.append((seg[j][1] - seg[i][1], (cum[j][0] - cum[i][0], cum[j][1] - cum[i][1], cum[j][2] - cum[i][2])))
    return out


def envelope(pairs, k, min_usd, fable_only=False):
    rs = []
    for dp, d in pairs:
        w = d[1] if fable_only else weighted(d, k)
        if w >= min_usd:
            rs.append((dp + TOL) / w)
    if len(rs) < 12:
        return None, len(rs)
    rs.sort()
    return rs[int(len(rs) * Q)], len(rs)


def least_squares_k(pairs):
    """Fable/Opus rate ratio from an ordinary two-feature regression (only the ratio is needed)."""
    soo = sof = sff = sop = sfp = 0.0
    n = 0
    for dp, d in pairs:
        o, f = d[0] + d[2], d[1]
        if o + f < 20:
            continue
        soo += o * o; sof += o * f; sff += f * f; sop += o * dp; sfp += f * dp; n += 1
    det = soo * sff - sof * sof
    if n < 200 or det <= 0:
        return None
    a = (sop * sff - sfp * sof) / det
    b = (sfp * soo - sop * sof) / det
    if a <= 0 or b <= 0:
        return None
    return b / a


def pure_k(pairs, min_o, min_f, pure=0.05):
    """Fable/Opus ratio from stretches where only one family ran: the same lower bound as the rates,
    taken separately per Fable dollar and per Opus dollar. Outside use cannot inflate it the way it
    inflates least squares, but it needs enough single-model stretches of both kinds."""
    ro, rf = [], []
    for dp, d in pairs:
        o, f = d[0] + d[2], d[1]
        if o >= min_o and f < pure * o:
            ro.append((dp + TOL) / o)
        elif f >= min_f and o < pure * f:
            rf.append((dp + TOL) / f)
    if len(ro) < 12 or len(rf) < 12:
        return None, 0
    ro.sort(); rf.sort()
    return rf[int(len(rf) * Q)] / ro[int(len(ro) * Q)], min(len(ro), len(rf))


def share_k(pairs, fable_rate, share, k):
    """Fable/Opus ratio at which the Fable cap takes exactly the share of the week the server reports.
    100 % of the Fable window is 100 / fable_rate Fable dollars, worth k × week_rate × that weekly percent,
    so share = 100 k × week_rate / fable_rate, k = share × fable_rate / (100 × week_rate).
    week_rate is itself measured with k, hence the fixed-point loop; it converges from any start
    (in 2 steps on an Opus-heavy history, in about 30 when Opus and Fable always ran together)."""
    for _ in range(60):
        w, _ = envelope(pairs, k, MIN_USD["7d"])
        if not w:
            return None
        nk = share * fable_rate / (100.0 * w)
        if abs(nk - k) < 1e-4 * k:
            return nk
        k = nk
    return k


def current_window(con, acc, win, now):
    """Latest sample of the window, if the window has not reset yet."""
    # our own sample beats the CodexBar history: it has the breakdown and the reset time
    r = con.execute("SELECT ts,pct,resets_at,source FROM samples WHERE acc=? AND win=? AND source='poll' AND ts>=? "
                    "ORDER BY ts DESC LIMIT 1", (acc, win, now - 3600)).fetchone()
    if not r:
        r = con.execute("SELECT ts,pct,resets_at,source FROM samples WHERE acc=? AND win=? ORDER BY ts DESC LIMIT 1",
                        (acc, win)).fetchone()
    if not r:
        return None
    ts, pct, ra, source = r
    if ra and ra <= now:
        return {"pct": 0.0, "ts": ts, "resets_at": None, "source": source, "rolled": True}
    return {"pct": pct, "ts": ts, "resets_at": ra, "source": source, "rolled": False}


def window_start(con, acc, win, cur, now, drop=RESET_DROP):
    """Start of the current window: reset time minus window length, or the last unscheduled reset."""
    ra = cur.get("resets_at")
    span = SPAN.get(win, 7 * 86400)
    if not ra:
        return None
    start = ra - span
    prev = None
    for ts, pct in con.execute("SELECT ts,pct FROM samples WHERE acc=? AND win=? AND ts>=? ORDER BY ts", (acc, win, start)):
        if prev is not None and pct < prev[1] - drop:
            # the reset happened between prev and this sample; prev itself still belongs to the old window,
            # so the new one starts right after it (otherwise the chart gets a 100 -> 0 cliff and the pace goes negative)
            start = prev[0] + 1
        prev = (ts, pct)
    return start


def window_series(con, sp, acc, win, start, rate, k, cap=96):
    """Window progress for the chart: [time, limit percentage, of which mine]. Only points where the percentage changed."""
    pts = con.execute("SELECT ts,pct FROM samples WHERE acc=? AND win=? AND ts>=? ORDER BY ts", (acc, win, start)).fetchall()
    keep = []
    for i, (ts, pct) in enumerate(pts):
        if not keep or pct != keep[-1][1] or i == len(pts) - 1:
            keep.append((ts, pct))
    if len(keep) > cap:
        step = len(keep) / (cap - 1)
        keep = [keep[int(i * step)] for i in range(cap - 1)] + [keep[-1]]
    # the window starts at zero; but if the first sample is far from the start, do not draw a line through the gap
    if keep and start + 60 < keep[0][0] <= start + 0.15 * SPAN.get(win, SPAN["7d"]):
        keep.insert(0, (start, 0.0))
    out = []
    for ts, pct in keep:
        mine = None
        if rate:
            d = sp.between(start, ts)
            mine = round(min(pct, rate * (d[1] if win == "fable" else weighted(d, k))), 1)
        out.append([ts, pct, mine])
    return out


# Forecast: (how many recent seconds count as the "recent pace", its weight when mixed with the window's average pace)
FC = {"5h": (3600, 0.5), "7d": (48 * 3600, 0.5)}
BAND_DEFAULT = (0.5, 1.0, 1.6)   # while own history is short: remaining spend from half to 1.6 of the forecast


def _interp(pts, t, start):
    """Limit percentage at time t from the window samples (linear between samples, before the first one from zero at the window start)."""
    i = bisect.bisect_right(pts, (t, 1e9))
    a = pts[i - 1] if i > 0 else (start, 0.0)
    if i >= len(pts) or pts[i][0] <= a[0]:
        return a[1]
    b = pts[i]
    return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])


def forecast_rate(pts, t, pct, start, kind, fc=None):
    """Expected pace, % per second: a mix of the average pace over the whole window and the pace over the last hours."""
    back, w = fc or FC[kind]
    avg = pct / max(60.0, t - start)
    t0 = max(start, t - back)
    recent = (pct - _interp(pts, t0, start)) / max(60.0, t - t0)
    return max(0.0, w * recent + (1 - w) * avg)


def past_windows(con, acc, win, now, drop=RESET_DROP):
    """Finished limit windows with samples: [(start, reset, [(time, percentage), ...])]."""
    span = SPAN.get(win, SPAN["7d"])
    by = {}
    for ts, pct, ra in con.execute("SELECT ts,pct,resets_at FROM samples WHERE acc=? AND win=? AND resets_at IS NOT NULL "
                                   "AND resets_at<? ORDER BY ts", (acc, win, now)):
        by.setdefault((ra + 150) // 300, []).append((ts, pct, ra))
    out = []
    for pts in by.values():
        ra = pts[-1][2]
        p = [(t, x) for t, x, _ in pts if ra - span <= t <= ra]
        if len(p) < 4 or any(y[1] < x[1] - drop for x, y in zip(p, p[1:])):
            continue
        if p[-1][1] < 99 and p[-1][0] < ra - 0.2 * span:
            continue   # we did not see how the window ended
        out.append((ra - span, ra, p))
    return out


def forecast_band(wins, kind, fc=None):
    """Backtests the forecast on past windows: by what factor the actual remaining spend differed from the forecast.
    Returns the 20/50/80 % quantiles of that ratio and the number of checks."""
    ratios = []
    for start, ra, pts in wins:
        span = ra - start
        hit = next((t for t, x in pts if x >= 99.5), None)
        end_t, end_pct = (hit, 100.0) if hit else (ra, pts[-1][1])
        seen = set()
        for f in (0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85):
            i = bisect.bisect_right(pts, (start + f * span, 1e9)) - 1
            if i < 0 or i in seen:
                continue
            seen.add(i)
            t, pct = pts[i]
            if pct < 3 or end_t - t < 0.05 * span or t < start + (f - 0.1) * span:
                continue
            r = forecast_rate(pts[:i + 1], t, pct, start, kind, fc)
            if r > 0:
                ratios.append(((end_pct - pct) / (end_t - t)) / r)
    if len(ratios) < 30:
        return None
    ratios.sort()
    q = lambda x: ratios[min(len(ratios) - 1, int(len(ratios) * x))]
    return (q(0.2), q(0.5), q(0.8), len(ratios))


def forecast(pts, start, reset, now, pct, kind, band, band_src=None):
    """Where the window will be at reset with my usual behaviour: the middle and the 20-80 % spread.
    band_src says whose past windows the spread comes from: "own", "pooled" (all accounts) or "default"."""
    lo, mid, hi = (band or BAND_DEFAULT)[:3]
    mid = min(1.5, max(0.25, mid))
    r = forecast_rate(pts, now, pct, start, kind)
    rem = max(0.0, reset - now)

    def end(q):
        return round(min(100.0, pct + q * r * rem), 1)

    def hit(q):
        if pct >= 100:
            return now
        if q * r <= 0:
            return None
        t = now + (100.0 - pct) / (q * r)
        return int(t) if t < reset else None

    return {"at": now, "pct": round(pct, 1), "ratePerHour": round(mid * r * 3600, 3),
            "endPct": end(mid), "endLo": end(lo), "endHi": end(hi),
            "hitAt": hit(mid), "hitEarly": hit(hi), "hitLate": hit(lo),
            "checks": band[3] if band else 0,
            # the Fable window has no history of its own and is checked against weekly windows
            "bandSource": band_src or ("own" if band else "default"), "bandWindow": kind}


def pad_models(m, fam=core.family):
    """For Claude accounts every per-model map carries exactly core.MODEL_KEYS (zeros where nothing ran);
    another family function (Antigravity) keeps its own keys."""
    if fam is not core.family:
        return m
    return {f: m.get(f, [0.0, 0]) for f in core.MODEL_KEYS}


def by_family(con, acc, t0, t1, fam=core.family):
    """My spend per model family over an interval: {family: [dollars, tokens]}."""
    out = {}
    for model, c, tok in con.execute("SELECT model, sum(cost), sum(inp+out+cw5+cw1h+cr) FROM rows "
                                     "WHERE acc=? AND ts>=? AND ts<? GROUP BY model", (acc, t0, t1)):
        d = out.setdefault(fam(model), [0.0, 0])
        d[0] += c or 0.0
        d[1] += tok or 0
    return pad_models({f: [round(v[0], 2), v[1]] for f, v in out.items()}, fam)


PEAK_FINAL = 6 * 3600   # a weekly reading this close to the reset is "where the week ended"


def split_points(parts, total):
    """Rounds {key: points} to 0.1 so that the rounded parts add up exactly to round(total, 1)."""
    tenths = round(total * 10)
    raw = {k: v * 10 for k, v in parts.items()}
    low = {k: int(v) for k, v in raw.items()}
    rest = tenths - sum(low.values())
    for k in sorted(raw, key=lambda k: raw[k] - low[k], reverse=True)[:max(0, rest)]:
        low[k] += 1
    return {k: v / 10 for k, v in low.items()}


def weekly_windows(con, sp, acc, now, count=8, fam=core.family, rate=None, k=1.0, cur7=None, drop=RESET_DROP):
    """My spend per weekly limit window of this account (boundaries from the reset time), the highest
    reading of the weekly limit in it and, for Claude accounts, how many points of it were mine.
    rate: the account's weekly % per weighted dollar (the one windows.7d.mine uses), k: its Fable weight,
    cur7: the account's windows["7d"], so the current week agrees with it exactly.

    A mid-week reset (the reading drops by more than `drop` points inside one window, as on 14.11.2026) gives
    the account a fresh limit for the rest of that window. The row then measures peakPct, minePct and
    othersPct from the last such reset (`resetAt`), as windows.7d does for the current week; usd, m and
    tokens stay the whole week. Before, the peak of the wiped part was set against the whole week's spend."""
    r = con.execute("SELECT resets_at FROM samples WHERE acc=? AND win='7d' AND resets_at IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1", (acc,)).fetchone()
    if not r:
        return []
    end = r[0]
    while end <= now:
        end += SPAN["7d"]
    # weekly limit hits from the Claude Code logs are exact 100 % readings
    hit_rows = con.execute("SELECT ts,resets_at FROM hits WHERE acc=? AND kind='seven_day' AND resets_at IS NOT NULL",
                           (acc,)).fetchall()
    claude = fam is core.family
    out = []
    for i in range(count):
        e = end - i * SPAN["7d"]
        b = e - SPAN["7d"]
        d = sp.between(b, min(e, now))
        tokens = con.execute("SELECT coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=? AND ts<?",
                             (acc, b, e)).fetchone()[0]
        # the window's readings in time order, hits included (a hit sits before the drop that follows it)
        pts = con.execute("SELECT ts,pct FROM samples WHERE acc=? AND win='7d' AND abs(resets_at-?)<=120",
                          (acc, e)).fetchall()
        pts += [(ts, 100.0) for ts, ra in hit_rows if abs((ra + 30) // 60 * 60 - e) <= 120]
        pts.sort()
        since = b
        for (t0, p0), (_, p1) in zip(pts, pts[1:]):
            if p1 < p0 - drop:
                since = t0 + 1   # the reset came after the last reading of the old part, as in window_start()
        cur = i == 0 and cur7 and not cur7.get("rolled") and abs((cur7.get("resetsAt") or 0) - e) <= 120
        if cur and (cur7.get("start") or 0) > since:
            since = cur7["start"]
        peak = peak_at = None
        for ts, pct in pts:
            # the highest reading after the last reset; of equal ones the latest
            if ts >= since and (peak is None or pct >= peak):
                peak, peak_at = pct, ts
        if i and not tokens and peak is None:
            continue
        m = by_family(con, acc, b, e, fam)
        w = {"start": b, "end": e, "current": i == 0,
             "usd": {"opus": round(d[0], 2), "fable": round(d[1], 2), "other": round(d[2], 2)},
             "tokens": tokens, "peakPct": peak, "peakAt": peak_at,
             # at 100 % the limit cannot go higher, so that reading is where the week ended too
             "peakFinal": None if peak is None else bool(peak >= 100 or (i > 0 and peak_at >= e - PEAK_FINAL)),
             "resetAt": since if since > b else None,
             "m": m}
        if claude:
            w.update({"minePct": None, "minePctByModel": None, "othersPct": None})
            if rate:
                # the same arithmetic as windows.7d.mine: weighted dollars (Fable x k) through the account's rate
                mm = m if since == b else by_family(con, acc, since, e, fam)
                raw = {f: rate * mm[f][0] * (k if f == "fable" else 1.0) for f in core.MODEL_KEYS}
                total = sum(raw.values())
                if cur and cur7.get("mine") is not None:
                    mine, others = cur7["mine"], cur7.get("outside")
                else:
                    # a final peak is where the week ended, so mine cannot be more; an early one is only
                    # "at least", and mine may run past it
                    mine = min(peak if (peak is not None and w["peakFinal"]) else 100.0, total)
                    others = None
                # rounded first, so the parts, minePct and othersPct add up to the tenth
                mine = round(mine, 1)
                if not cur and peak is not None:
                    others = max(0.0, peak - mine)
                w["minePct"] = mine
                w["minePctByModel"] = split_points({f: v * mine / total for f, v in raw.items()}, mine) if total \
                    else (dict.fromkeys(core.MODEL_KEYS, 0.0) if not mine else None)
                w["othersPct"] = None if others is None else round(others, 1)
        out.append(w)
    return out


def analyse(con, now=None):
    now = now or int(time.time())
    accs = core.accounts()
    spends = {a["uuid"]: Spend(con, a["uuid"]) for a in accs}
    t0 = now - LOOKBACK

    # 1. Fable/Opus ratio per account and window. One number for all accounts put the Fable part of the weekly
    # "mine" a quarter too high on some of them: on 23.11.2026 a Max 20x account measured 2.6 and a Max 5x one
    # 2.0 for the week and 2.3 for the 5-hour window.
    # Weekly order: from the server's Fable share and the account's own weekly and Fable rates (ties the Fable
    # dollar to the Fable window, which only Fable moves), single-model stretches, own least squares,
    # then borrowed from the best-measured account.
    pairs_of = {}
    kk_of = {}
    for a in accs:
        u = a["uuid"]
        pp = {}
        for win in ("7d", "5h", "fable"):
            pp[win] = []
            for seg in segments(load_points(con, u, win, t0)):
                pp[win] += seg_pairs(spends[u], seg)
        pairs_of[u] = pp
        n = len(pp["7d"])
        fit = least_squares_k(pp["7d"])
        ok_fit = fit if fit and K_FIT_RANGE[0] <= fit <= K_FIT_RANGE[1] else None
        shares = ((core.kv_get(con, f"last_ok:{u}") or {}).get("usage") or {}).get("weekly_scoped_shares") or []
        share = shares[0].get("allowance_percent_of_weekly") if shares else None
        f, _ = envelope(pp["fable"], 0, MIN_USD["fable"], True)
        ks = share_k(pp["7d"], f, share, ok_fit or K_DEFAULT) if f and share else None
        kp, _ = pure_k(pp["7d"], MIN_USD["7d"], MIN_USD["fable"])
        x = {"fit": round(fit, 3) if fit else None}
        for kk, src in ((ks, "share"), (kp, "pure"), (ok_fit, "fit")):
            if kk and K_GUARD[0] <= kk <= K_GUARD[1]:
                x.update({"k": kk, "source": src, "from": a["short"], "pairs": n})
                break
        k5, n5 = pure_k(pp["5h"], MIN_USD["5h"], MIN_USD["5h"])
        if k5 and K_GUARD[0] <= k5 <= K_GUARD[1]:
            x.update({"k5": k5, "source5": "pure", "from5": a["short"], "pairs5": n5})
        kk_of[u] = x
    # fallbacks: share-derived first, then the most weekly pairs. Pooling all accounts' pairs into one
    # regression would be wrong: their percent per dollar differs about 2.4 times.
    trust = {"share": 2, "pure": 1, "fit": 0}
    measured = sorted((x for x in kk_of.values() if "k" in x),
                      key=lambda x: (trust[x["source"]], x["pairs"]), reverse=True)
    pool = measured[0] if measured else {"k": K_DEFAULT, "source": "default", "from": "default", "pairs": 0}
    measured5 = sorted((x for x in kk_of.values() if "k5" in x), key=lambda x: x["pairs5"], reverse=True)
    for x in kk_of.values():
        if "k" not in x:
            x.update({"k": pool["k"], "source": "pooled" if measured else "default", "from": pool["from"],
                      "pairs": pool["pairs"]})
        if "k5" not in x:
            # the 5-hour ratio differs from the weekly one where both are measured, so borrow a 5-hour one first
            if measured5:
                m = measured5[0]
                x.update({"k5": m["k5"], "source5": "pooled", "from5": m["from5"], "pairs5": m["pairs5"]})
            else:
                x.update({"k5": x["k"], "source5": "weekly", "from5": x["from"], "pairs5": 0})

    here = os.path.dirname(os.path.abspath(__file__))
    vpn = core.kv_get(con, "vpn_up") or {}
    # k_fable / k_source / k_pairs: the fallback for accounts without a ratio of their own (and what older
    # readers show for every account); each account's own value is accounts[].kFable
    state = {"schema": 4, "generatedAt": now, "k_fable": round(pool["k"], 3), "k_source": pool["from"],
             "k_pairs": pool["pairs"],
             "dashboardPath": os.path.join(here, "dashboard.html"),
             # what CodexBar runs to refresh limits when the menu opens
             "collector": {"python": sys.executable, "tick": os.path.join(here, "tick.py")},
             "vpnUp": vpn.get("up"),
             "vpnState": vpn.get("state"),
             "accounts": []}
    bands = {}
    for kind in ("5h", "7d"):
        own = {a["uuid"]: past_windows(con, a["uuid"], kind, now) for a in accs}
        pooled = forecast_band([w for ws in own.values() for w in ws], kind)
        for a in accs:
            b = forecast_band(own[a["uuid"]], kind)
            bands[(a["uuid"], kind)] = (b, "own") if b else (pooled, "pooled" if pooled else "default")
    import poll  # here, not at the top: poll is not needed by the Antigravity block, which imports calib
    today0 = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    spans = ("today", "7d", "30d")
    try:
        cache = core.cache_totals(con, {"today": today0, "7d": now - 7 * 86400, "30d": now - 30 * 86400})
    except Exception:
        # the cache figures are extra: without them the accounts are still written (cache: null)
        cache = None
        state["cacheError"] = _error()
    for a in accs:
        u = a["uuid"]
        try:
            info = account_state(con, a, spends[u], pairs_of[u], kk_of[u], bands, now)
        except Exception:
            # one account that cannot be computed must not cost the others theirs (state.json would not be
            # written at all and every page would go stale); the readers need only uuid, name and short
            info = {"uuid": u, "name": a["name"], "short": a["short"], "active": a["active"],
                    "useHapp": a["useHapp"], "error": _error()}
        info["refresh"], info["refreshEverySec"] = poll.refresh_mode(a)
        info["pausedByVpn"] = (info["refresh"] == "background" and vpn.get("state") == "on")
        info["cache"] = None if cache is None else {p: core.cache_view((cache.get(u) or {}).get(p)) for p in spans}
        state["accounts"].append(info)
    # rolling 24 hours of every Claude Code call on this Mac, also calls no account was found for and accounts
    # whose own figures failed this time (tick.py builds the top-level spend24h from it)
    try:
        h24 = con.execute("SELECT coalesce(sum(cost),0), coalesce(sum(inp+out+cw5+cw1h+cr),0), "
                          "coalesce(sum(CASE WHEN acc IS NULL THEN cost END),0) FROM rows WHERE ts>=?",
                          (now - 86400,)).fetchone()
        state["claude24h"] = {"usd": round(h24[0], 2), "tokens": h24[1], "unattributedUsd": round(h24[2], 2)}
    except Exception:
        state["claude24h"] = {"error": _error()}
    # every Claude Code call on this Mac: the accounts above plus the few calls no account could be found for
    state["cache"] = None if cache is None else {
        p: core.cache_view(core.cache_merge(c.get(p) for c in cache.values())) for p in spans}
    return state


def _error():
    """The current exception as one short line, as tick.log writes it."""
    return "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]


def account_state(con, a, sp, pairs, kx, bands, now, fam=core.family, drop=RESET_DROP):
    """One account's entry of the summary: windows, rates, drift, hits, daily spend, weeks, value.
    a: {uuid, name, short, active, useHapp, orgs}; sp: its Spend; pairs: {window: sample pairs};
    kx: its Fable/Opus ratios (k, k5, sources); bands: {(uuid, "5h"|"7d"): (forecast band, source)};
    fam: model id -> family key for the per-model breakdowns (`m`); drop: the fall in points that counts
    as an unscheduled reset (RESET_DROP). Everything it reads from `con`
    is filtered by a["uuid"], so another source of samples and rows can be analysed the same way."""
    u = a["uuid"]
    k = kx["k"]
    rates = {}
    kw = {"7d": k, "5h": kx["k5"], "fable": k}   # the Fable window itself counts Fable dollars only
    info = {"uuid": u, "name": a["name"], "short": a["short"], "active": a["active"],
            "useHapp": a["useHapp"], "orgs": a["orgs"], "windows": {}, "rates": {}, "drift": [],
            # kSource: share | pure | fit | pooled | default; kFrom: whose data gave it;
            # kFit: own least squares, shown even when rejected; kFable5h: the same ratio for the 5-hour window
            "kFable": round(k, 3), "kSource": kx["source"], "kFrom": kx["from"], "kPairs": kx["pairs"],
            "kFit": kx["fit"], "kFable5h": round(kx["k5"], 3), "kSource5h": kx["source5"],
            "kFrom5h": kx["from5"]}
    last_ok = core.kv_get(con, f"last_ok:{u}") or {}
    info["plan"] = last_ok.get("plan")
    info["org"] = last_ok.get("org")
    info["tier"] = (last_ok.get("plan") or {}).get("rate_limit_tier")
    shares = (last_ok.get("usage") or {}).get("weekly_scoped_shares") or []
    info["fableShareOfWeek"] = shares[0].get("allowance_percent_of_weekly") if shares else None
    tier = core.TIERS.get(info["tier"] or "")
    info["planName"], info["planPriceUsd"] = (tier[0], tier[1]) if tier else (None, None)
    # whether the account has a 5-hour limit: the server either returns the window or not (Max 20x currently has none)
    info["has5h"] = bool((last_ok.get("usage") or {}).get("five_hour")) if last_ok.get("usage") else None
    info["lastMeasuredAt"] = last_ok.get("ts")
    lp = con.execute("SELECT ts,status FROM polls WHERE acc=? ORDER BY ts DESC LIMIT 1", (u,)).fetchone()
    info["lastPoll"] = {"ts": lp[0], "status": lp[1]} if lp else None

    # 2. rates for each window type
    fable_win = "fable"
    for win, src_win, fable_only in (("7d", "7d", False), ("5h", "5h", False), ("fable", "fable", True)):
        s, n = envelope(pairs[src_win], kw[win], MIN_USD[win], fable_only)
        if s:
            info["rates"][win] = {"pct_per_usd": s, "usd_per_100": 100 / s, "pairs": n, "source": "own"}
            rates[(u, win)] = s

        # 3. drift: the same estimate separately for each weekly window (backtest)
        if win == "7d":
            all_pts = load_points(con, u, "7d", 0)
            by_week = {}
            for seg in segments(all_pts, drop):
                ra = next((p[2] for p in reversed(seg) if p[2]), None)
                if ra:
                    by_week.setdefault(ra, []).extend(seg_pairs(sp, seg))
            for ra, pp in sorted(by_week.items()):
                s_w, n_w = envelope(pp, k, MIN_USD["7d"])
                if s_w and n_w >= 150:
                    wk = sp.between(ra - SPAN["7d"], ra)   # what I used that week
                    info["drift"].append({"weekEnd": ra, "usd_per_100": round(100 / s_w, 1), "pairs": n_w,
                                          "spend": {"opus": round(wk[0], 2), "fable": round(wk[1], 2),
                                                    "other": round(wk[2], 2)}})

    # Fable limit: while own samples are few, derive the rate from what the server itself reports:
    # what share of the weekly limit the Fable cap is (currently 50 %).
    share = info.get("fableShareOfWeek")
    if "fable" not in info["rates"] and "7d" in info["rates"] and share:
        s = k * info["rates"]["7d"]["pct_per_usd"] * 100.0 / share
        info["rates"]["fable"] = {"pct_per_usd": s, "usd_per_100": 100 / s, "pairs": 0, "source": "derived"}
        rates[(u, "fable")] = s

    # 4. current windows: how many percent are mine, how many are outside this Mac
    for win, src_win in (("5h", "5h"), ("7d", "7d"), ("fable", fable_win)):
        if not src_win:
            continue
        cur = current_window(con, u, src_win, now)
        if not cur:
            continue
        w = {"pct": cur["pct"], "measuredAt": cur["ts"], "resetsAt": cur["resets_at"], "source": cur["source"],
             "rolled": bool(cur.get("rolled"))}
        start = window_start(con, u, src_win, cur, now, drop)
        if start:
            d_meas = sp.between(start, cur["ts"])   # my spend up to the sample time
            d_now = sp.between(start, now)          # and up to the current minute
            w["start"] = start
            w["usd"] = {"opus": round(d_now[0], 2), "fable": round(d_now[1], 2), "other": round(d_now[2], 2)}
            s = rates.get((u, win))
            if s:
                wm = d_meas[1] if win == "fable" else weighted(d_meas, kw[win])
                wn = d_now[1] if win == "fable" else weighted(d_now, kw[win])
                mine = min(cur["pct"], s * wm)
                w["mine"] = round(mine, 1)
                w["outside"] = round(cur["pct"] - mine, 1)
                w["sinceMeasure"] = round(max(0.0, s * (wn - wm)), 1)  # mine after the sample, not yet in the percentage
            if not cur.get("rolled"):
                w["series"] = window_series(con, sp, u, src_win, start, s, kw[win])
                # no forecast in the first minutes of a window: 2 % after one minute would read as 120 % per hour
                if (now - cur["ts"] <= 1800 and cur["resets_at"] and cur["resets_at"] > now
                        and now - start >= 0.03 * SPAN.get(src_win, SPAN["7d"])):
                    # the forecast starts from the current minute: the sample plus what I spent after it
                    kind = "5h" if win == "5h" else "7d"
                    pct_now = min(100.0, cur["pct"] + (w.get("sinceMeasure") or 0.0))
                    pts = [(r[0], r[1]) for r in con.execute(
                        "SELECT ts,pct FROM samples WHERE acc=? AND win=? AND ts>=? AND ts<=? ORDER BY ts",
                        (u, src_win, start, cur["ts"]))]
                    w["forecast"] = forecast(pts, start, cur["resets_at"], now, pct_now, kind,
                                             *bands.get((u, kind), (None, "default")))
        if win == "7d" and cur["source"] == "poll" and not cur["rolled"]:
            # what the server itself attributes to Claude Code, chat and Cowork (in limit percent)
            br = {}
            for key in ("claude_code", "chat", "cowork", "other"):
                r = con.execute("SELECT pct FROM samples WHERE acc=? AND win=? AND ts=?",
                                (u, f"7d:{key}", cur["ts"])).fetchone()
                if r:
                    br[key] = r[0]
            if br:
                w["server"] = br
        info["windows"][win] = w

    # 5. recent limit hits: my spend in the window at the time of the hit (the "100 %" point)
    info["hits"] = []
    for kind, ra, ts in con.execute(
            "SELECT kind,resets_at,min(ts) FROM hits WHERE acc=? GROUP BY kind,resets_at ORDER BY 3", (u,)):
        span = SPAN["5h"] if kind == "five_hour" else SPAN["7d"]
        d = sp.between(ra - span, ts) if ra else (0, 0, 0)
        info["hits"].append({"ts": ts, "kind": kind, "opus": round(d[0], 1), "fable": round(d[1], 1),
                             "other": round(d[2], 1)})

    # 6. daily spend over 180 days: per model family, dollars and tokens
    days = {}
    for day, model, c, tok in con.execute(
            """SELECT date(ts,'unixepoch','localtime'), model, sum(cost), sum(inp+out+cw5+cw1h+cr)
               FROM rows WHERE acc=? AND ts>=? GROUP BY 1,2 ORDER BY 1""", (u, now - 180 * 86400)):
        d = days.setdefault(day, {}).setdefault(fam(model), [0.0, 0])
        d[0] += c or 0.0
        d[1] += tok or 0
    info["daily"] = [
        {"day": day,
         "opus": round(m.get("opus", [0])[0], 2), "fable": round(m.get("fable", [0])[0], 2),
         "other": round(sum(v[0] for f, v in m.items() if f not in ("opus", "fable")), 2),
         "tokens": sum(v[1] for v in m.values()),
         "m": pad_models({f: [round(v[0], 2), v[1]] for f, v in m.items()}, fam)}
        for day, m in sorted(days.items())]
    first = con.execute("SELECT min(ts) FROM rows WHERE acc=?", (u,)).fetchone()[0]
    info["trackedSince"] = first
    tot = con.execute("SELECT coalesce(sum(cost),0), coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=?",
                      (u, now - 30 * 86400)).fetchone()
    info["usd30d"], info["tokens30d"] = round(tot[0], 2), tot[1]
    today0 = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    td = con.execute("SELECT coalesce(sum(cost),0), coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=?",
                     (u, today0)).fetchone()
    info["usdToday"], info["tokensToday"] = round(td[0], 2), td[1]
    # rolling last 24 hours (not the calendar day): what the menu bar shows
    h24 = con.execute("SELECT coalesce(sum(cost),0), coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=?",
                      (u, now - 86400)).fetchone()
    info["usd24h"], info["tokens24h"] = round(h24[0], 2), h24[1]
    wk = con.execute("SELECT coalesce(sum(cost),0), coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=?",
                     (u, now - 7 * 86400)).fetchone()
    info["usd7d"], info["tokens7d"] = round(wk[0], 2), wk[1]
    info["weeks"] = weekly_windows(con, sp, u, now, fam=fam, rate=rates.get((u, "7d")), k=k,
                                   cur7=info["windows"].get("7d"), drop=drop)

    # 7. subscription value in API dollars
    r7 = info["rates"].get("7d")
    if r7:
        week_opus = r7["usd_per_100"]
        v = {"weekOpusUsd": round(week_opus), "monthOpusUsd": round(week_opus * 30.44 / 7)}
        rf = info["rates"].get("fable")
        if rf:
            cap = rf["usd_per_100"]                       # Fable cap per week, $
            share = min(100.0, cap * k * r7["pct_per_usd"])  # how much of the shared limit it uses up, %
            v["weekFableCapUsd"] = round(cap)
            v["fableCapShareOfWeek"] = round(share, 1)
            v["weekMixUsd"] = round(cap + (100 - share) / r7["pct_per_usd"])
        v["weekAllFableUsd"] = round(week_opus / k)
        if info.get("planPriceUsd"):
            v["timesPrice"] = round(v["monthOpusUsd"] / info["planPriceUsd"], 1)
        info["value"] = v
    return info


def check():
    """Self-check of weekly_windows() on made-up data in memory; never touches usage.db or state.json."""
    import sqlite3
    ok = True

    def say(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("ok    " if cond else "FAIL  ") + what)

    con = sqlite3.connect(":memory:")
    con.executescript(core.SCHEMA)
    D, W, E = 86400, SPAN["7d"], 1796000000     # E: end of the last finished week
    n = 0

    def spend(ts, usd, model="claude-opus-5-5"):
        nonlocal n
        n += 1
        con.execute("INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,cost,acc) VALUES(?,?,?,0,1000,0,0,0,?,'a')",
                    (str(n), ts, model, usd))

    def read(ts, pct, ra):
        con.execute("INSERT INTO samples VALUES('a','7d',?,?,?,'poll')", (ts, pct, ra))

    spend(E - 3 * W + D, 100)                                   # A: no reading at all
    b = E - 2 * W                                               # B: read until 2 h before the end
    spend(b + 2 * D, 100)
    for dt, p in ((D, 10), (3 * D, 40), (W - 7200, 70)):
        read(b + dt, p, E - W)
    b = E - W                                                   # C: 60, hit 100, reset, 5, 30 at the end
    spend(b + D + 3600, 800)
    spend(b + 4 * D, 50)
    read(b + D, 60, E)
    con.execute("INSERT INTO hits(key,ts,kind,resets_at,acc) VALUES('h',?,'seven_day',?,'a')", (b + 2 * D, E))
    read(b + 3 * D, 5, E)
    read(E - 3600, 30, E)
    spend(E + D, 0.5)                                           # current week: 0.3 x $0.5 = 0.15 points
    read(E + D, 2, E + W)
    now = E + 2 * D
    wk = {w["end"]: w for w in weekly_windows(con, Spend(con, "a"), "a", now, rate=0.3, k=1.0)}
    a, bb, c, cur = wk[E - 2 * W], wk[E - W], wk[E], wk[E + W]
    say(a["peakPct"] is None and a["peakFinal"] is None and a["minePct"] == 30.0 and a["othersPct"] is None,
        "a week without a reading: peak and others null, mine from the dollars")
    say(bb["peakPct"] == 70 and bb["peakFinal"] and bb["minePct"] == 30.0 and bb["othersPct"] == 40.0
        and bb["resetAt"] is None, "a week read near its end: final peak 70 = mine 30 + others 40")
    say(c["resetAt"] == b + 2 * D + 1 and c["peakPct"] == 30 and c["peakFinal"] and c["minePct"] == 15.0
        and c["othersPct"] == 15.0,
        f"mid-week reset after a hit: measured from the reset, peak 30 = mine 15 + others 15 "
        f"(before: peak 100, mine 100, others 0) ({c['peakPct']}, {c['minePct']}, {c['othersPct']})")
    say(all(round(sum(w["minePctByModel"].values()), 1) == w["minePct"] for w in wk.values()),
        f"minePctByModel adds up to minePct, also at 0.15 ({cur['minePct']} = {cur['minePctByModel']})")
    say(set(cur["minePctByModel"]) == set(core.MODEL_KEYS) and cur["othersPct"] == round(2 - cur["minePct"], 1),
        "current week without windows.7d: all five models, others = peak - mine")
    cur7 = {"pct": 2.0, "mine": 0.1, "outside": 1.9, "rolled": False, "resetsAt": E + W, "start": E}
    w0 = weekly_windows(con, Spend(con, "a"), "a", now, rate=0.3, k=1.0, cur7=cur7)[0]
    say(w0["minePct"] == 0.1 and w0["othersPct"] == 1.9, "current week: mine and others are windows.7d's")

    # rolling 24 hours: 23 h ago counts, 25 h ago does not (even on the same calendar day); unattributed adds
    con.execute("DELETE FROM rows")
    for i, (ago, usd, acc) in enumerate(((23 * 3600, 2.0, "a"), (25 * 3600, 5.0, "a"), (3600, 0.25, None))):
        con.execute("INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,cost,acc) VALUES(?,?,?,10,100,0,0,5,?,?)",
                    (f"h{i}", now - ago, "claude-opus-5-5", usd, acc))
    saved_accounts = core.accounts
    core.accounts = lambda: [{"uuid": "a", "name": "a", "short": "a", "id": "a", "useHapp": False, "active": True,
                              "orgs": []}]
    try:
        st = analyse(con, now)
    finally:
        core.accounts = saved_accounts
    info = st["accounts"][0]
    say(info.get("usd24h") == 2.0 and info.get("tokens24h") == 115,
        f"usd24h/tokens24h: 23 h ago counted, 25 h ago left out ({info.get('usd24h')}, {info.get('tokens24h')})")
    say(st["claude24h"] == {"usd": 2.25, "tokens": 230, "unattributedUsd": 0.25},
        f"claude24h: every call of the last 24 h, also unattributed ({st['claude24h']})")
    con.execute("DELETE FROM rows")

    # one broken account or a broken cache step costs only itself (state.json is still written)
    mod = sys.modules[__name__]
    saved = (core.accounts, mod.account_state, core.cache_totals)
    core.accounts = lambda: [{"uuid": u, "name": u, "short": u, "id": u, "useHapp": u == "v", "active": u == "a",
                              "orgs": []} for u in ("a", "v")]
    try:
        def broken(con, a, *rest, **kw):
            if a["uuid"] == "v":
                raise ValueError("made up")
            return saved[1](con, a, *rest, **kw)
        mod.account_state = broken
        st = analyse(con, now)
        good, bad = st["accounts"]
        say("error" not in good and good["weeks"] and bad["error"].startswith("error: ValueError")
            and bad["refresh"] == "manual" and set(bad) >= {"uuid", "name", "short"},
            "one account failing: the other is complete, the failed one has uuid/name/short/error")
        mod.account_state = saved[1]
        core.cache_totals = lambda *a: 1 / 0
        st = analyse(con, now)
        say(st["cache"] is None and st["cacheError"] and all(a["cache"] is None and "weeks" in a for a in st["accounts"]),
            "cache step failing: cache null, accounts complete")
    finally:
        core.accounts, mod.account_state, core.cache_totals = saved
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    return ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    con = core.db()
    st = analyse(con)
    os.makedirs(core.DATA, exist_ok=True)
    tmp = f"{core.DATA}/state.json.tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, f"{core.DATA}/state.json")
    for a in st["accounts"]:
        print(a["short"], json.dumps({"rates": {k: round(v["usd_per_100"]) for k, v in a["rates"].items()},
                                      "windows": a["windows"], "value": a.get("value"), "drift": a["drift"]},
                                     ensure_ascii=False))
    print("k_fable", st["k_fable"], st["k_source"])
