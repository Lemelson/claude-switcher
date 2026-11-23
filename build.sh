#!/bin/zsh
# Builds Claude Switcher.app and installs it into ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"

APP="build/Claude Switcher.app"
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 -target arm64-apple-macos14.0 \
  -o "$APP/Contents/MacOS/ClaudeSwitcher" Sources/main.swift

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Claude Switcher</string>
  <key>CFBundleDisplayName</key><string>Claude Switcher</string>
  <key>CFBundleIdentifier</key><string>com.lemelson.claudeswitcher</string>
  <key>CFBundleExecutable</key><string>ClaudeSwitcher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Icon: icons/AppIcon.svg -> AppIcon.icns (before signing, otherwise the signature breaks)
if [[ -f icons/AppIcon.svg ]]; then
  SET=build/AppIcon.iconset; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    rsvg-convert -w $s -h $s icons/AppIcon.svg -o "$SET/icon_${s}x${s}.png"
    rsvg-convert -w $((s*2)) -h $((s*2)) icons/AppIcon.svg -o "$SET/icon_${s}x${s}@2x.png"
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns"
fi

codesign --force --sign - "$APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p ~/Applications
  pkill -x ClaudeSwitcher 2>/dev/null || true
  rm -rf ~/Applications/"Claude Switcher.app"
  cp -R "$APP" ~/Applications/
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f ~/Applications/"Claude Switcher.app" || true
  mdimport ~/Applications/"Claude Switcher.app" 2>/dev/null || true
  open ~/Applications/"Claude Switcher.app"
  echo "Installed: ~/Applications/Claude Switcher.app"
fi
