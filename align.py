#!/usr/bin/env python3
"""
align.py — line up a local audio file against a short iTunes preview.

The iTunes preview is a ~30s slice from somewhere in the middle of a track and the
API doesn't say where. We compute a chromagram of each (chroma survives EQ/bitrate/
master differences that would break raw-waveform correlation), slide the preview's
chroma over the local file's, and report the best offset + a match confidence.

The pure-numpy core (find_offset/confidence_label) and the decode layer
(chroma_of/align_audio) both live in this module. librosa is imported lazily inside
chroma_of, so importing this module never requires librosa or ffmpeg — callers
degrade gracefully when the decode stack is absent.
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
    if sims.size < 2:
        # Only one candidate window (n_ref == n_q, e.g. a ~30s interlude vs a 30s
        # preview): there is no similarity landscape, so the median baseline would
        # equal the peak and zero out the confidence of even a perfect match. With
        # no sharpness to measure, raw match quality is the honest answer.
        return best, float(max(0.0, min(1.0, peak)))
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
