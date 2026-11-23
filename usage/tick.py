#!/usr/bin/env python3
"""One tracking pass: read new log lines -> query limits -> recompute -> write the summary.

Run by a LaunchAgent every 5 minutes. A lock prevents parallel runs.

  tick.py            regular scheduled pass
  tick.py --force    query the limits of all accounts right now
  tick.py --refresh  the same, but no more than once per 45 seconds per account: this is how CodexBar
                     refreshes limits when its menu opens. The VPN rule applies as always.
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
try:
    import antigravity  # noqa: E402  (Gemini through Antigravity: the state["antigravity"] block, beta)
    ANTIGRAVITY_ABSENT = None
except Exception:
    antigravity = None
    ANTIGRAVITY_ABSENT = "error: " + traceback.format_exc(limit=1).strip().splitlines()[-1]


def main():
    os.makedirs(core.DATA, exist_ok=True)
    lock = open(f"{core.DATA}/tick.lock", "w")
    refresh = "--refresh" in sys.argv
    # A scheduled pass may hold the lock for half a minute. A refresh asked for by the open menu waits for it
    # instead of being dropped; a scheduled pass that meets another one just leaves.
    deadline = time.time() + (30 if refresh else 0)
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                with open(f"{core.DATA}/tick.log", "a") as fh:
                    fh.write(json.dumps({"ts": core.local(int(time.time()), "%Y-%m-%d %H:%M:%S"),
                                         "skipped": "locked", **({"mode": "refresh"} if refresh else {})}) + "\n")
                return
            time.sleep(0.2)
    con = core.db()
    t0 = time.time()
    force = refresh or "--force" in sys.argv
    log = {"ts": core.local(int(t0), "%Y-%m-%d %H:%M:%S")}
    if refresh:
        log["mode"] = "refresh"
    for name, fn in (
        ("scan", lambda: scan.scan(con)),
        ("attributed", lambda: scan.attribute(con, since=t0 - 3 * 86400)),
        ("history", lambda: history_import.run(con) if history_import else "absent"),
        ("antigravityQuota", lambda: antigravity.import_quota(con) if antigravity else ANTIGRAVITY_ABSENT),
        ("poll", lambda: [f"{r['acc']}:{r['status']}" for r in poll.poll(con, force=force, min_gap=45 if refresh else 0)]),
    ):
        try:
            log[name] = fn()
        except Exception:
            log[name] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
    try:
        st = calib.analyse(con)
        if antigravity:
            # never raises: a failure becomes {"error": ...} inside the block and the Claude part is written as always
            st["antigravity"], log["antigravity"] = antigravity.block(con, st["generatedAt"])
        tmp = f"{core.DATA}/state.json.tmp"
        json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, f"{core.DATA}/state.json")
    except Exception:
        log["calib"] = "error: " + traceback.format_exc(limit=2).strip().splitlines()[-1]
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


if __name__ == "__main__":
    main()
