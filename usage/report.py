#!/usr/bin/env python3
"""Summary page: limits of all accounts, my share, subscription value, limit drift.

Writes usage/dashboard.html (refreshed on every tick.py pass); open it with a double-click.
"""
import html
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
TIERS = core.TIERS
MONTH = 30.44 / 7

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1c1a;--mute:#77736b;--line:#e4e1da;--me:#d9622b;--cc:#e9b08f;
--chat:#8f8ad6;--cowork:#5aa6a0;--out:#b9b4a9;--free:#ece9e2;--opus:#d9622b;--fable:#6b63c9;--other:#9a958a}
@media(prefers-color-scheme:dark){:root{--bg:#171614;--card:#211f1c;--ink:#f1eee8;--mute:#a09a8f;--line:#35322d;
--me:#f07d45;--cc:#8a5a41;--chat:#a7a2ee;--cowork:#6fc0b9;--out:#6d685f;--free:#2c2a26;--opus:#f07d45;--fable:#8f88ea;--other:#7d786e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}h2{font-size:18px;margin:34px 0 12px;letter-spacing:-.01em}
.sub{color:var(--mute);margin:0 0 22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 16px 14px}
.name{font-weight:650;font-size:17px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tag{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}
.tag.on{background:var(--me);border-color:var(--me);color:#fff}
.row{margin-top:14px}.lab{display:flex;justify-content:space-between;font-size:13px;color:var(--mute);margin-bottom:5px}
.lab b{color:var(--ink);font-weight:600}.bar{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--free)}
.bar i{display:block;height:100%}.note{font-size:12.5px;color:var(--mute);margin-top:5px}
.leg{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:var(--mute);margin:10px 0 0}
.leg span:before{content:"";display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;background:var(--c)}
.big{font-size:30px;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:1fr auto;gap:5px 12px;font-size:14px;margin-top:10px;font-variant-numeric:tabular-nums}
.kv span:nth-child(odd){color:var(--mute)}.kv span:nth-child(even){text-align:right;font-weight:600}
svg{display:block;width:100%;height:auto}svg text{fill:var(--mute);font-size:11px}
.method{font-size:13.5px;color:var(--mute)}.method li{margin:5px 0}
"""


def e(s):
    return html.escape(str(s))


def money(v):
    return "$" + f"{v:,.0f}".replace(",", " ")


def bar(parts):
    return '<div class="bar">' + "".join(
        f'<i style="width:{max(0, min(100, w)):.2f}%;background:var(--{c})" title="{e(t)}"></i>' for w, c, t in parts if w > 0
    ) + "</div>"


def window_block(title, w, now):
    if not w:
        return ""
    stale = now - (w.get("measuredAt") or 0) > 25 * 60
    pct = w.get("pct") or 0
    ra = w.get("resetsAt")
    if ra and ra <= now:
        pct = 0
    mine = (w.get("mine") or 0) if "mine" in w else None
    parts, legend = [], []
    srv = w.get("server")
    if mine is not None and srv:
        cc_other = max(0.0, srv.get("claude_code", 0) - mine)
        parts = [(mine, "me", "Me, Claude Code on this Mac"), (cc_other, "cc", "Claude Code not on this Mac"),
                 (srv.get("chat", 0), "chat", "Chat"), (srv.get("cowork", 0), "cowork", "Cowork"),
                 (srv.get("other", 0), "out", "Other")]
        rest = max(0.0, pct - sum(p[0] for p in parts))
        parts.append((rest, "out", "Unattributed"))
        legend = [f'<span style="--c:var(--{c})">{e(t)} {v:.0f} %</span>' for v, c, t in parts if v >= 0.5]
    elif mine is not None:
        mine = min(mine, pct)
        parts = [(mine, "me", "Me"), (pct - mine, "out", "Outside this Mac")]
        legend = [f'<span style="--c:var(--me)">me {mine:.0f} %</span>',
                  f'<span style="--c:var(--out)">outside this Mac {pct - mine:.0f} %</span>']
    elif srv and pct > 0:
        # the rate of this account is not known yet, but the server's own split is
        parts = [(srv.get("claude_code", 0), "cc", "Claude Code, all devices"), (srv.get("chat", 0), "chat", "Chat"),
                 (srv.get("cowork", 0), "cowork", "Cowork"), (srv.get("other", 0), "out", "Other")]
        parts.append((max(0.0, pct - sum(p[0] for p in parts)), "out", "Unattributed"))
        legend = [f'<span style="--c:var(--{c})">{e(t)} {v:.0f} %</span>' for v, c, t in parts if v >= 0.5]
    else:
        parts = [(pct, "out", "Used")]
    head = f"<b>{pct:.0f} %</b> used" + (f" · mine <b>{mine:.0f} %</b>" if mine is not None else "")
    when = ("resets " + core.local(ra, "%d.%m %H:%M")) if ra and ra > now else ""
    note = []
    if w.get("sinceMeasure"):
        note.append(f"since the reading I used ≈ {w['sinceMeasure']:.0f} more points")
    if stale:
        note.append("last known value, read " + core.local(w.get("measuredAt") or 0))
    u = w.get("usd")
    if u:
        note.append(f"my spend in this window: Opus {money(u['opus'])}, Fable {money(u['fable'])}")
    return (f'<div class="row"><div class="lab"><span>{e(title)} · {head}</span><span>{e(when)}</span></div>'
            + bar(parts) + (f'<div class="leg">{"".join(legend)}</div>' if legend else "")
            + (f'<div class="note">{e(" · ".join(note))}</div>' if note else "") + "</div>")


def account_card(a, now):
    tier, price = TIERS.get(a.get("tier") or "", (a.get("tier") or "plan unknown", None))
    tags = f'<span class="tag">{e(tier)}</span>'
    if a.get("active"):
        tags += '<span class="tag on">in Claude now</span>'
    if a.get("useHapp"):
        tags += '<span class="tag">VPN only</span>'
    ws = a.get("windows") or {}
    body = window_block("5 hours", ws.get("5h") if (ws.get("5h") or {}).get("source") == "poll" else None, now)
    body += window_block("Week", ws.get("7d"), now)
    body += window_block("Fable, week", ws.get("fable"), now)
    if not body:
        lp = (a.get("lastPoll") or {}).get("status")
        why = {"vpn_mismatch": "limits not queried yet: waiting for the VPN to be in the right state",
               "auth_rejected": "the server rejected the saved sign-in: sign in to the account through the switcher",
               "keychain_denied": "no access to the Keychain"}.get(lp, "no readings yet")
        body = f'<div class="note" style="margin-top:14px">{e(why)}</div>'
    kv = (f'<div class="kv"><span>My spend today (API prices)</span><span>{money(a.get("usdToday", 0))}</span>'
          f'<span>Last 30 days</span><span>{money(a.get("usd30d", 0))}</span></div>')
    return f'<div class="card"><div class="name">{e(a["short"])}{tags}</div>{body}{kv}</div>'


def value_card(a, k):
    v = a.get("value")
    if not v:
        return ""
    tier, price = TIERS.get(a.get("tier") or "", ("", None))
    rows = [("Whole week on Opus 5.5", v["weekOpusUsd"])]
    if v.get("weekFableCapUsd"):
        rows.append((f"Fable cap ({v['fableCapShareOfWeek']:.0f} % of the week)", v["weekFableCapUsd"]))
        rows.append(("Fable up to the cap + the rest on Opus", v["weekMixUsd"]))
    month = v["weekOpusUsd"] * MONTH
    out = f'<div class="card"><div class="name">{e(a["short"])}<span class="tag">{e(tier)}</span></div>'
    out += f'<div class="big" style="margin-top:10px">{money(month)}<span style="font-size:15px;font-weight:500;color:var(--mute)"> a month on Opus</span></div>'
    if price:
        out += f'<div class="note">${price} plan → Opus tokens {month / price:.0f}× cheaper than the API'
        if v.get("weekFableCapUsd"):
            out += f'; Fable up to the cap: {money(v["weekFableCapUsd"] * MONTH)} a month'
        out += "</div>"
    out += '<div class="kv">' + "".join(f"<span>{e(n)}, per week</span><span>{money(x)}</span>" for n, x in rows) + "</div>"
    r5 = (a.get("rates") or {}).get("5h")
    if r5:
        out += f'<div class="kv"><span>One 5-hour window on Opus</span><span>{money(r5["usd_per_100"])}</span></div>'
    return out + "</div>"


def drift_chart(accounts):
    series = [(a["short"], a["drift"]) for a in accounts if len(a.get("drift") or []) >= 1]
    if not series:
        return ""
    xs = [d["weekEnd"] for _, dr in series for d in dr]
    ys = [d["usd_per_100"] for _, dr in series for d in dr]
    x0, x1 = min(xs), max(xs) + 1
    y1 = max(ys) * 1.15
    W, H, L, B, T = 1000, 300, 56, 30, 14
    px = lambda x: L + (x - x0) / max(1, x1 - x0) * (W - L - 16)
    py = lambda y: T + (1 - y / y1) * (H - T - B)
    cols = ["me", "fable", "cowork"]
    s = f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Value of the weekly limit, week by week">'
    step = 500 if y1 < 2500 else 1000
    y = 0
    while y <= y1:
        s += f'<line x1="{L}" x2="{W-16}" y1="{py(y):.1f}" y2="{py(y):.1f}" stroke="var(--line)"/><text x="{L-8}" y="{py(y)+4:.1f}" text-anchor="end">${y}</text>'
        y += step
    for i, (name, dr) in enumerate(series):
        c = f"var(--{cols[i % 3]})"
        pts = " ".join(f"{px(d['weekEnd']):.1f},{py(d['usd_per_100']):.1f}" for d in dr)
        s += f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2.5" stroke-linejoin="round"/>'
        for d in dr:
            s += (f'<circle cx="{px(d["weekEnd"]):.1f}" cy="{py(d["usd_per_100"]):.1f}" r="4" fill="{c}">'
                  f'<title>{e(name)}: week ending {core.local(d["weekEnd"], "%d.%m")}, 100 % = {money(d["usd_per_100"])}</title></circle>')
        last = dr[-1]
        s += f'<text x="{px(last["weekEnd"])-6:.1f}" y="{py(last["usd_per_100"])-10:.1f}" text-anchor="end" style="fill:{c};font-weight:600">{e(name)}</text>'
    for x in sorted(set(xs))[::max(1, len(set(xs)) // 8)]:
        s += f'<text x="{px(x):.1f}" y="{H-8}" text-anchor="middle">{core.local(x, "%d.%m")}</text>'
    return s + "</svg>"


def daily_chart(accounts, now):
    days = sorted({d["day"] for a in accounts for d in a.get("daily", [])})
    if not days:
        return ""
    tot = {day: [0.0, 0.0, 0.0] for day in days}
    per = {}
    for i, a in enumerate(accounts):
        for d in a.get("daily", []):
            per.setdefault(d["day"], [0.0] * len(accounts))[i] += d["opus"] + d["fable"] + d["other"]
    ymax = max(sum(v) for v in per.values()) * 1.1 or 1
    W, H, L, B, T = 1000, 260, 56, 30, 10
    bw = (W - L - 16) / len(days)
    cols = ["me", "fable", "cowork"]
    s = f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="My spend by day and account">'
    step = 200 if ymax < 1200 else 500
    y = 0
    while y <= ymax:
        yy = T + (1 - y / ymax) * (H - T - B)
        s += f'<line x1="{L}" x2="{W-16}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="var(--line)"/><text x="{L-8}" y="{yy+4:.1f}" text-anchor="end">${y}</text>'
        y += step
    for j, day in enumerate(days):
        base = H - B
        for i, v in enumerate(per.get(day, [])):
            h = v / ymax * (H - T - B)
            if h > 0.3:
                s += (f'<rect x="{L + j*bw + 1.5:.1f}" y="{base-h:.1f}" width="{max(1, bw-3):.1f}" height="{h:.1f}" rx="2" '
                      f'fill="var(--{cols[i % 3]})"><title>{e(accounts[i]["short"])}, {day}: {money(v)}</title></rect>')
                base -= h
        if j % max(1, len(days) // 10) == 0:
            s += f'<text x="{L + j*bw + bw/2:.1f}" y="{H-8}" text-anchor="middle">{day[8:]}.{day[5:7]}</text>'
    s += "</svg>"
    leg = "".join(f'<span style="--c:var(--{cols[i % 3]})">{e(a["short"])}</span>' for i, a in enumerate(accounts))
    return s + f'<div class="leg">{leg}</div>'


def render(st):
    now = int(time.time())
    accs = st.get("accounts", [])
    k = st.get("k_fable", 2.5)  # shared fallback; each account may carry its own kFable
    out = [f"<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv='refresh' content='300'><title>Claude limits</title><style>{CSS}</style><main>",
           "<h1>Claude limits by account</h1>",
           f"<p class='sub'>Updated {core.local(st.get('generatedAt', now), '%d.%m.%Y %H:%M')} · “mine” is what was spent through Claude Code on this Mac</p>",
           "<div class='grid'>", *[account_card(a, now) for a in accs], "</div>"]
    vals = [value_card(a, a.get("kFable") or k) for a in accs]
    if any(vals):
        out += ["<h2>What the subscription is worth in API dollars</h2><div class='grid'>", *vals, "</div>",
                "<p class='note'>A Fable dollar uses up the shared weekly limit faster than an Opus dollar: "
                + ", ".join(f"{e(a['short'])} {(a.get('kFable') or k):.2f}×" for a in accs) + ".</p>"]
    dc = drift_chart(accs)
    if dc:
        out += ["<h2>Does the limit change: how many Opus dollars make 100 % of a week</h2><div class='card'>", dc,
                "<div class='note'>Each point is a separate week, computed from that week's data only. On shared accounts a point can be "
                "too low in weeks when other people used the account without breaks.</div></div>"]
    out += ["<h2>My spend by day, API prices</h2><div class='card'>", daily_chart(accs, now), "</div>"]
    out += ["""<h2>How it is calculated</h2><ul class='method'>
<li>Spend comes from the Claude Code logs on this Mac; each model reply is assigned to an account from the Claude app's session metadata.</li>
<li>Limit percentages are queried from claude.ai every 5 minutes for the active account and every 15 for the others. An account marked “VPN only” is queried only while the VPN is up, the others only while it is down.</li>
<li>The rate “how many dollars make one percent” is taken at its lower bound: spend outside this Mac is assumed never to be negative. My spend is not stretched to fill the whole percentage; the rest is shown as “not on this Mac”.</li>
<li>The server itself reports how the week splits into Claude Code, chat and Cowork.</li></ul>""", "</main></html>"]
    return "".join(out)


def write(st=None):
    if st is None:
        st = json.load(open(f"{core.DATA}/state.json"))
    tmp = OUT + ".tmp"
    open(tmp, "w").write(render(st))
    os.replace(tmp, OUT)
    return OUT


if __name__ == "__main__":
    print(write())
