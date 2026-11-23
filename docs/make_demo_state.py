#!/usr/bin/env python3
"""Writes a made-up usage summary (`state.json`, schema 4) for the README screenshots.

Every number here is invented. Nothing is read from the collector's data folder, its database or the
Claude Code logs: the accounts, plans, spend, limit readings and the Antigravity block come from the fixed
profiles below and a seeded random generator, so the output is the same on every run and every Mac.

    python3 docs/make_demo_state.py docs/demo/state.json
    python3 docs/make_demo_state.py --antigravity-sample usage/antigravity-state-sample.json
    python3 docs/make_demo_state.py out.json --now 2026-11-19T15:40:00Z

Times are epoch seconds and days are UTC dates, so render with `TZ=UTC` (docs/make_screenshots.sh does):
then the day keys line up with the panel's local days and the clock times read as written here.

The shape follows usage/README.md (Claude accounts, weekly rows, cache) and usage/ANTIGRAVITY-STATE.md
(the `antigravity` block). What the panel shows:

- three Claude accounts: `personal` (Max 20x, active), `team` (Max 5x, shared with other people),
  `client` (Max 5x, VPN-only, read by hand: its last reading is 14 hours old);
- 5-hour, weekly and Fable weekly windows with series, forecasts and the "who used it" split;
- 180 days of spend by model (Opus, Fable, Sonnet, Haiku), weekly rows, value, drift, cache;
- an Antigravity block with quota windows, three Gemini models, rates, a confident value and cache.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
from pathlib import Path

SEED = 20261119
DEFAULT_NOW = "2026-11-19T15:40:00Z"

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY
WEEKS_PER_MONTH = 30.44 / 7

FAMILIES = ["opus", "fable", "sonnet", "haiku", "other"]
# Blended API dollars per million tokens of Claude Code traffic (mostly cache reads).
USD_PER_MTOK = {"opus": 0.45, "fable": 0.75, "sonnet": 0.26, "haiku": 0.09, "other": 0.30}
# Blended cost is about 0.189 x the input price at ~95 % cache reads, ~3.5 % cache writes (1.25x input) and
# ~1 % output (5x input); a cache read costs a tenth of the input price.
INPUT_PER_MTOK = {family: usd / 0.189 for family, usd in USD_PER_MTOK.items()}

# The three made-up Claude accounts, in Claude Switcher order (this order sets the chart colors).
ACCOUNTS = [
    {
        "n": 1, "short": "personal", "name": "Personal", "plan": "Max 20x", "price": 200,
        "active": True, "vpn": False, "every": 300,
        "rate7d": 4150.0, "rate5h": 540.0, "kFable": 1.62, "kSource": "pure", "fableCap": 0.6,
        "since": 176, "level": 330.0,
        "mix": {"opus": 0.762, "fable": 0.228, "sonnet": 0.005, "haiku": 0.004, "other": 0.001},
        "reset7d": 3 * DAY + 1 * HOUR + 20 * 60,   # from now: Sun 17:00
        "win5h": {"elapsed": 2 * HOUR + 10 * 60, "mine": 33.0, "outside": 5.0},
        "win7d": {"mine": 36.0, "server": {"claude_code": 41.0, "chat": 3.0, "cowork": 2.0, "other": 0.0}},
        "read_ago": 60.0,
        # Past weeks, newest first: (mine %, others %, peak read in the last hours?, reset inside?)
        "weeks": [(58, 8, True, None), (74, 26, True, None), (52, 4, False, None),
                  (45, 7, True, 3.2), (63, 5, True, None), (70, 3, True, None), (57, 9, True, None)],
        "drift": [3720, 3810, 3650, 3900, 3980, 4050, 3890, 4120, 4210, 4060, 4180, 4100, 4240, 4150, 4090, 4170],
        "cacheRead": 96.4,
    },
    {
        "n": 2, "short": "team", "name": "Team", "plan": "Max 5x", "price": 100,
        "active": False, "vpn": False, "every": 900,
        "rate7d": 1080.0, "rate5h": 141.0, "kFable": 1.58, "kSource": "fit", "fableCap": 0.6,
        "since": 151, "level": 34.0,
        "mix": {"opus": 0.66, "fable": 0.326, "sonnet": 0.008, "haiku": 0.005, "other": 0.001},
        "reset7d": 5 * DAY + 5 * HOUR + 20 * 60,    # Tue 21:00
        "win5h": {"elapsed": 1 * HOUR + 25 * 60, "mine": 4.0, "outside": 8.0},
        "win7d": {"mine": 12.0, "server": {"claude_code": 22.0, "chat": 6.0, "cowork": 3.0, "other": 0.0}},
        "read_ago": 7 * 60.0,
        "weeks": [(24, 41, True, None), (19, 33, True, None), (31, 52, True, None), (22, 28, False, None),
                  (27, 37, True, None), (17, 30, True, None), (25, 44, True, None)],
        "drift": [990, 1040, 1010, 1120, 1060, 1090, 1000, 1150, 1070, 1110, 1040, 1100, 1060, 1080],
        "cacheRead": 94.8,
    },
    {
        "n": 3, "short": "client", "name": "Client (VPN)", "plan": "Max 5x", "price": 100,
        "active": False, "vpn": True, "every": None,
        "rate7d": 1140.0, "rate5h": 150.0, "kFable": 1.62, "kSource": "pooled", "kFrom": "personal",
        "fableCap": 0.6, "since": 96, "level": 95.0,
        "mix": {"opus": 0.822, "fable": 0.171, "sonnet": 0.004, "haiku": 0.002, "other": 0.001},
        # Read by hand while it was the active account, 19 hours ago (last evening).
        "read_ago": 19 * HOUR,
        "reset7d": 1 * DAY + 17 * HOUR + 20 * 60,  # Sat 09:00
        "win5h": {"elapsed": 3 * HOUR + 40 * 60, "mine": 58.0, "outside": 6.0},
        "win7d": {"mine": 49.0, "server": {"claude_code": 52.0, "chat": 5.0, "cowork": 0.0, "other": 0.0}},
        "weeks": [(63, 4, True, None), (78, 3, True, None), (55, 6, False, None), (69, 2, True, None),
                  (47, 5, True, None)],
        "drift": [1060, 1180, 1110, 1020, 1150, 1090, 1200, 1130, 1140],
        "cacheRead": 95.6,
    },
]


def fake_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def r1(x: float) -> float:
    return round(x, 1)


def r2(x: float) -> float:
    return round(x, 2)


def day_key(stamp: float) -> str:
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%d")


def day_start(stamp: float) -> float:
    return math.floor(stamp / DAY) * DAY


def hour_weights() -> list[float]:
    """How a working day's use spreads over UTC hours: a bell between 06:00 and 23:00, uneven hour to hour."""
    rng = random.Random(SEED - 1)
    weights = []
    for hour in range(24):
        bell = math.exp(-((hour + 0.5 - 14.5) ** 2) / (2 * 3.6 ** 2)) if 6 <= hour <= 23 else 0.0
        weights.append(bell * rng.uniform(0.35, 1.65))
    total = sum(weights)
    return [w / total for w in weights]


HOURS = hour_weights()


def thin(points: list, most: int = 96) -> list:
    """At most `most` readings, keeping the first and the last, as the collector writes them."""
    if len(points) <= most:
        return points
    step = (len(points) - 1) / (most - 1)
    return [points[round(i * step)] for i in range(most)]


def share_of_day_before(stamp: float) -> float:
    """Part of a typical day's use that happens before this moment of that UTC day."""
    seconds = stamp - day_start(stamp)
    whole = int(seconds // HOUR)
    part = (seconds - whole * HOUR) / HOUR
    return sum(HOURS[:whole]) + (HOURS[whole] * part if whole < 24 else 0)


# MARK: - Claude accounts


class Account:
    def __init__(self, profile: dict, now: float, rng: random.Random):
        self.p = profile
        self.now = now
        self.rng = rng
        self.measured = now - profile["read_ago"]
        # The account read by hand was last used when it was read; the others are in use until now.
        self.cutoff = self.measured if profile["vpn"] else now
        self.today = day_start(now)
        self.first_day = self.today - 179 * DAY
        self.tracked = self.today - profile["since"] * DAY + 10 * HOUR + 17 * 60
        self.reset7d = now + profile["reset7d"]
        self.start7d = self.reset7d - WEEK
        # raw dollars per day and family
        self.days: dict[float, dict[str, float]] = {}
        self.generate_days()

    # Day spend --------------------------------------------------------------------------------------

    def weight(self, family: str) -> float:
        return self.p["kFable"] if family == "fable" else 1.0

    def weighted(self, spend: dict[str, float]) -> float:
        return sum(usd * self.weight(f) for f, usd in spend.items())

    def generate_days(self) -> None:
        """Raw dollars per UTC day and family; the last day holds only the use before the cutoff."""
        p = self.p
        day = self.first_day
        while day <= day_start(self.cutoff):
            if day + DAY <= self.tracked:
                day += DAY
                continue
            weekday = dt.datetime.fromtimestamp(day, dt.timezone.utc).weekday()
            age = (self.today - day) / DAY
            trend = 0.72 + 0.28 * (1 - age / 180)          # use grew over the half year
            busy = [1.1, 1.15, 1.05, 1.1, 0.95, 0.35, 0.25][weekday]
            if self.rng.random() < 0.07:                   # a day off now and then
                busy *= 0.05
            total = p["level"] * trend * busy * self.rng.uniform(0.6, 1.4)
            mix = {f: p["mix"][f] * self.rng.uniform(0.7, 1.3) for f in FAMILIES}
            norm = sum(mix.values())
            share = share_of_day_before(self.cutoff) if day == day_start(self.cutoff) else 1.0
            self.days[day] = {f: total * share * mix[f] / norm for f in FAMILIES}
            day += DAY

    def spend_between(self, start: float, end: float) -> dict[str, float]:
        """Raw dollars by family between two moments, spreading each day over its usual hours."""
        result = {f: 0.0 for f in FAMILIES}
        for day, spend in self.days.items():
            day_end = min(day + DAY, self.cutoff)
            if day_end <= start or day >= end:
                continue
            whole = share_of_day_before(day_end - 1e-6) if day_end < day + DAY else 1.0
            if whole <= 0:
                continue
            hi = min(end, day_end)
            lo = max(start, day)
            upper = 1.0 if hi >= day + DAY else share_of_day_before(hi)
            part = max(0.0, min(1.0, (upper - share_of_day_before(lo)) / whole))
            for f in FAMILIES:
                result[f] += spend[f] * part
        return result

    def scale_days(self, start: float, end: float, target_weighted: float, keep: tuple = ()) -> None:
        """Scales the days fully inside [start, end), except `keep`, so the weighted spend hits the target."""
        inside = [d for d in self.days if d >= start and d + DAY <= end and d not in keep]
        current = self.weighted(self.spend_between(start, end))
        movable = sum(self.weighted(self.days[d]) for d in inside)
        if not inside or movable <= 0:
            return
        factor = max(0.05, 1 + (target_weighted - current) / movable)
        for d in inside:
            self.days[d] = {f: usd * factor for f, usd in self.days[d].items()}

    # Windows ----------------------------------------------------------------------------------------

    def shape_current_windows(self) -> None:
        """Makes the day of the 5-hour window and the rest of this week match the profile's readings."""
        p = self.p
        at = self.measured
        start5 = at - p["win5h"]["elapsed"]
        day5 = day_start(at)
        assert start5 >= day5, "the 5-hour window must lie inside one UTC day"
        want5 = p["win5h"]["mine"] / 100 * p["rate5h"]
        want7 = p["win7d"]["mine"] / 100 * p["rate7d"]
        for _ in range(4):
            have5 = self.weighted(self.spend_between(start5, at))
            if have5 > 0:
                self.days[day5] = {f: usd * want5 / have5 for f, usd in self.days[day5].items()}
            self.scale_days(self.start7d, day5 + DAY, want7, keep=(day5,))

    def week_series(self, start: float, at: float, mine_end: float, total_end: float, step: float):
        """Readings of a window: [time, server %, this Mac %]; the server % is a whole number."""
        points = [[int(start), 0, 0.0]]
        total_spend = self.weighted(self.spend_between(start, at)) or 1
        t = start + step
        last_pct = 0
        while t < at:
            share = self.weighted(self.spend_between(start, t)) / total_spend
            outside = (total_end - mine_end) * ((t - start) / (at - start)) ** 1.3
            mine = mine_end * share
            pct = round(mine + outside)
            if pct != last_pct:
                points.append([int(t), pct, r2(mine)])
                last_pct = pct
            t += step
        points.append([int(at), round(total_end), r2(mine_end)])
        return thin(points)

    @staticmethod
    def forecast(pct: float, at: float, start: float, resets: float, mid: float, lo: float, hi: float,
                 checks: int, band: str) -> dict:
        """Straight pace from the window so far, scaled by how past windows went on (mid / lo / hi)."""
        elapsed = max(at - start, 60) / HOUR
        pace = pct / elapsed
        left = (resets - at) / HOUR

        def end(k: float) -> float:
            return min(100.0, pct + pace * k * left)

        def hit(k: float):
            if pace * k <= 0 or pct + pace * k * left < 100:
                return None
            return int(at + (100 - pct) / (pace * k) * HOUR)

        return {"at": int(at), "pct": r1(pct), "ratePerHour": r2(pace * mid), "endPct": r1(end(mid)),
                "endLo": r1(end(lo)), "endHi": r1(end(hi)), "hitAt": hit(mid), "hitEarly": hit(hi),
                "hitLate": hit(lo), "checks": checks, "bandSource": band}

    def usd3(self, spend: dict[str, float]) -> dict:
        return {"opus": r2(spend["opus"]), "fable": r2(spend["fable"]),
                "other": r2(spend["sonnet"] + spend["haiku"] + spend["other"])}

    def windows(self) -> dict:
        p = self.p
        at = self.measured
        fresh = not p["vpn"]
        w5 = p["win5h"]
        start5 = at - w5["elapsed"]
        reset5 = start5 + 5 * HOUR
        spend5 = self.spend_between(start5, at)
        mine5 = self.weighted(spend5) / p["rate5h"] * 100
        pct5 = round(mine5 + w5["outside"])
        five = {
            "pct": float(pct5), "measuredAt": int(at), "resetsAt": int(reset5), "source": "poll", "rolled": False,
            "start": int(start5), "usd": self.usd3(spend5), "mine": r2(mine5), "outside": r2(max(0, pct5 - mine5)),
            "sinceMeasure": 0.0,
            "series": self.week_series(start5, at, mine5, pct5, 10 * 60),
        }
        spend7 = self.spend_between(self.start7d, at)
        mine7 = self.weighted(spend7) / p["rate7d"] * 100
        server = p["win7d"]["server"]
        pct7 = sum(server.values())
        week = {
            "pct": float(pct7), "measuredAt": int(at), "resetsAt": int(self.reset7d), "source": "poll",
            "rolled": False, "start": int(self.start7d), "usd": self.usd3(spend7), "mine": r2(mine7),
            "outside": r2(max(0, pct7 - mine7)), "sinceMeasure": 0.0, "server": server,
            "series": self.week_series(self.start7d, at, mine7, pct7, HOUR),
        }
        fable_rate = self.fable_rate
        fable_mine = spend7["fable"] / fable_rate * 100
        fable_pct = round(fable_mine + (pct7 - mine7) * 0.25)
        fable = {
            "pct": float(fable_pct), "measuredAt": int(at), "resetsAt": int(self.reset7d), "source": "poll",
            "rolled": False, "start": int(self.start7d), "usd": self.usd3(spend7), "mine": r2(fable_mine),
            "outside": r2(max(0, fable_pct - fable_mine)), "sinceMeasure": 0.0,
            "series": self.week_series(self.start7d, at, fable_mine, fable_pct, 3 * HOUR),
        }
        if fresh:
            band = "own" if p["short"] == "personal" else "pooled"
            five["forecast"] = self.forecast(pct5, at, start5, reset5, 0.42, 0.2, 1.45,
                                             212 if band == "own" else 640, band)
            week["forecast"] = self.forecast(pct7, at, self.start7d, self.reset7d, 0.86, 0.6, 1.3,
                                             41 if band == "own" else 118, band)
            fable["forecast"] = self.forecast(fable_pct, at, self.start7d, self.reset7d, 0.86, 0.6, 1.3,
                                              41 if band == "own" else 118, band)
        return {"5h": five, "7d": week, "fable": fable}

    @property
    def fable_rate(self) -> float:
        """Fable dollars that fill the Fable weekly cap."""
        return self.p["rate7d"] * self.p["fableCap"] / self.p["kFable"]

    # Weeks, drift, value ----------------------------------------------------------------------------

    def week_bounds(self, count: int) -> list[tuple[float, float]]:
        """Newest first: the current window, then `count` past ones."""
        return [(self.start7d - i * WEEK, self.reset7d - i * WEEK) for i in range(count + 1)]

    def shape_past_weeks(self) -> None:
        for (start, end), (mine, _others, _final, reset_day) in zip(self.week_bounds(len(self.p["weeks"]))[1:],
                                                                    self.p["weeks"]):
            target = mine / 100 * self.p["rate7d"]
            if reset_day is not None:
                # A reset inside the week: the percentages count from it, the dollars stay the whole week.
                reset_at = start + reset_day * DAY
                before = self.weighted(self.spend_between(start, reset_at))
                target += before
            self.scale_days(start, end, target)

    def models_of(self, spend: dict[str, float]) -> dict:
        return {f: [r2(spend[f]), int(spend[f] / USD_PER_MTOK[f] * 1e6)] for f in FAMILIES}

    def weeks(self) -> list[dict]:
        p = self.p
        rows = []
        bounds = self.week_bounds(len(p["weeks"]))
        win = self.windows_cache
        for index, (start, end) in enumerate(bounds):
            spend = self.spend_between(start, min(end, self.measured))
            models = self.models_of(spend)
            row = {"start": int(start), "end": int(end), "current": index == 0, "usd": self.usd3(spend),
                   "tokens": sum(v[1] for v in models.values()), "m": models}
            if index == 0:
                mine = win["7d"]["mine"]
                peak = win["7d"]["pct"]
                row.update({"peakPct": peak, "peakAt": win["7d"]["measuredAt"], "peakFinal": False,
                            "minePct": mine, "othersPct": r2(max(0, peak - mine)), "resetAt": None})
                part = spend
            else:
                mine_target, others, final, reset_day = p["weeks"][index - 1]
                reset_at = start + reset_day * DAY if reset_day is not None else None
                part = self.spend_between(reset_at or start, end)
                mine = self.weighted(part) / p["rate7d"] * 100
                peak = min(100.0, round(mine + others))
                if peak >= 100:
                    mine = min(mine, 100.0)
                peak_at = end - (2 * HOUR if final else 2 * DAY + 5 * HOUR)
                row.update({"peakPct": float(peak), "peakAt": int(peak_at), "peakFinal": bool(final or peak >= 100),
                            "minePct": r2(min(mine, peak)), "othersPct": r2(max(0, peak - mine)),
                            "resetAt": int(reset_at) if reset_at else None})
            # Parts by model in points of the limit; the rounded parts add up to minePct.
            weights = {f: part[f] * self.weight(f) for f in FAMILIES}
            total = sum(weights.values()) or 1
            parts = {f: r2(row["minePct"] * w / total) for f, w in weights.items()}
            parts["opus"] = r2(row["minePct"] - sum(v for f, v in parts.items() if f != "opus"))
            row["minePctByModel"] = parts
            rows.append(row)
        return rows

    def drift(self) -> list[dict]:
        points = []
        values = self.p["drift"]
        for back, usd in enumerate(reversed(values), start=1):
            start = self.start7d - back * WEEK
            end = start + WEEK
            spend = self.spend_between(start, end)
            points.append({"weekEnd": int(end), "usd_per_100": float(usd), "pairs": 160 + (back * 37) % 420,
                           "spend": self.usd3(spend)})
        return list(reversed(points))

    def value(self) -> dict:
        p = self.p
        week = p["rate7d"]
        month = week * WEEKS_PER_MONTH
        recent = self.spend_between(self.now - 30 * DAY, self.now)
        raw = sum(recent.values())
        mix = week * raw / self.weighted(recent) if raw else week
        return {"weekOpusUsd": round(week), "monthOpusUsd": round(month),
                "weekFableCapUsd": round(self.fable_rate), "fableCapShareOfWeek": p["fableCap"],
                "weekMixUsd": round(mix), "weekAllFableUsd": round(week / p["kFable"]),
                "timesPrice": r1(month / p["price"])}

    # Totals and cache -------------------------------------------------------------------------------

    def daily(self) -> list[dict]:
        rows = []
        for day in sorted(self.days):
            spend = self.days[day]
            if sum(spend.values()) < 0.005:
                continue
            models = self.models_of(spend)
            rows.append({"day": day_key(day), "opus": r2(spend["opus"]), "fable": r2(spend["fable"]),
                         "other": r2(spend["sonnet"] + spend["haiku"] + spend["other"]),
                         "tokens": sum(v[1] for v in models.values()), "m": models})
        return rows

    def span(self, start: float) -> dict[str, float]:
        return self.spend_between(start, self.now)

    def cache_span(self, start: float, read_pct: float) -> dict | None:
        spend = self.span(start)
        tokens = {f: spend[f] / USD_PER_MTOK[f] * 1e6 for f in FAMILIES}
        prompt = sum(tokens.values()) * 0.985
        if prompt <= 0:
            return None
        read = prompt * read_pct / 100
        miss = prompt - read
        saved = sum(tokens[f] / sum(tokens.values()) * read * 0.9 * INPUT_PER_MTOK[f] / 1e6 for f in FAMILIES)
        missed = sum(tokens[f] / sum(tokens.values()) * miss * 1.15 * INPUT_PER_MTOK[f] / 1e6 for f in FAMILIES)
        return {"readPct": r1(read_pct), "missPct": r1(100 - read_pct), "calls": int(prompt / 255_000) or 1,
                "readTokens": int(read), "missTokens": int(miss), "writeTokens": int(miss * 0.93),
                "savedUsd": r2(saved), "missUsd": r2(missed)}

    def cache(self) -> dict:
        read = self.p["cacheRead"]
        spans = {"today": (self.today, read + 0.9), "7d": (self.now - 7 * DAY, read),
                 "30d": (self.now - 30 * DAY, read - 0.7)}
        result = {}
        for key, (start, pct) in spans.items():
            span = self.cache_span(start, min(99.2, pct))
            if span:
                result[key] = span
        return result

    def build(self) -> dict:
        p = self.p
        self.shape_past_weeks()
        self.shape_current_windows()
        self.windows_cache = self.windows()
        totals = {}
        # Calendar days up to today, as the Spend page counts them.
        for key, start in (("Today", self.today), ("7d", self.today - 6 * DAY), ("30d", self.today - 29 * DAY)):
            spend = self.span(start)
            totals["usd" + key] = r2(sum(spend.values()))
            totals["tokens" + key] = int(sum(spend[f] / USD_PER_MTOK[f] * 1e6 for f in FAMILIES))
        account = {
            "uuid": fake_uuid(p["n"]), "name": p["name"], "short": p["short"], "active": p["active"],
            "useHapp": p["vpn"], "planName": p["plan"], "planPriceUsd": p["price"], "has5h": True,
            "fableShareOfWeek": int(p["fableCap"] * 100), "kFable": p["kFable"], "kSource": p["kSource"],
            "refresh": "manual" if p["vpn"] else "background", "refreshEverySec": p["every"],
            "windows": self.windows_cache,
            "rates": {
                "5h": {"pct_per_usd": round(100 / p["rate5h"], 4), "usd_per_100": p["rate5h"], "pairs": 96,
                       "source": "own"},
                "7d": {"pct_per_usd": round(100 / p["rate7d"], 4), "usd_per_100": p["rate7d"], "pairs": 410,
                       "source": "own"},
                "fable": {"pct_per_usd": round(100 / self.fable_rate, 4), "usd_per_100": r1(self.fable_rate),
                          "pairs": 130, "source": "own"},
            },
            "drift": self.drift(),
            "lastPoll": {"ts": int(self.measured), "status": "ok"},
            "lastMeasuredAt": int(self.measured),
            "weeks": self.weeks(),
            "daily": self.daily(),
            **totals,
            "trackedSince": int(self.tracked),
            "value": self.value(),
            "cache": self.cache(),
        }
        if "kFrom" in p:
            account["kFrom"] = p["kFrom"]
        return account


def add_cache(spans: list[dict]) -> dict:
    """The top-level cache: every account together plus a few calls not attributed to one."""
    result = {}
    for key in ("today", "7d", "30d"):
        parts = [s[key] for s in spans if key in s]
        read = sum(p["readTokens"] for p in parts) * 1.004
        miss = sum(p["missTokens"] for p in parts) * 1.004
        result[key] = {
            "readPct": r1(100 * read / (read + miss)), "missPct": r1(100 * miss / (read + miss)),
            "calls": int(sum(p["calls"] for p in parts) * 1.004), "readTokens": int(read), "missTokens": int(miss),
            "writeTokens": int(sum(p["writeTokens"] for p in parts) * 1.004),
            "savedUsd": r2(sum(p["savedUsd"] for p in parts) * 1.004),
            "missUsd": r2(sum(p["missUsd"] for p in parts) * 1.004),
        }
    return result


# MARK: - Antigravity

GEMINI = {
    # id: (blended $ per Mtok, share of the dollars, input price per Mtok)
    "gemini-4-neon": (0.46, 0.83, 2.0),
    "gemini-4-carbon": (0.62, 0.12, 3.0),
    "gemini-3.8-flash": (0.11, 0.05, 0.4),
}


def antigravity(now: float, rng: random.Random) -> dict:
    today = day_start(now)
    tracked = today - 92 * DAY + 13 * HOUR + 5 * 60
    measured = now - 4 * 60
    rate7 = 540.0
    per_week = 6.4
    rate5 = round(rate7 / per_week, 1)
    reset7 = now + 3 * DAY + 6 * HOUR + 20 * 60      # Sun 22:00
    start7 = reset7 - WEEK
    start5 = now - 1 * HOUR - 50 * 60
    reset5 = start5 + 5 * HOUR

    # Daily spend by model.
    days: dict[float, dict[str, float]] = {}
    day = today - 179 * DAY
    while day <= today:
        if day + DAY > tracked:
            weekday = dt.datetime.fromtimestamp(day, dt.timezone.utc).weekday()
            age = (today - day) / DAY
            busy = [1.1, 1.0, 1.15, 1.05, 0.9, 0.3, 0.2][weekday]
            if rng.random() < 0.1:
                busy *= 0.05
            total = 44 * (0.55 + 0.45 * (1 - age / 92)) * busy * rng.uniform(0.6, 1.4)
            mix = {m: share * rng.uniform(0.6, 1.4) for m, (_, share, _) in GEMINI.items()}
            norm = sum(mix.values())
            so_far = share_of_day_before(now) if day == today else 1.0    # today: only the use until now
            days[day] = {m: total * so_far * v / norm for m, v in mix.items()}
        day += DAY

    def between(lo: float, hi: float) -> dict[str, float]:
        out = {m: 0.0 for m in GEMINI}
        for d, spend in days.items():
            if d + DAY <= lo or d >= hi:
                continue
            whole = share_of_day_before(now) if d == today else 1.0
            upper = 1.0 if hi >= d + DAY else share_of_day_before(min(hi, now) if d == today else hi)
            part = (upper - share_of_day_before(max(lo, d))) / whole if whole > 0 else 0.0
            for m in GEMINI:
                out[m] += spend[m] * max(0.0, min(1.0, part))
        return out

    def scale(lo: float, hi: float, want: float) -> None:
        inside = [d for d in days if d >= lo and d + DAY <= hi]
        have = sum(between(lo, hi).values())
        movable = sum(sum(days[d].values()) for d in inside)
        if inside and movable > 0:
            factor = max(0.05, 1 + (want - have) / movable)
            for d in inside:
                days[d] = {m: v * factor for m, v in days[d].items()}

    # Past weeks: (peak %, read in the last hours?)
    past = [(71.0, True), (64.5, True), (78.0, True), (52.0, False), (58.5, True), (43.0, True)]
    for back, (peak, _) in enumerate(past, start=1):
        scale(start7 - back * WEEK, reset7 - back * WEEK, (peak - 1.2) / 100 * rate7)
    # This week: 47 % at the reading, of it ~1 point from the chat app; the 5-hour window 34 %.
    pct7, pct5 = 47.0, 34.0
    want7 = (pct7 - 1.0) / 100 * rate7
    want5 = pct5 / 100 * rate5
    for _ in range(4):
        have5 = sum(between(start5, measured).values())
        if have5 > 0:
            days[today] = {m: v * want5 / have5 for m, v in days[today].items()}
        inside = [d for d in days if d >= start7 and d + DAY <= today]
        have7 = sum(between(start7, measured).values())
        movable = sum(sum(days[d].values()) for d in inside)
        if inside and movable > 0:
            factor = max(0.05, 1 + (want7 - have7) / movable)
            for d in inside:
                days[d] = {m: v * factor for m, v in days[d].items()}

    def tokens(model: str, usd: float) -> int:
        return int(usd / GEMINI[model][0] * 1e6)

    def models_of(spend: dict[str, float]) -> dict:
        return {m: [r2(v), tokens(m, v)] for m, v in sorted(spend.items(), key=lambda kv: -kv[1]) if v > 0.004}

    def usd3(spend: dict[str, float]) -> dict:
        return {"opus": 0.0, "fable": 0.0, "other": r2(sum(spend.values()))}

    def series(lo: float, at: float, pct: float, step: float) -> list:
        total = sum(between(lo, at).values()) or 1
        points = [[int(lo), 0.0, None]]
        last = 0.0
        t = lo + step
        while t < at:
            value = round(pct * sum(between(lo, t).values()) / total, 1)
            if value - last >= 0.3:
                points.append([int(t), value, None])
                last = value
            t += step
        points.append([int(at), pct, None])
        return thin(points)

    spend5 = between(start5, measured)
    spend7 = between(start7, measured)
    five = {"pct": pct5, "measuredAt": int(measured), "resetsAt": int(reset5), "source": "live", "rolled": False,
            "start": int(start5), "usd": usd3(spend5), "sinceMeasure": 0.2,
            "series": series(start5, measured, pct5, 10 * 60),
            "forecast": Account.forecast(pct5, measured, start5, reset5, 0.42, 0.2, 1.3, 48, "own")}
    week = {"pct": pct7, "measuredAt": int(measured), "resetsAt": int(reset7), "source": "live", "rolled": False,
            "start": int(start7), "usd": usd3(spend7), "sinceMeasure": 0.1,
            "series": series(start7, measured, pct7, HOUR),
            "forecast": Account.forecast(pct7, measured, start7, reset7, 0.86, 0.6, 1.3, 12, "default"),
            "sessions": {"perWeek": per_week, "pairs": 6}}

    daily = []
    for d in sorted(days):
        spend = days[d]
        if sum(spend.values()) < 0.005:
            continue
        m = models_of(spend)
        daily.append({"day": day_key(d), "opus": 0, "fable": 0, "other": r2(sum(spend.values())),
                      "tokens": sum(v[1] for v in m.values()), "m": m})

    weeks = []
    for back in range(len(past) + 1):
        lo, hi = start7 - back * WEEK, reset7 - back * WEEK
        spend = between(lo, min(hi, measured))
        m = models_of(spend)
        row = {"start": int(lo), "end": int(hi), "current": back == 0, "usd": usd3(spend),
               "tokens": sum(v[1] for v in m.values())}
        if back == 0:
            row.update({"peakPct": pct7, "peakAt": int(measured), "peakFinal": False, "resetAt": None})
        else:
            peak, final = past[back - 1]
            row.update({"peakPct": peak, "peakAt": int(hi - (3 * HOUR if final else 2 * DAY)),
                        "peakFinal": final, "resetAt": None})
        row["m"] = m
        weeks.append(row)

    def totals(lo: float) -> tuple[float, int]:
        spend = between(lo, now)
        return r2(sum(spend.values())), sum(tokens(m, v) for m, v in spend.items())

    usd_today, tok_today = totals(today)
    usd7, tok7 = totals(today - 6 * DAY)
    usd30, tok30 = totals(today - 29 * DAY)

    # Models of the last 30 days and the cache.
    spend30 = between(today - 29 * DAY, now)
    models = {}
    miss_pct = {"gemini-4-neon": 5.4, "gemini-4-carbon": 8.1, "gemini-3.8-flash": 12.6}
    efforts = {"gemini-4-neon": {"High": 0.97, "Medium": 0.03}, "gemini-4-carbon": {"High": 1.0},
               "gemini-3.8-flash": {"High": 0.2, "Medium": 0.8}}
    for model in sorted(spend30, key=lambda k: -spend30[k]):
        usd = spend30[model]
        tok = tokens(model, usd)
        prompt = tok * 0.988
        calls = int(prompt / 165_000) or 1
        miss = prompt * miss_pct[model] / 100
        output = tok - prompt
        models[model] = {"calls": calls, "input": int(miss), "cached": int(prompt - miss), "cacheWrite": 0,
                         "output": int(output * 0.7), "thinking": int(output * 0.3), "usd": r2(usd),
                         "missPct": miss_pct[model],
                         "efforts": {k: round(calls * v) for k, v in efforts[model].items()},
                         "priced": True, "priceConfirmed": True, "unpricedCalls": 0}

    def cache_span(lo: float, shift: float) -> dict:
        spend = between(lo, now)
        read = miss = saved = missed = calls = cold = 0.0
        for model, usd in spend.items():
            prompt = tokens(model, usd) * 0.988
            mp = miss_pct[model] + shift
            r = prompt * (1 - mp / 100)
            m = prompt - r
            price = GEMINI[model][2] * 0.9 / 1e6     # input price minus the cache-read price
            read, miss, saved, missed = read + r, miss + m, saved + r * price, missed + m * price
            calls += prompt / 165_000
        cold = 2.6 + shift
        pct = 100 * read / (read + miss) if read + miss else 0
        return {"readPct": r1(pct), "missPct": r1(100 - pct), "coldCallsPct": r1(cold), "calls": int(calls),
                "readTokens": int(read), "missTokens": int(miss), "writeTokens": 0,
                "savedUsd": r2(saved), "missUsd": r2(missed)}

    cache = {"today": cache_span(today, -0.4), "7d": cache_span(now - 7 * DAY, 0.0),
             "30d": cache_span(now - 30 * DAY, 0.3)}

    plan, price = "Google AI Ultra 5x", 99.99
    month = round(rate7 * WEEKS_PER_MONTH)
    by_model = [
        {"model": "gemini-4-neon", "label": "Neon", "monthUsd": round(month * 0.97),
         "timesPrice": r1(month * 0.97 / price), "pairs": 7},
        {"model": "gemini-4-carbon", "label": "Carbon", "monthUsd": round(month * 1.42),
         "timesPrice": r1(month * 1.42 / price), "pairs": 2},
        {"model": "gemini-3.8-flash", "label": "Flash", "monthUsd": round(month * 0.27),
         "timesPrice": r1(month * 0.27 / price), "pairs": 2},
    ]
    stretch_usd = 640.0
    samples5 = {"count": 1180, "first": int(tracked + 2 * HOUR), "last": int(measured), "today": 31}
    samples7 = {"count": 1012, "first": int(tracked + 2 * HOUR), "last": int(measured), "today": 31}
    return {
        "uuid": "antigravity", "name": "Antigravity", "short": "Antigravity", "active": False, "useHapp": False,
        "windows": {"5h": five, "7d": week},
        "rates": {
            "7d": {"pct_per_usd": round(100 / rate7, 4), "usd_per_100": rate7, "pairs": 9, "source": "own",
                   "method": "From stretches with $60+ of this Mac's Gemini spend between two readings; a stretch "
                             "that moved the limit more than that spend explains (phone, chat app), or much less "
                             "than the others agree on, is left out.",
                   "pairsUsed": 9, "pairsDropped": 2, "pairsLow": 0, "range": [506, 583], "usd": stretch_usd},
            "5h": {"pct_per_usd": round(100 / rate5, 4), "usd_per_100": rate5, "pairs": 11, "source": "own",
                   "method": "From stretches with $15+ of this Mac's Gemini spend between two readings; a stretch "
                             "that moved the limit more than that spend explains (phone, chat app), or much less "
                             "than the others agree on, is left out.",
                   "pairsUsed": 11, "pairsDropped": 3, "pairsLow": 1, "range": [79, 92], "usd": 402.5},
        },
        "drift": [],
        "kFable": 1.0,
        "planName": plan, "planPriceUsd": price,
        "has5h": True,
        "lastMeasuredAt": int(measured),
        "lastPoll": None,
        "daily": daily,
        "trackedSince": int(tracked),
        "usd30d": usd30, "tokens30d": tok30, "usdToday": usd_today, "tokensToday": tok_today,
        "usd7d": usd7, "tokens7d": tok7,
        "weeks": weeks,
        "value": {"planName": plan, "planPriceUsd": price, "weekUsd": round(rate7), "monthUsd": month,
                  "timesPrice": r1(month / price), "pairs": 9, "confident": True, "reason": None,
                  "byModel": by_model, "weekOpusUsd": round(rate7), "monthOpusUsd": month},
        "sessionPctOfWeek": r1(100 / per_week),
        "beta": True,
        "models": models,
        "valueReason": None,
        "calibration": {
            "stretches": {"7d": 11, "5h": 15}, "sessionIntervals": 6, "sessionPerWeekSoFar": per_week,
            "thresholds": {"jitterPts": 2.0, "stretchUsd": {"5h": 15.0, "7d": 60.0}, "confidentStretches": 4,
                           "confidentUsd": 240.0, "sessionMinPts": {"5h": 5.0, "7d": 3.0}, "sessionPairs": 3,
                           "sessionSum7dPts": 12.0},
            "planFile": "~/Library/Application Support/ClaudeSwitcher/usage/antigravity-plan.json",
            "planFileExists": True,
        },
        "cache": cache,
        "coverage": {
            "tokens": "exact", "usd": "estimate", "historySince": day_key(tracked), "dbFiles": 148,
            "calls": sum(m["calls"] for m in models.values()) * 3, "copiesDropped": 512,
            "encryptedConversations": 37, "timeEstimatedCalls": 0, "outputCheckFailed": 0, "unreadableDbs": 0,
            "malformedRows": 0, "unpricedCalls30d": 0, "unconfirmedPrices": [], "pricesChecked": day_key(now - DAY),
            "notCounted": "encrypted .pb conversations, deleted conversations, Tab completions, other devices",
            "scanCached": True, "scanRescannedFiles": 2, "scanMs": 11,
        },
        "quotaSource": {
            "app": "CodexBar",
            "file": "~/Library/Application Support/com.steipete.codexbar/history/antigravity.json",
            "fileModifiedAt": int(now - 20 * 60), "lastSampleAt": int(measured), "ageSec": int(now - 60 - measured),
            "cadence": "CodexBar keeps at most the peak sample of each clock hour",
            "weekly": "the weekly quota of Antigravity's Gemini model group (older CodexBar history may hold the "
                      "highest weekly quota of any group)",
            "accountsInFile": 1,
            "bySource": {"codexbar": {"5h": {"count": 640, "first": samples5["first"], "last": int(now - 40 * 60)},
                                      "7d": {"count": 520, "first": samples7["first"], "last": int(now - 40 * 60)}},
                         "live": {"5h": {"count": 540, "first": int(now - 34 * DAY), "last": int(measured)},
                                  "7d": {"count": 492, "first": int(now - 34 * DAY), "last": int(measured)}}},
            "lastSource": "live",
            "liveFile": "~/Library/Application Support/ClaudeSwitcher/usage/antigravity-quota.jsonl",
            "liveFileExists": True, "liveBadLines": 0, "liveCompactedAt": None,
            "samples": {"5h": samples5, "7d": samples7},
        },
    }


# MARK: - Summary


def build(now: float) -> dict:
    rng = random.Random(SEED)
    accounts = [Account(profile, now, rng).build() for profile in ACCOUNTS]
    block = antigravity(now, random.Random(SEED + 1))
    return {
        "schema": 4,
        "generatedAt": int(now - 60),
        "k_fable": 1.62,
        "k_source": "personal",
        "k_pairs": 410,
        "vpnUp": False,
        "accounts": accounts,
        "cache": add_cache([a["cache"] for a in accounts]),
        "antigravity": block,
    }


def parse_now(text: str) -> float:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", nargs="?", help="where to write the whole summary (state.json)")
    parser.add_argument("--antigravity-sample", metavar="PATH",
                        help="also write the antigravity block alone (usage/antigravity-state-sample.json)")
    parser.add_argument("--now", default=DEFAULT_NOW, help=f"the summary's moment, ISO 8601 (default {DEFAULT_NOW})")
    parser.add_argument("--print-now", action="store_true", help="print the moment as epoch seconds and exit")
    args = parser.parse_args()
    now = parse_now(args.now)
    if args.print_now:
        print(int(now))
        return
    if not args.out and not args.antigravity_sample:
        parser.error("give an output path, --antigravity-sample, or both")
    state = build(now)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
    if args.antigravity_sample:
        Path(args.antigravity_sample).write_text(json.dumps(state["antigravity"], indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
