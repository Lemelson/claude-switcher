#!/bin/zsh
# Renders the README screenshots in docs/img/ from made-up data, without launching CodexBar.
#
#   docs/make_screenshots.sh
#
# 1. docs/make_demo_state.py writes the synthetic summary to docs/demo/state.json (and the Antigravity
#    sample to usage/antigravity-state-sample.json). Nothing is read from the real data folder.
# 2. The off-screen renderer LemelsonPanelPreview (CodexBar/Sources/LemelsonPanelPreview) draws each page
#    from that file. --state is always passed: without it the tool reads the real summary.
#
# OUT=dir renders somewhere else (for a look before replacing docs/img).
#
# TZ=UTC: the generator writes UTC days and times, so the renderer reads them in UTC too. The images come
# out at the main screen's backing scale (2x on a Retina Mac); the script warns if it is not 2x.
set -euo pipefail

ROOT=${0:A:h:h}
STATE=$ROOT/docs/demo/state.json
OUT=${OUT:-$ROOT/docs/img}
export TZ=UTC

/usr/bin/env python3 "$ROOT/docs/make_demo_state.py" "$STATE" \
    --antigravity-sample "$ROOT/usage/antigravity-state-sample.json"
NOW=$(/usr/bin/env python3 "$ROOT/docs/make_demo_state.py" --print-now)

cd "$ROOT/CodexBar"
swift build --product LemelsonPanelPreview >/dev/null
BIN=$(swift build --show-bin-path)/LemelsonPanelPreview

mkdir -p "$OUT"
render() {
    local name=$1
    shift
    "$BIN" "$OUT/$name" --state "$STATE" --now "$NOW" "$@"
}

# The menu's pages.
render limits-dark.png --only limits --dark
render limits-light.png --only limits
render spend-dark.png --only spend --dark --breakdown accounts
render spend-models-dark.png --only spend --dark --breakdown models
render value-dark.png --only value --dark --drift month
# Hover panels of the active account: the 5-hour window with its forecast, the weekly rows by model,
# and the weekly window with the "who used it" split and the sessions left.
render details-dark.png --only details --dark --account 0
# The prompt cache row at the bottom of the menu and its panel.
render cache-dark.png --only cache --dark
# The Antigravity (Gemini) page and its hover panels.
render antigravity-dark.png --only antigravity --dark
render antigravity-details-dark.png --only antigravity-details --dark
# The three Claude pages side by side.
render hero-dark.png --only limits,spend,value --dark

# Every picture should be 2x: a 310 pt menu card plus 40 pt of margin is 700 px wide.
width=$(sips -g pixelWidth "$OUT/limits-dark.png" | awk '/pixelWidth/ {print $2}')
if [[ $width != 700 ]]; then
    print -u2 "warning: limits-dark.png is $width px wide, expected 700 (2x); render on a Retina screen"
fi
