"""Self-checks of quota_provider.py on made-up data (no real file is read or written): the forecast from the
final rate, weeks from the windows the readings show, sessions and drift beside a saturated reading,
the account rule for plans and unknown accounts, calls of the current account only for the rates, error
codes, cache shares and the calendar start. Run by `codex.py --check` (check(say) below)."""
import copy
import json
import os
import sqlite3
import tempfile
import time
from unittest.mock import patch

import calib
import core
import quota_provider as qp

SPAN7 = calib.SPAN["7d"]


def _mem(rows=(), samples=()):
    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    mem.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", samples)
    mem.executemany("INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,fast,cost,acc) VALUES(?,?,?,?,0,0,0,0,0,?,?)",
                    rows)
    return mem


def check_forecast(p, t):
    """G1: the forecast starts from the reading plus the spend after it at the final rate."""
    T = 1795600000
    start, ra = T, T + 5 * 3600
    points = [(start + i * 1200, pct) for i, pct in enumerate((10, 40, 70, 95))]
    w = {"pct": 95.0, "measuredAt": start + 3600, "resetsAt": ra, "start": start, "sinceMeasure": 6.0,
         "rolled": False, "forecast": {"pct": 95.0, "endPct": 99.0, "hitAt": None}}
    now = start + 3600 + 600
    qp.refresh_forecast(p, "5h", w, points, now, {})
    f = w["forecast"]
    t(f["pct"] == 100.0 and f["endPct"] == 100.0 and f["hitAt"] == now,
      f"forecast: 95 % read + 6 points spent since -> at 100 % now ({f['pct']}, hit {f['hitAt'] and f['hitAt'] - now})")
    w2 = dict(w, sinceMeasure=1.0, forecast={"pct": 0})
    qp.refresh_forecast(p, "5h", w2, points, now, {})
    t(w2["forecast"]["pct"] == 96.0, "forecast: starts at reading + sinceMeasure (96)")
    w3 = dict(w, start=now - 120, measuredAt=now)
    qp.refresh_forecast(p, "5h", w3, [(now, w["pct"])], now, {})
    t("forecast" not in w3, "forecast: a newly reset window clears any previous forecast")


def check_weeks(p, t):
    """G2: weeks follow the reset times the readings give, even when a reset came early; whole weeks
    without readings are marked approximate; 0 % readings with moving reset times are no window."""
    now = 1795600000
    e3 = now + 2 * 86400                    # the current window
    e2 = e3 - SPAN7                         # the one before, ended as the current one began
    e1 = e2 - SPAN7 + 9 * 3600              # due 9 h after the next one began: it was reset early
    e0 = e1 - 3 * SPAN7                     # then two whole weeks without readings before e1's window
    smp = []
    for e, peak in ((e0, 40.0), (e1, 58.0), (e2, 29.0), (e3, 12.0)):
        for i in range(6):
            ts = e - SPAN7 + 3600 + i * 86400 // 2
            if ts < now:
                smp.append((p.acc, "7d", ts, round(peak * (i + 1) / 6, 1), e + (60 if i == 2 else 0), "live"))
    smp += [(p.acc, "7d", e0 + 3600 + i * 300, 0.0, e0 + 3600 + i * 300 + SPAN7, "live") for i in range(20)]
    gap_week = e1 - SPAN7 - SPAN7 // 2      # spend inside the gap's later week
    rows = [("a", e1 - SPAN7 + 7200, "m", 100, 3.0, p.acc), ("b", gap_week, "m", 50, 1.0, p.acc),
            ("c", e3 - SPAN7 + 7200, "m", 10, 2.0, p.acc)]
    mem = _mem(rows, smp)
    sp = calib.Spend(mem, p.acc, qp.model_key_identity)
    cl = qp.week_clusters(p, mem)
    t([c[2] for c in cl] == [e0, e1, e2, e3], f"weeks: one window per reset time, 0 % readings ignored ({len(cl)})")
    t(cl[1][1] == e2 - SPAN7 and cl[2][1] == e2, "weeks: a window ends where the next one began (early reset)")
    wk = qp.observed_weeks(p, mem, sp, now, None, clusters=cl)
    obs = [w for w in wk if not w["approx"]]
    t([w["peakPct"] for w in obs] == [12.0, 29.0, 58.0, 40.0] and wk[0]["current"],
      f"weeks: every window with readings has its peak ({[w['peakPct'] for w in obs]})")
    gaps = [w for w in wk if w["approx"]]
    t(len(gaps) == 1 and gaps[0]["peakPct"] is None and gaps[0]["usd"]["other"] == 1.0,
      "weeks: a week without readings but with spend is a row marked approx")
    t(obs[2]["usd"]["other"] == 3.0 and obs[2]["end"] == e2 - SPAN7 and obs[2]["scheduledEnd"] == e1,
      "weeks: spend within the window's own boundaries")
    wk2 = qp.observed_weeks(p, mem, sp, now, None, clusters=cl, since=e2 - SPAN7)
    t(all(w["start"] >= e2 - SPAN7 - 120 for w in wk2) and len(wk2) == 2, "weeks: nothing before the plan start")


def check_sessions_drift(p, t):
    """G6: a session interval stops before a saturated reading; drift uses the stretch estimator per window."""
    T = 1795600000
    r5, r7 = T + 5 * 3600, T + 5 * 86400
    snaps = [(0, 10, 2), (1, 50, 10), (2, 100, 20), (3, 100, 25)]
    smp = []
    for i, a, b in snaps:
        smp += [(p.acc, "5h", T + i * 1800, float(a), r5, "live"), (p.acc, "7d", T + i * 1800, float(b), r7, "live")]
    _, n, so_far = qp.sessions(p, _mem(samples=smp), 0)
    t(n == 1 and so_far == 5.0, f"sessions: the interval ends before 100 % (40/8 = 5.0, got {so_far})")
    # drift: 3 clean stretches inside one window
    e = T + SPAN7
    smp = [(p.acc, "7d", T + i * 3600, 20.0 * i, e, "live") for i in range(4)]
    evs = [{"ts": T + i * 3600 + 1800, "model": "m", "priceAs": "m", "usd": 150.0, "qusd": 100.0} for i in range(3)]
    mem = _mem([(str(i), ev["ts"], "m", 1, ev["usd"], p.acc) for i, ev in enumerate(evs)], smp)
    ms, ms_api = qp.ModelSpend(evs, 0, "qusd"), qp.ModelSpend(evs, 0)
    d = qp.drift(p, mem, calib.Spend(mem, p.acc, qp.model_key_identity), ms, ms_api, qp.week_clusters(p, mem), 0)
    t(len(d) == 1 and d[0]["quotaUsdPer100"] == 500.0 and d[0]["apiMix"] == 1.5 and d[0]["usd_per_100"] == 750.0
      and d[0]["weekEnd"] == e and d[0]["confident"] is False,
      f"drift: quota $/100 % x the week's mix, with confidence ({d})")


def check_accounts(p0, t):
    """G3: plan names follow the account; lines of accounts named while none is known stay apart; the
    rates use the current account's calls only."""
    T = 1795424400
    ka, kb = "codex:v1:provider-account:aaaa", "codex:v1:provider-account:bbbb"
    with tempfile.TemporaryDirectory(prefix="qp-acct-") as tmp:
        p = copy.copy(p0)
        p.history, p.live = os.path.join(tmp, "none.json"), os.path.join(tmp, "live.jsonl")
        con = _mem()
        line = lambda ts, pct, acc, plan: json.dumps({"ts": ts, "windows": {"7d": {"pct": pct, "resetsAt": T + 86400}},
                                                       "account": acc, "plan": plan}) + "\n"
        with open(p.live, "w") as fh:
            fh.write(line(T, 20.0, ka, "Plus") + line(T + 60, 80.0, kb, "Pro"))
        qp.import_quota(p, con)
        cur = [r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (p.acc,))]
        parked = [r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (f"{p.acc}:{qp.account_hash(ka)}",))]
        t(cur == [80.0] and parked == [20.0] and core.kv_get(con, p.plan_seen_key) == "Pro",
          f"no history: each named account apart, the last one current with its plan ({cur}, {parked})")
        with open(p.history, "w") as fh:
            json.dump({"preferredAccountKey": ka, "accounts": {ka: [], kb: []}}, fh)
        qp.import_quota(p, con)
        cur = [r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (p.acc,))]
        t(cur == [20.0] and core.kv_get(con, p.plan_seen_key) == "Plus",
          f"history names A: A's readings and plan come back ({cur}, {core.kv_get(con, p.plan_seen_key)})")
        if p.account_id:
            t(core.kv_get(con, p.account_id_key) == qp.account_hash("aaaa"), "account id of the logs kept as a hash")
        with open(p.history, "w") as fh:
            json.dump({"preferredAccountKey": kb, "accounts": {ka: [], kb: []}}, fh)
        os.utime(p.history, (T + 9, T + 9))
        qp.import_quota(p, con)
        t(core.kv_get(con, p.plan_seen_key) == "Pro", "switch to B: B's plan, not A's")
    ct = qp.calibration_tags
    t(ct("a", {"a"}) == {"a", ""} and ct("a", {"a", "b"}) == {"a"} and ct(None, {"a"}) is None
      and ct(None, {"a", "b"}) == set() and ct(None, set()) is None,
      "rates: own calls, unlabelled only while no other account shows up, none when ambiguous")


def check_small(p, t):
    """G10 error codes, G12 cache shares from integers and the calendar start."""
    err = qp.error_code("codex", PermissionError(13, "denied", os.path.expanduser("~/x/y.db")))
    t(err == "codex-failed:PermissionError" and "/" not in err, f"error: a code without path or text ({err})")
    ev = [{"ts": 10, "cacheRead": 9349, "input": 651, "cacheWrite": 0}]
    c = qp.cache_stats(ev, 0, lambda e: None, 0.1)
    t(c["readPct"] == 93.49 and c["missPct"] == 6.51, f"cache: shares from the integer counts ({c['readPct']})")
    now = int(time.time())
    cut = qp.calendar_cut(now)
    lt = time.localtime(cut)
    days = round((time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)) - cut) / 86400)
    t(lt.tm_hour == 0 and lt.tm_min == 0 and days == 179, f"calendar: 180 days start at local midnight ({days})")


def check(p, say):
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "provider: " + what)
    for f in (check_forecast, check_weeks, check_sessions_drift, check_accounts, check_small, check_attribution,
              check_encrypted_mtimes, check_claude_scan_coverage):
        try:
            f(p, t)
        except Exception as e:      # a broken check must fail, not stop the others
            t(False, f"{f.__name__} raised {type(e).__name__}: {e}")
    return ok


def check_attribution(p, t):
    import split_attrib as sa

    start = 1795500000
    reset = start + SPAN7
    samples = [(start, 0, reset), (start + 120, 7, reset),
               (start + 240, 6, reset), (start + 900, 6, reset), (start + 1500, 16, reset)]
    calls = [{"ts": start + 60, "usd": 50, "priced": True}]
    idle = sa.attribute(samples, [], "7d", 1)
    t(idle.at(start + 1500, 16)["method"] == "idle", "idle from start without a rate")
    tie = sa.attribute([(start + 60, 3.85, reset)], [], "7d", 1).at(start + 60, 3.85)
    t(tie["othersPct"] == round(3.85, 1), "split rounding matches the card's decimal tie")
    split = sa.attribute(samples, calls, "7d", 1).at(start + 1500, 16)
    t((split["youPct"], split["othersPct"], split["unknownPct"], split["method"]) == (0, 9, 7, "floor"),
      "cold start: settle jitter and a later idle jump give an outside lower bound")
    t(sa.attribute(samples, [], "7d", 2, encrypted_mtimes=[start - 86400] * 100)
      .at(start + 1500, 16)["othersPct"] == 16, "100 old encrypted files leave idle coverage intact")
    encrypted = sa.attribute(samples, [], "7d", 2, encrypted_mtimes=[start + 130])
    t(encrypted.at(start + 120, 7)["reason"] == "uncovered"
      and encrypted.at(start + 1500, 16)["othersPct"] == 8,
      "recent encrypted mtime affects its overlapping intervals and a later covered interval uses the floor")
    t(sa.attribute(samples, [], "7d", 1, complete=False).at(start + 1500, 16)["unknownPct"] == 16,
      "incomplete coverage prevents either idle attribution rule")
    for win, shift in (("7d", 360), ("5h", 600)):
        ra = start + calib.SPAN[win]
        reset_shift = [(start, 0, ra + 1200), (start + 700, 42, ra), (start + 1800, 43, ra + shift)]
        stable = sa.attribute(reset_shift, [], win, 1)
        t(sum(row[3] for row in stable.intervals) == 43,
          f"{win}: a small resetsAt drift preserves 43 points of growth")
    for win, shift in (("7d", 3601), ("5h", 601)):
        ra = start + calib.SPAN[win]
        reset_shift = [(start, 0, ra), (start + 700, 42, ra), (start + 3900, 43, ra + shift)]
        shifted = sa.attribute(reset_shift, [], win, 1)
        t(sum(row[3] for row in shifted.intervals) == 85,
          f"{win}: a reset time shift past the tolerance opens a new window")
    drop_samples = [(start, 0, reset), (start + 800, 42, reset), (start + 1000, 5, reset)]
    for offset in (700, 900):
        drop = sa.attribute(drop_samples, [{"ts": start + offset, "usd": 10}], "7d", 1, rate=2)
        t(drop.intervals[-1][2:5] == [0, 0, 5],
          f"unknown reset time: activity at {offset}s prevents definite attribution after the drop")
    drop_idle = sa.attribute(drop_samples, [], "7d", 1, rate=2)
    t(drop_idle.intervals[-1][2:5] == [0, 5, 0],
      "unknown reset time: a fully covered, idle interval can establish Others")
    next_samples = [*drop_samples, (start + 1200, 10, reset)]
    drop_later = sa.attribute(next_samples, [{"ts": start + 900, "usd": 10}], "7d", 1, rate=2)
    t(drop_later.at(start + 1200, 10)["youPct"] == 0,
      "unknown reset time: the next reading never reuses spend from the ambiguous reset gap")
    after_reset = [(reset - 60, 3, reset), (reset + 60, 0, reset + SPAN7 + 60)]
    a = sa.attribute(after_reset, [], "7d", 1)
    t(a.readings[reset + 60].start == reset and a.intervals[-1][-1] & sa.RESET_TAIL,
      "scheduled rollover is recognised even when a zero reading drops by less than four points")
    raw = [{"ts": start + 60, "usd": 1, "qusd": 1, "model": "test", "priceAs": "test",
            "input": 1, "output": 1, "cacheWrite": 0, "cacheRead": 0,
            "day": time.strftime("%Y-%m-%d", time.localtime(start))}]
    db = _mem(samples=[(p.acc, "7d", ts, pct, ra, "live") for ts, pct, ra in samples])
    info = qp.build_common(p, db, start + 1500, raw, {})
    w = info["windows"]["7d"]
    t(w["split"]["method"] == "floor" and w["mine"] is None and w["outside"] is None
      and w["series"][-1][2:] == [None, 9.0 if p.acc == "codex" else 8.0, 7.0 if p.acc == "codex" else 8.0],
      "card and series keep unknown, even when the flat reading was thinned")
    wk = info["weeks"][0]
    t(wk["peakAt"] == start + 1500 and wk["minePct"] is None and wk["splitMethod"] == "floor"
      and wk["othersPct"] + wk["unknownPct"] == wk["peakPct"], "weekly peak emits the same three-way split")
    db.close()


def check_encrypted_mtimes(p, t):
    import antigravity as ag

    with tempfile.TemporaryDirectory(prefix="provider-pb-") as tmp:
        directory = os.path.join(tmp, ag.agb.DB_DIRS[0])
        os.makedirs(directory)
        path = os.path.join(directory, "test.pb")
        with open(path, "wb") as fh:
            fh.write(b"encrypted")
        cache = os.path.join(tmp, "scan.json")
        stamp = 1795500000
        os.utime(path, (stamp, stamp))
        _, old, _ = ag.scan(tmp, cache)
        os.utime(path, (stamp + 1200, stamp + 1200))
        _, new, scan = ag.scan(tmp, cache)
        t(old["encryptedMtimes"] == [stamp] and new["encryptedMtimes"] == [stamp + 1200] and scan["cached"],
          "Antigravity reads current encrypted mtimes even on a priced-call cache hit")

        start = stamp + 3600
        now = start + 120
        reset = start + SPAN7
        db = _mem(samples=[(ag.ACC, "7d", start, 0, reset, "live"),
                           (ag.ACC, "7d", now, 5, reset, "live")])
        real_stat = os.stat

        def unreadable_pb(target, *args, **kwargs):
            if target == path:
                raise PermissionError(path)
            return real_stat(target, *args, **kwargs)

        try:
            good = ag.build(db, now, tmp, cache)
            t(good["windows"]["7d"]["split"]["method"] == "idle", "an old encrypted file permits idle")
            for scan_cache, label in ((cache, "a cache hit"), (os.path.join(tmp, "cold.json"), "a fresh scan")):
                inputs = {}
                with patch.object(ag.os, "stat", side_effect=unreadable_pb):
                    block = ag.build(db, now, tmp, scan_cache, timeline_inputs=inputs)
                split = block["windows"]["7d"]["split"]
                t(split["othersPct"] == 0 and split["unknownPct"] == 5
                  and inputs["antigravity"]["partial"],
                  f"encrypted file stat failure blocks idle on {label}")
        finally:
            db.close()


def check_claude_scan_coverage(p, t):
    import scan
    import timeline

    with tempfile.TemporaryDirectory(prefix="provider-claude-scan-") as tmp:
        path = os.path.join(tmp, "session.jsonl")
        now = 1795500600
        hole = now
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hole))
        with open(path, "wb") as fh:
            fh.write(f'{{"type":"assistant","timestamp":"{stamp}","usage":\n'.encode())
        start = now - 600
        reset = start + SPAN7
        con = _mem(samples=[("claude", "7d", start, 0, reset, "poll"),
                            ("claude", "7d", start + 60, 5, reset, "poll"),
                            ("claude", "7d", now, 10, reset, "poll")])
        try:
            with patch.object(core, "PROJECTS", tmp), patch.object(scan.time, "time", return_value=now):
                first = scan.scan(con)
                notice = core.kv_get(con, "claude_scan_incomplete_at")
                second = scan.scan(con)
            t(first["malformedUsageLines"] == 1 and second["malformedUsageLines"] == 0
              and second["files"] == 0 and notice == [[hole, hole]]
              and core.kv_get(con, "claude_scan_incomplete_at") == notice,
              "Claude retains a malformed-line coverage notice after advancing the file offset")
            cache = timeline.claude_input(con, [{"uuid": "claude"}], now)
            quota = cache["accounts"][0]["quota"]
            t(cache["partial"] and quota[-2][2:5] == [0, 5, 0] and quota[-1][2:5] == [0, 0, 5],
              "Claude blocks idle for the affected interval on later passes and keeps earlier covered growth")
        finally:
            con.close()


def check_split_shape(w, say):
    s = w["split"]
    parts = [s[k] for k in ("youPct", "othersPct", "unknownPct")]
    say(round(sum(parts), 1) == round(w["pct"], 1), "split accounts for the measured percentage")
    want = (None, None) if s["unknownPct"] else (s["youPct"], s["othersPct"])
    say((w["mine"], w["outside"]) == want and "server" not in w, "legacy split is present only for full attribution")
    say(all(len(p) == 5 for p in w.get("series", [])), "series has five columns")


if __name__ == "__main__":
    import codex

    raise SystemExit(0 if check(codex.P, lambda ok, msg: print("ok   " if ok else "FAIL ", msg)) else 1)
