"""Overview of local spend and measured weekly quota increases on fixed rolling grids."""
from __future__ import annotations

from collections import defaultdict
import math

import calib
import core
import scan
import split_attrib as sa

PROVIDERS = ("claude", "codex", "antigravity")
RANGES = {"24h": (86400, 900), "72h": (3 * 86400, 1800), "7d": (7 * 86400, 3600)}
KEEP = 8 * 86400
MAX_GAP = 7200
CACHE_MAX_AGE = 15 * 60


def _spend_rows(rows, now):
    buckets = defaultdict(lambda: [0.0, 0])
    for ts, usd, tokens in rows:
        if now - KEEP <= ts <= now:
            part = buckets[int(ts)]
            part[0] += usd or 0.0
            part[1] += tokens
    return [[ts, usd, tokens] for ts, (usd, tokens) in sorted(buckets.items())]


def event_spend(events):
    return ((e["ts"], e["usd"], e["input"] + e["cacheRead"] + e["cacheWrite"] + e["output"]) for e in events)


def account_input(provider, info, intervals, spend, now):
    rate = (info.get("rates", {}).get("7d") or {}).get("usd_per_100")
    through = intervals[-1][1] if intervals else None
    return {
        "meta": {"key": f"{provider}:{info['uuid']}", "provider": provider, "uuid": info["uuid"],
                 "short": info.get("short"), "planName": info.get("planName"), "active": bool(info.get("active")),
                 "rated": bool(rate and rate > 0), "usdPer100": rate if rate and rate > 0 else None,
                 "quotaThrough": through},
        "quota": [[a, b, round(y, 8), round(o, 8), round(u, 8), flags]
                  for a, b, y, o, u, flags in intervals if b >= now - KEEP],
        "spend": _spend_rows(spend, now),
    }


def provider_input(now, accounts, unattributed=(), *, partial=False):
    return {"builtAt": now, "partial": bool(partial), "accounts": accounts,
            "unattributed": _spend_rows(unattributed, now)}


def claude_input(con, accounts, now, complete=True):
    """Read Claude's full samples, weekly limit hits, and individual log costs from usage.db."""
    since = now - KEEP - calib.SPAN["7d"] - sa.G
    holes = [(a, b) for a, b in core.kv_get(con, scan.INCOMPLETE_KEY, []) if a <= now and b >= since]
    ids = {a["uuid"] for a in accounts}
    local, quota_calls = defaultdict(list), defaultdict(list)
    ambiguous = []
    for ts, acc, model, cost, tok in con.execute(
        "SELECT ts,acc,model,cost,coalesce(inp,0)+coalesce(out,0)+coalesce(cw5,0)+coalesce(cw1h,0)+coalesce(cr,0) "
        "FROM rows WHERE ts BETWEEN ? AND ? ORDER BY ts", (since, now)):
        local[acc if acc in ids else None].append((ts, cost, tok))
        if acc in ids:
            quota_calls[acc].append((ts, model, cost))
        elif acc in (None, "unknown") or acc.startswith("cli:"):
            ambiguous.append({"ts": ts})
    result = []
    for a in accounts:
        uid = a["uuid"]
        k = a.get("kFable") or 1.0
        rate = (a.get("rates", {}).get("7d") or {})
        calls = [{"ts": ts, "usd": None if usd is None else usd * (k if core.family(model) == "fable" else 1),
                  "priced": usd is not None} for ts, model, usd in quota_calls[uid]]
        pts = sa.samples_from(con, uid, "7d", now, since, with_hits=True)
        attr = sa.attribute(pts, calls, "7d", 1.0, rate=rate.get("pct_per_usd"), pairs=rate.get("pairs", 0),
                            ambiguous=ambiguous, complete=complete, coverage_holes=holes, drop=calib.RESET_DROP)
        result.append(account_input("claude", a, attr.intervals, local[uid], now))
    return provider_input(now, result, local[None],
                          partial=not complete or bool(holes) or any(a.get("error") for a in accounts))


def _spend_curve(rows, start, grid):
    dollars, tokens = 0.0, 0
    result = [0.0]
    i = 0
    for t in grid[1:]:
        while i < len(rows) and rows[i][0] <= t:
            ts, usd, tok = rows[i]
            if ts >= start:
                dollars += usd
                tokens += tok
            i += 1
        result.append(dollars)
    return result, tokens


def _quota_curve(rows, start, grid, partial):
    result = [[0.0, 0.0, 0.0, 0]]
    you = others = unknown = 0.0
    last = None
    last_flags = 0
    boundary = 0
    i = 0
    for t in grid[1:]:
        flags = sa.INCOMPLETE if partial else 0
        while i < len(rows) and rows[i][1] <= t:
            a, b, y, o, u, f = rows[i]
            i += 1
            last, last_flags = b, f
            if b <= start:
                continue
            flags |= f
            if b - a > MAX_GAP:
                flags |= sa.GAP
            if a < start:
                # The readings bound the change in time, but cannot tell
                # how much of it happened before the range began.
                if y or o or u:
                    boundary = sa.BOUNDARY
                continue
            you += y
            others += o
            unknown += u
        if i < len(rows):
            a, b, y, o, u, _ = rows[i]
            if a < start < b and (y or o or u):
                boundary = sa.BOUNDARY
        if last is None or t - last > MAX_GAP:
            flags |= sa.GAP
        flags |= last_flags & (sa.INCOMPLETE | sa.CALIBRATED | sa.RESET_TAIL)
        result.append([you, others, unknown, flags | boundary])
    return result


def _range(caches, accounts, now, duration, step):
    start = now - duration
    grid = range(start, now + 1, step)
    partial = any(p not in caches or caches[p]["partial"] for p in PROVIDERS)
    combined = [[0.0, 0.0, 0.0, sa.INCOMPLETE if partial and t > start else 0] for t in grid]
    total_usd = [0.0] * len(grid)
    total_others = [0.0] * len(grid)
    total_tokens = 0
    for cache in caches.values():
        usd, tokens = _spend_curve(cache["unattributed"], start, grid)
        total_usd = [a + b for a, b in zip(total_usd, usd)]
        total_tokens += tokens
    result, unrated, unknown = {}, [], []
    for account in accounts:
        meta = account["meta"]
        cache = caches[meta["provider"]]
        quota = _quota_curve(account["quota"], start, grid, cache["partial"])
        usd, tokens = _spend_curve(account["spend"], start, grid)
        rate = meta["usdPer100"]
        other_usd = [pt[1] * rate / 100 for pt in quota] if rate else None
        if other_usd is None and quota[-1][1] > 0:
            unrated.append(meta["key"])
        if quota[-1][2] > 0:
            unknown.append(meta["key"])
        for i, (y, o, u, f) in enumerate(quota):
            combined[i][0] += y / 100
            combined[i][1] += o / 100
            combined[i][2] += u / 100
            combined[i][3] |= f
            total_usd[i] += usd[i]
            if other_usd is not None:
                total_others[i] += other_usd[i]
        total_tokens += tokens
        result[meta["key"]] = {"quota": quota, "usdYou": [round(v, 8) for v in usd],
                               "usdOthers": [round(v, 8) for v in other_usd] if other_usd is not None else None,
                               "tokensYou": tokens}

    quota = [[round(y, 8), round(o, 8), round(u, 8), f] for y, o, u, f in combined]
    return {"from": start, "to": now, "stepSec": step,
            "combined": {"quota": quota, "usdYou": [round(v, 8) for v in total_usd],
                         "usdOthers": [round(v, 8) for v in total_others], "unratedAccounts": unrated,
                         "unknownAccounts": unknown,
                         "totalYouUsd": round(total_usd[-1], 2), "totalYouTokens": total_tokens,
                         "totalOthersUsd": round(total_others[-1], 2),
                         "totalYouQuota": quota[-1][0], "totalOthersQuota": quota[-1][1],
                         "totalUnknownQuota": quota[-1][2]},
            "accounts": result}


def overview(caches, now):
    ordered = [a for provider in PROVIDERS for a in (caches.get(provider) or {}).get("accounts", ())]
    return {"version": 1, "asOf": now, "quotaWindow": "7d",
            "providers": {p: {"builtAt": caches[p]["builtAt"], "partial": caches[p]["partial"]}
                          if p in caches else {"builtAt": None, "partial": True} for p in PROVIDERS},
            "accounts": [a["meta"] for a in ordered],
            "ranges": {name: _range(caches, ordered, now, duration, step)
                       for name, (duration, step) in RANGES.items()}}


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 2**53 and math.isfinite(value)


def _valid_spend(rows):
    return (isinstance(rows, list)
            and all(isinstance(row, list) and len(row) == 3 and _integer(row[0])
                    and _number(row[1]) and _integer(row[2]) for row in rows)
            and all(a[0] <= b[0] for a, b in zip(rows, rows[1:])))


def _validate_cache(provider, cache, now):
    """Validate the inputs before combining them with any other provider."""
    if (not isinstance(cache, dict) or not _integer(cache.get("builtAt")) or cache["builtAt"] > now
            or type(cache.get("partial")) is not bool or not isinstance(cache.get("accounts"), list)
            or not _valid_spend(cache.get("unattributed"))):
        raise ValueError("invalid timeline provider cache")
    seen = set()
    for account in cache["accounts"]:
        meta, quota = account["meta"], account["quota"]
        uid, key, rate = meta["uuid"], meta["key"], meta["usdPer100"]
        through = meta["quotaThrough"]
        if (not isinstance(uid, str) or not uid or key != f"{provider}:{uid}" or key in seen
                or meta["provider"] != provider or (rate is not None and (not _number(rate) or rate == 0))
                or type(meta["rated"]) is not bool or meta["rated"] != (rate is not None)
                or (through is not None and not _integer(through))
                or any(meta.get(k) is not None and not isinstance(meta[k], str) for k in ("short", "planName"))
                or ("active" in meta and type(meta["active"]) is not bool)
                or not _valid_spend(account["spend"])
                or not isinstance(quota, list)
                or not all(isinstance(row, list) and len(row) == 6 and _integer(row[0]) and _integer(row[1])
                           and row[0] <= row[1] and all(_number(n) for n in row[2:5])
                           and type(row[5]) is int and 0 <= row[5] <= 2**31 - 1 for row in quota)
                or not all(a[1] <= b[1] for a, b in zip(quota, quota[1:]))):
            raise ValueError("invalid timeline account cache")
        seen.add(key)
    return cache


def publish(con, state, now, inputs, *, codex_only=False, claude_complete=True):
    """Refresh provider inputs independently. A failed provider can keep its last good input."""
    try:
        caches = {}
        with con:
            for provider in PROVIDERS:
                key = f"timeline_provider:{provider}"
                try:
                    fresh = inputs.get(provider)
                    if provider == "claude" and not codex_only:
                        fresh = claude_input(con, state.get("accounts", []), now, claude_complete)
                    if fresh is not None:
                        cache = _validate_cache(provider, fresh, now)
                        core.kv_set(con, key, cache)
                        caches[provider] = _with_partial(cache, now)
                        continue
                except Exception:
                    pass  # a failed rebuild can still use this provider's last good cache
                try:
                    cache = _validate_cache(provider, core.kv_get(con, key), now)
                except (ValueError, TypeError, KeyError):
                    continue
                caches[provider] = _with_partial(cache, now, rebuild_failed=not codex_only or provider == "codex")
            state["overviewTimeline"] = overview(caches, now)
    except Exception as exc:
        state["overviewTimeline"] = {"error": f"timeline-failed:{type(exc).__name__}"}


def _with_partial(cache, now, *, rebuild_failed=False):
    return {**cache, "partial": cache["partial"] or rebuild_failed or now - cache["builtAt"] > CACHE_MAX_AGE}
