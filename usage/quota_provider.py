#!/usr/bin/env python3
"""Shared part of the one-quota provider blocks of the summary: `state["antigravity"]` and `state["codex"]`.

Both providers have one quota (a 5-hour and a weekly window) that counts every use of the account, wherever
it happens, and a local log of this Mac's calls priced at API list prices. So both blocks are built the same
way, by the functions here, from a `Provider` (paths, names, texts) and a list of priced calls:

  * quota: CodexBar's hourly history (`history/<provider>.json`) and the app's live readings file
    (`<provider>-quota.jsonl`), imported into usage.db `samples` as account `<provider>`
    (import_history, import_live, compact_live), merged so that every reading counts once (merged_samples);
  * windows, series, forecast, drift, daily, weeks: calib.account_state() on an in-memory database that
    holds the provider's calls as `rows` (analysis_db), so the calls never enter usage.db;
  * rates, value, sessions: disjoint stretches with polluted ones dropped (estimate, sessions, value_block).

The method is described in ANTIGRAVITY-STATE.md (sections Rate, Sessions per week, Value, Live readings
file); CODEX-STATE.md says "same as Antigravity" for all of it.

A call ("event") is a dict with at least: ts, model, priceAs (the priced model id or None), effort, input
(uncached prompt tokens), cacheRead, cacheWrite, output, thinking (inside output), usd (or None), day.
"""
import bisect
import fcntl
import hashlib
import json
import os
import sqlite3
import time

import calib
import core
import split_attrib

# How a reading can be off, in percentage points of a window. On 23.11.2026 Antigravity's readings alternated
# between two values (e.g. 5-hour 6.26 / weekly 42.77 and 5.44 / 42.10 within three minutes, with no spend in
# between), and then fell to 4.30 / 41.16 by 17:52, although this Mac had made no Gemini call since 16:44:
# a reading taken during or soon after use can sit 2.0 (5-hour) and 1.6 (weekly) points above the settled
# one (in August: up to 1 point). The rate's bound holds only while JITTER covers that, hence 2.0.
# Codex readings are whole percents, so they are off by up to 1 point from rounding alone.
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
# A fall larger than this between readings of one window is a reset (or, for sessions, a switch of quota
# group), not a reading settling, which falls by up to JITTER. Passed to calib as its reset rule
# (calib.RESET_DROP, 1.5 points, is made for Claude): with 1.5, a reading that settled by 1.6 points in one
# step would move windows.*.start to that moment and cut the week's dollars and peak there.
DROP_PTS = split_attrib.DROP_PTS
# A reading at or above this is at the top of the limit (Codex readings are whole percents, Antigravity's are
# not): from here on more use cannot move the reading, so no stretch may end here.
SATURATED = split_attrib.SATURATED
WEEK_RESET_TOL = split_attrib.WEEK_RESET_TOL
LIVE_KEEP_DAYS = 40
LIVE_COMPACT_BYTES = 1_000_000
WINS = {"session": "5h", "weekly": "7d"}
# keys of a Claude account that mean nothing here (see the contract)
DROP = ("orgs", "plan", "org", "tier", "fableShareOfWeek", "kSource", "kFrom", "kPairs", "kFit", "kFable5h",
        "kSource5h", "kFrom5h", "hits")
DROP_WINDOW = ("server",)
# Replace Claude's attribution on weekly rows with the provider's three-way split.
DROP_WEEK = ("minePct", "minePctByModel", "othersPct")
METHOD = ("From stretches with ${usd:.0f}+ of this Mac's {spend} spend between two readings; a stretch that moved "
          "the limit more than that spend explains ({elsewhere}), or much less than the others agree on, "
          "is left out.")


class Provider:
    """What differs between the providers: names, files, kv keys, plan prices and the page's wording."""

    def __init__(self, acc, name, history, live, plan_file, plan_prices, spend_word, elsewhere, weekly_text,
                 labels, stretch_usd=None, confident_usd=CONFIDENT_USD, account_id=None):
        self.acc = acc                    # samples.acc and the block's uuid
        self.name = name
        self.history = history            # CodexBar's hourly history file
        self.live = live                  # the app's live readings file
        self.live_key = f"{acc}_live"     # kv: offset, inode, bad lines, compaction time of the live file
        self.plan_file = plan_file        # the owner's plan override
        self.plan_seen_key = f"{acc}_plan_seen"
        self.account_key = f"{acc}_account"   # kv: account_hash() of the current CodexBar account
        self.legacy_key = f"{acc}_account_legacy"   # kv: the account live lines without "account" belong to
        # account key -> the id the provider's own logs name the account by (Codex: creator_account_id), or
        # None; kv account_id_key holds account_hash() of it for the current account (calibration_tags)
        self.account_id = account_id
        self.account_id_key = f"{acc}_account_id"
        self.plan_prices = plan_prices    # ((words, monthly USD), ...): first match wins
        self.spend_word = spend_word      # "Gemini", "Codex": rates.*.method
        self.elsewhere = elsewhere        # where outside use comes from, for rates.*.method
        self.weekly_text = weekly_text    # quotaSource.weekly
        self.labels = labels              # [model id] -> {id: short name}
        # dollars of this Mac's spend that close a stretch, and that value needs to be "confident": they must
        # move the limit far more than JITTER, so a provider with a small limit (Codex on Plus) needs less
        self.stretch_usd = dict(stretch_usd or STRETCH_USD)
        self.confident_usd = confident_usd
        # {"planName", "planPriceUsd"} used instead of plan_file (read_plan), or None: a per-account block
        # (codex_accounts.py) resolves its own plan, the owner's file describes the current account only
        self.plan_override = None


def tilde(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


# ------------------------------------------------------------------------------- quota ----
def account_hash(key):
    """Short opaque name of a CodexBar account key (the key itself never leaves usage.db's kv)."""
    return hashlib.sha256(key.encode()).hexdigest()[:12] if isinstance(key, str) and key else None


def use_account(p, con, key):
    """Makes `key` (CodexBar's preferredAccountKey for this provider) the account whose samples are
    samples(acc=p.acc). On a change the stored samples are parked under acc "<p.acc>:<hash of the old key>",
    the new account's parked samples come back, and the live file is read again from its start with the new
    account filter. The first account seen adopts the samples already stored (only the preferred account
    was ever imported). Returns True when the account changed."""
    h = account_hash(key)
    if h is None:
        return False
    if p.account_id:
        aid = account_hash(p.account_id(key))
        if core.kv_get(con, p.account_id_key) != aid:
            core.kv_set(con, p.account_id_key, aid)
    old = core.kv_get(con, p.account_key)
    if old == h:
        return False
    core.kv_set(con, p.account_key, h)
    if old is None:
        core.kv_set(con, p.legacy_key, h)   # lines without "account" were written for this one
        _restore_account(p, con, h)         # what _insert_live parked for it while no account was known
        con.commit()
        return False
    _park_account(p, con, old)
    _restore_account(p, con, h)
    lv = core.kv_get(con, p.live_key) or {}
    if lv:
        core.kv_set(con, p.live_key, {**lv, "offset": 0})
    con.commit()
    return True


def _park_account(p, con, h):
    """Moves the current account's samples to acc "<p.acc>:<h>" and its plan name to "<plan_seen_key>@<h>"."""
    con.execute("UPDATE OR IGNORE samples SET acc=? WHERE acc=?", (f"{p.acc}:{h}", p.acc))
    con.execute("DELETE FROM samples WHERE acc=?", (p.acc,))
    plan = core.kv_get(con, p.plan_seen_key)
    if plan is not None:
        core.kv_set(con, f"{p.plan_seen_key}@{h}", plan)
    con.execute("DELETE FROM kv WHERE k=?", (p.plan_seen_key,))


def _restore_account(p, con, h):
    """Brings back what was parked for account hash `h` (samples, plan name): it becomes the current one's."""
    con.execute("UPDATE OR IGNORE samples SET acc=? WHERE acc=?", (p.acc, f"{p.acc}:{h}"))
    con.execute("DELETE FROM samples WHERE acc=?", (f"{p.acc}:{h}",))
    plan = core.kv_get(con, f"{p.plan_seen_key}@{h}")
    if plan is not None:
        core.kv_set(con, p.plan_seen_key, plan)
        con.execute("DELETE FROM kv WHERE k=?", (f"{p.plan_seen_key}@{h}",))


def import_history(p, con):
    """CodexBar's quota history -> samples(acc=p.acc, win='5h'|'7d', source='codexbar'), of the account
    CodexBar prefers (preferredAccountKey) only; see use_account() for a change of that account.
    Skipped when the file has not changed since the last import. Returns the number of new samples."""
    try:
        st = os.stat(p.history)
    except OSError:
        return "absent"
    sig = [st.st_size, st.st_mtime_ns]
    # (once after the account rule came, and once more for the account id: read the unchanged file too)
    if core.kv_get(con, f"{p.acc}_history_sig") == sig and core.kv_get(con, f"{p.acc}_history_v") == 3:
        return 0
    with open(p.history) as fh:
        h = json.load(fh)
    core.kv_set(con, f"{p.acc}_history_v", 3)
    accounts = h.get("accounts") or {}
    key = h.get("preferredAccountKey") if h.get("preferredAccountKey") in accounts else (
        next(iter(accounts)) if len(accounts) == 1 else None)
    use_account(p, con, key)
    series = accounts.get(key) if key else h.get("unscoped") or []
    rows = history_rows(p.acc, series)
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    added = con.total_changes - before
    core.kv_set(con, f"{p.acc}_history_sig", sig)
    core.kv_set(con, f"{p.acc}_history_accounts", len(accounts))
    con.commit()
    return added


def history_rows(acc, series):
    """One account's series of CodexBar's history file -> samples rows (acc, win, ts, pct, resets_at, 'codexbar');
    lanes other than `session` and `weekly` are skipped."""
    rows = []
    for s in series or []:
        win = WINS.get(s.get("name")) if isinstance(s, dict) else None
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
            rows.append((acc, win, ts, round(pct, 4), ra, "codexbar"))
    return rows


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


def live_account(line):
    """The CodexBar account key of a live line (optional "account" key), or None: a line from before the
    key existed, taken as the account that was current when the collector first saw one (use_account)."""
    try:
        acc = json.loads(line).get("account")
    except Exception:
        return None
    return acc if isinstance(acc, str) and acc else None


def live_plan(line):
    """The plan name the app fetched with a reading (optional "plan" key of a live line), or None."""
    try:
        name = json.loads(line).get("plan")
    except Exception:
        return None
    return name.strip()[:80] if isinstance(name, str) and name.strip() else None


def _insert_live(p, con, lines):
    """Complete lines (bytes) -> samples; the last plan name seen is remembered. A line of another account
    than the current one (use_account) is skipped. While no account is known yet (no CodexBar history to
    name it), a line that names its account is kept apart under acc "<p.acc>:<hash>" with its plan under
    "<plan_seen_key>@<hash>", and the account of the last such line becomes the current one (use_account
    brings its samples back; another account's stay parked). Returns (new samples, bad lines)."""
    rows, bad, plans, last = [], 0, {}, None
    cur = core.kv_get(con, p.account_key)
    legacy_ok = cur is None or core.kv_get(con, p.legacy_key) in (None, cur)
    for raw in lines:
        if not raw.strip():
            continue
        text = raw.decode("utf-8", "replace")
        got = parse_live(text)
        if not got:
            bad += 1
        acc = live_account(text)
        if (acc and cur and account_hash(acc) != cur) or (not acc and not legacy_ok):
            continue
        h = account_hash(acc) if acc and not cur else None
        if h:
            last = acc
        rows += [(f"{p.acc}:{h}" if h else p.acc, win, ts, pct, ra, "live") for win, ts, pct, ra in got]
        if live_plan(text):
            plans[h] = live_plan(text)
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    added = con.total_changes - before
    for h, plan in plans.items():
        core.kv_set(con, f"{p.plan_seen_key}@{h}" if h else p.plan_seen_key, plan)
    if last:
        use_account(p, con, last)
    return added, bad


def import_live(p, con, path=None, now=None, compact_bytes=LIVE_COMPACT_BYTES, kv_key=None):
    """The app's live readings -> samples(acc=p.acc, source='live'), from the byte offset reached last
    time (like scan.py does for logs). Only lines ending in a newline are taken: a line the app is still
    writing is read on the next pass. A new inode (the file was compacted or recreated) or a file shorter
    than the offset starts again from 0; INSERT OR IGNORE makes re-reading harmless.
    Returns the number of new samples, or "absent"."""
    path = path or p.live
    kv_key = kv_key or p.live_key
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
        added, bad = _insert_live(p, con, chunk[:end].split(b"\n"))
        off += end
    st_kv = {"ino": st.st_ino, "offset": off, "bad": st_kv.get("bad", 0) + bad if st_kv.get("ino") == st.st_ino
             else bad, "compactedAt": st_kv.get("compactedAt")}
    if st.st_size > compact_bytes:
        res = compact_live(p, con, path, now or time.time())
        if res:
            st_kv.update(res)
    core.kv_set(con, kv_key, st_kv)
    con.commit()
    return added


def compact_live(p, con, path, now):
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
        _insert_live(p, con, lines)            # nothing may be lost, even lines the offset had not reached
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


def import_quota(p, con):
    """Both quota sources; what tick.py logs."""
    return {"history": import_history(p, con), "live": import_live(p, con)}


# ---------------------------------------------------------------------------- analysis ----
def model_key(ev):
    """`m` and `models` key: the model id the call is priced as, else the recorded id."""
    return ev["priceAs"] or ev["model"]


def model_key_identity(model):
    """Family function for calib: rows already hold the model key, so it is its own family
    (never "opus" or "fable", so Spend puts every dollar into "other")."""
    return model


def merged_samples(p, con):
    """This account's samples with each reading counted once. The CodexBar history keeps the peak of each
    clock hour, which is one of the readings the live file also has (same reading, same or nearly the same
    second). So in every (window, clock hour) that has at least one 'live' sample, the 'codexbar' samples
    are dropped; hours without live samples (the app was not running, or before the file existed) keep
    the CodexBar sample. Live samples are never dropped."""
    rows = con.execute("SELECT acc,win,ts,pct,resets_at,source FROM samples WHERE acc=?", (p.acc,)).fetchall()
    live_hours = {(r[1], r[2] // 3600) for r in rows if r[5] == "live"}
    return [r for r in rows if not (r[5] == "codexbar" and (r[1], r[2] // 3600) in live_hours)]


def analysis_db(p, con, events, since=None):
    """In-memory database with the collector schema: this account's calls as `rows`, its samples copied
    (from `since` on, when given: readings of an earlier plan measure another limit).

    Plus one exact point per used window: 0 % at its start (reset time minus window length, source
    "start"). Both providers start a window with the first call after the previous one ran out (for
    Antigravity on this Mac the window start and the first call agreed within 2 s for 12 of 14 five-hour
    windows and 2 of 3 weekly ones), so the start is a real zero. calib does the same for 5-hour windows
    it saw hit 100 %. These points exist only here, never in usage.db."""
    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    mem.executemany(
        "INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,fast,cost,acc) VALUES(?,?,?,?,?,?,0,?,0,?,?)",
        [(f"{i}", int(ev["ts"]), model_key(ev), ev["input"], ev["output"], ev["cacheWrite"], ev["cacheRead"],
          ev["usd"], p.acc) for i, ev in enumerate(events)])
    t_min = since or 0
    mem.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                    [r for r in merged_samples(p, con) if r[2] >= t_min])
    for win in ("5h", "7d"):
        span = calib.SPAN[win]
        mem.execute("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) "
                    "SELECT acc, win, resets_at-?, 0.0, resets_at, 'start' FROM samples "
                    "WHERE acc=? AND win=? AND resets_at IS NOT NULL AND resets_at-?>=? GROUP BY resets_at "
                    "HAVING max(pct)>0 AND min(ts)>=resets_at-?", (span, p.acc, win, span, t_min, span))
    return mem


def cache_stats(events, since, prices, read_x):
    """Share of prompt tokens read from the cache, and what that saved / what the misses cost.
    A prompt token is read (cacheRead) or missed (input + cacheWrite). savedUsd = reads × (input price −
    cache-read price); missUsd = misses × the same difference, i.e. what the misses cost above the
    cache-read price. prices(ev) -> (input, cache read) in $ per token, or None: such calls add tokens only."""
    import cache_compare_beta as ccb
    acc = ccb.blank()
    read = miss = write = 0
    saved = missed = 0.0
    for ev in events:
        if ev["ts"] < since:
            continue
        ccb.add(acc, ev["cacheRead"], ev["input"], ev["cacheWrite"], 1.0, read_x)
        read += ev["cacheRead"]
        write += ev["cacheWrite"]
        miss += ev["input"] + ev["cacheWrite"]
        pr = prices(ev)
        if pr:
            saved += ev["cacheRead"] * (pr[0] - pr[1])
            missed += (ev["input"] + ev["cacheWrite"]) * (pr[0] - pr[1])
    r = ccb.rates(acc)
    out = {"missPct": None, "coldCallsPct": None, "calls": 0, "readPct": None}
    if r and read + miss:
        # both shares from the integer counts, to 2 decimals (93.49 % must not become 93.5 and then 94)
        out = {"missPct": round(100 * miss / (read + miss), 2), "coldCallsPct": r["coldCallsPct"],
               "calls": r["calls"], "readPct": round(100 * read / (read + miss), 2)}
    out.update({"readTokens": read, "missTokens": miss, "writeTokens": write,
                "savedUsd": round(saved, 2), "missUsd": round(missed, 2)})
    return out


# ------------------------------------------------------------- rates, sessions, value ----
class ModelSpend:
    """Cumulative dollars of this Mac's calls, in all and per model key: spend between two times."""

    def __init__(self, events, t0=None, key="usd"):
        """t0: calls before it are left out (between() is only asked about later times). key: the dollars
        summed, "usd" (API prices) or "qusd" (what the quota counts, see quota_usd)."""
        self.t, self.c = [], []
        self.m = {}
        tot = 0.0
        run = {}
        for ev in sorted(events, key=lambda e: e["ts"]):
            usd = ev.get(key, ev["usd"]) or 0.0
            if usd <= 0 or (t0 is not None and ev["ts"] < t0):
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


def stretches(p, mem, ms, win, t0, resets=()):
    """Disjoint stretches of one window type: walk the readings of each window (the exact 0 % at its
    start included) and close a stretch at the first reading with STRETCH_USD of spend since its start.
    Each is {"t0", "t1", "dp" (points the limit moved), "usd" (this Mac's dollars), "byModel"}.
    Disjoint on purpose: overlapping pairs of readings would count one stretch of use many times over.
    A window's walk ends at its first reading at the top (SATURATED): there the limit stops moving while
    the spend may go on, so a stretch ending there (or later) would read too few points per dollar."""
    out = []
    points = split_attrib.samples_from(mem, p.acc, win, 2**63 - 1, t0)
    tolerance = WEEK_RESET_TOL if win == "7d" else split_attrib.SESSION_RESET_TOL
    groups = split_attrib.segments(points, calib.SPAN[win], tolerance, DROP_PTS, resets)
    for seg, start, _, _ in groups:
        if start is not None and t0 <= start < seg[0][0]:
            seg = [(start, 0.0, seg[0][2]), *seg]
        i = 0
        for j in range(1, len(seg)):
            if seg[j][1] >= SATURATED:
                break
            usd = ms.between(seg[i][0], seg[j][0])
            if usd >= p.stretch_usd[win]:
                out.append({"t0": seg[i][0], "t1": seg[j][0], "dp": seg[j][1] - seg[i][1], "usd": usd,
                            "byModel": ms.by_model(seg[i][0], seg[j][0])})
                i = j
    return out


def method_text(p, win):
    return METHOD.format(usd=p.stretch_usd[win], spend=p.spend_word, elsewhere=p.elsewhere)


def estimate(p, st, win="7d"):
    """Percent of the window per dollar of this Mac's spend, from disjoint stretches.

    Use elsewhere (phone, chat app, cloud) only ever adds to how far the limit moved, so every stretch caps
    the true rate at (dp + JITTER) / usd; read with the jitter the other way, (dp - JITTER) / usd is its
    floor. Two stretches agree when their floor-to-cap ranges overlap. `upper` is the lowest cap that at
    least one other stretch agrees with (3 % of them from 67 stretches on), so one stretch alone cannot pull
    it down: a reading off by more than the jitter, calls dated wrongly, spend that the shown quota does not
    count. With no agreeing pair (one stretch, or all apart) it is the lowest cap. A stretch is clean when
    its range holds `upper`; above it the limit moved more than this Mac's spend explains (`pairsDropped`),
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
            "method": method_text(p, win), "pairsUsed": len(clean),
            "pairsDropped": sum(f > upper for f in floor), "pairsLow": sum(c < upper for c in cap),
            "range": [round(each[0]), round(each[-1])] if each else None, "usd": round(usd, 2)}


def sessions(p, mem, t0):
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
            "AND resets_at IS NOT NULL AND win IN ('5h','7d')", (p.acc, t0)):
        snaps.setdefault((ts, src), {})[win] = (pct, (ra + 150) // 300)
    groups = {}
    for (ts, _), w in sorted(snaps.items()):
        if "5h" in w and "7d" in w:
            groups.setdefault((w["5h"][1], w["7d"][1]), []).append((ts, w["5h"][0], w["7d"][0]))
    used = []
    for g in groups.values():
        # up to the last reading before either window reads its top: there a percentage stops moving
        # while the other may go on, so "last minus first" would come out wrong
        g = g[:next((i for i, x in enumerate(g) if x[1] >= SATURATED or x[2] >= SATURATED), len(g))]
        if len(g) < 2:
            continue
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


def week_clusters(p, mem):
    """The weekly windows the readings show: [(start, end, reset, [(ts, pct), ...])], oldest first.
    Readings are grouped by their reset time (within WEEK_RESET_TOL: rounding); a 0 % reading says
    nothing (an unused window's reset time moves with every reading), so only readings above 0 group.
    start = reset - 7 days (a window starts with its first use); end = the reset, or the start of the next
    window when that came earlier (a reset before its time)."""
    span = calib.SPAN["7d"]
    groups = []
    for ts, pct, ra in mem.execute("SELECT ts,pct,resets_at FROM samples WHERE acc=? AND win='7d' AND "
                                   "resets_at IS NOT NULL AND source!='start' AND pct>0 ORDER BY resets_at, ts",
                                   (p.acc,)):
        if groups and ra - groups[-1]["ra"][-1] <= WEEK_RESET_TOL:
            groups[-1]["ra"].append(ra)
            groups[-1]["pts"].append((ts, pct))
        else:
            groups.append({"ra": [ra], "pts": [(ts, pct)]})
    out = []
    for g in groups:
        ras = sorted(g["ra"])
        e = max(set(ras), key=lambda r: (ras.count(r), r))     # the reset time most readings give
        out.append([e - span, e, e, sorted(g["pts"])])
    for a, b in zip(out, out[1:]):
        a[1] = min(a[1], max(a[0], b[0]))
    return [tuple(w) for w in out]


def observed_weeks(p, mem, sp, now, cur7=None, fam=model_key_identity, count=8, drop=DROP_PTS, clusters=None,
                   since=None, resets=()):
    """`weeks`: this Mac's spend and the highest weekly reading per weekly window, newest first, with the
    boundaries of the windows the readings show (week_clusters), not steps of 7 days back from the latest
    reset: a reset before its time moves every later window. A stretch of time without readings gets rows
    of 7 days marked "approx": true (their boundaries are a guess). Same keys as calib.weekly_windows,
    plus "approx" and "scheduledEnd" (the reset time when the window ended before it, else None).
    since: the plan's start; a week that began before it measured another plan's limit and is left out."""
    span = calib.SPAN["7d"]
    cl = week_clusters(p, mem) if clusters is None else clusters
    spans = []                                    # (start, end, reset, pts, approx)
    nxt = None
    for b, end, e, pts in reversed(cl):
        if nxt is None:
            t = e
            while t <= now:                          # after the last window: guessed weeks up to now
                spans.append((t, t + span, t + span, [], True))
                t += span
        else:
            t = nxt
            while t - span >= end:                   # a whole week without readings between two windows
                spans.append((t - span, t, t, [], True))
                t -= span
        spans.append((b, end, e, pts, False))
        nxt = b
    if nxt is not None:
        for _ in range(count):                       # before the first window seen
            spans.append((nxt - span, nxt, nxt, [], True))
            nxt -= span
    spans.sort(key=lambda x: -x[0])
    hit_rows = mem.execute("SELECT ts,resets_at FROM hits WHERE acc=? AND kind='seven_day' AND resets_at IS NOT NULL",
                           (p.acc,)).fetchall()
    out = []
    for b, end, e, pts, approx in spans:
        if len(out) >= count or (since and b < since - 120):
            break
        current = b <= now < e and end == e
        stop = min(end, now)
        d = sp.between(b, stop)
        tokens = mem.execute("SELECT coalesce(sum(inp+out+cw5+cw1h+cr),0) FROM rows WHERE acc=? AND ts>=? AND ts<?",
                             (p.acc, b, stop)).fetchone()[0]
        pts = sorted([(ts, pct) for ts, pct in pts if b <= ts <= stop] +
                     [(ts, 100.0) for ts, ra in hit_rows if b <= ts <= stop
                      and abs((ra + 30) // 60 * 60 - e) <= 120])
        fresh = b                                 # after a reset that kept the reset time (calib's rule)
        for (t0, p0), (_, p1) in zip(pts, pts[1:]):
            if p1 < p0 - drop:
                fresh = t0 + 1
        credit = max((ts for ts in resets if b <= ts <= stop), default=None)
        if credit is not None:
            fresh = max(fresh, credit)
        if current and cur7 and not cur7.get("rolled") and abs((cur7.get("resetsAt") or 0) - e) <= WEEK_RESET_TOL \
                and (cur7.get("start") or 0) > fresh:
            fresh = cur7["start"]
        peak = peak_at = None
        for ts, pct in pts:
            if ts >= fresh and ts != credit and (peak is None or pct >= peak):
                peak, peak_at = pct, ts
        if not current and not tokens and peak is None:
            continue
        out.append({"start": b, "end": end, "current": current,
                    "usd": {"opus": round(d[0], 2), "fable": round(d[1], 2), "other": round(d[2], 2)},
                    "tokens": tokens, "peakPct": peak, "peakAt": peak_at,
                    "peakFinal": None if peak is None else bool(peak >= 100 or (not current and
                                                                               peak_at >= end - calib.PEAK_FINAL)),
                    "resetAt": fresh if fresh > b else None, "m": calib.by_family(mem, p.acc, b, stop, fam),
                    "approx": approx, "scheduledEnd": e if end < e else None})
    return out


def drift(p, mem, sp, ms, ms_api, clusters, t0):
    """`drift`: the weekly rate measured separately in each weekly window the readings show, with the same
    estimator as rates.7d (disjoint stretches in quota dollars, estimate()) and turned into API dollars at
    that week's own mix. Before, it came from calib's envelope over overlapping pairs in API dollars."""
    st = stretches(p, mem, ms, "7d", t0)
    out = []
    for b, end, e, _ in clusters:
        r = estimate(p, [s for s in st if b - WEEK_RESET_TOL <= s["t0"] and s["t1"] <= end + WEEK_RESET_TOL])
        if not r:
            continue
        q, a = ms.between(b, end), ms_api.between(b, end)
        mix = a / q if ms is not ms_api and q > 0 and a > 0 else 1.0
        wk = sp.between(b, end)
        out.append({"weekEnd": e, "usd_per_100": round(r["usd_per_100"] * mix, 1), "pairs": r["pairsUsed"],
                    "spend": {"opus": round(wk[0], 2), "fable": round(wk[1], 2), "other": round(wk[2], 2)},
                    "quotaUsdPer100": round(r["usd_per_100"], 2), "apiMix": round(mix, 3),
                    "confident": r["pairsUsed"] >= CONFIDENT_STRETCHES and r["usd"] >= p.confident_usd})
    return out


def plan_price(p, name):
    """List price of a plan by its name, None when the name is not in p.plan_prices."""
    words = (name or "").lower().replace("×", "x").split()
    for key, price in p.plan_prices:
        if all(w in words for w in key.split()):
            return price
    return None


def read_plan(p, path=None, seen=None):
    """(planName, planPriceUsd). The owner's file wins, field by field; otherwise the name the app saw
    (`seen`) and its list price. (None, None) when nothing is known."""
    f = p.plan_override if p.plan_override is not None and path is None else load_json(path or p.plan_file)
    f = f if isinstance(f, dict) else {}
    name = f.get("planName") if isinstance(f.get("planName"), str) and f.get("planName").strip() else None
    price = f.get("planPriceUsd")
    price = float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None
    name = name or (seen if isinstance(seen, str) and seen.strip() else None)
    return name, price if price is not None else plan_price(p, name)


def value_block(p, rate, st, plan, models, mix=None):
    """The block's "value" from the weekly rate (see ANTIGRAVITY-STATE.md, Value). mix: {model: API dollars
    per quota dollar} when stretches are in quota dollars (see build_common), else None."""
    name, price = plan
    week = rate["usd_per_100"]
    month = week * WEEKS_PER_MONTH
    used, usd = rate["pairsUsed"], rate["usd"]
    confident = used >= CONFIDENT_STRETCHES and usd >= p.confident_usd
    reason = None if confident else (
        f"Measured {min(used, CONFIDENT_STRETCHES - 1)} of {CONFIDENT_STRETCHES} times so far. "
        f"Each measurement takes about ${p.stretch_usd['7d']:.0f} of use on this Mac.")
    labels = p.labels(list(models))
    by_model = []
    for k in models:   # last 30 days, most dollars first
        own = estimate(p, [s for s in st if s["usd"] and s["byModel"].get(k, 0) >= PURE_SHARE * s["usd"]])
        m_month = own["usd_per_100"] * (mix or {}).get(k, 1.0) * WEEKS_PER_MONTH if own else None
        by_model.append({"model": k, "label": labels[k], "monthUsd": round(m_month) if own else None,
                         "timesPrice": round(m_month / price, 1) if own and price else None,
                         "pairs": own["pairsUsed"] if own else 0})
    return {"planName": name, "planPriceUsd": price, "weekUsd": round(week), "monthUsd": round(month),
            "timesPrice": round(month / price, 1) if price else None, "pairs": used,
            "confident": confident, "reason": reason, "byModel": by_model,
            "weekOpusUsd": round(week), "monthOpusUsd": round(month)}


def quota_source(p, con, now):
    q = {"app": "CodexBar", "file": tilde(p.history), "fileModifiedAt": None, "lastSampleAt": None, "ageSec": None,
         "cadence": "CodexBar keeps at most the peak sample of each clock hour",
         "weekly": p.weekly_text,
         "accountsInFile": core.kv_get(con, f"{p.acc}_history_accounts"), "samples": {}}
    try:
        q["fileModifiedAt"] = int(os.path.getmtime(p.history))
    except OSError:
        pass
    today0 = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
    for win in ("5h", "7d"):
        n, first, last, today = con.execute(
            "SELECT count(*), min(ts), max(ts), sum(ts>=?) FROM samples WHERE acc=? AND win=?",
            (today0, p.acc, win)).fetchone()
        q["samples"][win] = {"count": n, "first": first, "last": last, "today": today or 0}
    last = max((s["last"] for s in q["samples"].values() if s["last"]), default=None)
    q["lastSampleAt"] = last
    q["ageSec"] = now - last if last else None
    # per source: "codexbar" = CodexBar's hourly history, "live" = the app's live readings file
    q["bySource"] = {}
    for src, win, n, first, last_s in con.execute(
            "SELECT source, win, count(*), min(ts), max(ts) FROM samples WHERE acc=? GROUP BY source, win", (p.acc,)):
        q["bySource"].setdefault(src, {})[win] = {"count": n, "first": first, "last": last_s}
    r = con.execute("SELECT source FROM samples WHERE acc=? ORDER BY ts DESC, source='live' DESC LIMIT 1",
                    (p.acc,)).fetchone()
    q["lastSource"] = r[0] if r else None
    lv = core.kv_get(con, p.live_key) or {}
    q["account"] = core.kv_get(con, p.account_key)   # account_hash() of the account the numbers are of
    q["liveFile"] = tilde(p.live)
    q["liveFileExists"] = os.path.exists(p.live)
    q["liveBadLines"] = lv.get("bad", 0)
    q["liveCompactedAt"] = lv.get("compactedAt")
    return q


def refresh_forecast(p, win, w, points, now, bands):
    w.pop("forecast", None)
    start, reset = w.get("start"), w["resetsAt"]
    if (w["rolled"] or start is None or reset is None or reset <= now or now - w["measuredAt"] > 1800
            or now - start < 0.03 * calib.SPAN[win]):
        return
    w["forecast"] = calib.forecast(points, start, reset, now,
                                   min(100.0, w["pct"] + w.get("sinceMeasure", 0)), win,
                                   *bands.get((p.acc, win), (None, "default")))


def align_window(p, mem, win, w, sample, attribution, sp, now, credit):
    """Use the last real reading and the same boundary as interval attribution."""
    ts, pct, reset = sample
    source = mem.execute(
        "SELECT source FROM samples WHERE acc=? AND win=? AND ts=? AND source!='start' "
        "ORDER BY CASE source WHEN 'live' THEN 2 WHEN 'poll' THEN 1 ELSE 0 END DESC, source DESC LIMIT 1",
        (p.acc, win, ts)).fetchone()[0]
    waiting = credit is not None and ts <= credit
    rolled = waiting or (reset is not None and reset <= now)
    w.update(pct=0.0 if rolled else pct, measuredAt=ts, resetsAt=None if rolled else reset, source=source, rolled=rolled)
    w.pop("start", None)
    w.pop("usd", None)
    if rolled:
        w.pop("series", None)
    start = credit if waiting else None if rolled else attribution.readings[ts].start
    if start is not None:
        w["start"] = start
        usd = sp.between(start - 1, now)
        w["usd"] = dict(zip(("opus", "fable", "other"), (round(d, 2) for d in usd)))


def apply_split(w, attribution):
    measured = w["measuredAt"]
    split = w["split"] = attribution.at(None if w.get("rolled") else measured, w["pct"])
    full = not split["unknownPct"]
    w["mine"] = split["youPct"] if full else None
    w["outside"] = split["othersPct"] if full else None
    if w.get("rolled"):
        return
    pts = [(ts, pct) for ts, pct in attribution.segment if ts <= measured]
    if not pts:
        w["series"] = []
        return
    first = pts[0][0]
    start = attribution.readings[first].start
    if start < first:
        pts.insert(0, (start, 0.0))
    keep = [pts[0]]
    for i, (ts, pct) in enumerate(pts[1:], 1):
        if pct != keep[-1][1] or i == len(pts) - 1:
            keep.append((ts, pct))
    cap = 96
    if len(keep) > cap:
        step = len(keep) / (cap - 1)
        keep = [keep[int(i * step)] for i in range(cap - 1)] + [keep[-1]]
    series = []
    for ts, pct in keep:
        s = attribution.at(ts, pct)
        mine = s["youPct"] if attribution.rated or not s["unknownPct"] else None
        series.append([ts, pct, mine, s["othersPct"], s["unknownPct"]])
    w["series"] = series


def build_common(p, con, now, events, models, plan_seen=None, since=None, *,
                 complete=True, ambiguous=(), encrypted_mtimes=(), resets=(), attribution_calls=None,
                 weekly_intervals=None):
    """Everything of the block that does not depend on how the provider's calls are read: windows, rates,
    sessions, drift, daily, weeks, value, calibration, quotaSource. `models` = the block's `models`
    (last 30 days, most dollars first: the order of value.byModel). The caller adds cache and coverage.
    since: start of the current plan (epoch) or None. Readings before it measure another plan's limit, so
    the forecast bands, rates, sessions, drift and weeks use only readings from it on; spend (daily,
    totals, models) is not cut."""
    mem = analysis_db(p, con, events, since)
    fam = model_key_identity
    sp = calib.Spend(mem, p.acc, fam)
    t0 = max(now - calib.LOOKBACK, since or 0)
    pairs = {"fable": []}
    for win in ("7d", "5h"):
        pairs[win] = []
        for seg in calib.segments(calib.load_points(mem, p.acc, win, t0), DROP_PTS):
            pairs[win] += calib.seg_pairs(sp, seg)
    # these dollars are not weighted: "weighted spend" is plain spend, k = 1
    kx = {"k": 1.0, "k5": 1.0, "source": "none", "from": "none", "pairs": 0, "fit": None,
          "source5": "none", "from5": "none"}
    bands = {}
    for kind in ("5h", "7d"):
        b = calib.forecast_band(calib.past_windows(mem, p.acc, kind, now, DROP_PTS), kind)
        bands[(p.acc, kind)] = (b, "own") if b else (None, "default")
    a = {"uuid": p.acc, "name": p.name, "short": p.name, "active": False, "useHapp": False, "orgs": []}
    info = calib.account_state(mem, a, sp, pairs, kx, bands, now, fam=fam, drop=DROP_PTS)
    # Rates: disjoint stretches with polluted ones dropped (estimate()), instead of calib's envelope over
    # overlapping pairs. The windows' sinceMeasure follows the new rate.
    # Quota dollars: a provider whose quota does not count a price premium (Codex: calls above 272K prompt
    # tokens cost 2x input and 1.5x output at the API, the quota counts them like other calls, see
    # CODEX-STATE.md) gives each call "qusd" next to "usd". Stretches are measured in them; each rate is
    # then turned into API dollars at the API-to-quota mix of its window, so "100 % ≈ $X" and "this window
    # ≈ $Y at Z %" agree. Without "qusd" (Antigravity) the mix is 1 and nothing changes.
    qkey = "qusd" if any("qusd" in ev for ev in events) else "usd"
    # Calls that measure this account's limit: the current account's (calibration_tags). The spend shown
    # (windows.*.usd, daily, weeks) stays all of this Mac's calls.
    t_dr = since or 0                       # drift looks at every week of the plan
    cur_id = core.kv_get(con, p.account_id_key) if p.account_id else None
    near = [ev for ev in events if ev["ts"] >= t_dr - 86400]
    tags = calibration_tags(cur_id, {ev.get("acct") for ev in near if ev.get("acct")})
    cal = events if tags is None else [ev for ev in events if (ev.get("acct") or "") in tags]
    cal_info = {"filter": "all" if tags is None else ("account" if tags else "ambiguous"),
                "otherAccountCalls": sum(ev.get("calls", 1) for ev in near if ev.get("acct") and ev["acct"] != cur_id),
                "unlabelledCalls": sum(ev.get("calls", 1) for ev in near if not ev.get("acct")),
                "unlabelledUsed": tags is None or "" in tags}
    ms = ModelSpend(cal, min(t0, t_dr) - 86400, qkey)
    ms_api = ModelSpend(cal, min(t0, t_dr) - 86400) if qkey != "usd" else ms
    resets = sorted(ts for ts in resets if ts <= now and ts >= (since or 0))
    st = {win: stretches(p, mem, ms, win, t0, resets) for win in ("7d", "5h")}
    info["rates"] = {win: r for win in ("7d", "5h") if (r := estimate(p, st[win], win))}
    quota_rates = {win: r["pct_per_usd"] for win, r in info["rates"].items()}
    splits = {}
    for win in ("5h", "7d"):
        r = info["rates"].get(win) or {}
        samples = split_attrib.samples_from(mem, p.acc, win, now)
        splits[win] = split_attrib.attribute(
            samples, cal if attribution_calls is None else attribution_calls, win,
            1.0 if p.acc == "codex" else JITTER, rate=quota_rates.get(win), pairs=r.get("pairsUsed", 0),
            resets=resets, spend_key=qkey, bucket_sec=60 if p.acc == "codex" else 0,
            complete=complete, ambiguous=ambiguous, encrypted_mtimes=encrypted_mtimes, since=since or 0)
        if samples:
            align_window(p, mem, win, info["windows"].setdefault(win, {}), samples[-1], splits[win], sp, now,
                         resets[-1] if resets else None)
        else:
            info["windows"].pop(win, None)
    if weekly_intervals is not None:
        weekly_intervals.extend(splits["7d"].intervals)
    for win, r in info["rates"].items():
        if qkey == "usd":
            break
        w = info["windows"].get(win) or {}
        t_mix = w["start"] if w.get("start") and not w.get("rolled") else now - calib.SPAN[win]
        q, a = ms.between(t_mix, now), ms_api.between(t_mix, now)
        if q < 0.5:                         # too little use in the window: the last 7 days' mix
            q, a = ms.between(now - 7 * 86400, now), ms_api.between(now - 7 * 86400, now)
        mix = a / q if q > 0 and a > 0 else 1.0
        r["quotaUsdPer100"] = round(r["usd_per_100"], 2)
        r["apiMix"] = round(mix, 3)
        r["usd_per_100"] *= mix
        r["pct_per_usd"] /= mix
        r["range"] = [round(x * mix) for x in r["range"]] if r.get("range") else r.get("range")
    for win, w in info["windows"].items():
        r = info["rates"].get(win)
        if r and w.get("start") and not w.get("rolled"):
            w["sinceMeasure"] = round(max(0.0, quota_rates[win] * ms.between(w["measuredAt"], now)), 1)
        else:
            w.pop("sinceMeasure", None)
        refresh_forecast(p, win, w, splits[win].segment, now, bands)
        apply_split(w, splits[win])
    ses, ses_n, ses_so_far = sessions(p, mem, t0)
    if "7d" in info["windows"]:
        info["windows"]["7d"]["sessions"] = ses
    # the name the Swift Account already decodes: one full 5-hour window as a share of the week
    info["sessionPctOfWeek"] = round(100 / ses["perWeek"], 2) if ses else None
    clusters = week_clusters(p, mem)
    info["weeks"] = observed_weeks(p, mem, sp, now, info["windows"].get("7d"), fam, clusters=clusters, since=since,
                                   resets=resets)
    info["drift"] = drift(p, mem, sp, ms, ms_api, [c for c in clusters if c[0] >= t_dr - 120], t_dr)
    mem.close()

    for k in DROP:
        info.pop(k, None)
    for w in info["windows"].values():
        for k in DROP_WINDOW:
            w.pop(k, None)
    if since:
        # a week that began before the plan measured the old plan's limit
        info["weeks"] = [wk for wk in info.get("weeks") or [] if wk["start"] >= since - 120]
    for wk in info.get("weeks") or []:
        for k in DROP_WEEK:
            wk.pop(k, None)
        if wk["peakPct"] is not None:
            a7 = splits["7d"]
            s = a7.at(wk["peakAt"], wk["peakPct"])
            wk.update(minePct=s["youPct"] if a7.rated or not s["unknownPct"] else None,
                      othersPct=s["othersPct"], unknownPct=s["unknownPct"], splitMethod=s["method"])
    # daily: whole calendar days (the panel's 180 days end today), and per day the tokens of calls without
    # a price (their dollars are 0, not "less than a cent"), by model
    cut = calendar_cut(now)
    cut_day = time.strftime("%Y-%m-%d", time.localtime(cut))
    info["daily"] = [d for d in info.get("daily") or [] if d["day"] >= cut_day]
    unpriced = {}
    for ev in events:
        if ev["ts"] >= cut and not ev.get("priced", True):
            day = unpriced.setdefault(ev["day"], {})
            k = model_key(ev)
            day[k] = day.get(k, 0) + ev["input"] + ev["cacheRead"] + ev["cacheWrite"] + ev["output"]
    for d in info["daily"]:
        if d["day"] in unpriced:
            d["unpriced"] = unpriced[d["day"]]
    q = quota_source(p, con, now)
    q["planSince"] = since
    info["beta"] = True
    info["kFable"] = 1.0
    info["lastPoll"] = None
    info["lastMeasuredAt"] = q["lastSampleAt"]
    info["has5h"] = True if q["samples"]["5h"]["count"] else (False if q["samples"]["7d"]["count"] else None)
    seen = core.kv_get(con, p.plan_seen_key) or plan_seen
    plan = read_plan(p, seen=seen)
    info["planName"], info["planPriceUsd"] = plan
    info["models"] = models

    r7 = info["rates"].get("7d")
    if r7:
        mix = None
        if qkey != "usd":
            a30, q30 = {}, {}
            for ev in events:
                if ev["ts"] >= now - 30 * 86400:
                    k = model_key(ev)
                    a30[k] = a30.get(k, 0.0) + (ev["usd"] or 0.0)
                    q30[k] = q30.get(k, 0.0) + (ev.get("qusd") or 0.0)
            mix = {k: a30[k] / q30[k] for k in q30 if q30[k] > 0 and a30[k] > 0}
        info["value"] = value_block(p, r7, st["7d"], plan, models, mix)
        info["valueReason"] = None
    else:
        info["value"] = None
        info["valueReason"] = (f"the weekly rate needs one stretch with ${p.stretch_usd['7d']:.0f}+ of this Mac's "
                               f"spend between two readings of the weekly limit (last {calib.LOOKBACK // 86400} "
                               f"days); have {len(st['7d'])}, none that this Mac's spend explains"
                               if st["7d"] else
                               f"the weekly rate needs one stretch with ${p.stretch_usd['7d']:.0f}+ of this Mac's "
                               f"spend between two readings of the weekly limit (last {calib.LOOKBACK // 86400} "
                               f"days); have 0")
    info["calibration"] = {
        "stretches": {win: len(st[win]) for win in st},
        "sessionIntervals": ses_n, "sessionPerWeekSoFar": ses_so_far,
        "thresholds": {"jitterPts": JITTER, "stretchUsd": p.stretch_usd, "confidentStretches": CONFIDENT_STRETCHES,
                       "confidentUsd": p.confident_usd, "sessionMinPts": {"5h": SESSION_D5, "7d": SESSION_D7},
                       "sessionPairs": SESSION_PAIRS, "sessionSum7dPts": SESSION_SUM7},
        "planFile": tilde(p.plan_file), "planFileExists": os.path.exists(p.plan_file),
        "account": cal_info}
    info["quotaSource"] = q
    return info


def error_code(provider, exc):
    """What `error` says: a fixed code with the exception's type only. Its message can hold a file path or
    an account name, and the summary must hold neither."""
    return f"{provider}-failed:{type(exc).__name__}"


def calendar_cut(now, days=180):
    """Local midnight that begins the `days` calendar days ending today (the panel's 180-day period)."""
    y, m, d = time.localtime(now)[:3]
    return int(time.mktime((y, m, d - (days - 1), 0, 0, 0, 0, 0, -1)))


def calibration_tags(cur, labelled):
    """Which calls (by their account tag, "" = the log names no account) may measure the current account's
    limit. cur: account_hash() of the current account's id, or None; labelled: the tags seen.
    The current account's calls; unlabelled ones too while no other account shows up. Without a current
    id: all calls (None) while at most one account shows up, else none (set()): no rate beats a mixed one."""
    labelled = set(labelled) - {""}
    if cur:
        return {cur} | ({""} if labelled <= {cur} else set())
    return None if len(labelled) <= 1 else set()


def error_block(p, err):
    return {"uuid": p.acc, "name": p.name, "short": p.name, "beta": True, "error": err}
