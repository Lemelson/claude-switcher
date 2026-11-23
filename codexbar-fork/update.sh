#!/bin/bash
# Updates the modified CodexBar to the latest official version.
#
#   ./update.sh            move to the upstream author's newest version (the latest v* tag)
#   ./update.sh v0.86.0    move to a specific version
#
# What happens: the author's new sources are fetched, our changes (branch lemelson/accounts)
# are rebased on top of them, then the app is built and installed. If the author changed the same
# places in the code, the rebase stops with a conflict: then ask Claude for help (see README.md next to this file).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../CodexBar"
BRANCH="lemelson/accounts"

cd "$SRC"
[[ -z "$(git status --porcelain)" ]] || { echo "$SRC has uncommitted changes. Save them first." >&2; exit 1; }
git checkout -q "$BRANCH"
git fetch -q --tags origin

target="${1:-$(git tag --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)}"
current="$(git describe --tags --abbrev=0 2>/dev/null || echo '?')"
if [[ "$current" == "$target" ]]; then
    echo "Already on $target, nothing to update."
    exit 0
fi

echo "Moving our changes from $current to $target..."
git branch -f "backup/before-$target" "$BRANCH"
if ! git rebase --onto "$target" "$current" "$BRANCH"; then
    echo >&2
    echo "CONFLICT while moving our changes to $target." >&2
    echo "To cancel: git -C '$SRC' rebase --abort   (the previous state is in branch backup/before-$target)" >&2
    echo "Next: ask Claude to resolve the conflict in $SRC." >&2
    exit 1
fi

"$HERE/build.sh"
"$HERE/install.sh"
echo "CodexBar updated to $target with our changes."
