# Auto-align & match-confidence for the compare player

**Date:** 2026-07-15
**Status:** Approved (design), ready for implementation plan
**Scope:** Option A — a self-contained, review-time aid. Nothing is persisted; the
scan verdict (S1–S6) is untouched.

## Problem

The review UI's compare panel plays the local file next to a 30-second iTunes
preview so a human can A/B them before accepting a candidate. But Apple's preview
is a ~30s slice from *somewhere in the middle* of the track, and that offset is not
exposed by the iTunes Search API. To line the two up you currently nudge manually
with `[` / `]` (±5s) — slow and annoying. There is also no read on *how well* the
two actually match, which is exactly the information that would catch a wrong
candidate (a cover, a live take, the wrong song).

## Goal

When a candidate with an iTunes preview is selected, automatically:

1. Find where in the local file the preview sits, and seek the local player there.
2. Show a confidence badge for how well the two audios match.

This replaces the manual nudge and turns "do these actually match?" into a glanceable
signal — without changing the stored verdict or the scan pipeline.

## Non-goals (YAGNI)

- **No persistence.** The computed `{offset, confidence}` is not written to the DB.
  (A cache is a possible later follow-on — explicitly out of scope here.)
- **No pipeline/verdict change.** This is not a new S7; verify.py and scan.py are
  untouched. Alignment is a review-time, on-demand computation only.
- **No new audio source.** Alignment uses the existing local file + the existing
  iTunes preview. (Background: iTunes is the only source that serves free,
  streamable preview audio; MusicBrainz/AcoustID are pure metadata, and Shazam only
  returns an Apple Music deep link to the same catalog.)

## Architecture

Three isolated units, sized so the hard-to-test parts (audio decode, HTTP) are thin
and the real logic is pure and unit-testable.

### `align.py` (new module)

- `find_offset(chroma_ref, chroma_query) -> (offset_frames, confidence)`
  - **Pure numpy. No audio decode, no network.** Slides the query (preview) chroma
    over the reference (local file) chroma and returns the lag of peak similarity
    plus a normalized confidence in `[0, 1]`.
  - Confidence rewards a *sharp* peak, not just a high one: a sharp, high peak means
    the same recording; a flat similarity landscape means a different recording.
    (Concretely: peak similarity normalized against the background/second-best, so a
    single decisive alignment scores high and a diffuse match scores low. Exact
    normalization is an implementation detail for the plan; the contract is a
    monotonic-ish 0–1 score.)
  - This is where the logic lives and it is fully unit-testable with synthetic arrays.

- `chroma_of(source, sr=22050, hop=512) -> np.ndarray`
  - Decode `source` (a file path, or preview bytes/temp file) to mono at `sr`, compute
    a 12-bin chromagram (`librosa.feature.chroma_cqt` or `chroma_stft`).
  - Chroma is chosen deliberately: it survives EQ / bitrate / master differences
    between the user's file and Apple's AAC preview, where raw-waveform correlation
    would not.

- `align_audio(local_path, preview_source) -> {"offset_sec": float, "confidence": float}`
  - Orchestrator: `chroma_of` both sides, `find_offset`, convert frames→seconds
    (`offset_sec = offset_frames * hop / sr`).
  - `librosa` is imported *inside* this module's functions (lazy), so importing
    `review.py` never hard-depends on librosa/ffmpeg.

### `review.py` (new endpoint)

`GET /api/align?path=<local>&preview=<url>`

- Validates `path` is a known track in the DB and exists on disk (same guard as
  `/api/audio`). Rejects unknown paths.
- Downloads the preview URL (short timeout), runs `align_audio`, returns:
  `{"ok": true, "offset": <sec>, "confidence": <0..1>, "label": "strong|weak|none"}`.
- On any failure returns `{"ok": false, "reason": "<no_preview|download_failed|decode_failed|unavailable>"}`.
- librosa is lazy-imported (via `align.py`); if librosa or ffmpeg is missing the
  endpoint returns `ok:false, reason:"unavailable"` instead of 500ing.
- Disabled in demo mode (no real local audio).

### `review.html` (frontend)

- In `loadCompare(...)`: after the preview is loaded, and only when NOT in demo mode
  and a preview exists, fire a **debounced** `fetch('/api/align?...')` guarded by an
  `AbortController`. Moving to the next card (or selecting another candidate) aborts
  the in-flight request; stale responses are ignored.
- On `ok:true`: set `localAudio.currentTime = offset` and render the confidence badge.
- On `ok:false`: render an "align unavailable" state; the existing `[` `]` nudge and
  `⇄ Cue` buttons remain as manual fallback (they are not removed).
- Show a small "aligning…" spinner in the compare panel while the request is in flight.

## Confidence → badge

`find_offset` returns a 0–1 confidence. Two tunable thresholds map it to a badge:

| Badge | Range (initial) | Meaning |
|-------|-----------------|---------|
| 🟢 strong | `>= 0.7` | same recording |
| 🟡 weak | `0.4 – 0.7` | aligned, but low similarity |
| ⚪ none | `< 0.4` | couldn't find an overlap — likely a different recording |

Behavior is **always cue + label honestly**: the player always seeks to the best-guess
offset, and the badge states how much to trust it. Thresholds are placeholders to be
tuned against the real library once running.

## Edge cases

- **Demo mode / no local audio:** feature off.
- **Candidate has no preview:** skip (nothing to align to).
- **Preview download fails / times out:** `ok:false, reason:"download_failed"`; manual
  nudge remains.
- **Decode fails (corrupt file, ffmpeg missing) / librosa missing:** `ok:false,
  reason:"decode_failed"` or `"unavailable"`; manual nudge remains.
- **Card switched mid-compute:** `AbortController` cancels; stale result discarded.

## Dependencies (relevant to the future `.app`)

- Adds `librosa` (pulls numpy/scipy) — **dev/review dependency only**.
- librosa decodes WAV/FLAC natively but **MP3/M4A/AAC (including Apple's preview)
  need `ffmpeg`**. So auto-align requires `ffmpeg` on the machine (`brew install
  ffmpeg` for now).
- Both are **lazy-loaded**: demo, scan, review, and the eventual bundle all keep
  working without them; only auto-align degrades to "unavailable."
- When the notarized `.app` is built, vendor ffmpeg or document the brew requirement
  in PACKAGING.md. (Tracked as a packaging note, not part of this change.)

## Testing

- **`find_offset` (pure):** synthetic chroma arrays — plant a query slice at a known
  offset in a reference → assert the offset is recovered and confidence is high;
  plant an unrelated slice → assert low confidence. No audio or network.
- **`align_audio`:** generate a WAV with a known-offset excerpt → assert recovered
  offset within tolerance. (Skipped automatically if the decode backend is
  unavailable in the test environment.)
- **`/api/align`:** mock the preview download and `align_audio` → assert the JSON
  shape and each `ok:false` reason path (no_preview, download_failed, decode_failed,
  unavailable, unknown path).
