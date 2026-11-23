#!/usr/bin/env python3
"""BETA: how much of the prompt misses the cache, for Gemini (Antigravity), Claude Code and Codex.

Run:  python3 usage/cache_compare_beta.py            (last 7 and 30 days)
      python3 usage/cache_compare_beta.py --json

What "cache miss" means here
----------------------------
Every model call sends the whole conversation so far as its prompt. The provider keeps recent
prompts and, when the start of a new prompt is the same as a stored one, reads that part from the
cache at a fraction of the price. The rest of the prompt "misses": it is processed at full price.

  prompt tokens  = cache read + not cached (+ written to the cache, Claude only)
  miss rate      = 1 - cache read / prompt tokens          (by tokens, the number that costs money)
  cold calls     = calls where nothing at all was read from the cache
  big-miss calls = calls where more than half of a prompt of 20k+ tokens missed
  input price    = what the prompt cost compared with the same prompt with no cache at all

Some miss is unavoidable: the new message and the previous answer are never in the cache yet. A
high miss rate beyond that means the cache expired between calls (a pause longer than the cache
lifetime), the start of the prompt changed, or the provider did not find a match.

The three providers differ in ways that matter when comparing:
  * Claude charges for writing to the cache (1.25x of input for 5 minutes, 2x for 1 hour) and 0.1x
    for reading. A token written to the cache is a miss on this call and a hit on the next ones.
  * Gemini 4 (Antigravity) caches implicitly: no charge for writing, reading costs 0.05x of input
    (Neon: $0.15 against $3.00), and Google gives no guarantee that a repeated prefix is found.
  * Codex (OpenAI) caches implicitly too: no charge for writing, reading costs 0.1x of input.

Sources, all local and read-only:
  Gemini  ~/.gemini/antigravity*/conversations/*.db   (through antigravity_beta.py)
  Claude  the collector's database (usage.db, table rows), filled from ~/.claude/projects
  Codex   ~/.codex/sessions/**/*.jsonl, `token_count` events (`last_token_usage`)
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BIG_PROMPT = 20_000
# Price of a cached / cache-written token as a multiple of the plain input price.
READ_X = {"gemini": 0.05, "claude": 0.10, "codex": 0.10}
CLAUDE_WRITE_5M_X, CLAUDE_WRITE_1H_X = 1.25, 2.0


def blank():
    return {"calls": 0, "prompt": 0, "read": 0, "write": 0, "plain": 0, "cold": 0, "bigMiss": 0,
            "bigCalls": 0, "weighted": 0.0}


def add(acc, read, plain, write=0, write_weight=1.0, read_x=0.1):
    prompt = read + plain + write
    if prompt <= 0:
        return
    acc["calls"] += 1
    acc["prompt"] += prompt
    acc["read"] += read
    acc["write"] += write
    acc["plain"] += plain
    acc["cold"] += read == 0
    if prompt >= BIG_PROMPT:
        acc["bigCalls"] += 1
        acc["bigMiss"] += read < prompt / 2
    acc["weighted"] += plain + write * write_weight + read * read_x


def gemini(since):
    import antigravity_beta as agy
    events, _ = agy.collect(os.path.expanduser("~/.gemini"))
    acc = blank()
    for ev in events:
        if ev.get("ts") and ev["ts"] >= since:
            add(acc, ev["cacheRead"], ev["input"], ev["cacheWrite"], 1.0, READ_X["gemini"])
    return acc


def claude(since):
    """All Claude Code calls on this Mac. The read / plain / written split is core.cache_split(), the same
    one behind state.json `cache`, so missPct here equals `cache.7d.missPct` / `cache.30d.missPct` there."""
    import core
    acc = blank()
    if not os.path.exists(core.DB):
        return acc
    con = sqlite3.connect(f"file:{core.DB}?mode=ro", uri=True, timeout=30)
    for inp, cw5, cw1h, cr in con.execute("SELECT inp,cw5,cw1h,cr FROM rows WHERE ts>=?", (int(since),)):
        read, plain, write = core.cache_split(inp, cw5, cw1h, cr)
        weight = ((cw5 or 0) * CLAUDE_WRITE_5M_X + (cw1h or 0) * CLAUDE_WRITE_1H_X) / write if write else 1.0
        add(acc, read, plain, write, weight, READ_X["claude"])
    con.close()
    return acc


def codex(since):
    """`input_tokens` of Codex includes the cached part (OpenAI convention)."""
    acc = blank()
    for path in glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < since:
                continue
            last_total = None
            with open(path, "rb") as fh:
                for raw in fh:
                    if b'"token_count"' not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    info = (rec.get("payload") or {}).get("info") or {}
                    total, last = info.get("total_token_usage"), info.get("last_token_usage")
                    # The same counters are sent again with every rate-limit update: one call, one change.
                    if not total or not last or total == last_total:
                        continue
                    last_total = total
                    ts = rec.get("timestamp") or ""
                    try:
                        when = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                    except ValueError:
                        when = os.path.getmtime(path)
                    if when < since:
                        continue
                    cached = last.get("cached_input_tokens") or 0
                    add(acc, cached, max(0, (last.get("input_tokens") or 0) - cached),
                        last.get("cache_write_input_tokens") or 0, 1.0, READ_X["codex"])
        except OSError:
            continue
    return acc


def rates(acc):
    p = acc["prompt"]
    if not p:
        return None
    return {
        "calls": acc["calls"], "promptTokens": p,
        "missPct": round(100 * (1 - acc["read"] / p), 1),
        "plainPct": round(100 * acc["plain"] / p, 1),
        "writePct": round(100 * acc["write"] / p, 1),
        "coldCallsPct": round(100 * acc["cold"] / acc["calls"], 1),
        "bigMissCallsPct": round(100 * acc["bigMiss"] / acc["bigCalls"], 1) if acc["bigCalls"] else None,
        "inputPricePct": round(100 * acc["weighted"] / p, 1),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    now = time.time()
    out = {}
    for days in (7, 30):
        since = now - days * 86400
        out[f"{days}d"] = {name: rates(fn(since)) for name, fn in
                           (("gemini", gemini), ("claude", claude), ("codex", codex))}
    if args.json:
        print(json.dumps(out, indent=1))
        return
    print("Cache miss by provider on this Mac  [BETA]   (tokens of the prompt that were not read from the cache)\n")
    head = f"{'':<18}{'calls':>7}{'prompt':>9}{'miss':>8}{'  = plain':>10}{'+ written':>10}{'cold':>8}{'big miss':>10}{'input price':>13}"
    for period, rows in out.items():
        print(f"Last {period[:-1]} days\n{head}")
        for name, r in rows.items():
            label = {"gemini": "Gemini/Antigravity", "claude": "Claude Code", "codex": "Codex"}[name]
            if not r:
                print(f"{label:<18}{'no calls':>7}")
                continue
            big = "-" if r["bigMissCallsPct"] is None else f"{r['bigMissCallsPct']}%"
            print(f"{label:<18}{r['calls']:>7}{r['promptTokens'] / 1e6:>8.0f}M{r['missPct']:>7}%"
                  f"{r['plainPct']:>9}%{r['writePct']:>9}%{r['coldCallsPct']:>7}%{big:>10}{r['inputPricePct']:>12}%")
        print()
    print("miss = plain + written.  cold = calls with nothing read from the cache.  big miss = calls where more\n"
          f"than half of a prompt of {BIG_PROMPT // 1000}k+ tokens missed.  input price = cost of the prompts against no cache at all.")


if __name__ == "__main__":
    main()
