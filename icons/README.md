# Claude Switcher icons

The app icon is `AppIcon.svg` (1024×1024). The drafts it was chosen from (`v1.svg` … `v5.svg`, the comparison pages and `make_preview.py`) are kept locally and are not part of the repository. `../build.sh` turns it into
`AppIcon.icns` before signing and sets `CFBundleIconFile`, so `./build.sh --install` does not wipe it.
