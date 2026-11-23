#!/usr/bin/env python3
"""BETA: Gemini tokens and API-price dollars spent through Google Antigravity on this Mac.

Run:  python3 usage/antigravity_beta.py            (last 30 days per day and model, totals)
      python3 usage/antigravity_beta.py --all      (whole local history)
      python3 usage/antigravity_beta.py --json     (machine-readable, for the collector)

Where the numbers come from
---------------------------
Antigravity (app and `agy` CLI) keeps every conversation in its own SQLite file:
  ~/.gemini/antigravity/conversations/<conversation>.db
  ~/.gemini/antigravity-cli/conversations/<conversation>.db
Table `gen_metadata(idx, data, size)` has one row per model call ("generation"); `data` is a
protobuf blob without a published schema. The layout below was decoded locally and matches what
CodexBar (CodexBar/Sources/CodexBarCore/Providers/Antigravity/AntigravityProtoReader.swift),
CodeBurn and agent-walker read:

  root #1        chat-model message
    #1.#4        usage (Codeium-style ModelUsageStats)
      #1  model enum number (not a token count)
      #2  input tokens, NOT cached          -> "input"
      #3  output total = #9 + #10           -> "output" (thinking included, billed as output)
      #4  cache-write tokens (never seen on this Mac; priced as input if it appears)
      #5  cache-read tokens                 -> "cached"
      #6  API provider number
      #9  thinking tokens
      #10 visible response tokens
      #11 response id                       -> de-duplication key
    #1.#9.#4     request timestamp (only older builds write it)
    #1.#19       model id, e.g. "gemini-4-neon"
    #1.#20       key/value pairs: cascade_id, request_id, model_enum, ...
    #1.#21       display name, e.g. "Gemini 4 Neon (High)"
  root #2        packed list of `steps.idx` rows produced by this call
  root #4        step/execution uuid (= steps.metadata #12)
Table `steps(idx, metadata, ...)`: metadata #1 is the step's created-at timestamp.

What is exact and what is an estimate
-------------------------------------
* Exact (as recorded by Antigravity per call): input, cached input, output and thinking tokens,
  number of calls, model id. Field meanings are reverse-engineered, so every row is checked:
  #3 must equal #9 + #10 (rows that fail are still counted with #3 and reported).
* Time: newer builds do not store a time on the call itself. We take the earliest created-at of the
  steps listed in root #2 (on rows that also carry their own time the two agree within 0.2 s), then
  the step uuid, then the conversation's start, then the file's modification time. Anything but the
  first two is reported as "time estimated".
* Dollars are an ESTIMATE of what the same tokens would cost on the Gemini API at list price
  (Standard tier, prices below). Antigravity itself does not bill per token: Google AI Pro / Ultra
  give a quota that refreshes every 5 hours with a weekly cap. Prices change on given dates
  (Gemini 4 Carbon/Argon on 2026-12-01, Flash on 2027-01-01); each call is priced with the price in
  force on its local date according to today's published table.
* Copies: a conversation that continues or forks another one carries copies of the parent's
  generation rows (same response id, same tokens). They are counted once; the number dropped is
  reported. CodexBar de-duplicates only inside one file, so it may count these copies again.
* Not covered: older conversations stored as encrypted `.pb` files (before the SQLite format,
  here before 2026-08-09) and conversations deleted in the app. Reading them would need the running
  language server (RPC GetCascadeTrajectoryGeneratorMetadata), which this script does not call.

The script only reads. SQLite files are opened with `mode=ro&immutable=1`; a file that is being
written (non-empty -wal next to it) is first copied with its -wal into a temporary folder and read
there, so nothing is ever written into ~/.gemini. Python 3 standard library only.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

# ---------------------------------------------------------------------------------------------
# Prices, $ per 1M tokens: (effective from local date, input, output incl. thinking, cache read).
# Source: https://ai.google.dev/gemini-api/docs/pricing (now redirects to
# https://aistudio.google.com/docs/pricing), Standard tier, checked 2026-11-23.
# Gemini 4 models: "implicit caching only", no >200k prompt tier.
# UNCONFIRMED (not on the official page on 2026-11-23, taken from the models.dev catalog that
# CodexBar cached on 2026-11-22, https://models.dev/api.json): gemini-3.7-flash, gemini-3.5-flash.
PRICES = {
    "gemini-4-neon": [("", 3.00, 15.00, 0.15)],
    "gemini-4-carbon": [("", 2.00, 10.00, 0.10), ("2026-12-01", 4.00, 20.00, 0.20)],
    "gemini-4-argon": [("", 2.00, 10.00, 0.10), ("2026-12-01", 4.00, 20.00, 0.20)],
    "gemini-4-hydrogen": [("", 1.50, 7.50, 0.075)],
    "gemini-3.8-flash": [("", 0.75, 3.75, 0.075), ("2027-01-01", 1.50, 7.50, 0.15)],
    "gemini-3.7-flash": [("", 0.75, 3.75, 0.075), ("2027-01-01", 1.50, 7.50, 0.15)],  # unconfirmed
    "gemini-3.6-flash": [("", 0.75, 3.75, 0.075), ("2027-01-01", 1.50, 7.50, 0.15)],
    "gemini-3.5-flash": [("", 1.50, 9.00, 0.15)],  # unconfirmed
    "gemini-3.5-flash-lite": [("", 0.30, 2.50, 0.03)],
    "gemini-3-flash": [("", 0.50, 3.00, 0.05)],
    "gemini-3.1-pro": [("", 2.00, 12.00, 0.20)],
}
# Prompts (input + cached + cache write) above the threshold are billed at the long-context price.
LONG_CONTEXT = {"gemini-3.1-pro": (200_000, (4.00, 18.00, 0.40))}
UNCONFIRMED = {"gemini-3.7-flash", "gemini-3.5-flash"}
PRICES_CHECKED = "2026-11-23"
PRICE_SOURCE = "https://aistudio.google.com/docs/pricing"

# Product aliases recorded by Antigravity -> priced model.
ALIASES = {
    "gemini-pro-default": "gemini-3.1-pro",
    "gemini-pro-agent": "gemini-3.1-pro",
    "gemini-3.1-pro-preview": "gemini-3.1-pro",
    "gemini-3-flash-preview": "gemini-3-flash",
    "gemini-3.7-flash-safety-le": "gemini-3.7-flash",
}
# Routing / effort suffixes that do not change the list price ("-tiered-high", "-exp-a", ...).
VARIANT_SUFFIX = re.compile(r"-(tiered|x?high|medium|low|minimal|thinking|preview|customtools|exp(-[a-z0-9]+)?)$")
# Last resort when a row has neither a model id nor a display name. Seen on this Mac
# (model id <-> model_enum on the same rows); M16/M36/M37/M84/M18 from CodeBurn's table.
ENUM_MODELS = {
    "MODEL_PLACEHOLDER_M532": "gemini-4-neon", "MODEL_PLACEHOLDER_M533": "gemini-4-neon",
    "MODEL_PLACEHOLDER_M427": "gemini-4-carbon", "MODEL_PLACEHOLDER_M530": "gemini-4-hydrogen",
    "MODEL_PLACEHOLDER_M298": "gemini-3.7-flash", "MODEL_PLACEHOLDER_M71": "gemini-3.6-flash",
    "MODEL_PLACEHOLDER_M16": "gemini-3.1-pro", "MODEL_PLACEHOLDER_M36": "gemini-3.1-pro",
    "MODEL_PLACEHOLDER_M37": "gemini-3.1-pro", "MODEL_PLACEHOLDER_M84": "gemini-3.5-flash",
    "MODEL_PLACEHOLDER_M18": "gemini-3-flash",
}
DB_DIRS = ("antigravity/conversations", "antigravity-cli/conversations", "antigravity-ide/conversations")


# ------------------------------------------------------------------------------- protobuf ----
def _varint(b, i):
    r = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise ValueError("bad varint")
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def pb_fields(b):
    """[(field, wire, value)]: value is int for varints, bytes otherwise. Raises on malformed."""
    out, i = [], 0
    while i < len(b):
        tag, i = _varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError("wire type")
        if i > len(b):
            raise ValueError("truncated")
        out.append((fn, wt, v))
    return out


def pb_timestamp(b):
    secs = nanos = 0
    for fn, wt, v in pb_fields(b):
        if wt == 0 and fn == 1:
            secs = v
        elif wt == 0 and fn == 2:
            nanos = v
    return secs + nanos / 1e9 if 0 < secs < 253_402_300_799 else None


def pb_packed(wt, v):
    if wt == 0:
        return [v]
    out, i = [], 0
    while i < len(v):
        x, i = _varint(v, i)
        out.append(x)
    return out


def pb_text(v):
    try:
        s = v.decode("utf-8").strip()
    except Exception:
        return None
    return s or None


# --------------------------------------------------------------------------------- models ----
def price_key(model_id, label, enum):
    """Recorded model -> key of PRICES (or None)."""
    cands = []
    if model_id:
        cands.append(model_id.lower())
    if label:  # "Gemini 4 Neon (High)" -> "gemini-4-neon"
        cands.append(re.sub(r"\s+", "-", re.sub(r"\(.*?\)", "", label).strip().lower()))
    for m in cands:
        for _ in range(4):
            m = ALIASES.get(m, m)
            if m in PRICES:
                return m
            stripped = VARIANT_SUFFIX.sub("", m)
            if stripped == m:
                break
            m = stripped
    return ENUM_MODELS.get(enum or "")


def model_name(model_id, label, enum):
    if model_id:
        return model_id
    if label:
        return re.sub(r"\s+", "-", re.sub(r"\(.*?\)", "", label).strip().lower())
    return ENUM_MODELS.get(enum or "", enum or "unknown")


def effort(label):
    m = re.search(r"\(([^)]+)\)", label or "")
    return m.group(1) if m else None


def prices_on(key, day):
    """(input, output, cache_read) in force on local date `day` (YYYY-MM-DD)."""
    cur = None
    for since, *p in PRICES[key]:
        if since <= day:
            cur = p
    return cur or PRICES[key][0][1:]


def cost_usd(key, day, inp, cr, cw, out):
    if key is None:
        return None
    pi, po, pr = prices_on(key, day)
    lc = LONG_CONTEXT.get(key)
    if lc and inp + cr + cw > lc[0]:
        pi, po, pr = lc[1]
    return (inp * pi + cw * pi + cr * pr + out * po) / 1e6


# ------------------------------------------------------------------------------- reading -----
def open_ro(path, tmpdir):
    """Read-only connection. A database with a live -wal is copied (with the -wal) first."""
    wal = path + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        dst = os.path.join(tmpdir, os.path.basename(path))
        shutil.copyfile(path, dst)
        shutil.copyfile(wal, dst + "-wal")
        return sqlite3.connect(f"file:{dst}?mode=ro", uri=True), True
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True), False


def read_db(path, tmpdir, stats):
    """Yields one dict per generation row that carries usage."""
    con, copied = open_ro(path, tmpdir)
    stats["dbCopiedBecauseLive"] += int(copied)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gen_metadata" not in tables:
            stats["dbWithoutGenMetadata"] += 1
            return
        conv = os.path.basename(path)[:-3]
        step_ts, uuid_ts = {}, {}
        if "steps" in tables:
            for idx, md in con.execute("SELECT idx, metadata FROM steps"):
                if not md:
                    continue
                try:
                    t = u = None
                    for fn, wt, v in pb_fields(md):
                        if fn == 1 and wt == 2:
                            t = pb_timestamp(v)
                        elif fn == 12 and wt == 2:
                            u = pb_text(v)
                except ValueError:
                    continue
                if t:
                    step_ts[idx] = t
                    if u:
                        uuid_ts[u] = min(t, uuid_ts.get(u, t))
        conv_ts = None
        if "trajectory_metadata_blob" in tables:
            for (blob,) in con.execute("SELECT data FROM trajectory_metadata_blob"):
                try:
                    conv_ts = next((pb_timestamp(v) for fn, wt, v in pb_fields(blob or b"") if fn == 2 and wt == 2), None)
                except ValueError:
                    pass
        mtime = os.path.getmtime(path)
        for idx, data in con.execute("SELECT idx, data FROM gen_metadata"):
            stats["rows"] += 1
            try:
                ev = parse_generation(data or b"")
            except ValueError:
                stats["rowsMalformed"] += 1
                continue
            if ev is None:
                stats["rowsWithoutUsage"] += 1
                continue
            ts, src = ev.pop("ts"), "row"
            if ts is None:
                ts = min((step_ts[s] for s in ev["steps"] if s in step_ts), default=None)
                src = "steps"
            if ts is None and ev["uuid"] in uuid_ts:
                ts, src = uuid_ts[ev["uuid"]], "step-uuid"
            if ts is None and conv_ts:
                ts, src = conv_ts, "conversation-start"
            if ts is None:
                ts, src = mtime, "file-mtime"
            ev.update(ts=ts, timeSource=src, conv=conv, idx=idx)
            del ev["steps"], ev["uuid"]
            yield ev
    finally:
        con.close()


def parse_generation(data):
    chat, steps, uuid = None, [], None
    for fn, wt, v in pb_fields(data):
        if fn == 1 and wt == 2:
            chat = v
        elif fn == 2:
            steps += pb_packed(wt, v)
        elif fn == 4 and wt == 2:
            uuid = pb_text(v)
    if chat is None:
        return None
    usage, model_id, label, ts, kv = None, None, None, None, {}
    for fn, wt, v in pb_fields(chat):
        if wt != 2:
            continue
        if fn == 4:
            usage = {f: x for f, w, x in pb_fields(v) if w == 0}
            rid = [pb_text(x) for f, w, x in pb_fields(v) if f == 11 and w == 2]
            usage["rid"] = rid[0] if rid else None
        elif fn == 9:
            for f, w, x in pb_fields(v):
                if f == 4 and w == 2:
                    ts = pb_timestamp(x)
        elif fn == 19:
            model_id = pb_text(v)
        elif fn == 20:
            d = {f: x for f, w, x in pb_fields(v) if w == 2}
            k = pb_text(d.get(1, b""))
            if k:
                kv[k] = pb_text(d.get(2, b""))
        elif fn == 21:
            label = pb_text(v)
    if not usage or not any(usage.get(f) for f in (2, 3, 4, 5, 9, 10)):
        return None
    think, resp = usage.get(9, 0), usage.get(10, 0)
    out = usage.get(3, think + resp)
    enum = kv.get("model_enum")
    return {
        "rid": usage["rid"], "reqId": kv.get("request_id"), "cascade": kv.get("cascade_id"),
        "model": model_name(model_id, label, enum), "priceAs": price_key(model_id, label, enum),
        "effort": effort(label), "input": usage.get(2, 0), "cacheRead": usage.get(5, 0),
        "cacheWrite": usage.get(4, 0), "output": out, "thinking": think,
        "outputCheckFailed": 3 in usage and usage[3] != think + resp,
        "ts": ts, "steps": steps, "uuid": uuid,
    }


FILE_STATS = ("dbCopiedBecauseLive", "dbWithoutGenMetadata", "dbUnreadable", "rows", "rowsMalformed",
              "rowsWithoutUsage")


def conversation_files(gemini_home):
    """(database paths, names of encrypted .pb conversations). The same old conversation sits in more
    than one folder (app and IDE), so .pb files are counted once, by file name."""
    paths = sorted(p for d in DB_DIRS for p in glob.glob(os.path.join(gemini_home, d, "*.db"))
                   if os.path.getsize(p) > 0)
    pb = sorted({os.path.basename(p) for d in DB_DIRS for p in glob.glob(os.path.join(gemini_home, d, "*.pb"))})
    return paths, pb


def read_file(path, tmp):
    """All usage rows of one conversation database: (events, {FILE_STATS counter: n}). Never raises
    on a broken database: it is counted as unreadable."""
    stats = {k: 0 for k in FILE_STATS}
    events = []
    try:
        for ev in read_db(path, tmp, stats):
            events.append(ev)
    except (sqlite3.Error, OSError):
        stats["dbUnreadable"] += 1
    return events, stats


def dedupe(events):
    """Drops copies of the same call (forked or continued conversations): (unique events, copies dropped)."""
    best, dropped = {}, 0
    for ev in events:
        key = (("r", ev["rid"]) if ev["rid"] else ("q", ev["reqId"]) if ev["reqId"]
               else ("f", ev["conv"], ev["idx"]))
        # prefer the copy in the conversation the call belongs to, then the best time evidence
        score = (ev["cascade"] == ev["conv"], ev["timeSource"] in ("row", "steps"))
        old = best.get(key)
        if old is None:
            best[key] = (score, ev)
        else:
            dropped += 1
            if score > old[0]:
                best[key] = (score, ev)
    return [ev for _, ev in best.values()], dropped


def collect(gemini_home):
    stats = {k: 0 for k in ("dbFiles", *FILE_STATS, "copiesDropped", "outputCheckFailed")}
    paths, pb = conversation_files(gemini_home)
    raw = []
    with tempfile.TemporaryDirectory(prefix="agy-beta-") as tmp:
        for p in paths:
            stats["dbFiles"] += 1
            evs, st = read_file(p, tmp)
            raw += evs
            for k, v in st.items():
                stats[k] += v
    events, stats["copiesDropped"] = dedupe(raw)
    stats["outputCheckFailed"] = sum(ev["outputCheckFailed"] for ev in events)
    stats["encryptedPbFiles"] = len(pb)
    return events, stats


# ----------------------------------------------------------------------------- aggregation ----
def blank():
    return {"requests": 0, "input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0, "thinking": 0,
            "usd": 0.0, "unpricedRequests": 0, "timeEstimated": 0}


def add(acc, ev):
    acc["requests"] += 1
    for k in ("input", "cacheRead", "cacheWrite", "output", "thinking"):
        acc[k] += ev[k]
    if ev["usd"] is None:
        acc["unpricedRequests"] += 1
    else:
        acc["usd"] += ev["usd"]
    acc["timeEstimated"] += ev["timeSource"] not in ("row", "steps")


def summarize(events, stats, now, days_shown):
    for ev in events:
        ev["day"] = datetime.fromtimestamp(ev["ts"]).strftime("%Y-%m-%d")
        ev["usd"] = cost_usd(ev["priceAs"], ev["day"], ev["input"], ev["cacheRead"], ev["cacheWrite"], ev["output"])
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    windows = {"today": today0, "7d": now - 7 * 86400, "30d": now - 30 * 86400, "all": 0}
    totals = {w: {"all": blank(), "models": {}} for w in windows}
    daily = {}
    first_day = datetime.fromtimestamp(now - (days_shown - 1) * 86400).strftime("%Y-%m-%d") if days_shown else ""
    for ev in sorted(events, key=lambda e: e["ts"]):
        for w, since in windows.items():
            if ev["ts"] >= since:
                add(totals[w]["all"], ev)
                add(totals[w]["models"].setdefault(ev["model"], blank()), ev)
        if ev["day"] >= first_day:
            d = daily.setdefault(ev["day"], {"all": blank(), "models": {}})
            add(d["all"], ev)
            add(d["models"].setdefault(ev["model"], blank()), ev)
    models = {}
    for ev in events:
        m = models.setdefault(ev["model"], {"priceAs": ev["priceAs"], "efforts": {}})
        if ev["effort"]:
            m["efforts"][ev["effort"]] = m["efforts"].get(ev["effort"], 0) + 1
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    for m in models.values():
        k = m["priceAs"]
        m["pricePerMTok"] = None if k is None else dict(zip(("input", "output", "cacheRead"), prices_on(k, today)))
        m["priceChanges"] = [] if k is None else [
            {"from": since, "input": p[0], "output": p[1], "cacheRead": p[2]} for since, *p in PRICES[k] if since > today]
        m["priceConfirmed"] = k is not None and k not in UNCONFIRMED
    stats["timeSources"] = {}
    for ev in events:
        stats["timeSources"][ev["timeSource"]] = stats["timeSources"].get(ev["timeSource"], 0) + 1
    span = [min(e["day"] for e in events), max(e["day"] for e in events)] if events else None
    for d in [*daily.values(), *totals.values()]:
        for acc in [d["all"], *d["models"].values()]:
            acc["usd"] = round(acc["usd"], 4)
    return {
        "schema": "antigravity-beta-1",
        "generatedAt": int(now),
        "tokens": "exact, as recorded by Antigravity per call (output includes thinking)",
        "usd": "estimate at Gemini API list prices, Standard tier; not what Google bills",
        "priceSource": PRICE_SOURCE, "pricesChecked": PRICES_CHECKED,
        "historySpan": span, "coverage": stats, "models": models,
        "daily": [{"day": k, **v} for k, v in sorted(daily.items())],
        "totals": totals,
    }


# --------------------------------------------------------------------------------- output ----
def tok(n):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


def miss_pct(a):
    """Share of the prompt (input + cached + cache write) that was NOT read from the cache, in percent."""
    prompt = a["input"] + a["cacheRead"] + a["cacheWrite"]
    return round(100 * (1 - a["cacheRead"] / prompt), 1) if prompt else None


def line(name, a, width):
    usd = f"${a['usd']:,.2f}" + ("*" if a["unpricedRequests"] else "")
    miss = miss_pct(a)
    return (f"{name:<{width}} {a['requests']:>6} {tok(a['input']):>8} {tok(a['cacheRead']):>8} "
            f"{'-' if miss is None else f'{miss}%':>6} "
            f"{tok(a['output']):>8} {tok(a['thinking']):>8} {usd:>11}")


def print_text(r):
    st = r["coverage"]
    w = max([len(m) for m in r["models"]] + [24]) + 2
    head = f"{'':<{w}} {'calls':>6} {'input':>8} {'cached':>8} {'miss':>6} {'output':>8} {'thinking':>8} {'$ (estim.)':>11}"
    print("Antigravity / Gemini usage on this Mac  [BETA]")
    print(f"Tokens: exact, from {st['dbFiles']} conversation databases ({st['copiesDropped']} copied rows "
          f"counted once). Output includes thinking; the thinking column is its part of output. "
          f"miss = share of the prompt not read from the cache (see cache_compare_beta.py).")
    print(f"Dollars: ESTIMATE at Gemini API list prices, Standard tier ({PRICE_SOURCE}, checked "
          f"{PRICES_CHECKED}). Antigravity bills a subscription quota, not tokens.")
    if r["historySpan"]:
        print(f"History: {r['historySpan'][0]} .. {r['historySpan'][1]}" +
              (f"; {st['encryptedPbFiles']} older conversations are encrypted .pb files and are not counted"
               if st["encryptedPbFiles"] else ""))
    print()
    print(head)
    for d in r["daily"]:
        print(line(d["day"], d["all"], w))
        for m, a in sorted(d["models"].items(), key=lambda x: -x[1]["usd"]):
            print(line("  " + m, a, w))
    print()
    for key, title in (("today", "Today"), ("7d", "Last 7 days"), ("30d", "Last 30 days"), ("all", "All local history")):
        t = r["totals"][key]
        print(line(title, t["all"], w))
        for m, a in sorted(t["models"].items(), key=lambda x: -x[1]["usd"]):
            print(line("  " + m, a, w))
    print()
    print("Prices used ($ per 1M tokens: input / output incl. thinking / cached input):")
    for m, info in sorted(r["models"].items()):
        p = info["pricePerMTok"]
        eff = ", ".join(f"{k} {v}" for k, v in sorted(info["efforts"].items()))
        price = ("UNPRICED" if p is None else
                 f"{info['priceAs']}: {p['input']:g} / {p['output']:g} / {p['cacheRead']:g}"
                 + "".join(f"; from {c['from']}: {c['input']:g} / {c['output']:g} / {c['cacheRead']:g}"
                           for c in info["priceChanges"])
                 + ("" if info["priceConfirmed"] else "  (unconfirmed)"))
        print(f"  {m:<{w - 2}} {price}" + (f"   [{eff}]" if eff else ""))
    notes = []
    if any(r["totals"]["all"]["models"][m]["unpricedRequests"] for m in r["totals"]["all"]["models"]):
        notes.append("* some calls use a model without a price; their dollars are missing")
    est = sum(v for k, v in st["timeSources"].items() if k not in ("row", "steps"))
    if est:
        notes.append(f"{est} calls have an estimated time (conversation start or file date)")
    if st["outputCheckFailed"]:
        notes.append(f"{st['outputCheckFailed']} calls failed the output = thinking + response check")
    if st["dbUnreadable"] or st["rowsMalformed"]:
        notes.append(f"unreadable: {st['dbUnreadable']} databases, {st['rowsMalformed']} rows")
    for n in notes:
        print("Note: " + n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--days", type=int, default=30, help="days to list one by one (default 30)")
    ap.add_argument("--all", action="store_true", help="list every day of the local history")
    ap.add_argument("--gemini-home", default=os.environ.get("GEMINI_CLI_HOME") or os.path.expanduser("~/.gemini"),
                    help="Antigravity data folder (default ~/.gemini)")
    a = ap.parse_args()
    events, stats = collect(a.gemini_home)
    now = time.time()
    r = summarize(events, stats, now, 0 if a.all else max(1, a.days))  # 0 = every day
    if a.json:
        json.dump(r, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print_text(r)


if __name__ == "__main__":
    main()
