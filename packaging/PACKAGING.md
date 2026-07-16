# Packaging Earditor as a native app

Earditor's engine is Python and its UI is the local Flask review app. To ship it as a
double-click app — no terminal, a real Dock/taskbar icon — we wrap that same server in
a native window with [pywebview](https://pywebview.flowrl.com/). **None of the
review/verification logic changes.**

| | macOS | Windows |
|---|---|---|
| Bundler | [py2app](https://py2app.readthedocs.io/) — `packaging/setup_app.py` | [PyInstaller](https://pyinstaller.org/) — `packaging/setup_win.spec` |
| Web view | WebKit | WebView2 |
| Output | `dist/Earditor.app` | `dist/Earditor/Earditor.exe` (one-dir) |
| Signed | Developer ID + notarized (§3) | Unsigned for v1 — SmartScreen warns (§4) |
| Music.app | Full integration | Files-only — see [CONFIGURATION.md](../docs/CONFIGURATION.md#platform-support) |

py2app is macOS-only, which is why the two builds need separate configs. They must
stay behaviorally identical — **`--demo` works in both**, and that's the support answer
for "is it broken, or is it my setup?"

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

## 4. Build for Windows

```powershell
pip install -r requirements.txt -r packaging\requirements-app.txt pyinstaller
pyinstaller packaging\setup_win.spec --noconfirm
dist\Earditor\Earditor.exe --demo        # smoke-test before shipping
```

Zip `dist\Earditor\*` as `Earditor-windows.zip`. CI does all of this on every `v*`
tag (`.github/workflows/build-windows.yml`), including vendoring `fpcalc.exe`.

- **One-dir, not one-file.** One-file unpacks to a temp directory on every launch:
  slow, and it moves the vendored-fpcalc lookup around.
- **WebView2** is preinstalled on Windows 11. Windows 10 users may need the
  [Evergreen runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **The exe is unsigned.** A Windows code-signing certificate is a separate purchase
  and out of scope for v1, so SmartScreen shows "Windows protected your PC" on first
  launch — users click **More info → Run anyway**. Document this on the Release page.
- **No auto-align on Windows for v1**: librosa/ffmpeg aren't bundled, so the badge
  reads "align n/a" and the manual `[` `]` nudge still works.
- **Frozen imports** are the classic PyInstaller failure and a green test suite will
  never catch them — shazamio's async stack and pykakasi's data files both need
  explicit `hiddenimports`/`datas` (already in the spec). The CI smoke-test launches
  the real exe for exactly this reason.

## Vendored `fpcalc`

AcoustID needs Chromaprint's `fpcalc`, and hunting for that binary is the #1 setup
friction. Both builds vendor it — Chromaprint's terms permit redistribution:

- **macOS:** the build workflow copies it from the Homebrew binary (so the arch
  matches the runner) into `packaging/vendor/fpcalc`. It's a Mach-O binary inside the
  bundle, so it must be signed with the app — the `--deep` sign in §3 covers it.
- **Windows:** the build workflow downloads the official Chromaprint release zip into
  `packaging/vendor/fpcalc.exe`.

`config.ensure_fpcalc()` points `$FPCALC` at it at startup. An explicit `$FPCALC`
always wins, and a source checkout with `fpcalc` on PATH is unaffected. If the vendored
binary is missing the build still succeeds and prints a warning — AcoustID simply stays
disabled, exactly as it does from source without a key.

`packaging/vendor/` is gitignored: these are fetched build artifacts, never committed.

## Runtime and release notes

- **Local state:** packaged builds store `config.json`, `earditor.db`, and logs in
  `~/Library/Application Support/Earditor` (macOS) or `%APPDATA%\Earditor` (Windows).
  They are not written into the app bundle and survive upgrades.
- **Version:** `__version__` in `config.py` is the single source of truth — it feeds
  `CFBundleShortVersionString` and the review UI footer. The release tag must match it.
- **Resources:** the review template, demo fixtures, and example config are bundled
  under py2app's Resources directory. The in-app Scan button calls the bundled scan
  entry point directly; it does not depend on a loose `scan.py` file.
- **`fpcalc`:** vendored into both builds — see [Vendored `fpcalc`](#vendored-fpcalc).
- **Auto-align is NOT bundled.** librosa drags numpy/scipy/numba in, roughly doubling
  the bundle for the most fragile things to freeze, so it's excluded from both builds
  and lives in `requirements-align.txt` for source installs. `align.py` lazy-imports
  it, so packaged apps report "align n/a" and the manual `[` `]` nudge still works.
  A future build could vendor librosa + a signed `ffmpeg`; it isn't worth it for v1.
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
