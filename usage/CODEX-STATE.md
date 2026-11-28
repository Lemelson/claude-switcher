# `state.json` → `codex` block (contract)

Written 25 November 2026 for the Codex page of the CodexBar panel. Producer: `usage/codex.py`, called by `usage/tick.py`. Consumer: the Swift panel (`LemelsonUsageKit`).

**The block is shaped exactly like the `antigravity` block** (`ANTIGRAVITY-STATE.md`): the same keys, value shapes and units, and the same `coverage` / `quotaSource` / `calibration` / `models` / `cache` objects (coverage has its own keys, below). Decode it with the same types (`UsagePanelState.Account` plus the Antigravity extras). Everything not listed here is "same as Antigravity". The shared code is `usage/quota_provider.py`; `antigravity.py` and `codex.py` only read their own call logs and prices.

## Changes

- 2026-11-25, first version.
- 2026-11-25, review fixes. Keys added, none renamed or removed; values change:
  - Copied calls are left out (section Calls). Forked and spawned sessions start with a copy of their parent's history, `token_count` events included, re-stamped with the fork's time. The first version counted them twice: 19 901 calls, 2.50 B tokens, $1 410.53 at list prices over 180 days (all of it July and August 2026), plus 725 M tokens under model `unknown`. New `coverage.copiesDropped`, `coverage.copyTokensDropped`.
  - Readings before the plan change (Pro until 30.10.2026) are not analysed (section Plan start). New `coverage.planSince`, `quotaSource.planSince`. `drift` was all Pro weeks and is empty now; `weeks` keeps Plus weeks only; the forecast bands use Plus windows only.
  - Rates are measured in quota dollars and given in API dollars at the window's mix (section Rates). New `rates.*.quotaUsdPer100`, `rates.*.apiMix`.
  - A stretch never ends at a saturated reading (99.5 % or more; `ANTIGRAVITY-STATE.md`, Rate).
  - Live lines may carry `"account"`; only the current CodexBar account's readings are used. New `quotaSource.account` (a 12-character hash). Section "Live readings file".
  - The scan cache is now `codex-scan.db`, read in bounded steps (section Scan cost). New `coverage.scanComplete`, `coverage.scanPendingFiles`.
  - Price added: `gpt-6.2-astra` (on the pricing page, not yet in the logs).
- 2026-11-25, third review. Keys added, none renamed or removed; values change. Shared with Antigravity (`ANTIGRAVITY-STATE.md`, Changes, third review): forecast after the final rate, `weeks[]` from the windows the readings show (`approx`, `scheduledEnd`), `drift[]` from the rate's estimator (`quotaUsdPer100`, `apiMix`, `confident`), sessions before a saturated reading, `daily[]` from local midnight 179 days back with `daily[].unpriced`, cache shares to 2 decimals, `calibration.account`, `error` = `"codex-failed:<ExceptionType>"`, plan name per account. On 25.11.2026: the three past Plus weeks show their peaks (58, 29, 49 %; they read "no limit reading" before), `drift` has the current week ($64.9 per 100 %), the 5-hour forecast starts at 21 % (9 % read + 12 points spent since), not at 9 %. Codex only:
  - Rates, sessions per week and plan start use the current account's calls only (section Accounts). New `calibration.account`.
  - Plan start is kept per account (kv `codex_plan_since@<account>`).
  - Two plan readings in the same second no longer fail the block (one of them without a weekly start).
  - Scan: a log is read on only when it grew and still begins with what was read (a hash of the 4 KB before the old end); otherwise it is read again in full. A log that cannot be read keeps its last good numbers and counts in `coverage.scanErrorFiles` (new); `scanComplete` is false while any log is pending, stale or unreadable. A log moved to the archive keeps counting under its old path until its new path is read.
  - Prices: `gpt-6.2-astra` cache write $10 / $20 (short / long, was $8 / $16 by rule). Fast long-context calls of `gpt-5.5` and `gpt-5.4` (no published price, priced by rule) make that model's `priceConfirmed` false; new `models.*.estimatedPriceCalls`; `coverage.unconfirmedPrices` lists `"<model> fast long"`.
- 2026-11-27, rolling 24 hours. Keys added, none renamed or removed:
  - `usd24h`, `tokens24h` (number): API-price dollars and tokens of the last 24 hours, rolling (`ts >= now - 86400`, not since local midnight like `usdToday`). Written by `calib.account_state`, so every Claude account, the `codex` block and the `antigravity` block carry them.
  - Top level `claude24h`: `{"usd", "tokens", "unattributedUsd"}` over every row of `usage.db` in the last 24 hours (also calls no account was found for and accounts whose figures failed this time), or `{"error"}`.
  - Top level `spend24h` (written by `tick.py` after both blocks, never raises; on an internal failure `{"error"}` only): `{"usd", "tokens", "asOf", "byProvider": {"claude": {"usd", "tokens", "accounts": [{"uuid", "name", "short", "usd", "tokens"}], "failedAccounts"?, "error"?}, "codex": {"usd", "tokens", "error"?}, "antigravity": {...}}}`. `claude.usd` is `claude24h.usd` (falls back to the sum of accounts); a provider whose block failed adds 0 and carries `error`; a provider without a block is left out. `usd` = the sum of `byProvider`. The CodexBar menu bar shows `usd`.
  - After a Codex account switch Claude Switcher runs `tick.py --switch --provider codex`: only the Codex readings are imported and only `codex` and `spend24h` are rebuilt (no Claude polling or log scan; the rest of `state.json` stays as the last pass wrote it, a full pass when there is none). Both distributed notifications of that switch (`com.lemelson.claudeswitcher.accountDidSwitch`, `…usageRefreshDidFinish`) carry `provider: "codex"` and `uuid` = the newly active account's `accounts[].uuid`.

- 2026-11-28, one block per Codex account. Keys added, none renamed or removed; every existing key keeps its value and meaning (all of this Mac's Codex spend, CodexBar's current account's limits), so Overview and the menu bar's `spend24h` read the same numbers:
  - `accounts` (array): one block per Codex account on this Mac, section "Accounts list".
  - `unattributed` (object): spend of calls no listed account owns, section "Accounts list".
  - `planName` of the `prolite` plan reads `"Pro 5x"` (was `"Pro Lite"`); only an account on that plan sees the change.

## Rules

- `state["codex"]` is one object next to `state["accounts"]` and `state["antigravity"]`. `schema` stays 4.
- Always `beta: true`. Dollars are estimates at OpenAI API list prices; Codex on a ChatGPT plan is not billed per token.
- On failure: `{"uuid", "name", "short", "beta", "error"}` only. The rest of `state.json` is unaffected.
- `uuid` = `"codex"`, `name` = `short` = `"Codex"`.

## Quota attribution

The Codex quota counts all use of the ChatGPT account. The collector estimates **You**, **Others** and **Unknown** from quota readings and the Mac's local logs. The top-level Codex block and every account in `codex.accounts[]` use the same fields:

- `windows[win].split` has `youPct`, `othersPct`, `unknownPct`, `method`, `preliminary` and `reason`. The parts add up to `pct` rounded to one decimal. Methods are `idle`, `calibrated`, `floor`, `mixed` and `unknown`. `preliminary` is true when the rate uses fewer than two clean stretches. `reason` is a short code or null.
- `windows[win].mine` and `outside` mirror `youPct` and `othersPct` when `unknownPct` is zero. Both are null for a partial split.
- `windows[win].series` rows are `[ts, usedPct, mineOrNull, othersOrNull, unknownOrNull]`. The split is computed from all merged readings before thinning. `mineOrNull` is null when the split has an unknown share and the account has no calibrated rate.
- An observed `weeks[]` row with a peak has `minePct`, `othersPct`, `unknownPct` and `splitMethod` at `peakAt`, after any reset inside that week. The same nullable rule applies to `minePct`. `usd`, `tokens` and `m` cover the full weekly row.

The collector checks minute buckets, including unpriced calls such as `codex-auto-review`. Historical logs folded into hourly buckets retain their full hour for the overlap check. The collector needs a complete scan and no ambiguous unlabelled calls in the interval to prove idle. With no local calls since the window started, a complete interval's increase goes to Others. Without a rate, a later idle interval assigns all but one percentage point of its increase to Others. The calibrated calculation uses quota dollars (`qusd`) before the API-price mix, and spends its local budget once in timestamp order. The grace period for a delayed reading is 600 seconds. An unpriced call or coverage gap leaves the unexplained increase in Unknown.

Confirmed redemptions from `snapshot.codexResetCredits.credits` take priority over changes to the scheduled reset time or drops in quota used. Both `redeemedAt` and `redeemed_at` are accepted. The collector remembers redemption times in `usage.db` under `codex_resets@<account hash>`, so removing used credits from a later snapshot preserves the reset boundary.

## Overview timeline

`state.overviewTimeline` belongs to schema 4. It has `version: 1`, Unix-second `asOf`, `quotaWindow: "7d"`, `providers`, `accounts` and `ranges`.

- `providers` is keyed by `claude`, `codex` and `antigravity`. Each entry has `builtAt` and `partial`. A missing provider cache has a null `builtAt` and `partial: true`.
- `accounts` follows the order of Claude's `state.accounts`, then `codex.accounts[]`, then Antigravity. Entries have `key` (`provider:uuid`), `provider`, `uuid`, `short`, `planName`, `active`, `rated`, `usdPer100` and `quotaThrough`. Antigravity's key is `antigravity:antigravity`.
- Each range has `from`, `to`, `stepSec`, `combined` and `accounts`. The grids have 97, 145 and 169 points, with steps of 900, 1800 and 3600 seconds for `24h`, `72h` and `7d` respectively. Index zero starts at zero. All quota series accumulate weekly quota increases across resets.
- Each range's `accounts[key]` contains `quota` rows `[youPct, othersPct, unknownPct, flags]`, cumulative local API dollars in `usdYou`, rated outside use in `usdOthers`, and total `tokensYou`. Outside dollars are `othersPct × usdPer100 / 100`. The whole `usdOthers` field is null if the account is unrated.
- `combined.quota` rows use full weekly account quotas as the unit, so each part sums account points divided by 100. `combined.usdYou` also includes unattributed local calls. `combined.usdOthers` sums rated accounts. `unratedAccounts` lists unrated accounts with nonzero outside or unknown quota. The final values are published as `totalYouUsd`, `totalYouTokens`, `totalOthersUsd`, `totalYouQuota`, `totalOthersQuota` and `totalUnknownQuota`.
- Flags are a bitmask. Bit 1 marks a stale reading or gap, 2 incomplete local coverage, 4 calibration, 8 an unresolved increase straddling `from`, and 16 a saturation or reset tail. An increase straddling `from` is omitted because its in-range portion is unresolved.

The combined timeline counts each Codex account once using `codex.accounts[]`. The collector persists eight days of provider inputs in `usage.db` under `timeline_provider:<provider>`. The Codex switch pass refreshes its own cache and builds the combined view using the existing Claude and Antigravity caches. It atomically replaces `state.json`. Timeline failures are surfaced as `overviewTimeline: {"error": "..."}`.

## Where the numbers come from

| What | Source |
|---|---|
| `windows`, `series`, `forecast`, `rates`, `sessions` | CodexBar's history `~/Library/Application Support/com.steipete.codexbar/history/codex.json`, lanes `session` → `5h`, `weekly` → `7d` (the `monthly` lane is ignored), its preferred account; plus the app's live readings file, below. Imported into `usage.db` `samples` as account `codex`. Readings are whole percents. |
| spend, `daily`, `weeks`, `models`, `cache` | Codex's session logs on this Mac: `~/.codex/sessions/**/*.jsonl` and `*.jsonl.zst`, `~/.codex/archived_sessions/*` (archived sessions are moved there, not copied: no overlap on 25.11.2026). Every Codex client writes there: Codex Desktop, the CLI, the IDE extension, `codex exec`, T3 Code, Claude Code's Codex plugin. |
| `planName` | the owner's file `codex-plan.json`, else the plan name the app writes into its live line (`plan`), else the newest `rate_limits.plan_type` in the session logs (`plus` → `"Plus"`). |
| `planPriceUsd` | the owner's file, else the list price: Plus $20 a month (help.openai.com "What is ChatGPT Plus?", checked 2026-11-25). Other plans have no confirmed list price, so `null` unless set in the file. |
| `trackedSince` | the oldest Codex session file name on this Mac. Spend itself covers the last 180 days only. |
| `quotaSource.planSince`, `coverage.planSince` | start of the current plan, from the plan type in the session logs (section Plan start). |

### Calls

- One call = one `token_count` event whose cumulative `total_token_usage` changed. Codex repeats the same counters with every rate-limit update; repeats are skipped (`coverage.repeatsDropped`). The call's tokens are `last_token_usage`.
- `input_tokens` includes the cached part (OpenAI convention): uncached input = input − cached − cache writes. Reasoning is inside `output_tokens`; `thinking` is the reasoning part.
- Model and effort: the newest `turn_context` before the call (else `thread_settings_applied`). Service tier: the newest `thread_settings_applied` (`priority` = Fast mode).
- A session log that exists twice (moved to the archive, or compressed next to the plain file) is counted once, by session id.
- **Copies are left out.** A forked session (`session_meta.forked_from_id`) or a spawned one (`parent_thread_id`: sub-agents, the approvals reviewer) starts with a copy of its parent's history: `turn_context` and `token_count` events with the parent's counters, all stamped with the new session's time. So the first version's check (timestamp + cumulative counters) found no duplicates although there were 19 901. A call is a copy when its turn was created before the session (session and turn ids are UUIDv7, the creation time is in the id), or, in a forked or spawned session, when no `turn_context` came before it. Checked on 25.11.2026 against matching cumulative counters in the session's ancestors: the rule catches all 19 649 calls found there (2.44 B tokens, $1 179) and 459 more that copy a turn older than the session (the approvals reviewer copies the previous review's last turn; $21); it drops no call with counters of its own. The plan type of a copied call is ignored too. `coverage.copiesDropped` / `copyTokensDropped` count them.
- Calls are kept as one bucket per minute, model, tier, long-context flag and effort; `cache.*.calls` and `coldCallsPct` count calls, not buckets.

### Prices (`codex.PRICES`, $ per 1M tokens, checked 2026-11-25 on https://developers.openai.com/api/docs/pricing)

| Model | Input | Cached | Output | Cache write (30 min) |
|---|---|---|---|---|
| gpt-6.2-sol, gpt-6.1-sol | 2.00 | 0.10 | 10.00 | 2.50 |
| gpt-6.1-terra | 1.00 | 0.05 | 5.00 | 1.25 |
| gpt-6-sol | 2.00 | 0.20 | 10.00 | 2.50 |
| gpt-6-astra | 10.00 | 1.00 | 50.00 | 12.50 |
| gpt-6.2-astra | 8.00 | 0.40 | 40.00 | — (input price) |
| gpt-6-luna | 0.10 | 0.01 | 0.50 | 0.125 |
| gpt-5.6-sol | 4.00 | 0.40 | 20.00 | 5.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 12.00 | 2.50 |
| gpt-5.6-luna | 0.20 | 0.02 | 1.20 | 0.25 |
| gpt-5.5 | 5.00 | 0.50 | 30.00 | — (input price) |
| gpt-5.4 | 2.50 | 0.25 | 15.00 | — (input price) |

- Prompt above 272K tokens: the whole call at the page's long-context prices (`PRICES_LONG`, 2× input and cached input, 1.5× output; re-checked 25.11.2026: "Short context: ≤272K input tokens. Long context: >272K input tokens", every value in the table matches). The page does not say whether only the part above 272K is long; the whole call is assumed.
- Fast mode: 2× (gpt-5.5: 2.5×), as the page's Fast table; long context in Fast mode is not listed, long × the same factor.
- No price: `codex-auto-review` (the approvals reviewer), `gpt-5.5-codex`, an unknown model. They add tokens only, `usd` 0, counted in `models.*.unpricedCalls` and `coverage.unpricedCalls30d`. Their use still moves the quota, so the rates are slightly too high per dollar.
- Today's prices are used for every date (no history of OpenAI prices is kept).
- Codex logs never had a cache write above 0 on this Mac; the column is there for completeness.

## Rates: quota dollars and the API mix

The quota does not count the long-context premium. On 25.11.2026 the three clean 5-hour stretches of the day cost $9.5, $9.8 and $9.7 per 100 % when long-context calls are priced like short ones, but $11.2, $9.8 and $15.9 at API prices (the last one was 77 % long-context dollars and was rejected as "low"). So:

- Each call also has quota dollars: the same call at short-context prices (Fast mode keeps its factor). Stretches, `rates`, `sinceMeasure`, sessions and value are measured in them.
- Each rate is then given in API dollars at the API-to-quota mix of its own window (from the window's start to now; with less than $0.50 in it, the last 7 days): `usd_per_100 = quotaUsdPer100 × apiMix`, `pct_per_usd` the same way, `range` too. So "100 % ≈ $X at API prices" and "this window ≈ $Y at Z %" agree: on 25.11.2026 12:26 the 5-hour rate is $13.25 (quota $9.63 × mix 1.376), the window $11.93 at 90 %.
- `value.byModel[].monthUsd` uses that model's own mix over 30 days. `rates.*.usd` (spend in the clean stretches) is in quota dollars.
- Spend everywhere else (`daily`, totals, `models`, `windows.*.usd`) stays at API prices.

## Plan start

Readings of an earlier plan measure another limit (Pro until 30.10.2026: its primary window was the week, `usd_per_100` about $3 600). The plan type comes with every `token_count` (`rate_limits.plan_type`). `planSince` = the first reading of the current plan after the last reading of another plan, moved back to the start of its weekly window when that lies after the other plan's last reading: 1793375297 = 30.10.2026 15:48:17 UTC on this Mac. It is kept in `kv` (`codex_plan_since`) so the cut stays when the old logs age out; a later change seen in the logs replaces it.

From `planSince` on only: the readings copied into the analysis (forecast bands, `drift`, the rates' and sessions' 35 days, the windows' start points) and `weeks` (a week that began before it is dropped). Spend is not cut. `null` when the logs show no earlier plan.

## Accounts

The session logs name the ChatGPT account of each session (`session_meta.creator_account_id`, the id in CodexBar's account key `codex:v1:provider-account:<id>`; logs older than 12.10.2026 name none). The collector keeps a hash of the current account's id (kv `codex_account_id`) and measures the limit with that account's calls only: rates, `sinceMeasure`, forecasts and drift. Calls of logs that name no account count too while no other account shows up in the logs; with the current account unknown and two or more accounts in the logs there is no rate rather than a mixed one (`calibration.account.filter`: `"account"`, `"all"` or `"ambiguous"`). The spend shown (`usd*`, `daily`, `weeks`, `windows.*.usd`, `models`) stays all of this Mac's Codex calls. The plan seen in the logs and the plan start come from the current account's logs the same way.

## Differences in thresholds

Everything in Rate, Sessions per week and Value is the same method as Antigravity (`ANTIGRAVITY-STATE.md`). Only the dollar sizes differ, because Plus limits are about ten times smaller (on 25.11.2026: 100 % of a week ≈ $66 of this Mac's API-price spend, a 5-hour window ≈ $9–13):

| | Antigravity | Codex |
|---|---|---|
| `calibration.thresholds.stretchUsd` | `{"5h": 15, "7d": 60}` | `{"5h": 3, "7d": 10}` |
| `calibration.thresholds.confidentUsd` | 240 | 40 |

`rates.*.method` and `value.reason` name these sizes ("$10+ of this Mac's Codex spend", "about $10 of use"). Jitter (2 points), the reset rule (fall of more than 4 points) and the session rule are the same.

## `coverage` (own keys)

```jsonc
"coverage": {
  "tokens": "exact", "usd": "estimate",
  "historySince": "2026-05-30",        // first local day with calls in the last 180 days
  "sessionFiles": 4523, "compressedFiles": 4491, "archivedFiles": 276,
  "duplicateFilesDropped": 0,          // the same session id in two files: counted once
  "calls": 218688,                     // calls in the last 180 days
  "repeatsDropped": 8537,              // repeated cumulative token_count events
  "copiesDropped": 19901, "copyTokensDropped": 2499454902,   // calls copied from a parent session (section Calls)
  "planSince": 1793375297,             // start of the current plan, or null (section Plan start)
  "unreadableFiles": 0, "malformedLines": 0,
  "unpricedCalls30d": 396, "unconfirmedPrices": [],
  "fastCalls30d": 0, "longContextCalls30d": 249,
  "sessionsByClient30d": {"Codex Desktop": 52, "T3 Code": 15, "codex_exec": 2},   // session files with calls in 30 days, by originator
  "accounts30d": 1,                    // ChatGPT accounts in those sessions
  "planSeen": "plus", "planSeenAt": 1795599641,   // newest rate_limits.plan_type in the logs
  "pricesChecked": "2026-11-25", "priceSource": "https://developers.openai.com/api/docs/pricing",
  "priceTier": "Standard; Fast-mode calls at Fast prices; prompts above 272K tokens at long-context prices",
  "notCounted": "Codex cloud tasks, code review on GitHub, ChatGPT chat, other computers, calls older than 180 days",
  "scanCached": true, "scanRescannedFiles": 0, "scanMs": 94,
  "scanComplete": true, "scanPendingFiles": 0   // false / > 0 while logs wait for a later pass (section Scan cost)
}
```

`quotaSource` has the same keys as Antigravity's; `file` is `history/codex.json`, `liveFile` is `codex-quota.jsonl`, `weekly` describes the ChatGPT account's Codex quota.

`models` keys are OpenAI model ids as recorded (`"gpt-6.2-sol"`); so are the keys of `daily[].m`, `weeks[].m` and `value.byModel[].model`. `value.byModel[].label` (`"gpt-6.2-sol"` → `"Sol 6.2"`, `"gpt-5.5"` → `"GPT-5.5"`, anything else as recorded) is for the command line only: the panel names every model from its raw id with its own naming (`UsageModelNaming`), so one model has one name in Spend, Value and Prompt cache. `efforts` counts calls per effort (`"High"`, `"Medium"`, `"Low"`, `"Xhigh"`).

## Live readings file (written by the app, phase 2)

`~/Library/Application Support/ClaudeSwitcher/usage/codex-quota.jsonl` (`core.DATA + "/codex-quota.jsonl"`), lock file `codex-quota.jsonl.lock`. **Same format, lock, compaction and merge rule as `antigravity-quota.jsonl`** (`ANTIGRAVITY-STATE.md`, "Live readings file"): `"5h"` = Codex's primary (session) window, `"7d"` = the secondary (weekly) window, `pct` = used percent, optional `plan` = the plan name CodexBar shows, optional `account` = CodexBar's `preferredAccountKey` of `history/codex.json` for the account the reading is of (`codex:v1:provider-account:…` or `codex:v1:email-hash:…`, never an e-mail). Only the current account's readings are used, and only its history is imported (rule: `ANTIGRAVITY-STATE.md`, "Account"); `quotaSource.account` is its 12-character hash. The collector handles the file being absent (`quotaSource.liveFileExists: false`, `codexQuota.live: "absent"` in `tick.log`).

## Scan cost

Cache: `codex-scan.db` (SQLite) next to `state.json`. One row per log (signature = size, mtime, inode; reader version; summary; state with its buckets) plus the merged buckets of all sessions. A warm pass stats the logs and reads the signatures and the merged buckets only; a changed log's old buckets leave the merge and its new ones go in. A growing plain log is read on from its saved offset, a changed `.zst` again in full. Merged buckets older than 40 days are kept per clock hour (3 900 instead of 46 800 on 25.11.2026). Deleting the cache is safe.

- Reading is bounded: at most 4 s of log reading per pass (at least one log). New and changed logs come first, newest first; then logs read by an older reader version (their old numbers count until then); then logs that failed, again after an hour. `coverage.scanComplete` is false and `coverage.scanPendingFiles` counts what is left while a pass could not read everything. A full read (first run, deleted cache, new reader version) is about 18 GB and 60 s of reading in all, spread over the passes.
- `zstd`: Python 3.14's `compression.zstd`; without it the `zstd` CLI, whose process is always waited for, and whose failure makes the log an error entry (tried again later) instead of an empty log.
- The first cache `codex-scan.json` (reader 1) is taken over once and removed.
- On 25.11.2026: warm Codex part of a scheduled tick 0.73 s (was 3.1–4.2 s), 0.15 s run by hand (was 0.6 s).

## Sample

`usage/codex-state-sample.json`: the real block of 25.11.2026 with its long lists cut short (`python3 usage/codex.py --sample`).

## Accounts list (`accounts`, `unattributed`)

Producer: `usage/codex_accounts.py`, called at the end of `codex.build()`. A failure there gives `accounts: []` and `unattributed: {"error"}` and leaves the rest of the block as it is; a failure of one account gives that entry `{"uuid", "name", "short", "active", "beta", "error"}` only.

### Which accounts

Read-only, never a token copied or refreshed:

| Source | What is read |
|---|---|
| `~/.codex/auth.json` (the live system account) | `tokens.account_id`; the id_token's claims: `email`, `https://api.openai.com/auth` → `chatgpt_account_id`, `chatgpt_plan_type` |
| `~/Library/Application Support/CodexBar/managed-codex-accounts.json` (version 3) | `id`, `email`, `workspaceAccountID` (else `providerAccountID`: CodexBar's `effectiveWorkspaceAccountID`), `managedHomePath`; the plan claim of that home's `auth.json`, only for a home inside `CodexBar/managed-codex-homes/` |

- One entry per normalized (lowercase) ChatGPT workspace account id. A record known only by e-mail joins the one entry with that e-mail; when no entry or more than one (two workspaces of one login) has it, it stays an entry of its own. The live account kept by CodexBar as a managed one too is one entry with both sources, and stays `active` wherever the live record lands.
- Order: the order of the ids in `account-names.json` (the file Claude accounts use too), then by e-mail.
- No account on the Mac: `accounts` is `[]` (the switcher hides its Codex section).

### One entry

The same shape as the block itself (`UsagePanelState.Account` plus the provider extras: `models`, `cache`, `coverage`, `quotaSource`, `calibration`, `valueReason`, ...), built by the same `quota_provider.build_common`, so the panel can render an entry with the components it uses for the block. What differs:

```jsonc
{
  "uuid": "<lowercase ChatGPT workspace account id>",   // no id known: "email-hash:<first 12 hex of sha256(lowercase e-mail)>", never ""
  "name": "<e-mail>",
  "short": "<account-names.json[uuid], else the e-mail>",
  "active": true,                      // uuid is the account of ~/.codex/auth.json
  "codexbarCurrent": true,             // CodexBar's preferred account in history/codex.json (whose limits the block shows)
  "managed": false,                    // CodexBar keeps it in managed-codex-accounts.json
  "planName": "Plus", "planPriceUsd": 20.0,
  "planSource": "snapshot",            // "file" | "snapshot" | "live" | "auth" | "logs" | null
  "has5h": true,                       // false: the account reports a weekly window only (5h row hidden)
  "lastMeasuredAt": 1795830878,        // newest reading of this account (Unix seconds)
  "snapshotAt": 1795830878,            // updatedAt of its codex-account-snapshots.json record (only with a record)
  "windows": {"5h": {...}, "7d": {...}},   // same keys as the block's windows, from this account's readings only
  "credits": {                         // null without a snapshot record holding credit data
    "remaining": 57469.46,             // credits balance, null when CodexBar could not read it
    "note": null,                      // "balance not read" | "workspace balance" | null
    "resetCredits": [1796066337.09],   // expiry of each free rate-limit reset that is available and not expired, soonest first
    "updatedAt": 1795830888
  },
  "usdToday": 2.08, "usd24h": 21.27, "tokens24h": 125704309, "usd7d": 91.47, "usd30d": 115.99, "...": "...",
  "daily": [...], "weeks": [...], "models": {...}, "cache": {...}, "rates": {...}, "drift": [...], "value": {...},
  "trackedSince": 1792000000,          // first call of this account in the logs, or null
  "coverage": {"tokens", "usd", "historySince", "calls", "unpricedCalls30d", "unconfirmedPrices", "fastCalls30d",
               "longContextCalls30d", "planSeen", "planSeenAt", "planSince", "pricesChecked", "priceSource",
               "attribution", "notCounted"}
}
```

### Readings of one account

Merged into an in-memory database for that account (usage.db is only read), each reading once (`samples` key: window, second, source):

1. usage.db `samples` of this CodexBar account: `codex` when it is CodexBar's current account, else the parked `codex:<hash of its key>` (`quota_provider.use_account`).
2. Its series in CodexBar's `history/codex.json` (key `codex:v1:provider-account:<id>`, lanes `session` → `5h`, `weekly` → `7d`).
3. Its lines in `codex-quota.jsonl` (`account` = that key; lines without `account` belong to the account they were first taken for, kv `codex_account_legacy`).
4. Its record in `codex-account-snapshots.json` (matched by `accountIdentity.workspaceAccountID`; by normalized e-mail only from a record with no workspace id (or, for an account with no id, one no listed account owns), only for an e-mail no other listed account has, and only when exactly one record matches: two workspaces of one login never share a record): `snapshot.primary` / `secondary` / `tertiary` by `windowMinutes` (300 → `5h`, 10080 → `7d`), stamped `snapshot.updatedAt`, source `live` (the same reading as its live line when CodexBar wrote one).

**CodexBar's JSON dates are Apple reference dates** (seconds since 2001-01-01): `updatedAt`, `resetsAt`, `expires_at`, `observedAt` + 978 307 200 = Unix seconds (`codex_accounts.APPLE_EPOCH`).

A weekly-only account: when its snapshot reports a weekly window and no 5-hour one, and no 5-hour reading is newer than the snapshot, `windows` has no `5h` and `has5h` is false (an older 5-hour reading is stale, not current).

### Plan of one account

`codex-plan.json` may hold `"accounts": {"<account id or e-mail>": {"planName", "planPriceUsd"}}`; such an entry wins for that account. The file's top-level fields describe CodexBar's current account only (as before). Otherwise, first found: the snapshot's `planObservation.rawPlan` (else `loginMethod`), the plan of the account's newest live line, the auth.json `chatgpt_plan_type` claim, the newest `rate_limits.plan_type` of the account's own logs. Raw plans are named by `codex.PLAN_NAMES` (`plus` → Plus, `prolite` → Pro 5x, `pro` → Pro, ...). Price: the file's, else the list price (Plus $20), else null. Plan start: kv `codex_plan_since@<hash of the account id>`, moved on when the account's own logs show a later change.

### Spend: who owns a call

- A call belongs to the account its session log names (`session_meta.creator_account_id`, compared as `account_hash` of the lowercase id). `accounts[i]` sums only those calls: spend, `daily`, `weeks`, `models`, `cache`, and the calls its rates, sessions and value are measured with.
- Logs older than 12.10.2026 name no account, and a log may name an account no longer on this Mac. Those calls belong to no entry. They are summed in `unattributed`:

```jsonc
"unattributed": {"usdToday", "usd24h", "usd7d", "usd30d", "usd180d", "tokens24h", "tokens30d", "tokens180d",
                 "calls", "noAccountCalls", "otherAccountCalls", "firstAt", "lastAt", "note"}
```

- No call is counted twice: for every span, the sum over `accounts` plus `unattributed` equals the block's own total (`usd7d`, `usd30d`, `usd24h`, ...), which stays all of this Mac's Codex calls. Checked by `codex_check.py` on the real data.
- On 28.11.2026: two accounts (Plus, live; Pro 5x, managed, not yet used on this Mac and with no CodexBar reading yet), `unattributed` = 189 696 calls, $18 016 at list prices, all before 12.10.2026.

`python3 usage/codex_accounts.py` lists the accounts found (display name, e-mail, id, managed id, plan claim) without building anything; `ClaudeSwitcher codex-list` does the same from the switcher.
