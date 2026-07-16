# Configuring Earditor

Everything configurable lives in **`config.json`**, next to the source (or in the
app's state directory for a packaged build — see [State directory](#state-directory)).
It is gitignored, so your library path never leaves your machine. Copy the example
and edit it:

```bash
cp config.example.json config.json
```

If `config.json` is missing, Earditor runs on the built-in defaults. Any key you
omit falls back to its default, so a three-line config is perfectly normal:

```json
{
  "music_path": "~/Music/Music/Media.localized/Music",
  "exclude_paths": ["Podcasts", "Audiobooks"]
}
```

**The AcoustID API key is never stored here.** It is read at runtime from
`$ACOUSTID_API_KEY`, or on macOS from the Keychain (service `acoustid`). See
[AcoustID](#acoustid-api-key).

---

## How your library must be laid out

Earditor assumes a **three-level** structure — the stock iTunes/Music layout:

```
<music_path>/<Artist>/<Album>/track.mp3
```

This isn't cosmetic. **The artist folder is treated as the uploader's identity**,
and two behaviors depend on it:

- **The S6 signal** compares the folder name against the fingerprinted artist. When
  they disagree, that's the tell that the folder owner is covering someone else's
  song — which is what drives the cover/original distinction in the review card.
- **The cover rule**: a track whose folder identity differs from the identified
  artist gets `cover_album_template` stamped on it rather than the catalog album.

A flat folder of loose files still scans and tags, but every folder-derived signal
goes quiet, so more tracks land in UNVERIFIED and you review more by hand. If your
library is organized `<Genre>/<Artist>/...` or similar, point `music_path` at the
level whose children are artists.

You do **not** need Music.app, or a Music.app library, to use Earditor — see
[Platform support](#platform-support). Point `music_path` at any folder tree.

---

## `config.json` keys

### Library

| Key | Default | What it does |
|---|---|---|
| `music_path` | `~/Music` | Root of the library. `~` and `$VARS` are expanded. Everything below is scoped to this tree. |
| `audio_extensions` | `[".mp3", ".m4a", ".flac", ".wav", ".aac"]` | Extensions the scanner will pick up. Case-insensitive. |
| `include_paths` | `[]` | Restrict scanning to these directories. Empty = all of `music_path`. See [Path filters](#path-filters). |
| `exclude_paths` | `[]` | Directories to skip entirely. Always beats `include_paths`. |

### Matching thresholds

| Key | Default | What it does |
|---|---|---|
| `thresholds.fuzzy_match` | `85` | rapidfuzz `token_set_ratio` cutoff for "these two strings mean the same thing". Lower = more permissive matching, more false agreement. |
| `thresholds.duration_pct` | `0.10` | Allowed duration drift as a fraction of track length. |
| `thresholds.duration_abs_sec` | `15` | Allowed drift in absolute seconds. The **larger** of this and `duration_pct` wins, so short tracks aren't judged too harshly. |
| `thresholds.acoustid_min_score` | `0.5` | AcoustID results below this score are ignored outright. |

### Politeness delays

Seconds to wait between calls, so the free APIs don't get hammered. Lowering these
risks rate-limiting rather than speed.

| Key | Default |
|---|---|
| `delays.shazam` | `1.5` |
| `delays.musicbrainz` | `1.1` |
| `delays.itunes` | `0.3` |

### Tagging behavior

| Key | Default | What it does |
|---|---|---|
| `playlist_name` | `Earditor — Tagged` | Music.app playlist that accepted tracks are added to. macOS only. |
| `music_app_integration` | `true` | Refresh accepted tracks in Music.app and add them to `playlist_name`. **Forced off on non-macOS** regardless of this setting. See [Platform support](#platform-support). |
| `cover_album_template` | `{artist} (Covers)` | Album stamped on a cover that has no real album, so it reads as fully tagged and doesn't loop back into the queue. `{artist}` = the cover artist. Set `""` to leave covers album-less (they will re-queue on rescan). |
| `original_album_template` | `{artist} (Originals)` | Same idea for an uploader's own original song (review option 3), which no catalog documents. |
| `stamp_cover_grouping` | `true` | Stamp `Grouping=Cover` (ID3 `TIT1` / MP4 `©grp`) on catalog-verified cover artists, so covers are smart-playlist-able while keeping the artist's real album and art. |

### Scan sizing

| Key | Default | What it does |
|---|---|---|
| `scan_queue_target` | `10` | "Scan for more" scans until this many **review cards** exist — not a fixed file count, since most pending files are already clean-tagged and resolve without producing a card. |
| `scan_max_files` | `500` | Safety cap so a `queue_target` run can't crawl the whole library hunting for cards. |

### LLM tie-breaker

| Key | Default | What it does |
|---|---|---|
| `use_llm_tiebreaker` | `false` | Optional local-LLM tie-breaker for UNVERIFIED tracks. Off by design — it is never part of the core verification pipeline. |
| `llm_model` | `qwen2.5:7b` | Model used when the tie-breaker is on. |

---

## Path filters

`include_paths` and `exclude_paths` narrow the library without moving files or
editing `music_path`.

**The rules:**

1. Each entry names a **directory**. Everything beneath it matches, at any depth.
2. A trailing `/**` or `/*` is accepted and ignored — `"Live"`, `"Live/*"`, and
   `"Live/**"` all mean the same thing.
3. **Relative entries resolve against `music_path`**; absolute paths also work.
4. **`exclude_paths` always wins** over `include_paths`.
5. Matching is **case-insensitive** on every platform.
6. **`music_path` is an implicit include.** A file outside it never matches, even
   if `include_paths` names it explicitly. This is what makes a stale registration
   from an older `music_path` drop out of the queue instead of being scanned.
7. Whole path components are compared, so `Keep` never matches `Keep Extra`.

The filters apply everywhere a file can enter the queue: the library walk, the
pending queue, `--triage`, and explicit `--files` arguments. An excluded folder is
invisible to all of them, not just the scanner.

### Worked examples

**Skip podcasts and audiobooks** (the common case):

```json
{
  "music_path": "~/Music/Music/Media.localized/Music",
  "exclude_paths": ["Podcasts", "Audiobooks"]
}
```

**Work through one artist at a time**, without touching the rest:

```json
{
  "include_paths": ["Ado", "YOASOBI"]
}
```

**Everything for an artist except their live albums** (exclude wins):

```json
{
  "include_paths": ["Ado"],
  "exclude_paths": ["Ado/Live Sessions"]
}
```

**An absolute path outside the library does nothing** — rule 6:

```json
{
  "music_path": "~/Music/Music/Media.localized/Music",
  "include_paths": ["/Volumes/Backup/Music"]
}
```

This scans **nothing**. To scan that drive, set it as `music_path`.

### Why your queue is shorter than you expected

Every run ends with a line like:

```
Skipped 412 pending file(s): 380 excluded by filters, 32 outside music_path.
```

- **excluded by filters** — matched `exclude_paths`, or missed a non-empty
  `include_paths`.
- **outside music_path** — registered under a different `music_path` in an earlier
  run and still sitting in the database. Harmless; they stay pending in case you
  point `music_path` back.

---

## `scan.py` flags

```bash
python3 scan.py [flags]
```

| Flag | What it does |
|---|---|
| `--limit N` | Scan at most N pending files. Filters are applied **before** the limit, so you always get a full N of eligible files. |
| `--queue-target N` | Keep scanning until N files reach the **review queue**, rather than scanning a fixed count. Most pending files are already clean-tagged and produce no card. |
| `--max-files N` | Hard cap on files scanned. Use with `--queue-target` so a run can't crawl forever. Defaults to `scan_max_files`. |
| `--triage` | Fast pass: read tags only — no fingerprinting, no API calls, no delays. Retires every already-clean file as ALREADY_TAGGED, then exits. Roughly half a real library is like this. **Run this first**; afterwards "pending" means "actually needs identifying". |
| `--files A B C` | Scan specific paths instead of walking. These obey path filters and `music_path` like everything else — rejected paths are reported and skipped. |
| `--music-path PATH` | Override the library root for this run. Also scopes which pending files are eligible, so it genuinely narrows the run. |
| `--no-acoustid` | Disable the AcoustID second fingerprint. Shazam alone still works. |
| `--rescan-tagged` | Also re-identify files that already have complete tags (normally skipped). |
| `--no-auto-accept` | Read-only: never write tags or touch Music.app. Topic-channel matches become review cards instead of auto-accepting. |
| `-v`, `--verbose` | DEBUG logging. |

A typical first run on a real library:

```bash
python3 scan.py --triage                     # retire the already-clean half
python3 scan.py --queue-target 20            # fill a review queue
python3 review.py                            # http://127.0.0.1:5000
```

---

## Platform support

| | macOS | Windows / Linux |
|---|---|---|
| Scan, verify, review UI | ✅ | ✅ |
| Write tags + embed cover art | ✅ | ✅ |
| Add accepted tracks to a playlist | ✅ | ❌ |
| Push artwork into Music.app | ✅ | ❌ |
| AcoustID key from Keychain | ✅ | ❌ — use `$ACOUSTID_API_KEY` |
| `refresh_artwork.py`, `fix_cover_albums.py` | ✅ | ❌ — exit with a message |

Off macOS, Earditor runs **files-only**: it does everything except talk to Music.app.
Accepting a track still writes tags and embeds art into the file itself — the
artwork push only exists because Music.app caches art in its own database and won't
re-read it. Where there's no such cache, there's nothing to push. The review UI says
*"tagged (not added to Music.app)"* on accept so this reads as intended rather than
as a failure.

`music_app_integration` is forced off when not on macOS. Setting it `true` there
would buy an AppleScript timeout per accept and nothing else.

---

## AcoustID API key

Optional. Earditor runs on Shazam alone; AcoustID is an independent second
fingerprint whose only job is to cross-check Shazam and catch the rare
"confidently wrong song". It powers the **S1** signal and unlocks the VERIFIED tier.

It is **never** written to `config.json` or any file. Resolution order:

1. `$ACOUSTID_API_KEY`
2. macOS Keychain, service `acoustid` (`security find-generic-password -s acoustid -w`)

```bash
export ACOUSTID_API_KEY="your-key"                      # any platform

security add-generic-password -s acoustid -a "$USER" -w  # macOS, persistent
```

Without a key, AcoustID is disabled and logs one warning. Everything else works.

`fpcalc` (from Chromaprint) must be on `PATH` for AcoustID to fingerprint, or point
`$FPCALC` at the binary directly.

---

## State directory

Where `earditor.db`, `config.json`, and `logs/` live:

| How you run it | Location |
|---|---|
| From source | Next to the source files |
| Packaged app (macOS) | `~/Library/Application Support/Earditor` |
| Packaged app (Windows) | `%APPDATA%\Earditor` |
| Packaged app (Linux) | `$XDG_DATA_HOME/Earditor`, else `~/.local/share/Earditor` |

A packaged app keeps state outside itself so an upgrade replaces only the app and
never your queue.

Override with **`$EARDITOR_DATA_DIR`** (useful for tests, or for keeping several
libraries side by side):

```bash
EARDITOR_DATA_DIR=~/earditor-state python3 scan.py --triage
```

`$EARDITOR_CONFIG` overrides the config file path alone.
