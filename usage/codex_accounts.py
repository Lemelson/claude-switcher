#!/usr/bin/env python3
"""Codex accounts on this Mac and one block per account: `state["codex"]["accounts"]` (CODEX-STATE.md,
section "Accounts list").

Accounts are found in two places, both read-only:
  * the live system account: ~/.codex/auth.json (`tokens.account_id`; e-mail and plan from the id_token's
    claims, the token itself is never kept or printed);
  * CodexBar's managed accounts: managed-codex-accounts.json (version 3: id, email, providerAccountID,
    workspaceAccountID, managedHomePath), plan from the id_token claims of the managed home's auth.json.
One entry per normalized (lowercase) ChatGPT account id, else per lowercase e-mail. Order: the order of the
ids in account-names.json, then by e-mail. Display name: account-names.json[id], else the e-mail.

Per account, the same shape as the `codex` block itself (UsagePanelState.Account plus the provider extras),
built by quota_provider.build_common from:
  * the account's own calls: buckets of the session logs whose creator_account_id is this account;
  * the account's own readings, merged in an in-memory database (usage.db is only read): usage.db samples
    of this CodexBar account (current or parked), its series in CodexBar's history/codex.json, its lines in
    codex-quota.jsonl, and the latest reading of codex-account-snapshots.json (Apple reference-date
    timestamps: + APPLE_EPOCH).
Calls of logs that name no account (before 12.10.2026) belong to no account: `state["codex"]["unattributed"]`.
Never raises out of blocks(): a failed account becomes {"uuid", "name", "short", "active", "beta", "error"}.

  python3 usage/codex_accounts.py     list the accounts found (no tokens, no state.json)
"""
import base64
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402
import quota_provider as qp  # noqa: E402
from timeline import account_input, event_spend  # noqa: E402

APPLE_EPOCH = 978307200        # 2001-01-01T00:00:00Z: CFAbsoluteTime (CodexBar's JSON dates) + this = Unix time
CODEXBAR_DIR = os.path.join(core.HOME, "Library", "Application Support", "CodexBar")
MANAGED = os.path.join(CODEXBAR_DIR, "managed-codex-accounts.json")
SNAPSHOTS = os.path.join(CODEXBAR_DIR, "codex-account-snapshots.json")
MANAGED_HOMES = os.path.join(CODEXBAR_DIR, "managed-codex-homes")
KEY_PREFIX = "codex:v1:provider-account:"
# windowMinutes of a CodexBar reading -> the block's window
WINDOW_MINUTES = {300: "5h", 10080: "7d"}


def paths(codex_home=None):
    """Where the account files are (tests pass their own)."""
    import codex
    h = codex_home or codex.CODEX_HOME
    return {"auth": os.path.join(h, "auth.json"), "managed": MANAGED, "snapshots": SNAPSHOTS,
            "managedHomes": MANAGED_HOMES, "history": codex.HISTORY, "live": codex.LIVE,
            "names": core.NAMES_FILE, "planFile": codex.PLAN_FILE}


def norm(s):
    return s.strip().lower() if isinstance(s, str) and s.strip() else None


def apple_to_unix(x):
    """CodexBar's JSON dates (seconds since 2001-01-01) -> Unix seconds; None for anything else."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) + APPLE_EPOCH


def jwt_claims(token):
    """The payload of a JWT as a dict (no signature check: only e-mail, account id and plan are read)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        out = json.loads(base64.urlsafe_b64decode(part))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def auth_identity(path):
    """(account id, e-mail, raw plan) of a Codex auth.json, or None. Read-only; tokens are not kept."""
    a = qp.load_json(path)
    if not isinstance(a, dict):
        return None
    tok = a.get("tokens") if isinstance(a.get("tokens"), dict) else {}
    claims = jwt_claims(tok.get("id_token")) if isinstance(tok.get("id_token"), str) else {}
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    aid = norm(tok.get("account_id")) or norm(auth.get("chatgpt_account_id"))
    email = norm(claims.get("email"))
    if not aid and not email:
        return None
    return aid, email, norm(auth.get("chatgpt_plan_type"))


def email_uuid(email):
    """The stable stand-in id of an account known by its e-mail only: "email-hash:<12 hex of sha256>"."""
    return "email-hash:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:12] if email else None


def block_uuid(a):
    """The block's `uuid` of a discovered account: its account id, else email_uuid(e-mail); never empty."""
    return a["uuid"] or email_uuid(a["email"]) or "unknown"


def discover(p=None):
    """[{"uuid", "email", "short", "active", "managedId", "authPlan", "sources", "emailShared"}], ordered (see
    the module doc). uuid: normalized ChatGPT workspace account id (None when only the e-mail is known).

    Two passes: first every record with an id (one entry per id); then each e-mail-only record joins the one
    entry with that e-mail, or stays an entry of its own when no entry, or more than one (two workspaces of one
    login), has it. `active` follows the live record wherever it lands (sources has "live")."""
    p = p or paths()
    raw = []      # (uuid, email, source, managed id, plan)
    live = auth_identity(p["auth"])
    if live:
        raw.append((live[0], live[1], "live", None, live[2]))
    m = qp.load_json(p["managed"])
    for r in (m.get("accounts") if isinstance(m, dict) and isinstance(m.get("accounts"), list) else []):
        if not isinstance(r, dict):
            continue
        # CodexBar's effectiveWorkspaceAccountID: the workspace first, then the provider account
        uuid = norm(r.get("workspaceAccountID")) or norm(r.get("providerAccountID"))
        email = norm(r.get("email"))
        plan = None
        home = r.get("managedHomePath")
        # only a home inside CodexBar's own folder is looked into (its auth.json, for the plan claim)
        if isinstance(home, str) and os.path.realpath(home).startswith(os.path.realpath(p["managedHomes"]) + os.sep):
            ident = auth_identity(os.path.join(home, "auth.json"))
            if ident:
                uuid = uuid or ident[0]
                email = email or ident[1]
                plan = ident[2]
        raw.append((uuid, email, "managed", r.get("id") if isinstance(r.get("id"), str) else None, plan))

    found = []

    def merge(a, email, source, managed_id, plan):
        a["email"] = a["email"] or email
        a["managedId"] = a["managedId"] or managed_id
        a["authPlan"] = a["authPlan"] or plan
        if source not in a["sources"]:
            a["sources"].append(source)

    def new(uuid, email):
        a = {"uuid": uuid, "email": email, "managedId": None, "authPlan": None, "sources": []}
        found.append(a)
        return a

    for uuid, email, source, mid, plan in raw:
        if uuid:
            merge(next((a for a in found if a["uuid"] == uuid), None) or new(uuid, email), email, source, mid, plan)
    for uuid, email, source, mid, plan in raw:
        if uuid or not email:
            continue
        with_id = [a for a in found if a["uuid"] and a["email"] == email]
        same = with_id[0] if len(with_id) == 1 else next(
            (a for a in found if not a["uuid"] and a["email"] == email), None)
        merge(same or new(None, email), email, source, mid, plan)
    names = core.display_names(p["names"])
    order = {norm(k): i for i, k in enumerate(names)}
    lower = {norm(k): v for k, v in names.items()}
    mails = [a["email"] for a in found if a["email"]]
    for a in found:
        a["active"] = "live" in a["sources"]
        a["emailShared"] = bool(a["email"]) and mails.count(a["email"]) > 1
        a["short"] = (lower.get(a["uuid"]) if a["uuid"] else None) or lower.get(a["email"]) or a["email"] or a["uuid"]
    found.sort(key=lambda a: (order.get(a["uuid"], order.get(a["email"], len(order))), a["email"] or "", a["uuid"] or ""))
    return found


# ---------------------------------------------------------------------------- snapshots ----
def snapshot_for(records, uuid, email, claimed=(), email_shared=False):
    """The record of codex-account-snapshots.json for an account: the one whose
    accountIdentity.workspaceAccountID is this account's id. Else by normalized e-mail, but only from records
    that carry no workspace id or (for an account without an id) one no other listed account owns (`claimed`),
    only when this e-mail belongs to this one account (`email_shared` false), and only when exactly one record
    matches: two workspaces of one login never take each other's readings."""
    cands = []
    for r in records:
        if not isinstance(r, dict):
            continue
        ident = r.get("accountIdentity") if isinstance(r.get("accountIdentity"), dict) else {}
        wid = norm(ident.get("workspaceAccountID"))
        if uuid and wid == uuid:
            return r
        if wid and (uuid or wid in claimed):
            continue      # another workspace's record
        snap = r.get("snapshot") if isinstance(r.get("snapshot"), dict) else {}
        mails = {norm(ident.get("normalizedEmail")), norm(snap.get("accountEmail")), norm(r.get("id"))}
        if email and not email_shared and email in mails:
            cands.append(r)
    return cands[0] if len(cands) == 1 else None


def snapshot_records(path):
    s = qp.load_json(path)
    return [r for r in s.get("records") or [] if isinstance(r, dict)] if isinstance(s, dict) else []


def snapshot_readings(rec):
    """[(win, ts, pct, resets_at)] of a snapshot record (the windows it reports, by windowMinutes), in Unix
    seconds. ts = the snapshot's updatedAt; resets_at rounded like the live readings."""
    snap = rec.get("snapshot") if isinstance(rec.get("snapshot"), dict) else {}
    ts = apple_to_unix(snap.get("updatedAt"))
    if ts is None:
        return []
    out = []
    for lane in ("primary", "secondary", "tertiary"):
        w = snap.get(lane)
        if not isinstance(w, dict):
            continue
        win = WINDOW_MINUTES.get(w.get("windowMinutes"))
        pct = w.get("usedPercent")
        if not win or isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            continue
        ra = apple_to_unix(w.get("resetsAt"))
        out.append((win, int(ts), round(float(pct), 4), (int(ra) + 30) // 60 * 60 if ra else None))
    return out


def snapshot_plan(rec):
    snap = rec.get("snapshot") if isinstance(rec.get("snapshot"), dict) else {}
    obs = snap.get("planObservation") if isinstance(snap.get("planObservation"), dict) else {}
    ident = snap.get("identity") if isinstance(snap.get("identity"), dict) else {}
    return norm(obs.get("rawPlan")) or norm(snap.get("loginMethod")) or norm(ident.get("loginMethod"))


def credits_of(rec, now):
    """{"remaining", "note", "resetCredits", "updatedAt"} of a snapshot record, or None without credit data.
    resetCredits: expiry (Unix seconds) of each free rate-limit reset that is available and not expired,
    soonest first."""
    if not rec:
        return None
    c = rec.get("credits") if isinstance(rec.get("credits"), dict) else None
    snap = rec.get("snapshot") if isinstance(rec.get("snapshot"), dict) else {}
    rc = snap.get("codexResetCredits") if isinstance(snap.get("codexResetCredits"), dict) else None
    if c is None and rc is None:
        return None
    remaining = c.get("remaining") if c else None
    remaining = float(remaining) if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None
    note = None
    if c and c.get("balanceReadSucceeded") is False:
        note, remaining = "balance not read", None
    elif c and c.get("balanceIsWorkspace"):
        note = "workspace balance"
    resets = []
    for x in (rc.get("credits") if rc and isinstance(rc.get("credits"), list) else []):
        if not isinstance(x, dict) or x.get("status") != "available":
            continue
        exp = apple_to_unix(x.get("expires_at"))
        if exp is None or exp > now:
            resets.append(exp)
    resets.sort(key=lambda e: (e is None, e or 0))
    upd = apple_to_unix((c or {}).get("updatedAt")) or apple_to_unix((rc or {}).get("updatedAt"))
    return {"remaining": remaining, "note": note, "resetCredits": resets,
            "updatedAt": int(upd) if upd else None}


def remember_resets(con, tag, rec, now):
    """Keep confirmed redemptions when a later snapshot omits used credits."""
    if not tag:
        return []
    key = f"codex_resets@{tag}"
    old = core.kv_get(con, key, [])
    saved = set(old)
    snap = (rec or {}).get("snapshot")
    reset_credits = snap.get("codexResetCredits") if isinstance(snap, dict) else None
    credits = reset_credits.get("credits") if isinstance(reset_credits, dict) else None
    credits = credits if isinstance(credits, list) else []
    for credit in credits:
        if not isinstance(credit, dict) or credit.get("status") not in ("redeemed", None):
            continue
        raw = credit.get("redeemedAt") or credit.get("redeemed_at")
        try:
            if isinstance(raw, str):
                ts = core.parse_iso(raw)
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                ts = int(raw if raw >= 1e9 else apple_to_unix(raw))
            else:
                continue
            if 1e9 < ts <= now:
                saved.add(ts)
        except (ValueError, OverflowError, OSError):
            continue
    resets = sorted(saved)
    if resets != old:
        core.kv_set(con, key, resets)
    return resets


# ------------------------------------------------------------------------------ readings ----
def history_key(history, uuid):
    """CodexBar's account key of an account id in history/codex.json (exact case of the file), else the
    key as CodexBar writes it."""
    for k in (history or {}).get("accounts") or {}:
        if isinstance(k, str) and k.lower() == (KEY_PREFIX + uuid).lower():
            return k
    return KEY_PREFIX + uuid


def parse_live_lines(lines):
    """[(account key or None, plan or None, readings)] of the app's live file lines (quota_provider's rules)."""
    return [(qp.live_account(t), qp.live_plan(t), qp.parse_live(t)) for t in lines]


def readings_db(con, key, uuid, history, live, rec):
    """In-memory database with the collector schema holding this account's readings as samples(acc='codex')
    and the kv entries build_common reads. usage.db (`con`) is only read."""
    import codex
    P = codex.P
    mem = sqlite3.connect(":memory:")
    mem.executescript(core.SCHEMA)
    h = qp.account_hash(key)
    cur = core.kv_get(con, P.account_key)
    stored = P.acc if h and h == cur else f"{P.acc}:{h}"
    rows = [(P.acc,) + tuple(r[1:]) for r in con.execute(
        "SELECT acc,win,ts,pct,resets_at,source FROM samples WHERE acc=?", (stored,))]
    rows += qp.history_rows(P.acc, ((history or {}).get("accounts") or {}).get(key) or [])
    legacy = core.kv_get(con, P.legacy_key)
    for acc, _plan, got in live:
        if (acc and acc.lower() != key.lower()) or (not acc and (not h or legacy != h)):
            continue
        rows += [(P.acc, win, ts, pct, ra, "live") for win, ts, pct, ra in got]
    # the snapshot is the reading CodexBar fetched last: the same one as its live line when it wrote one
    rows += [(P.acc, win, ts, pct, ra, "live") for win, ts, pct, ra in (snapshot_readings(rec) if rec else [])]
    mem.executemany("INSERT OR IGNORE INTO samples(acc,win,ts,pct,resets_at,source) VALUES(?,?,?,?,?,?)", rows)
    core.kv_set(mem, P.account_key, h)
    core.kv_set(mem, P.account_id_key, qp.account_hash(uuid) if uuid else None)
    for k in (P.live_key, f"{P.acc}_history_accounts"):
        v = core.kv_get(con, k)
        if v is not None:
            core.kv_set(mem, k, v)
    return mem


def plan_since_for(con, tag, seen):
    """Plan start of one account: kv codex_plan_since@<tag> (kept by codex.plan_since_kept for the current
    account), moved on when the account's own logs show a later change."""
    import codex
    key = f"{codex.PLAN_SINCE_KEY}@{tag}"
    old = core.kv_get(con, key)
    if seen and (not isinstance(old, (int, float)) or seen > old):
        core.kv_set(con, key, int(seen))
        return int(seen)
    return int(old) if isinstance(old, (int, float)) else None


def plan_for(a, rec, live_plan, log_plan, plan_file, current):
    """(planName, planPriceUsd, planSource). codex-plan.json "accounts": {<id>: {...}} wins for that account;
    its top-level fields describe the current CodexBar account only (as before). Then the snapshot's plan,
    the app's live line, the auth.json plan claim, the session logs."""
    import codex
    f = qp.load_json(plan_file)
    f = f if isinstance(f, dict) else {}
    own = ((f.get("accounts") or {}) if isinstance(f.get("accounts"), dict) else {})
    own = next((v for k, v in own.items() if a["uuid"] and norm(k) == a["uuid"] and isinstance(v, dict)), None) or \
        next((v for k, v in own.items() if a["email"] and not a.get("emailShared") and norm(k) == a["email"]
              and isinstance(v, dict)), None)
    src = own if own is not None else (f if current else {})
    name = src.get("planName") if isinstance(src.get("planName"), str) and src["planName"].strip() else None
    price = src.get("planPriceUsd")
    price = float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None
    source = "file" if name else None
    if not name:
        for raw, s in ((snapshot_plan(rec) if rec else None, "snapshot"), (norm(live_plan), "live"),
                       (a.get("authPlan"), "auth"), (norm(log_plan), "logs")):
            if raw:
                name, source = codex.PLAN_NAMES.get(raw, raw.capitalize()), s
                break
    return name, price if price is not None else qp.plan_price(codex.P, name), source


def account_block(con, now, a, events, stats, history, live, records, p, claimed=(), timeline_accounts=None):
    import codex
    uuid, email = a["uuid"], a["email"]
    key = history_key(history, uuid) if uuid else None
    rec = snapshot_for(records, uuid, email, claimed, a.get("emailShared", False))
    tags = {qp.account_hash(uuid)} if uuid else set()
    evs = [e for e in events if e.get("acct") in tags]
    tag = qp.account_hash(uuid) if uuid else None
    resets = remember_resets(con, tag or qp.account_hash(block_uuid(a)), rec, now)
    log_plan, log_plan_at, log_since = codex.plans_of(stats["plans"], tags) if tags else (None, None, None)
    since = plan_since_for(con, tag, log_since) if tag else None
    h = qp.account_hash(key) if key else None
    current = bool(h and h == core.kv_get(con, codex.P.account_key))
    live_plan = next((plan for acc, plan, _ in reversed(live)
                      if key and acc and acc.lower() == key.lower() and plan), None)
    plan = plan_for(a, rec, live_plan, log_plan, p["planFile"], current)
    mem = readings_db(con, key, uuid, history, live, rec) if key else None
    if mem is None:
        mem = sqlite3.connect(":memory:")
        mem.executescript(core.SCHEMA)
    prov = copy.copy(codex.P)
    prov.plan_override = {"planName": plan[0], "planPriceUsd": plan[1]}
    models = codex.models_30d(evs, now)
    intervals = [] if timeline_accounts is not None else None
    try:
        info = qp.build_common(prov, mem, now, evs, models, plan_seen=plan[0], since=since,
                               complete=codex.scan_complete(stats), ambiguous=[e for e in events if not e.get("acct")],
                               resets=resets, weekly_intervals=intervals)
        n5 = mem.execute("SELECT max(ts) FROM samples WHERE win='5h'").fetchone()[0]
    finally:
        mem.close()
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    info["cache"] = {"today": codex.cache_stats(evs, today0), "7d": codex.cache_stats(evs, now - 7 * 86400),
                     "30d": codex.cache_stats(evs, now - 30 * 86400)}
    snap_wins = {r[0] for r in snapshot_readings(rec)} if rec else set()
    snap_ts = max((r[1] for r in snapshot_readings(rec)), default=None) if rec else None
    # the account reports no 5-hour window now (Pro 5x: weekly only): an older 5-hour reading is stale
    if snap_ts and "7d" in snap_wins and "5h" not in snap_wins and (n5 is None or n5 < snap_ts):
        info.get("windows", {}).pop("5h", None)
        info["has5h"] = False
    recent = [e for e in evs if e["ts"] >= now - 30 * 86400]
    info["coverage"] = {
        "tokens": "exact", "usd": "estimate", "historySince": evs[0]["day"] if evs else None,
        "calls": sum(e["calls"] for e in evs),
        "unpricedCalls30d": sum(m["unpricedCalls"] for m in models.values()),
        "unconfirmedPrices": sorted(f"{k} fast long" for k, m in models.items() if m["estimatedPriceCalls"]),
        "fastCalls30d": sum(e["calls"] for e in recent if e["tier"] == "priority"),
        "longContextCalls30d": sum(e["calls"] for e in recent if e["long"]),
        "planSeen": log_plan, "planSeenAt": log_plan_at, "planSince": since,
        "pricesChecked": codex.PRICES_CHECKED, "priceSource": codex.PRICE_SOURCE,
        "attribution": "calls whose session log names this account (creator_account_id)",
        "notCounted": "calls of logs that name no account (state.codex.unattributed), Codex cloud tasks, "
                      "code review on GitHub, other computers",
    }
    info.update({"uuid": block_uuid(a), "name": email or uuid or "", "short": a["short"] or "",
                 "active": bool(a["active"]), "planName": plan[0], "planPriceUsd": plan[1], "planSource": plan[2],
                 "managed": a["managedId"] is not None, "codexbarCurrent": current,
                 "credits": credits_of(rec, now), "trackedSince": evs[0]["ts"] if evs else None})
    if rec:
        info["snapshotAt"] = snap_ts
    if timeline_accounts is not None:
        timeline_accounts.append(account_input("codex", info, intervals, event_spend(evs), now))
    return info


def unattributed(events, known_tags, now):
    """Spend of calls no listed account owns: logs that name no account (Codex before 12.10.2026), or an
    account that is no longer on this Mac. Sum of accounts[] + this = the block's totals."""
    out = {"usdToday": 0.0, "usd24h": 0.0, "usd7d": 0.0, "usd30d": 0.0, "usd180d": 0.0, "tokens24h": 0,
           "tokens30d": 0, "tokens180d": 0, "calls": 0, "noAccountCalls": 0, "otherAccountCalls": 0,
           "firstAt": None, "lastAt": None}
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    for e in events:
        if e.get("acct") in known_tags:
            continue
        tok = e["input"] + e["cacheRead"] + e["cacheWrite"] + e["output"]
        out["usd180d"] += e["usd"]
        out["tokens180d"] += tok
        out["calls"] += e["calls"]
        out["noAccountCalls" if not e.get("acct") else "otherAccountCalls"] += e["calls"]
        for k, since in (("usdToday", today0), ("usd24h", now - 86400), ("usd7d", now - 7 * 86400),
                         ("usd30d", now - 30 * 86400)):
            if e["ts"] >= since:
                out[k] += e["usd"]
        if e["ts"] >= now - 86400:
            out["tokens24h"] += tok
        if e["ts"] >= now - 30 * 86400:
            out["tokens30d"] += tok
        out["firstAt"] = e["ts"] if out["firstAt"] is None else min(out["firstAt"], e["ts"])
        out["lastAt"] = e["ts"] if out["lastAt"] is None else max(out["lastAt"], e["ts"])
    for k in ("usdToday", "usd24h", "usd7d", "usd30d", "usd180d"):
        out[k] = round(out[k], 2)
    out["note"] = ("Calls whose session log names no ChatGPT account (Codex wrote creator_account_id from "
                   "12.10.2026 on) or an account not on this Mac: in the totals above, in no account below.")
    return out


def blocks(con, now, events, stats, p=None, *, records=None, timeline_accounts=None):
    """(accounts list, unattributed). Never raises: a failed account becomes an error entry, a failure to
    list the accounts gives ([], unattributed of every call)."""
    import codex
    p = p or paths()
    try:
        accts = discover(p)
    except Exception as e:
        return [], {**unattributed(events, set(), now), "error": qp.error_code("codex-accounts", e)}
    history = qp.load_json(p["history"])
    history = history if isinstance(history, dict) else {}
    try:
        with open(p["live"], "rb") as fh:
            data = fh.read()
        # complete lines only, as import_live: the last one may still be being written
        live = parse_live_lines(ln.decode("utf-8", "replace") for ln in data[:data.rfind(b"\n") + 1].split(b"\n")
                                if ln.strip())
    except OSError:
        live = []
    if records is None:
        records = snapshot_records(p["snapshots"])
    claimed = {a["uuid"] for a in accts if a["uuid"]}
    out = []
    for a in accts:
        try:
            out.append(account_block(con, now, a, events, stats, history, live, records, p, claimed, timeline_accounts))
        except Exception as e:
            out.append({"uuid": block_uuid(a), "name": a["email"] or a["uuid"] or "", "short": a["short"] or "",
                        "active": bool(a["active"]), "beta": True, "error": qp.error_code("codex", e)})
            if timeline_accounts is not None:
                tag = qp.account_hash(a["uuid"])
                evs = [e for e in events if tag and e.get("acct") == tag]
                timeline_accounts.append(account_input("codex", out[-1], [], event_spend(evs), now))
    known = {qp.account_hash(a["uuid"]) for a in accts if a["uuid"]}
    return out, unattributed(events, known, now)


def main():
    for a in discover():
        print(("✓ " if a["active"] else "  ") + f"{a['short']}  <{a['email']}>  {a['uuid'] or '-'}  "
              f"managed={a['managedId'] or '-'}  plan={a['authPlan'] or '-'}  from={','.join(a['sources'])}")


if __name__ == "__main__":
    main()
