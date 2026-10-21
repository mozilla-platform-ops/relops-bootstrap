#!/bin/bash
# Install Xcode from a .xip in ronin-puppet-package-repo, matching the prod
# builder layout (/Applications/Xcode-<ver>.app + Xcode.app symlink, selected).
# Usage: sudo bash install-xcode.sh <https URL to Xcode_<ver>.xip> [expected build]
# Staged and run by `reprovision builder-upgrade` (orchestrator/builder_upgrade.py).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
URL=${1:?xip URL required}
WANT_BUILD=${2:-16F6}
WORK=/private/var/tmp/xcode-install
mkdir -p "$WORK"; cd "$WORK"
XIP=$WORK/$(basename "$URL")

echo "== free space: $(df -h / | awk 'NR==2{print $4}')"
if [ ! -f "$XIP" ]; then
    echo "== downloading $URL"
    curl -fL --retry 3 -o "$XIP.part" "$URL"
    mv "$XIP.part" "$XIP"
fi
ls -la "$XIP"
pkgutil --check-signature "$XIP" | head -4

echo "== expanding (takes a while)"
rm -rf "$WORK/expand"; mkdir "$WORK/expand"; cd "$WORK/expand"
xip --expand "$XIP"
APP=$(find "$WORK/expand" -maxdepth 1 -name "Xcode*.app" | head -1)
VER=$(defaults read "$APP/Contents/Info" CFBundleShortVersionString)
BUILD=$(defaults read "$APP/Contents/version" ProductBuildVersion)
echo "== expanded: $APP version=$VER build=$BUILD"
[ "$BUILD" = "$WANT_BUILD" ] || { echo "!! expected build $WANT_BUILD, got $BUILD"; exit 1; }

DEST=/Applications/Xcode-$VER.app
rm -rf "$DEST"
mv "$APP" "$DEST"
if [ -e /Applications/Xcode.app ] && [ ! -L /Applications/Xcode.app ]; then
    mv /Applications/Xcode.app "/Applications/Xcode.app.pre-$VER"
fi
ln -sfn "$DEST" /Applications/Xcode.app

xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -license accept
xcodebuild -runFirstLaunch
DevToolsSecurity -enable || true

echo "== result"
xcode-select -p
xcodebuild -version
xcrun --show-sdk-version
clang --version | head -1
rm -rf "$WORK/expand"
rm -f "$XIP"
echo "done"
