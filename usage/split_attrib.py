"""Three-way attribution of quota readings and their unrounded interval increments."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import accumulate

import calib

G = 600
DROP_PTS = 4.0
WEEK_RESET_TOL = 3600
SESSION_RESET_TOL = 600
SATURATED = 99.5

GAP = 1
INCOMPLETE = 2
CALIBRATED = 4
BOUNDARY = 8
RESET_TAIL = 16

REASON_PRIORITY = ("bucket_boundary", "reset_boundary", "uncovered", "unpriced_calls",
                   "missing_baseline", "possible_local_activity", "rounding")


@dataclass
class Reading:
    start: int
    parts: tuple
    method: str
    reason: str | None


class Attribution:
    def __init__(self, rate, pairs):
        self.rated = bool(rate)
        self.preliminary = bool(rate and pairs < 2)
        self.readings = {}
        self.intervals = []
        self.segment = []

    def at(self, ts, pct):
        reading = self.readings.get(ts)
        y, o, _ = reading.parts if reading else (0.0, 0.0, pct)
        y = min(y, pct)
        o = min(o, pct - y)
        # Readings can sit temporarily above the settled curve. Keep the measured
        # percentage on the card and leave the excess unassigned.
        u = max(0.0, pct - y - o)
        parts = calib.split_points({"youPct": y, "othersPct": o, "unknownPct": u}, round(pct, 1))
        method = reading.method if reading else ("unknown" if pct else "idle")
        reason = reading.reason if reading else ("missing_baseline" if pct else None)
        if parts["unknownPct"]:
            if self.rated and method != "unknown":
                method = "mixed"
            elif not self.rated:
                method = "floor" if parts["othersPct"] else "unknown"
            reason = reason or "rounding"
        return {**parts, "method": method, "preliminary": self.preliminary and method in ("calibrated", "mixed"),
                "reason": reason}


class Spans:
    def __init__(self, starts, ends):
        self.starts = starts
        self.ends = ends
        self.max_end = list(accumulate(ends, max))

    def overlapping(self, lower, upper, *, include_lower=False):
        # Exact events use (lower, upper]. Buckets use [start, end) and
        # cannot contribute at the sample that starts the bucket.
        first = (bisect_left if include_lower else bisect_right)(self.max_end, lower)
        stop = bisect_right(self.starts, upper, lo=first)
        for i in range(first, stop):
            a, b = self.starts[i], self.ends[i]
            if a == b:
                if b > lower or (include_lower and b == lower):
                    yield i, a, b
            elif b > lower and a < upper:
                yield i, a, b

    def has(self, lower, upper, *, include_lower=False):
        return next(self.overlapping(lower, upper, include_lower=include_lower), None) is not None


class Activity:
    def __init__(self, calls, bucket_sec, complete, ambiguous, encrypted_mtimes, spend_key, coverage_holes=()):
        self.calls = sorted((e for e in calls if e.get("calls", 1)), key=lambda e: e["ts"])
        self.bucket_sec = bucket_sec
        self.complete = complete
        self.spend_key = spend_key
        self.local = self.spans(self.calls)
        self.ambiguous = self.spans(sorted((e for e in ambiguous if e.get("calls", 1)), key=lambda e: e["ts"]))
        self.encrypted = sorted(encrypted_mtimes)
        starts, ends = zip(*sorted(coverage_holes)) if coverage_holes else ((), ())
        self.holes = Spans(starts, ends)

    def end(self, ev):
        return ev["ts"] + ev.get("bucketSec", self.bucket_sec)

    def spans(self, events):
        starts, ends = [], []
        for e in events:
            starts.append(e["ts"])
            ends.append(self.end(e))
        return Spans(starts, ends)

    @staticmethod
    def straddles(a, b, sample, reset):
        return a < sample < b or (reset is not None and a < reset < b)

    def covered(self, lower, upper, include_lower, ambiguous):
        i = (bisect_left if include_lower else bisect_right)(self.encrypted, lower)
        recent_encrypted = i < len(self.encrypted) and self.encrypted[i] < upper + 60
        return (self.complete and not recent_encrypted and not ambiguous
                and not self.holes.has(lower, upper, include_lower=include_lower))


def samples_from(con, acc, win, now, since=0, with_hits=False):
    """Full readings in order, with one value per second and live readings winning ties."""
    points = {ts: (ts, pct, ra) for ts, pct, ra in con.execute(
        "SELECT ts,pct,resets_at FROM samples WHERE acc=? AND win=? AND ts BETWEEN ? AND ? "
        "AND source!='start' ORDER BY ts, CASE source WHEN 'live' THEN 2 WHEN 'poll' THEN 1 ELSE 0 END, source",
        (acc, win, since, now))}
    if with_hits:
        for ts, ra in con.execute(
            "SELECT ts,resets_at FROM hits WHERE acc=? AND kind='seven_day' AND ts BETWEEN ? AND ? ORDER BY ts",
            (acc, since, now)):
            points[ts] = (ts, 100.0, (ra + 30) // 60 * 60 if ra else (points.get(ts) or (None, None, None))[2])
    return [points[ts] for ts in sorted(points)]


def segments(samples, span, tolerance, drop, resets):
    """Return (readings, known start, preceding reading, inferred-reset tail)."""
    groups = []
    cur, start, prev, reset, tail = [], None, None, None, False
    for point in samples:
        ts, pct, ra = point
        guess = ra - span if pct > 0 and ra else None
        credit_idx = bisect_left(resets, ts) - 1
        credit = resets[credit_idx] if credit_idx >= 0 else None
        boundary = credit is not None and (credit >= cur[-1][0] if cur else guess is None or credit >= guess)
        changed = pct > 0 and reset and ra and abs(ra - reset) > tolerance
        dropped = cur and pct < cur[-1][1] - drop
        scheduled = cur and reset and cur[-1][0] < reset <= ts and (pct == 0 or not ra or ra != reset)
        if cur and (boundary or changed or dropped or scheduled):
            groups.append((cur, start, prev, tail))
            prev, cur, start, reset, tail = cur[-1][0], [], reset if scheduled else None, None, True
        if not cur and boundary:
            # A redemption after the preceding reading proves a reset. If
            # the new window began later, its own natural start bounds spend.
            start = max(credit, min(guess, ts)) if guess is not None else credit
        elif not reset and guess is not None and (prev is None or changed):
            # Rounded reset times can place a start slightly after its first reading.
            # A same-window drop only bounds the reset between readings.
            first = cur[0][0] if cur else ts
            if (prev is None or guess > prev) and guess <= first + tolerance:
                inferred = min(guess, first)
                start = inferred if start is None else max(start, inferred)
        if pct > 0 and ra:
            reset = ra
        cur.append(point)
    if cur:
        groups.append((cur, start, prev, tail))
    return groups


def attribute(samples, calls, win, idle_jitter, *, rate=None, pairs=0, resets=(), spend_key="usd",
              bucket_sec=0, complete=True, ambiguous=(), encrypted_mtimes=(), coverage_holes=(), drop=DROP_PTS, since=0):
    """Attribute each observed increase once. Spend can explain an increase measured
    across the call itself, or one whose interval starts within the call's grace period."""
    result = Attribution(rate, pairs)
    activity = Activity(calls, bucket_sec, complete, ambiguous, encrypted_mtimes, spend_key, coverage_holes)
    tolerance = WEEK_RESET_TOL if win == "7d" else SESSION_RESET_TOL
    for points, start, prev, tail in segments(samples, calib.SPAN[win], tolerance, drop, sorted(resets)):
        if start is not None and start < since:
            start = None
        _attribute_segment(result, points, start, prev, tail, activity, rate, idle_jitter)
    return result


def _attribute_segment(out, points, known_start, prev, tail, activity, rate, jitter):
    first = points[0][0]
    baseline = known_start is None and prev is None
    start = known_start if known_start is not None else (prev if prev is not None else first)
    smooth = [p[1] for p in points]
    for i in range(len(smooth) - 2, -1, -1):
        smooth[i] = min(smooth[i], smooth[i + 1])

    used = 0.0
    totals = [0.0, 0.0, 0.0]
    reasons = set()
    methods = set()
    out.segment = []
    last = start
    remaining = {}
    reserved = {}
    unknown_reset = first if known_start is None and prev is not None else None
    idle = known_start is not None
    for i, (ts, raw, _) in enumerate(points):
        lower = last - G
        if known_start is not None:
            lower = max(lower, known_start)
        include_lower = lower == known_start
        local = list(activity.local.overlapping(lower, ts, include_lower=include_lower))
        ambiguous = list(activity.ambiguous.overlapping(lower, ts, include_lower=include_lower))
        covered = activity.covered(lower, ts, include_lower, ambiguous)
        idle = idle and covered and not local
        bucket_boundary = (any(activity.straddles(a, b, ts, known_start)
                               for spans in (local, ambiguous) for _, a, b in spans)
                           or any(reserved.get(j, 0) > 0 for j, _, _ in local))
        unpriced = any(not activity.calls[j].get("priced", True) for j, _, _ in local)
        # For an inferred reset, only activity after its first reading can
        # supply certain budget. Recent activity before that remains ambiguous.
        reset_boundary = unknown_reset is not None and any(a <= unknown_reset for _, a, _ in local)

        delta = max(0.0, smooth[i] - used)
        y, o = 0.0, 0.0
        reason = None
        if baseline and i == 0:
            method, reason = "unknown", "missing_baseline"
        elif idle or (unknown_reset == ts and covered and not local):
            o, method = delta, "idle"
        elif rate:
            straddlers = []
            for j, a, b in local:
                if (a < start or (unknown_reset is not None and a <= unknown_reset)
                        or not activity.calls[j].get("priced", True)):
                    continue
                balance = remaining.get(j, rate * max(0.0, activity.calls[j].get(activity.spend_key) or 0.0))
                if a < ts < b:
                    straddlers.append((j, balance))
                    continue
                take = min(delta - y, max(0.0, balance))
                remaining[j] = balance - take
                y += take
            if delta > y:
                for j, balance in straddlers:
                    # Unknown growth could already have used this much of the
                    # bucket. After it closes, only the unreserved part is certain.
                    hold = min(delta - y, balance)
                    remaining[j] = balance - hold
                    reserved[j] = reserved.get(j, 0.0) + hold
            if covered and not (unpriced or bucket_boundary or reset_boundary):
                o, method = delta - y, "calibrated"
            else:
                method = "mixed" if y else "unknown"
        elif covered and not local:
            o = max(0.0, delta - jitter)
            method, reason = ("floor" if o else "unknown"), "rounding"
        else:
            method = "unknown"

        if reason is None and method in ("unknown", "mixed"):
            reason = ("bucket_boundary" if bucket_boundary else "reset_boundary" if reset_boundary else
                      "uncovered" if not covered else "unpriced_calls" if unpriced else "possible_local_activity")

        u = max(0.0, delta - y - o)
        if delta:
            methods.add(method)
            if u > 1e-9 and reason:
                reasons.add(reason)
        totals = [totals[0] + y, totals[1] + o, totals[2] + u]
        overall = method
        if methods:
            if methods == {"idle"}:
                overall = "idle"
            elif rate:
                overall = ("calibrated" if totals[2] <= 1e-9 else
                           "mixed" if totals[0] or totals[1] else "unknown")
            else:
                overall = "floor" if totals[1] else "unknown"
        overall_reason = next((r for r in REASON_PRIORITY if r in reasons), None)
        out.readings[ts] = Reading(start, tuple(totals), overall, overall_reason)
        out.segment.append((ts, raw))
        flags = (INCOMPLETE if not covered or unpriced else 0) | (CALIBRATED if rate else 0)
        if bucket_boundary and u > 1e-9:
            flags |= BOUNDARY
        if (i == 0 and tail) or raw >= SATURATED:
            flags |= RESET_TAIL
        if i == 0 and baseline:
            y, o, u = 0.0, 0.0, 0.0   # already used before observations began
            flags |= GAP
        out.intervals.append([last, ts, y, o, u, flags])
        last, used = ts, smooth[i]
