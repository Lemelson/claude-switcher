#!/bin/bash
# Builds the modified CodexBar from the sources in ../CodexBar (branch lemelson/accounts).
# Result: ../CodexBar/CodexBar.app, signed with a persistent developer certificate.
#
# The persistent signature is needed so that macOS does not ask again for Keychain access
# after every rebuild. Which certificate to use is set in signing.env (see signing.env.example).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../CodexBar"
# The signing certificate is personal data: it lives in signing.env, which is not committed to the repository.
[[ -f "$HERE/signing.env" ]] && source "$HERE/signing.env"
IDENTITY="${CODEXBAR_LOCAL_IDENTITY:-}"
TEAM="${APP_TEAM_ID:-}"
if [[ -z "$IDENTITY" || -z "$TEAM" ]]; then
    echo "No signing data. Copy signing.env.example to signing.env and fill in your certificate" >&2
    echo "(list them with: security find-identity -v -p codesigning)." >&2
    exit 1
fi
LOG="$HERE/build.log"

cd "$SRC"
echo "Building CodexBar $(grep -E '^MARKETING_VERSION=' version.env | cut -d= -f2) from branch $(git branch --show-current)..."
echo "Log: $LOG"

if ! APP_TEAM_ID="$TEAM" \
    CODEXBAR_SIGNING=adhoc \
    CODEXBAR_LOCAL_IDENTITY="$IDENTITY" \
    CODEXBAR_SKIP_LAUNCH_SMOKE=1 \
    Scripts/package_app.sh release >"$LOG" 2>&1; then
    echo "BUILD FAILED. Last lines of the log:" >&2
    tail -30 "$LOG" >&2
    exit 1
fi

APP="$SRC/CodexBar.app"
codesign --verify --deep --strict "$APP"
# An Xcode newer than the installed macOS produces a build that crashes at launch: catch that right away.
python3 "$HERE/check_symbols.py" "$APP"
echo "Done: $APP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|TeamIdentifier|Authority" | head -4 || true
