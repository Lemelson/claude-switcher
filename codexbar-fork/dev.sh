#!/bin/bash
# Fast loop for UI work: an incremental debug build of ../CodexBar, run in place of the installed app.
# About 1-2 minutes instead of a full release build. The debug build keeps its own settings
# (com.steipete.codexbar.debug); the installed app in /Applications is not replaced, only quit.
# Go back to the installed app:  pkill -x CodexBar; open -a /Applications/CodexBar.app
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../CodexBar"
[[ -f "$HERE/signing.env" ]] && source "$HERE/signing.env"
LOG="$HERE/build-debug.log"

cd "$SRC"
if ! APP_TEAM_ID="${APP_TEAM_ID:-}" \
    CODEXBAR_SIGNING=adhoc \
    CODEXBAR_LOCAL_IDENTITY="${CODEXBAR_LOCAL_IDENTITY:-}" \
    CODEXBAR_SKIP_LAUNCH_SMOKE=1 \
    Scripts/package_app.sh debug >"$LOG" 2>&1; then
    echo "Build failed. Last lines of $LOG:" >&2
    grep -E "error:" "$LOG" | head -20 >&2 || true
    tail -15 "$LOG" >&2
    exit 1
fi
python3 "$HERE/check_symbols.py" "$SRC/CodexBar.app"
[[ "${1:-}" == "--no-run" ]] && exit 0
pkill -x CodexBar 2>/dev/null || true
sleep 1
defaults write com.steipete.codexbar.debug selectedMenuProvider claude
open "$SRC/CodexBar.app"
echo "Debug CodexBar is running from $SRC/CodexBar.app"
