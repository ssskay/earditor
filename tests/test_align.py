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
