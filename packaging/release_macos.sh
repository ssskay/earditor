#!/bin/bash
# release_macos.sh — sign, notarize, and staple Earditor.app for direct download.
#
# Earditor ships outside the App Store, so the .app has to carry a Developer ID
# signature and a stapled notarization ticket or Gatekeeper refuses to open it.
# CI builds the unsigned bundle; signing needs the private key, so it happens
# here, on the release machine.
#
#   1. Tag the release (git tag v1.0.1 && git push --tags) and let CI build.
#   2. Download the macOS artifact from that run and unzip it into WORKDIR:
#        mkdir -p dist/release && cd dist/release
#        unzip ~/Downloads/Earditor-unsigned-macOS.zip     # → Earditor.app
#   3. packaging/release_macos.sh
#   4. gh release create v1.0.1 dist/release/Earditor-macOS.zip <windows.zip>
#
# Usage: packaging/release_macos.sh [WORKDIR]      (default: dist/release)
#
# Prerequisites:
#   - A "Developer ID Application" certificate in the login keychain. Override
#     the identity with $EARDITOR_SIGN_IDENTITY, or list yours with:
#       security find-identity -v -p codesigning
#   - A stored notarytool credential profile named earditor-notary:
#       xcrun notarytool store-credentials earditor-notary \
#         --apple-id <you@example.com> --team-id <TEAMID> --password <app-specific-pw>
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKDIR="${1:-$REPO/dist/release}"
APP="$WORKDIR/Earditor.app"
ZIP="$WORKDIR/Earditor-macOS.zip"
ENTITLEMENTS="$REPO/packaging/entitlements.plist"
IDENTITY="${EARDITOR_SIGN_IDENTITY:-Developer ID Application: Sara Kay (AH785WYH3F)}"
PROFILE="${EARDITOR_NOTARY_PROFILE:-earditor-notary}"

[ -d "$APP" ] || { echo "No app bundle at $APP — unzip the CI artifact there first (see header)." >&2; exit 1; }
[ -f "$ENTITLEMENTS" ] || { echo "Missing $ENTITLEMENTS" >&2; exit 1; }
cd "$WORKDIR"

echo "== 1/6 signing nested libraries =="
# Inside-out: every nested Mach-O needs its own signature with a secure
# timestamp before the bundle itself can be sealed.
find Earditor.app \( -name '*.so' -o -name '*.dylib' \) -type f -print0 |
  xargs -0 -n 20 codesign --force --options runtime --timestamp --sign "$IDENTITY"

echo "== 2/6 signing embedded executables =="
# The vendored fpcalc and anything else executable in Resources.
find Earditor.app/Contents/Resources -type f -perm +111 -print0 | while IFS= read -r -d '' f; do
  if file "$f" | grep -q Mach-O; then
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f"
    echo "signed: $f"
  fi
done
# The bundled Python.framework, when py2app included one.
if [ -d Earditor.app/Contents/Frameworks ]; then
  find Earditor.app/Contents/Frameworks -type f -print0 | while IFS= read -r -d '' f; do
    if file "$f" | grep -q Mach-O; then
      codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f" || true
    fi
  done
fi

echo "== 3/6 signing main executable + app =="
for exe in Earditor.app/Contents/MacOS/*; do
  codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$exe"
done
codesign --force --options runtime --timestamp \
  --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" Earditor.app

echo "== 4/6 verifying =="
codesign --verify --deep --strict --verbose=2 Earditor.app

echo "== 5/6 smoke test (demo mode, HTTP check) =="
# Hardened runtime + a signed Python is exactly where a bundle breaks silently,
# so prove the app still SERVES before spending a notarization round-trip on it.
Earditor.app/Contents/MacOS/Earditor --demo &
PID=$!
SMOKE_OK=0
for _ in $(seq 1 30); do
  sleep 2
  if curl -sf http://127.0.0.1:5001/ | grep -qi earditor; then SMOKE_OK=1; break; fi
done
kill "$PID" 2>/dev/null || true
pkill -f 'Earditor.app/Contents/MacOS/Earditor' 2>/dev/null || true
if [ "$SMOKE_OK" != "1" ]; then
  echo "SMOKE_FAIL — no HTTP answer on :5001" >&2
  exit 1
fi
echo "SMOKE_OK — app serves the review UI under hardened runtime"

echo "== 6/6 notarizing + stapling =="
rm -f "$ZIP"
ditto -c -k --keepParent Earditor.app "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple Earditor.app
# Re-zip AFTER stapling: the ticket lives in the .app, and the zip submitted for
# notarization was made before it existed.
rm -f "$ZIP"
ditto -c -k --keepParent Earditor.app "$ZIP"

xcrun stapler validate Earditor.app
spctl --assess --type execute --verbose Earditor.app
echo "ALL_DONE → $ZIP"
