# Custom CodexBar build

`/Applications/CodexBar.app` is a CodexBar built from the author's sources (steipete/CodexBar) with our changes. The sources are in `../CodexBar`, branch `lemelson/accounts`, based on the official version `v0.85.0`.

## What is changed compared with the official CodexBar

The Claude tab has four modes, switched with buttons at the top of the menu:

| Mode | What it shows |
|---|---|
| Limits | The limits of all three accounts (a VPN-only account shows its last reading, coloured by age: orange from 12 hours, red from a day or after the window reset; an account that is not read because the VPN is on says "Paused while VPN is on"): how much I used (Claude Code on this Mac), how much went to other Claude Code, chat and Cowork; a pace mark on every bar (green: in reserve, red: in deficit); under the weekly limit, how many full 5-hour sessions are left and how many fit before the reset (only for accounts that have a 5-hour window); a warning when the limit will run out before the reset; on hover over a row, a chart of the window with the forecast, the exact reserve percent and details |
| Spend | My spend on this Mac: today, 7 days and the selected period (30 / 60 / 90 / 180 days), a daily chart with hover, split by accounts or by models (the switch is remembered like the period), four models with tokens (Opus, Fable, Sonnet, Haiku, each with its own colour), accounts (on hover: each week as a part of the weekly limit, "you" against "others", models and the account switcher). The standard Cost and Usage history stay below |
| Value | What the subscription is worth at API prices: the most the limit can buy on Opus alone and on Fable alone, and my real use ("Real use, 30 days"); whether the limit changes, by month (bars: the average week of a calendar month × 4.35, faded while under two weeks of it are measured) or by week, in Opus dollars, at my mix or in Fable dollars, with the values shown when the pointer is over the chart. All switches on the page are remembered |
| Classic | The standard CodexBar card, unchanged |

The menu opens on the provider tab and, on Claude, in the mode that was shown last (`lemelsonPanelLastMode`, kept across restarts). The pin to the right of the buttons overrides that: a pinned mode is used every time the Claude tab opens; clicking the filled pin unpins.

A low row "Prompt cache · N% reused" sits above the first action row on every Claude mode and on Antigravity (`UsageCacheRowView`); its detail panel says in plain words what was reused, what was paid in full and what that saved, per account or per model. It is absent when the summary has no cache numbers.

The Antigravity tab is one page of ours (`UsageAntigravityPage`, beta) in place of the stock card and the stock rows under it (Plan Usage, Cost: the stock Cost counts copied conversations twice): the 5-hour and weekly Gemini quota with a pace mark and the detail panel, spend at Gemini API prices with the period switch, the value of the plan in the form of Claude's Value card (hidden behind a one-line reason until the collector calls the estimate confident), and a footer. Under the weekly limit: how many full 5-hour sessions are left, or "measuring, X of Y windows seen" until the collector has measured enough windows. It needs the `antigravity` block of `state.json`; without it the stock card stays. `defaults write com.steipete.codexbar lemelsonAntigravityPage stock` brings the stock tab back (next menu build, no restart).

Every fresh Antigravity reading the app gets is appended to `~/Library/Application Support/ClaudeSwitcher/usage/antigravity-quota.jsonl` (when a value changed or ten minutes passed; contract: `usage/ANTIGRAVITY-STATE.md`, "Live readings file"), which the collector imports. When the page is shown and the store's reading is older than two minutes, the app fetches Antigravity again (about half a minute; the footer says it is refreshing); a new line while the menu is open runs the collector and rebuilds the page.

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

The author's code is touched in these places, each marked `lemelson:`: `Package.swift` (the Lemelson* targets and the dependency on LemelsonCompat), `Scripts/package_app.sh`, `StatusItemController+Menu.swift`, `+MenuTracking.swift`, `+ProviderNavigation.swift`, `+ProviderSwitcher.swift` and `+MenuSwitcherWarmup.swift` (menu insertion, paging, and holding the tab warm-up back while a page is turning), `StatusItemController.swift` (watching the store for fresh Antigravity readings), `+MenuPresentation.swift` (a row with no registered controls skips the hit test that measured its whole content on every pointer move; a row with controls hands a click to the AppKit control under the pointer, which SwiftUI's hosting view otherwise swallowed), `SessionUsageMenuAction.swift` (closing the right menu from a chart shown in the detail panel), `ClaudeProviderDescriptor.swift`, `SettingsStore.swift`, and one test expectation in `SettingsStoreTests.swift`.

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

After the menu opens it pages through the whole line of pages with arrow keys and swipes (including two quick presses and a reversal mid-slide), points at the two Antigravity limit rows on the last page (the detail panel appears and morphs between them and back), hovers two Claude limit rows and closes, about fourteen seconds in all. With `lemelsonSelfTestDetail -bool YES` set as well it runs a plan for the detail panel instead (Spend account rows, the account switcher, pointing at a week). Other plans, each a key set to YES next to `lemelsonSelfTest`: `lemelsonSelfTestTour` (every page, the cache rows, the Spend switches, the Status Page panels), `lemelsonSelfTestValue` (every switch combination of the Value page, with row heights logged against what the content wants), `lemelsonSelfTestPeek` / `lemelsonSelfTestPeekClick` (one row and a click on it), `lemelsonSelfTestStart` (the page to start on). Reset them to NO afterwards.; `lemelsonSelfTestAdopted` walks the adopted chart rows of the current page and `lemelsonSelfTestBrowse` presses "Browse sessions" inside the panel. These plans post mouse events to the app's own queue (`NSApp.postEvent`), so they take the real path through the menu's tracking loop; nothing is posted to the system and the pointer does not move. The keys and swipes are built inside the app and handed to the same functions the real ones reach; nothing is posted to the system. Turn it off with `-bool NO`. With `lemelsonSelfTestHover -bool YES` set as well, it only hovers the first and the last limit row of the page the menu opened on (to look at how the detail panel joins the menu). Each step is logged:

```bash
/usr/bin/log show --last 2m --info --predicate 'subsystem == "com.steipete.codexbar.lemelson"'
```

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
