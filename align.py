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
