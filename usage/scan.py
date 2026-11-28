#!/usr/bin/env python3
"""Reads new lines of the Claude Code logs into the database and attributes every model response to an account.

The logs are only read. Re-running is cheap: for each file we remember
up to which byte it has been read.
"""
import bisect
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

INCOMPLETE_KEY = "claude_scan_incomplete_at"
STAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')


def malformed_span(raw, born, now):
    match = STAMP.search(raw)
    if match:
        try:
            ts = core.parse_iso(match[1].decode("ascii"))
            return ts, ts
        except (ValueError, OverflowError, OSError):
            pass
    # A truncated line may have lost its timestamp. Its file lifetime
    # bounds where this pass could have missed activity.
    return min(int(born), now), now + 1


def record_incomplete(con, holes, now):
    recent = sorted({tuple(span) for span in (*core.kv_get(con, INCOMPLETE_KEY, []), *holes)})
    merged = []
    for start, end in recent:
        if end <= now - 16 * 86400:  # eight days of timeline plus one quota window and grace
            continue
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    core.kv_set(con, INCOMPLETE_KEY, merged)

UPSERT = """
INSERT INTO rows(key,ts,model,inp,out,cw5,cw1h,cr,fast,cost,sid,parent,file_id,entry,sub)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, model=excluded.model, inp=excluded.inp,
  out=excluded.out, cw5=excluded.cw5, cw1h=excluded.cw1h, cr=excluded.cr, fast=excluded.fast,
  cost=excluded.cost, entry=excluded.entry
WHERE rows.file_id = excluded.file_id
"""
# Streamed chunks of one response in the same file: the latest entry replaces the earlier one.
# A copy of the same response in another file (a branch or a continued session) is not counted twice.


def list_files():
    out = []
    for root, _dirs, names in os.walk(core.PROJECTS):
        for n in names:
            if n.endswith(".jsonl"):
                out.append(os.path.join(root, n))
    return out


def parent_session(path):
    rel = os.path.relpath(path, core.PROJECTS).split(os.sep)
    if len(rel) < 2:
        return None
    p = rel[1]
    return p[:-6] if p.endswith(".jsonl") else p


def scan(con, verbose=False):
    now = int(time.time())
    known = {p: (i, off, size) for i, p, off, size in con.execute("SELECT id,path,offset,size FROM files")}
    todo = []
    unreadable = malformed = 0
    for p in list_files():
        try:
            st = os.stat(p)
        except OSError:
            unreadable += 1
            continue
        k = known.get(p)
        if k and k[1] == st.st_size:
            continue
        todo.append((getattr(st, "st_birthtime", st.st_mtime), p, st.st_size))
    todo.sort()  # oldest files first: the original response comes before its copies
    n_rows = n_hits = n_bytes = 0
    for born, p, size in todo:
        k = known.get(p)
        if k:
            fid, off = k[0], k[1]
            if size < off:  # the file was rewritten: read it again from the start
                off = 0
        else:
            fid = con.execute("INSERT INTO files(path,born) VALUES(?,?)", (p, born)).lastrowid
            off = 0
        is_sub = int(os.sep + "subagents" + os.sep in p or len(os.path.relpath(p, core.PROJECTS).split(os.sep)) > 2)
        parent = parent_session(p)
        batch, hits, holes = [], [], []
        try:
            fh = open(p, "rb")
        except OSError:
            unreadable += 1
            continue
        fh.seek(off)
        pos = off
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # the line is still being written
            pos += len(raw)
            if b'"assistant"' not in raw:
                continue
            has_usage = b'"usage"' in raw
            has_quota = b'"quotaLimits"' in raw
            if not (has_usage or has_quota):
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                malformed += 1
                holes.append(malformed_span(raw, born, now))
                continue
            if r.get("type") != "assistant":
                continue
            ts = r.get("timestamp")
            if not ts:
                continue
            try:
                t = core.parse_iso(ts)
            except Exception:
                continue
            sid = r.get("sessionId") or parent
            entry = r.get("entrypoint") or "?"
            q = r.get("quotaLimits")
            if isinstance(q, dict) and q.get("status") == "rejected":
                hits.append((f"{sid}:{r.get('uuid')}", t, q.get("rateLimitType"), q.get("resetsAt"),
                             sid, parent, entry, json.dumps(q, ensure_ascii=False)))
            msg = r.get("message") or {}
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            model = msg.get("model") or ""
            if not model or model.startswith("<synthetic"):
                continue
            cc = u.get("cache_creation") or {}
            cw = u.get("cache_creation_input_tokens") or 0
            cw1h = min(cc.get("ephemeral_1h_input_tokens") or 0, cw)
            cw5 = cw - cw1h
            inp = u.get("input_tokens") or 0
            out = u.get("output_tokens") or 0
            cr = u.get("cache_read_input_tokens") or 0
            fast = int(u.get("speed") == "fast")
            key = f"{msg.get('id')}|{r.get('requestId')}" if msg.get("id") else f"{sid}:{r.get('uuid')}"
            batch.append((key, t, model, inp, out, cw5, cw1h, cr, fast,
                          core.cost(model, inp, out, cw5, cw1h, cr, fast), sid, parent, fid, entry, is_sub))
        fh.close()
        if batch:
            con.executemany(UPSERT, batch)
        if hits:
            con.executemany(
                "INSERT OR IGNORE INTO hits(key,ts,kind,resets_at,sid,parent,entry,raw) VALUES(?,?,?,?,?,?,?,?)", hits)
        if holes:
            record_incomplete(con, holes, now)
        con.execute("UPDATE files SET offset=?, size=? WHERE id=?", (pos, pos, fid))
        n_rows += len(batch)
        n_hits += len(hits)
        n_bytes += pos - off
        con.commit()
    return {"files": len(todo), "rows": n_rows, "hits": n_hits, "mb": round(n_bytes / 1e6, 1),
            "unreadableFiles": unreadable, "malformedUsageLines": malformed}


def attribute(con, since=None):
    """Sets the account: from the session metadata first, otherwise from the switcher log."""
    sess = core.session_accounts()
    tl = core.switch_timeline()
    tl_ts = [t for t, _ in tl]
    cli_tl = core.cli_switch_timeline()
    cli_tl_ts = [t for t, _ in cli_tl]

    def by_time(t):
        i = bisect.bisect_right(tl_ts, t) - 1
        return tl[i][1] if i >= 0 else None

    def cli_by_time(t):
        i = bisect.bisect_right(cli_tl_ts, t) - 1
        return cli_tl[i][1] if i >= 0 else None

    changed = 0
    for table in ("rows", "hits"):
        where = "WHERE (how IS NULL OR how != 'meta')" + (f" AND ts >= {int(since)}" if since else "")
        upd = []
        for key, ts, sid, parent, entry in con.execute(f"SELECT key,ts,sid,parent,entry FROM {table} {where}"):
            a = sess.get(sid) or sess.get(parent)
            if a:
                upd.append((a, "meta", key))
            elif entry == "claude-desktop":
                a = by_time(ts)
                upd.append((a or "unknown", "time" if a else "none", key))
            else:
                a = cli_by_time(ts)
                if a:
                    upd.append((a, "cli-time", key))
                else:
                    upd.append((f"cli:{entry}", "other", key))
        con.executemany(f"UPDATE {table} SET acc=?, how=? WHERE key=? AND (acc IS NOT ? OR how IS NOT ?)",
                        [(a, h, k, a, h) for a, h, k in upd])
        changed += len(upd)
    con.commit()
    return changed


def main():
    con = core.db()
    t0 = time.time()
    res = scan(con)
    res["attributed"] = attribute(con)
    res["sec"] = round(time.time() - t0, 1)
    core.kv_set(con, "last_scan", {"ts": int(time.time()), **res})
    con.commit()
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
