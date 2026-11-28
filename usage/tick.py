#!/usr/bin/env python3
"""One tracking pass: read new log lines -> query limits -> recompute -> write the summary.

Run by a LaunchAgent every 5 minutes. A lock prevents parallel runs.

  tick.py            regular scheduled pass
  tick.py --force    query the limits of all accounts right now
  tick.py --refresh  the same, but no more than once per 45 seconds per account: this is how CodexBar
                     refreshes limits when its menu opens. The VPN rule applies as always.
  tick.py --switch   triggered right after Claude Switcher changes accounts: waits for any running pass,
                     always queries the newly active account immediately (ignoring the 45-second gap),
                     and still obeys the VPN rule and auth_rejected.
  tick.py --switch --provider codex
                     right after a Codex account switch: no Claude polling or log scan. Imports the Codex
                     readings and rebuilds `codex`, `spend24h` and Codex's overview inputs, keeping the rest of state.json
                     as the last pass wrote it (a full pass when there is no state.json yet).
"""
import fcntl
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calib  # noqa: E402
import core  # noqa: E402
try:
    import history_import  # noqa: E402  (private one-off import; not part of the repository)
except ImportError:
    history_import = None
import poll  # noqa: E402
import scan  # noqa: E402
import timeline  # noqa: E402
try:
    import antigravity  # noqa: E402  (Gemini through Antigravity: the state["antigravity"] block, beta)
    ANTIGRAVITY_ABSENT = None
except Exception:
    antigravity = None
    ANTIGRAVITY_ABSENT = "error: " + traceback.format_exc(limit=1).strip().splitlines()[-1]
try:
    import codex  # noqa: E402  (Codex on the ChatGPT plan: the state["codex"] block, beta)
    CODEX_ABSENT = None
except Exception:
    codex = None
    CODEX_ABSENT = "error: " + traceback.format_exc(limit=1).strip().splitlines()[-1]


def _arg_value(flag):
    try:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    except ValueError:
        return None


def _num(x):
    """A number from the summary, or 0 for anything else (None, an error string, a bool)."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else 0


def spend24h(st):
    """The top-level `spend24h` of state.json: API dollars and tokens of the last 24 hours (rolling) across
    every subscription on this Mac. Never raises: a missing or failed provider adds 0 and is marked."""
    try:
        accounts = [a for a in st.get("accounts") or [] if isinstance(a, dict)]
        good = [a for a in accounts if not a.get("error")]
        whole = st.get("claude24h") or {}
        claude = {"usd": 0.0, "tokens": 0, "accounts": [
            {"uuid": a.get("uuid"), "name": a.get("name"), "short": a.get("short"),
             "usd": round(_num(a.get("usd24h")), 2), "tokens": int(_num(a.get("tokens24h")))} for a in good]}
        if "usd" in whole:
            # every row of usage.db: also unattributed calls and accounts whose figures failed this time
            claude["usd"], claude["tokens"] = round(_num(whole["usd"]), 2), int(_num(whole.get("tokens")))
        else:
            claude["usd"] = round(sum(x["usd"] for x in claude["accounts"]), 2)
            claude["tokens"] = sum(x["tokens"] for x in claude["accounts"])
            if whole.get("error"):
                claude["error"] = whole["error"]
        if len(good) < len(accounts):
            claude["failedAccounts"] = len(accounts) - len(good)
        by = {"claude": claude}
        for key in ("codex", "antigravity"):
            b = st.get(key)
            if not isinstance(b, dict):
                continue
            if b.get("error") or "usd24h" not in b:
                by[key] = {"usd": 0.0, "tokens": 0, "error": b.get("error") or "no usd24h"}
            else:
                by[key] = {"usd": round(_num(b.get("usd24h")), 2), "tokens": int(_num(b.get("tokens24h")))}
        partial = bool(claude.get("error") or claude.get("failedAccounts") or any(v.get("error") for v in by.values()))
        res = {"usd": round(sum(v["usd"] for v in by.values()), 2),
               "tokens": int(sum(v["tokens"] for v in by.values())),
               "asOf": int(_num(st.get("generatedAt"))),
               "byProvider": by}
        if partial:
            res["partial"] = True
        return res
    except Exception:
        return {"error": "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]}


def main():
    os.makedirs(core.DATA, exist_ok=True)
    refresh = "--refresh" in sys.argv
    switch = "--switch" in sys.argv
    expected_uuid = _arg_value("--account")
    provider = _arg_value("--provider")
    if provider not in (None, "claude", "codex"):
        sys.exit(f"tick.py: unknown --provider {provider!r} (claude or codex)")
    codex_only = switch and provider == "codex"
    with open(f"{core.DATA}/tick.lock", "w") as lock:
        run_locked(lock, refresh, switch, expected_uuid, codex_only)


def run_locked(lock, refresh, switch, expected_uuid, codex_only):
    mode = ("switch-codex" if codex_only else "switch") if switch else ("refresh" if refresh else None)
    # A scheduled pass may hold the lock for half a minute. A refresh asked for by the open menu or by an
    # account switch waits for it instead of being dropped; a scheduled pass that meets another one just leaves.
    deadline = time.time() + (45 if switch else (30 if refresh else 0))
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                with open(f"{core.DATA}/tick.log", "a") as fh:
                    fh.write(json.dumps({"ts": core.local(int(time.time()), "%Y-%m-%d %H:%M:%S"),
                                         "skipped": "locked", **({"mode": mode} if mode else {})}) + "\n")
                if switch:
                    sys.exit(3)
                return
            time.sleep(0.2)
    con = core.db()
    t0 = time.time()
    force = switch or refresh or "--force" in sys.argv
    log = {"ts": core.local(int(t0), "%Y-%m-%d %H:%M:%S")}
    if mode:
        log["mode"] = mode
    if expected_uuid:
        log["account"] = expected_uuid

    def publish_state():
        st = calib.analyse(con)
        inputs = {}
        if antigravity:
            # never raises: a failure becomes {"error": ...} inside the block and the Claude part is written as always
            st["antigravity"], log["antigravity"] = antigravity.block(con, st["generatedAt"], timeline_inputs=inputs)
        if codex:
            # never raises either: same rule as the Antigravity block
            st["codex"], log["codex"] = codex.block(con, st["generatedAt"], timeline_inputs=inputs)
        # after both blocks: the menu bar's one number, every subscription on this Mac
        st["spend24h"] = spend24h(st)
        scan_log = log.get("scan")
        complete = (isinstance(scan_log, dict) and isinstance(log.get("attributed"), int)
                    and not scan_log.get("unreadableFiles"))
        timeline.publish(con, st, st["generatedAt"], inputs, claude_complete=complete)
        tmp = f"{core.DATA}/state.json.tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, f"{core.DATA}/state.json")
        return st

    def publish_codex_only():
        """Refresh Codex and the two combined views using the last Claude and Antigravity inputs."""
        old = None
        try:
            with open(f"{core.DATA}/state.json") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            pass
        if not isinstance(old, dict) or not codex:
            return publish_state()
        st = old
        now = int(time.time())
        inputs = {}
        st["codex"], log["codex"] = codex.block(con, now, timeline_inputs=inputs)
        st["spend24h"] = spend24h(st)
        timeline.publish(con, st, now, inputs, codex_only=True)
        coverage = st["overviewTimeline"].get("providers", {})
        if any(coverage.get(p, {}).get("builtAt") != now or coverage[p]["partial"] for p in timeline.PROVIDERS):
            st["spend24h"]["partial"] = True
        tmp = f"{core.DATA}/state.json.tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, f"{core.DATA}/state.json")
        return st

    st = None
    if codex_only:
        try:
            log["codexQuota"] = codex.import_quota(con) if codex else CODEX_ABSENT
        except Exception:
            log["codexQuota"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
        try:
            st = publish_codex_only()
        except Exception:
            log["calib"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
        steps = ()
    elif switch:
        # Fast path on account switch: poll the newly active account FIRST and publish state.json immediately
        # so CodexBar updates in seconds, then run log scanning / report afterward.
        try:
            log["poll"] = [f"{r['acc']}:{r['status']}" for r in poll.poll(
                con, force=True, min_gap=45, force_active=True, active_only=True,
                expected_active_uuid=expected_uuid)]
        except Exception:
            log["poll"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
        try:
            st = publish_state()
        except Exception:
            log["calib"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
        steps = (
            ("scan", lambda: scan.scan(con)),
            ("attributed", lambda: scan.attribute(con, since=t0 - 3 * 86400)),
            ("history", lambda: history_import.run(con) if history_import else "absent"),
            ("antigravityQuota", lambda: antigravity.import_quota(con) if antigravity else ANTIGRAVITY_ABSENT),
            ("codexQuota", lambda: codex.import_quota(con) if codex else CODEX_ABSENT),
        )
    else:
        steps = (
            ("scan", lambda: scan.scan(con)),
            ("attributed", lambda: scan.attribute(con, since=t0 - 3 * 86400)),
            ("history", lambda: history_import.run(con) if history_import else "absent"),
            ("antigravityQuota", lambda: antigravity.import_quota(con) if antigravity else ANTIGRAVITY_ABSENT),
            ("codexQuota", lambda: codex.import_quota(con) if codex else CODEX_ABSENT),
            ("poll", lambda: [f"{r['acc']}:{r['status']}" for r in poll.poll(
                con, force=force, min_gap=45 if refresh else 0, force_active=False)]),
        )
    for name, fn in steps:
        try:
            log[name] = fn()
        except Exception:
            log[name] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
    if not codex_only:
        try:
            st = publish_state()
        except Exception:
            log["calib"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
    if st is not None:
        try:
            import report
            report.write(st)
        except Exception:
            log["report"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
    log["sec"] = round(time.time() - t0, 1)
    core.kv_set(con, "last_tick", log)
    con.commit()
    with open(f"{core.DATA}/tick.log", "a") as fh:
        fh.write(json.dumps(log, ensure_ascii=False) + "\n")
    con.close()
    if "calib" in log:
        sys.exit(2)


if __name__ == "__main__":
    main()
