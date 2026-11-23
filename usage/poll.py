#!/usr/bin/env python3
"""Polls the limits of all Claude Switcher accounts, following the VPN rule.

The rule: an account marked VPN-only in the switcher (VPN Happ enabled) is queried only while
the VPN is up, otherwise it would show up from the home IP. All other accounts are queried only
without the VPN. When a query is not allowed, the last known value stays, with the time it was
measured.

Two refresh modes (state.json `accounts[].refresh`):
  background  accounts that do not need the VPN. Every scheduled pass (every 5 minutes) checks them:
              the active one is queried every 5 minutes, the others every 15, whichever account is
              active in Claude. They cannot be queried while the VPN is up (status vpn_mismatch): the
              query would reach claude.ai from the VPN's address. The first pass after the VPN goes
              down queries them again.
  manual      the VPN-only account. Queried only while it is the active account in Claude and the VPN
              is up, that is, only while the owner is working in it (then every 5 minutes, and on every
              menu opening). Otherwise it is not queried at all, not even on demand, and keeps its last
              reading. The owner decided this: no background query may reach that account.
After the server refuses (429) the pause grows up to an hour. The login is never refreshed or changed:
if the server rejected the saved login, the account is marked and not queried again until the profile is
re-saved. Nothing here switches the account or starts or stops the VPN.

History: every answer becomes rows of the `samples` table (one row per window, source "poll"), which is
the whole reading history; `polls` logs every attempt. The full server answer (`polls.raw`, ~3 KB) is
kept for RAW_KEEP_DAYS and then dropped; the numbers stay in `samples`.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HELPER = f"{core.DATA}/bin/claude-limits-helper"
ACTIVE_EVERY = 5 * 60
OTHER_EVERY = 15 * 60
RAW_KEEP_DAYS = 14
# claude.ai response field -> window name in the database
WINDOWS = {
    "five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d_opus", "seven_day_sonnet": "7d_sonnet",
}


def helper(*args, timeout=60):
    try:
        p = subprocess.run([HELPER, *args], capture_output=True, text=True, timeout=timeout)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout"}
    except Exception as e:
        return {"ok": False, "status": "helper_failed", "detail": str(e)[:200]}


def vpn_state():
    """'on' (the expected VPN), 'off' (direct connection), 'foreign' (some other VPN) or 'unknown'.
    Limits are queried only in the first two states, and only for the accounts that belong to that state."""
    r = helper("vpn")
    return r.get("state") or ("on" if r.get("vpn") else "off")


def cookies_path(acc):
    live = f"{core.CLAUDE_DIR}/Cookies"
    saved = f"{core.SWITCHER_DIR}/profiles/{acc['id']}/Cookies"
    if acc["active"] and os.path.exists(live):
        return live, "live"
    return saved, "profile"


def window_rows(usage):
    """Server response -> [(window, percentage, reset time)]. Unknown windows are stored under their own name."""
    usage = usage or {}
    rows = {}

    def reset(v):
        try:
            return (core.parse_iso(v["resets_at"]) + 30) // 60 * 60 if v.get("resets_at") else None
        except Exception:
            return None

    for k, v in usage.items():
        if k in WINDOWS and isinstance(v, dict) and v.get("utilization") is not None:
            rows[WINDOWS[k]] = (float(v["utilization"]), reset(v))
    # new format: a list of limits, which also holds the separate weekly per-model limit (Fable)
    for lim in usage.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("percent") is None:
            continue
        kind = lim.get("kind")
        if kind == "session":
            win = "5h"
        elif kind == "weekly_all":
            win = "7d"
        elif kind == "weekly_scoped":
            name = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped").lower()
            win = "fable" if name == "fable" else f"scoped:{name}"
        else:
            win = f"limit:{kind}"
        rows.setdefault(win, (float(lim["percent"]), reset(lim)))
    # breakdown of the weekly limit by source, as the server itself computes it (in limit percent)
    week_reset = rows.get("7d", (None, None))[1]
    for r in ((usage.get("seven_day_breakdown") or {}).get("rows") or []):
        if isinstance(r, dict) and r.get("key") and r.get("percent_of_limit") is not None:
            rows[f"7d:{r['key']}"] = (float(r["percent_of_limit"]), week_reset)
    return [(w, p, ra) for w, (p, ra) in rows.items()]


def due(con, acc, now):
    last = con.execute("SELECT ts,status FROM polls WHERE acc=? ORDER BY ts DESC LIMIT 8", (acc["uuid"],)).fetchall()
    if not last:
        return True
    ts, status = last[0]
    if status == "auth_rejected":
        # wait until the switcher re-saves the profile (the login refreshes by itself on the next sign-in)
        prof = f"{core.SWITCHER_DIR}/profiles/{acc['id']}/Cookies"
        fresh = os.path.exists(prof) and os.path.getmtime(prof) > ts
        return fresh or (acc["active"] and now - ts > 3600)
    if status in ("rate_limited", "http_error", "timeout"):
        fails = 0
        for _, s in last:
            if s in ("rate_limited", "http_error", "timeout"):
                fails += 1
            else:
                break
        return now - ts >= min(3600, 300 * 2 ** (fails - 1))
    if status in ("vpn_mismatch", "keychain_denied"):
        return now - ts >= 60
    return now - ts >= (ACTIVE_EVERY if acc["active"] else OTHER_EVERY) - 20


def last_poll(con, acc):
    return con.execute("SELECT ts,status FROM polls WHERE acc=? ORDER BY ts DESC LIMIT 1", (acc["uuid"],)).fetchone()


def refresh_mode(acc):
    """("background", target seconds between readings) or ("manual", None): see the module description."""
    if acc["useHapp"]:
        return "manual", None
    return "background", ACTIVE_EVERY if acc["active"] else OTHER_EVERY


def compact(con, now):
    """Once a day, for data older than RAW_KEEP_DAYS: drop the full server answers (the numbers are in
    `samples`), and thin the server's weekly breakdown rows (`7d:claude_code` etc., read only for the
    current reading) to the readings where the value changed. The 5h / 7d / fable rows are never thinned:
    rates, forecasts, weekly peaks and series read every one of them.

    Thinning works inside one breakdown key of one weekly window (acc, win, resets_at): a row equal to both
    its neighbours there adds nothing to the step line and goes. The first and the last reading of every
    value in every week stay, so a week whose value never moved keeps two rows, the newest row of a key is
    never touched, and the step line is exactly what it was. A thinned row no longer sits at the time of the
    7d row of the same answer: read old breakdown rows as a step line (the value at t is the last row of that
    week at or before t), never by equal time. Running it twice changes nothing more.
    Returns None when it ran less than a day ago, else {"raw": answers dropped, "thinned": rows deleted}."""
    last = core.kv_get(con, "polls_compacted_at") or 0
    if now - last < 86400:
        return None
    cut = now - RAW_KEEP_DAYS * 86400
    raw = con.execute("UPDATE polls SET raw=NULL WHERE raw IS NOT NULL AND ts<?", (cut,)).rowcount
    # neighbours are looked up over all rows, old and new; only rows older than `cut` may go
    thin = con.execute("""
        DELETE FROM samples WHERE rowid IN (
          SELECT rowid FROM (
            SELECT rowid, ts, pct, lag(pct) OVER w AS prev, lead(pct) OVER w AS next FROM samples
            WHERE source='poll' AND win LIKE '7d:%'
            WINDOW w AS (PARTITION BY acc, win, resets_at ORDER BY ts))
          WHERE ts<? AND prev=pct AND next=pct)""", (cut,)).rowcount
    core.kv_set(con, "polls_compacted_at", now)
    con.commit()
    return {"raw": raw, "thinned": thin}


def poll(con, force=False, min_gap=0):
    """Polls the accounts. force: do not wait for the schedule; min_gap: but no more often than once per this many seconds
    (refresh on opening the CodexBar menu: without it every menu opening would hit the server)."""
    now = int(time.time())
    if not os.path.exists(HELPER):
        return [{"acc": "-", "status": "no_helper"}]
    state = vpn_state()
    up = state == "on"
    core.kv_set(con, "vpn_up", {"ts": now, "up": up, "state": state})
    con.commit()
    res, todo = [], []
    try:
        done = compact(con, now)
        if done:
            res.append({"acc": "compact", "status": f"raw={done['raw']},thinned={done['thinned']}"})
    except Exception as e:
        # housekeeping must never stop the queries; it is tried again on the next pass
        con.rollback()
        res.append({"acc": "compact", "status": f"error {type(e).__name__}: {str(e)[:120]}"})
    for acc in core.accounts():
        if refresh_mode(acc)[0] == "manual" and not acc["active"]:
            # the VPN-only account is never queried in the background: only while the owner works in it
            res.append({"acc": acc["short"], "status": "manual_idle"})
            continue
        if force:
            last = last_poll(con, acc)
            if last and min_gap and last[1] == "ok" and now - last[0] < min_gap:
                continue
            # a rejected login is not queried even on demand: wait until the profile is re-saved
            if last and min_gap and last[1] == "auth_rejected" and not due(con, acc, now):
                continue
        elif not due(con, acc, now):
            continue
        if state != ("on" if acc["useHapp"] else "off"):
            # querying is not allowed: write a mark at most once per 15 minutes so the log does not bloat
            last = last_poll(con, acc)
            if not last or last[1] != "vpn_mismatch" or now - last[0] > 900:
                con.execute("INSERT INTO polls(ts,acc,status,vpn,via) VALUES(?,?,?,?,?)",
                            (now, acc["uuid"], "vpn_mismatch", int(up), "-"))
            res.append({"acc": acc["short"], "status": "vpn_mismatch"})
            continue
        todo.append(acc)

    known = {acc["uuid"]: core.kv_get(con, f"last_ok:{acc['uuid']}") or {} for acc in todo}

    def ask(acc):
        path, via = cookies_path(acc)
        args = ["usage", "--cookies", path, "--vpn", "on" if acc["useHapp"] else "off"]
        # the plan rarely changes: one extra request per day is enough
        prev = known[acc["uuid"]]
        if not prev.get("plan") or now - (prev.get("planTs") or 0) > 86400:
            args.append("--plan")
        return acc, via, helper(*args)

    # queries for different accounts run in parallel: a refresh on menu opening takes seconds
    if len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            answers = list(pool.map(ask, todo))
    else:
        answers = [ask(acc) for acc in todo]
    for acc, via, r in answers:
        status = r.get("status", "helper_failed")
        raw = None
        if r.get("ok"):
            raw = json.dumps({"usage": r.get("usage"), "plan": r.get("plan"), "org": r.get("org")}, ensure_ascii=False)
            for win, pct, ra in window_rows(r.get("usage")):
                con.execute("INSERT OR REPLACE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                            (acc["uuid"], win, now, pct, ra, "poll"))
            prev = known[acc["uuid"]]
            core.kv_set(con, f"last_ok:{acc['uuid']}", {
                "ts": now, "usage": r.get("usage"), "org": r.get("org"),
                "plan": r.get("plan") or prev.get("plan"),
                "planTs": now if r.get("plan") else prev.get("planTs")})
        con.execute("INSERT INTO polls(ts,acc,status,vpn,via,raw) VALUES(?,?,?,?,?,?)",
                    (now, acc["uuid"], status, int(up), via, raw))
        res.append({"acc": acc["short"], "status": status, "via": via,
                    **({"windows": window_rows(r.get("usage"))} if r.get("ok") else {"detail": r.get("detail", "")})})
        con.commit()
    con.commit()
    return res


def check():
    """Self-check on made-up data in memory: who may be queried, and the daily compaction. The helper is
    replaced by a stub, so nothing is queried and neither usage.db nor the VPN is touched."""
    import sqlite3
    ok = True

    def say(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("ok    " if cond else "FAIL  ") + what)

    # --- the VPN rule and the manual account, on every path (scheduled, --force, --refresh)
    mod = sys.modules[__name__]
    saved = (mod.helper, mod.cookies_path, mod.HELPER, core.accounts)
    accs = [{"uuid": "u-a", "id": "a", "short": "a", "useHapp": False, "active": False},
            {"uuid": "u-b", "id": "b", "short": "b", "useHapp": False, "active": True},
            {"uuid": "u-v", "id": "v", "short": "v", "useHapp": True, "active": False}]
    calls, vpn = [], ["off"]

    def stub(*args, timeout=60):
        if args[0] == "vpn":
            return {"state": vpn[0]}
        calls.append((args[args.index("--cookies") + 1], args[args.index("--vpn") + 1]))
        return {"ok": False, "status": "http_error"}

    mod.helper, mod.cookies_path, mod.HELPER = stub, (lambda acc: (acc["id"], "stub")), __file__
    core.accounts = lambda: [dict(a) for a in accs]
    try:
        for active in ("b", "v"):
            for a in accs:
                a["active"] = a["id"] == active
            for state in ("on", "off", "foreign", "unknown"):
                for force, gap in ((False, 0), (True, 0), (True, 45)):
                    con = sqlite3.connect(":memory:")
                    con.executescript(core.SCHEMA)
                    vpn[0], calls[:] = state, []
                    poll(con, force=force, min_gap=gap)
                    asked = sorted(calls)
                    want = sorted((a["id"], "on" if a["useHapp"] else "off") for a in accs
                                  if state == ("on" if a["useHapp"] else "off") and (not a["useHapp"] or a["active"]))
                    say(asked == want, f"active {active}, VPN {state}, force {force}, gap {gap}: asked {asked}")
    finally:
        mod.helper, mod.cookies_path, mod.HELPER, core.accounts = saved

    # --- compaction: two old weeks and a recent one of one breakdown key, plus 7d rows that must all stay
    con = sqlite3.connect(":memory:")
    con.executescript(core.SCHEMA)
    D, now = 86400, 1796000000
    weeks = {now - 37 * D: [0, 0, 0], now - 30 * D: [0, 0, 0, 0, 0, 0], now - 23 * D: [0, 0, 0, 2, 2, 2, 3],
             now - 2 * D: [3, 3, 3, 3]}
    rows = []
    for ra, vals in weeks.items():
        for j, v in enumerate(vals):
            ts = ra - 6 * D + j * 3600
            rows += [("u", "7d:chat", ts, float(v), ra, "poll"), ("u", "7d", ts, float(v), ra, "poll"),
                     ("u", "7d:chat", ts, float(v), ra, "codexbar")]
    con.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO polls(ts,acc,status,raw) VALUES(?,?,?,?)",
                    [(now - 20 * D, "u", "ok", "{}"), (now - D, "u", "ok", "{}")])
    before = {(w, ts, src): p for w, ts, p, src in con.execute("SELECT win,ts,pct,source FROM samples")}
    res = compact(con, now)
    after = con.execute("SELECT win,ts,pct,resets_at,source FROM samples ORDER BY ts").fetchall()
    kept = [(ts, p, ra) for w, ts, p, ra, src in after if w == "7d:chat" and src == "poll"]
    # the inner 0 of the first week, 4 of the six zeros of the second, the inner 0 and 2 of the third.
    # Partitioned by key only (as first written) it took 11 and left no row at all of the second week:
    # the zeros of the weeks around it made every one of its rows an "equal neighbour".
    say(res == {"raw": 1, "thinned": 7}, f"7 breakdown rows thinned, one old answer dropped ({res})")
    say(sum(w == "7d" for w, *_ in after) == 20 and sum(s == "codexbar" for *_, s in after) == 20,
        "7d rows and other sources untouched")
    say({ra for *_, ra in kept} == set(weeks), "every week keeps rows of its own, even one whose value never moved")
    say(sum(ra == now - 2 * D for *_, ra in kept) == 4, "recent rows are never thinned")

    def week_of(ts):
        return next(r for r in weeks if r - 6 * D <= ts < r)
    step = all(next(p for t, p, r in reversed(kept) if r == week_of(ts) and t <= ts) == v
               for (w, ts, src), v in before.items() if w == "7d:chat" and src == "poll")
    say(step, "the step line of each week is unchanged at every old reading")
    core.kv_set(con, "polls_compacted_at", 0)
    say(compact(con, now) == {"raw": 0, "thinned": 0}, "a second run changes nothing")
    say(compact(con, now) is None, "at most once a day")
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    return ok


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    print(json.dumps(poll(core.db(), force="--force" in sys.argv), ensure_ascii=False, indent=1))
