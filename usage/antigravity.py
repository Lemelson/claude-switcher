#!/usr/bin/env python3
"""Antigravity (Gemini) block of the summary: `state["antigravity"]`, contract in ANTIGRAVITY-STATE.md.

It is built like a Claude account so the panel can reuse its views:
  * spend: every Gemini call recorded in Antigravity's local conversation databases, read by
    antigravity_beta.py (exact tokens, dollars estimated at Gemini API list prices);
  * quota: the 5-hour and weekly percentages CodexBar recorded for its `antigravity` provider in
    ~/Library/Application Support/com.steipete.codexbar/history/antigravity.json, imported into the
    collector's `samples` table as account "antigravity" (source "codexbar");
  * windows, series, forecast, drift, daily, weeks: calib.account_state(), the same code as for a Claude
    account, run on an in-memory database that holds this account's calls as `rows` and its samples.
    The calls never go into usage.db, so nothing that sums Claude rows can see them;
  * rates, value, windows.7d.sessions: measured here (estimate(), sessions(), value_block()), because
    Antigravity readings jitter by up to a point and use outside this Mac is common; method in
    ANTIGRAVITY-STATE.md, sections Rate, Sessions per week, Value.

Cheap on purpose: tick.py runs on every menu open. The scan of the conversation databases (about 2 s)
is cached per file in DATA/antigravity-scan.json, keyed on the size and modification time of each
database and its -wal file; an unchanged Antigravity costs one stat per file. Only changed
conversations are read again. The CodexBar history is imported only when that file changed.

Read-only towards ~/.gemini and CodexBar's folders. Never raises out of block(): a failure becomes
{"uuid", "name", "short", "beta", "error"} and the rest of state.json is written as before.

  python3 usage/antigravity.py            print the block
  python3 usage/antigravity.py --check    self-check (shape, totals, cache, failure isolation)
  python3 usage/antigravity.py --sample   also write usage/antigravity-state-sample.json
"""
import fcntl  # noqa: F401  (kept for callers that used antigravity.fcntl)
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import antigravity_beta as agb  # noqa: E402
import cache_compare_beta as ccb  # noqa: E402
import calib  # noqa: E402
import core  # noqa: E402
import quota_provider as qp  # noqa: E402
from quota_provider import (  # noqa: E402,F401  (the names this module always had)
    CONFIDENT_STRETCHES, CONFIDENT_USD, DROP, DROP_PTS, DROP_WEEK, DROP_WINDOW, JITTER, LIVE_COMPACT_BYTES,
    LIVE_KEEP_DAYS, PURE_SHARE, SESSION_D5, SESSION_D7, SESSION_PAIRS, SESSION_SUM7, STRETCH_USD, WEEKS_PER_MONTH,
    WINS, ModelSpend, live_plan, model_key, model_key_identity, parse_live, tilde)

ACC = "antigravity"
NAME = "Antigravity"
GEMINI_HOME = os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~/.gemini")
HISTORY = os.path.join(os.path.dirname(core.CODEXBAR_HISTORY), "antigravity.json")
SCAN_CACHE = f"{core.DATA}/antigravity-scan.json"
# Every fresh reading the menu bar app gets, one JSON line each (format in ANTIGRAVITY-STATE.md,
# "Live readings file"). Writer and compaction serialise on the separate lock file.
LIVE = f"{core.DATA}/antigravity-quota.jsonl"
CACHE_VERSION = 1
# what is kept of each call in the scan cache (everything dedupe() and summarize() need)
FIELDS = ("rid", "reqId", "cascade", "conv", "idx", "model", "priceAs", "effort", "input", "cacheRead",
          "cacheWrite", "output", "thinking", "outputCheckFailed", "ts", "timeSource")
# what is kept of each call after merging and pricing
CALL_FIELDS = ("ts", "model", "priceAs", "effort", "input", "cacheRead", "cacheWrite", "output", "thinking",
               "timeSource", "usd", "day")
# Plan: the app writes the plan name it fetched into its live readings; the price comes from the list below.
# The owner's file overrides both (see ANTIGRAVITY-STATE.md, Plan).
PLAN_FILE = f"{core.DATA}/antigravity-plan.json"
PLAN_SEEN_KEY = "antigravity_plan_seen"
# Monthly list prices of the Google AI plans (USD, as of November 2026). First match wins: every word of
# the key must be in the plan name. A name that matches nothing has no price.
PLAN_PRICES = (("ultra 20x", 199.99), ("ultra 5x", 99.99), ("pro", 19.99))


def model_labels(ids):
    """The panel's short names: "gemini-4-neon" -> "Neon"; two models with one name keep the version."""
    def parse(i):
        parts = i.lower().split("-")
        if parts and parts[0] == "gemini":
            parts = parts[1:]
        ver = parts.pop(0) if parts and parts[0][:1].isdigit() else None
        return (parts[0].capitalize() if parts else (f"Gemini {ver}" if ver else i)), ver
    parsed = {i: parse(i) for i in ids}
    count = {}
    for name, _ in parsed.values():
        count[name] = count.get(name, 0) + 1
    return {i: f"{n} {v}" if count[n] > 1 and v else n for i, (n, v) in parsed.items()}


P = qp.Provider(
    acc=ACC, name=NAME, history=HISTORY, live=LIVE, plan_file=PLAN_FILE, plan_prices=PLAN_PRICES,
    spend_word="Gemini", elsewhere="phone, chat app",
    weekly_text="the weekly quota of Antigravity's Gemini model group (older CodexBar history may hold the "
                "highest weekly quota of any group)",
    labels=model_labels)
assert P.plan_seen_key == PLAN_SEEN_KEY and P.live_key == "antigravity_live"


# ------------------------------------------------------------------------------- quota ----
# The quota part is shared with the Codex block: quota_provider.py. These keep this module's old names.
def import_history(con):
    return qp.import_history(P, con)


def _insert_live(con, lines):
    return qp._insert_live(P, con, lines)


def import_live(con, path=LIVE, now=None, compact_bytes=LIVE_COMPACT_BYTES, kv_key="antigravity_live"):
    return qp.import_live(P, con, path, now, compact_bytes, kv_key)


def compact_live(con, path, now):
    return qp.compact_live(P, con, path, now)


def import_quota(con):
    """Both quota sources; what tick.py logs."""
    return qp.import_quota(P, con)


# -------------------------------------------------------------------------------- scan ----
def file_sig(path):
    s = os.stat(path)
    try:
        w = os.stat(path + "-wal")
        wal = [w.st_size, w.st_mtime_ns]
    except OSError:
        wal = [0, 0]
    return [s.st_size, s.st_mtime_ns, *wal]


_load = qp.load_json
_save = qp.save_json

def scan(home=GEMINI_HOME, cache_path=SCAN_CACHE):
    """(unique priced events, coverage stats, {"cached": bool, "rescanned": n, "ms": n}).

    Two cache layers in DATA: `antigravity-scan.json` keeps the raw rows of every conversation
    database with its signature (size and mtime of the .db and its -wal), so only changed databases
    are read again; `antigravity-scan-calls.json` keeps the merged result (copies dropped, each call
    priced by summarize() on its own local date) under the signature of all files. When nothing changed,
    only the second file is loaded. Prices depend on the call's date, not on today, so a cached price
    stays right; an edit of antigravity_beta.py (the parser or the price table) invalidates both."""
    t0 = time.time()
    paths, pb = agb.conversation_files(home)
    encrypted_mtimes = []
    unreadable_encrypted = 0
    for directory in agb.DB_DIRS:
        for path in glob.glob(os.path.join(home, directory, "*.pb")):
            try:
                encrypted_mtimes.append(os.path.getmtime(path))
            except OSError:
                unreadable_encrypted += 1
    sigs = {}
    for p in paths:
        try:
            sigs[p] = file_sig(p)
        except OSError:
            continue
    key = [CACHE_VERSION, file_sig(agb.__file__)[:2], home, len(pb), sorted(sigs.items())]
    key = json.loads(json.dumps(key))   # the shape it has after a round trip through the file
    calls_path = cache_path[:-5] + "-calls.json" if cache_path.endswith(".json") else cache_path + "-calls"
    merged = _load(calls_path)
    file_coverage = {"encryptedMtimes": encrypted_mtimes, "unreadableEncryptedFiles": unreadable_encrypted,
                     "statErrors": len(paths) - len(sigs)}
    if merged and merged.get("key") == key:
        events = [dict(zip(CALL_FIELDS, e)) for e in merged["events"]]
        stats = {**merged["stats"], **file_coverage}
        return events, stats, {"cached": True, "rescanned": 0, "ms": round((time.time() - t0) * 1000)}

    cache = _load(cache_path)
    old = cache["files"] if cache and cache.get("key0") == key[:4] else {}
    files, changed = {}, 0
    with tempfile.TemporaryDirectory(prefix="agy-scan-") as tmp:
        for p, sig in sigs.items():
            c = old.get(p)
            if c and c["sig"] == sig:
                files[p] = c
                continue
            evs, st = agb.read_file(p, tmp)
            files[p] = {"sig": sig, "stats": st, "events": [[ev[k] for k in FIELDS] for ev in evs]}
            changed += 1
    if changed or set(files) != set(old):
        _save(cache_path, {"key0": key[:4], "files": files})
    stats = {k: 0 for k in ("dbFiles", *agb.FILE_STATS)}
    raw = []
    for p in paths:
        f = files.get(p)
        if not f:
            continue
        stats["dbFiles"] += 1
        for k, v in f["stats"].items():
            stats[k] = stats.get(k, 0) + v
        raw += [dict(zip(FIELDS, e)) for e in f["events"]]
    events, stats["copiesDropped"] = agb.dedupe(raw)
    stats["outputCheckFailed"] = sum(bool(ev["outputCheckFailed"]) for ev in events)
    stats["encryptedPbFiles"] = len(pb)
    r = agb.summarize(events, stats, time.time(), 1)   # sets ev["usd"], ev["day"] and stats["timeSources"]
    stats["historySpan"] = r["historySpan"]
    events = [{k: ev[k] for k in CALL_FIELDS} for ev in events]
    _save(calls_path, {"key": key, "stats": stats, "events": [[ev[k] for k in CALL_FIELDS] for ev in events]})
    stats.update(file_coverage)
    return events, stats, {"cached": False, "rescanned": changed, "ms": round((time.time() - t0) * 1000)}


def models_30d(events, now):
    out = {}
    for ev in events:
        if ev["ts"] < now - 30 * 86400:
            continue
        k = model_key(ev)
        m = out.setdefault(k, {"acc": agb.blank(), "efforts": {}})
        agb.add(m["acc"], ev)
        if ev["effort"]:
            m["efforts"][ev["effort"]] = m["efforts"].get(ev["effort"], 0) + 1
    res = {}
    for k, m in sorted(out.items(), key=lambda x: -x[1]["acc"]["usd"]):
        a = m["acc"]
        res[k] = {"calls": a["requests"], "input": a["input"], "cached": a["cacheRead"], "cacheWrite": a["cacheWrite"],
                  "output": a["output"], "thinking": a["thinking"], "usd": round(a["usd"], 2),
                  "missPct": agb.miss_pct(a), "efforts": m["efforts"],
                  "priced": k in agb.PRICES, "priceConfirmed": k in agb.PRICES and k not in agb.UNCONFIRMED,
                  "unpricedCalls": a["unpricedRequests"]}
    return res

def input_prices(ev):
    """(full input price, cache-read price) in $ per token for this call, or None if the model has no price."""
    key = ev["priceAs"]
    if not key or key not in agb.PRICES:
        return None
    pi, _, pr = agb.prices_on(key, ev["day"])
    lc = agb.LONG_CONTEXT.get(key)
    if lc and ev["input"] + ev["cacheRead"] + ev["cacheWrite"] > lc[0]:
        pi, _, pr = lc[1]
    return pi / 1e6, pr / 1e6


def cache_stats(events, since):
    """Share of prompt tokens read from the cache, and what that saved / what the misses cost (shared code in
    quota_provider.cache_stats). Gemini bills a cache write at the full input price, so writes count as missed."""
    return qp.cache_stats(events, since, input_prices, ccb.READ_X["gemini"])


# ------------------------------------------------------------- rates, sessions, value ----
# Shared with the Codex block (quota_provider.py); method in ANTIGRAVITY-STATE.md.
def analysis_db(con, events):
    return qp.analysis_db(P, con, events)


def merged_samples(con):
    return qp.merged_samples(P, con)


def stretches(mem, ms, win, t0):
    return qp.stretches(P, mem, ms, win, t0)


def estimate(st, win="7d"):
    return qp.estimate(P, st, win)


def sessions(mem, t0):
    return qp.sessions(P, mem, t0)


def plan_price(name):
    return qp.plan_price(P, name)


def read_plan(path=PLAN_FILE, seen=None):
    return qp.read_plan(P, path, seen)


def value_block(rate, st, plan, models):
    return qp.value_block(P, rate, st, plan, models)


def quota_source(con, now):
    return qp.quota_source(P, con, now)


def build(con, now, home=GEMINI_HOME, cache_path=SCAN_CACHE, *, timeline_inputs=None):
    events, stats, scan_info = scan(home, cache_path)
    for ev in events:
        ev["priced"] = ev["priceAs"] in agb.PRICES     # daily[].unpriced (quota_provider.build_common)
    models = models_30d(events, now)
    complete = not any(stats.get(k) for k in ("dbUnreadable", "rowsMalformed", "statErrors",
                                             "unreadableEncryptedFiles"))
    intervals = [] if timeline_inputs is not None else None
    info = qp.build_common(P, con, now, events, models, complete=complete,
                           encrypted_mtimes=stats["encryptedMtimes"], weekly_intervals=intervals)
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    info["cache"] = {"today": cache_stats(events, today0), "7d": cache_stats(events, now - 7 * 86400),
                     "30d": cache_stats(events, now - 30 * 86400)}
    ts = stats.get("timeSources") or {}
    info["coverage"] = {
        "tokens": "exact", "usd": "estimate",
        "historySince": stats["historySpan"][0] if stats.get("historySpan") else None,
        "dbFiles": stats["dbFiles"], "calls": len(events), "copiesDropped": stats["copiesDropped"],
        "encryptedConversations": stats["encryptedPbFiles"],
        "unreadableEncryptedFiles": stats["unreadableEncryptedFiles"],
        "timeEstimatedCalls": sum(v for k, v in ts.items() if k not in ("row", "steps")),
        "outputCheckFailed": stats["outputCheckFailed"],
        "unreadableDbs": stats["dbUnreadable"], "malformedRows": stats["rowsMalformed"],
        "unpricedCalls30d": sum(m["unpricedCalls"] for m in info["models"].values()),
        "unconfirmedPrices": sorted(k for k in info["models"] if k in agb.UNCONFIRMED),
        "pricesChecked": agb.PRICES_CHECKED, "priceSource": agb.PRICE_SOURCE,
        "notCounted": "encrypted .pb conversations, deleted conversations, Tab completions, other devices",
        "scanCached": scan_info["cached"], "scanRescannedFiles": scan_info["rescanned"], "scanMs": scan_info["ms"],
    }
    if timeline_inputs is not None:
        from timeline import account_input, event_spend, provider_input
        entry = account_input("antigravity", {**info, "active": True}, intervals, event_spend(events), now)
        timeline_inputs["antigravity"] = provider_input(now, [entry], partial=not complete)
    return info


def block(con, now=None, *, timeline_inputs=None):
    """(state["antigravity"], short log entry). Never raises."""
    now = int(now or time.time())
    t0 = time.time()
    try:
        info = build(con, now, timeline_inputs=timeline_inputs)
        c = info["coverage"]
        return info, {"ms": round((time.time() - t0) * 1000), "scan": "cached" if c["scanCached"] else
                      f"read {c['scanRescannedFiles']} files", "calls": c["calls"],
                      "rates": sorted(info["rates"])}
    except Exception as e:
        err = qp.error_code("antigravity", e)
        return qp.error_block(P, err), "error: " + err


# ------------------------------------------------------------------------------- check ----
def check_live(say):
    """The live readings file: incremental import, broken and unfinished lines, merge, compaction.
    Runs on a temporary file and an in-memory database, never on the real ones."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "live: " + what)

    con = sqlite3.connect(":memory:")
    con.executescript(core.SCHEMA)
    T = 1795424400 + 60          # one minute into a clock hour
    R5, R7 = 1795439520, 1795873920
    line = lambda ts, w: json.dumps({"ts": ts, "source": "codexbar-live", "windows": w}) + "\n"
    with tempfile.TemporaryDirectory(prefix="agy-live-") as tmp:
        path = os.path.join(tmp, "antigravity-quota.jsonl")
        t(import_live(con, path) == "absent", "missing file is 'absent'")
        with open(path, "w") as fh:
            fh.write(line(T, {"5h": {"pct": 10.5, "resetsAt": R5}, "7d": {"pct": 40.0, "resetsAt": R7}}))
            fh.write("not json at all\n")
            fh.write(line(T + 600, {"7d": {"pct": 41.0, "resetsAt": R7}}))            # 5h missing
            fh.write(line(T + 900, {"5h": {"pct": 250}}))                            # out of range: bad
            fh.write(line(T + 1000, {"5h": {"pct": 11, "resetsAt": None}}))           # null reset: kept
            fh.write('{"ts": %d, "windows": {"7d": {"pct": 4' % (T + 1200))          # still being written
        t(import_live(con, path) == 4, "4 samples from 3 good lines, the broken ones skipped")
        lv = core.kv_get(con, "antigravity_live")
        t(lv["bad"] == 2, f"2 bad lines counted (got {lv['bad']})")
        t(lv["offset"] == os.path.getsize(path) - len('{"ts": %d, "windows": {"7d": {"pct": 4' % (T + 1200)),
          "offset stops before the unfinished line")
        with open(path, "a") as fh:
            fh.write('2.0, "resetsAt": %d}}}\n' % R7)
        t(import_live(con, path) == 1, "the finished line is read on the next pass")
        t(import_live(con, path) == 0, "nothing new: nothing imported")
        # merge: a CodexBar sample in the same clock hour as live ones is dropped, another hour is kept
        con.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                        [(ACC, "7d", T + 600, 41.0, R7, "codexbar"), (ACC, "7d", T - 7200, 38.0, R7, "codexbar"),
                         (ACC, "5h", T - 7200, 0.0, R5 - 7200, "codexbar")])
        m = merged_samples(con)
        src = sorted((r[1], r[2] - T, r[5]) for r in m)
        t(("7d", 600, "codexbar") not in src and ("7d", 600, "live") in src, "same hour: live wins")
        t(("7d", -7200, "codexbar") in src and ("5h", -7200, "codexbar") in src, "other hours keep CodexBar")
        t(len(m) == 7, f"each reading counted once ({len(m)} of 8 rows)")
        # compaction: old lines go, the offset follows the new file, nothing is imported twice
        with open(path, "a") as fh:
            for i in range(30):
                fh.write(line(T - 50 * 86400 + i, {"7d": {"pct": 1.0, "resetsAt": R7 - 49 * 86400}}))
        ino = os.stat(path).st_ino
        n_before = con.execute("SELECT count(*) FROM samples WHERE source='live'").fetchone()[0]
        import_live(con, path, now=T + 3000, compact_bytes=500)
        with open(path) as fh:
            kept = fh.read().splitlines()
        lv = core.kv_get(con, "antigravity_live")
        t(os.stat(path).st_ino != ino and len(kept) == 4, f"compacted to the 4 recent good lines ({len(kept)})")
        t(lv["offset"] == os.path.getsize(path) and lv["compactedAt"] == T + 3000, "offset = new size")
        t(con.execute("SELECT count(*) FROM samples WHERE source='live'").fetchone()[0] == n_before + 30,
          "old lines were imported before they were dropped")
        t(import_live(con, path) == 0, "after compaction nothing is imported again")
        t(not any(f.startswith("antigravity-quota.jsonl.tmp") for f in os.listdir(tmp)), "no temp file left")
    return ok


def check_estimators(say):
    """estimate(), sessions(), cache_stats(), read_plan(), value_block() on made-up data."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "calc: " + what)

    R = 0.2   # true rate: 0.2 % of the window per dollar, $500 per 100 %
    clean = [{"dp": R * u + j, "usd": u, "byModel": {"gemini-4-neon": u}}
             for u, j in ((60, 0.3), (75, -0.6), (90, 0.8), (64, -0.2), (120, 0.0))]
    polluted = [{"dp": R * u + extra, "usd": u, "byModel": {"gemini-4-neon": u}}
                for u, extra in ((60, 6.0), (80, 12.0), (70, 3.5))]
    a, b = estimate(clean), estimate(clean + polluted)
    t(a and abs(a["usd_per_100"] - 500) / 500 < 0.02, f"clean stretches give $500 per 100 % ({a and round(a['usd_per_100'])})")
    t(b and b["pairsUsed"] == 5 and b["pairsDropped"] == 3, "the three stretches with outside use are dropped")
    t(b and a and abs(b["pct_per_usd"] - a["pct_per_usd"]) < 1e-12, "outside use does not move the estimate")
    t(estimate([]) is None and estimate([{"dp": -2.0, "usd": 60, "byModel": {}}]) is None, "no usable stretch: no rate")
    one = estimate(clean[:1])
    t(one and one["pairsUsed"] == 1 and one["range"][0] == one["range"][1], "one stretch: a rate with a zero range")
    t(all(e["pairsUsed"] + e["pairsDropped"] + e["pairsLow"] == n for e, n in ((a, 5), (b, 8), (one, 1))),
      "every stretch is used, dropped or low")
    t("$60+" in b["method"] and "$15+" in estimate(clean, "5h")["method"], "method names the window's stretch size")

    def lowest_cap(st):   # the rule as first written: the lowest cap, clean = floor not above it
        upper = min((s["dp"] + JITTER) / s["usd"] for s in st)
        cl = [s for s in st if (s["dp"] - JITTER) / s["usd"] <= upper]
        return min(upper, sum(s["dp"] for s in cl) / sum(s["usd"] for s in cl)), len(cl)
    # a $60 stretch that moved the limit 2 points where 12 were due: a reading off by more than the jitter,
    # calls dated wrongly, or spend the shown quota does not count
    odd = {"dp": 2.0, "usd": 60, "byModel": {"gemini-4-neon": 60}}
    c = estimate(clean + polluted + [odd])
    old_r, old_n = lowest_cap(clean + polluted + [odd])
    t(c and c["pct_per_usd"] == b["pct_per_usd"] and c["pairsLow"] == 1 and c["pairsUsed"] == 5,
      f"one odd low stretch is set aside: ${c and round(c['usd_per_100'])} per 100 % (the lowest cap alone gave "
      f"${round(100 / old_r)} from {old_n} stretch and dropped the 5 clean ones)")
    lucky = {"dp": R * 60 - 0.8, "usd": 60, "byModel": {"gemini-4-neon": 60}}   # both readings 0.4 off, apart
    d = estimate(clean + [lucky])
    t(d and abs(d["usd_per_100"] - 500) / 500 < 0.02 and abs(100 / lowest_cap(clean + [lucky])[0] - 500) / 500 < 0.02,
      f"jitter alone moves neither rule by 2 % (${d and round(d['usd_per_100'])} per 100 %)")
    # two stretches that begin on a reading taken during use (1.6 points high, as on 23.11) and end settled:
    # their caps must still be above the true rate, or the two agree with each other and set the ceiling
    settled = [{"dp": R * u - 1.6, "usd": u, "byModel": {"gemini-4-neon": u}} for u in (60, 70)]
    e = estimate(clean + settled)
    t(all((s["dp"] + JITTER) / s["usd"] >= R for s in settled) and e and abs(e["usd_per_100"] - 500) / 500 < 0.03,
      f"readings that settle by 1.6 points stay inside the jitter (${e and round(e['usd_per_100'])} per 100 %)")
    two = estimate([odd, clean[0]])
    t(two and two["pairsUsed"] == 1 and two["usd_per_100"] > 2000,
      "two stretches that disagree: the lower cap stays (which one is wrong cannot be told; value not confident)")

    v = value_block(b, clean + polluted, ("Plan X", 100.0), {"gemini-4-neon": {}, "gemini-4-carbon": {}})
    t(v["confident"] and v["reason"] is None and v["timesPrice"] == round(v["monthUsd"] / 100, 1),
      "5 clean stretches, $409: confident, × against the plan price")
    t(v["byModel"][0]["monthUsd"] == v["monthUsd"] and v["byModel"][1]["monthUsd"] is None,
      "byModel: the measured model gets its own value, an unmeasured one null")
    v1 = value_block(one, clean[:1], (None, None), {})
    t(not v1["confident"] and v1["reason"] and v1["timesPrice"] is None, "one stretch: not confident, no price, no ×")

    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    T, W = 1795000000, 1795500000
    rows = []
    for k, (d5, d7) in enumerate(((30, 5), (24, 4), (36, 6), (2, 0.4))):   # the last one moved too little
        r5, t0 = T + k * 18000 + 18000, T + k * 18000 + 600
        for i, (p5, p7) in enumerate(((0.0, 10 + k), (d5, 10 + k + d7))):
            rows += [(ACC, "5h", t0 + i * 9000, p5, r5, "live"), (ACC, "7d", t0 + i * 9000, p7, W, "live")]
    # a fifth window with a reset of the weekly limit inside (14 -> 0.5 -> 20): last minus first says 6 points,
    # the week really took 14 + 20; it must be skipped, not counted as 30 / 6
    r5 = T + 5 * 18000 + 18000
    for i, (p5, p7) in enumerate(((0.0, 14.0), (15.0, 0.5), (30.0, 20.0))):
        rows += [(ACC, "5h", T + 5 * 18000 + 600 + i * 4000, p5, r5, "live"),
                 (ACC, "7d", T + 5 * 18000 + 600 + i * 4000, p7, W, "live")]
    mem.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    s, n, so_far = sessions(mem, 0)
    t(s == {"perWeek": 6.0, "pairs": 3} and n == 3, f"sessions: 90 / 15 points = 6.0 per week from 3 windows ({s})")
    t(n == 3, "sessions: a window with a reset inside is skipped (counted, it gave 120 / 21 = 5.7)")
    mem.execute("DELETE FROM samples WHERE resets_at=?", (T + 2 * 18000 + 18000,))
    s, n, so_far = sessions(mem, 0)
    t(s is None and n == 2 and so_far == 6.0, "sessions: two windows are too few, null")

    # A reading taken during use, then the next one after it settled 1.6 points lower in one step (on 23.11
    # the weekly reading fell 42.77 -> 41.16 within an hour, with no call): not a reset. Claude's 1.5-point
    # rule would restart the week there; a real reset (45 -> 0.5) must still be one.
    rs = sqlite3.connect(":memory:")
    rs.executescript(core.SCHEMA)
    W0 = W - calib.SPAN["7d"]
    rs.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                   [(ACC, "7d", W0 + h * 3600, p, W, "live") for h, p in ((10, 30.0), (20, 42.77), (21, 41.17), (30, 45.0))])
    pts = calib.load_points(rs, ACC, "7d", 0)
    t(calib.window_start(rs, ACC, "7d", {"resets_at": W}, W, DROP_PTS) == W0 and len(calib.segments(pts, DROP_PTS)) == 1,
      "a reading that settles by 1.6 points is not a reset (Claude's rule would start the week at it: "
      f"{(calib.window_start(rs, ACC, '7d', {'resets_at': W}, W) - W0) // 3600} h in, {len(calib.segments(pts))} segments)")
    rs.execute("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
               (ACC, "7d", W0 + 31 * 3600, 0.5, W, "live"))
    t(calib.window_start(rs, ACC, "7d", {"resets_at": W}, W, DROP_PTS) == W0 + 30 * 3600 + 1,
      "a real reset (45 -> 0.5) still starts a new window")

    evs = [{"ts": T, "priceAs": "gemini-4-neon", "day": "2026-11-23", "input": 1000, "cacheRead": 9000,
            "cacheWrite": 0}, {"ts": T, "priceAs": None, "day": "2026-11-23", "input": 500, "cacheRead": 500,
                               "cacheWrite": 100}]
    c = cache_stats(evs, 0)
    t(c["readPct"] + c["missPct"] == 100 and c["readTokens"] == 9500 and c["missTokens"] == 1600 and
      c["writeTokens"] == 100, f"cache: read + missed = 100 %, token counts ({c['readPct']} / {c['missPct']})")
    t(abs(c["savedUsd"] - round(9000 * 2.85e-6, 2)) < 1e-9 and abs(c["missUsd"] - round(1000 * 2.85e-6, 2)) < 1e-9,
      "cache: saved and missed dollars of priced calls only")
    with tempfile.TemporaryDirectory(prefix="agy-plan-") as tmp:
        p = os.path.join(tmp, "plan.json")
        t(read_plan(p) == (None, None), "no plan file: plan unknown")
        with open(p, "w") as fh:
            fh.write('{"planName": "Plan X", "planPriceUsd": 19.99}')
        t(read_plan(p) == ("Plan X", 19.99), "plan file read")
        with open(p, "w") as fh:
            fh.write('{"planName": "", "planPriceUsd": "lots"}')
        t(read_plan(p) == (None, None), "malformed plan file: plan unknown")
        t(read_plan(p, seen="Google AI Ultra 5x") == ("Google AI Ultra 5x", 99.99), "plan seen by the app: list price")
        t(read_plan(p, seen="Something new") == ("Something new", None), "unknown plan name: no price")
        with open(p, "w") as fh:
            fh.write('{"planPriceUsd": 120}')
        t(read_plan(p, seen="Google AI Pro") == ("Google AI Pro", 120.0), "the owner's price wins over the list")
    t(plan_price("Google AI Ultra 20x") == 199.99 and plan_price("Ultra") is None and plan_price(None) is None,
      "list prices: Ultra needs its size")
    t(live_plan('{"ts": 1, "plan": " Google AI Pro "}') == "Google AI Pro" and live_plan('{"plan": 5}') is None
      and live_plan("not json") is None, "plan name in a live line")
    t(model_labels(["gemini-4-neon", "gemini-3.7-flash", "gemini-3.6-flash"]) ==
      {"gemini-4-neon": "Neon", "gemini-3.7-flash": "Flash 3.7", "gemini-3.6-flash": "Flash 3.6"}, "model labels")
    return ok


def check():
    """Self-check on this Mac's real data. Prints one line per check, exits 1 on a failure."""
    import provider_check

    ok = True

    def say(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("ok    " if cond else "FAIL  ") + what)

    con = core.db()
    n = import_quota(con)
    say(all(v == "absent" or isinstance(v, int) for v in n.values()), f"quota import ran ({n})")
    ok &= check_live(say)
    ok &= check_estimators(say)
    ok &= provider_check.check(P, say)
    now = int(time.time())
    with tempfile.TemporaryDirectory(prefix="agy-check-") as tmp:
        cp = os.path.join(tmp, "scan.json")
        t = time.time(); cold = build(con, now, cache_path=cp); t_cold = time.time() - t
        t = time.time(); warm = build(con, now, cache_path=cp); t_warm = time.time() - t
        events, stats, _ = scan(cache_path=cp)
        totals = agb.summarize(events, stats, now, 1)["totals"]
    say(not cold["coverage"]["scanCached"] and warm["coverage"]["scanCached"], "second build comes from the scan cache")
    strip = lambda b: json.dumps({k: v for k, v in b.items() if k != "coverage"}, sort_keys=True)
    say(strip(cold) == strip(warm), "cached build equals a fresh one")
    print(f"      build: cold {t_cold * 1000:.0f} ms, warm {t_warm * 1000:.0f} ms")
    b = warm
    for k in ("uuid", "name", "short"):
        say(isinstance(b.get(k), str), f"Swift-required key {k} is a string")
    say(b["name"] == NAME and b["uuid"] == ACC and b["beta"] is True, "name/uuid/beta literal")
    for k in ("usdToday", "usd7d", "usd30d", "tokensToday", "tokens7d", "tokens30d"):
        say(isinstance(b.get(k), (int, float)), f"{k} is a number")
    for w in ("today", "7d", "30d"):
        key = {"today": "usdToday", "7d": "usd7d", "30d": "usd30d"}[w]
        tot = round(totals[w]["all"]["usd"], 2)
        say(abs(b[key] - tot) <= 0.05, f"{key} {b[key]} = antigravity_beta total {tot}")
    for win, w in b["windows"].items():
        say(win in ("5h", "7d"), f"window {win} is 5h or 7d")
        provider_check.check_split_shape(w, say)
        say(set(w["usd"]) == {"opus", "fable", "other"} if "usd" in w else True, f"window {win} usd shape")
    say(all(set(d) >= {"day", "opus", "fable", "other", "tokens", "m"} for d in b["daily"]), "daily rows shape")
    say(all(d["opus"] == 0 and d["fable"] == 0 for d in b["daily"]), "all Gemini dollars are in other")
    say((b["value"] is None) == (b["valueReason"] is not None), "value xor valueReason")
    for win, r in b["rates"].items():
        say({"pct_per_usd", "usd_per_100", "pairs", "source", "method", "pairsUsed", "pairsDropped",
             "pairsLow"} <= set(r), f"rates.{win} shape")
    if b["value"]:
        say({"planName", "planPriceUsd", "weekUsd", "monthUsd", "timesPrice", "pairs", "confident", "reason",
             "byModel", "weekOpusUsd", "monthOpusUsd"} <= set(b["value"]), "value shape")
        say(b["value"]["confident"] == (b["value"]["reason"] is None), "value: confident xor reason")
    if "7d" in b["windows"]:
        s = b["windows"]["7d"].get("sessions", "missing")
        say(s is None or (set(s) == {"perWeek", "pairs"} and s["pairs"] >= SESSION_PAIRS), "windows.7d.sessions shape")
    for span, c in b["cache"].items():
        say({"readPct", "missPct", "calls", "coldCallsPct", "readTokens", "missTokens", "writeTokens", "savedUsd",
             "missUsd"} <= set(c), f"cache.{span} shape")
        say(c["readPct"] is None or abs(c["readPct"] + c["missPct"] - 100) < 1e-9, f"cache.{span} read + missed = 100")
    say(not any(k in b for k in DROP), "Claude-only keys dropped")
    text = json.dumps(b)
    say("@" not in text, "no e-mail-like text in the block")
    say(os.path.expanduser("~") not in text, "no home path in the block")
    # a failure must stay inside the block
    mod = sys.modules[__name__]
    real = mod.build
    mod.build = lambda *a, **k: 1 / 0
    try:
        eb, log = block(con, now)
    finally:
        mod.build = real
    say(set(eb) == {"uuid", "name", "short", "beta", "error"} and log.startswith("error:"),
        "a failure gives an error block, not an exception")
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    return ok


def main():
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    con = core.db()
    import_quota(con)
    b, log = block(con)
    print(json.dumps(b, ensure_ascii=False, indent=1))
    print(json.dumps(log), file=sys.stderr)
    if "--sample" in sys.argv:
        with open(os.path.join(HERE, "antigravity-state-sample.json"), "w") as fh:
            json.dump(b, fh, ensure_ascii=False, indent=1)
            fh.write("\n")


if __name__ == "__main__":
    main()
