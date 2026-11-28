# `state.json` → `antigravity` block (contract)

Written 23 November 2026 for the Antigravity (Gemini) page of the CodexBar panel. Producer: `usage/antigravity.py`, called by `usage/tick.py`. Consumer: the Swift panel (`LemelsonUsageKit`).

## Changes

- 2026-11-23, first version.
- 2026-11-23, same day, additions only (no key renamed or removed):
  - `windows.*.series` always starts with `[start, 0, null]` once the window has been used: Antigravity starts a window with the first call, so 0 % at the start is an exact point (it is also used by the rate). Before, it was added only when the first sample was close to the start.
  - `coverage` gained `unreadableDbs`, `malformedRows`, `scanRescannedFiles`.
  - each `models` entry gained `unpricedCalls`; `quotaSource` gained `accountsInFile`.
- 2026-11-23, live readings, additions only: a second quota source, the app's live readings file (section "Live readings file (written by the app)"). Its samples have source `"live"`. Windows, series and rates now use both sources merged (rule below). `windows.*.source` is `"live"` or `"codexbar"`, whichever gave the newest sample. `quotaSource` gained `bySource`, `lastSource`, `liveFile`, `liveFileExists`, `liveBadLines` and `liveCompactedAt`. `quotaSource.samples` now counts the samples of both sources before merging (it is still never counting the synthetic start points).
  - the `valueReason` wording is fixed (see Value).
- 2026-11-23, rates, sessions, value, cache. Additions, plus one change of method:
  - `rates.*` are now measured from disjoint stretches with polluted ones dropped (section Rate), not from calib.py's envelope over overlapping pairs. So `rates.*.pairs` now counts clean stretches (today 1 where it said 15). New keys `method`, `pairsUsed`, `pairsDropped`, `range`, `usd`. `windows.*.sinceMeasure` uses the new rate.
  - `windows["7d"].sessions` (`{perWeek, pairs}` or null) and top-level `sessionPctOfWeek` (the name the Swift `Account` already decodes).
  - `value` gained `planName`, `planPriceUsd`, `weekUsd`, `monthUsd`, `timesPrice`, `confident`, `reason`, `byModel`. `weekOpusUsd`, `monthOpusUsd` and `pairs` stay.
  - `planName` / `planPriceUsd`: the owner's plan file, else the plan name the app writes into its live readings and its list price (section Plan).
  - each `cache.*` gained `readPct`, `readTokens`, `missTokens`, `writeTokens`, `savedUsd`, `missUsd`.
  - new key `calibration`: counts behind the rate and the sessions, and the thresholds.
- 2026-11-23, review the same evening. One key added (`rates.*.pairsLow`), none renamed or removed; values change:
  - Jitter allowance 1 → 2 points (`calibration.thresholds.jitterPts`): readings settle by up to 2.0 (5-hour) and 1.6 (weekly) points after use.
  - `upper` is now the lowest cap that another stretch agrees with, not the lowest cap of all (section Rate). One odd stretch can no longer set the rate alone; such a stretch is counted in the new `pairsLow`.
  - `rates.*.method` names the window's own stretch size ($60+ weekly, $15+ 5-hour).
  - Sessions need the week to move 3+ points per interval (was 1.5) and 12 points in all (was 8), and skip an interval with a fall of more than 4 points inside.
  - A fall counts as an unscheduled reset only above 4 points here (Claude accounts keep 1.5). With 1.5, a reading that settled 1.6 points in one step would have moved `windows.*.start` to that moment.
  - `weeks[]` rows carry `peakAt`, `peakFinal` and `resetAt` like Claude's (`README.md`, Weekly windows).
  - `quotaSource.weekly` describes the Gemini group's quota.
- 2026-11-25, no change in the output: the quota, rate, sessions and value code moved to `usage/quota_provider.py`, shared with the Codex block (`CODEX-STATE.md`). The block built from the same data before and after is identical.
- 2026-11-25, review fixes, additions only (no key renamed or removed):
  - Live lines may carry `"account"` (section "Live readings file", "Account"). Only the current CodexBar account's readings are kept; `quotaSource.account` (new) names that account by a 12-character hash.
  - `quotaSource.planSince` (new) is always `null` here: the cut by plan start is used by Codex only.
  - A stretch never ends at a reading of 99.5 % or more (section Rate). On 25.11.2026 this changed no Antigravity number.
- 2026-11-25, third review (shared code, both blocks). Keys added, none renamed or removed; values change:
  - `windows.*.forecast` is built after the final rate, from `pct + sinceMeasure` (before: from calib's own rate, so `forecast.pct` could sit below the reading plus the spend after it). `forecast.pct` = `min(100, pct + sinceMeasure)`.
  - `weeks[]` rows follow the weekly windows the readings show (grouped by reset time, 0 % readings left out), not steps of 7 days back from the latest reset. A window reset before its time ends where the next one began: `end` is that moment and new `scheduledEnd` the reset it was due at (else null). A whole week without readings but with spend is a row with new `approx: true` (boundaries guessed in 7-day steps; `peakPct` null). Every row has `approx` (false or true). On 25.11.2026 the Antigravity rows are 23.11–30.11 (current), 21.11–23.11 (ended early, peak 42.77 %), 14.11–21.11 (approx), then August.
  - `drift[]` uses the rate's own estimator per weekly window (disjoint stretches, `rates`), not calib's envelope: `pairs` counts clean stretches (was overlapping pairs, 17 097). New `quotaUsdPer100`, `apiMix` (1 here) and `confident` (same rule as `value.confident`).
  - Sessions per week: an interval ends at the last pair of readings before either window reads 99.5 % or more.
  - `daily[]` covers whole calendar days: the first day is local midnight 179 days before today (180 days with today). New `daily[].unpriced` `{model: tokens}`, only on days with calls that have no price (their dollars are 0, not "below a cent").
  - `cache.*.readPct` / `missPct` are computed from the integer token counts and rounded to 2 decimals (93.49, was 93.5).
  - `error` is a fixed code `"antigravity-failed:<ExceptionType>"`, never the exception's text (which can hold a file path).
  - `calibration.account` (new): `{filter, otherAccountCalls, unlabelledCalls, unlabelledUsed}`, which calls measured the rates. Here always `"all"` (Gemini's logs name no account).
  - Plan name (`planName`) is kept per account like the readings: on an account change the old one's plan is parked and the new one's comes back (or none until the app sends it). While no CodexBar history names the account, live lines that name one are kept apart per account, and the account of the newest such line becomes the current one.

## Rules

- `state["antigravity"]` is ONE object next to `state["accounts"]`. `schema` stays 4 and nothing in `accounts` changes. An older reader ignores the block.
- It is shaped like an entry of `state["accounts"]` (`UsagePanelState.Account`): the same key names, the same value shapes, the same units (epoch seconds, percent 0–100, API dollars). So `JSONDecoder().decode(UsagePanelState.Account.self, …)` works on it as is, and the Claude views for windows, rates, daily spend, weeks and value can be reused.
- Keys added only here: `beta`, `models`, `cache`, `coverage`, `quotaSource`, `calibration`, `valueReason`, `error`.
- The block is always `beta: true`. Dollars are never measured: they are an estimate at Gemini API list prices. Show the "estimate" label wherever a dollar figure appears.
- If the Antigravity step fails, the block is `{"uuid","name","short","beta","error"}` (no other keys), or the key is absent if the collector never got that far. The rest of `state.json` is unaffected.

## Keys shared with a Claude account

| Key | Shape | Meaning here | Exact / estimate |
|---|---|---|---|
| `uuid` | string | always `"antigravity"` | — |
| `name`, `short` | string | always `"Antigravity"` | — |
| `active` | bool | always `false` | — |
| `useHapp` | bool | always `false` (no VPN rule) | — |
| `planName` | string \| null | Google AI plan name: the owner's plan file, else the name the app saw (section Plan), else `null` | as entered or as fetched |
| `planPriceUsd` | number \| null | monthly price in $: the owner's plan file, else the list price of that name, else `null` | as entered or list price |
| `has5h` | bool \| null | `true` when CodexBar has recorded a 5-hour window | exact |
| `sessionPctOfWeek` | number \| null | one full 5-hour window as a percent of the weekly limit, `100 / windows["7d"].sessions.perWeek`; null while `sessions` is null | estimate |
| `lastMeasuredAt` | epoch \| null | time of the newest CodexBar quota sample (either window) | exact |
| `lastPoll` | null | the collector does not poll Google itself | — |
| `trackedSince` | epoch \| null | first Antigravity call in the local conversation databases | exact |
| `kFable` | number | always `1.0`: Gemini dollars are not weighted, so "weighted spend" = plain spend | — |
| `windows` | `{"5h": Window, "7d": Window}` | quota windows, see below; a window is absent if CodexBar never recorded it | — |
| `rates` | `{"5h": Rate, "7d": Rate}` | dollars at API prices per 1 % of the limit; a key is absent until that window has one clean stretch | estimate |
| `drift` | `[DriftPoint]` | the 7d rate per past weekly window (≥ 150 pairs each), same as Claude | estimate |
| `daily` | `[Day]` | 180 days, local dates | tokens exact, dollars estimate |
| `usdToday`, `usd24h`, `usd7d`, `usd30d` | number | API-price dollars since local midnight / last rolling 24 h / last 7×24 h / last 30×24 h | estimate |
| `tokensToday`, `tokens24h`, `tokens7d`, `tokens30d` | number | input + cached + cache write + output (thinking is inside output) | exact |
| `weeks` | `[Week]` | spend per weekly quota window, boundaries from the 7d `resetsAt` | tokens exact, dollars estimate |
| `value` | Value \| null | what 100 % of the weekly limit is worth at API prices | estimate |

Claude-specific fields omitted for Antigravity include `windows.fable`, `windows.*.server` and `weeks[].minePctByModel`.

### Window (`windows["5h"]`, `windows["7d"]`)

| Key | Meaning | Exact / estimate |
|---|---|---|
| `pct` | used percent from the newest CodexBar sample (a float, e.g. `40.83`), `0` if the window has rolled | exact (as CodexBar read it) |
| `measuredAt` | time of that sample | exact |
| `resetsAt` | reset time from that sample, `null` if rolled | exact |
| `source` | `"live"` (the app's live readings file) or `"codexbar"` (CodexBar's hourly history): where the newest sample came from | — |
| `rolled` | the sample's reset time has passed: the window restarted and no newer sample exists | exact |
| `start` | window start = `resetsAt` − window length (or just after an unscheduled reset) | exact |
| `usd` | `{"opus": 0, "fable": 0, "other": N}`: API-price dollars of Antigravity calls since `start`; all Gemini dollars go to `other` | estimate |
| `sinceMeasure` | percent the calls after `measuredAt` would add, `rate × dollars`; present only when the window has a rate | estimate |
| `split` | `{youPct, othersPct, unknownPct, method, preliminary, reason}` at `measuredAt`. The three parts add up to `pct` rounded to one decimal | attribution |
| `mine`, `outside` | `split.youPct` and `split.othersPct` when `unknownPct` is zero, otherwise both null | attribution |
| `series` | `[[ts, usedPct, mineOrNull, othersOrNull, unknownOrNull], …]`, at most 96 rows. When unrated with a positive unknown share, `mineOrNull` is null | readings and attribution |
| `forecast` | same shape and method as Claude (`at`, `pct`, `ratePerHour`, `endPct`, `endLo`, `endHi`, `hitAt`, `hitEarly`, `hitLate`, `checks`, `bandSource`, `bandWindow`). `bandSource` is `"own"` when Antigravity has ≥ 30 checks on its own past windows, otherwise `"default"` (never pooled with Claude accounts) | estimate |
| `sessions` | `windows["7d"]` only, always present there: `{"perWeek": 5.8, "pairs": 4}` or `null`. How many full 5-hour windows one weekly window holds, measured from the readings (below). The page turns it into "≈N full 5h sessions left · M fit before reset" | estimate |

Present only when the window has not rolled: `series`, `forecast` (the forecast also needs a sample from the last 30 minutes).

### You, Others and Unknown

The split uses all merged readings before thinning. Within each continuous quota window, a reverse running minimum removes short-lived upward reading jitter from the increase calculation. The card displays the raw latest reading. A change in reset time of more than 300 seconds for `7d` or 120 seconds for `5h`, or a drop of more than four percentage points, starts a new segment. Reset times on zero-percent readings can move without starting a new segment.

The method is `idle` when complete logs prove no calls since the window started, `calibrated` for a full split using a rate, `mixed` when a calibrated split leaves an unknown share, `floor` for a positive lower bound on Others without a rate, or `unknown`. A rate measured from one clean stretch sets `preliminary: true`. `reason` is null or a code such as `unpriced_calls` or `uncovered`.

A subsequent idle interval without calibration assigns the increase minus two points to Others, floored at zero. A calibrated split uses local API-price spend once, in timestamp order, and permits a 600-second reporting lag. A recent encrypted conversation makes its overlapping interval uncovered. The check uses each `.pb` file's modification time from 600 seconds before the preceding reading through 60 seconds after the next reading.

Observed `weeks[]` with a peak have `minePct`, `othersPct`, `unknownPct` and `splitMethod` from the peak after any mid-week reset. `minePct` is null when the rate is missing and some quota is unknown. Weekly `usd`, `tokens` and `m` remain totals for the full weekly row.

### Overview timeline

Schema 4 also publishes the top-level `overviewTimeline` with rolling `24h`, `72h` and `7d` ranges. The single Antigravity entry uses the key `antigravity:antigravity`. It contributes local API spend and measured weekly quota increases even before a rate is available. The collector stores its last eight days of inputs under `timeline_provider:antigravity` in `usage.db` for reuse during Codex account switches. [See the full overview timeline format](CODEX-STATE.md#overview-timeline).

### Sessions per week (`windows["7d"].sessions`)

Both percentages count the same use, wherever it happens (this Mac, the chat app, a phone). So how far the weekly limit moves while the 5-hour limit moves does not depend on where the use came from, and no dollars are involved.

- For every pair (5-hour window, weekly window) take the first and the last reading that lie inside both. That is one interval.
- An interval counts when the 5-hour percentage moved at least 5 points and the weekly one at least 3 points. A reading can be up to 2 points off (it settles after use), so smaller moves are mostly noise.
- An interval in which either percentage falls by more than 4 points between two readings is skipped: a reset that keeps its reset time, or a switch of quota group, would make "last minus first" wrong. (A scheduled reset needs no rule: a new reset time is a new window.)
- `perWeek` = sum of the 5-hour moves / sum of the weekly moves, one decimal. A full 5-hour window is 100 points, so it costs `100 × weekly move / 5-hour move` points of the week.
- `pairs` = intervals used. Readings of the last 35 days only, like the rates.
- **null until** there are at least 3 such intervals with at least 12 weekly points in all, so the jitter of one interval moves the answer by a sixth at most. Until then `calibration.sessionPerWeekSoFar` holds the number so far, for debugging only.
- Caveat: both figures come from the same Gemini quota group in the app's live readings (titles "Gemini 5-hour" and "Gemini weekly"). Older entries of CodexBar's hourly history may hold the highest weekly quota of any group, so an interval built from those can mix groups.

### Rate (`rates["5h"]`, `rates["7d"]`)

```json
{"pct_per_usd": 0.1752, "usd_per_100": 570.9, "pairs": 1, "source": "own",
 "method": "From stretches with $60+ of this Mac's Gemini spend between two readings; a stretch that moved the limit more than that spend explains (phone, chat app), or much less than the others agree on, is left out.",
 "pairsUsed": 1, "pairsDropped": 0, "pairsLow": 0, "range": [571, 571], "usd": 229.52}
```

`method` names the window's stretch size: `$60+` in `rates["7d"]`, `$15+` in `rates["5h"]`. `pairsUsed + pairsDropped + pairsLow` = all stretches of that window (`calibration.stretches`).

`usd_per_100` = dollars at API prices that fill 100 % of the window. Estimate. The key is absent until there is one clean stretch.

How it is measured (`estimate()` in `antigravity.py`):

1. **Stretches.** Walk the readings of each window, including the exact 0 % at its start, and close a stretch at the first reading with at least $60 (weekly) / $15 (5-hour) of this Mac's spend since the stretch began. Stretches don't overlap, so one stretch of use is counted once. (calib.py's pairs overlap: on 23.11 its "15 pairs" were one stretch counted 15 times.) A window's walk stops at its first reading of 99.5 % or more (added 2026-11-25): at the top the reading cannot move any more while the spend can, so a stretch ending there would show too few points per dollar.
2. **Bound.** Use elsewhere only ever adds to how far the limit moved. So each stretch caps the true rate at `(moved + 2 points) / dollars`, and `(moved − 2) / dollars` is its floor; the 2 points allow for a reading that is off (it jumps, or it was taken during use and settles later: on 23.11 by 2.0 points on the 5-hour limit and 1.6 on the weekly one). Two stretches **agree** when their floor-to-cap ranges overlap. `upper` is the lowest cap that at least one other stretch agrees with (from 67 stretches on, 3 % of them, as in calib.py). With one stretch, or none agreeing, it is the lowest cap.
3. **Clean, polluted or low.** A stretch is clean when its range holds `upper`: this Mac's spend explains it. A stretch whose floor is above `upper` moved the limit more than this Mac's spend explains, which means use in the chat app or on a phone: dropped, counted in `pairsDropped`. A stretch whose cap is below `upper` moved the limit much less than the others agree on (a reading off by more than the jitter, calls dated wrongly, spend the shown quota does not count): set aside, counted in `pairsLow`.
4. **Rate.** The pooled rate of the clean stretches, `sum(moved) / sum(dollars)`, capped at `upper`.

In two sentences, as the page can say it: each stretch says "the limit cannot cost more than this per dollar", and the lowest such ceiling that a second stretch agrees with is the bound. The rate pools the stretches that fit under it; a stretch above it had outside use, one far below it is set aside.

Why "agrees with": the lowest cap alone lets one odd stretch decide everything. In the self-check, five clean stretches at $500 per 100 % plus one $60 stretch that moved the limit 2 points gave $3000 under the old rule (the five clean ones dropped as "polluted"); the new rule gives $498 and counts the odd one in `pairsLow`. Two stretches that do not agree keep the lower cap, since which one is wrong cannot be told; `value.confident` stays false then anyway.

What that means in practice:
- A polluted stretch changes nothing, and neither does one low stretch while two others agree.
- Every new clean stretch narrows the jitter error, because the shared readings cancel inside the sum.
- `range` = `[lowest, highest]` dollars per 100 % among the clean stretches. `usd` = this Mac's dollars in them. `pairs` = `pairsUsed`.
- What it cannot see: use elsewhere that happens in every stretch at the same pace looks like local use and makes the limit look smaller. With `pairsUsed` 1 the figure is a floor ("at least this much"), not a measurement.

### Day, Week

As for Claude: `Day = {"day": "YYYY-MM-DD", "opus": 0, "fable": 0, "other": usd, "tokens": n, "m": {model: [usd, tokens]}}`, `Week = {"start", "end", "current", "usd": {"opus": 0, "fable": 0, "other": usd}, "tokens", "peakPct", "peakAt", "peakFinal", "resetAt", "m"}` (meanings in `README.md`, Weekly windows; a reset here needs a fall of more than 4 points, not 1.5). `peakPct` is the highest reading, so it can sit up to about 2 points above where the week settled.
Difference: the keys of `m` are Gemini model ids as priced (`"gemini-4-neon"`, `"gemini-4-carbon"`, `"gemini-3.8-flash"`, …), not Claude families. A call whose model has no Gemini price is listed under its recorded id with `usd` 0.

### Value

The Value page: "You pay $X → You get ≈ $Y, ×Z", monthly, plus one line per model. `null` until `rates["7d"]` exists.

```json
"value": {"planName": null, "planPriceUsd": null, "weekUsd": 571, "monthUsd": 2483, "timesPrice": null,
          "pairs": 1, "confident": false,
          "reason": "Measured 1 of 4 times so far. Each measurement takes about $60 of use on this Mac.",
          "byModel": [{"model": "gemini-4-neon", "label": "Neon", "monthUsd": 2483, "timesPrice": null, "pairs": 1},
                      {"model": "gemini-4-carbon", "label": "Carbon", "monthUsd": null, "timesPrice": null, "pairs": 0}],
          "weekOpusUsd": 571, "monthOpusUsd": 2483}
```

- `weekUsd`: 100 % of the weekly limit ≈ N dollars at Gemini API prices = `rates["7d"].usd_per_100`, rounded. `monthUsd` = `weekUsd × 30.44 / 7`.
- `weekOpusUsd` / `monthOpusUsd`: the same two numbers under the names the current page reads.
- `planName`, `planPriceUsd`: copies of the top-level keys (section Plan).
- `timesPrice` = `monthUsd / planPriceUsd`, one decimal; `null` while the price is unknown. The page then shows "You get" without "You pay" and without ×.
- `pairs` = `rates["7d"].pairsUsed`.
- `confident`: `true` from **4 clean weekly stretches with at least $240 of this Mac's spend in them**.
  - Fewer, and one stretch with unnoticed use elsewhere can still pull the figure down by a lot.
  - Four stretches of $60 come from about three days of normal use.
  - `reason` (string) says what is missing while `confident` is `false`, else `null`. The page shows the reason instead of the numbers.
- `byModel`: one entry per model used in the last 30 days, most dollars first.
  - `label` is the page's short name.
  - `monthUsd` = the whole monthly limit spent on that model alone, measured only from stretches where that model is at least 90 % of the dollars (same method as the rate). `timesPrice` is that over the plan price. `pairs` = such clean stretches.
  - A model without such a stretch has `monthUsd` / `timesPrice` `null` and `pairs` 0: its own value is not measured yet.
  - This is on purpose. On 14 August, $3.40 of Flash moved the 5-hour limit 15.5 points, about $22 per full window. Neon needs at least $99 now, so a Flash dollar went about 4 times further. The plan may have changed since, but one shared dollar rate for every model would likely be wrong.
- Not present: `weekFableCapUsd`, `fableCapShareOfWeek`, `weekMixUsd`, `weekAllFableUsd`.
- When `value` is `null`, `valueReason` (string) says why, e.g. `"the weekly rate needs one stretch with $60+ of this Mac's spend between two readings of the weekly limit (last 35 days); have 0"`. When `value` is set, `valueReason` is `null`.

### Plan

Name and price come from three places, in this order:

1. **The owner's file** `~/Library/Application Support/ClaudeSwitcher/usage/antigravity-plan.json` (`antigravity.PLAN_FILE`), which overrides everything, field by field:

   ```json
   {"planName": "Google AI Ultra 5x", "planPriceUsd": 99.99}
   ```

   Both keys are optional. A missing or malformed file, an empty name, or a price that is not a positive number counts as not set. The file is read on every pass, so a change shows up within 5 minutes.
2. **The name the app saw.** CodexBar asks Google for the plan name on every refresh, and the fork writes it into each line of the live readings file as `"plan"` (section "Live readings file"). The collector remembers the last one (`kv` key `antigravity_plan_seen`).
3. **The list price** for that name from `antigravity.PLAN_PRICES` (monthly, USD, checked on 23.11.2026): Google AI Pro 19.99, Ultra 5x 99.99, Ultra 20x 199.99. Every word of a key must be in the name; "Ultra" without its size, a free plan or an unknown name has no price.

Nothing else on this Mac holds the plan: Antigravity's own `state.vscdb` has a cached user status without a tier (dated June 2026), and `~/.gemini` has no tier field. Without a price `value.timesPrice` is null and the page shows "You get" without "You pay".

## Keys only in this block

```jsonc
"beta": true,
"models": {                       // last 30 days, per priced model id; exact tokens, estimated usd
  "gemini-4-neon": {"calls": 812, "input": 3100000, "cached": 61000000, "cacheWrite": 0,
                    "output": 410000, "thinking": 190000, "usd": 31.4, "missPct": 4.8,
                    "efforts": {"High": 800, "Medium": 12}, "priced": true, "priceConfirmed": true,
                    "unpricedCalls": 0}
},
"cache": {                        // prompt tokens read from the cache vs missed; tokens exact, dollars estimate
  "today": {"readPct": 94.5, "missPct": 5.5, "coldCallsPct": 4.3, "calls": 1181,
            "readTokens": 180833575, "missTokens": 10529294, "writeTokens": 0,
            "savedUsd": 512.12, "missUsd": 29.49},
  "7d":    {…same keys…},
  "30d":   {…same keys…}
},
"calibration": {                  // what the rate and the sessions are built from, and the thresholds
  "stretches": {"7d": 1, "5h": 0},         // all stretches (clean + dropped), last 35 days
  "sessionIntervals": 0,                   // intervals that count for windows.7d.sessions
  "sessionPerWeekSoFar": null,             // perWeek from them even below the threshold (debugging only)
  "thresholds": {"jitterPts": 2.0, "stretchUsd": {"5h": 15.0, "7d": 60.0}, "confidentStretches": 4,
                 "confidentUsd": 240.0, "sessionMinPts": {"5h": 5.0, "7d": 3.0}, "sessionPairs": 3,
                 "sessionSum7dPts": 12.0},
  "planFile": "~/Library/Application Support/ClaudeSwitcher/usage/antigravity-plan.json",
  "planFileExists": false
},
"coverage": {
  "tokens": "exact", "usd": "estimate",
  "historySince": "2026-08-09",          // first local day with calls
  "dbFiles": 210,                        // conversation databases read
  "calls": 3880,                         // calls counted (all local history)
  "copiesDropped": 847,                  // copied rows of forked conversations, counted once
  "encryptedConversations": 100,         // older .pb conversations that cannot be read; not counted
  "timeEstimatedCalls": 0,               // calls dated by conversation start or file date
  "outputCheckFailed": 0,
  "unreadableDbs": 0, "malformedRows": 0,
  "unpricedCalls30d": 0,                 // calls without a Gemini price: no dollars
  "unconfirmedPrices": ["gemini-3.7-flash"],   // models used in 30 days whose price is not on the official page
  "pricesChecked": "2026-11-23", "priceSource": "https://aistudio.google.com/docs/pricing",
  "notCounted": "encrypted .pb conversations, deleted conversations, Tab completions, other devices",
  "scanCached": true, "scanRescannedFiles": 0, "scanMs": 9   // scan from the cache? databases read again, time
},
"quotaSource": {
  "app": "CodexBar",
  "file": "~/Library/Application Support/com.steipete.codexbar/history/antigravity.json",
  "fileModifiedAt": 1795421347,
  "lastSampleAt": 1795417747,            // = lastMeasuredAt
  "ageSec": 1980,                        // generatedAt − lastSampleAt
  "cadence": "CodexBar keeps at most the peak sample of each clock hour",
  "weekly": "the weekly quota of Antigravity's Gemini model group (older CodexBar history may hold the highest weekly quota of any group)",
  "accountsInFile": 1,                   // Google accounts in CodexBar's file; with more than one, its preferred one is used
  "bySource": {"codexbar": {"5h": {"count": 372, "first": 1786287573, "last": 1795417747}, "7d": {…}},
               "live":     {"5h": {"count": 40, "first": 1795420000, "last": 1795427000}, "7d": {…}}},
  "lastSource": "live",                  // source of the newest sample ("live" wins a tie); null without samples
  "liveFile": "~/Library/Application Support/ClaudeSwitcher/usage/antigravity-quota.jsonl",
  "liveFileExists": true,
  "liveBadLines": 0,                     // lines of the live file that could not be used, since the last compaction
  "liveCompactedAt": null,               // epoch of the last compaction of the live file, null if never
  "samples": {"5h": {"count": 374, "first": 1786287573, "last": 1795417747, "today": 2},
              "7d": {"count": 223, "first": 1786287573, "last": 1795417747, "today": 2}}
},                                       // samples: real samples of both sources before merging (no start points)
"valueReason": null
```

Cache keys (same shape as `accounts[].cache`, see `README.md`):

- A prompt token is either **read** from the cache (`readTokens` = cached input) or **missed** (`missTokens` = uncached input + cache writes). Gemini bills a cache write at the full input price, so writes count as missed. `writeTokens` = the cache-write part of the misses; it is 0 on this Mac, because Gemini 4 only caches implicitly.
- `readPct` = 100 × read / (read + missed). `missPct` = 100 − `readPct` (the old formula: 100 × (1 − cached / (input + cached + cacheWrite))).
- `coldCallsPct` = share of calls that read nothing from the cache. All three are `null` when there are no calls.
- `savedUsd` = read tokens × (input price − cache-read price): what the cache saved against paying full price for those tokens.
- `missUsd` = missed tokens × (input price − cache-read price): what the misses cost above what they would have cost as cache reads.
- Both are dollars at API list prices, per call with that call's model and date. Calls without a price add tokens only.

## Live readings file (written by the app)

CodexBar's own history keeps only the peak of each clock hour. So the menu bar app also appends every fresh Antigravity reading it gets to a file of its own, and the collector imports it.

**Path:** `~/Library/Application Support/ClaudeSwitcher/usage/antigravity-quota.jsonl`, which is `core.DATA + "/antigravity-quota.jsonl"`, next to `state.json`. The directory exists whenever `state.json` does: the collector creates it on every pass. The writer should still create it (`createDirectory(withIntermediateDirectories: true)`) if it is missing.

**Format:** UTF-8 JSON Lines. One reading per line, one JSON object per line, and every line ends with `\n`. No pretty-printing and no blank lines.

```json
{"ts": 1795424947, "source": "codexbar-live", "windows": {"5h": {"pct": 1.0, "resetsAt": 1795439520}, "7d": {"pct": 40.8, "resetsAt": 1795873920}}}
```

| Field | Type | Meaning |
|---|---|---|
| `ts` | integer epoch seconds (a decimal is truncated) | when the reading was taken |
| `source` | string | who wrote it, `"codexbar-live"`; informational, the collector stores every line as source `live` |
| `windows` | object | keys `"5h"` (Antigravity's 5-hour window) and `"7d"` (the weekly window, the same lane CodexBar records as `weekly`); either may be missing |
| `windows.*.pct` | number 0–100, decimals allowed | USED percent (not remaining) |
| `windows.*.resetsAt` | integer epoch seconds or `null` | when that window resets |
| `plan` | string, optional | the plan name the app fetched with this reading, as fetched; left out when unknown |

**When to write:** a line goes in only when some value changed, or when 10 minutes have passed since the last line. That gives up to about 150 lines a day.

**How the collector reads it:**
- It reads from the byte offset it reached last time.
- It takes only lines that end in `\n`. A line still being written is read on the next pass.
- A line that is not valid JSON, has no `ts` or `windows`, or has no usable window is skipped and counted in `quotaSource.liveBadLines`.
- A window with `pct` outside 0–100 is skipped. A `resetsAt` that is not a number is stored as unknown.

**Writing, the lock, and compaction:**
1. For every line, open the lock file `antigravity-quota.jsonl.lock` (create it if missing, never delete it) and take an exclusive `flock` on it.
2. Open `antigravity-quota.jsonl` in append mode (`O_WRONLY|O_APPEND|O_CREAT`, mode 0644), write the whole line, including `\n`, in one `write`, and close it.
3. Release the lock. Do not keep the data file open between lines.
4. When the file is larger than 1,000,000 bytes, the collector rewrites it under the same lock:
   - it imports every line first, then drops lines older than 40 days and broken lines;
   - it writes a temporary file in the same folder and renames it over the original, so the original is replaced in one step.
5. If the lock is busy, the collector skips compaction until the next pass. It never waits for the writer.

**The race this avoids:** a writer that appended between the collector's read and its rename would write into the old, already replaced file, and that line would be lost. The shared lock rules that out. A writer that ignores the lock or keeps the file open would lose at most the lines written during the few milliseconds of one compaction (once per ~6 weeks at 150 lines a day). That is harmless: readings come every ≤ 10 minutes and CodexBar's hourly history still covers that hour. A recreated or compacted file (new inode, or shorter than the saved offset) is read from the start again. Every sample is stored with `INSERT OR IGNORE`, so re-reading never duplicates anything.

**Account (added 2026-11-25):** a line may carry `"account"`: the same string CodexBar writes as `preferredAccountKey` into its history file for this provider (`history/antigravity.json`, `history/codex.json`). It is a CodexBar key, never an e-mail address.
- The current account is the history file's `preferredAccountKey` (or its only account). Only its readings are kept: a live line of another account is skipped, and the history import takes only that account's series.
- A line without `"account"` (written before the key existed) belongs to the account that was current when the collector first saw the rule work (kept in `kv` as `<provider>_account_legacy`). After a switch to another account such lines are skipped.
- When the preferred account changes, the stored samples of the old account are parked under `acc = "<provider>:<hash>"`, the new account's parked samples come back, its history is imported, and the live file is read again from its start with the new filter. Switching back restores the old account's readings.
- The block names the account only as `quotaSource.account`: the first 12 hex digits of SHA-256 of the key. No key or e-mail enters `state.json`.

**Merge with CodexBar's history:** both sources go into `samples` (`acc = 'antigravity'`, source `live` or `codexbar`). For windows, series, rates and forecasts, a `codexbar` sample is dropped whenever the same window has a `live` sample in the same clock hour. The hourly history entry is one of the readings the live file also holds, so keeping both would count that reading twice. Hours without live readings (the app was not running, or before the file existed) keep the CodexBar sample. Live samples are never dropped.

## Small example (shape only, numbers made up)

```json
{
 "uuid": "antigravity", "name": "Antigravity", "short": "Antigravity", "beta": true,
 "active": false, "useHapp": false, "planName": null, "planPriceUsd": null, "has5h": true, "sessionPctOfWeek": 17.2,
 "lastMeasuredAt": 1795417747, "lastPoll": null, "trackedSince": 1786287000, "kFable": 1.0,
 "windows": {
  "5h": {"pct": 1.0, "measuredAt": 1795417747, "resetsAt": 1795439540, "source": "codexbar", "rolled": false,
         "start": 1795421540, "usd": {"opus": 0, "fable": 0, "other": 4.12},
         "split": {"youPct": 0.0, "othersPct": 0.0, "unknownPct": 1.0, "method": "unknown",
                   "preliminary": false, "reason": "possible_local_activity"},
         "mine": null, "outside": null,
         "series": [[1795421520, 0.0, 0.0, 0.0, 0.0], [1795424350, 0.33, null, 0.0, 0.3],
                    [1795424947, 1.0, null, 0.0, 1.0]]},
  "7d": {"pct": 40.83, "measuredAt": 1795417747, "resetsAt": 1795873903, "source": "codexbar", "rolled": false,
         "start": 1795269103, "usd": {"opus": 0, "fable": 0, "other": 96.3}, "sinceMeasure": 0.4,
         "split": {"youPct": 20.0, "othersPct": 20.8, "unknownPct": 0.0, "method": "calibrated",
                   "preliminary": false, "reason": null},
         "mine": 20.0, "outside": 20.8,
         "series": [[1795269120, 0.0, 0.0, 0.0, 0.0], [1795424350, 40.2, 19.8, 20.4, 0.0],
                    [1795424947, 40.83, 20.0, 20.8, 0.0]],
         "sessions": {"perWeek": 5.8, "pairs": 4}}
 },
 "rates": {"7d": {"pct_per_usd": 0.41, "usd_per_100": 243.9, "pairs": 6, "source": "own", "method": "From stretches …",
                  "pairsUsed": 6, "pairsDropped": 2, "range": [231, 262], "usd": 380.0}},
 "drift": [],
 "daily": [{"day": "2026-11-23", "opus": 0, "fable": 0, "other": 12.5, "tokens": 9100000,
            "m": {"gemini-4-neon": [12.5, 9100000]}}],
 "usdToday": 12.5, "tokensToday": 9100000, "usd7d": 253.0, "tokens7d": 190000000,
 "usd30d": 700.0, "tokens30d": 520000000,
 "weeks": [{"start": 1795269103, "end": 1795873903, "current": true,
            "usd": {"opus": 0, "fable": 0, "other": 96.3}, "tokens": 70000000, "peakPct": 40.83,
            "m": {"gemini-4-neon": [96.3, 70000000]}}],
 "value": {"planName": null, "planPriceUsd": null, "weekUsd": 244, "monthUsd": 1061, "timesPrice": null, "pairs": 6,
           "confident": true, "reason": null, "byModel": [], "weekOpusUsd": 244, "monthOpusUsd": 1061},
 "valueReason": null,
 "models": {}, "cache": {}, "coverage": {}, "quotaSource": {}, "calibration": {}
}
```

A full sample with made-up numbers (synthetic, not from any real use): `usage/antigravity-state-sample.json`, written by `docs/make_demo_state.py --antigravity-sample`.
