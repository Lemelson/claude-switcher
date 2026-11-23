#!/bin/bash
# Installs the built CodexBar into /Applications in place of the current one.
#
# The previous app is not deleted for good: it is packed into an archive in
# ~/Library/Application Support/ClaudeSwitcher/codexbar-backups. CodexBar settings and history
# are not touched (the app identifier stays the same: com.steipete.codexbar).
# Claude and Claude Switcher are not affected; only CodexBar is restarted.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NEW="$HERE/../CodexBar/CodexBar.app"
DEST="/Applications/CodexBar.app"
BACKUPS="$HOME/Library/Application Support/ClaudeSwitcher/codexbar-backups"
PLIST=/usr/libexec/PlistBuddy

[[ -d "$NEW" ]] || { echo "No build found: $NEW. Run build.sh first" >&2; exit 1; }
codesign --verify --deep --strict "$NEW"

mkdir -p "$BACKUPS"
if [[ -d "$DEST" ]]; then
    old_version="$($PLIST -c 'Print :CFBundleShortVersionString' "$DEST/Contents/Info.plist" 2>/dev/null || echo unknown)"
    old_build="$($PLIST -c 'Print :CFBundleVersion' "$DEST/Contents/Info.plist" 2>/dev/null || echo 0)"
    old_team="$(codesign -dv "$DEST" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
    kind="fork"
    [[ "$old_team" == "Y5PE65HELJ" ]] && kind="official"
    archive="$BACKUPS/CodexBar-$old_version-$old_build-$kind-$(date +%Y%m%d-%H%M%S).zip"
    echo "Archiving the current CodexBar ($old_version, $kind) -> $archive"
    ditto -c -k --keepParent "$DEST" "$archive"
    unzip -tq "$archive" >/dev/null
fi

if pgrep -x CodexBar >/dev/null; then
    echo "Quitting CodexBar..."
    pkill -x CodexBar || true
    for _ in $(seq 1 20); do
        pgrep -x CodexBar >/dev/null || break
        sleep 0.5
    done
    if pgrep -x CodexBar >/dev/null; then
        echo "CodexBar did not quit by itself, force-stopping it."
        pkill -9 -x CodexBar || true
        sleep 1
    fi
fi

echo "Installing the new build to $DEST"
rm -rf "$DEST"
ditto "$NEW" "$DEST"
codesign --verify --deep --strict "$DEST"

echo "Launching CodexBar..."
open -a "$DEST"
for _ in $(seq 1 20); do
    pgrep -x CodexBar >/dev/null && break
    sleep 0.5
done
sleep 6
if pgrep -x CodexBar >/dev/null; then
    new_version="$($PLIST -c 'Print :CFBundleShortVersionString' "$DEST/Contents/Info.plist")"
    echo "CodexBar $new_version is running (process $(pgrep -x CodexBar | head -1))."
else
    echo "ERROR: the new build did not start." >&2
    if [[ -n "${archive:-}" && -f "$archive" ]]; then
        echo "Restoring the previous CodexBar from $archive" >&2
        pkill -x CodexBar 2>/dev/null || true
        rm -rf "$DEST"
        ditto -x -k "$archive" /Applications/
        open -a "$DEST"
    fi
    echo "Crash report: ~/Library/Logs/DiagnosticReports/CodexBar-*.ips" >&2
    exit 1
fi

# Old archives: keep the five most recent.
# The newest archive of the official version is never removed: it is what we go back to if something goes wrong.
keep_official="$(ls -t "$BACKUPS"/CodexBar-*-official-*.zip 2>/dev/null | head -1)"
ls -t "$BACKUPS"/CodexBar-*.zip 2>/dev/null | tail -n +6 | while read -r old; do
    [[ "$old" == "$keep_official" ]] || rm -f "$old"
done
