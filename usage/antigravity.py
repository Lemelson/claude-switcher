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
import bisect
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import antigravity_beta as agb  # noqa: E402
import cache_compare_beta as ccb  # noqa: E402
import calib  # noqa: E402
import core  # noqa: E402

ACC = "antigravity"
NAME = "Antigravity"
GEMINI_HOME = os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~/.gemini")
HISTORY = os.path.join(os.path.dirname(core.CODEXBAR_HISTORY), "antigravity.json")
SCAN_CACHE = f"{core.DATA}/antigravity-scan.json"
# Every fresh reading the menu bar app gets, one JSON line each (format in ANTIGRAVITY-STATE.md,
# "Live readings file"). Writer and compaction serialise on the separate lock file.
LIVE = f"{core.DATA}/antigravity-quota.jsonl"
LIVE_KEEP_DAYS = 40
LIVE_COMPACT_BYTES = 1_000_000
CACHE_VERSION = 1
WINS = {"session": "5h", "weekly": "7d"}
# what is kept of each call in the scan cache (everything dedupe() and summarize() need)
FIELDS = ("rid", "reqId", "cascade", "conv", "idx", "model", "priceAs", "effort", "input", "cacheRead",
          "cacheWrite", "output", "thinking", "outputCheckFailed", "ts", "timeSource")
# what is kept of each call after merging and pricing
CALL_FIELDS = ("ts", "model", "priceAs", "effort", "input", "cacheRead", "cacheWrite", "output", "thinking",
               "timeSource", "usd", "day")
# keys of a Claude account that mean nothing here (see the contract)
DROP = ("orgs", "plan", "org", "tier", "fableShareOfWeek", "kSource", "kFrom", "kPairs", "kFit", "kFable5h",
        "kSource5h", "kFrom5h", "hits")
DROP_WINDOW = ("mine", "outside", "server")
# the same "this Mac vs elsewhere" split on weekly rows (calib.weekly_windows adds it for Claude accounts)
DROP_WEEK = ("minePct", "minePctByModel", "othersPct")


def tilde(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


# ------------------------------------------------------------------------------- quota ----
def import_history(con):
    """CodexBar's Antigravity quota history -> samples(acc='antigravity', win='5h'|'7d', source='codexbar').
    Skipped when the file has not changed since the last import. Returns the number of new samples."""
    try:
        st = os.stat(HISTORY)
    except OSError:
        return "absent"
    sig = [st.st_size, st.st_mtime_ns]
    if core.kv_get(con, "antigravity_history_sig") == sig:
        return 0
    with open(HISTORY) as fh:
        h = json.load(fh)
    accounts = h.get("accounts") or {}
    # One Google account is expected. With several, CodexBar's preferred one is taken.
    key = h.get("preferredAccountKey") if h.get("preferredAccountKey") in accounts else (
        next(iter(accounts)) if len(accounts) == 1 else None)
    series = accounts.get(key) if key else h.get("unscoped") or []
    rows = []
    for s in series or []:
        win = WINS.get(s.get("name"))
        if not win:
            continue
        for e in s.get("entries") or []:
            try:
                ts = core.parse_iso(e["capturedAt"])
                ra = core.parse_iso(e["resetsAt"]) if e.get("resetsAt") else None
                pct = float(e["usedPercent"])
            except Exception:
                continue
            if ra is not None:
                ra = (ra + 30) // 60 * 60  # as history_import.py: 09:59:59 and 10:00:00 are one window
            rows.append((ACC, win, ts, round(pct, 4), ra, "codexbar"))
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    added = con.total_changes - before
    core.kv_set(con, "antigravity_history_sig", sig)
    core.kv_set(con, "antigravity_history_accounts", len(accounts))
    con.commit()
    return added


def parse_live(line):
    """One line of the live readings file -> [(win, ts, pct, resets_at)], [] if it carries nothing usable."""
    try:
        r = json.loads(line)
        ts = int(r["ts"])
        wins = r["windows"]
    except Exception:
        return []
    if not (1e9 < ts < 1e10) or not isinstance(wins, dict):
        return []
    out = []
    for win in ("5h", "7d"):
        w = wins.get(win)
        if not isinstance(w, dict):
            continue
        pct, ra = w.get("pct"), w.get("resetsAt")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            continue
        if isinstance(ra, bool) or not isinstance(ra, (int, float)) or not 1e9 < ra < 1e10:
            ra = None
        else:
            ra = (int(ra) + 30) // 60 * 60  # same rounding as the CodexBar history
        out.append((win, ts, round(float(pct), 4), ra))
    return out


def live_plan(line):
    """The plan name the app fetched with a reading (optional "plan" key of a live line), or None."""
    try:
        name = json.loads(line).get("plan")
    except Exception:
        return None
    return name.strip()[:80] if isinstance(name, str) and name.strip() else None


def _insert_live(con, lines):
    """Complete lines (bytes) -> samples; the last plan name seen is remembered. Returns (new samples, bad lines)."""
    rows, bad, plan = [], 0, None
    for raw in lines:
        if not raw.strip():
            continue
        text = raw.decode("utf-8", "replace")
        got = parse_live(text)
        if not got:
            bad += 1
        rows += [(ACC, win, ts, pct, ra, "live") for win, ts, pct, ra in got]
        plan = live_plan(text) or plan
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    added = con.total_changes - before
    if plan:
        core.kv_set(con, PLAN_SEEN_KEY, plan)
    return added, bad


def import_live(con, path=LIVE, now=None, compact_bytes=LIVE_COMPACT_BYTES, kv_key="antigravity_live"):
    """The app's live readings -> samples(acc='antigravity', source='live'), from the byte offset reached last
    time (like scan.py does for logs). Only lines ending in a newline are taken: a line the app is still
    writing is read on the next pass. A new inode (the file was compacted or recreated) or a file shorter
    than the offset starts again from 0; INSERT OR IGNORE makes re-reading harmless.
    Returns the number of new samples, or "absent"."""
    st_kv = core.kv_get(con, kv_key) or {}
    try:
        st = os.stat(path)
    except OSError:
        return "absent"
    off = st_kv.get("offset", 0)
    if st_kv.get("ino") != st.st_ino or st.st_size < off:
        off = 0
    added = bad = 0
    if st.st_size > off:
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(st.st_size - off)
        end = chunk.rfind(b"\n") + 1          # 0 when no line is complete yet
        added, bad = _insert_live(con, chunk[:end].split(b"\n"))
        off += end
    st_kv = {"ino": st.st_ino, "offset": off, "bad": st_kv.get("bad", 0) + bad if st_kv.get("ino") == st.st_ino
             else bad, "compactedAt": st_kv.get("compactedAt")}
    if st.st_size > compact_bytes:
        res = compact_live(con, path, now or time.time())
        if res:
            st_kv.update(res)
    core.kv_set(con, kv_key, st_kv)
    con.commit()
    return added


def compact_live(con, path, now):
    """Rewrites the file without lines older than LIVE_KEEP_DAYS (and without broken lines), atomically:
    temp file in the same folder + rename, under the writer's lock file. The writer takes the same lock for
    each append, so no line can land in the old file between our read and the rename. Returns the new
    {"ino", "offset", "compactedAt"}, or None if the lock is busy (it is tried again on the next pass)."""
    with open(path + ".lock", "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
        lines = data[:data.rfind(b"\n") + 1].split(b"\n")
        _insert_live(con, lines)            # nothing may be lost, even lines the offset had not reached
        keep = []
        for raw in lines:
            got = parse_live(raw.decode("utf-8", "replace")) if raw.strip() else []
            if got and got[0][1] >= now - LIVE_KEEP_DAYS * 86400:
                keep.append(raw + b"\n")
        out = b"".join(keep)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(out)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return {"ino": os.stat(path).st_ino, "offset": len(out), "bad": 0, "compactedAt": int(now)}


def import_quota(con):
    """Both quota sources; what tick.py logs."""
    return {"history": import_history(con), "live": import_live(con)}


# -------------------------------------------------------------------------------- scan ----
def file_sig(path):
    s = os.stat(path)
    try:
        w = os.stat(path + "-wal")
        wal = [w.st_size, w.st_mtime_ns]
    except OSError:
        wal = [0, 0]
    return [s.st_size, s.st_mtime_ns, *wal]


def _load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


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
    if merged and merged.get("key") == key:
        events = [dict(zip(CALL_FIELDS, e)) for e in merged["events"]]
        return events, merged["stats"], {"cached": True, "rescanned": 0, "ms": round((time.time() - t0) * 1000)}

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
    return events, stats, {"cached": False, "rescanned": changed, "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------------------- analysis ----
def model_key(ev):
    """`m` and `models` key: the Gemini model id the call is priced as, else the recorded id."""
    return ev["priceAs"] or ev["model"]


def analysis_db(con, events):
    """In-memory database with the collector schema: this account's calls as `rows`, its samples copied.

    Plus one exact point per used window: 0 % at its start (reset time minus window length, source
    "start"). Antigravity starts a window with the first call after the previous one ran out (on this
    Mac the window start and the first call agreed within 2 s for 12 of 14 five-hour windows and 2 of 3
    weekly ones), so the start is a real zero. calib does the same for 5-hour windows it saw hit 100 %.
    These points exist only here, never in usage.db."""
    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    mem.executemany(
        "INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,fast,cost,acc) VALUES(?,?,?,?,?,?,0,?,0,?,?)",
        [(f"{i}", int(ev["ts"]), model_key(ev), ev["input"], ev["output"], ev["cacheWrite"], ev["cacheRead"],
          ev["usd"], ACC) for i, ev in enumerate(events)])
    mem.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", merged_samples(con))
    for win in ("5h", "7d"):
        span = calib.SPAN[win]
        mem.execute("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) "
                    "SELECT acc, win, resets_at-?, 0.0, resets_at, 'start' FROM samples "
                    "WHERE acc=? AND win=? AND resets_at IS NOT NULL GROUP BY resets_at "
                    "HAVING max(pct)>0 AND min(ts)>=resets_at-?", (span, ACC, win, span))
    return mem


def merged_samples(con):
    """This account's samples with each reading counted once. The CodexBar history keeps the peak of each
    clock hour, which is one of the readings the live file also has (same reading, same or nearly the same
    second). So in every (window, clock hour) that has at least one 'live' sample, the 'codexbar' samples
    are dropped; hours without live samples (the app was not running, or before the file existed) keep
    the CodexBar sample. Live samples are never dropped."""
    rows = con.execute("SELECT acc,win,ts,pct,resets_at,source FROM samples WHERE acc=?", (ACC,)).fetchall()
    live_hours = {(r[1], r[2] // 3600) for r in rows if r[5] == "live"}
    return [r for r in rows if not (r[5] == "codexbar" and (r[1], r[2] // 3600) in live_hours)]


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
    """Share of prompt tokens read from the cache, and what that saved / what the misses cost.
    A prompt token is read (cacheRead) or missed (input + cacheWrite: Gemini bills a cache write at the
    full input price). savedUsd = reads × (input price − cache-read price); missUsd = misses × the same
    difference, i.e. what the misses cost above the cache-read price. Calls without a price add tokens only."""
    acc = ccb.blank()
    read = miss = write = 0
    saved = missed = 0.0
    for ev in events:
        if ev["ts"] < since:
            continue
        ccb.add(acc, ev["cacheRead"], ev["input"], ev["cacheWrite"], 1.0, ccb.READ_X["gemini"])
        read += ev["cacheRead"]
        write += ev["cacheWrite"]
        miss += ev["input"] + ev["cacheWrite"]
        p = input_prices(ev)
        if p:
            saved += ev["cacheRead"] * (p[0] - p[1])
            missed += (ev["input"] + ev["cacheWrite"]) * (p[0] - p[1])
    r = ccb.rates(acc)
    out = {"missPct": None, "coldCallsPct": None, "calls": 0, "readPct": None}
    if r:
        out = {"missPct": r["missPct"], "coldCallsPct": r["coldCallsPct"], "calls": r["calls"],
               "readPct": round(100 - r["missPct"], 1)}
    out.update({"readTokens": read, "missTokens": miss, "writeTokens": write,
                "savedUsd": round(saved, 2), "missUsd": round(missed, 2)})
    return out


# ------------------------------------------------------------- rates, sessions, value ----
# How a reading can be off, in percentage points of a window. On 23.11.2026 the app's readings alternated
# between two values (e.g. 5-hour 6.26 / weekly 42.77 and 5.44 / 42.10 within three minutes, with no spend in
# between), and then fell to 4.30 / 41.16 by 17:52, although this Mac had made no Gemini call since 16:44:
# a reading taken during or soon after use can sit 2.0 (5-hour) and 1.6 (weekly) points above the settled
# one (in August: up to 1 point). The rate's bound holds only while JITTER covers that, hence 2.0.
JITTER = 2.0
# A stretch: consecutive readings of one window with at least this much of this Mac's spend between its ends.
STRETCH_USD = {"5h": 15.0, "7d": 60.0}
# Value is "confident" from this many clean weekly stretches and this much spend in them.
CONFIDENT_STRETCHES = 4
CONFIDENT_USD = 240.0
# A stretch counts for one model when that model is at least this share of its dollars.
PURE_SHARE = 0.9
# windows.7d.sessions: an interval counts when inside one 5-hour and one weekly window the 5-hour
# percentage moved at least D5 points and the weekly one at least D7 (both above JITTER);
# published from SESSION_PAIRS intervals with SESSION_SUM7 weekly points in all (so the jitter of one
# interval moves the answer by a sixth at most).
SESSION_D5, SESSION_D7 = 5.0, 3.0
SESSION_PAIRS, SESSION_SUM7 = 3, 12.0
WEEKS_PER_MONTH = 30.44 / 7
# Plan: the app writes the plan name it fetched into its live readings; the price comes from the list below.
# The owner's file overrides both (see ANTIGRAVITY-STATE.md, Plan).
PLAN_FILE = f"{core.DATA}/antigravity-plan.json"
PLAN_SEEN_KEY = "antigravity_plan_seen"
# Monthly list prices of the Google AI plans (USD, as of November 2026). First match wins: every word of
# the key must be in the plan name. A name that matches nothing has no price.
PLAN_PRICES = (("ultra 20x", 199.99), ("ultra 5x", 99.99), ("pro", 19.99))
# rates.*.method, the page's one sentence ({usd}: STRETCH_USD of that window)
METHOD = ("From stretches with ${usd:.0f}+ of this Mac's Gemini spend between two readings; a stretch that moved "
          "the limit more than that spend explains (phone, chat app), or much less than the others agree on, "
          "is left out.")
# A fall larger than this between readings of one window is a reset (or, for sessions, a switch of quota
# group), not a reading settling, which falls by up to JITTER. Passed to calib as its reset rule
# (calib.RESET_DROP, 1.5 points, is made for Claude): with 1.5, a reading that settled by 1.6 points in one
# step would move windows.*.start to that moment and cut the week's dollars and peak there.
DROP_PTS = 2 * JITTER


class ModelSpend:
    """Cumulative dollars of this Mac's calls, in all and per model key: spend between two times."""

    def __init__(self, events):
        self.t, self.c = [], []
        self.m = {}
        tot = 0.0
        run = {}
        for ev in sorted(events, key=lambda e: e["ts"]):
            usd = ev["usd"] or 0.0
            if usd <= 0:
                continue
            ts, k = int(ev["ts"]), model_key(ev)
            tot += usd
            run[k] = run.get(k, 0.0) + usd
            self.t.append(ts)
            self.c.append(tot)
            mt, mc = self.m.setdefault(k, ([], []))
            mt.append(ts)
            mc.append(run[k])

    @staticmethod
    def _at(ts_list, cum, t):
        i = bisect.bisect_right(ts_list, t) - 1
        return cum[i] if i >= 0 else 0.0

    def between(self, t0, t1):
        return self._at(self.t, self.c, t1) - self._at(self.t, self.c, t0)

    def by_model(self, t0, t1):
        return {k: self._at(mt, mc, t1) - self._at(mt, mc, t0) for k, (mt, mc) in self.m.items()}


def stretches(mem, ms, win, t0):
    """Disjoint stretches of one window type: walk the readings of each window (the exact 0 % at its
    start included) and close a stretch at the first reading with STRETCH_USD of spend since its start.
    Each is {"t0", "t1", "dp" (points the limit moved), "usd" (this Mac's dollars), "byModel"}.
    Disjoint on purpose: overlapping pairs of readings would count one stretch of use many times over."""
    out = []
    for seg in calib.segments(calib.load_points(mem, ACC, win, t0), DROP_PTS):
        i = 0
        for j in range(1, len(seg)):
            usd = ms.between(seg[i][0], seg[j][0])
            if usd >= STRETCH_USD[win]:
                out.append({"t0": seg[i][0], "t1": seg[j][0], "dp": seg[j][1] - seg[i][1], "usd": usd,
                            "byModel": ms.by_model(seg[i][0], seg[j][0])})
                i = j
    return out


def estimate(st, win="7d"):
    """Percent of the window per dollar of this Mac's spend, from disjoint stretches.

    Use elsewhere (phone, chat app) only ever adds to how far the limit moved, so every stretch caps the
    true rate at (dp + JITTER) / usd; read with the jitter the other way, (dp - JITTER) / usd is its floor.
    Two stretches agree when their floor-to-cap ranges overlap. `upper` is the lowest cap that at least one
    other stretch agrees with (3 % of them from 67 stretches on), so one stretch alone cannot pull it down:
    a reading off by more than the jitter, calls dated wrongly, spend that the shown quota does not count.
    With no agreeing pair (one stretch, or all apart) it is the lowest cap. A stretch is clean when its
    range holds `upper`; above it the limit moved more than this Mac's spend explains (`pairsDropped`),
    below it much less than the clean ones agree on (`pairsLow`). The rate is the pooled clean rate,
    sum(dp) / sum(usd), capped at `upper`: each new clean stretch narrows it, a dropped one changes nothing.
    Returns None without a clean stretch that moved the limit."""
    if not st:
        return None
    cap = [(s["dp"] + JITTER) / s["usd"] for s in st]
    floor = [(s["dp"] - JITTER) / s["usd"] for s in st]
    need = max(1, int(len(st) * calib.Q))
    agreed = [c for i, c in enumerate(cap)
              if sum(j != i and floor[j] <= c and floor[i] <= cap[j] for j in range(len(st))) >= need]
    upper = min(agreed or cap)
    if upper <= 0:
        return None
    clean = [s for s, f, c in zip(st, floor, cap) if f <= upper <= c]
    dp, usd = sum(s["dp"] for s in clean), sum(s["usd"] for s in clean)
    if dp <= 0 or usd <= 0:
        return None
    r = min(upper, dp / usd)
    each = sorted(100 * s["usd"] / s["dp"] for s in clean if s["dp"] > 0)
    return {"pct_per_usd": r, "usd_per_100": 100 / r, "pairs": len(clean), "source": "own",
            "method": METHOD.format(usd=STRETCH_USD[win]), "pairsUsed": len(clean),
            "pairsDropped": sum(f > upper for f in floor), "pairsLow": sum(c < upper for c in cap),
            "range": [round(each[0]), round(each[-1])] if each else None, "usd": round(usd, 2)}


def sessions(mem, t0):
    """How many full 5-hour windows one weekly window holds, straight from the readings.

    Both percentages count the same use, wherever it happened, so the ratio of their moves is not thrown off
    by use outside this Mac (the dollar rates are). For every pair (5-hour window, weekly window) the first
    and the last reading taken inside both give one interval; it counts when the 5-hour percentage moved
    SESSION_D5+ points and the weekly one SESSION_D7+. perWeek = sum of 5-hour moves / sum of weekly moves
    (a full 5-hour window is 100 points, so it costs 100 × d7/d5 weekly points). An interval in which either
    percentage drops by more than DROP_PTS is skipped: a reset that keeps the reset time (as Claude had on
    14.11.2026) or a switch to another quota group would make "last minus first" wrong. Scheduled resets
    need no rule: a new reset time is a new window, so an interval never spans one. Returns (sessions or
    None, intervals, perWeek so far or None)."""
    snaps = {}
    for win, ts, pct, ra, src in mem.execute(
            "SELECT win,ts,pct,resets_at,source FROM samples WHERE acc=? AND ts>=? AND source!='start' "
            "AND resets_at IS NOT NULL AND win IN ('5h','7d')", (ACC, t0)):
        snaps.setdefault((ts, src), {})[win] = (pct, (ra + 150) // 300)
    groups = {}
    for (ts, _), w in sorted(snaps.items()):
        if "5h" in w and "7d" in w:
            groups.setdefault((w["5h"][1], w["7d"][1]), []).append((ts, w["5h"][0], w["7d"][0]))
    used = []
    for g in groups.values():
        if any(b[1] < a[1] - DROP_PTS or b[2] < a[2] - DROP_PTS for a, b in zip(g, g[1:])):
            continue
        d5, d7 = g[-1][1] - g[0][1], g[-1][2] - g[0][2]
        if d5 >= SESSION_D5 and d7 >= SESSION_D7:
            used.append((d5, d7))
    s5, s7 = sum(u[0] for u in used), sum(u[1] for u in used)
    so_far = round(s5 / s7, 1) if s7 > 0 else None
    if len(used) < SESSION_PAIRS or s7 < SESSION_SUM7:
        return None, len(used), so_far
    return {"perWeek": so_far, "pairs": len(used)}, len(used), so_far


def plan_price(name):
    """List price of a plan by its name, None when the name is not in PLAN_PRICES."""
    words = (name or "").lower().replace("×", "x").split()
    for key, price in PLAN_PRICES:
        if all(w in words for w in key.split()):
            return price
    return None


def read_plan(path=PLAN_FILE, seen=None):
    """(planName, planPriceUsd). The owner's file wins, field by field; otherwise the name the app saw
    (`seen`) and its list price. (None, None) when nothing is known."""
    p = _load(path)
    p = p if isinstance(p, dict) else {}
    name = p.get("planName") if isinstance(p.get("planName"), str) and p.get("planName").strip() else None
    price = p.get("planPriceUsd")
    price = float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None
    name = name or (seen if isinstance(seen, str) and seen.strip() else None)
    return name, price if price is not None else plan_price(name)


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


def value_block(rate, st, plan, models):
    """state["antigravity"]["value"] from the weekly rate (see ANTIGRAVITY-STATE.md, Value)."""
    name, price = plan
    week = rate["usd_per_100"]
    month = week * WEEKS_PER_MONTH
    used, usd = rate["pairsUsed"], rate["usd"]
    confident = used >= CONFIDENT_STRETCHES and usd >= CONFIDENT_USD
    reason = None if confident else (
        f"Measured {min(used, CONFIDENT_STRETCHES - 1)} of {CONFIDENT_STRETCHES} times so far. "
        f"Each measurement takes about ${STRETCH_USD['7d']:.0f} of use on this Mac.")
    labels = model_labels(list(models))
    by_model = []
    for k in models:   # last 30 days, most dollars first
        own = estimate([s for s in st if s["usd"] and s["byModel"].get(k, 0) >= PURE_SHARE * s["usd"]])
        m_month = own["usd_per_100"] * WEEKS_PER_MONTH if own else None
        by_model.append({"model": k, "label": labels[k], "monthUsd": round(m_month) if own else None,
                         "timesPrice": round(m_month / price, 1) if own and price else None,
                         "pairs": own["pairsUsed"] if own else 0})
    return {"planName": name, "planPriceUsd": price, "weekUsd": round(week), "monthUsd": round(month),
            "timesPrice": round(month / price, 1) if price else None, "pairs": used,
            "confident": confident, "reason": reason, "byModel": by_model,
            "weekOpusUsd": round(week), "monthOpusUsd": round(month)}


def quota_source(con, now):
    q = {"app": "CodexBar", "file": tilde(HISTORY), "fileModifiedAt": None, "lastSampleAt": None, "ageSec": None,
         "cadence": "CodexBar keeps at most the peak sample of each clock hour",
         "weekly": "the weekly quota of Antigravity's Gemini model group (older CodexBar history may hold the "
                   "highest weekly quota of any group)",
         "accountsInFile": core.kv_get(con, "antigravity_history_accounts"), "samples": {}}
    try:
        q["fileModifiedAt"] = int(os.path.getmtime(HISTORY))
    except OSError:
        pass
    today0 = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    for win in ("5h", "7d"):
        n, first, last, today = con.execute(
            "SELECT count(*), min(ts), max(ts), sum(ts>=?) FROM samples WHERE acc=? AND win=?",
            (today0, ACC, win)).fetchone()
        q["samples"][win] = {"count": n, "first": first, "last": last, "today": today or 0}
    last = max((s["last"] for s in q["samples"].values() if s["last"]), default=None)
    q["lastSampleAt"] = last
    q["ageSec"] = now - last if last else None
    # per source: "codexbar" = CodexBar's hourly history, "live" = the app's live readings file
    q["bySource"] = {}
    for src, win, n, first, last_s in con.execute(
            "SELECT source, win, count(*), min(ts), max(ts) FROM samples WHERE acc=? GROUP BY source, win", (ACC,)):
        q["bySource"].setdefault(src, {})[win] = {"count": n, "first": first, "last": last_s}
    r = con.execute("SELECT source FROM samples WHERE acc=? ORDER BY ts DESC, source='live' DESC LIMIT 1",
                    (ACC,)).fetchone()
    q["lastSource"] = r[0] if r else None
    lv = core.kv_get(con, "antigravity_live") or {}
    q["liveFile"] = tilde(LIVE)
    q["liveFileExists"] = os.path.exists(LIVE)
    q["liveBadLines"] = lv.get("bad", 0)
    q["liveCompactedAt"] = lv.get("compactedAt")
    return q


def build(con, now, home=GEMINI_HOME, cache_path=SCAN_CACHE):
    events, stats, scan_info = scan(home, cache_path)
    mem = analysis_db(con, events)
    fam = model_key_identity
    sp = calib.Spend(mem, ACC, fam)
    t0 = now - calib.LOOKBACK
    pairs = {"fable": []}
    for win in ("7d", "5h"):
        pairs[win] = []
        for seg in calib.segments(calib.load_points(mem, ACC, win, t0), DROP_PTS):
            pairs[win] += calib.seg_pairs(sp, seg)
    # Gemini dollars are not weighted: "weighted spend" is plain spend, k = 1
    kx = {"k": 1.0, "k5": 1.0, "source": "none", "from": "none", "pairs": 0, "fit": None,
          "source5": "none", "from5": "none"}
    bands = {}
    for kind in ("5h", "7d"):
        b = calib.forecast_band(calib.past_windows(mem, ACC, kind, now, DROP_PTS), kind)
        bands[(ACC, kind)] = (b, "own") if b else (None, "default")
    a = {"uuid": ACC, "name": NAME, "short": NAME, "active": False, "useHapp": False, "orgs": []}
    info = calib.account_state(mem, a, sp, pairs, kx, bands, now, fam=fam, drop=DROP_PTS)
    # Rates: disjoint stretches with polluted ones dropped (estimate()), instead of calib's envelope over
    # overlapping pairs. The windows' sinceMeasure follows the new rate.
    ms = ModelSpend(events)
    st = {win: stretches(mem, ms, win, t0) for win in ("7d", "5h")}
    info["rates"] = {win: r for win in ("7d", "5h") if (r := estimate(st[win], win))}
    for win, w in info["windows"].items():
        r = info["rates"].get(win)
        if r and w.get("start") and not w.get("rolled"):
            w["sinceMeasure"] = round(max(0.0, r["pct_per_usd"] * ms.between(w["measuredAt"], now)), 1)
        else:
            w.pop("sinceMeasure", None)
    ses, ses_n, ses_so_far = sessions(mem, t0)
    if "7d" in info["windows"]:
        info["windows"]["7d"]["sessions"] = ses
    # the name the Swift Account already decodes: one full 5-hour window as a share of the week
    info["sessionPctOfWeek"] = round(100 / ses["perWeek"], 2) if ses else None
    mem.close()

    for k in DROP:
        info.pop(k, None)
    for w in info["windows"].values():
        for k in DROP_WINDOW:
            w.pop(k, None)
        if "series" in w:
            w["series"] = [[p[0], p[1], None] for p in w["series"]]
    for wk in info.get("weeks") or []:
        for k in DROP_WEEK:
            wk.pop(k, None)
    q = quota_source(con, now)
    info["beta"] = True
    info["kFable"] = 1.0
    info["lastPoll"] = None
    info["lastMeasuredAt"] = q["lastSampleAt"]
    info["has5h"] = True if q["samples"]["5h"]["count"] else (False if q["samples"]["7d"]["count"] else None)
    plan = read_plan(seen=core.kv_get(con, PLAN_SEEN_KEY))
    info["planName"], info["planPriceUsd"] = plan
    info["models"] = models_30d(events, now)

    r7 = info["rates"].get("7d")
    if r7:
        info["value"] = value_block(r7, st["7d"], plan, info["models"])
        info["valueReason"] = None
    else:
        info["value"] = None
        info["valueReason"] = (f"the weekly rate needs one stretch with ${STRETCH_USD['7d']:.0f}+ of this Mac's "
                               f"spend between two readings of the weekly limit (last {calib.LOOKBACK // 86400} "
                               f"days); have {len(st['7d'])}, none that this Mac's spend explains"
                               if st["7d"] else
                               f"the weekly rate needs one stretch with ${STRETCH_USD['7d']:.0f}+ of this Mac's "
                               f"spend between two readings of the weekly limit (last {calib.LOOKBACK // 86400} "
                               f"days); have 0")
    info["calibration"] = {
        "stretches": {win: len(st[win]) for win in st},
        "sessionIntervals": ses_n, "sessionPerWeekSoFar": ses_so_far,
        "thresholds": {"jitterPts": JITTER, "stretchUsd": STRETCH_USD, "confidentStretches": CONFIDENT_STRETCHES,
                       "confidentUsd": CONFIDENT_USD, "sessionMinPts": {"5h": SESSION_D5, "7d": SESSION_D7},
                       "sessionPairs": SESSION_PAIRS, "sessionSum7dPts": SESSION_SUM7},
        "planFile": tilde(PLAN_FILE), "planFileExists": os.path.exists(PLAN_FILE)}
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    info["cache"] = {"today": cache_stats(events, today0), "7d": cache_stats(events, now - 7 * 86400),
                     "30d": cache_stats(events, now - 30 * 86400)}
    ts = stats.get("timeSources") or {}
    info["coverage"] = {
        "tokens": "exact", "usd": "estimate",
        "historySince": stats["historySpan"][0] if stats.get("historySpan") else None,
        "dbFiles": stats["dbFiles"], "calls": len(events), "copiesDropped": stats["copiesDropped"],
        "encryptedConversations": stats["encryptedPbFiles"],
        "timeEstimatedCalls": sum(v for k, v in ts.items() if k not in ("row", "steps")),
        "outputCheckFailed": stats["outputCheckFailed"],
        "unreadableDbs": stats["dbUnreadable"], "malformedRows": stats["rowsMalformed"],
        "unpricedCalls30d": sum(m["unpricedCalls"] for m in info["models"].values()),
        "unconfirmedPrices": sorted(k for k in info["models"] if k in agb.UNCONFIRMED),
        "pricesChecked": agb.PRICES_CHECKED, "priceSource": agb.PRICE_SOURCE,
        "notCounted": "encrypted .pb conversations, deleted conversations, Tab completions, other devices",
        "scanCached": scan_info["cached"], "scanRescannedFiles": scan_info["rescanned"], "scanMs": scan_info["ms"],
    }
    info["quotaSource"] = q
    return info


def model_key_identity(model):
    """Family function for calib: rows already hold the model key, so it is its own family
    (never "opus" or "fable", so Spend puts every dollar into "other")."""
    return model


def block(con, now=None):
    """(state["antigravity"], short log entry). Never raises."""
    now = int(now or time.time())
    t0 = time.time()
    try:
        info = build(con, now)
        c = info["coverage"]
        return info, {"ms": round((time.time() - t0) * 1000), "scan": "cached" if c["scanCached"] else
                      f"read {c['scanRescannedFiles']} files", "calls": c["calls"],
                      "rates": sorted(info["rates"])}
    except Exception:
        err = traceback.format_exc(limit=2).strip().splitlines()[-1]
        return {"uuid": ACC, "name": NAME, "short": NAME, "beta": True, "error": err}, "error: " + err


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
        say(not any(k in w for k in DROP_WINDOW), f"window {win} has no mine/outside/server")
        say(all(len(p) == 3 and p[2] is None for p in w.get("series", [])), f"window {win} series third column null")
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
