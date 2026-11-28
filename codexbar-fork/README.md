# Custom CodexBar build

`/Applications/CodexBar.app` is a CodexBar built from the author's sources (steipete/CodexBar) with our changes. The sources are in `../CodexBar`, branch `lemelson/accounts`, based on the official version `v0.85.0`.

## What is changed compared with the official CodexBar

The Claude tab has four modes, switched with buttons at the top of the menu:

| Mode | What it shows |
|---|---|
| Limits | The limits of all three accounts (a VPN-only account shows its last reading, coloured by age: orange from 12 hours, red from a day or after the window reset; an account that is not read because the VPN is on shows a compact "Paused · VPN" status in the card header's top-right corner so the card stays single-line): how much I used (Claude Code on this Mac), how much went to other Claude Code, chat and Cowork; a pace mark on every bar (green: in reserve, red: in deficit); under the weekly limit, how many full 5-hour sessions are left and how many fit before the reset (only for accounts that have a 5-hour window); a warning in place of the reset time when the limit will run out before the reset ("⚠ Out 3d 4h early"; the reset time is in the row's panel), so a warning never makes a row taller; under the cards one quiet line with the reading time and the refresh state ("Updated 19:14 · refreshes when you open this menu", "Refreshing limits…", or how long the collector has not run, in the warning colour), grouped with the Plan worth and Prompt cache rows the way the provider pages group their footer; on hover over a row, a chart of the window with the forecast, the exact reserve percent and details |
| Spend | My spend on this Mac: today, 7 days and the selected period (30 / 60 / 90 / 180 days), a daily chart with hover, split by accounts or by models (the switch is remembered like the period), four models with tokens (Opus, Fable, Sonnet, Haiku, each with its own colour), accounts (on hover: each week as a part of the weekly limit, "you" against "others", models and the account switcher). The standard Cost and Usage history stay below |
| Value | What the subscription is worth at API prices: the most the limit can buy on Opus alone and on Fable alone, and my real use ("Real use, 30 days"); whether the limit changes, by month (bars: the average week of a calendar month × 4.35, faded while under two weeks of it are measured) or by week, in Opus dollars, at my mix or in Fable dollars, with the values shown when the pointer is over the chart. All switches on the page are remembered |
| Classic | The standard CodexBar card, unchanged |

The menu opens on the provider tab and, on Claude, in the mode that was shown last (`lemelsonPanelLastMode`, kept across restarts). The pin to the right of the buttons overrides that: a pinned mode is used every time the Claude tab opens; clicking the filled pin unpins.

A low row "Prompt cache · N% reused" sits above the first action row on every Claude mode, on Antigravity and on Codex (`UsageCacheRowView`, a `UsagePageLineRow`); its detail panel says in plain words what was reused, what was paid in full and what that saved, per account or per model. Reused and paid-in-full shares are rounded together and add up to 100 (`UsageFormat.split`). It is absent when the summary has no cache numbers.

The Overview tab is a page of ours too: spend of every subscription on this Mac together (Claude, Codex, Antigravity) with the same period switch and per-day chart, one row per provider (its detail panel is that provider's numbers), and one "Prompt cache" row for all of them together (`UsageCacheSummary.all`: percentages weighted by the tokens behind them, dollars added up, one part per provider; a provider whose step failed is left out). `defaults write com.steipete.codexbar lemelsonOverviewPage stock` brings the stock Overview back (next menu build, no restart).

The merged menu bar item shows one number: API dollars spent over the last 24 hours, rolling, across every subscription (`state.json` → `spend24h`, written by `usage/tick.py`; older summaries fall back to the accounts' `usd24h`). Under $10 with cents ("$4.20"), above in whole dollars ("$182", "$1,240"); the tooltip splits it by provider (Claude by account). The figure follows every collector tick and account switch; while the menu is open it waits for the menu to close. Drawn in the secondary colour when the collector has not run for an hour. `defaults write com.steipete.codexbar lemelsonMenuBarSpend stock` brings the stock "5h 0% $18.96" layout back; `lemelsonMenuBarSpendCents -bool YES` always shows cents.

The per-day spend charts (Overview, Claude Spend, Codex, Antigravity) take the pointer inside the open menu (`UsageChartPointer`, fed by the event filter before the hover panel): pointing at a day slides a day panel out left of the menu (that day's total, how it compares with the period's daily average, its rank, and the day by provider or account and by model; `UsageDayDetailPanelView`); pressing and dragging across days zooms into them (the panel shows the span while it is dragged; the header then shows the span's total, daily average and busiest day, and a chip with ✕). A double click, a period button, the ✕ or Escape zooms out; the first Escape ends a drag or a zoom and only the next one closes the menu. Closing the menu or turning the page also ends the zoom. The zoom is menu-wide and never saved.

The Antigravity tab is one page of ours (`UsageProviderPage` with `UsageProviderSpec.antigravity`, beta) in place of the stock card and the stock rows under it (Plan Usage, Cost: the stock Cost counts copied conversations twice): a limit card like Claude Limits (the same header, rows, verdict and time mark: `Sources/LemelsonUsageKit/UsageLimitCard.swift`) with the 5-hour and weekly Gemini quota and the detail panel, spend at Gemini API prices with the period switch (By model names the leading models of the selected period, with that period's legend and colors and always the same number of rows; "Other" lists the rest on one line; "no price" comes from the days of the period, `daily[].unpriced`), a one-line footer ("Beta · dollars at … prices · updated HH:mm"), and under it two one-line rows above the action rows: "Plan worth · ≈$289/mo · ×15" (or "measuring, 3 of 4") and "Prompt cache". The Plan worth row's panel holds Claude's Value card for the provider and the value by month (`UsageDriftMonthChartView`, the chart of Claude's Value page); the weekly limit's panel ends with the last four weekly windows (`UsageWeekWindows`, the rows of Claude Spend's week list; a window with no readings has "≈" dates and says "No readings that week", one reset ahead of schedule says "reset early"). Model names come from one rule (`UsageModelNaming`, `UsageProviderBlock.modelLabels`) in spend, value and cache. Under the weekly limit: how many full 5-hour sessions are left (and, in the panel, what the running 5-hour window still allows until it resets and how many full sessions fit after it), or "measuring, X of Y windows seen" until the collector has measured enough windows. It needs the `antigravity` block of `state.json`; without it the stock card stays. `defaults write com.steipete.codexbar lemelsonAntigravityPage stock` brings the stock tab back (next menu build, no restart).

The Codex tab is the same page with Codex data (`UsageProviderSpec.codex`, beta), from the `codex` block of `state.json` (contract: `usage/CODEX-STATE.md`): a card "Codex" with the plan from the collector (`planName`, "Plus") and, under the name, the account e-mail exactly as the stock Codex card shows it (hidden when CodexBar hides personal info); the session (5-hour) and weekly rows with the detail panel; then two facts from CodexBar's own Codex snapshot, each only when the stock card would show it: "Credits · N left" (the balance; CodexBar's line about a monthly credit cap or an unreadable balance goes under it) and "Limit reset credits · N available" with the next expiry (the detail panel lists every credit's expiry); spend of Codex on this Mac at OpenAI API prices by model, the footer, the "Plan worth" and "Prompt cache" rows. The credits fact explains itself in its panel (what credits are for and the exact balance). The quota has no split by surface, so the bars are one segment, like Antigravity's. The page replaces the stock usage card, Limit Reset Credits, Credits, Extra usage (for a Plus account the same balance again; a monthly cap is in the credits line), Plan Usage (the limit rows' detail panel) and Cost (the page's spend). "Buy Credits..." (CodexBar's own `makeBuyCreditsItem`) is the first action row, right under Prompt cache; Usage breakdown and Credits history (OpenAI web charts the page has no counterpart for) stay as rows with their detail panel when CodexBar has their data. Several Codex accounts shown at once keep the stock cards. Without the `codex` block the stock card stays; `defaults write com.steipete.codexbar lemelsonCodexPage stock` brings the stock tab back (next menu build, no restart).

Every fresh Antigravity or Codex reading the app gets is appended to `~/Library/Application Support/ClaudeSwitcher/usage/antigravity-quota.jsonl` or `codex-quota.jsonl` next to it (when a value changed or ten minutes passed; Codex's session window is `"5h"`, its weekly window `"7d"`, `plan` the plan name as the Codex card shows it; contract: `usage/ANTIGRAVITY-STATE.md`, "Live readings file", and `usage/CODEX-STATE.md`), which the collector imports. Each line carries `account`: the same key CodexBar files that account's history under (`planUtilizationHistorySelection(...).accountKey`, the history file's `preferredAccountKey`; never an e-mail). The collector names the account its numbers belong to in `quotaSource.account` (a 12-character SHA-256 prefix of that key); when it differs from the account shown, the page shows "No limits collected yet for this … account." instead of the limit rows, and leaves out the plan name, the reading age and the Plan worth row (spend on this Mac stays: it is not per account). One writer serves both files (`Sources/LemelsonUsageKit/UsageQuotaLive.swift`, an actor per file, so waiting for the collector's lock never blocks the menu; each write waits for the one before it, and the log shows the values but keeps the plan and the account key private); the app side is `Sources/CodexBar/Lemelson/StatusItemController+LemelsonProviderPages.swift`. When a page is shown and the store's reading is older than two minutes, the app fetches that provider again (Antigravity takes about half a minute; the footer says it is refreshing); a new line while the menu is open runs the collector and rebuilds the page.

Other changes:

- Every time the menu opens, CodexBar runs the collector `usage/tick.py --refresh`, which queries the limits again. The VPN rule still applies: an account marked VPN-only is queried only while Happ is on, all others only without the VPN.
- CodexBar itself no longer goes to the network for Claude limits; it takes them from the collector's summary. Otherwise it would query the server bypassing the VPN rule.
- Left and right arrows and a two-finger horizontal swipe page through one bounded line: Overview, Codex, Claude Limits, Spend, Value, Classic, then the other enabled providers (Antigravity; the Gemini provider is switched off in `~/.codexbar/config.json`). They never leave CodexBar for the neighbouring menu bar items and never wrap around. See "Paging" below.
- The white warning-threshold ticks on the bars are off by default (to bring them back: Settings → Menu → Show quota warning markers).
- Sparkle auto-update does not work in the custom build (it turns itself off for any signature other than the author's). Updating is manual, see below.

All our code lives in five folders so that the author's updates almost never touch it:

- `Sources/LemelsonUsageKit`: the panel layout (SwiftUI);
- `Sources/CodexBar/Lemelson`: the menu insertion, the modes, the refresh on opening;
- `Sources/CodexBarCore/Providers/Claude/Lemelson`: reading limits from the summary;
- `Sources/LemelsonPanelPreview`: renders the panel to a PNG without launching CodexBar.
- `Sources/LemelsonCompat`: a shim for an Xcode / macOS mismatch (see below).

The author's code is touched in these places, each marked `lemelson:`: `Package.swift` (the Lemelson* targets and the dependency on LemelsonCompat), `Scripts/package_app.sh`, `StatusItemController+Menu.swift` (menu insertion; under the Codex page only the Usage breakdown and Credits history rows of the stock rows), `+MenuTracking.swift`, `+ProviderNavigation.swift`, `+ProviderSwitcher.swift` and `+MenuSwitcherWarmup.swift` (menu insertion, paging, and holding the tab warm-up back while a page is turning), `StatusItemController.swift` (watching the store for fresh Antigravity and Codex readings), `+MenuPresentation.swift` (a row with no registered controls skips the hit test that measured its whole content on every pointer move; a row with controls hands a click to the AppKit control under the pointer, which SwiftUI's hosting view otherwise swallowed), `SessionUsageMenuAction.swift` (closing the right menu from a chart shown in the detail panel), `ClaudeProviderDescriptor.swift`, `SettingsStore.swift`, and one test expectation in `SettingsStoreTests.swift`.

## Commands

```bash
./build.sh      # release build of ../CodexBar/CodexBar.app (several minutes)
./install.sh    # archive the current CodexBar and install the built one
./update.sh     # move to the author's latest version: rebase our changes, build, install
```

`update.sh` requires the changes in `../CodexBar` to be committed to the `lemelson/accounts` branch. Until they are committed, the script stops and says so.

If the author changed the same places in the code, `update.sh` stops with a conflict. Then open Claude Code in this folder and write: "update my CodexBar to the latest version, there is a conflict while rebasing the changes". The previous state stays in the `backup/before-<version>` branch.

It is worth updating every few weeks or when CodexBar gets something you need. To check whether a new version is out:

```bash
git -C ../CodexBar ls-remote --tags origin | tail -3
```

## If the build does not start

`install.sh` restores the previous CodexBar by itself if the new build does not stay up for a few seconds.

Known cause (23.11.2026): Xcode newer than the installed macOS. This was found on a macOS 27.2 beta; Xcode 27.2 builds for the final 27.2 and emits calls to functions that the beta does not have: `Date >=` and `Date <=`. The app crashed a second after launch. The build includes a shim for these two functions in `Sources/LemelsonCompat`.

After building, `build.sh` runs `check_symbols.py`: it lists the system functions that the app references and that this macOS lacks. If the list is not empty, the build is considered failed. The fix is to update macOS or to add another shim following the same pattern.

## Going back to the official CodexBar

Before replacing the app, `install.sh` puts the previous one into an archive: `~/Library/Application Support/ClaudeSwitcher/codexbar-backups/`. To go back: quit CodexBar, unpack the archive you need into `/Applications`, launch it. Both builds share the same settings and history.

## Fast loop for UI work

```bash
./dev.sh        # incremental debug build, run in place of the installed app (about 40 seconds)
```

The debug build runs from `../CodexBar/CodexBar.app` with its own settings (`com.steipete.codexbar.debug`); the app in `/Applications` is only quit, not replaced. Go back with `pkill -x CodexBar; open -a /Applications/CodexBar.app`.

The app can drive its own menu, so hover and paging can be checked from a screen recording without touching the pointer. It works in both builds (`com.steipete.codexbar` for the installed one):

```bash
defaults write com.steipete.codexbar.debug lemelsonSelfTest -bool YES   # then open the menu
```

After the menu opens it pages through the whole line of pages with arrow keys and swipes (including two quick presses and a reversal mid-slide), points at the two Antigravity limit rows on the last page (the detail panel appears and morphs between them and back), at the two Codex limit rows on the way back, hovers two Claude limit rows and closes, about sixteen seconds in all. With `lemelsonSelfTestDetail -bool YES` set as well it runs a plan for the detail panel instead (Spend account rows, the account switcher, pointing at a week). Other plans, each a key set to YES next to `lemelsonSelfTest`: `lemelsonSelfTestTour` (every page, the cache rows, the Spend switches, the Status Page panels; on Claude Limits, Spend, Value and Classic and on Antigravity and Codex it lists the page's rows with their heights, the native rows under it and "menu content N pt, window N pt, visible N pt" (Limits, Spend, Antigravity and Codex must fit the visible height without scrolling; Value and Classic are longer and scroll), and on Antigravity and Codex points at the Plan worth row, on Codex also both limit rows, the credits and limit reset credits facts, the cache row and the footer), `lemelsonSelfTestValue` (every switch combination of the Value page, with row heights logged against what the content wants), `lemelsonSelfTestPeek` / `lemelsonSelfTestPeekClick` (one row and a click on it), `lemelsonSelfTestStart` (the page to start on), `lemelsonSelfTestOverview` (with `lemelsonSelfTestStart overview`: the Overview rows and "menu content N pt, window N pt, visible N pt", a hover on the spend chart that must open the day panel, a press, drag and release across 40 % of the plot that must leave a zoom, one Escape that must clear it with the menu still open, then every Overview row with a detail, its panel size logged; the tour also visits Overview between Codex and Claude and logs its height). Reset them to NO afterwards. `lemelsonSelfTestAdopted` walks the adopted chart rows of the current page and `lemelsonSelfTestBrowse` presses "Browse sessions" inside the panel. These plans post mouse events to the app's own queue (`NSApp.postEvent`), so they take the real path through the menu's tracking loop; nothing is posted to the system and the pointer does not move. The keys and swipes are built inside the app and handed to the same functions the real ones reach; nothing is posted to the system. Turn it off with `-bool NO`. With `lemelsonSelfTestHover -bool YES` set as well, it only hovers the first and the last limit row of the page the menu opened on (to look at how the detail panel joins the menu). Each step is logged:

```bash
/usr/bin/log show --last 2m --info --predicate 'subsystem == "com.steipete.codexbar.lemelson"'
```

The runner and the plan selection are in `Sources/CodexBar/Lemelson/StatusItemController+LemelsonSelfTestRunner.swift`, the plans in `+LemelsonDetailSelfTest.swift`.

## Hover panel

Details are not shown in native submenus. A native submenu waits for the pointer to settle, closes the previous one and opens the next from scratch. One window next to the menu is used instead (`Sources/LemelsonUsageKit/UsageHoverPanel.swift` for the shape and style, `UsageHoverPanelController.swift` for the window): it appears at once, slides to the hovered row and cross-fades its content, and the content of every row is built ahead of time. In the fused shape the panel reaches 2 pt under the menu to cover its hairline border, and its concave corners end on that border (`UsageHoverOutline.joinInset`), not past it.

The panel is used on every tab:

- Rows the fork builds itself (limit rows, the account rows in Spend) register a detail with `lemelsonRegisterDetail(UsageHoverEntry)`.
- The author's chart submenus (Usage history, Plan Usage, cost, credits, usage breakdown, storage; on Claude Spend and Classic, Codex, Gemini, Antigravity and Overview) are adopted in `StatusItemController+LemelsonDetailAdoption.swift`. After the menu is populated, each such submenu is detached from its row and a private copy of it is filled for the panel; before the next populate and when the menu closes, the original is put back, so the author's reconcile and tab caches always see their own menu. A plain row keeps its arrow through an empty stub submenu, which draws the arrow and never opens.
- Status Page submenus that list components (Claude, Codex) are adopted the same way; the panel ends with an "Open Status Page" row, and a click on the row itself still opens the browser. A Status Page that is only a link (Antigravity) and the command submenus stay native.

`defaults write com.steipete.codexbar lemelsonDetailPanel native` brings the native submenus back (Limits keeps the panel).

The panel takes the pointer although the menu is tracking. `LemelsonMenuEventFilter` sees mouse and scroll events dequeued in the menu's tracking loop; when the pointer is over the panel they are routed to it and kept from the menu, which then neither closes nor drops the row's highlight. Traps found on the way:

- Forwarded mouse-moved events do not fire tracking areas. The panel sends enter, move and exit to the views itself.
- `NSWindow.sendEvent` is not enough for a segmented control, which reads the hardware button state. The panel selects the segment itself.
- Controls run their own tracking loops, and those also pass through the filter: while an event is being forwarded, the filter lets everything through.
- The visible rect of a SwiftUI-hosted view can reach past its bounds; it is clipped to the bounds.
- Something resets a hosting view's origin, so each row of a stacked submenu copy sits in its own wrapper view.
- NSMenu never sees the pointer leave a row while it is over the panel, so the panel runs its own grace delay before hiding.
- "Browse sessions" walks `supermenu` to find the menu to close; a copy has none. `SessionUsageMenuAction.swift` asks the panel which menu it belongs to.

Two settings change how it looks, without a rebuild (use `com.steipete.codexbar.debug` for the debug build):

```bash
defaults write com.steipete.codexbar lemelsonHoverStyle morph    # instant | morph | spring
defaults write com.steipete.codexbar lemelsonHoverShape fused    # fused | card
```

In Spend, the totals for 30 / 60 / 90 / 180 days are computed once per menu build (`UsageSpendDigest`), and all four period layers are laid out at once; the period buttons only change which layer is visible. Rebuilding the chart and the model rows on each press took long enough to be seen.

Switching between Limits, Spend and Value attaches rows that were rendered ahead of time (shortly after the menu opens, and kept when a mode is left). A freshly created SwiftUI row paints a frame late, which used to read as a blink.

## Paging

`Sources/CodexBar/Lemelson/StatusItemController+LemelsonPaging.swift` holds the line of pages, the swipe recognizer and the sideways slide. A swipe works with the pointer anywhere over the menu.

A page turn mixes the menu's own rows with pictures of pages:

- The rows above the page (the tab row, and the mode row inside Claude) are never covered or copied. They are the menu's own and change when the menu rebuilds.
- The page that leaves starts moving as the real rows (their layers get an additive animation). When the menu rebuilds its rows for the new page, a picture of the old page takes over at the same position and keeps moving in a strip inside the menu window.
- The page that arrives is the real rows of the new page, moved in with the same animation as the strip. If it was shown before, its picture travels beside the old page from the first frame and is replaced by the real rows as soon as they exist.
- A page that has no picture yet is not slid to: the strip would bring in an empty half. The leaving page stays where it is until the new rows are placed (40–160 ms in the debug build, Spend the longest), then both slide together. The log line is `begin a -> b index=N, waits for rows`.
- The picture of the leaving rows is taken in `menuWillPopulate`, before the rebuild touches them: the rebuild puts an intermediate state on screen (`flushHostedMenuRowRendering`) long before it returns, and without the picture the old page was gone for that time.
- The menu's table makes the layers of its rows only when it lays out and draws. Rows placed right after the window's resize have none yet, so `placeRows` forces a layout and a display pass before it starts the rows moving; otherwise the new page appears in place.
- Pictures live as long as the app. Ten minutes after the menu closes they are compressed to PNG (`archivePictures`) and decoded again in the background when the menu opens, so a page is "new" once per launch. A picture older than 30 s is taken again 0.3 s after a turn ends, while the page stands still.
- Another press during the slide does not restart it: the strip gets a new target and continues from where it is. Going back mid-slide turns it around.
- When the strip arrives there is nothing to swap: the real rows are already in place, and only the pictures of the pages that left are removed.
- The menu window changes height over 0.3 s (`lemelsonHeightDuration`, seconds). NSMenu sets the new height in one step; the resize notification arrives before anything is drawn, the step is taken back there, and a timer walks the window (and its glass background, a child window) to the new height. The top edge stays put.

Things that went wrong on the way, worth knowing before changing this file:

- A picture of a page is brighter than the page. `cacheDisplay` cannot render vibrancy, so the dimmed secondary text (unselected tabs, captions) comes out whiter. Any picture standing still next to real rows, or swapped for them at rest, reads as a flash. That is why pictures are only ever shown in motion and the top rows are never pictures.
- A layer keeps implicit animations unless told otherwise. Adding a sublayer cross-faded the whole strip for 0.25 s. Every layer here gets `noImplicitActions`.
- The pictures must be in the same window as the rows. Two windows are two render contexts and do not always commit on the same frame.
- Do not toggle `isHidden` on the host view: AppKit fades it back in. The copy is excluded from the snapshot by setting its layer opacity to 0 for the duration of `cacheDisplay`.
- The rows under the row that stays are hidden with a layer mask on the menu's clip view, not by hiding views: the new rows appear in the window before they can be reached one by one. A view's layer uses the view's own coordinates (the clip view is flipped), so the mask rectangle is not flipped again.
- The rows are moved as layers (the row views of the menu's table), found by walking the layer tree under the row that stays.
- The menu's scroll view keeps its own height and sits at the bottom of the window. While the window is between two heights it gets the taller of the two and is held against the top, and is given the window's bounds again at the end. With the old height it left the part a growing window uncovers empty, and the rows there appeared in one frame.
- NSMenu decides about its scroll arrows (two `NSImageView`s, direct subviews of the window's content view) after its own resize, when the transition has already put the window back to the old height. A page that grows to a height it fits in (Gemini → Antigravity) therefore kept the "more below" arrow over its last row. While the rows fit the height the window is going to, the transition hides the arrows on every step and at the end; on a page that really scrolls NSMenu shows them again itself. NSMenu adds the arrow views anew in the middle of each rebuild, after its resize, so until the rebuild returns nothing can reach them: on the first visit to a taller page the arrow can show over the still empty page for 0.1–0.2 s (an `alphaValue` or a layer mask set when the page begins to turn does not help, the views do not exist yet). In the self-test, pointing at no row logs the window's view tree (`tree …` lines), arrows included.
- A mode change used to clear the cached content of the Overview and Codex tabs, and the author's warm-up rebuilt them right after each switch (0.2–0.3 s on the main thread). Now only the Claude entries are dropped, and warm-ups wait while pages are turning.

The duration can be changed without a rebuild (seconds, 0.06–1, default 0.24):

```bash
defaults write com.steipete.codexbar lemelsonSlideDuration -float 0.3
```

While a menu is open, AppKit reads events in its own loop, and a menu never sees scroll events at all. `LemelsonMenuEventFilter.swift` therefore replaces `-[NSApplication nextEventMatchingMask:untilDate:inMode:dequeue:]` and looks at events dequeued in event-tracking mode. This is app-wide method replacement: fine for this build, not something to send upstream as is.

With a submenu open, the arrows are left to the menu (they close and open submenus as usual).

Vertical scrolling of a menu taller than the screen is the system's. An experimental smoother version is off by default:

```bash
defaults write com.steipete.codexbar lemelsonSmoothScroll -bool YES
```

## Layout preview

```bash
cd ../CodexBar && swift build -c debug --product LemelsonPanelPreview && .build/debug/LemelsonPanelPreview /tmp/panel.png --dark
```

The `--warm` flag applies a warm screen tint (like Night Shift); `--only limits|spend|value|details` renders a single screen.

## Signing

The build is signed with an "Apple Development" certificate (which one is set in `signing.env`, a file that is not committed to the repository). The persistent signature is needed so that macOS does not ask again for Keychain access after every rebuild. When the certificate expires, put the new one into `CODEXBAR_LOCAL_IDENTITY` in `signing.env`.
