"""Unit tests for /api/queue pagination (the Safari-hang fix).

The review UI must render the queue in batches, not all 2,731 cards at once.
The server slices a worst-first-ordered item list by offset/limit while still
reporting tier_counts and total over the WHOLE queue, so the header stays
accurate and the client knows when to stop paging.

These tests exercise the pure pagination helper `_paginate` with a no-op
enricher, so they need no DB and don't touch the (expensive) options rebuild.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review import _paginate, TIER_ORDER  # noqa: E402

NOOP = lambda it: None  # noqa: E731 — enrich stub; pagination logic is what's under test


def q(*verdicts):
    """Build a worst-first item list from a sequence of verdicts."""
    return [{"verdict": v, "filepath": f"/f/{i}.mp3"} for i, v in enumerate(verdicts)]


class PaginateTest(unittest.TestCase):
    def test_total_and_tier_counts_span_whole_queue_not_the_slice(self):
        items = q("UNVERIFIED", "UNVERIFIED", "UNVERIFIED", "VERIFIED", "VERIFIED")
        _groups, counts, total = _paginate(items, offset=0, limit=2, enrich=NOOP)
        self.assertEqual(total, 5)
        self.assertEqual(counts["UNVERIFIED"], 3)
        self.assertEqual(counts["VERIFIED"], 2)
        self.assertEqual(counts["COVER"], 0)
        # every tier key present so the header can render all pills
        self.assertEqual(set(counts), set(TIER_ORDER))

    def test_slice_returns_only_limit_items(self):
        items = q(*(["UNVERIFIED"] * 10))
        groups, _counts, _total = _paginate(items, offset=0, limit=4, enrich=NOOP)
        self.assertEqual(sum(len(g["items"]) for g in groups), 4)

    def test_second_page_continues_where_first_ended(self):
        items = q("UNVERIFIED", "UNVERIFIED", "UNVERIFIED", "VERIFIED", "VERIFIED")
        p1, _, _ = _paginate(items, offset=0, limit=3, enrich=NOOP)
        p2, _, _ = _paginate(items, offset=3, limit=3, enrich=NOOP)
        # page 1 = the three UNVERIFIED; page 2 = the two VERIFIED
        self.assertEqual([g["verdict"] for g in p1], ["UNVERIFIED"])
        self.assertEqual(sum(len(g["items"]) for g in p1), 3)
        self.assertEqual([g["verdict"] for g in p2], ["VERIFIED"])
        self.assertEqual(sum(len(g["items"]) for g in p2), 2)

    def test_page_groups_are_worst_first(self):
        # a page straddling two tiers groups them UNVERIFIED before VERIFIED
        items = q("UNVERIFIED", "VERIFIED")
        groups, _, _ = _paginate(items, offset=0, limit=2, enrich=NOOP)
        self.assertEqual([g["verdict"] for g in groups], ["UNVERIFIED", "VERIFIED"])

    def test_offset_past_end_is_empty_but_counts_intact(self):
        items = q("UNVERIFIED", "VERIFIED")
        groups, counts, total = _paginate(items, offset=10, limit=5, enrich=NOOP)
        self.assertEqual(groups, [])
        self.assertEqual(total, 2)
        self.assertEqual(counts["UNVERIFIED"], 1)

    def test_only_page_items_are_enriched(self):
        items = q(*(["UNVERIFIED"] * 6))
        seen = []
        _paginate(items, offset=0, limit=2, enrich=lambda it: seen.append(it["filepath"]))
        self.assertEqual(len(seen), 2)  # not all 6


if __name__ == "__main__":
    unittest.main()
