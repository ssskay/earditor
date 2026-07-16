"""Unit tests for align.py.

find_offset/confidence_label are pure numpy — no audio decode, no network — so
the core alignment logic is fully testable with synthetic chroma arrays.
"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from align import find_offset, confidence_label  # noqa: E402

try:
    import soundfile as _sf   # librosa dep; present when the decode stack is installed
    import librosa as _lb      # noqa: F401
    _HAVE_AUDIO = True
except Exception:
    _HAVE_AUDIO = False


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

    def test_same_length_perfect_match_is_confident(self):
        """A ~30s interlude vs a 30s preview leaves ONE candidate window.

        There's no similarity landscape to measure peak sharpness against, so
        confidence must fall back to raw match quality rather than collapsing
        to 0.0 — a false 'no match' on a perfect match is the one failure this
        feature must never produce.
        """
        pitches = np.random.default_rng(0).integers(0, 12, size=30)
        chroma = one_hot_chroma(pitches)
        offset, conf = find_offset(chroma, one_hot_chroma(pitches))
        self.assertEqual(offset, 0)
        self.assertGreater(conf, 0.7)

    def test_same_length_unrelated_query_is_not_confident(self):
        ref = one_hot_chroma(np.random.default_rng(0).integers(0, 12, size=30))
        query = one_hot_chroma(np.random.default_rng(999).integers(0, 12, size=30))
        offset, conf = find_offset(ref, query)
        self.assertEqual(offset, 0)   # only one window to pick
        self.assertLess(conf, 0.4)

    def test_silent_chroma_returns_cleanly(self):
        """All-zero chroma (digital silence) must not divide by zero or NaN."""
        silence = np.zeros((12, 30))
        offset, conf = find_offset(silence, silence[:, :10])
        self.assertEqual(offset, 0)
        self.assertFalse(np.isnan(conf))
        self.assertEqual(conf, 0.0)


class ConfidenceLabelTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(confidence_label(0.9), "strong")
        self.assertEqual(confidence_label(0.7), "strong")
        self.assertEqual(confidence_label(0.5), "weak")
        self.assertEqual(confidence_label(0.4), "weak")
        self.assertEqual(confidence_label(0.1), "none")


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


if __name__ == "__main__":
    unittest.main()
