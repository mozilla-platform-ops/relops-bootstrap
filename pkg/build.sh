#!/bin/bash
# build.sh — assemble + sign the m4-bootstrap MDM pkg.
#
# Output: dist/m4-bootstrap-<version>.pkg (Developer ID Installer signed).
#
# Usage:
#   ./build.sh                     # default signing identity (Mozilla Corporation)
#   SIGN_IDENTITY="Developer ID Installer: <name> (<TEAMID>)" ./build.sh   # override (e.g. personal)
#   PKG_VERSION=2026.07.08 ./build.sh
#
# Optional post-sign notarize (requires xcrun notarytool credentials in the
# keychain profile named 'relops-notarize'):
#   NOTARIZE=1 ./build.sh

set -euo pipefail

PKG_ROOT=$(cd "$(dirname "$0")" && pwd)
PAYLOAD_DIR="$PKG_ROOT/payload"
SCRIPTS_DIR="$PKG_ROOT/scripts"
BUILD_DIR="$PKG_ROOT/build"
DIST_DIR="$PKG_ROOT/dist"

PKG_IDENTIFIER="com.mozilla.relops.m4-bootstrap"
PKG_VERSION="${PKG_VERSION:-$(/bin/date +%Y.%m.%d.%H%M)}"
PKG_NAME="m4-bootstrap"

COMPONENT_PKG="$BUILD_DIR/${PKG_NAME}-component.pkg"
UNSIGNED_PKG="$BUILD_DIR/${PKG_NAME}-${PKG_VERSION}-unsigned.pkg"
SIGNED_PKG="$DIST_DIR/${PKG_NAME}-${PKG_VERSION}.pkg"

# Discover signing identity if not provided. Default to the Mozilla Corporation org
# Developer ID Installer cert (team 43AQ936H96); override with SIGN_IDENTITY to use a
# different cert (e.g. a personal Developer ID Installer identity on the keychain).
if [[ -z "${SIGN_IDENTITY:-}" ]]; then
  SIGN_IDENTITY=$(/usr/bin/security find-identity -v -p basic \
    | /usr/bin/awk -F\" '/Developer ID Installer: Mozilla Corporation/ { print $2; exit }')
fi
if [[ -z "${SIGN_IDENTITY:-}" ]]; then
  SIGN_IDENTITY=$(/usr/bin/security find-identity -v -p basic \
    | /usr/bin/awk -F\" '/Developer ID Installer:/ { print $2; exit }')
fi
if [[ -z "${SIGN_IDENTITY:-}" ]]; then
  echo "ERROR: no Developer ID Installer identity found. Set SIGN_IDENTITY." >&2
  exit 1
fi
echo "Signing identity: $SIGN_IDENTITY"
echo "Package version:  $PKG_VERSION"
echo

# Clean + create build dirs
/bin/rm -rf "$BUILD_DIR"
/bin/mkdir -p "$BUILD_DIR" "$DIST_DIR"

# Ensure payload perms are correct BEFORE pkgbuild reads them. pkgbuild with
# --ownership recommended will still fix ownership to root:wheel, but modes
# come from the source tree.
/bin/chmod 0755 "$PAYLOAD_DIR/usr/local/sbin/m4-bootstrap.sh"
/bin/chmod 0644 "$PAYLOAD_DIR/Library/LaunchDaemons/com.mozilla.m4-bootstrap-entrypoint.plist"
/bin/chmod 0755 "$SCRIPTS_DIR/postinstall"

# Stage payload + scripts via `ditto --norsrc --noextattr` into a clean tree so
# HFS extended attributes (com.apple.provenance, com.apple.quarantine, etc.)
# don't get captured as AppleDouble `._*` sidecars in the pkg archive.
STAGING_PAYLOAD="$BUILD_DIR/staging/payload"
STAGING_SCRIPTS="$BUILD_DIR/staging/scripts"
/bin/mkdir -p "$STAGING_PAYLOAD" "$STAGING_SCRIPTS"
/usr/bin/ditto --norsrc --noextattr "$PAYLOAD_DIR" "$STAGING_PAYLOAD"
/usr/bin/ditto --norsrc --noextattr "$SCRIPTS_DIR" "$STAGING_SCRIPTS"

# 1. Build the component pkg
/usr/bin/pkgbuild \
  --root "$STAGING_PAYLOAD" \
  --identifier "$PKG_IDENTIFIER" \
  --version "$PKG_VERSION" \
  --scripts "$STAGING_SCRIPTS" \
  --ownership recommended \
  --install-location / \
  "$COMPONENT_PKG"

# 2. Wrap into a distribution pkg (required for productsign)
/usr/bin/productbuild \
  --package "$COMPONENT_PKG" \
  "$UNSIGNED_PKG"

# 3. Sign with Developer ID Installer
/usr/bin/productsign \
  --sign "$SIGN_IDENTITY" \
  "$UNSIGNED_PKG" \
  "$SIGNED_PKG"

# 4. Verify signature
echo
echo "=== signature ==="
/usr/sbin/pkgutil --check-signature "$SIGNED_PKG"

# 5. Optional notarize + staple
if [[ "${NOTARIZE:-0}" == "1" ]]; then
  echo
  echo "=== notarize ==="
  /usr/bin/xcrun notarytool submit "$SIGNED_PKG" \
    --keychain-profile relops-notarize \
    --wait
  /usr/bin/xcrun stapler staple "$SIGNED_PKG"
  /usr/sbin/spctl --assess --type install "$SIGNED_PKG" || true
fi

echo
echo "Signed pkg: $SIGNED_PKG"
echo
echo "Upload to SimpleMDM → Apps → Add App → macOS App → this file →"
echo "assign to the 'gecko-t-osx-1500-m4-no-sip' Assignment Group."
