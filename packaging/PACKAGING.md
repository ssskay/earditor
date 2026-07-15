# Packaging Earditor as a native macOS app

Earditor's engine is Python and its UI is the local Flask review app. To ship it as a
double-click **Earditor.app** — no terminal, a real Dock icon — we wrap that same
server in a native window with [pywebview](https://pywebview.flowrl.com/) and bundle it
with [py2app](https://py2app.readthedocs.io/). **None of the review/verification logic
changes.**

## Why direct distribution, not the App Store

Earditor automates **Music.app** via AppleScript and reads/writes files across your
music library. The Mac App Store sandbox fights both of those. The correct channel is a
**Developer ID–signed, notarized `.app` distributed directly** (e.g. a GitHub Release).
That's the one thing an Apple Developer account is genuinely needed for here —
notarization so Gatekeeper doesn't block downloads — **not** the Store.

## 1. Run the wrapped app in dev

```bash
pip install -r requirements.txt -r packaging/requirements-app.txt
python3 packaging/app.py
```

A native window opens on the review UI. If it works here, the logic is bundle-ready.

## 2. Build the .app

```bash
python3 packaging/setup_app.py py2app       # → dist/Earditor.app
open dist/Earditor.app
```

The repository's **Build macOS app** GitHub Actions workflow runs this same build
on demand and for `v*` tags, then uploads `Earditor-unsigned-macOS.zip` as a smoke-
test artifact. Do not publish that unsigned artifact as the main download; sign and
notarize the final zip first.

## 3. Sign + notarize (needs your Apple Developer ID)

```bash
# Set your reverse-domain id in setup_app.py first (CFBundleIdentifier).
codesign --deep --force --options runtime \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" dist/Earditor.app

ditto -c -k --keepParent dist/Earditor.app Earditor.zip
xcrun notarytool submit Earditor.zip \
  --apple-id "you@example.com" --team-id TEAMID --wait
xcrun stapler staple dist/Earditor.app
```

Then zip the stapled `.app` and attach it to a GitHub Release.

## Runtime and release notes

- **Local state:** packaged builds store `config.json`, `earditor.db`, and logs in
  `~/Library/Application Support/Earditor`. They are not written into the signed app
  bundle and survive upgrades.
- **Resources:** the review template, demo fixtures, and example config are bundled
  under py2app's Resources directory. The in-app Scan button calls the bundled scan
  entry point directly; it does not depend on a loose `scan.py` file.
- **`fpcalc`:** AcoustID needs the Chromaprint `fpcalc` binary. Either require users to
  `brew install chromaprint`, or vendor a signed `fpcalc` into the bundle and point
  `$FPCALC` at it.
- **AcoustID key:** still per-user via `$ACOUSTID_API_KEY` or Keychain — never bake a
  key into a distributed build.
- **First launch:** macOS will prompt for Automation (Music) and Files-and-Folders
  access. A signed/notarized build makes those prompts trustworthy; an unsigned one
  triggers the scary "unidentified developer" wall.
- **shazamio / native deps:** verify `shazamio` and its async stack import cleanly from
  the frozen bundle; add anything py2app misses to `includes` / `packages`.

Before publishing, smoke-test the stapled app on a second Mac or a clean macOS user:
launch it from Finder, approve Music automation, scan a small copied library, accept
and undo one track, quit/relaunch, and confirm the queue persists.
