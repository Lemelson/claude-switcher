#!/usr/bin/env python3
"""Self-check of codex.py (`python3 usage/codex.py --check` runs it): the log reader on made-up logs (repeats,
offsets, zstd, copies of forked sessions), the scan cache, the shared quota code through the Codex provider
(live import, account rule, saturated stretches, plan cut, rate), and the block built from this Mac's real data.
Prints one line per check."""
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import calib  # noqa: E402
import core  # noqa: E402
import quota_provider as qp  # noqa: E402
import codex  # noqa: E402
from codex import (ACC, FOLD_DAYS, NAME, P, RETRY_ERROR_S, _ZstdCli, bucket_usd, build, cache_stats,  # noqa: E402
                   events_from, import_quota, model_labels, new_state, plan_since, prices_for, read_session, scan)


def _log_line(**kw):
    return (json.dumps(kw) + "\n").encode()


def check_reader(say):
    """read_session() and scan() on made-up logs: repeats, model and tier changes, offsets, zstd, duplicates."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "read: " + what)

    def tc(ts, total, last, plan="plus"):
        u = lambda n: {"input_tokens": n[0], "cached_input_tokens": n[1], "cache_write_input_tokens": 0,
                       "output_tokens": n[2], "reasoning_output_tokens": n[3], "total_tokens": n[0] + n[2]}
        return _log_line(timestamp=ts, type="event_msg", payload={
            "type": "token_count", "info": {"total_token_usage": u(total), "last_token_usage": u(last)},
            "rate_limits": {"limit_id": "codex", "plan_type": plan,
                            "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1795610360}}})
    sid = "01a21200-b6ac-7e93-b92d-047885084ea3"
    lines = [
        _log_line(timestamp="2026-11-25T09:00:00.000Z", type="session_meta",
                  payload={"id": sid, "creator_account_id": "acct-1", "originator": "T3 Code"}),
        _log_line(timestamp="2026-11-25T09:00:00.100Z", type="event_msg",
                  payload={"type": "thread_settings_applied", "thread_settings": {"model": "gpt-6.2-sol",
                                                                                  "service_tier": "default"}}),
        _log_line(timestamp="2026-11-25T09:00:00.200Z", type="turn_context", payload={"model": "gpt-6.2-sol",
                                                                                       "effort": "medium"}),
        tc("2026-11-25T09:00:10.000Z", (1000, 0, 100, 40), (1000, 0, 100, 40)),
        tc("2026-11-25T09:00:11.000Z", (1000, 0, 100, 40), (1000, 0, 100, 40)),      # repeat: skipped
        tc("2026-11-25T09:00:30.000Z", (3000, 800, 150, 40), (2000, 800, 50, 0)),
        b'{"type": "event_msg", "payload": {"type": "token_count", broken\n',
        _log_line(timestamp="2026-11-25T09:05:00.000Z", type="event_msg",
                  payload={"type": "thread_settings_applied", "thread_settings": {"model": "gpt-6.2-sol",
                                                                                  "service_tier": "priority"}}),
        tc("2026-11-25T09:05:10.000Z", (303000, 300800, 250, 40), (300000, 300000, 100, 0)),
    ]
    with tempfile.TemporaryDirectory(prefix="codex-check-") as home:
        d = os.path.join(home, "sessions", "2026", "11", "25")
        os.makedirs(d)
        path = os.path.join(d, f"rollout-2026-11-25T09-00-00-{sid}.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"".join(lines[:6]) + b'{"timestamp":"2026-11-25T09:00:40.000Z","type":"event_msg","payl')
        cp = os.path.join(home, "scan.db")
        b1, s1, i1 = scan(home, cp, now=1795600000, old_cache=None)
        t(sum(v[0] for v in b1.values()) == 2 and s1["repeatsDropped"] == 1, "a repeated cumulative event counts once")
        t(not i1["cached"], "first scan reads the file")
        with open(path, "ab") as fh:
            fh.write(b'oad":{}}\n' + b"".join(lines[6:]))
        b2, s2, i2 = scan(home, cp, now=1795600000, old_cache=None)
        db = sqlite3.connect(cp)
        off = json.loads(db.execute("SELECT st FROM files WHERE path=?", (path,)).fetchone()[0])["offset"]
        db.close()
        t(off == os.path.getsize(path), "offset reaches the end of the grown file")
        t(sum(v[0] for v in b2.values()) == 3 and s2["malformedLines"] == 1,
          f"grown file read on from its offset (calls {sum(v[0] for v in b2.values())}, bad {s2['malformedLines']})")
        keys = sorted(b2)
        t(any("|priority|1|medium" in k for k in keys), f"fast tier and long context recorded ({keys})")
        t(s2["planSeen"] == "plus", "plan type seen in the logs")
        b3, _, i3 = scan(home, cp, now=1795600000, old_cache=None)
        t(i3["cached"] and b3 == b2, "unchanged: from the cache")
        # the same session compressed next to the plain file (a compression job mid-way): counted once
        try:
            from compression import zstd
            with open(path, "rb") as fh, zstd.open(path + ".zst", "wb") as zf:
                zf.write(fh.read())
            b4, s4, _ = scan(home, cp, now=1795600000, old_cache=None)
            t(b4 == b2 and s4["duplicateFilesDropped"] == 1 and s4["compressedFiles"] in (0, 1),
              "a compressed copy of the same session is counted once")
        except ImportError:
            t(True, "zstd module missing: compressed copy test skipped")
        ev = events_from(b2, 1795600000, i2["fold"])
        # call 1: 1000 uncached × 2 + 100 out × 10; call 2: 1200 uncached × 2 + 800 cached × 0.1 + 50 × 10;
        # call 3 (fast, long): 300000 cached × 0.20 × 2 + 100 × 15 × 2
        want = (1000 * 2 + 100 * 10 + 1200 * 2 + 800 * 0.1 + 50 * 10 + 300000 * 0.4 + 100 * 30) / 1e6
        t(abs(sum(e["usd"] for e in ev) - want) < 1e-9, f"dollars at list prices ({sum(e['usd'] for e in ev):.6f})")
        c = cache_stats(ev, 0)
        t(c["calls"] == 3 and c["coldCallsPct"] == 33.3 and c["readTokens"] == 300800 and c["missTokens"] == 2200,
          f"cache counts ({c})")
    ok &= check_copies(say)
    ok &= check_cache(say)
    t(prices_for("codex-auto-review", "default", 0) is None and bucket_usd("x", "default", 0, [1] * 7) is None,
      "a model without a price has no dollars")
    t(model_labels(["gpt-5.6-sol", "gpt-5.5", "codex-auto-review"]) ==
      {"gpt-5.6-sol": "Sol 5.6", "gpt-5.5": "GPT-5.5", "codex-auto-review": "codex-auto-review"}, "model labels")
    with tempfile.TemporaryDirectory(prefix="codex-plan-") as tmp:
        p = os.path.join(tmp, "plan.json")
        t(qp.read_plan(P, p, seen="Plus") == ("Plus", 20.0), "Plus seen in the logs: $20")
        t(qp.read_plan(P, p, seen="Pro") == ("Pro", None), "Pro: no confirmed price")
        with open(p, "w") as fh:
            fh.write('{"planName": "Pro", "planPriceUsd": 200}')
        t(qp.read_plan(P, p, seen="Plus") == ("Pro", 200.0), "the owner's file wins")
    return ok


def check_copies(say):
    """A forked session that starts with a copy of its parent's history: the copied calls (old turn ids, or
    no turn yet in a forked session) are left out, the fork's own calls count, the copied plan is ignored."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "copies: " + what)

    def u(n):
        return {"input_tokens": n[0], "cached_input_tokens": n[1], "cache_write_input_tokens": 0,
                "output_tokens": n[2], "reasoning_output_tokens": 0, "total_tokens": n[0] + n[2]}

    def tc(ts, total, last, plan):
        return _log_line(timestamp=ts, type="event_msg", payload={
            "type": "token_count", "info": {"total_token_usage": u(total), "last_token_usage": u(last)},
            "rate_limits": {"plan_type": plan}})
    parent = "019f75cb-31ad-7cd2-bedd-6972688a589d"   # 2026-07-18
    fork = "019f8302-aafc-7133-b55c-0674eaf72fca"     # 2026-07-21 04:50:17
    old_turn = "019f82b6-97ac-76a2-b2c5-3af4bec83fc8"  # 2026-07-21 03:26: a turn of the parent
    own_turn = "019f8302-b000-7000-8000-000000000001"  # just after the fork was created
    at = "2026-07-21T04:50:18.208Z"                    # the copy is stamped with the fork's time
    lines = [
        _log_line(timestamp=at, type="session_meta", payload={"id": fork, "forked_from_id": parent}),
        tc(at, (900, 0, 10), (900, 0, 10), "pro"),                          # before any turn_context: copy
        _log_line(timestamp=at, type="turn_context", payload={"model": "gpt-5.6-sol", "turn_id": old_turn}),
        tc(at, (2000, 0, 30), (1100, 0, 20), "pro"),                        # copied turn: copy
        tc(at, (3500, 1000, 50), (1500, 1000, 20), "pro"),
        _log_line(timestamp="2026-07-21T04:50:30.000Z", type="turn_context",
                  payload={"model": "gpt-5.6-sol", "turn_id": own_turn}),
        tc("2026-07-21T04:50:40.000Z", (5500, 2500, 80), (2000, 1500, 30), "plus"),   # own
        tc("2026-07-21T04:50:50.000Z", (8000, 4000, 100), (2500, 1500, 20), "plus"),  # own
    ]
    with tempfile.TemporaryDirectory(prefix="codex-fork-") as home:
        d = os.path.join(home, "sessions", "2026", "07", "21")
        os.makedirs(d)
        path = os.path.join(d, f"rollout-2026-07-21T07-50-17-{fork}.jsonl")
        with open(path, "wb") as fh:
            fh.write(b"".join(lines))
        st = read_session(path, new_state())
        n = sum(v[0] for v in st["b"].values())
        t(n == 2 and st["copies"] == 3, f"3 copied calls left out, 2 own calls counted (own {n}, copies {st['copies']})")
        t(sum(v[2] + v[3] for v in st["b"].values()) == 4500, "only the fork's own tokens are in the buckets")
        t(st["plan"] == "plus" and [r[0] for r in st["plans"]] == ["plus"], "the copied plan readings are ignored")
        # the same session without a parent and with its own turn only: nothing is a copy
        plain = [ln for i, ln in enumerate(lines) if i not in (1, 2, 3, 4)]
        plain[0] = _log_line(timestamp=at, type="session_meta", payload={"id": fork})
        with open(path, "wb") as fh:
            fh.write(b"".join(plain))
        st = read_session(path, new_state())
        t(st["copies"] == 0 and sum(v[0] for v in st["b"].values()) == 2, "a session with its own turns only: no copy")
    runs = [["pro", 100, 500, None], ["plus", 900, 2000, 850], ["plus", 3000, 4000, 2950]]
    t(plan_since(runs, "plus") == 850, "plan start: first reading of the new plan, back to its weekly window")
    t(plan_since([["plus", 10, 20, None]], "plus") is None, "plan start: none without another plan")
    t(plan_since(runs + [["pro", 2500, 2600, None]], "plus") == 2950, "plan start: after the last other reading")
    return ok


def check_cache(say):
    """scan()'s cache: the incremental merge equals a fresh scan, old buckets are folded per hour, the read
    budget leaves logs pending for the next pass, a broken log is retried later, the zstd CLI fallback."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "cache: " + what)

    def session(home, day, sid, calls):
        d = os.path.join(home, "sessions", *day.split("-"))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"rollout-{day}T09-00-00-{sid}.jsonl")
        tot = [0, 0]
        out = [_log_line(timestamp=f"{day}T09:00:00.000Z", type="session_meta", payload={"id": sid}),
               _log_line(timestamp=f"{day}T09:00:00.000Z", type="turn_context", payload={"model": "gpt-6.2-sol"})]
        for i, (inp, o) in enumerate(calls):
            tot[0] += inp
            tot[1] += o
            uu = lambda a, b: {"input_tokens": a, "cached_input_tokens": 0, "output_tokens": b, "total_tokens": a + b}
            out.append(_log_line(timestamp=f"{day}T09:{i:02d}:10.000Z", type="event_msg", payload={
                "type": "token_count", "info": {"total_token_usage": uu(*tot), "last_token_usage": uu(inp, o)}}))
        with open(path, "wb") as fh:
            fh.write(b"".join(out))
        return path

    now = 1795600000                                    # 2026-11-25
    with tempfile.TemporaryDirectory(prefix="codex-cache-") as home:
        cp = os.path.join(home, "scan.db")
        fresh = lambda: scan(home, os.path.join(home, f"fresh{time.time_ns()}.db"), now, 1e9, None)[0]
        session(home, "2026-11-24", "01a21200-0000-7000-8000-000000000001", [(100, 10), (200, 20)])
        old = session(home, "2026-09-01", "01a00000-0000-7000-8000-000000000002", [(300, 30), (400, 40), (50, 5)])
        b1, s1, _ = scan(home, cp, now, 1e9, None)
        t(b1 == fresh(), "first pass equals a fresh scan")
        t(all(int(k.split("|")[0]) % 60 == 0 for k in b1 if int(k.split("|")[0]) * 60 < now - FOLD_DAYS * 86400)
          and sum(v[0] for v in b1.values()) == 5, "buckets older than FOLD_DAYS are per hour, no call lost")
        # a grown log, a new log, a moved (archived) copy of the old one
        p3 = session(home, "2026-11-25", "01a21300-0000-7000-8000-000000000003", [(10, 1)])
        os.makedirs(os.path.join(home, "archived_sessions"))
        os.replace(old, os.path.join(home, "archived_sessions", os.path.basename(old)))
        b2, s2, _ = scan(home, cp, now, 1e9, None)
        t(b2 == fresh() and s2["sessionFiles"] == 3 and s2["archivedFiles"] == 1,
          "after a new, a moved and a removed log the merge equals a fresh scan")
        with open(p3, "ab") as fh:
            fh.write(_log_line(timestamp="2026-11-25T09:30:00.000Z", type="event_msg", payload={
                "type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 30, "output_tokens": 3, "total_tokens": 33},
                    "last_token_usage": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22}}}))
        b3, _, i3 = scan(home, cp, now, 1e9, None)
        t(b3 == fresh() and i3["rescanned"] == 1 and sum(v[0] for v in b3.values()) == 7,
          "a grown log is read on and merged once")
        # read budget: only the newest changed log per pass, the rest stays pending
        for i in range(3):
            session(home, f"2026-11-1{i}", f"01a21400-0000-7000-8000-00000000000{i}", [(5, 1)])
        _, s4, i4 = scan(home, cp, now, 0, None)
        _, s5, i5 = scan(home, cp, now, 0, None)
        b6, s6, _ = scan(home, cp, now, 1e9, None)
        t(i4["rescanned"] == 1 and s4["pendingFiles"] == 2 and s5["pendingFiles"] == 1 and s6["pendingFiles"] == 0
          and b6 == fresh(), "with no time left one log per pass, pending counted, then complete")
        # a broken compressed log: an error entry, tried again after RETRY_ERROR_S
        bad = os.path.join(home, "sessions", "2026", "11", "24", "rollout-x-01a21500-0000-7000-8000-000000000009.jsonl.zst")
        with open(bad, "wb") as fh:
            fh.write(b"not zstd at all")
        _, s7, _ = scan(home, cp, now, 1e9, None)
        _, _, i8 = scan(home, cp, now + 60, 1e9, None)
        _, _, i9 = scan(home, cp, now + RETRY_ERROR_S + 1, 1e9, None)
        t(s7["unreadableFiles"] == 1 and i8["rescanned"] == 0 and i9["rescanned"] == 1,
          "a broken log is an error entry, tried again after an hour")
        if shutil.which("zstd"):
            z = _ZstdCli(bad)
            failed = False
            try:
                list(z)
                z.close()
            except OSError:
                failed = True
            t(failed and z.proc.returncode not in (None, 0), "zstd CLI: a failed decompression raises, process reaped")
            good = p3 + ".zst"
            subprocess.run(["zstd", "-qf", p3, "-o", good], check=True)
            z = _ZstdCli(good)
            n = sum(1 for _ in z)
            z.close()
            t(n == 4 and z.proc.returncode == 0, "zstd CLI: lines of a good file, process reaped")
            os.remove(good)
        else:
            t(True, "zstd CLI missing: fallback test skipped")
    return ok


def check_round2(say):
    """Plan readings in one second, a log rewritten in place or moved while the read budget is spent, a log
    that cannot be read, the Astra and Fast long-context prices."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "logs: " + what)
    try:
        r = plan_since([["pro", 100, 100, None], ["plus", 200, 200, None], ["plus", 200, 200, 150]], "plus")
        t(r == 150, f"plan start: two runs in one second, one with its weekly start ({r})")
    except Exception as e:
        t(False, f"plan start: two runs in one second raised {type(e).__name__}")
    t(codex.plans_of([("", "plus", 5, []), ("", "pro", 5, [])])[0] ==
      codex.plans_of([("", "pro", 5, []), ("", "plus", 5, [])])[0], "plan seen: same second, same answer in any order")
    t(prices_for("gpt-6.2-astra", "default", 0)[3] == 10.0 and prices_for("gpt-6.2-astra", "default", 1)[3] == 20.0,
      "Astra cache write $10 / $20 (short / long)")
    t(codex.price_estimated("gpt-5.5", "priority", 1) and not codex.price_estimated("gpt-5.5", "priority", 0)
      and not codex.price_estimated("gpt-6.2-sol", "priority", 1), "Fast long context of 5.5 / 5.4 is an estimate")
    now = 1795600000
    ev = {"ts": now - 60, "model": "gpt-5.5", "priceAs": "gpt-5.5", "effort": None, "calls": 2, "input": 1,
          "cacheRead": 0, "cacheWrite": 0, "output": 1, "thinking": 0, "usd": 1.0, "priced": True, "estimated": True}
    m = codex.models_30d([ev], now)["gpt-5.5"]
    t(m["priceConfirmed"] is False and m["estimatedPriceCalls"] == 2, "a model with estimated calls is not confirmed")

    def log(path, n_in):
        uu = {"input_tokens": n_in, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": n_in + 1}
        return (_log_line(timestamp="2026-11-24T09:00:00.000Z", type="session_meta",
                          payload={"id": os.path.basename(path)[-42:-6]}) +
                _log_line(timestamp="2026-11-24T09:00:00.000Z", type="turn_context", payload={"model": "gpt-6.2-sol"}) +
                _log_line(timestamp="2026-11-24T09:01:00.000Z", type="event_msg", payload={
                    "type": "token_count", "info": {"total_token_usage": uu, "last_token_usage": uu}}))
    tokens = lambda b: sum(v[2] for v in b.values())
    with tempfile.TemporaryDirectory(prefix="codex-r2-") as home:
        cp = os.path.join(home, "scan.db")
        fresh = lambda: scan(home, os.path.join(home, f"fresh{time.time_ns()}.db"), now, 1e9, None)[0]
        d = os.path.join(home, "sessions", "2026", "11", "24")
        os.makedirs(d)
        a = os.path.join(d, "rollout-2026-11-24T09-00-00-01a21200-0000-7000-8000-00000000000a.jsonl")
        with open(a, "wb") as fh:
            fh.write(log(a, 123))
        scan(home, cp, now, 1e9, None)
        with open(a, "r+b") as fh:                      # same size, same inode, other numbers
            fh.write(log(a, 987))
        os.utime(a, ns=(time.time_ns(), time.time_ns() + 10**9))
        b1 = scan(home, cp, now, 1e9, None)[0]
        t(tokens(b1) == 987 and b1 == fresh(), f"a log rewritten at the same size is read again ({tokens(b1)})")
        with open(a, "r+b") as fh:                      # cut short, then grown past the old size
            fh.truncate(10)
        scan(home, cp, now, 1e9, None)
        with open(a, "wb") as fh:
            fh.write(log(a, 4567))
        b2 = scan(home, cp, now, 1e9, None)[0]
        t(tokens(b2) == 4567 and b2 == fresh(), "a log cut and grown again on the same inode: no mix of both")
        # a move to the archive while the budget reaches only a newer log: the session must not drop out
        os.makedirs(os.path.join(home, "archived_sessions"))
        os.replace(a, os.path.join(home, "archived_sessions", os.path.basename(a)))
        n = os.path.join(d, "rollout-2026-11-24T10-00-00-01a21300-0000-7000-8000-00000000000b.jsonl")
        with open(n, "wb") as fh:
            fh.write(log(n, 10))
        os.utime(n, ns=(time.time_ns() + 5 * 10**9,) * 2)
        b3, s3, _ = scan(home, cp, now, 0, None)
        t(tokens(b3) == 4577 and s3["pendingFiles"] == 1, f"moved log pending: its calls stay counted ({tokens(b3)})")
        b4, s4, _ = scan(home, cp, now, 0, None)
        t(tokens(b4) == 4577 and s4["pendingFiles"] == 0 and b4 == fresh(), "then read at its new path, once")
        # a log that cannot be read keeps its last good numbers and makes the scan incomplete
        with open(n, "ab") as fh:
            fh.write(b"\n")
        os.chmod(n, 0)
        try:
            b5, s5, _ = scan(home, cp, now + 60, 1e9, None)
        finally:
            os.chmod(n, 0o644)
        t(tokens(b5) == 4577 and s5["unreadableFiles"] == 1,
          f"an unreadable log keeps its last good numbers and counts as unreadable ({tokens(b5)})")
    return ok


def check_bucket_granularity():
    with tempfile.TemporaryDirectory() as home:
        now = 1795701600
        path = os.path.join(home, "cache.db")
        fold = codex._fold_min(now)
        key = f"{fold}|gpt-5.4|default|0|"
        buckets = {key: [1, 1, 10, 0, 0, 0, 0],
                   f"{fold - 60}|gpt-5.4|default|0|": [1, 1, 10, 0, 0, 0, 0]}
        db = codex._open_cache(path)
        db.executemany("INSERT INTO meta VALUES(?,?)", [
            ("home", home), ("schema", str(codex.CACHE_SCHEMA_V)), ("fold", str(fold)),
            ("merged", json.dumps(buckets))])
        db.commit()
        db.close()
        cached, _, info = scan(home, path, now + 7200, old_cache=None)
        events = events_from(cached, now + 7200, info["fold"])
        return [e["bucketSec"] for e in events] == [3600, 60]


def check_split_and_resets(say):
    import codex_accounts as ca
    import split_attrib as sa
    from provider_check import _mem

    ok = True

    def test(cond, message):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "attribution: " + message)

    start = 1795500000
    reset = start + calib.SPAN["7d"]
    for key in ("malformedLines", "unreadableFiles", "scanErrorFiles", "scanPendingFiles", "pendingFiles", "staleFiles"):
        blocked = sa.attribute([(start, 0, reset), (start + 120, 80, reset)], [], "7d", 1,
                               complete=codex.scan_complete({key: 1}))
        test(blocked.at(start + 120, 80)["unknownPct"] == 80, f"{key} blocks idle coverage")
    points = [(start, 0, reset), (start + 15, 3, reset), (start + 60, 6, reset), (start + 120, 9, reset)]
    bucket = {"ts": start, "usd": 6, "bucketSec": 60}
    for rate in (None, 1):
        for priced in (True, False):
            a = sa.attribute(points, [{**bucket, "priced": priced}], "7d", 1, rate=rate, bucket_sec=60)
            first = a.at(start + 15, 3)
            test(first["unknownPct"] == 3 and first["reason"] == "bucket_boundary",
                 f"sample-straddling bucket stays unknown (rate={rate}, priced={priced})")
            if rate and priced:
                test(a.intervals[2][2:5] == [3, 0, 0] and a.intervals[3][2] == 0,
                     "only the unreserved sample-straddling spend is eligible once the minute ends")
    a = sa.attribute(points, [{**bucket, "usd": 3}], "7d", 1, rate=1, bucket_sec=60)
    test(a.intervals[2][2:5] == [0, 0, 3],
         "spend that could all have contributed to earlier unknown growth is not counted again")
    a = sa.attribute(points, [], "7d", 1, bucket_sec=60, ambiguous=[bucket])
    test(a.at(start + 15, 3)["reason"] == "bucket_boundary", "unlabelled minute overlaps the sample boundary")
    a = sa.attribute([(start - 60, 0, reset), (start, 3, reset)], [bucket], "7d", 1, rate=1, bucket_sec=60)
    test(a.at(start, 3)["othersPct"] == 3, "a bucket starting at the reading has no activity before that reading")
    credit = start + 20
    a = sa.attribute([(start - 60, 42, reset), (start + 75, 3, reset), (start + 120, 6, reset)],
                     [bucket], "7d", 1, rate=1, resets=[credit], bucket_sec=60)
    test(a.at(start + 120, 6)["unknownPct"] == 6 and a.at(start + 120, 6)["reason"] == "bucket_boundary",
         "reset-straddling spend stays ambiguous on either side of the minute end")
    overlaps = sa.Activity([{"ts": start, "bucketSec": 3600}, {"ts": start + 60, "bucketSec": 60}],
                           60, True, (), (), "usd")
    test(overlaps.local.has(start + 600, start + 660), "mixed bucket ends preserve hour overlap")
    samples = [(start + offset, value, reset)
               for offset, value in ((0, 0), (120, 2), (180, 8), (900, 8), (960, 13))]
    events = [{"ts": start + 60, "usd": 30, "qusd": 3},
              {"ts": start + 240, "usd": 40, "qusd": 4}]
    a = sa.attribute(samples, events, "7d", 1, rate=2, pairs=1, spend_key="qusd", bucket_sec=60)
    test(a.at(start + 180, 8) == {"youPct": 6.0, "othersPct": 2.0, "unknownPct": 0.0,
                                  "method": "calibrated", "preliminary": True, "reason": None},
         "Codex quota dollars consume the FIFO balance once, before API mix")
    test(a.at(start + 960, 13)["youPct"] == 6, "an unspent budget expires after the lag grace")

    review = {"ts": start + 900, "usd": 0, "qusd": 0, "priced": False, "model": "codex-auto-review"}
    a = sa.attribute(samples, [*events, review], "7d", 1, rate=2, spend_key="qusd", bucket_sec=60)
    s = a.at(start + 960, 13)
    test((s["youPct"], s["othersPct"], s["unknownPct"], s["reason"]) == (6, 2, 5, "unpriced_calls"),
         "an unpriced Codex call blocks the otherwise outside share")
    a = sa.attribute(samples, events, "7d", 1, rate=2, spend_key="qusd", bucket_sec=60, ambiguous=[review])
    test(a.at(start + 960, 13)["reason"] == "uncovered", "an unlabelled call is ambiguous in its interval")
    edge = [(start + 0, 0, reset), (start + 10, 0, reset),
            (start + 670, 0, reset), (start + 710, 9, reset)]
    overlap = {"ts": start + 60, "calls": 2, "priced": False}
    a = sa.attribute(edge, [overlap], "7d", 1, bucket_sec=60)
    test(a.at(start + 710, 9)["unknownPct"] == 9,
         "minute bucket overlaps the grace interval even when its start does not")
    hist = {**overlap, "bucketSec": 3600}
    a = sa.attribute(samples, [hist], "7d", 1, bucket_sec=60)
    test(a.at(start + 960, 13)["unknownPct"] == 13,
         "a historical bucket folded by hour still covers the original hour")

    con = _mem(samples=[(P.acc, "7d", ts, pct, reset, "live")
                        for ts, pct in ((start, 0), (start + 300, 10))])
    mixed_price = {"ts": start + 60, "usd": 50, "qusd": 20, "model": "test", "priceAs": "test",
                   "input": 1, "output": 1, "cacheWrite": 0, "cacheRead": 0,
                   "day": time.strftime("%Y-%m-%d", time.localtime(start))}
    measured = qp.build_common(P, con, start + 300, [mixed_price], {})
    rate = measured["rates"]["7d"]
    test(rate["pct_per_usd"] == 0.2 and rate["apiMix"] == 2.5
         and measured["windows"]["7d"]["split"]["youPct"] == 10,
         "the published rate uses API dollars while the card consumes quota dollars at the original rate")
    con.close()

    credit = start + 1500
    samples = [(start, 0, reset), (start + 1200, 42, reset), (start + 1600, 8, reset),
               (start + 1700, 15, reset)]
    con = _mem(samples=[(P.acc, "7d", ts, pct, ra, "live") for ts, pct, ra in samples])
    tag = qp.account_hash("workspace")
    record = {"snapshot": {"codexResetCredits": {"credits": [
        {"status": "redeemed", "redeemedAt": credit - ca.APPLE_EPOCH},
        {"status": "redeemed", "redeemed_at": credit},
        {"status": "available", "redeemed_at": credit + 1},
    ]}}}
    ca.remember_resets(con, tag, record, start + 1700)
    resets = ca.remember_resets(con, tag, None, start + 1800)
    test(resets == core.kv_get(con, f"codex_resets@{tag}") == [credit]
         and ca.remember_resets(con, "another", None, start + 1800) == [],
         "used credits survive disappearance from the snapshot and stay with their account")
    intervals = []
    b = qp.build_common(P, con, start + 1800, [], {}, resets=resets, weekly_intervals=intervals)
    w, week = b["windows"]["7d"], b["weeks"][0]
    test(w["series"][0] == [credit, 0.0, 0.0, 0.0, 0.0]
         and w["split"]["othersPct"] == 15 and week["peakPct"] == week["othersPct"] == 15
         and week["resetAt"] == credit and sum(r[3] for r in intervals) == 57,
         "a weekly card and peak restart at the credit while interval increments preserve earlier consumption")
    a = sa.attribute([(start + 1200, 42, reset), (start + 1600, 47, reset)], [],
                     "7d", 1, resets=resets)
    test(a.readings[start + 1600].start == credit and a.intervals[-1][3] == 47,
         "a confirmed credit beats a flat reset time and a rising next reading")
    spend = [{**mixed_price, "ts": start + 1320, "priced": False, "usd": 0, "qusd": 0},
             {**mixed_price, "ts": start + 1380}, {**mixed_price, "ts": start + 1560, "usd": 9, "qusd": 9}]
    waiting = qp.build_common(P, con, start + 1530, spend[:2], {}, resets=resets)
    w = waiting["windows"]["7d"]
    test(w["rolled"] and w["pct"] == 0 and w["start"] == credit
         and "forecast" not in w and "sinceMeasure" not in w and waiting["weeks"][0]["peakPct"] is None,
         "credit with no later reading clears stale window and weekly peak")
    near = qp.build_common(P, con, start + 1620, spend, {}, resets=resets)
    w = near["windows"]["7d"]
    test(not w["rolled"] and w["start"] == credit and w["usd"]["other"] == 9
         and "forecast" not in w and near["weeks"][0]["peakPct"] == 8
         and w["split"]["reason"] != "unpriced_calls",
         "fresh reading uses credit spend and age, with pre-reset unpriced calls outside grace")
    a = sa.attribute([(start - 600, 42, reset), (credit + 120, 8, reset)], [spend[0]], "7d", 1,
                     rate=1, resets=resets, bucket_sec=60)
    test(a.at(credit + 120, 8)["othersPct"] == 8, "known reset clamps grace for first post-reset reading")
    new_start = start + 7200
    a = sa.attribute([(new_start + 60, 7, new_start + calib.SPAN["7d"])], [], "7d", 1, resets=resets)
    test(a.intervals[0][0] == new_start, "older credit cannot set the start of a newer window")
    a = sa.attribute([(start + 1200, 42, reset), (credit + 60, 0, credit + 60 + calib.SPAN["7d"])],
                     [], "7d", 1, resets=resets)
    test(a.readings[credit + 60].start == credit, "zero-percent reset clock does not override a known redemption")
    ts = credit + 19000
    con.executemany("INSERT INTO samples VALUES(?,?,?,?,?,?)",
                    [(P.acc, "7d", credit, 42, reset, "live"), (P.acc, "7d", ts, 30, reset, "live")])
    same_second = qp.build_common(P, con, credit, [], {}, resets=resets)["windows"]["7d"]
    test(same_second["rolled"] and same_second["pct"] == 0, "a reading at redemption is not yet a fresh reading")
    later_spend = [{**mixed_price, "ts": stamp, "usd": 20, "qusd": 20}
                   for stamp in (start + 60, credit + 300, ts + 60)]
    later = qp.build_common(P, con, ts + 120, later_spend, {}, resets=resets)["windows"]["7d"]
    test(later["start"] == credit and later["usd"]["other"] == 40 and later.get("sinceMeasure", 0) > 0
         and later["forecast"]["pct"] == round(min(100, later["pct"] + later["sinceMeasure"]), 1),
         "forecast after redemption uses only the fresh reading and later spend")
    con.close()
    return ok


def check():
    """Self-check: the reader on made-up logs, the shared estimators through this provider, and the block
    built from this Mac's real data. Prints one line per check, exits 1 on a failure."""
    import antigravity
    ok = True

    def say(cond, what):
        nonlocal ok
        ok &= bool(cond)
        print(("ok    " if cond else "FAIL  ") + what)

    ok &= check_reader(say)
    # the live-file and estimator checks of the shared code, run once more through the Codex provider
    ok &= qp_checks(say)
    ok &= check_round2(say)
    ok &= check_accounts(say)
    ok &= check_account_isolation(say)
    ok &= check_split_and_resets(say)
    say(check_bucket_granularity(), "bucket widths use SQLite's stored fold between the daily refolds")
    import provider_check
    ok &= provider_check.check(P, say)
    con = core.db()
    n = import_quota(con)
    say(all(v == "absent" or isinstance(v, int) for v in n.values()), f"quota import ran ({n})")
    now = int(time.time())
    t = time.time()
    b = build(con, now)
    print(f"      build: {(time.time() - t) * 1000:.0f} ms (scan cached: {b['coverage']['scanCached']})")
    for k in ("uuid", "name", "short"):
        say(isinstance(b.get(k), str), f"Swift-required key {k} is a string")
    say(b["name"] == NAME and b["uuid"] == ACC and b["beta"] is True, "name/uuid/beta literal")
    # the same keys as the Antigravity block (antigravity-state-sample.json), except scan details in coverage
    sample = qp.load_json(os.path.join(HERE, "antigravity-state-sample.json")) or {}
    # Codex-only additions next to them: the per-account list and the calls no account owns
    say(set(sample) - set(b) <= set() and set(b) - set(sample) <= {"accounts", "unattributed"},
        f"top-level keys equal the Antigravity block's (missing {sorted(set(sample) - set(b))}, "
        f"extra {sorted(set(b) - set(sample))})")
    for k in ("usdToday", "usd7d", "usd30d", "tokensToday", "tokens7d", "tokens30d"):
        say(isinstance(b.get(k), (int, float)), f"{k} is a number")
    buckets, _, scan_info = scan(now=now)
    ev = events_from(buckets, now, scan_info["fold"])
    for key, since in (("usd7d", now - 7 * 86400), ("usd30d", now - 30 * 86400)):
        tot = round(sum(e["usd"] for e in ev if e["ts"] >= since), 2)
        say(abs(b[key] - tot) <= 0.05, f"{key} {b[key]} = sum of the calls {tot}")
    say(abs(sum(m["usd"] for m in b["models"].values()) - b["usd30d"]) <= 0.5, "models add up to usd30d")
    for win, w in b["windows"].items():
        say(win in ("5h", "7d"), f"window {win} is 5h or 7d")
        provider_check.check_split_shape(w, say)
    say(all(d["opus"] == 0 and d["fable"] == 0 for d in b["daily"]), "all Codex dollars are in other")
    since = b["coverage"]["planSince"]
    say(since is None or since == b["quotaSource"]["planSince"], f"plan start in coverage and quotaSource ({since})")
    if since:
        mem = qp.analysis_db(P, con, [], since)
        starts = [w[0] for kind in ("5h", "7d") for w in calib.past_windows(mem, ACC, kind, now, qp.DROP_PTS)]
        mem.close()
        say(all(x >= since - 60 for x in starts), f"no forecast-band window starts before the plan ({len(starts)} windows)")
        say(all(wk["start"] >= since - 120 for wk in b["weeks"]), "no week before the plan")
    for win, w in b["windows"].items():
        f = w.get("forecast")
        if f:
            want = round(min(100.0, w["pct"] + (w.get("sinceMeasure") or 0.0)), 1)
            say(f["pct"] == want, f"forecast {win} starts at reading + sinceMeasure ({f['pct']} = {want})")
    seen = [wk for wk in b["weeks"] if not wk["approx"] and not wk["current"]]
    say(all(wk["peakPct"] is not None for wk in seen), f"every past week with readings has its peak ({len(seen)} weeks)")
    say(core.kv_get(con, P.account_id_key) is not None and b["calibration"]["account"]["filter"] == "account",
        f"rates from the current account's calls ({b['calibration']['account']})")
    for win, r in b["rates"].items():
        w = b["windows"].get(win) or {}
        mix = r.get("apiMix", 1.0)
        say(abs(r["usd_per_100"] - r["quotaUsdPer100"] * mix) <= 0.02 * r["usd_per_100"] if "quotaUsdPer100" in r
            else True, f"rate {win}: API dollars = quota dollars x the window's mix ({mix})")

    say((b["value"] is None) == (b["valueReason"] is not None), "value xor valueReason")
    if b["value"]:
        say(b["value"]["confident"] == (b["value"]["reason"] is None), "value: confident xor reason")
    for span, c in b["cache"].items():
        say(c["readPct"] is None or abs(c["readPct"] + c["missPct"] - 100) < 1e-9, f"cache.{span} read + missed = 100")
    say(not any(k in b for k in qp.DROP), "Claude-only keys dropped")
    text = json.dumps({k: v for k, v in b.items() if k != "accounts"})
    say("@" not in text, "no e-mail-like text in the block (accounts[] aside)")
    ok &= check_real_accounts(say, b, con, now)
    say(os.path.expanduser("~") not in text, "no home path in the block")
    say(antigravity.P.acc != P.acc and P.live_key == "codex_live", "own samples account and live-file key")
    real = codex.build
    codex.build = lambda *a, **k: 1 / 0
    try:
        eb, log = codex.block(con, now)
    finally:
        codex.build = real
    say(set(eb) == {"uuid", "name", "short", "beta", "error"} and log.startswith("error:"),
        "a failure gives an error block, not an exception")
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    return ok


def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def _auth(path, aid, email, plan):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"auth_mode": "chatgpt", "tokens": {"account_id": aid, "refresh_token": "never-read", "id_token": _jwt(
            {"email": email, "https://api.openai.com/auth": {"chatgpt_account_id": aid, "chatgpt_plan_type": plan}})}},
            fh)


def check_accounts(say):
    """codex_accounts on made-up files: discovery (live + managed, dedupe, order, names, active), Apple
    reference-date conversion, snapshot matching and windows, credits, merged readings, plan, the stale
    5-hour window, and the split of the spend between accounts and `unattributed`."""
    import codex_accounts as ca
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "accounts: " + what)

    t(ca.apple_to_unix(817523368.11954) == 817523368.11954 + 978307200 and ca.apple_to_unix(None) is None
      and ca.apple_to_unix(True) is None, "Apple reference date + 978307200 = Unix time")
    t(abs(ca.apple_to_unix(817523368.11954) - 1795830568.11954) < 1e-6, "snapshot updatedAt 817523368 = 2026-11-28 01:49 UTC")
    A, B, C = "aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b", "cccccccc-0000-4000-8000-0000000000cc"
    now = 1795830600
    with tempfile.TemporaryDirectory(prefix="codex-accts-") as tmp:
        homes = os.path.join(tmp, "managed-codex-homes")
        p = {"auth": os.path.join(tmp, "codex", "auth.json"), "managed": os.path.join(tmp, "managed.json"),
             "snapshots": os.path.join(tmp, "snapshots.json"), "managedHomes": homes,
             "history": os.path.join(tmp, "history.json"), "live": os.path.join(tmp, "codex-quota.jsonl"),
             "names": os.path.join(tmp, "names.json"), "planFile": os.path.join(tmp, "codex-plan.json")}
        _auth(p["auth"], A, "plus.user@example.com", "plus")
        _auth(os.path.join(homes, "M2", "auth.json"), B, "pro.user@example.com", "prolite")
        with open(p["managed"], "w") as fh:
            json.dump({"version": 3, "accounts": [
                # the live account kept by CodexBar too (upper-case id, mixed-case e-mail): one entry
                {"id": "M1", "email": "Plus.User@Example.com", "providerAccountID": A.upper(), "workspaceAccountID": A.upper(),
                 "managedHomePath": os.path.join(homes, "M1")},
                {"id": "M2", "email": "pro.user@example.com", "providerAccountID": B, "workspaceAccountID": B,
                 "managedHomePath": os.path.join(homes, "M2")},
                {"id": "M3", "email": "third@example.com", "providerAccountID": C, "workspaceAccountID": C,
                 "managedHomePath": "/etc"}]}, fh)
        with open(p["names"], "w") as fh:
            fh.write(json.dumps({"11111111-claude-account": "Claude", B: "Second", A: "First"}))
        accts = ca.discover(p)
        t([a["uuid"] for a in accts] == [B, A, C], f"dedupe by lower-case id, names-file order, then e-mail "
          f"({[a['short'] for a in accts]})")
        t([a["short"] for a in accts] == ["Second", "First", "third@example.com"], "display name, else the e-mail")
        t([a["active"] for a in accts] == [False, True, False], "active = the account of ~/.codex/auth.json")
        t(accts[1]["managedId"] == "M1" and sorted(accts[1]["sources"]) == ["live", "managed"],
          "the live account keeps its managed id (promote by UUID)")
        t(accts[0]["authPlan"] == "prolite" and accts[2]["authPlan"] is None,
          "plan claim from a managed home inside CodexBar's folder only")
        T = now - 3600
        snaps = {"version": 1, "records": [
            {"id": "plus.user@example.com", "accountIdentity": {"workspaceAccountID": A, "normalizedEmail": "plus.user@example.com"},
             "credits": {"remaining": 120.5, "balanceReadSucceeded": True, "updatedAt": now - ca.APPLE_EPOCH - 5},
             "snapshot": {"updatedAt": T + 1800 - ca.APPLE_EPOCH + 0.4, "planObservation": {"rawPlan": "plus"},
                          "primary": {"usedPercent": 19, "windowMinutes": 300, "resetsAt": T + 3 * 3600 - ca.APPLE_EPOCH},
                          "secondary": {"usedPercent": 3, "windowMinutes": 10080, "resetsAt": T + 6 * 86400 - ca.APPLE_EPOCH},
                          "codexResetCredits": {"credits": [
                              {"status": "available", "expires_at": now + 86400 - ca.APPLE_EPOCH},
                              {"status": "available", "expires_at": now - 60 - ca.APPLE_EPOCH},     # expired
                              {"status": "used", "expires_at": now + 2 * 86400 - ca.APPLE_EPOCH}]}}},
            # weekly only (Pro 5x), matched by e-mail: no workspace id in the record
            {"id": "pro.user@example.com", "accountIdentity": {"normalizedEmail": "pro.user@example.com"},
             "snapshot": {"updatedAt": T + 1700 - ca.APPLE_EPOCH, "loginMethod": "prolite",
                          "secondary": {"usedPercent": 41, "windowMinutes": 10080, "resetsAt": T + 2 * 86400 - ca.APPLE_EPOCH}}}]}
        with open(p["snapshots"], "w") as fh:
            json.dump(snaps, fh)
        recs = ca.snapshot_records(p["snapshots"])
        t(ca.snapshot_for(recs, A, None) is recs[0] and ca.snapshot_for(recs, B, "pro.user@example.com") is recs[1]
          and ca.snapshot_for(recs, C, "third@example.com") is None, "snapshot matched by workspace id, else e-mail")
        r0 = ca.snapshot_readings(recs[0])
        t(sorted(r0) == [("5h", T + 1800, 19.0, T + 3 * 3600), ("7d", T + 1800, 3.0, T + 6 * 86400)],
          f"snapshot windows by windowMinutes, Unix seconds ({r0})")
        t([r[0] for r in ca.snapshot_readings(recs[1])] == ["7d"], "a weekly-only snapshot has no 5-hour reading")
        cr = ca.credits_of(recs[0], now)
        t(cr == {"remaining": 120.5, "note": None, "resetCredits": [now + 86400.0], "updatedAt": now - 5},
          f"credits: available, unexpired resets in Unix seconds ({cr})")
        t(ca.credits_of(recs[1], now) is None, "no credit data: null")
        kA, kB = ca.KEY_PREFIX + A, ca.KEY_PREFIX + B
        series = lambda name, pct, ra: {"name": name, "entries": [
            {"capturedAt": core.iso(T - 7200 + i * 600), "resetsAt": core.iso(ra), "usedPercent": pct + i} for i in range(3)]}
        with open(p["history"], "w") as fh:
            json.dump({"preferredAccountKey": kA, "accounts": {
                kA: [series("session", 5, T + 3 * 3600 - 7200), series("weekly", 1, T + 6 * 86400)],
                # B once had a 5-hour window: older than its weekly-only snapshot, so stale
                kB: [series("session", 50, T - 3600), series("weekly", 30, T + 2 * 86400)]}}, fh)
        with open(p["live"], "w") as fh:
            for ln in ({"ts": T + 1800, "windows": {"5h": {"pct": 19.0, "resetsAt": T + 3 * 3600},
                                                    "7d": {"pct": 3.0, "resetsAt": T + 6 * 86400}},
                        "plan": "Plus", "account": kA},
                       {"ts": T + 1000, "windows": {"7d": {"pct": 40.0, "resetsAt": T + 2 * 86400}}, "account": kB}):
                fh.write(json.dumps(ln) + "\n")
            fh.write('{"ts": 1, "partial')             # a line being written: not read
        con = sqlite3.connect(":memory:")
        con.executescript(core.SCHEMA)
        core.kv_set(con, P.account_key, qp.account_hash(kA))
        con.execute("INSERT INTO samples VALUES(?,?,?,?,?,?)", (ACC, "7d", T - 86400, 2.0, T + 6 * 86400, "live"))
        con.execute("INSERT INTO samples VALUES(?,?,?,?,?,?)",
                    (f"{ACC}:{qp.account_hash(kB)}", "7d", T - 86400, 20.0, T + 2 * 86400, "live"))
        live = ca.parse_live_lines([ln for ln in open(p["live"]).read().split("\n")[:-1] if ln.strip()])
        mem = ca.readings_db(con, kA, A, qp.load_json(p["history"]), live, recs[0])
        got = mem.execute("SELECT win, count(*), max(pct) FROM samples GROUP BY win ORDER BY win").fetchall()
        t(got == [("5h", 4, 19.0), ("7d", 5, 3.0)], f"readings merged: usage.db + history + live, snapshot = live line ({got})")
        mem.close()
        mem = ca.readings_db(con, kB, B, qp.load_json(p["history"]), live, recs[1])
        t(mem.execute("SELECT count(*) FROM samples WHERE pct=20.0").fetchone()[0] == 1,
          "a parked account's usage.db samples come along")
        mem.close()
        sid = lambda tag: qp.account_hash(tag) if tag else None
        ev = lambda ts, tag, usd: {"ts": ts, "model": "gpt-6.2-sol", "priceAs": "gpt-6.2-sol", "qusd": usd, "effort": None,
                                   "tier": "default", "long": 0, "calls": 1, "cold": 0, "input": 1000, "cacheRead": 0,
                                   "cacheWrite": 0, "output": 100, "thinking": 0, "usd": usd, "priced": True,
                                   "estimated": False, "acct": sid(tag),
                                   "day": time.strftime("%Y-%m-%d", time.localtime(ts))}
        events = [ev(T - 600, A, 1.25), ev(T + 600, A, 2.5), ev(T, B, 4.0), ev(T - 120, None, 8.0), ev(T - 60, "gone", 16.0)]
        stats = {"plans": [(sid(A), "plus", T, [["plus", T - 86400, T, None]])]}
        blocks, un = ca.blocks(con, now, events, stats, p)
        by = {b["uuid"]: b for b in blocks}
        t([b["uuid"] for b in blocks] == [B, A, C] and [b["active"] for b in blocks] == [False, True, False],
          "blocks in discovery order, one active")
        t(all(not b.get("error") for b in blocks), f"no account failed ({[b.get('error') for b in blocks]})")
        a, b2 = by[A], by[B]
        t(a["planName"] == "Plus" and a["planPriceUsd"] == 20.0 and a["planSource"] == "snapshot",
          f"Plus at $20 from the snapshot ({a['planName']}, {a['planPriceUsd']})")
        t(b2["planName"] == "Pro 5x" and b2["planPriceUsd"] is None, f"prolite = Pro 5x, no list price ({b2['planName']})")
        t(by[C]["planName"] is None and by[C].get("windows") == {}, "an account with nothing known: no plan, no windows")
        t(a["windows"]["5h"]["pct"] == 19.0 and a["windows"]["7d"]["pct"] == 3.0 and a["has5h"] is True
          and a["lastMeasuredAt"] == T + 1800, f"live windows of the active account ({a['lastMeasuredAt']})")
        t("5h" not in b2["windows"] and b2["has5h"] is False and b2["windows"]["7d"]["pct"] == 41.0,
          f"weekly-only account: stale 5-hour window hidden, has5h false ({sorted(b2['windows'])})")
        t(a["credits"]["remaining"] == 120.5 and b2["credits"] is None, "credits per account")
        t(abs(a["usd30d"] - 3.75) < 1e-9 and abs(b2["usd30d"] - 4.0) < 1e-9 and by[C]["usd30d"] == 0,
          f"spend per account from its own calls ({a['usd30d']}, {b2['usd30d']})")
        t(un["usd30d"] == 24.0 and un["noAccountCalls"] == 1 and un["otherAccountCalls"] == 1,
          f"calls of no listed account are unattributed ({un['usd30d']})")
        t(abs(sum(x["usd30d"] for x in blocks) + un["usd30d"] - sum(e["usd"] for e in events)) < 1e-9,
          "accounts + unattributed = all calls, nothing counted twice")
        need = ("uuid", "name", "short", "active", "planName", "planPriceUsd", "lastMeasuredAt", "windows", "usdToday",
                "usd7d", "usd30d", "usd24h", "tokens24h", "daily", "weeks", "models", "cache", "rates", "value",
                "drift", "quotaSource", "credits", "coverage")
        t(all(k in x for x in blocks for k in need), "every block has the Account keys and the extras")
        t(all(len(pt) == 5 for x in blocks for w in x["windows"].values() for pt in w.get("series", [])),
          "series has five columns in accounts too")
        with open(p["planFile"], "w") as fh:
            json.dump({"planName": "Owner plan", "planPriceUsd": 25, "accounts": {B: {"planName": "Pro 5x", "planPriceUsd": 100}}}, fh)
        blocks, _ = ca.blocks(con, now, events, stats, p)
        by = {b["uuid"]: b for b in blocks}
        t(by[B]["planPriceUsd"] == 100.0 and by[B]["planSource"] == "file" and by[A]["planName"] == "Owner plan"
          and by[C]["planName"] is None, "codex-plan.json: per-account entries; top-level fields = current account only")
        os.remove(p["auth"])
        os.remove(p["managed"])
        blocks, un = ca.blocks(con, now, events, stats, p)
        t(blocks == [] and un["calls"] == len(events), "no Codex account on the Mac: empty list, every call unattributed")
    return ok


def check_account_isolation(say):
    """Two workspaces of one login, an e-mail-only live account and an account with no id at all: no account
    takes another's snapshot, the live one stays active wherever it merges, every block has a uuid."""
    import codex_accounts as ca
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "account isolation: " + what)

    W1, W2, P1 = "11111111-0000-4000-8000-0000000000w1", "22222222-0000-4000-8000-0000000000w2", "pppppppp-provider"
    mail, solo = "shared@example.com", "solo@example.com"
    rec = lambda wid, m: {"id": m, "accountIdentity": {**({"workspaceAccountID": wid} if wid else {}),
                                                        "normalizedEmail": m}, "snapshot": {"accountEmail": m}}
    recs = [rec(W1, mail), rec(W2, mail)]
    t(ca.snapshot_for(recs, W1, mail) is recs[0] and ca.snapshot_for(recs, W2, mail) is recs[1],
      "each workspace gets its own record")
    t(ca.snapshot_for([recs[1]], W1, mail) is None and ca.snapshot_for([recs[0]], W2, mail) is None,
      "a workspace whose record is missing never takes the other workspace's record by e-mail")
    t(ca.snapshot_for(recs, None, mail) is None, "an account without an id: two matching records = none")
    t(ca.snapshot_for([recs[0]], None, mail, claimed={W1}) is None,
      "an account without an id never takes a record another listed account owns")
    bare = [rec(None, solo)]
    t(ca.snapshot_for(bare, None, solo) is bare[0] and ca.snapshot_for(bare, W1, solo) is bare[0],
      "a record without a workspace id: matched by a unique e-mail")
    t(ca.snapshot_for(bare + [rec(None, solo)], None, solo) is None, "two records with that e-mail: none")
    t(ca.snapshot_for(bare, W1, solo, email_shared=True) is None, "an e-mail two accounts share: no e-mail match")
    with tempfile.TemporaryDirectory(prefix="codex-iso-") as tmp:
        homes = os.path.join(tmp, "managed-codex-homes")
        p = {"auth": os.path.join(tmp, "codex", "auth.json"), "managed": os.path.join(tmp, "managed.json"),
             "snapshots": os.path.join(tmp, "snapshots.json"), "managedHomes": homes,
             "history": os.path.join(tmp, "history.json"), "live": os.path.join(tmp, "codex-quota.jsonl"),
             "names": os.path.join(tmp, "names.json"), "planFile": os.path.join(tmp, "codex-plan.json")}

        def write(path, obj):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(obj, fh)

        # the live auth.json names an e-mail only; CodexBar keeps it with a workspace id (and a different
        # provider id: the workspace wins, as CodexBar's effectiveWorkspaceAccountID)
        write(p["auth"], {"tokens": {"refresh_token": "never-read", "id_token": _jwt({"email": "Solo@Example.com"})}})
        write(p["managed"], {"version": 3, "accounts": [
            {"id": "M1", "email": solo, "providerAccountID": P1, "workspaceAccountID": W1.upper()},
            {"id": "M2", "email": mail, "workspaceAccountID": W2},
            {"id": "M3", "email": "noid@example.com"}]})
        write(p["names"], {})
        accts = ca.discover(p)
        by = {a["email"]: a for a in accts}
        t(by[solo]["uuid"] == W1 and by[solo]["active"] is True and sorted(by[solo]["sources"]) == ["live", "managed"],
          f"e-mail-only live merged into the managed workspace id stays active ({by[solo]})")
        t(sum(a["active"] for a in accts) == 1 and P1 not in [a["uuid"] for a in accts],
          "one active account; workspaceAccountID preferred over providerAccountID")
        noid = by["noid@example.com"]
        want = "email-hash:" + __import__("hashlib").sha256(b"noid@example.com").hexdigest()[:12]
        t(noid["uuid"] is None and ca.block_uuid(noid) == want and len(want) == len("email-hash:") + 12,
          f"an account without an id gets the stable uuid {want}")
        # two workspaces of one login plus the e-mail-only live login: the live one joins neither
        write(p["auth"], {"tokens": {"refresh_token": "never-read", "id_token": _jwt({"email": mail})}})
        write(p["managed"], {"version": 3, "accounts": [
            {"id": "M1", "email": mail, "workspaceAccountID": W1}, {"id": "M2", "email": mail, "workspaceAccountID": W2}]})
        accts = ca.discover(p)
        t(sorted(a["uuid"] or "-" for a in accts) == sorted(["-", W1, W2]) and
          [a["active"] for a in accts if a["uuid"] is None] == [True] and all(a["emailShared"] for a in accts),
          "an e-mail-only live login with two workspaces of that e-mail stays its own active entry")
        write(p["snapshots"], {"version": 1, "records": [
            dict(rec(W1, mail), snapshot={"accountEmail": mail, "updatedAt": 1795830000 - ca.APPLE_EPOCH,
                                          "secondary": {"usedPercent": 77, "windowMinutes": 10080}})]})
        con = sqlite3.connect(":memory:")
        con.executescript(core.SCHEMA)
        blocks, _ = ca.blocks(con, 1795830600, [], {"plans": []}, p)
        bb = {b["uuid"]: b for b in blocks}
        t(all(b["uuid"] for b in blocks) and not any(b.get("error") for b in blocks),
          f"every block has a non-empty uuid ({sorted(bb)})")
        t(bb[W1]["windows"].get("7d", {}).get("pct") == 77.0 and "7d" not in bb[W2]["windows"]
          and "7d" not in bb[ca.email_uuid(mail)]["windows"],
          "W1's snapshot reaches W1 only: not W2, not the e-mail-only login")
        con.close()
    return ok


def check_real_accounts(say, b, con, now):
    """The per-account list built from this Mac's real data."""
    import codex_accounts as ca
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "real accounts: " + what)

    accts = b.get("accounts")
    t(isinstance(accts, list), f"accounts is a list ({len(accts or [])})")
    if not accts:
        return ok
    found = ca.discover()
    t([a["uuid"] for a in accts] == [ca.block_uuid(a) for a in found], "one block per discovered account, same order")
    t(all(a["uuid"] for a in accts), "every block has a non-empty uuid")
    t(sum(1 for a in accts if a["active"]) == (1 if ca.auth_identity(ca.paths()["auth"]) else 0),
      "exactly one active account (the one of ~/.codex/auth.json)")
    t(all(not a.get("error") for a in accts), f"no account failed ({[a.get('error') for a in accts if a.get('error')]})")
    for a in accts:
        t(all(isinstance(a.get(k), str) for k in ("uuid", "name", "short")), f"{a['short']}: uuid/name/short strings")
        rest = json.dumps({k: v for k, v in a.items() if k not in ("name", "short")})
        t("@" not in rest and os.path.expanduser("~") not in rest, f"{a['short']}: no e-mail or home path outside name/short")
    un = b["unattributed"]
    for k in ("usd7d", "usd30d", "usd24h"):
        tot = round(sum(a.get(k) or 0 for a in accts) + (un.get(k) or 0), 2)
        t(abs(tot - b[k]) <= 0.05, f"{k}: accounts + unattributed {tot} = block {b[k]}")
    cur = next((a for a in accts if a.get("codexbarCurrent")), None)
    if cur:
        for win, w in (cur.get("windows") or {}).items():
            top = b["windows"].get(win) or {}
            t(w.get("pct") is not None and (top.get("measuredAt") or 0) <= (w.get("measuredAt") or 0),
              f"{cur['short']} {win}: reading at least as fresh as the block's ({w.get('pct')} %)")
    return ok


def qp_account_checks(t):
    """Account rule of the quota import: only the current CodexBar account's readings (history and live)."""
    import copy
    ok = True

    def tt(cond, what):
        nonlocal ok
        ok &= bool(cond)
        t(cond, what)
    con = sqlite3.connect(":memory:")
    con.executescript(core.SCHEMA)
    T = 1795424400
    ka, kb = "codex:v1:provider-account:aaaa", "codex:v1:provider-account:bbbb"

    def series(pct):
        return [{"name": "weekly", "entries": [{"capturedAt": core.iso(T + i * 3600), "resetsAt": core.iso(T + 86400 * 5),
                                                "usedPercent": pct + i} for i in range(3)]}]
    with tempfile.TemporaryDirectory(prefix="codex-acct-") as tmp:
        p = copy.copy(P)
        p.history, p.live = os.path.join(tmp, "codex.json"), os.path.join(tmp, "codex-quota.jsonl")

        def hist(pref):
            with open(p.history, "w") as fh:
                json.dump({"preferredAccountKey": pref, "accounts": {ka: series(10), kb: series(60)}}, fh)
            os.utime(p.history, ns=(time.time_ns(), time.time_ns()))

        def live(lines):
            with open(p.live, "a") as fh:
                for ln in lines:
                    fh.write(json.dumps(ln) + "\n")
        line = lambda ts, pct, acc=None: {"ts": ts, "windows": {"7d": {"pct": pct, "resetsAt": T + 86400 * 5}},
                                          **({"account": acc} if acc else {})}
        hist(ka)
        live([line(T + 100, 11.0), line(T + 200, 12.0, ka), line(T + 300, 70.0, kb)])
        qp.import_quota(p, con)
        pcts = sorted(r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (ACC,)))
        tt(pcts == [10.0, 11.0, 11.0, 12.0, 12.0], f"account A: its history, legacy and own live lines only ({pcts})")
        hist(kb)
        qp.import_quota(p, con)
        pcts = sorted(r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (ACC,)))
        tt(pcts == [60.0, 61.0, 62.0, 70.0], f"switch to B: only B's readings, legacy lines stay A's ({pcts})")
        parked = con.execute("SELECT count(*) FROM samples WHERE acc LIKE 'codex:%'").fetchone()[0]
        hist(ka)
        qp.import_quota(p, con)
        pcts = sorted(r[0] for r in con.execute("SELECT pct FROM samples WHERE acc=?", (ACC,)))
        tt(parked == 5 and pcts == [10.0, 11.0, 11.0, 12.0, 12.0], f"back to A: A's readings return ({pcts})")
        tt(core.kv_get(con, p.account_key) == qp.account_hash(ka) and "@" not in json.dumps(qp.quota_source(p, con, T)),
           "the block names the account by a short hash only")
    return ok


def qp_checks(say):
    """The parts of quota_provider that depend on the provider: live import into account "codex", the
    merge rule, and the rate on made-up stretches (method text names Codex)."""
    ok = True

    def t(cond, what):
        nonlocal ok
        ok &= bool(cond)
        say(cond, "shared: " + what)

    con = sqlite3.connect(":memory:")
    con.executescript(core.SCHEMA)
    T = 1795424400 + 60
    with tempfile.TemporaryDirectory(prefix="codex-live-") as tmp:
        path = os.path.join(tmp, "codex-quota.jsonl")
        t(qp.import_live(P, con, path) == "absent", "missing live file is 'absent'")
        with open(path, "w") as fh:
            fh.write(json.dumps({"ts": T, "source": "codexbar-live", "plan": "Plus",
                                 "windows": {"5h": {"pct": 49.0, "resetsAt": 1795610360},
                                             "7d": {"pct": 20.0, "resetsAt": 1795822832}}}) + "\n")
        t(qp.import_live(P, con, path) == 2, "a live line gives two samples")
    con.execute("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                (ACC, "7d", T + 100, 20.0, 1795822800, "codexbar"))
    rows = con.execute("SELECT DISTINCT acc FROM samples").fetchall()
    t(rows == [(ACC,)], "samples are stored as account 'codex'")
    t(len(qp.merged_samples(P, con)) == 2, "same clock hour: the live reading wins over CodexBar's")
    t(core.kv_get(con, P.plan_seen_key) == "Plus", "plan name from the live line remembered")
    ok &= qp_account_checks(t)
    # a window that runs into the top: no stretch may end at (or after) a reading of 100 %
    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    ra = T + 4 * 3600
    mem.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                    [(ACC, "5h", T + i * 600, pct, ra, "live") for i, pct in enumerate((0, 30, 60, 92, 100, 100))])
    evs = [{"ts": T + i * 600 + 300, "model": "gpt-6.2-sol", "priceAs": "gpt-6.2-sol", "usd": 4.0} for i in range(6)]
    st = qp.stretches(P, mem, qp.ModelSpend(evs), "5h", 0)
    t(st and all(x["t1"] < T + 4 * 600 for x in st) and len(st) == 3,
      f"no stretch ends at a saturated reading ({[(x['t1'] - T) // 600 for x in st]})")
    # readings before the plan began are not analysed (bands, rates, weeks)
    con2 = sqlite3.connect(":memory:")
    con2.executescript(core.SCHEMA)
    con2.executemany("INSERT INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)",
                     [(ACC, "7d", T - 86400 * 10, 50.0, T - 86400 * 5, "codexbar"),
                      (ACC, "7d", T + 60, 5.0, T + 86400 * 6, "codexbar")])
    m2 = qp.analysis_db(P, con2, [], since=T)
    t(m2.execute("SELECT min(ts) FROM samples").fetchone()[0] >= T, "samples before the plan start are cut")
    clean = [{"dp": 0.2 * u, "usd": u, "byModel": {"gpt-5.6-sol": u}} for u in (60, 80, 100)]
    e = qp.estimate(P, clean)
    t(e and round(e["usd_per_100"]) == 500 and "Codex spend" in e["method"], "rate and method text")
    return ok


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
