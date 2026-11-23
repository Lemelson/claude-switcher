#!/bin/zsh
# Builds claude-limits-helper and installs it next to the tracking data.
# Signed with a persistent developer certificate: the "Always Allow" permission in the Keychain
# survives a rebuild (with ad-hoc signing it would be lost every time).
set -euo pipefail
cd "$(dirname "$0")"

DEST="$HOME/Library/Application Support/ClaudeSwitcher/usage/bin"
mkdir -p build "$DEST"
swiftc -O -swift-version 5 -target arm64-apple-macos14.0 \
  -o build/claude-limits-helper limits-helper/main.swift -lsqlite3

IDENTITY=$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development/ {print $2; exit}')
if [[ -n "$IDENTITY" ]]; then
  codesign --force --sign "$IDENTITY" --identifier com.lemelson.claude-limits-helper build/claude-limits-helper
else
  codesign --force --sign - --identifier com.lemelson.claude-limits-helper build/claude-limits-helper
fi
cp build/claude-limits-helper "$DEST/claude-limits-helper"
echo "Built: $DEST/claude-limits-helper"
