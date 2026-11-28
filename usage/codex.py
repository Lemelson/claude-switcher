#!/usr/bin/env python3
"""Codex (ChatGPT plan) block of the summary: `state["codex"]`, contract in CODEX-STATE.md.

Shaped exactly like the Antigravity block (ANTIGRAVITY-STATE.md) and built by the same code
(quota_provider.py); only the spend source and the price table are Codex's own:
  * spend: every model call Codex recorded on this Mac in ~/.codex/sessions/**/*.jsonl[.zst] and
    ~/.codex/archived_sessions/*.jsonl[.zst] (`token_count` events, `last_token_usage`). Every Codex client
    on this Mac writes there: Codex Desktop, the CLI, the IDE extension, `codex exec`, T3 Code and Claude
    Code's Codex plugin (field `originator` of `session_meta`). Exact tokens, dollars estimated at OpenAI
    API list prices (PRICES below);
  * quota: CodexBar's Codex history (history/codex.json, lanes `session` and `weekly`) and the app's live
    readings file codex-quota.jsonl, merged like Antigravity's;
  * `accounts`: the same block once per Codex account on this Mac, from that account's calls and readings
    only (codex_accounts.py), and `unattributed`: calls of logs that name no account.

One quota: the 5-hour and weekly windows count all Codex use. Local logs and quota readings feed the
shared attribution calculation in split_attrib.py.

Scan cost: ~60 s of reading the first time (about 18 GB of session logs in 180 days, mostly zstd-compressed),
spread over the passes at SCAN_BUDGET_S each; then only changed files are read: a growing plain .jsonl from
the byte offset reached last time, a changed .zst file again in full. Cache: DATA/codex-scan.db (see scan).
Read-only towards ~/.codex and CodexBar's folders.
Never raises out of block().

  python3 usage/codex.py            print the block
  python3 usage/codex.py --check    self-check (codex_check.py)
  python3 usage/codex.py --sample   write usage/codex-state-sample.json (the real block, lists cut short)
"""
import calendar
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402
import quota_provider as qp  # noqa: E402

ACC = "codex"
NAME = "Codex"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
HISTORY = os.path.join(os.path.dirname(core.CODEXBAR_HISTORY), "codex.json")
LIVE = f"{core.DATA}/codex-quota.jsonl"
SCAN_DB = f"{core.DATA}/codex-scan.db"
OLD_SCAN_CACHE = f"{core.DATA}/codex-scan.json"   # the first cache (reader 1): taken over once, then removed
PLAN_FILE = f"{core.DATA}/codex-plan.json"
CACHE_VERSION = 2              # reader version; logs read by an older reader are read again (see scan)
CACHE_SCHEMA_V = 2             # layout of codex-scan.db (2: merged keys end in the log's account tag)
SCAN_BUDGET_S = 4.0            # seconds of log reading per pass at most (the first scan takes ~60 s in all)
RETRY_ERROR_S = 3600           # a log that could not be read is tried again after this
FOLD_DAYS = 40                 # merged buckets older than this are per clock hour
PLAN_RUNS = 20                 # plan runs kept per log
PLAN_SINCE_KEY = "codex_plan_since"   # kv: start of the current plan (forecast bands, rates, weeks use only it)
DAYS = 180                     # calls older than this are not needed (daily covers 180 days)
# Monthly list prices of the ChatGPT plans, USD. Plus: "$20/month (billed monthly)",
# https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus, checked 2026-11-25. Other plans have no
# price on an official page that could be read (chatgpt.com/pricing shows no amounts to a script), so they
# get none: set it in codex-plan.json.
PLAN_PRICES = (("plus", 20.0),)
# Smaller stretches than Antigravity's $60 / $15: on Plus 100 % of the weekly limit was about $66 of this Mac's
# API-price spend on 25.11.2026 (27 % for $18) and a 5-hour window about $13 (89 % for $12), so $60 would need
# 90 % of a week. $10 still moves the weekly limit about 15 points and $3 the 5-hour one about 20, both far
# above the 2-point jitter. Value is confident from 4 clean weekly stretches with $40 in them.
STRETCH_USD = {"5h": 3.0, "7d": 10.0}
CONFIDENT_USD = 40.0
PLAN_NAMES = {"plus": "Plus", "pro": "Pro", "prolite": "Pro 5x", "go": "Go", "free": "Free", "team": "Business",
              "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}

# OpenAI API list prices, $ per 1M tokens, Standard tier, from https://developers.openai.com/api/docs/pricing,
# checked 2026-11-25. Columns: input, cached input, output, cache write (30 minutes; None = not listed).
# `input_tokens` of Codex includes the cached part (OpenAI convention); reasoning is inside output.
PRICES_CHECKED = "2026-11-25"
PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICES = {
    "gpt-6.2-sol":   (2.00, 0.10, 10.00, 2.50),
    "gpt-6.1-sol":   (2.00, 0.10, 10.00, 2.50),
    "gpt-6.1-terra": (1.00, 0.05, 5.00, 1.25),
    "gpt-6-sol":     (2.00, 0.20, 10.00, 2.50),
    "gpt-6-astra":   (10.00, 1.00, 50.00, 12.50),
    "gpt-6.2-astra": (8.00, 0.40, 40.00, 10.00),
    "gpt-6-luna":    (0.10, 0.01, 0.50, 0.125),
    "gpt-5.6-sol":   (4.00, 0.40, 20.00, 5.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00, 2.50),
    "gpt-5.6-luna":  (0.20, 0.02, 1.20, 0.25),
    "gpt-5.5":       (5.00, 0.50, 30.00, None),
    "gpt-5.4":       (2.50, 0.25, 15.00, None),
}
# Long context (prompt above 272K tokens): the whole call at these prices (the page does not say whether only
# the part above the threshold is; the whole call is assumed).
LONG_CONTEXT = 272_000
PRICES_LONG = {
    "gpt-6.2-sol":   (4.00, 0.20, 15.00, 5.00),
    "gpt-6.1-sol":   (4.00, 0.20, 15.00, 5.00),
    "gpt-6.1-terra": (2.00, 0.10, 7.50, 2.50),
    "gpt-6-sol":     (4.00, 0.40, 15.00, 5.00),
    "gpt-6-astra":   (20.00, 2.00, 75.00, 25.00),
    "gpt-6.2-astra": (16.00, 0.80, 60.00, 20.00),
    "gpt-6-luna":    (0.20, 0.02, 0.75, 0.25),
    "gpt-5.6-sol":   (8.00, 0.80, 30.00, 10.00),
    "gpt-5.6-terra": (4.00, 0.40, 18.00, 5.00),
    "gpt-5.6-luna":  (0.40, 0.04, 1.80, 0.50),
    "gpt-5.5":       (10.00, 1.00, 45.00, None),
    "gpt-5.4":       (5.00, 0.50, 22.50, None),
}
# Fast mode (service tier "priority"; OpenAI renamed Priority to Fast on 30.07.2026): the page's Fast table is this
# multiple of Standard, short and long context alike, except that it has no long-context Fast price for the
# models in FAST_LONG_UNLISTED ("-", checked 2026-11-25): such calls are priced long × the same multiple and
# counted as estimated (models.*.estimatedPriceCalls, coverage.unconfirmedPrices "<model> fast long").
FAST_X = {"gpt-5.5": 2.5}      # every other priced model: 2.0
FAST_LONG_UNLISTED = {"gpt-5.5", "gpt-5.4"}
UNCONFIRMED = set()            # every other price above is on the official page
# what one bucket holds: calls, calls with nothing read from the cache, uncached input, cached input,
# cache writes, output (reasoning included), reasoning
N_VALS = 7
FIELD_MARK = (b'"token_count"', b'"turn_context"', b'"session_meta"', b'"thread_settings_applied"')
ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(\.zst)?$")


def model_labels(ids):
    """The panel's short names: "gpt-5.6-sol" -> "Sol 5.6"; "gpt-5.5" -> "GPT-5.5"; others as recorded."""
    out = {}
    for i in ids:
        m = re.fullmatch(r"gpt-(\d+(?:\.\d+)?)(?:-([a-z]+))?", i)
        if m and m.group(2):
            out[i] = f"{m.group(2).capitalize()} {m.group(1)}"
        elif m:
            out[i] = f"GPT-{m.group(1)}"
        else:
            out[i] = i
    return out


P = qp.Provider(
    acc=ACC, name=NAME, history=HISTORY, live=LIVE, plan_file=PLAN_FILE, plan_prices=PLAN_PRICES,
    spend_word="Codex", elsewhere="Codex cloud, code review, another computer",
    weekly_text="the weekly limit of the ChatGPT account's Codex quota (one quota for every Codex surface)",
    labels=model_labels, stretch_usd=STRETCH_USD, confident_usd=CONFIDENT_USD,
    # CodexBar's account key -> the account id the session logs carry as creator_account_id
    account_id=lambda key: (m.group(1) if (m := re.fullmatch(r"codex:v1:provider-account:(.+)", key)) else None))


# ------------------------------------------------------------------------------ prices ----
def prices_for(model, tier, long):
    """(input, cached, output, write) in $ per 1M tokens for a call, or None if the model has no price."""
    base = (PRICES_LONG if long else PRICES).get(model)
    if not base:
        return None
    x = FAST_X.get(model, 2.0) if tier == "priority" else 1.0
    write = base[3] if base[3] is not None else base[0]
    return base[0] * x, base[1] * x, base[2] * x, write * x


def price_estimated(model, tier, long):
    """True when the call's price is not on the page but derived (Fast mode with a long prompt, FAST_LONG_UNLISTED)."""
    return bool(long) and tier == "priority" and model in FAST_LONG_UNLISTED


def bucket_usd(model, tier, long, v):
    pr = prices_for(model, tier, long)
    if not pr:
        return None
    return (v[2] * pr[0] + v[3] * pr[1] + v[5] * pr[2] + v[4] * pr[3]) / 1e6


# -------------------------------------------------------------------------------- scan ----
def session_files(home=CODEX_HOME, since=0):
    """[(path, sig, session id)] of the session logs changed since `since`: plain and zstd-compressed,
    active and archived."""
    out = []
    pats = (os.path.join(home, "sessions", "**", "*.jsonl"), os.path.join(home, "sessions", "**", "*.jsonl.zst"),
            os.path.join(home, "archived_sessions", "*.jsonl"), os.path.join(home, "archived_sessions", "*.jsonl.zst"))
    for pat in pats:
        for p in glob.glob(pat, recursive=True):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            m = ID_RE.search(p)
            out.append((p, [st.st_size, st.st_mtime_ns, st.st_ino], m.group(1) if m else p))
    return out


class _ZstdCli:
    """Lines of `zstd -dc <path>` (Python without compression.zstd). close() waits for the process, so no
    zombie is left, and raises when zstd failed: a broken file then becomes an error entry of the scan (tried
    again later) instead of reading as an empty file."""

    def __init__(self, path):
        self.proc = subprocess.Popen(["zstd", "-dcq", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.eof = False

    def __iter__(self):
        yield from self.proc.stdout
        self.eof = True

    def close(self):
        self.proc.stdout.close()
        if not self.eof:
            self.proc.kill()            # closed before the end: its exit status means nothing
        code = self.proc.wait()
        if code and self.eof:
            raise OSError(f"zstd exited with {code}")


def open_lines(path, offset=0):
    """Binary line iterator over a session log (plain from `offset`, or zstd). Has close()."""
    if path.endswith(".zst"):
        try:
            from compression import zstd    # Python 3.14+
            return zstd.open(path, "rb")
        except ImportError:
            return _ZstdCli(path)
    fh = open(path, "rb")
    fh.seek(offset)
    return fh


def new_state():
    # self: creation time (ms) of the session's id; child: the session was forked from another one or
    # spawned by one (forked_from_id / parent_thread_id); turnAt: creation time (ms) of the current turn's
    # id; turnSeen: a turn_context was read; copies / copyTokens: copied calls left out (read_session);
    # plans: [[plan, first ts, last ts, start of its weekly window or None], ...] in log order
    return {"offset": 0, "lastTotal": None, "model": None, "tier": None, "effort": None, "acct": None,
            "originator": None, "plan": None, "planTs": 0, "b": {}, "repeats": 0, "bad": 0,
            "self": None, "child": False, "turnAt": None, "turnSeen": False, "copies": 0, "copyTokens": 0,
            "plans": []}


def parse_ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def uuid7_ms(s):
    """Creation time in ms of a version-7 UUID (Codex's session and turn ids), else None."""
    if isinstance(s, str) and len(s) == 36 and s[14] == "7" and s[8] == "-":
        try:
            return int(s[:8] + s[9:13], 16)
        except ValueError:
            return None
    return None


def read_session(path, st):
    """Reads new lines of one session log into the state `st` (see new_state) and returns it.

    One call = one `token_count` event whose cumulative `total_token_usage` changed: Codex sends the same
    counters again with every rate-limit update, so a repeat is skipped (counted in `repeats`). The call's
    tokens are `last_token_usage`; its model and effort are those of the newest `turn_context` before it
    (else of `thread_settings_applied`), its service tier that of the newest `thread_settings_applied`.
    Calls are summed into one bucket per minute, model, tier, long-context flag and effort.

    Copied calls are left out (`copies`): a forked or spawned session (sub-agent, approvals reviewer) starts
    with a copy of its parent's history, `token_count` events included, all stamped with the new session's
    time (in July and August 2026 Codex copied 19 649 calls this way, $1 179 at list prices). A call is a
    copy when its turn was created before the session itself (turn and session ids are UUIDv7, so their
    creation time is in the id), or, in a forked or spawned session, when no turn_context came before it.
    Its rate-limit reading (plan) is old too and is skipped as well."""
    plain = not path.endswith(".zst")
    off = st["offset"] if plain else 0
    fh = open_lines(path, off)
    b = st["b"]
    try:
        for raw in fh:
            if plain:
                if not raw.endswith(b"\n"):
                    break               # a line still being written: next pass
                off += len(raw)
            head = raw[:320]
            if not any(mk in head for mk in FIELD_MARK):
                continue
            try:
                rec = json.loads(raw)
                pay = rec.get("payload") or {}
            except ValueError:
                st["bad"] += 1
                continue
            typ = rec.get("type")
            if typ == "session_meta":
                if st["acct"] is None:      # the first one is this session; later ones belong to copies
                    st["acct"] = pay.get("creator_account_id") or ""
                    st["originator"] = pay.get("originator")
                    st["self"] = uuid7_ms(pay.get("id"))
                    st["child"] = bool(pay.get("forked_from_id") or pay.get("parent_thread_id"))
                continue
            if typ == "turn_context":
                st["model"] = pay.get("model") or st["model"]
                eff, cm = pay.get("effort"), pay.get("collaboration_mode")
                if not eff and isinstance(cm, dict):
                    eff = (cm.get("settings") or {}).get("reasoning_effort")
                st["effort"] = eff if isinstance(eff, str) else None
                st["turnAt"] = uuid7_ms(pay.get("turn_id"))
                st["turnSeen"] = True
                continue
            ptype = pay.get("type")
            if ptype == "thread_settings_applied":
                ts_ = pay.get("thread_settings") or {}
                st["tier"] = ts_.get("service_tier")
                if not st["model"]:
                    st["model"] = ts_.get("model")
                continue
            if ptype != "token_count":
                continue
            copy = ((st["turnAt"] is not None and st["self"] is not None and st["turnAt"] < st["self"])
                    or (st["child"] and not st["turnSeen"]))
            info = pay.get("info") or {}
            total, last = info.get("total_token_usage"), info.get("last_token_usage")
            rl = pay.get("rate_limits") or {}
            t = parse_ts(rec.get("timestamp"))
            if rl.get("plan_type") and t and not copy:
                plan = rl["plan_type"]
                if t >= st["planTs"]:
                    st["plan"], st["planTs"] = plan, t
                runs = st["plans"]
                if runs and runs[-1][0] == plan:
                    runs[-1][2] = max(runs[-1][2], t)
                else:
                    sec = rl.get("secondary") or {}
                    ra, mins = sec.get("resets_at"), sec.get("window_minutes")
                    week0 = int(ra - mins * 60) if isinstance(ra, (int, float)) and isinstance(mins, (int, float)) \
                        else None
                    runs.append([plan, t, t, week0])
                    del runs[:-PLAN_RUNS]
            if not total or not last:
                continue
            if total == st["lastTotal"]:
                st["repeats"] += 1
                continue
            st["lastTotal"] = total
            if t is None:
                st["bad"] += 1
                continue
            try:
                inp = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                write = int(last.get("cache_write_input_tokens") or 0)
                out = int(last.get("output_tokens") or 0)
                reas = int(last.get("reasoning_output_tokens") or 0)
            except (TypeError, ValueError):
                st["bad"] += 1
                continue
            if inp + out <= 0:
                continue
            if copy:
                st["copies"] += 1
                st["copyTokens"] += inp + out
                continue
            long = 1 if inp > LONG_CONTEXT else 0
            key = f"{t // 60}|{st['model'] or 'unknown'}|{st['tier'] or 'default'}|{long}|{st['effort'] or ''}"
            v = b.setdefault(key, [0] * N_VALS)
            v[0] += 1
            v[1] += cached == 0
            v[2] += max(0, inp - cached - write)
            v[3] += cached
            v[4] += write
            v[5] += out
            v[6] += reas
    finally:
        fh.close()
    st["offset"] = off if plain else 0
    return st


# ------------------------------------------------------------------------- scan cache ----
# codex-scan.db (SQLite, next to state.json): one row per session log with its signature, the reader version
# that read it, a small summary and its state (buckets included), plus the merged buckets of all sessions
# (one log per session id). A warm pass reads the signatures and the merged buckets only; a changed log's
# old buckets are taken out of the merge and its new ones put in. Merged buckets older than FOLD_DAYS are
# kept per clock hour instead of per minute: rates, sessions and windows need minutes for the last 35 days
# only (calib.LOOKBACK), daily and weeks are fine with hours. Deleting the file is safe (one cold scan).
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sig TEXT NOT NULL, sid TEXT NOT NULL, v INTEGER NOT NULL,
  err_at INTEGER, chosen INTEGER NOT NULL DEFAULT 0, mtime INTEGER NOT NULL, summary TEXT NOT NULL, st TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _summary(st, err=False):
    b = st["b"]
    return {"n": sum(v[0] for v in b.values()), "last": max((int(k.split("|", 1)[0]) * 60 for k in b), default=None),
            "bad": st["bad"], "repeats": st["repeats"], "copies": st.get("copies", 0),
            "copyTokens": st.get("copyTokens", 0), "plans": st.get("plans") or [], "plan": st.get("plan"),
            "planTs": st.get("planTs") or 0, "originator": st.get("originator"), "acct": st.get("acct"),
            "error": err}


def _fold_min(now):
    """Minute index before which merged buckets are kept per clock hour (a whole hour)."""
    return int(now - FOLD_DAYS * 86400) // 3600 * 60


def _fold_key(k, fold):
    m, rest = k.split("|", 1)
    m = int(m)
    return k if m >= fold else f"{m // 60 * 60}|{rest}"


def _merge_into(merged, b, fold, sign, tag=None):
    """Adds (sign +1) or takes out (-1) the buckets `b` of one log. tag: the log's account tag
    (qp.account_hash of its creator_account_id, "" when the log names none), appended to each merged key."""
    for k, v in b.items():
        fk = _fold_key(k, fold) if tag is None else f"{_fold_key(k, fold)}|{tag}"
        acc = merged.get(fk)
        if acc is None:
            acc = merged[fk] = [0] * N_VALS
        for i in range(N_VALS):
            acc[i] += sign * v[i]
        if not any(acc):
            del merged[fk]


def _tag(summary):
    return qp.account_hash(summary.get("acct")) or ""


def _fingerprint(path, off):
    """Hash of the last 4 KB before byte `off` of a plain log: a log read on from `off` must still start with
    what was read (a rewrite of the same length, or a truncation and new growth, changes it)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, off - 4096))
            return hashlib.blake2b(fh.read(min(off, 4096)), digest_size=8).hexdigest()
    except OSError:
        return None


def _open_cache(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    db.executescript(CACHE_SCHEMA)
    return db


def _migrate_v1(db, home, old_path):
    """Takes over the per-file states of the first cache (codex-scan.json, reader 1) as entries of reader 1:
    they are used as they are until read again, newest first, so the block stays complete meanwhile."""
    old = qp.load_json(old_path)
    if not old or old.get("key0") != [1, home]:
        return 0
    rows = []
    for path, c in (old.get("files") or {}).items():
        st = {**new_state(), **c["st"]}
        rows.append((path, json.dumps(c["sig"]), c["sid"], 1, int(time.time()) if c.get("error") else None,
                     c["sig"][1] // 10**9, json.dumps(_summary(st, bool(c.get("error")))), json.dumps(st)))
    db.executemany("INSERT OR REPLACE INTO files(path,sig,sid,v,err_at,mtime,summary,st) VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def scan(home=CODEX_HOME, cache_path=SCAN_DB, now=None, budget=None, old_cache=OLD_SCAN_CACHE):
    """(merged buckets, stats, scan info) of every session log changed in the last DAYS + 2 days.

    A log whose signature (size, mtime, inode) and reader version are unchanged costs one stat. A plain log
    that only grew (same inode, not shorter) is read on from its saved offset; anything else changed is read
    again from the start. At most `budget` seconds of reading per pass (at least one log): new and changed
    logs first, newest first, then logs read by an older reader (CACHE_VERSION; their old numbers count until
    then), then logs that failed (again after RETRY_ERROR_S). What is left is reported as pending
    (stats["pendingFiles"], ["staleFiles"]) and read on the next passes."""
    t0 = time.time()
    now = now or time.time()
    budget = SCAN_BUDGET_S if budget is None else budget
    files = session_files(home, now - (DAYS + 2) * 86400)
    db = _open_cache(cache_path)
    try:
        db.execute("BEGIN IMMEDIATE")       # one scan at a time (tick, --check, the CLI)
        meta = dict(db.execute("SELECT k, v FROM meta"))
        migrated = 0
        if meta.get("home") != home or meta.get("schema") not in ("1", str(CACHE_SCHEMA_V)):
            db.execute("DELETE FROM files")
            db.execute("DELETE FROM meta")
            meta = {}
            if old_cache:
                migrated = _migrate_v1(db, home, old_cache)
        elif meta.get("schema") != str(CACHE_SCHEMA_V):
            meta.pop("merged", None)        # layout 1: same logs, merged keys without account tag: merge again
        rows = {r[0]: {"sig": json.loads(r[1]), "sid": r[2], "v": r[3], "err_at": r[4], "chosen": r[5],
                       "mtime": r[6], "summary": json.loads(r[7])}
                for r in db.execute("SELECT path,sig,sid,v,err_at,chosen,mtime,summary FROM files")}
        fold = int(meta["fold"]) if meta.get("merged") else _fold_min(now)
        merged = {k: v for k, v in json.loads(meta["merged"]).items()} if meta.get("merged") else None
        cur = {p: (sig, sid) for p, sig, sid in files}
        need, stale, retry = [], [], []
        for p, sig, sid in files:
            c = rows.get(p)
            if c is None or c["sig"] != sig:
                need.append(p)
            elif c["err_at"] is not None:
                if now - c["err_at"] >= RETRY_ERROR_S:
                    retry.append(p)
            elif c["v"] != CACHE_VERSION:
                stale.append(p)
        by_mtime = lambda p: -cur[p][0][1]
        queue = sorted(need, key=by_mtime) + sorted(stale, key=by_mtime) + sorted(retry, key=by_mtime)
        old_b = {}                          # (buckets, tag) of chosen logs before they were read again
        read = []
        for p in queue:
            if read and time.time() - t0 >= budget:
                break
            sig, sid = cur[p]
            c = rows.get(p)
            prev = None
            if c is not None:
                prev = json.loads(db.execute("SELECT st FROM files WHERE path=?", (p,)).fetchone()[0])
                if c["chosen"]:
                    old_b[p] = (prev["b"], _tag(c["summary"]))
            # read on only from a log that grew (same inode, longer) and still holds what was read before it
            go_on = (prev is not None and c["v"] == CACHE_VERSION and c["err_at"] is None and not p.endswith(".zst")
                     and c["sig"][2] == sig[2] and sig[0] > prev["offset"]
                     and prev.get("fp") == _fingerprint(p, prev["offset"]))
            st = json.loads(json.dumps(prev)) if go_on else new_state()
            err = None
            try:
                st = read_session(p, st)
                if not p.endswith(".zst"):
                    st["fp"] = _fingerprint(p, st["offset"])
            except Exception:
                # keep the last good numbers of this log until a later pass reads it (coverage says so)
                err = int(now)
                st = prev if prev is not None and c["err_at"] is None else new_state()
            rows[p] = {"sig": sig, "sid": sid, "v": CACHE_VERSION, "err_at": err, "chosen": c["chosen"] if c else 0,
                       "mtime": sig[1] // 10**9, "summary": _summary(st, err is not None)}
            db.execute("INSERT OR REPLACE INTO files(path,sig,sid,v,err_at,chosen,mtime,summary,st) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (p, json.dumps(sig), sid, CACHE_VERSION, err, rows[p]["chosen"],
                                                     rows[p]["mtime"], json.dumps(rows[p]["summary"]), json.dumps(st)))
            read.append(p)
        unread = set(need) - set(read)
        # a log that is gone stays while another log of its session waits to be read (a move to the archive
        # whose new path the budget did not reach): the session must not drop out for a pass
        waiting = {cur[p][1] for p in unread}
        gone = [p for p in rows if p not in cur and rows[p]["sid"] not in waiting]
        touched = {rows[p]["sid"] for p in read} | {rows[p]["sid"] for p in gone}
        for p in gone:
            if rows[p]["chosen"]:
                old_b[p] = (json.loads(db.execute("SELECT st FROM files WHERE path=?", (p,)).fetchone()[0])["b"],
                            _tag(rows[p]["summary"]))
            db.execute("DELETE FROM files WHERE path=?", (p,))
            del rows[p]
        groups = {}
        for p, c in rows.items():
            groups.setdefault(c["sid"], []).append(p)
        if merged is None:                  # first pass of this cache: merge everything once
            merged, old_b, touched = {}, {}, set(groups)
            for c in rows.values():
                c["chosen"] = 0
        load_b = lambda p: json.loads(db.execute("SELECT st FROM files WHERE path=?", (p,)).fetchone()[0])["b"]
        # 1. take out the old buckets of every chosen log that was read again or is gone
        for b, tag in old_b.values():
            _merge_into(merged, b, fold, -1, tag)
        # 2. one log per session id: a log that was moved (sessions -> archived_sessions) or compressed
        # (.jsonl -> .jsonl.zst) can briefly exist twice; the copy with more calls wins (then the newer).
        # Put in each touched session's chosen log, take out a log that is no longer chosen.
        for sid in touched:
            ps = groups.get(sid, [])
            if not ps:
                continue
            best = max(ps, key=lambda p: (rows[p]["summary"]["n"], rows[p]["sig"][1]))
            for p in ps:
                in_merge = rows[p]["chosen"] and p not in old_b
                want = p == best
                if in_merge and not want:
                    _merge_into(merged, load_b(p), fold, -1, _tag(rows[p]["summary"]))
                elif want and not in_merge:
                    _merge_into(merged, load_b(p), fold, +1, _tag(rows[p]["summary"]))
                if rows[p]["chosen"] != want:
                    rows[p]["chosen"] = int(want)
                    db.execute("UPDATE files SET chosen=? WHERE path=?", (int(want), p))
        nfold = _fold_min(now)
        if nfold - fold >= 24 * 60:         # once a day: fold another day of minutes into hours
            refolded = {}
            _merge_into(refolded, merged, nfold, +1)
            merged, fold = refolded, nfold
        changed = bool(read or gone or touched or migrated or not meta.get("merged") or str(fold) != meta.get("fold"))
        if changed:
            for k, v in (("home", home), ("schema", str(CACHE_SCHEMA_V)), ("fold", str(fold)),
                         ("merged", json.dumps(merged, separators=(",", ":")))):
                db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    if migrated and old_cache:
        try:
            os.remove(old_cache)
        except OSError:
            pass
    keep = [p for p, c in rows.items() if c["chosen"]]
    sm = [rows[p]["summary"] for p in keep]
    stats = {"sessionFiles": len(keep), "compressedFiles": sum(p.endswith(".zst") for p in keep),
             "archivedFiles": sum("/archived_sessions/" in p for p in keep),
             "duplicateFilesDropped": len(rows) - len(keep),
             "unreadableFiles": sum(bool(s["error"]) for s in sm),
             "malformedLines": sum(s["bad"] for s in sm),
             "repeatsDropped": sum(s["repeats"] for s in sm),
             "copiesDropped": sum(s["copies"] for s in sm),
             "copyTokensDropped": sum(s["copyTokens"] for s in sm),
             "pendingFiles": len(unread),
             "staleFiles": sum(1 for p, c in rows.items() if c["v"] != CACHE_VERSION)}
    stats["plans"] = [(_tag(s), s["plan"], s["planTs"], s["plans"]) for s in sm]
    stats["planSeen"], stats["planSeenAt"], stats["planSince"] = plans_of(stats["plans"])
    stats["perFile"] = [(s["last"], s["originator"], s["acct"], s["n"], _tag(s)) for s in sm]
    return merged, stats, {"cached": not read and not gone, "rescanned": len(read), "migrated": migrated, "fold": fold,
                           "ms": round((time.time() - t0) * 1000)}


def plans_of(plans, tags=None):
    """(newest plan type, its time, plan start) from the logs' plan readings [(tag, plan, ts, runs)], of the
    logs whose account tag is in `tags` only (None: all)."""
    plan, plan_ts, runs = None, 0, []
    for tag, p, ts, rs in plans:
        if tags is not None and tag not in tags:
            continue
        if p and (ts, p) > (plan_ts, plan or ""):     # same second: the larger name, so the order of logs never matters
            plan, plan_ts = p, ts
        runs += rs
    return plan, plan_ts or None, plan_since(runs, plan)


def plan_since(runs, plan):
    """When the current plan began, from the plan type of the rate-limit readings in the logs: the first
    reading of `plan` after the last reading of another plan, moved back to the start of its weekly window
    when that lies after the other plan's last reading. None when the logs show no other plan."""
    if not plan:
        return None
    other = max((r[2] for r in runs if r[0] != plan), default=None)
    if other is None:
        return None
    firsts = [(max(r[1], other + 1), r[3]) for r in runs if r[0] == plan and r[2] > other]
    if not firsts:
        return None
    t = min(f[0] for f in firsts)
    # of the runs that begin at that second, the earliest valid weekly-window start wins (None never compares)
    weeks = [w for f, w in firsts if f == t and w is not None and other < w < t]
    return min(weeks) if weeks else t


def first_session(home=CODEX_HOME):
    """Epoch of the oldest Codex session log on this Mac (from its file name), or None. Looks only into the
    oldest day folder of sessions/YYYY/MM/DD and the archive."""
    names = []
    d = os.path.join(home, "sessions")
    for _ in range(3):              # YYYY, MM, DD: the smallest name at each level
        try:
            sub = sorted(x for x in os.listdir(d) if x.isdigit())
        except OSError:
            sub = []
        if not sub:
            d = None
            break
        d = os.path.join(d, sub[0])
    for folder in ([d] if d else []) + [os.path.join(home, "archived_sessions")]:
        try:
            names += os.listdir(folder)
        except OSError:
            pass
    best = None
    for n in names:
        m = re.match(r"rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)", n)
        if m:
            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S"))
            best = t if best is None or t < best else best
    return int(best) if best else None


def calendar_cut(now):
    """Local midnight that begins the DAYS calendar days ending today: spend covers whole days, as the panel's
    180-day period does."""
    return qp.calendar_cut(now, DAYS)


def events_from(buckets, now, fold):
    """Buckets -> provider calls at the start of their minute or folded historical hour."""
    cut = calendar_cut(now)
    fold *= 60
    evs = []
    days = {}                       # local date per clock hour (time zones move by whole hours here)
    for k, v in buckets.items():
        parts = k.split("|")
        minute, model, tier, long, effort = parts[:5]
        tag = parts[5] if len(parts) > 5 else ""
        ts = int(minute) * 60
        if ts < cut:
            continue
        usd = bucket_usd(model, tier, int(long), v)
        qusd = bucket_usd(model, tier, 0, v) if int(long) else usd    # what the quota counts (QUOTA_PRICES)
        evs.append({"ts": ts, "bucketSec": 3600 if ts < fold else 60,
                    "model": model, "priceAs": model if usd is not None else None, "qusd": qusd or 0.0,
                    "effort": effort.capitalize() if effort else None, "tier": tier, "long": int(long),
                    "calls": v[0], "cold": v[1], "input": v[2], "cacheRead": v[3], "cacheWrite": v[4],
                    "output": v[5], "thinking": v[6], "usd": usd if usd is not None else 0.0,
                    "priced": usd is not None, "estimated": price_estimated(model, tier, int(long)),
                    "acct": tag or None, "day": days.get(ts // 3600) or days.setdefault(
                        ts // 3600, time.strftime("%Y-%m-%d", time.localtime(ts)))})
    evs.sort(key=lambda e: e["ts"])
    return evs


# ---------------------------------------------------------------------------- analysis ----
def models_30d(events, now):
    """The block's `models`: per model, last 30 days, most dollars first (same keys as Antigravity's)."""
    acc = {}
    for ev in events:
        if ev["ts"] < now - 30 * 86400:
            continue
        m = acc.setdefault(qp.model_key(ev), {"calls": 0, "input": 0, "cached": 0, "cacheWrite": 0, "output": 0,
                                              "thinking": 0, "usd": 0.0, "efforts": {}, "unpricedCalls": 0,
                                              "estimatedPriceCalls": 0})
        m["calls"] += ev["calls"]
        m["input"] += ev["input"]
        m["cached"] += ev["cacheRead"]
        m["cacheWrite"] += ev["cacheWrite"]
        m["output"] += ev["output"]
        m["thinking"] += ev["thinking"]
        m["usd"] += ev["usd"]
        if not ev["priced"]:
            m["unpricedCalls"] += ev["calls"]
        elif ev.get("estimated"):
            m["estimatedPriceCalls"] += ev["calls"]
        if ev["effort"]:
            m["efforts"][ev["effort"]] = m["efforts"].get(ev["effort"], 0) + ev["calls"]
    res = {}
    for k, m in sorted(acc.items(), key=lambda x: -x[1]["usd"]):
        prompt = m["input"] + m["cached"] + m["cacheWrite"]
        res[k] = {**{x: m[x] for x in ("calls", "input", "cached", "cacheWrite", "output", "thinking")},
                  "usd": round(m["usd"], 2), "missPct": round(100 * (1 - m["cached"] / prompt), 1) if prompt else None,
                  "efforts": m["efforts"], "priced": k in PRICES,
                  # false too when some calls were priced from a rule, not a published price (Fast long context)
                  "priceConfirmed": k in PRICES and k not in UNCONFIRMED and not m["estimatedPriceCalls"],
                  "unpricedCalls": m["unpricedCalls"], "estimatedPriceCalls": m["estimatedPriceCalls"]}
    return res


def cache_stats(events, since):
    """Same keys and meaning as the Antigravity block's cache. A prompt token is read (cached input) or missed
    (uncached input + cache writes). Calls are counted from the buckets."""
    calls = cold = read = miss = write = 0
    saved = missed = 0.0
    for ev in events:
        if ev["ts"] < since:
            continue
        calls += ev["calls"]
        cold += ev["cold"]
        read += ev["cacheRead"]
        write += ev["cacheWrite"]
        miss += ev["input"] + ev["cacheWrite"]
        pr = prices_for(ev["model"], ev["tier"], ev["long"])
        if pr:
            saved += ev["cacheRead"] * (pr[0] - pr[1]) / 1e6
            missed += (ev["input"] + ev["cacheWrite"]) * (pr[0] - pr[1]) / 1e6
    prompt = read + miss
    # both from the integer counts, to 2 decimals: 93.49 must not become 93.5 and then 94 on the page
    read_pct = round(100 * read / prompt, 2) if prompt else None
    return {"missPct": round(100 * miss / prompt, 2) if prompt else None,
            "coldCallsPct": round(100 * cold / calls, 1) if calls else None, "calls": calls, "readPct": read_pct,
            "readTokens": read, "missTokens": miss, "writeTokens": write,
            "savedUsd": round(saved, 2), "missUsd": round(missed, 2)}


def scan_complete(stats):
    return not any(stats.get(k) for k in ("pendingFiles", "staleFiles", "unreadableFiles", "malformedLines",
                                         "scanPendingFiles", "scanErrorFiles"))


def build(con, now, home=CODEX_HOME, cache_path=SCAN_DB, budget=None, *, timeline_inputs=None):
    import codex_accounts

    buckets, stats, scan_info = scan(home, cache_path, now, budget)
    events = events_from(buckets, now, scan_info["fold"])
    models = models_30d(events, now)
    # plan and plan start of the current account only: logs of another account (creator_account_id) say
    # nothing about this one's plan; unlabelled logs count while no other account shows up
    cur = core.kv_get(con, P.account_id_key)
    tags = qp.calibration_tags(cur, {t for t, *_ in stats["plans"] if t})
    plan, _, plan_from = plans_of(stats["plans"], tags) if tags != set() else (None, None, None)
    seen = PLAN_NAMES.get(plan, plan) if plan else None
    since = plan_since_kept(con, plan_from, cur)
    paths = codex_accounts.paths(home)
    records = codex_accounts.snapshot_records(paths["snapshots"])
    for rec in records:
        ident = rec.get("accountIdentity")
        if not isinstance(ident, dict):
            continue
        tag = qp.account_hash(codex_accounts.norm(ident.get("workspaceAccountID")))
        codex_accounts.remember_resets(con, tag, rec, now)
    timeline_accounts = [] if timeline_inputs is not None else None
    try:
        accounts, unattributed = codex_accounts.blocks(
            con, now, events, stats, paths, records=records, timeline_accounts=timeline_accounts)
    except Exception as e:
        accounts, unattributed = [], {"error": qp.error_code("codex-accounts", e)}
        if timeline_accounts is not None:
            timeline_accounts.clear()
    # Unlabelled calls still contribute to Mac-wide totals. They cannot prove
    # activity or inactivity for the workspace whose readings are on the card.
    own = [e for e in events if cur and e.get("acct") == cur]
    ambiguous = [e for e in events if not cur or not e.get("acct")]
    resets = codex_accounts.remember_resets(con, cur, None, now)
    info = qp.build_common(P, con, now, events, models, plan_seen=seen, since=since,
                           complete=scan_complete(stats), ambiguous=ambiguous, resets=resets, attribution_calls=own)
    info["trackedSince"] = first_session(home) or info.get("trackedSince")
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    info["cache"] = {"today": cache_stats(events, today0), "7d": cache_stats(events, now - 7 * 86400),
                     "30d": cache_stats(events, now - 30 * 86400)}
    m30 = now - 30 * 86400
    origin, accts = {}, set()
    for last, orig, acct, n, _tag in stats["perFile"]:
        if last and last >= m30 and n:
            origin[orig or "unknown"] = origin.get(orig or "unknown", 0) + 1
            accts.add(acct)
    recent = [e for e in events if e["ts"] >= m30]
    info["coverage"] = {
        "tokens": "exact", "usd": "estimate",
        "historySince": events[0]["day"] if events else None,
        "sessionFiles": stats["sessionFiles"], "compressedFiles": stats["compressedFiles"],
        "archivedFiles": stats["archivedFiles"], "duplicateFilesDropped": stats["duplicateFilesDropped"],
        "calls": sum(e["calls"] for e in events),
        "repeatsDropped": stats["repeatsDropped"],
        "unreadableFiles": stats["unreadableFiles"], "malformedLines": stats["malformedLines"],
        "unpricedCalls30d": sum(m["unpricedCalls"] for m in info["models"].values()),
        "unconfirmedPrices": sorted([k for k in info["models"] if k in UNCONFIRMED] +
                                    [f"{k} fast long" for k, m in info["models"].items() if m["estimatedPriceCalls"]]),
        "fastCalls30d": sum(e["calls"] for e in recent if e["tier"] == "priority"),
        "longContextCalls30d": sum(e["calls"] for e in recent if e["long"]),
        "sessionsByClient30d": dict(sorted(origin.items(), key=lambda x: -x[1])),
        "accounts30d": len(accts),
        "planSeen": plan, "planSeenAt": stats["planSeenAt"] if plan == stats["planSeen"] else None,
        "pricesChecked": PRICES_CHECKED, "priceSource": PRICE_SOURCE,
        "priceTier": "Standard; Fast-mode calls at Fast prices; prompts above 272K tokens at long-context prices",
        "notCounted": "Codex cloud tasks, code review on GitHub, ChatGPT chat, other computers, "
                      f"calls older than {DAYS} days",
        "copiesDropped": stats["copiesDropped"], "copyTokensDropped": stats["copyTokensDropped"],
        "planSince": since,
        "scanCached": scan_info["cached"], "scanRescannedFiles": scan_info["rescanned"], "scanMs": scan_info["ms"],
        # an unreadable log keeps its last good numbers (or none), so the sums are not complete either
        "scanComplete": scan_complete(stats),
        "scanPendingFiles": stats["pendingFiles"] + stats["staleFiles"],
        "scanErrorFiles": stats["unreadableFiles"],
    }
    info["accounts"], info["unattributed"] = accounts, unattributed
    if timeline_inputs is not None:
        from timeline import event_spend, provider_input
        visible = {qp.account_hash(a["uuid"]) for a in info["accounts"]}
        missing = (e for e in events if e.get("acct") not in visible)
        timeline_inputs["codex"] = provider_input(
            now, timeline_accounts, event_spend(missing), partial=not scan_complete(stats)
            or info["unattributed"].get("error") or any(a.get("error") for a in info["accounts"]))
    return info


def plan_since_kept(con, seen, account=None):
    """When the current plan began (epoch), as seen in the logs now, else as seen before (kv): the logs of
    the change age out after DAYS days, CodexBar's history does not. A later change seen in the logs wins.
    Kept per account (`account`: P.account_id_key's value, the account the logs call creator_account_id):
    "codex_plan_since@<account>"; the first account seen adopts the value kept before accounts were told
    apart. Without a known account the plain key is used."""
    key = PLAN_SINCE_KEY
    if account:
        key = f"{PLAN_SINCE_KEY}@{account}"
        if core.kv_get(con, key) is None and core.kv_get(con, PLAN_SINCE_KEY + "_owner") is None:
            core.kv_set(con, PLAN_SINCE_KEY + "_owner", account)
            if core.kv_get(con, PLAN_SINCE_KEY) is not None:
                core.kv_set(con, key, core.kv_get(con, PLAN_SINCE_KEY))
    old = core.kv_get(con, key)
    if seen and (not isinstance(old, (int, float)) or seen > old):
        core.kv_set(con, key, int(seen))
        return int(seen)
    return int(old) if isinstance(old, (int, float)) else None


def import_quota(con):
    """Both quota sources; what tick.py logs."""
    return qp.import_quota(P, con)


def block(con, now=None, *, timeline_inputs=None):
    """(state["codex"], short log entry). Never raises."""
    now = int(now or time.time())
    t0 = time.time()
    try:
        info = build(con, now, timeline_inputs=timeline_inputs)
        c = info["coverage"]
        return info, {"ms": round((time.time() - t0) * 1000), "scan": "cached" if c["scanCached"] else
                      f"read {c['scanRescannedFiles']} files", "calls": c["calls"],
                      "rates": sorted(info["rates"])}
    except Exception as e:
        err = qp.error_code("codex", e)
        return qp.error_block(P, err), "error: " + err


def main():
    if "--check" in sys.argv:
        import codex_check
        sys.exit(0 if codex_check.check() else 1)
    con = core.db()
    import_quota(con)
    b, log = block(con)
    con.commit()
    con.close()
    if "--sample" in sys.argv:
        # the real block with its long lists cut short: the shape the Swift side decodes
        smp = json.loads(json.dumps(b))
        smp["daily"] = smp.get("daily", [])[-3:]
        smp["weeks"] = smp.get("weeks", [])[:2]
        smp["drift"] = smp.get("drift", [])[-2:]
        for w in smp.get("windows", {}).values():
            if "series" in w:
                w["series"] = w["series"][:1] + w["series"][-3:]
        # the sample is in the repository: accounts by placeholder names, their lists cut short as well
        for i, a in enumerate(smp.get("accounts") or []):
            a["uuid"] = f"00000000-0000-4000-8000-{i + 1:012d}"
            a["name"], a["short"] = f"account{i + 1}@example.com", f"Account {i + 1}"
            a["quotaSource"] = {k: v for k, v in (a.get("quotaSource") or {}).items() if k != "account"}
            a["daily"], a["weeks"], a["drift"] = a.get("daily", [])[-3:], a.get("weeks", [])[:2], a.get("drift", [])[-2:]
            for w in (a.get("windows") or {}).values():
                if "series" in w:
                    w["series"] = w["series"][:1] + w["series"][-3:]
        with open(os.path.join(HERE, "codex-state-sample.json"), "w") as fh:
            json.dump(smp, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        print(json.dumps(log), file=sys.stderr)
        return
    print(json.dumps(b, ensure_ascii=False, indent=1))
    print(json.dumps(log), file=sys.stderr)


if __name__ == "__main__":
    main()
