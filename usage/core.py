"""Shared code for tracking Claude spend and limits per account: paths, database, prices, accounts.

The data lives next to the switcher profiles:
  ~/Library/Application Support/ClaudeSwitcher/usage/usage.db
"""
import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
PROJECTS = f"{HOME}/.claude/projects"
CLAUDE_DIR = f"{HOME}/Library/Application Support/Claude"
SESSIONS = f"{CLAUDE_DIR}/claude-code-sessions"
SWITCHER_DIR = f"{HOME}/Library/Application Support/ClaudeSwitcher"
SWLOG = f"{HOME}/Library/Logs/ClaudeSwitcher/switcher.log"
DATA = f"{SWITCHER_DIR}/usage"
DB = f"{DATA}/usage.db"
CODEXBAR_HISTORY = f"{HOME}/Library/Application Support/com.steipete.codexbar/history/claude.json"
PRICE_CACHE = f"{HOME}/Library/Caches/CodexBar/model-pricing/models-dev-v1.json"
# $ per 1M tokens: (input, output, 5-minute cache write, cache read).
# Checked on 23.11.2026 against the official Anthropic price table. A 1-hour cache write = 2 × input.
PRICES = {
    "claude-fable-5-2": (8, 40, 10, 0.2),
    "claude-fable-5-1": (10, 50, 12.5, 0.25),
    "claude-fable-5": (10, 50, 12.5, 1),
    "claude-mythos-5": (10, 50, 12.5, 1),
    "claude-opus-5-5": (4, 20, 5, 0.2),
    "claude-opus-5": (5, 25, 6.25, 0.5),
    "claude-opus-4-8": (5, 25, 6.25, 0.5),
    "claude-opus-4-7": (5, 25, 6.25, 0.5),
    "claude-opus-4-6": (5, 25, 6.25, 0.5),
    "claude-opus-4-5": (5, 25, 6.25, 0.5),
    "claude-opus-4-1": (15, 75, 18.75, 1.5),
    "claude-opus-4": (15, 75, 18.75, 1.5),
    "claude-sonnet-5-5": (2, 10, 2.5, 0.1),
    "claude-sonnet-5": (2, 10, 2.5, 0.2),
    "claude-sonnet-4": (3, 15, 3.75, 0.3),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.01),
    "claude-haiku-4-5": (1, 5, 1.25, 0.1),
}
HAIKU55_LONG = (0.5, 2.5, 0.625, 0.05)  # requests longer than 100k tokens
_PRICE_KEYS = sorted(PRICES, key=len, reverse=True)


# The model families every per-model breakdown of a Claude account carries (`daily[].m`, `weeks[].m`,
# `weeks[].minePctByModel`), always all five, in this order.
MODEL_KEYS = ("opus", "fable", "sonnet", "haiku", "other")


def family(model):
    """Model family, used for limit calibration."""
    for f in ("fable", "mythos", "opus", "sonnet", "haiku"):
        if f in model:
            return "fable" if f == "mythos" else f
    return "other"


def price_of(model):
    for k in _PRICE_KEYS:
        if model.startswith(k):
            return k, PRICES[k]
    return None, None


def cost(model, inp, out, cw5, cw1h, cr, fast=False):
    k, p = price_of(model)
    if p is None:
        return None
    if k == "claude-haiku-5-5" and inp + cw5 + cw1h + cr > 100_000:
        p = HAIKU55_LONG
    pi, po, pw, pr = p
    c = (inp * pi + out * po + cw5 * pw + cw1h * pi * 2 + cr * pr) / 1e6
    return c * 2 if fast else c


# ---- prompt cache -------------------------------------------------------------------------------------
# One definition for the whole collector (state.json `cache` blocks and cache_compare_beta.py):
#   prompt tokens = cache read + plain input + cache write (5 min and 1 h)
#   read  = the part of the prompt served from the cache
#   miss  = everything else; a cache write is a miss on this call (it is paid at 1.25x or 2x of input)
#   readPct + missPct = 100
# savedUsd: what the cache reads saved against paying full input price for them.
# missUsd:  what the missed tokens cost above the cache-read price (what a perfect cache would have saved).

def cache_split(inp, cw5, cw1h, cr):
    """One Claude call -> (read, plain, written) prompt tokens."""
    return cr or 0, inp or 0, (cw5 or 0) + (cw1h or 0)


def cache_usd(model, inp, cw5, cw1h, cr, fast=False, long=None):
    """(saved, missed) dollars of one call, or of a sum of calls of one model, speed and prompt-length class
    (long: prompt over 100k tokens; taken from the tokens when not given). (None, None) if unpriced."""
    k, p = price_of(model)
    if p is None:
        return None, None
    if long is None:
        long = inp + cw5 + cw1h + cr > 100_000
    if k == "claude-haiku-5-5" and long:
        p = HAIKU55_LONG
    pi, _po, pw, pr = p
    x = 2 if fast else 1
    saved = cr * (pi - pr) * x / 1e6
    missed = (inp * (pi - pr) + cw5 * (pw - pr) + cw1h * (2 * pi - pr)) * x / 1e6
    return saved, missed


def cache_totals(con, periods):
    """Cache totals per account and period, in one pass over the rows.
    periods: {name: since_epoch}. Returns {acc: {name: {calls, read, plain, write, saved, missed}}}.
    Haiku 5.5 is priced per call (long prompts cost more), so its rows are split by prompt length."""
    # rows are summed once per band between consecutive period starts; a period is its band and all later ones
    names = sorted(periods, key=lambda n: periods[n], reverse=True)
    since = [int(periods[n]) for n in names]
    band = "CASE " + " ".join(f"WHEN ts>={s} THEN {i}" for i, s in enumerate(since)) + " END"
    sql = (f"SELECT acc, model, fast, (inp+cw5+cw1h+cr) > 100000, {band}, count(*), sum(cr), sum(inp), sum(cw5), "
           f"sum(cw1h) FROM rows WHERE ts>=? AND inp+cw5+cw1h+cr > 0 GROUP BY 1,2,3,4,5")
    out = {}
    for row in con.execute(sql, (since[-1],)):
        acc, model, fast, long, b = row[:5]
        n, cr, inp, cw5, cw1h = (v or 0 for v in row[5:])
        for name in names[b:]:
            t = out.setdefault(acc or "unknown", {}).setdefault(name, {
                "calls": 0, "read": 0, "plain": 0, "write": 0, "saved": 0.0, "missed": 0.0})
            read, plain, write = cache_split(inp, cw5, cw1h, cr)
            t["calls"] += n
            t["read"] += read
            t["plain"] += plain
            t["write"] += write
            # one sum per model, speed and prompt-length class: every row in it has the same price
            s, m = cache_usd(model, inp, cw5, cw1h, cr, bool(fast), bool(long))
            if s is not None:
                t["saved"] += s
                t["missed"] += m
    return out


def cache_merge(parts):
    """Adds several cache_totals() entries (dicts of one period) together."""
    t = {"calls": 0, "read": 0, "plain": 0, "write": 0, "saved": 0.0, "missed": 0.0}
    for p in parts:
        for k in t:
            t[k] += (p or {}).get(k, 0)
    return t


def cache_view(t):
    """cache_totals() entry of one period -> the state.json shape."""
    t = t or {}
    read, plain, write = t.get("read", 0), t.get("plain", 0), t.get("write", 0)
    prompt = read + plain + write
    read_pct = round(100.0 * read / prompt, 1) if prompt else None
    return {"readPct": read_pct, "missPct": round(100.0 - read_pct, 1) if prompt else None,
            "calls": t.get("calls", 0), "readTokens": read, "missTokens": plain + write, "writeTokens": write,
            "savedUsd": round(t.get("saved", 0.0), 2), "missUsd": round(t.get("missed", 0.0), 2)}


SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, offset INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0, born REAL);
CREATE TABLE IF NOT EXISTS rows(
  key TEXT PRIMARY KEY, ts INTEGER NOT NULL, model TEXT NOT NULL,
  inp INTEGER, out INTEGER, cw5 INTEGER, cw1h INTEGER, cr INTEGER,
  fast INTEGER NOT NULL DEFAULT 0, cost REAL,
  sid TEXT, parent TEXT, file_id INTEGER, entry TEXT, sub INTEGER,
  acc TEXT, how TEXT);
CREATE INDEX IF NOT EXISTS rows_ts ON rows(ts);
CREATE INDEX IF NOT EXISTS rows_acc_ts ON rows(acc, ts);
CREATE INDEX IF NOT EXISTS rows_sid ON rows(sid);
-- limit hits from Claude Code logs: at that moment the window was 100 % full
CREATE TABLE IF NOT EXISTS hits(
  key TEXT PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT, resets_at INTEGER,
  sid TEXT, parent TEXT, entry TEXT, acc TEXT, how TEXT, raw TEXT);
-- limit percentage samples: source = codexbar | poll
CREATE TABLE IF NOT EXISTS samples(
  acc TEXT NOT NULL, win TEXT NOT NULL, ts INTEGER NOT NULL, pct REAL NOT NULL,
  resets_at INTEGER, source TEXT NOT NULL, PRIMARY KEY(acc, win, ts, source));
CREATE TABLE IF NOT EXISTS polls(
  ts INTEGER NOT NULL, acc TEXT NOT NULL, status TEXT NOT NULL, vpn INTEGER, via TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS polls_acc_ts ON polls(acc, ts);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v, ensure_ascii=False)))


def short_name(name):
    """'client50030@example.com (Team)' -> 'client (Team)', 'first.last@example.com' -> 'first.last'."""
    base = re.sub(r"\d+$", "", re.split(r"[@ ]", name)[0]) or name
    m = re.search(r"\(([^)]+)\)", name)
    return f"{base} ({m.group(1)})" if m else base


NAMES_FILE = f"{DATA}/account-names.json"


def display_names(path=None):
    """The owner's own names for accounts: {account uuid, full Switcher name or derived short name: label}.
    Kept next to the data, outside the repository. A missing or broken file means no renames."""
    try:
        d = json.load(open(path or NAMES_FILE))
    except Exception:
        return {}
    return {k: v.strip() for k, v in d.items() if isinstance(v, str) and v.strip()} if isinstance(d, dict) else {}


def accounts():
    """Switcher accounts: [{uuid, name, short, id, useHapp, active, org}]."""
    try:
        store = json.load(open(f"{SWITCHER_DIR}/accounts.json"))
    except Exception:
        return []
    own = display_names()
    res = []
    for a in store.get("accounts", []):
        u = a.get("accountUuid")
        if not u:
            continue
        name = a.get("name") or u[:8]
        orgs = [os.path.basename(p) for p in glob.glob(f"{SESSIONS}/{u}/*") if os.path.isdir(p)]
        res.append({
            "uuid": u, "name": name, "id": a.get("id"),
            "short": own.get(u) or own.get(name) or own.get(short_name(name)) or short_name(name),
            "useHapp": bool(a.get("useHapp")), "active": a.get("id") == store.get("activeId"),
            "needsRelogin": bool(a.get("needsRelogin")), "orgs": orgs,
        })
    return res


def short_names():
    return {a["uuid"]: a["short"] for a in accounts()}


def session_accounts():
    """cliSessionId -> account uuid, from the Claude app session metadata."""
    m = {}
    for f in glob.glob(f"{SESSIONS}/*/*/local_*.json"):
        acc = f.split("/claude-code-sessions/")[1].split("/")[0]
        try:
            r = json.load(open(f))
        except Exception:
            continue
        for k in ("cliSessionId", "lastAssistantCliSessionId"):
            v = r.get(k)
            if isinstance(v, str) and v:
                m.setdefault(v, acc)
    return m


# The two switcher log lines that say which account is in the Claude app. Builds before November 2026 wrote
# them in Russian, newer ones in English; both forms are accepted, since an installed older build keeps writing
# the Russian ones until it is replaced. The checks are in check() below.
#   restore готово, в папке Claude: Cookies=… uuid=<uuid> oauth=…
#   restore done, in the Claude folder: Cookies=… uuid=<uuid> oauth=…
#   === Claude Switcher запущен. Аккаунты: …; в Claude сейчас: <uuid>|никто; Claude закрыт
#   === Claude Switcher started. Accounts: …; in Claude now: <uuid>|nobody; Claude closed
SW_RESTORE = ("restore готово", "restore done")
SW_START = ("=== Claude Switcher запущен", "=== Claude Switcher started")
SW_START_UUID = re.compile(r"(?:в Claude|in Claude now:)[^0-9a-f]*([0-9a-f]{8}-[0-9a-f-]{27})")


def switch_line_uuid(msg):
    """The account uuid that a switcher log message (without its timestamp) puts in Claude, or None."""
    mm = None
    if msg.startswith(SW_RESTORE):
        mm = re.search(r"uuid=([0-9a-f-]{36})", msg)
    elif msg.startswith(SW_START):
        mm = SW_START_UUID.search(msg)
    return mm.group(1) if mm else None


def switch_timeline():
    """[(epoch, uuid)]: which account was in the Claude app, from the switcher log."""
    tl = []
    rx = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
    try:
        fh = open(SWLOG, errors="replace")
    except Exception:
        return tl
    for line in fh:
        m = rx.match(line)
        if not m:
            continue
        u = switch_line_uuid(m.group(2))
        if u:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")  # the switcher logs in system local time
            tl.append((int(ts.timestamp()), u))
    tl.sort()
    return tl


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(ts, fmt="%d.%m %H:%M"):
    return datetime.fromtimestamp(ts).strftime(fmt)


def parse_iso(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# Plans: internal rate-limit tier name -> (display name, subscription price in $ per month)
TIERS = {
    "default_claude_max_20x": ("Max 20x", 200),
    "default_claude_max_5x": ("Max 5x", 100),
    "default_claude_pro": ("Pro", 20),
}


def check():
    """Self-check of the switcher log parsing on made-up lines in a temporary file; never touches the real log."""
    import sys
    import tempfile
    ok = True

    def say(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("ok    " if cond else "FAIL  ") + what)

    a, b, c = ("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002",
               "cccccccc-0000-0000-0000-000000000003")
    files = "Cookies=4096 Cookies-journal=0 Local Storage=1200 Session Storage=none IndexedDB=0"
    lines = [  # (log line, the uuid it must give)
        (f"2026-11-01 10:00:00 === Claude Switcher запущен. Аккаунты: A={a}, B={b}(нужен вход); "
         f"в Claude сейчас: {a}; Claude закрыт", a),
        (f"2026-11-01 10:05:00 restore готово, в папке Claude: {files} uuid={b} oauth=oauth:tokenCache", b),
        ("2026-11-01 10:06:00 === Claude Switcher запущен. Аккаунты: ; в Claude сейчас: никто; Claude открыт", None),
        ("2026-11-01 10:07:00 restore готово, в папке Claude: Cookies=none uuid=- oauth=", None),
        (f"2026-11-02 10:00:00 === Claude Switcher started. Accounts: A={a}, B={b}(needs sign-in); "
         f"in Claude now: {c}; Claude closed", c),
        (f"2026-11-02 10:05:00 restore done, in the Claude folder: {files} uuid={a} oauth=oauth:tokenCache", a),
        ("2026-11-02 10:06:00 === Claude Switcher started. Accounts: ; in Claude now: nobody; Claude open", None),
        ("2026-11-02 10:07:00 restore done, in the Claude folder: Cookies=none uuid=- oauth=", None),
        (f"2026-11-02 10:08:00 restore “A”: Cookies=4096 uuid={b} oauth=oauth:tokenCache", None),
        (f"2026-11-02 10:09:00 switch: “A” ({a}), in Claude now {b}", None),
    ]
    for line, want in lines:
        got = switch_line_uuid(line[20:])
        say(got == want, f"{line[20:56]}… -> {got}")

    mod = sys.modules[__name__]
    saved = mod.SWLOG
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write("\n".join(line for line, _ in lines) + "\n")
    try:
        mod.SWLOG = fh.name
        tl = switch_timeline()
    finally:
        mod.SWLOG = saved
        os.remove(fh.name)
    say([u for _, u in tl] == [a, b, c, a], f"timeline from a mixed old/new log: {len(tl)} entries in order")
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    return ok


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
