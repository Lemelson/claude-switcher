# Claude limits and spend per account

Built on 23 November 2026. It extends Claude Switcher: it shows the limits of all accounts, keeps their spend separate and computes how many percent of the limit were used through Claude Code on this Mac.

## Where things are

| What | Where |
|---|---|
| Code | this folder (`usage/`) |
| Database, summary, pass log | `~/Library/Application Support/ClaudeSwitcher/usage/` (`usage.db`, `state.json`, `tick.log`) |
| Program that reads the saved login | `~/Library/Application Support/ClaudeSwitcher/usage/bin/claude-limits-helper` |
| Autostart every 5 minutes | `~/Library/LaunchAgents/com.lemelson.claude-usage.plist` |
| Summary page | `usage/dashboard.html` (rebuilt on every pass) |
| CodexBar settings | `~/.codexbar/config.json`, keys `claudeSwapEnabled`, `claudeSwapExecutablePath` under the `claude` provider; copy of the previous settings: `config.json.bak-2026-11-23` |

## Parts

- `scan.py` reads new lines of `~/.claude/projects/**/*.jsonl` into the database (it remembers up to which byte each file has been read), computes the cost of every response at API prices and attributes it to an account using the Claude app session metadata. Along the way it collects "hit the limit" records: these are exact "100 %" points.
- `limits-helper/main.swift` is the only part that touches the saved login. It takes the `sessionKey` cookie from the profile's `Cookies` file, decrypts it with the key from the Keychain ("Claude Safe Storage") and requests `claude.ai/api/organizations/{org}/usage`. It writes and refreshes nothing and does not print the values. Build: `./build.sh`.
- `poll.py` polls with the VPN rule: an account with "Connect VPN Happ" enabled is queried only while the VPN is up, all others only without it. The active account every 5 minutes, the others every 15. Since 23.11.2026 (evening) the VPN account is also queried only while it is the active account (see "Limit readings in the background" below).
- `history_import.py` imports the percentage history from CodexBar (for retroactive calculation).
- `calib.py` computes the "how many dollars per 1 %" rates, my share, the subscription value and the drift by week. The method is described at the top of the file.
- `report.py` builds the summary page.
- `cswap` is the account provider for CodexBar in the claude-swap format. Switching from CodexBar is disabled on purpose.
- `tick.py` is one full pass; this is what the autostart runs.

## Commands

```bash
/opt/homebrew/bin/python3 usage/tick.py --force
```

```bash
launchctl kickstart -k gui/$(id -u)/com.lemelson.claude-usage
```

Turn collection off: `launchctl bootout gui/$(id -u)/com.lemelson.claude-usage`.
Restore CodexBar to how it was: remove the three `claudeSwap*` keys from `~/.codexbar/config.json` and restart CodexBar.

## Limitations

- The server returns the limit percentage as an integer, so "my share" is accurate to about ±1-2 percentage points.
- The 5-hour window rate is still preliminary: before 23.11.2026 there were only sparse CodexBar samples and limit hits for it.
- An account marked VPN-only has rates only once enough samples accumulate (they are taken only while it is the active account and the VPN is up).
- The background accounts get no readings while the VPN is up, so their history has gaps for exactly those hours.
- CodexBar shows the limits of all accounts and "mine X of Y %" in the account name; the breakdown, dollars per account and subscription value are on the summary page. Showing them directly in CodexBar requires rebuilding it.
- After rebuilding `claude-limits-helper` the Keychain permission is kept as long as it is signed with the same developer certificate.

## On-demand refresh and the custom CodexBar build (23.11.2026)

- `tick.py --refresh` queries the limits immediately, but no more than once per 45 seconds per account; accounts are queried in parallel and a pass takes 2-3 seconds. The custom CodexBar build runs it every time its menu opens. The VPN rule applies as always.
- The `state.json` summary (schema 3) gained: the progress of each window for the chart (`windows.*.series`), my spend per weekly window (`weeks`), spend per model in the weeks of the limit chart (`drift[].spend`), the time of the last server answer (`lastMeasuredAt`), the path to the collector (`collector`).
- `cswap` gives CodexBar short account names in English.
- The owner's own account names: `account-names.json` in the data folder, `{"<account uuid>": "Label"}` (the full Switcher name or the derived short name also work as keys). `core.accounts()` puts the label into `short`, which every page, `cswap` and `tick.log` show. The file lives outside the repository; without it the short name is derived from the Switcher name as before.
- What was changed in CodexBar itself and how to update it: `../codexbar-fork/README.md`.

## Window forecast and spend per model (23.11.2026, schema 4)

- `windows.*.forecast` is where the window will be at reset. Pace = half the window's average pace + half the pace over the last hour (for weekly windows, over 48 hours). Then a correction: on past windows we compute by what factor the actual remaining spend differed from such a forecast and take the 20 / 50 / 80 % quantiles. The middle goes to `endPct` and `hitAt`, the edges to `endLo` / `endHi` and `hitLate` / `hitEarly`. For 5-hour windows the middle is about 0.4: I usually slow down, and the straight "at this pace" line overestimates spend 2.5 times. For weekly windows it is about 0.86.
- If an account has few past windows (fewer than 30 checks), the spread is pooled across all accounts. Backtest on history: `forecast_band()`; the numbers for the report come from calling it with different `fc`.
- `daily` now covers 180 days; each day has `m`, the spend per model family `[dollars, tokens]` (opus, fable, sonnet, haiku, other), and `tokens`. The same in `weeks[].m`. `trackedSince` is the first spend record of the account.
- The weight of Sonnet and Haiku in the limit is not measured: a regression on the available data gives nonsense (for Haiku from -16 to +26), because the spend on them is too small.

## Models, weekly windows, background readings, cache (23.11.2026, evening, schema 4)

`schema` stays 4: every new field is optional, and the CodexBar reader ignores fields it does not know.

### Models

- Four Claude models are first class: `opus`, `fable` (Mythos counts as Fable), `sonnet`, `haiku`; anything else is `other`. `daily[].m` and `weeks[].m` of every Claude account always carry exactly these five keys in this order, `[0.0, 0]` where nothing ran. The old three-way totals (`opus`, `fable`, `other` in `daily[]`, `usd{}` in `weeks[]` and windows, `hits[]`) did not change; there `other` still means everything except Opus and Fable.
- The Antigravity block keeps its own model keys.

### Weekly windows (`accounts[].weeks[]`)

Each row now says how far the weekly limit went that week from all sources, and how much of it was Claude Code on this Mac.

- `peakPct`: the highest reading of the weekly limit in that window, all sources. A weekly limit hit in the Claude Code logs counts as a 100 % reading. null = no reading at all.
- `peakAt`: when that reading was taken (the latest one, if the same value was read several times).
- `peakFinal`: true when the reading was taken in the last 6 hours of the window, or it is 100 % (the limit cannot go higher): this is where the week ended. false: "at least this much", the week may have ended higher. Always false for the current week below 100 %.
- `minePct`: Claude Code on this Mac in points of that week's limit: the week's dollars, Fable times the account's `kFable`, through the account's current weekly rate. This is the arithmetic of `windows.7d.mine`, and for the current week the value is copied from there, so the two always agree. It is capped at `peakPct` when the peak is final and at 100 otherwise. null when the account has no weekly rate yet.
- `minePctByModel`: `{opus, fable, sonnet, haiku, other}` in the same points; the rounded parts add up exactly to `minePct`.
- `othersPct`: `max(0, peakPct - minePct)`: what was used from elsewhere (chat, phone, other people). For the current week it is `windows.7d.outside`. When `peakFinal` is false it is a lower bound. null when either number is null.
- `resetAt`: when the limit was reset in the middle of that window (the reading fell by more than 1.5 points while the reset time stayed, as on 16.10.2026 and 14.11.2026; `calib.RESET_DROP`, and 4 points for the Antigravity block, whose readings settle by up to 2 points after use), else null. Such a reset gives a fresh limit for the rest of the window, so `peakPct`, `peakAt`, `peakFinal`, `minePct`, `minePctByModel` and `othersPct` then describe the part after the last reset, the same part `windows.7d` shows for the current week; `usd`, `m` and `tokens` stay the whole week. A weekly limit hit just before the reset belongs to the part before it. (Until this rule the week of 14.11 on the Max 20x account read "peak 100, mine 100, others 0": the hit before the reset set against the whole week's dollars, 169 points of them. It now reads "peak 81 at the end, mine 73.2, others 7.8".)
- How reliable `minePct` is for old weeks: the rate is measured over the last 35 days and applied to every week. The same rate measured on one week alone (`drift[]`) moves between weeks: on the Max 5x account from $804 to $1 307 per 100 % over eleven weeks against $1 255 now. So for weeks older than about a month `minePct` can be off by a third in either direction; the current and last week are as good as `windows.7d.mine`. Spend the collector could not attribute makes single weeks worse.
- A failure stays local: an account that cannot be computed is written as `{"uuid", "name", "short", "active", "useHapp", "refresh", "refreshEverySec", "cache", "error"}` and the other accounts are complete; a failed cache step leaves `cache` null (top level and per account) and `cacheError` at the top level. Before, either one left the whole `state.json` unwritten.

### Limit readings in the background

- `accounts[].refresh`: `"background"` for every account that does not need the VPN. The 5-minute pass queries them by itself whichever account is active in Claude: the active one every 5 minutes, the others every 15 (`accounts[].refreshEverySec` = 300 or 900). No account switch, no VPN change, no write to the Keychain or to the Claude Code login: the helper only reads the saved `Cookies`.
- `"manual"` for the VPN-only account (`refreshEverySec` null). It is queried only while it is the active account in Claude and the VPN is up, so only while the owner works in it (then every 5 minutes and on every menu opening). Otherwise it is not queried at all, not even by `--force` or `--refresh`, and keeps its last reading; the pass logs it as `manual_idle`. The owner decided this: a background query must never reach that account from a wrong address.
- While the VPN is up, the background accounts cannot be queried: every connection of the Mac, the helper's too, leaves through the VPN, so claude.ai would see those accounts from the VPN's address, the same address the VPN-only account uses. The helper refuses (status `vpn_mismatch`, written to `polls` at most once per 15 minutes), the account keeps its last reading, and the first pass after the VPN goes down (within 5 minutes) queries it again. The page can tell this case from `vpnUp: true` plus `lastPoll.status == "vpn_mismatch"`; the age of the reading is `windows.*.measuredAt`.
- On demand: `tick.py --refresh` (the CodexBar menu) queries the same accounts as the scheduled pass, under the same rules, but right away and at most once per 45 seconds per account.

History of readings: there is no separate history file, because the database already keeps all of it.

| What | Where | Written | Kept |
|---|---|---|---|
| Every limit reading | `usage.db`, table `samples`: `acc, win, ts, pct, resets_at, source`; one row per window per reading. `win`: `5h`, `7d`, `fable`, `7d:claude_code` / `7d:chat` / `7d:cowork` / `7d:other` (the server's own split of the week); `source`: `poll` (this collector), `codexbar` (imported CodexBar history), `live` (Antigravity) | on every successful query, so every 5 or 15 minutes while the account can be queried | `5h`, `7d`, `fable`: forever, every reading (rates, forecasts, weekly peaks and series read all of them). The `7d:*` split: after 14 days thinned within each key and week to the first and last reading of every value (the step line of every week stays the same). About 130 bytes per row: roughly 40 MB a year for an account queried every 5 minutes, 15 MB for one queried every 15; the thinning takes the split's share of that down by about nine tenths |
| Every query attempt | `usage.db`, table `polls`: `ts, acc, status, vpn, via, raw` | on every attempt; `vpn_mismatch` at most once per 15 minutes | rows forever; the full server answer in `raw` (about 3 KB) only for 14 days, then set to null |
| Limit hits | `usage.db`, table `hits` | from the Claude Code logs on every pass | forever |

Both clean-ups run once a day inside the poll step (`poll.compact()`, mark `polls_compacted_at` in `kv`); the day it runs, `tick.log` has `compact:raw=N,thinned=M` in `poll`. A failure there is logged as `compact:error …` and never stops the queries (it is tried again on the next pass). `windows.*.server` reads the split at the time of the current reading, which is never thinned. Anything that reads older split rows must take the last row of that key and week at or before the time it wants, not a row at exactly the time of a `7d` reading. A year of split rows of three accounts (about 700 000) thins in 2.4 s the first time and 0.1 s a day after that. `windows.*.series` in `state.json` is drawn from `samples` for the current window only.

### Cache (`accounts[].cache` and the top-level `cache`)

Same shape in both, for `today` (since local midnight), `7d` and `30d`:

```json
"cache": {"7d": {"readPct": 98.1, "missPct": 1.9, "calls": 16887, "readTokens": 4334924995,
                 "missTokens": 85634503, "writeTokens": 85599133, "savedUsd": 20725.18, "missUsd": 594.22}}
```

- Every reply of Claude starts by reading the whole conversation so far (the prompt). Anthropic keeps recent prompts for 5 minutes or 1 hour; the part of a new prompt that matches a kept one is read from the cache at about a tenth of the input price or less, the rest is paid in full.
- `readPct`: the part of the prompt tokens read from the cache. `missPct` = 100 - `readPct`: the part paid in full. A token written to the cache counts as missed on that call (writing costs 1.25x of input for 5 minutes, 2x for 1 hour) and as read on the following ones.
- `calls`: replies with a prompt. `readTokens`, `missTokens` (plain input plus cache writes), `writeTokens` (the cache-write part of `missTokens`).
- `savedUsd`: what the cache reads saved against paying the full input price for those tokens. `missUsd`: what the missed tokens cost above the cache-read price, so the most a perfect cache could have saved. Both at API prices, per model, fast mode doubled.
- The top-level `cache` covers every Claude Code call on this Mac: the accounts plus the few calls the collector could not attribute to one.
- One definition for the whole collector: `core.cache_split()`, `core.cache_usd()`, `core.cache_totals()`. `cache_compare_beta.py` uses the same split, so its Claude `missPct` equals `cache.7d.missPct` and `cache.30d.missPct` here.

## Antigravity (Gemini) block (23.11.2026, beta)

- `state.json` has a new top-level `antigravity` object shaped like an entry of `accounts`. The contract is in `ANTIGRAVITY-STATE.md`, and a synthetic sample is in `antigravity-state-sample.json`. `schema` stays 4, and `accounts` did not change.
- `antigravity.py` builds it on every pass. Spend comes from Antigravity's conversation databases (read by `antigravity_beta.py`). The quota percentages come from CodexBar's `history/antigravity.json`, which is imported into `samples` as account `antigravity`. Windows, rates, weeks and value come from `calib.account_state()`, the same function that builds a Claude account. It runs on an in-memory database, so Gemini calls never enter `usage.db`'s `rows`.
- Scan cache: `antigravity-scan.json` (rows per conversation database, keyed on size and modification time of the `.db` and its `-wal`) and `antigravity-scan-calls.json` (the merged, priced calls), both next to `state.json`. Deleting them is safe; the next pass rebuilds them in about 2 s.
- A failure in this step leaves `{"uuid", "name", "short", "beta", "error"}` in the block and the `antigravity` entry of `tick.log` starts with `error:`. The Claude part is written as before.
- Live quota readings: the menu bar app appends each fresh reading to `antigravity-quota.jsonl` next to `state.json`. The collector imports new lines from a saved byte offset as source `live`. In any clock hour that has live readings, CodexBar's hourly sample is dropped, so a reading is never counted twice. Above 1 MB the file is compacted to its last 40 days (format and lock in `ANTIGRAVITY-STATE.md`).
- Self-check: `python3 usage/antigravity.py --check`. Print the block: `python3 usage/antigravity.py`.

## Self-checks

All three run on made-up data in memory, never query a server and never write `state.json` (the Antigravity one also imports new quota readings into `usage.db`, as every pass does, and builds the block from the real data):

- `python3 usage/core.py --check`: the Switcher log lines the collector reads, in both the old Russian and the new English form.
- `python3 usage/poll.py --check`: who may be queried for every active account, VPN state and pass kind (scheduled, `--force`, `--refresh`), with the helper replaced by a stub; the daily compaction.
- `python3 usage/calib.py --check`: weekly rows (no reading, final peak, mid-week reset after a hit, rounding of `minePctByModel`, the current week), and that one failing account or a failing cache step does not stop the summary.
- `python3 usage/antigravity.py --check`: the Antigravity block (see above), including the rate rule against one odd stretch, readings that settle, and that a 1.6-point settling fall is not taken for a reset.
