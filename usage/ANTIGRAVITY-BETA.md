# Gemini spend through Antigravity (beta)

Status: **beta**, built on 23 November 2026. The standalone script `antigravity_beta.py` is now also used by the collector: `antigravity.py` writes the `antigravity` block of `state.json` on every pass. The contract is in `ANTIGRAVITY-STATE.md`. The first collector items of "What to build next" are done there.

## What it does

It reads Antigravity's local conversation databases and prints, per day and per model: calls, input tokens, cached input tokens, output tokens (thinking included), the thinking part, and the dollars the same tokens would cost on the Gemini API. It also prints totals for today, the last 7 days, the last 30 days and the whole local history. It only reads: databases are opened with `mode=ro&immutable=1`, and a database that is still being written is copied with its `-wal` file into a temporary folder first. Python 3 standard library only.

```bash
python3 usage/antigravity_beta.py              # last 30 days, day by day, plus totals
python3 usage/antigravity_beta.py --days 7     # list fewer days
python3 usage/antigravity_beta.py --all        # every day of the local history
python3 usage/antigravity_beta.py --json       # for the collector
```

## Cache miss

The text report has a `miss` column: the share of the prompt (input + cached input) that was not read from the cache. `cache_compare_beta.py` puts the same number next to Claude Code (from the collector's database) and Codex (from `~/.codex/sessions`), with the share of calls that read nothing from the cache, the share of big prompts where more than half missed, and what the prompts cost against no cache at all.

```bash
python3 usage/cache_compare_beta.py
```

On 23.11.2026, last 7 days: Gemini through Antigravity missed 6.0 % of prompt tokens, Codex 4.8 %, Claude Code 2.0 %. Gemini's prompts still cost the least against no cache (10.7 %, Claude 12.9 %, Codex 14.3 %), because Gemini charges nothing for writing to the cache and 0.05x for reading, while Claude pays 1.25x–2x to write. The Codex sample is small (236 calls).

## Where the numbers come from

- `~/.gemini/antigravity/conversations/<id>.db` (the app) and `~/.gemini/antigravity-cli/conversations/<id>.db` (the `agy` CLI): one SQLite file per conversation.
- Table `gen_metadata(idx, data, size)`: one row per model call. `data` is protobuf with no published schema. Inside it, message `#1.#4` holds the token counts: `#2` uncached input, `#5` cache read, `#4` cache write, `#9` thinking, `#10` visible output, `#3` = `#9` + `#10`, `#11` response id. `#1.#19` is the model id and `#1.#21` the display name.
- Table `steps(idx, metadata)`: `metadata #1` is the step's time. Newer builds don't store a time on the call itself, so the call gets the time of its first step (root `#2` lists those steps).
- An anonymized example call: model id `gemini-4-neon`, display name `Gemini 4 Neon (High)`, input 3,821, cached 73,819, output 283 (thinking 141 plus visible 142), cache write absent.
- No token data anywhere else: `conversation_summaries.db` holds titles, step counts and times, `daemon/*.log` are old language-server logs, and `antigravity-ide/` has only `.pb` files.

## Exact or estimated

| What | Status |
|---|---|
| Calls, input, cached input, output, thinking, model id | Exact, as Antigravity recorded them. The field meanings are reverse-engineered, but every row passes the output = thinking + visible check, and CodexBar, CodeBurn and agent-walker read the fields the same way. |
| Day of each call | Exact for older rows that store their own time. Otherwise it comes from the call's first step, which agrees with the stored time within 0.2 s wherever a row has both. |
| Dollars | Estimate at Gemini API list prices, Standard tier. Antigravity doesn't bill per token: Pro and Ultra get a quota that refreshes every 5 hours, up to a weekly limit. |
| Copies | A forked or continued conversation contains copies of the parent's rows, with the same response id and the same tokens. They are counted once. On 23.11.2026 there were 847 copied rows against 3,880 real calls. |
| Not counted | 100 conversations from March to May 2026 are stored as encrypted `.pb` files (the same 100 sit in both `antigravity/` and `antigravity-ide/`). Also not counted: deleted conversations, Tab completions, and use on other devices. |

## Prices ($ per 1M tokens, Standard tier)

Source: https://ai.google.dev/gemini-api/docs/pricing (now redirects to https://aistudio.google.com/docs/pricing), checked 2026-11-23. Output includes thinking tokens. Gemini 4 models use implicit caching only and have no >200k prompt tier.

| Model | Input | Output | Cached input | Change |
|---|---|---|---|---|
| gemini-4-neon | 3.00 | 15.00 | 0.15 | |
| gemini-4-carbon, gemini-4-argon | 2.00 | 10.00 | 0.10 | 4 / 20 / 0.20 from 2026-12-01 |
| gemini-4-hydrogen | 1.50 | 7.50 | 0.075 | |
| gemini-3.8-flash, gemini-3.6-flash | 0.75 | 3.75 | 0.075 | 1.50 / 7.50 / 0.15 from 2027-01-01 |
| gemini-3.7-flash | 0.75 | 3.75 | 0.075 | unconfirmed: not on the official page; the price comes from the models.dev catalog |
| gemini-3.1-pro | 2.00 / 4.00 above 200k | 12.00 / 18.00 | 0.20 / 0.40 | |

Recorded ids map to these prices as follows: `gemini-4-hydrogen-tiered-high` is priced as `gemini-4-hydrogen`, and `gemini-3.6-flash-exp-a` as `gemini-3.6-flash`. "High" and "Medium" are effort levels of the same model, and the script shows them as counts.

## What to build next

Done on 23 November 2026: the collector step (`antigravity.py`, the `antigravity` block of `state.json`, contract in `ANTIGRAVITY-STATE.md`), the Antigravity page in the CodexBar panel (`UsageAntigravityPage`, described in `codexbar-fork/README.md`), and the live readings file the app writes (`antigravity-quota.jsonl`).

Also done on 23 November 2026, in the collector (`antigravity.py`, contract in `ANTIGRAVITY-STATE.md`):
- Rates now come from stretches of $60 or more of this Mac's spend between two readings (weekly; $15 for the 5-hour limit). The stretches do not overlap. A stretch where the limit moved more than this Mac's spend explains is dropped: that was use in the chat app or on a phone. Since the review the same day, a stretch whose ceiling no other stretch agrees with (it moved the limit much less than the rest) is left out too, so one odd reading can no longer set the rate alone (`ANTIGRAVITY-STATE.md`, Rates).
- Sessions per week are measured straight from the readings, with no dollars involved.
- Value has a confidence flag and one line per model.
- Cache has read and missed tokens and dollars.

1. **Rate per 1 %, Value, sessions: let data come in, then check.**
   - On 23.11 the weekly rate rests on one clean stretch: $230 moved the week 40 %, so the whole week is worth at least about $571. Value says `confident: false` until there are 4 clean stretches with $240 in them, about three days of normal use.
   - Sessions per week stay null until 3 five-hour windows have each moved the 5-hour limit 5+ points and the week 3+ points, with no fall of more than 4 points inside the window (a fall that large is a reset, not a reading settling).
   - When they appear, check the two against each other: perWeek ≈ weekly $ per 100 % / 5-hour $ per 100 %, measured on stretches without outside use.
   - Today's one interval says about 2.3 five-hour windows per week. The dollar rates say up to 5.8 if that window had no outside use. They agree only if about 60 % of that window's use came from somewhere else.
2. **The old oddity, explained.** On 23.11 the 5-hour window read 1 % for $0.36 while the week read 41 % for $230.
   - The 1 % was a jump in the reading, not use. At 10:09 both limits jumped about 0.65 points over the 09:59 reading, with no spend on this Mac in between. For the next 4 hours both read 0.33 % and 40.08 % again.
   - Readings of one moment differ by up to about 0.8 points, and both limits jump by about the same amount. Today the app's readings alternated between two such values (5-hour 6.26 / weekly 42.77 and 5.44 / 42.10 within three minutes).
   - On a 1 % move that is most of the signal. The method allows 2 points of jitter on every stretch (readings also settle: on 23.11 they fell to 4.30 / 41.16 with no call since 16:44, 2.0 and 1.6 points below the highs) and needs moves far larger than that.
   - Where the jumps come from is still open. The app's live line takes the 5-hour and weekly values from the same "Gemini" quota group, so a switch between groups is unlikely inside one line; but with several unversioned Gemini groups it takes the first window of each title, and CodexBar's history written before 23.11 could hold a provider-wide weekly maximum. Requests still in flight are the other candidate. A fix in CodexBar would let the collector use a smaller allowance.
3. **Plan price.** The tier cannot be read on this Mac.
   - CodexBar asks Google for it on every refresh and does not store it.
   - Antigravity's `state.vscdb` has only a stale cached user status from June 2026, without a tier.
   - The owner can set name and price in `~/Library/Application Support/ClaudeSwitcher/usage/antigravity-plan.json` (`{"planName": …, "planPriceUsd": …}`); then "You pay" and × appear.
   - The collector already reads an optional `"plan"` key of the app's live readings line (the name CodexBar fetches, `identity.loginMethod`) and keeps the last one seen; the price then comes from `antigravity.PLAN_PRICES`. What is left is for the app to write that key.
4. **Per-model value.** Only a model that makes up 90 %+ of a stretch's dollars gets its own value; today that is Neon only. August readings suggest a Flash dollar moves the limit several times more than a Neon dollar, so Carbon, Hydrogen and Flash need their own single-model stretches before the page can show their ×.
5. **First visit to the page.** Turning to a page for the first time shows its half of the strip empty for about a tenth of a second (every page does this; Antigravity builds in 15 ms from rows rendered ahead, the rest is NSMenu attaching the rows).
6. **Duplicates in CodexBar.** CodexBar's own Antigravity history (`AntigravityLocalReader.swift`) keys duplicates by file plus response id, so copied rows count once per file. Our page does not use it, but the Overview tab's Antigravity cost row still does.
7. **Overview.** Pointing at the Antigravity cost row on Overview can first show the Codex and Claude charts: the menu's own highlight goes back to earlier rows after the empty stand-in submenu opens for a moment. Not fixed.

## Open questions

- The field meanings are unofficial. A future Antigravity build can move them. The output check catches changes on the output side but not on the input side.
- The old `.pb` history could only be read through the running language server (`GetCascadeTrajectoryGeneratorMetadata`, with a port and CSRF token taken from its process arguments). The script does not do this on purpose.
- The price of `gemini-3.7-flash` needs confirming. So does whether Antigravity's "tiered" Hydrogen bills like the public model.
- Calls are not attributed to an account: the databases don't record which Google account made a call.
- Plan prices: Google's I/O 2026 subscriptions post gives Ultra 5x at $100 a month and Ultra 20x at $200 (cut from $250); `PLAN_PRICES` uses 99.99 and 199.99. It gives no price for Pro, so Pro's 19.99 is not confirmed from Google. A name that matches no key (for example "Ultra" without its size) gets no price, so `planPriceUsd` is never guessed from a vague name.
