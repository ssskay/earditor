# Auto-align & Match-Confidence for the Compare Player — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a candidate with an iTunes preview is selected in the review UI, auto-cue the local file to the moment the preview matches and show a match-confidence badge — replacing today's manual `[` `]` nudging.

**Architecture:** A new pure-numpy `find_offset` (chroma cross-correlation) is the core, wrapped by a thin librosa decode layer (`align_audio`), exposed by a lazy-loading `GET /api/align` endpoint, and called from `loadCompare(...)` in the review page with a debounced/abortable fetch that seeks the local `<audio>` and renders a badge. No persistence; scan/verify pipeline untouched (Option A per the design spec).

**Tech Stack:** Python 3.12, numpy, librosa (lazy), Flask, ffmpeg (runtime, for MP3/M4A/AAC decode), vanilla JS in `templates/review.html`. Tests: `unittest` (matches repo convention), run via `python3 -m pytest tests/`.

**Spec:** `docs/superpowers/specs/2026-07-15-auto-align-compare-player-design.md`

---

## File Structure

- **Create `align.py`** (repo root) — audio-alignment module. Three functions:
  `find_offset` (pure numpy), `chroma_of` (lazy librosa decode), `align_audio`
  (orchestrator), plus `confidence_label` (pure). Lazy-imports librosa *inside*
  functions so importing the module never requires librosa/ffmpeg.
- **Create `tests/test_align.py`** — unit tests for the pure logic + a WAV round-trip.
- **Modify `review.py`** — add `_fetch_preview(url)` helper + `GET /api/align` route.
- **Create `tests/test_align_endpoint.py`** — Flask-test-client tests for the route.
- **Modify `templates/review.html`** — badge markup + CSS, `autoAlign()` + `seekLocal()`
  JS, hook into `loadCompare(...)`, wire the existing `⇄ Cue` button to the aligned offset.
- **Modify `requirements.txt`** — add `librosa` (optional/auto-align only) + ffmpeg note.
- **Modify `packaging/PACKAGING.md`** — note the ffmpeg requirement for the bundle.

Follow the existing test convention (see `tests/test_queue_pagination.py`): `unittest`
classes, `sys.path.insert(0, <repo root>)` at top, test pure helpers where possible.

---

## Task 1: `find_offset` + `confidence_label` (pure numpy core)

**Files:**
- Create: `align.py`
- Test: `tests/test_align.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_align.py`:

```python
"""Unit tests for align.py.

find_offset/confidence_label are pure numpy — no audio decode, no network — so
the core alignment logic is fully testable with synthetic chroma arrays.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from align import find_offset, confidence_label  # noqa: E402


def one_hot_chroma(pitches):
    """A (12, N) chromagram where each frame is a single active pitch class."""
    m = np.zeros((12, len(pitches)), dtype=float)
    for j, p in enumerate(pitches):
        m[p, j] = 1.0
    return m


class FindOffsetTest(unittest.TestCase):
    def test_recovers_known_offset_with_high_confidence(self):
        rng = np.random.default_rng(0)
        ref_pitches = rng.integers(0, 12, size=200)
        ref = one_hot_chroma(ref_pitches)
        query = one_hot_chroma(ref_pitches[50:80])   # exact 30-frame slice at 50
        offset, conf = find_offset(ref, query)
        self.assertEqual(offset, 50)
        self.assertGreater(conf, 0.7)

    def test_unrelated_query_has_low_confidence(self):
        ref = one_hot_chroma(np.random.default_rng(0).integers(0, 12, size=200))
        query = one_hot_chroma(np.random.default_rng(999).integers(0, 12, size=30))
        _offset, conf = find_offset(ref, query)
        self.assertLess(conf, 0.4)

    def test_query_longer_than_ref_is_safe(self):
        ref = one_hot_chroma([0, 1, 2])
        query = one_hot_chroma([0, 1, 2, 3, 4])
        offset, conf = find_offset(ref, query)
        self.assertEqual((offset, conf), (0, 0.0))


class ConfidenceLabelTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(confidence_label(0.9), "strong")
        self.assertEqual(confidence_label(0.7), "strong")
        self.assertEqual(confidence_label(0.5), "weak")
        self.assertEqual(confidence_label(0.4), "weak")
        self.assertEqual(confidence_label(0.1), "none")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_align.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'align'`

- [ ] **Step 3: Write the minimal implementation**

Create `align.py`:

```python
#!/usr/bin/env python3
"""
align.py — line up a local audio file against a short iTunes preview.

The iTunes preview is a ~30s slice from somewhere in the middle of a track and the
API doesn't say where. We compute a chromagram of each (chroma survives EQ/bitrate/
master differences that would break raw-waveform correlation), slide the preview's
chroma over the local file's, and report the best offset + a match confidence.

librosa is imported lazily inside chroma_of, so importing this module never requires
librosa or ffmpeg — callers degrade gracefully when the decode stack is absent.
"""

import logging

import numpy as np

logger = logging.getLogger("earditor.align")

STRONG_THRESHOLD = 0.7
WEAK_THRESHOLD = 0.4


def _l2norm(chroma):
    norms = np.linalg.norm(chroma, axis=0, keepdims=True)
    norms[norms == 0] = 1.0
    return chroma / norms


def find_offset(chroma_ref, chroma_query):
    """
    Slide chroma_query (the preview, shape (12, Nq)) over chroma_ref (the local
    file, shape (12, Nref)) and return (offset_frames, confidence).

    confidence in [0,1] rewards a SHARP peak, not just a high one: a single decisive
    alignment (same recording) scores high; a flat similarity landscape (different
    recording) scores low. Returns (0, 0.0) when the query can't fit in the ref.
    """
    ref = _l2norm(np.asarray(chroma_ref, dtype=float))
    q = _l2norm(np.asarray(chroma_query, dtype=float))
    n_ref, n_q = ref.shape[1], q.shape[1]
    if n_q == 0 or n_ref < n_q:
        return 0, 0.0
    sims = np.empty(n_ref - n_q + 1)
    for k in range(n_ref - n_q + 1):
        sims[k] = float(np.mean(np.sum(ref[:, k:k + n_q] * q, axis=0)))
    best = int(np.argmax(sims))
    peak = float(sims[best])
    baseline = float(np.median(sims))
    contrast = (peak - baseline) / (1.0 - baseline + 1e-6)
    confidence = float(max(0.0, min(1.0, peak * contrast)))
    return best, confidence


def confidence_label(confidence):
    """Map a 0..1 confidence to a badge label."""
    if confidence >= STRONG_THRESHOLD:
        return "strong"
    if confidence >= WEAK_THRESHOLD:
        return "weak"
    return "none"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_align.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add align.py tests/test_align.py
git commit -m "feat(align): pure chroma cross-correlation core (find_offset + confidence_label)"
```

---

## Task 2: `chroma_of` + `align_audio` (decode + orchestrate)

**Files:**
- Modify: `align.py`
- Test: `tests/test_align.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_align.py` (above the `if __name__` block):

```python
import tempfile  # noqa: E402  (top-of-file group is fine too)

try:
    import soundfile as _sf   # librosa dep; present when the decode stack is installed
    import librosa as _lb      # noqa: F401
    _HAVE_AUDIO = True
except Exception:
    _HAVE_AUDIO = False


def _tone_sequence(pitches_hz, sr=22050, seg=0.5):
    """Concatenate pure tones so the chromagram has real, varying structure."""
    out = []
    for f in pitches_hz:
        t = np.linspace(0, seg, int(sr * seg), endpoint=False)
        out.append(0.5 * np.sin(2 * np.pi * f * t))
    return np.concatenate(out)


@unittest.skipUnless(_HAVE_AUDIO, "librosa/soundfile not installed")
class AlignAudioTest(unittest.TestCase):
    def test_recovers_offset_from_wav_excerpt(self):
        from align import align_audio
        sr = 22050
        full = _tone_sequence([220, 277, 330, 392, 440, 494, 523, 587], sr=sr)
        # excerpt = seconds 1.5..3.5 of the full signal (offset should be ~1.5s)
        start = int(1.5 * sr)
        excerpt = full[start:start + int(2.0 * sr)]
        with tempfile.TemporaryDirectory() as d:
            fp_full = os.path.join(d, "full.wav")
            fp_part = os.path.join(d, "part.wav")
            _sf.write(fp_full, full, sr)
            _sf.write(fp_part, excerpt, sr)
            res = align_audio(fp_full, fp_part, sr=sr)
        self.assertAlmostEqual(res["offset_sec"], 1.5, delta=0.3)
        self.assertGreater(res["confidence"], 0.5)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_align.py::AlignAudioTest -v`
Expected: FAIL — `ImportError: cannot import name 'align_audio'`

- [ ] **Step 3: Implement `chroma_of` + `align_audio`**

Append to `align.py`:

```python
def chroma_of(source, sr=22050, hop=512):
    """
    Decode `source` (a file path or file-like) to mono at `sr` and return its
    12-bin chromagram, shape (12, n_frames). librosa (and, for MP3/M4A/AAC, ffmpeg)
    are required here and imported lazily — callers should catch ImportError/other
    decode errors and degrade to "align unavailable".
    """
    import librosa
    y, _sr = librosa.load(source, sr=sr, mono=True)
    return librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)


def align_audio(local_path, preview_source, sr=22050, hop=512):
    """
    Align `preview_source` inside `local_path`. Returns
    {"offset_sec": float, "confidence": float}. Raises on decode failure (caller
    handles). offset_sec is where in the local file the preview begins.
    """
    ref = chroma_of(local_path, sr=sr, hop=hop)
    query = chroma_of(preview_source, sr=sr, hop=hop)
    offset_frames, confidence = find_offset(ref, query)
    return {"offset_sec": offset_frames * hop / sr, "confidence": confidence}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_align.py -v`
Expected: PASS (all Task 1 tests + `AlignAudioTest`). If librosa/soundfile were
missing the audio test would SKIP — on this machine it should run and PASS.

- [ ] **Step 5: Commit**

```bash
git add align.py tests/test_align.py
git commit -m "feat(align): chroma_of decode + align_audio orchestrator"
```

---

## Task 3: `GET /api/align` endpoint

**Files:**
- Modify: `review.py` (add helper + route; place the route near `/api/preview`, ~line 407)
- Test: `tests/test_align_endpoint.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_align_endpoint.py`:

```python
"""Tests for the /api/align endpoint.

Uses Flask's test client. The preview download and the (heavy) align_audio call are
monkeypatched, and track validation is stubbed, so the route's control flow and JSON
shape are tested without network, ffmpeg, or a real DB.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review  # noqa: E402
import align   # noqa: E402


class AlignEndpointTest(unittest.TestCase):
    def setUp(self):
        review.DEMO = False
        self.client = review.app.test_client()

    def _get(self, path="/x.mp3", preview="http://itunes/p.m4a"):
        from urllib.parse import urlencode
        return self.client.get("/api/align?" + urlencode({"path": path, "preview": preview}))

    def test_happy_path_returns_offset_and_label(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio",
                               return_value={"offset_sec": 12.3, "confidence": 0.82}):
            r = self._get()
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertAlmostEqual(data["offset"], 12.3)
        self.assertEqual(data["label"], "strong")

    def test_missing_preview_param(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True):
            r = self._get(preview="")
        self.assertEqual(r.get_json(), {"ok": False, "reason": "no_preview"})

    def test_unknown_track(self):
        with mock.patch.object(review.db, "get_track", return_value=None):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "unknown_track")

    def test_download_failure(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value=None):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "download_failed")

    def test_decode_failure(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio", side_effect=RuntimeError("boom")):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "decode_failed")

    def test_librosa_missing_is_unavailable(self):
        with mock.patch.object(review.db, "get_track", return_value={"filepath": "/x.mp3"}), \
             mock.patch.object(review.os.path, "isfile", return_value=True), \
             mock.patch.object(review, "_fetch_preview", return_value="/tmp/p.m4a"), \
             mock.patch.object(align, "align_audio", side_effect=ImportError("no librosa")):
            r = self._get()
        self.assertEqual(r.get_json()["reason"], "unavailable")

    def test_demo_mode_is_unavailable(self):
        review.DEMO = True
        try:
            r = self._get()
        finally:
            review.DEMO = False
        self.assertEqual(r.get_json(), {"ok": False, "reason": "unavailable"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_align_endpoint.py -v`
Expected: FAIL — 404s / `AttributeError: module 'review' has no attribute '_fetch_preview'`

- [ ] **Step 3: Implement the helper + route**

In `review.py`, add `import tempfile` to the imports if not present (there is already
`import os`, `import requests` is NOT imported — the app uses `sources.itunes`; add
`import requests` near the top imports). Then add this **after** the `api_preview`
route (around line 407):

```python
def _fetch_preview(url, timeout=10):
    """Download an iTunes preview to a temp .m4a file. Returns the path, or None."""
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.debug("preview download failed (%s): %s", url, e)
        return None
    tf = tempfile.NamedTemporaryFile(prefix="earditor_align_", suffix=".m4a", delete=False)
    tf.write(resp.content)
    tf.close()
    return tf.name


@app.route("/api/align")
def api_align():
    """
    Align the local file at ?path= against the iTunes preview at ?preview= and
    return {ok, offset, confidence, label}. Review-time aid only — nothing is
    stored. Degrades to {ok:false, reason} on any failure so the UI can fall back
    to the manual nudge. Off in demo mode (no real local audio).
    """
    import align
    if DEMO:
        return jsonify({"ok": False, "reason": "unavailable"})
    path = request.args.get("path", "")
    preview = request.args.get("preview", "")
    c = conn()
    track = db.get_track(c, path)
    c.close()
    if not track or not os.path.isfile(path):
        return jsonify({"ok": False, "reason": "unknown_track"})
    if not preview:
        return jsonify({"ok": False, "reason": "no_preview"})
    tmp = _fetch_preview(preview)
    if not tmp:
        return jsonify({"ok": False, "reason": "download_failed"})
    try:
        res = align.align_audio(path, tmp)
    except ImportError as e:
        log.warning("align unavailable (librosa/ffmpeg missing): %s", e)
        return jsonify({"ok": False, "reason": "unavailable"})
    except Exception as e:
        log.debug("align decode failed for %s: %s", path, e)
        return jsonify({"ok": False, "reason": "decode_failed"})
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jsonify({
        "ok": True,
        "offset": res["offset_sec"],
        "confidence": res["confidence"],
        "label": align.confidence_label(res["confidence"]),
    })
```

Note: the route does `import align` at call time and the test monkeypatches
`align.align_audio` / `align.confidence_label` on the `align` module object, so the
patches take effect. `_fetch_preview` is a module-level `review` function so the test
can patch `review._fetch_preview`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_align_endpoint.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the whole suite (nothing regressed)**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (existing test_verify / test_config / test_queue_pagination + the new files)

- [ ] **Step 6: Commit**

```bash
git add review.py tests/test_align_endpoint.py
git commit -m "feat(review): /api/align endpoint (lazy librosa, graceful degrade)"
```

---

## Task 4: Frontend — badge, auto-align on select, seek, cue integration

**Files:**
- Modify: `templates/review.html`

- [ ] **Step 1: Add the badge markup to the compare slot**

In `templates/review.html`, the compare slot's label is (around line 690):

```html
      <div class="plabel">▶ Compare<span class="cmp-src hint"></span></div>
```

Change it to add a badge span:

```html
      <div class="plabel">▶ Compare<span class="cmp-src hint"></span><span class="align-badge"></span></div>
```

- [ ] **Step 2: Add badge CSS**

In the `<style>` block, next to the existing `.cmp-src` rule (around line 200), add:

```css
  .align-badge { font-family: var(--font-mono); font-size: 10px; margin-left: 8px;
    padding: 1px 6px; border-radius: 4px; letter-spacing: .02em; display: none; }
  .align-badge.show { display: inline-block; }
  .align-badge.aligning { color: var(--ink-mute); background: var(--panel-2); }
  .align-badge.strong { color: var(--ok); background: var(--ok-wash); }
  .align-badge.weak   { color: #9a6a00; background: #fff3d6; }
  .align-badge.none   { color: var(--ink-mute); background: var(--panel-2); }
  .align-badge.na     { color: var(--ink-mute); background: var(--panel-2); }
```

(If `--ok`/`--ok-wash`/`--panel-2` aren't defined, reuse the values from the existing
`.badge-official` rule at line 147 — grep `--ok-wash` to confirm the token names.)

- [ ] **Step 3: Add `seekLocal` + `autoAlign` JS and a module-level abort handle**

Just above `async function loadCompare(` (line 1024), add:

```javascript
// Auto-align: one in-flight request at a time; selecting another card/candidate
// aborts the previous so rapid clicking stays smooth.
let ALIGN_AC = null;

function setBadge(el, cls, text) {
  const b = el.querySelector(".align-badge");
  if (!b) return;
  b.className = "align-badge show " + cls;
  b.textContent = text;
}

function seekLocal(el, sec) {
  const local = el.querySelector("audio.local");
  if (!local) return;
  const apply = () => { local.currentTime = Math.max(0, sec); };
  if (local.readyState >= 1) apply();
  else { local.addEventListener("loadedmetadata", apply, { once: true }); local.load(); }
}

async function autoAlign(el, previewUrl) {
  if (DEMO || !previewUrl) return;
  if (ALIGN_AC) ALIGN_AC.abort();
  ALIGN_AC = new AbortController();
  const signal = ALIGN_AC.signal;
  setBadge(el, "aligning", "aligning…");
  try {
    const r = await fetch(`/api/align?path=${enc(el.dataset.fp)}&preview=${enc(previewUrl)}`,
                          { signal });
    const d = await r.json();
    if (signal.aborted) return;
    if (d.ok) {
      el._alignOffset = d.offset;
      seekLocal(el, d.offset);
      const pct = Math.round((d.confidence || 0) * 100);
      const txt = { strong: `⇄ strong ${pct}%`, weak: `⇄ weak ${pct}%`,
                    none: `⇄ no match` }[d.label] || "⇄";
      setBadge(el, d.label, txt);
    } else {
      setBadge(el, "na", "align n/a");
    }
  } catch (e) {
    if (e && e.name === "AbortError") return;   // superseded by a newer select
    setBadge(el, "na", "align n/a");
  }
}
```

- [ ] **Step 4: Call `autoAlign` from `loadCompare` when a preview is present**

In `loadCompare` (line 1037), the `if (url) { ... }` branch currently ends after
setting `src.textContent`. Add the auto-align kick and clear the badge in the `else`:

```javascript
  if (url) {
    audio.src = url; audio.style.display = "";
    if (empty) empty.style.display = "none";
    if (src) src.textContent = srcLabel ? " · " + srcLabel : "";
    autoAlign(el, url);                       // <-- add: line up local to this preview
  } else {
    try { audio.pause(); } catch(_){}
    audio.removeAttribute("src"); audio.style.display = "none";
    if (empty) empty.style.display = "";
    if (src) src.textContent = "";
    const b = el.querySelector(".align-badge");  // <-- add: hide any stale badge
    if (b) b.className = "align-badge";
  }
```

- [ ] **Step 5: Wire the existing `⇄ Cue` button to the aligned offset**

`cueToPreview` (line 1078) currently guesses 30%. Make it prefer the real aligned
offset when we have one. Replace the body of the `seek` closure's first line:

```javascript
function cueToPreview(el) {
  const local = el.querySelector("audio.local");
  if (!local) return;
  const seek = () => {
    const d = local.duration;
    // Prefer the auto-aligned offset; fall back to the ~30% highlight guess.
    if (typeof el._alignOffset === "number") local.currentTime = el._alignOffset;
    else if (d && isFinite(d)) local.currentTime = d * 0.30;
    document.querySelectorAll("audio").forEach(a => { if (a !== local) a.pause(); });
    stopPreview();
    local.play().catch(() => toast("Can't play this file"));
    toast("Local cued to preview zone — 🔊 to A/B, [ ] to fine-tune");
  };
  if (local.readyState >= 1) seek();
  else { local.addEventListener("loadedmetadata", seek, { once: true }); local.load(); }
}
```

- [ ] **Step 6: Verify the page still serves (no JS syntax error)**

Run: `pkill -f review.py; python3 review.py --port 5021 >/tmp/al.log 2>&1 & sleep 3; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5021/; pkill -f review.py`
Expected: `200`. (No automated JS test harness exists in this repo — behavior is
verified end-to-end in Task 6.)

- [ ] **Step 7: Commit**

```bash
git add templates/review.html
git commit -m "feat(review-ui): auto-align local file to preview on select + confidence badge"
```

---

## Task 5: Dependencies + packaging note

**Files:**
- Modify: `requirements.txt`
- Modify: `packaging/PACKAGING.md`

- [ ] **Step 1: Add librosa to requirements with an honest comment**

In `requirements.txt`, add after the `requests` line:

```
librosa>=0.10,<0.12     # OPTIONAL: audio alignment for the compare player (lazy-loaded).
                        # Needs ffmpeg on PATH to decode MP3/M4A/AAC (brew install ffmpeg).
                        # Everything else runs without it; auto-align just shows "unavailable".
```

- [ ] **Step 2: Add the ffmpeg note to PACKAGING.md**

In `packaging/PACKAGING.md`, in the "Runtime and release notes" list (near the
`fpcalc` bullet, ~line 61), add a bullet:

```markdown
- **`ffmpeg` (auto-align):** the compare-player alignment feature decodes MP3/M4A/AAC
  via librosa, which needs `ffmpeg` on PATH. Either `brew install ffmpeg` or vendor a
  signed `ffmpeg` into the bundle. The feature is lazy-loaded — its absence only
  disables auto-align (badge shows "unavailable"); the rest of the app is unaffected.
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt packaging/PACKAGING.md
git commit -m "docs: note optional librosa + ffmpeg requirement for auto-align"
```

---

## Task 6: End-to-end verification against the real library

**Files:** none (verification only)

- [ ] **Step 1: Boot the real review server**

Run: `pkill -f review.py; python3 review.py --port 5001 >/tmp/review.log 2>&1 &`
Wait for `Review UI → http://127.0.0.1:5001` in `/tmp/review.log`.

- [ ] **Step 2: Exercise the endpoint directly with a real VERIFIED track**

Pick a VERIFIED track that exists on disk and has an iTunes preview, then:

```bash
python3 - <<'PY'
import sqlite3, json, os, urllib.parse, urllib.request
c = sqlite3.connect("earditor.db"); c.row_factory = sqlite3.Row
row = None
for r in c.execute("SELECT filepath, proposed_json FROM tracks WHERE status='scanned' AND verdict='VERIFIED'"):
    p = json.loads(r["proposed_json"] or "{}")
    if p.get("preview_url") and os.path.isfile(r["filepath"]):
        row = (r["filepath"], p["preview_url"]); break
assert row, "no verified track with preview + existing file"
qs = urllib.parse.urlencode({"path": row[0], "preview": row[1]})
print(urllib.request.urlopen("http://127.0.0.1:5001/api/align?" + qs, timeout=60).read().decode())
PY
```

Expected: a JSON line like `{"ok": true, "offset": <seconds>, "confidence": <0..1>, "label": "strong"}`.
A genuine VERIFIED match should generally be `strong` (or at least `weak`) with a
plausible offset less than the track's length.

- [ ] **Step 3: Drive the UI in the browser**

Use the preview/browser tools: open `http://127.0.0.1:5001`, jump to the VERIFIED
tier, select a candidate on a card, and confirm: (a) the "aligning…" badge appears
then resolves to a coloured strong/weak/none badge, (b) the local `<audio>` playhead
jumps to the aligned offset, (c) pressing 🔊 then Play-both has the two lined up by
ear, (d) selecting a different card aborts cleanly (no stale badge). Capture a
screenshot as proof.

- [ ] **Step 4: Sanity-check a deliberate mismatch**

On a COVER-tier card (a cover/live version that is NOT the catalog recording), select
the catalog candidate and confirm the badge reports `weak` or `no match` — i.e. the
confidence score actually discriminates. Note the result.

- [ ] **Step 5: Full test suite + commit any final tweak**

Run: `python3 -m pytest tests/ -q`
Expected: all green. If threshold tuning was needed from Steps 3–4, adjust
`STRONG_THRESHOLD` / `WEAK_THRESHOLD` in `align.py`, re-run, and:

```bash
git add align.py
git commit -m "tune(align): confidence thresholds against real library"
```

---

## Self-Review (completed while writing)

**Spec coverage:**
- Auto-cue on select → Task 4 (loadCompare → autoAlign → seekLocal). ✅
- Confidence badge strong/weak/none → Task 1 (`confidence_label`) + Task 4 (badge). ✅
- Review-time, on-demand, no persistence, verdict untouched → Task 3 route stores nothing; scan/verify not modified. ✅
- Lazy librosa, graceful degrade → Task 2 (import inside `chroma_of`) + Task 3 (ImportError→`unavailable`). ✅
- Debounced/abortable on card switch → Task 4 (`ALIGN_AC` AbortController). ✅
- Edge cases (demo off, no preview, download/decode fail) → Task 3 route + tests. ✅
- ffmpeg dependency flagged → Task 5. ✅
- Testing: pure `find_offset`, WAV `align_audio`, endpoint paths → Tasks 1–3. ✅

**Placeholder scan:** none — every code step shows complete code.

**Type/name consistency:** `find_offset(chroma_ref, chroma_query) -> (offset_frames, confidence)`,
`align_audio(...) -> {"offset_sec","confidence"}`, endpoint returns `{ok, offset, confidence, label}`,
frontend reads `d.offset/d.confidence/d.label` and stores `el._alignOffset`. Consistent across tasks. ✅
