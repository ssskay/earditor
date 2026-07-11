#!/usr/bin/env python3
"""
Unit tests for verify.py — synthetic, fully fictional fixtures.

Every artist, uploader, song, album and OST name below is invented (it shares the
same made-up universe as demo/fixtures.json). No real music-library data is used;
the fixtures only reproduce the SHAPES of the evidence and the signal/verdict
relationships the engine cares about.

Covers the cases the spec calls out:
  - exact match (VERIFIED)
  - cover with 歌ってみた (COVER)
  - nightcore with duration mismatch (COVER)
  - Topic channel upload (neutral folder, still VERIFIED)
  - wrong-song fingerprint, AcoustID disagrees (UNVERIFIED)
  - Japanese/romaji filename corroboration (S4)
  - cover by folder-artist mismatch without a keyword (COVER)
  - no fingerprint anywhere (NO_MATCH)

Run: python3 tests/test_verify.py   (or: python3 -m pytest tests/)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verify import verify, VERIFIED, LIKELY, COVER, UNVERIFIED, NO_MATCH, contains_ratio  # noqa: E402
from utils import title_from_filename  # noqa: E402


def ev(**kw):
    base = {
        "file_duration": None, "folder_name": None, "folder_raw": None, "filename": "",
        "shazam": None, "acoustid": None, "itunes": None, "itunes_candidates": [],
        "itunes_folder": None,
    }
    base.update(kw)
    return base


class TestVerdicts(unittest.TestCase):

    def test_exact_match_verified(self):
        r = verify(ev(
            file_duration=260,
            folder_name="Yuzuki Ame",
            filename="Yuzuki Ame - Hoshizora Memory",
            shazam={"title": "Hoshizora Memory", "artist": "Yuzuki Ame",
                    "album": "Astralis", "art_url": "http://x/sz.jpg"},
            acoustid={"title": "Hoshizora Memory", "artist": "Yuzuki Ame",
                      "duration": 258, "score": 0.95, "album": "Astralis"},
            itunes={"title": "Hoshizora memory", "artist": "Yuzuki Ame",
                    "album": "Astralis", "duration": 258.8,
                    "art_url": "http://x/it.jpg", "preview_url": "http://x/p.m4a"},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertTrue(r["signals"]["S1"]["pass"])
        self.assertTrue(r["signals"]["S3"]["pass"])
        # album + art proposed from iTunes
        self.assertEqual(r["proposed"]["album"], "Astralis")
        self.assertEqual(r["proposed"]["art_url"], "http://x/it.jpg")
        self.assertEqual(r["proposed"]["artist"], "Yuzuki Ame")

    def test_utaite_cover_japanese(self):
        r = verify(ev(
            file_duration=250,
            folder_name="Lumina",
            filename="星空メモリー 歌ってみた",
            shazam={"title": "Hoshizora Memory", "artist": "Yuzuki Ame", "album": "Astralis"},
            itunes={"title": "Hoshizora Memory", "artist": "Yuzuki Ame", "duration": 258},
        ))
        self.assertEqual(r["verdict"], COVER)
        self.assertTrue(r["signals"]["S5"]["pass"])
        self.assertIn("歌ってみた", r["signals"]["S5"]["value"])
        # cover artist = folder, album blanked
        self.assertEqual(r["proposed"]["artist"], "Lumina")
        self.assertIsNone(r["proposed"]["album"])
        self.assertEqual(r["proposed"]["original_artist"], "Yuzuki Ame")

    def test_nightcore_duration_mismatch(self):
        r = verify(ev(
            file_duration=150,             # sped up
            folder_name="NightcoreZone",
            filename="Neon Lullaby (Nightcore)",
            shazam={"title": "Neon Lullaby", "artist": "Sora Kanata"},
            acoustid={"title": "Neon Lullaby", "artist": "Sora Kanata", "duration": 240, "score": 0.9},
            itunes={"title": "Neon Lullaby", "artist": "Sora Kanata", "album": "Prism", "duration": 240},
        ))
        self.assertEqual(r["verdict"], COVER)     # S5 nightcore keyword
        self.assertTrue(r["signals"]["S5"]["pass"])
        self.assertFalse(r["signals"]["S3"]["pass"])   # duration off by 90s
        self.assertEqual(r["signals"]["S3"]["status"], "red")

    # --- folder-artist catalog release (the "which cover is it?" problem) ---------
    def test_folder_artist_catalog_release_beats_conflicting_fingerprint(self):
        # The file sits in Mochizuki Rei's folder, but the fingerprint confidently
        # says a DIFFERENT cover artist (nekomelody). iTunes has Mochizuki Rei's own
        # release of the same title → propose that version, and never call it
        # VERIFIED: a genuine which-cover ambiguity a human should confirm.
        r = verify(ev(
            file_duration=239, folder_name="Mochizuki Rei",
            filename='Kaleido Girl (From "Astral Voyage the Movie")',
            acoustid={"title": "Kaleido Girl (From Astral Voyage the Movie)", "artist": "nekomelody",
                      "duration": 240, "score": 0.98},
            itunes={"title": "Kaleido Girl (From Astral Voyage the Movie)", "artist": "nekomelody",
                    "album": "Kaleido Girl (From Astral Voyage the Movie) - Single", "duration": 240},
            itunes_folder={"title": 'Kaleido Girl (From "Astral Voyage the Movie")', "artist": "Mochizuki Rei",
                           "album": 'Kaleido Girl (From "Astral Voyage the Movie") - Single',
                           "duration": 239, "art_url": "http://x/rei.jpg",
                           "preview_url": "http://x/rei.m4a"},
        ))
        self.assertNotEqual(r["verdict"], VERIFIED)   # must not be batch-acceptable
        self.assertEqual(r["proposed"]["artist"], "Mochizuki Rei")
        self.assertEqual(r["proposed"]["album"], 'Kaleido Girl (From "Astral Voyage the Movie") - Single')
        self.assertEqual(r["proposed"]["art_url"], "http://x/rei.jpg")
        # both readings offered, folder artist first
        arts = [c["artist"] for c in r["candidates"]]
        self.assertEqual(arts[0], "Mochizuki Rei")
        self.assertIn("nekomelody", arts)

    def test_folder_release_ignored_when_artist_agrees_with_fingerprint(self):
        # No conflict: fingerprint and folder are the same artist → normal flow.
        r = verify(ev(
            file_duration=239, folder_name="Mochizuki Rei",
            filename="Neon Lullaby (From Prismatic Hearts)",
            acoustid={"title": "Neon Lullaby (From \"Prismatic Hearts\")", "artist": "Mochizuki Rei",
                      "duration": 240, "score": 0.95},
            itunes={"title": "Neon Lullaby (From \"Prismatic Hearts\")", "artist": "Mochizuki Rei",
                    "album": "Neon Lullaby - Single", "duration": 240},
            itunes_folder={"title": "Neon Lullaby (From \"Prismatic Hearts\")", "artist": "Mochizuki Rei",
                           "album": "Neon Lullaby - Single", "duration": 240},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertEqual(r["proposed"]["artist"], "Mochizuki Rei")
        self.assertEqual(r["proposed"]["album"], "Neon Lullaby - Single")

    def test_folder_release_ignored_when_it_is_a_different_song(self):
        # iTunes returned something by the folder artist, but it's not this title →
        # ignore it entirely; don't hijack the proposal.
        r = verify(ev(
            file_duration=239, folder_name="Mochizuki Rei",
            filename="Kaleido Girl",
            acoustid={"title": "Kaleido Girl", "artist": "nekomelody", "duration": 240, "score": 0.98},
            itunes={"title": "Kaleido Girl", "artist": "nekomelody", "duration": 240},
            itunes_folder={"title": "Some Other Song", "artist": "Mochizuki Rei",
                           "album": "Other - Single", "duration": 200},
        ))
        self.assertNotEqual((r["proposed"] or {}).get("artist"), "Mochizuki Rei")

    def test_feat_credit_is_not_a_cover_keyword(self):
        # "feat." is a collaboration credit that appears in official titles.
        # It must not fire the S5 cover signal on its own.
        r = verify(ev(
            file_duration=249, folder_name="Sora Kanata",
            filename='Starlit (From "Prismatic Hearts") [feat. Alex Rivers]',
            shazam={"title": 'Starlit (From "Prismatic Hearts") [feat. Alex Rivers]',
                    "artist": "Sora Kanata"},
        ))
        self.assertFalse(r["signals"]["S5"]["pass"], r["signals"]["S5"]["explain"])

    def test_catalog_confirmed_official_release_is_not_a_cover(self):
        # iTunes confirms this exact artist+title (S2) and the folder agrees it's
        # that artist (S6 match) → an official release, not a cover. The catalog
        # album must be kept, not blanked out by a COVER verdict.
        title = 'Starlit (From "Prismatic Hearts: The Awakening") [feat. Alex Rivers]'
        r = verify(ev(
            file_duration=249,
            folder_name="Sora Kanata",
            filename=title,
            shazam={"title": title, "artist": "Sora Kanata", "album": "Starlit - Single"},
            itunes={"title": title, "artist": "Sora Kanata", "album": "Starlit - Single",
                    "duration": 250, "art_url": "http://x/it.jpg",
                    "preview_url": "http://x/p.m4a"},
        ))
        self.assertNotEqual(r["verdict"], COVER)
        self.assertEqual(r["proposed"]["artist"], "Sora Kanata")
        self.assertEqual(r["proposed"]["album"], "Starlit - Single")   # album preserved
        self.assertEqual(r["proposed"]["art_url"], "http://x/it.jpg")

    def test_official_release_keeps_album_despite_version_keyword(self):
        # Even a genuine keyword (acoustic / ver.) must not blank the album when the
        # catalog confirms this exact artist+title AND the folder is that same artist.
        # A COVER exists to fix a WRONG artist; here the artist isn't wrong.
        r = verify(ev(
            file_duration=249, folder_name="Sora Kanata",
            filename="Yumeutsutsu (Acoustic Ver.)",
            shazam={"title": "Yumeutsutsu (Acoustic Ver.)", "artist": "Sora Kanata",
                    "album": "Acoustic - Single"},
            itunes={"title": "Yumeutsutsu (Acoustic Ver.)", "artist": "Sora Kanata",
                    "album": "Acoustic - Single", "duration": 250, "art_url": "http://x/a.jpg"},
        ))
        self.assertNotEqual(r["verdict"], COVER)
        self.assertEqual(r["proposed"]["album"], "Acoustic - Single")
        self.assertEqual(r["proposed"]["artist"], "Sora Kanata")

    def test_cover_keyword_still_wins_when_folder_differs(self):
        # Guard must NOT rescue a real cover: keyword fires, folder is a different
        # artist than the catalog's → still COVER, artist reassigned to the folder.
        r = verify(ev(
            file_duration=250, folder_name="MapleUta (Utaite)",
            filename="First Light - Sora Kanata - Starfall OP2 Cover 歌ってみた",
            shazam={"title": "First Light", "artist": "Sora Kanata"},
            itunes={"title": "First Light", "artist": "Sora Kanata", "album": "First Light - Single", "duration": 250},
        ))
        self.assertEqual(r["verdict"], COVER)
        self.assertEqual(r["proposed"]["artist"], "MapleUta (Utaite)")

    def test_topic_channel_artist_match_autoaccepts(self):
        # "Artist - Topic" is YouTube Music's official auto channel. When the
        # fingerprint/search candidate's artist matches the channel name minus
        # "- Topic", it's definitively that artist → VERIFIED + auto-accept, so it
        # never enters the manual queue. (folder_name arrives stripped in production;
        # folder_raw carries the original "- Topic" suffix for detection.)
        r = verify(ev(
            file_duration=242,
            folder_name="Sora Kanata", folder_raw="Sora Kanata - Topic",
            filename="Neon Lullaby",
            shazam={"title": "Neon Lullaby", "artist": "Sora Kanata", "album": "Prism"},
            acoustid={"title": "Neon Lullaby", "artist": "Sora Kanata", "duration": 240, "score": 0.9},
            itunes={"title": "Neon Lullaby", "artist": "Sora Kanata", "album": "Prism", "duration": 240,
                    "art_url": "http://x/it.jpg", "preview_url": "http://x/p.m4a"},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertTrue(r["auto_accept"])
        # tags filled from the candidate, exactly like accepting in review
        self.assertEqual(r["proposed"]["title"], "Neon Lullaby")
        self.assertEqual(r["proposed"]["artist"], "Sora Kanata")
        self.assertEqual(r["proposed"]["album"], "Prism")
        self.assertEqual(r["proposed"]["art_url"], "http://x/it.jpg")

    def test_topic_channel_detects_from_folder_name_when_no_raw(self):
        # Detection also works when "- Topic" is only present on folder_name
        # (defensive: some callers may not supply folder_raw).
        r = verify(ev(
            file_duration=242,
            folder_name="Sora Kanata - Topic",
            filename="Neon Lullaby",
            shazam={"title": "Neon Lullaby", "artist": "Sora Kanata", "album": "Prism"},
            acoustid={"title": "Neon Lullaby", "artist": "Sora Kanata", "duration": 240, "score": 0.9},
            itunes={"title": "Neon Lullaby", "artist": "Sora Kanata", "album": "Prism", "duration": 240},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertTrue(r["auto_accept"])

    def test_topic_channel_artist_mismatch_unverified(self):
        # Topic channel, but the fingerprint's artist does NOT match the channel
        # name → never auto-accept on the folder name alone; send to manual review.
        r = verify(ev(
            file_duration=200,
            folder_name="Yuzuki Ame", folder_raw="Yuzuki Ame - Topic",
            filename="some upload",
            shazam={"title": "Kaleido Girl", "artist": "Hoshino Rin"},   # artist != channel
            itunes=None,
        ))
        self.assertEqual(r["verdict"], UNVERIFIED)
        self.assertFalse(r["auto_accept"])
        self.assertIsNone(r["proposed"])                  # no pre-filled guess

    def test_topic_channel_no_api_result_unverified(self):
        # Topic channel but nothing came back from Shazam/AcoustID/iTunes →
        # UNVERIFIED for manual review, NOT NO_MATCH, and never auto-accepted.
        r = verify(ev(
            file_duration=200,
            folder_name="Aoi Rhythm", folder_raw="Aoi Rhythm - Topic",
            filename="unknown track",
            shazam=None, acoustid=None, itunes=None,
        ))
        self.assertEqual(r["verdict"], UNVERIFIED)
        self.assertFalse(r["auto_accept"])
        self.assertIsNone(r["proposed"])

    def test_non_topic_unknown_folder_still_manual(self):
        # A plain non-Topic upload with conflicting signals keeps the normal
        # manual-review flow — Topic fast-pass must not touch it, no auto-accept.
        r = verify(ev(
            file_duration=None,
            folder_name=None, folder_raw=None,
            filename="track 03",
            shazam={"title": "Kaleido Girl", "artist": "Aoi Rhythm"},
            acoustid={"title": "Different Song", "artist": "Someone Else",
                      "duration": 200, "score": 0.88},
        ))
        self.assertEqual(r["verdict"], UNVERIFIED)
        self.assertFalse(r["auto_accept"])

    def test_normal_verified_is_not_autoaccepted(self):
        # A non-Topic VERIFIED track still goes through the manual/batch queue —
        # only Topic-channel matches auto-accept.
        r = verify(ev(
            file_duration=260,
            folder_name="Yuzuki Ame", folder_raw="Yuzuki Ame",
            filename="Yuzuki Ame - Hoshizora Memory",
            shazam={"title": "Hoshizora Memory", "artist": "Yuzuki Ame", "album": "Astralis"},
            acoustid={"title": "Hoshizora Memory", "artist": "Yuzuki Ame",
                      "duration": 258, "score": 0.95},
            itunes={"title": "Hoshizora memory", "artist": "Yuzuki Ame",
                    "album": "Astralis", "duration": 258.8},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertFalse(r["auto_accept"])

    def test_wrong_song_acoustid_disagrees(self):
        r = verify(ev(
            file_duration=None,
            folder_name=None,
            filename="track 07",
            shazam={"title": "Kaleido Girl", "artist": "Aoi Rhythm"},
            acoustid={"title": "Random Unrelated Song", "artist": "Someone Else",
                      "duration": 200, "score": 0.88},
            itunes=None,
        ))
        self.assertEqual(r["verdict"], UNVERIFIED)
        self.assertEqual(r["signals"]["S1"]["status"], "red")
        self.assertIsNone(r["proposed"])          # NO pre-filled guess
        # candidates offered for the human
        self.assertTrue(any(c["source"] == "Shazam" for c in r["candidates"]))
        self.assertTrue(any("AcoustID" in c["source"] for c in r["candidates"]))

    def test_romaji_filename_corroboration(self):
        # filename is romaji, Shazam title is Japanese — S4 must still match.
        score = contains_ratio("Hoshizora Memory Yuzuki Ame", "星空メモリー")
        self.assertGreaterEqual(score, 85)

    def test_s1_pass_folder_mismatch_is_not_a_cover(self):
        # 6a: an independent fingerprint (S1) confirms the artist, but the song
        # isn't in iTunes (S2 fail — common for older/JP-only anime) and the folder
        # is a fan channel (S6 different). Folder mismatch must NOT be read as a
        # cover once S1 already confirms the identified artist. Suggests option 1.
        r = verify(ev(
            file_duration=240, folder_name="FanChannel99",
            filename="Prismatic Hearts OP1 - First Light",
            shazam={"title": "First Light", "artist": "Hoshino Rin"},
            acoustid={"title": "First Light", "artist": "Hoshino Rin",
                      "duration": 240, "score": 0.96},
            itunes=None,
        ))
        self.assertTrue(r["signals"]["S1"]["pass"])
        self.assertNotEqual(r["verdict"], COVER)
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertEqual(r["proposed"]["artist"], "Hoshino Rin")

    def test_s2_echo_cannot_lift_wrong_title_to_likely(self):
        # 6b: iTunes is queried with the fingerprint's OWN title/artist, so on a
        # wrong-song fingerprint it echoes garbage back and S2 "confirms" it. Right
        # artist / wrong title: the folder matches the artist (S6 match) but the
        # filename shows a different real song (S4-title red). S2 + S6-match must
        # NOT reach LIKELY — S6 corroborates only the artist, never the title.
        r = verify(ev(
            file_duration=230, folder_name="Mochizuki Rei",
            filename="Starfall OST - Aigata ENGLISH ver Mochizuki Rei",
            shazam={"title": "Wrong Echo Song", "artist": "Mochizuki Rei"},
            itunes={"title": "Wrong Echo Song", "artist": "Mochizuki Rei",
                    "album": "Wrong - Single", "duration": 231},
        ))
        self.assertTrue(r["signals"]["S2"]["pass"])
        self.assertTrue(r["signals"]["S3"]["pass"])
        self.assertFalse(r["signals"]["S4"]["pass"])   # filename ≠ echoed title
        self.assertEqual(r["verdict"], UNVERIFIED)

    def test_cover_by_folder_only_when_catalog_cannot_confirm(self):
        # Genuine cover NOT in the catalog: iTunes can't confirm the original artist,
        # folder is the cover singer, title matches but artist doesn't → COVER.
        r = verify(ev(
            file_duration=210,
            folder_name="YukiSongs",
            filename="Kaleido Girl - YukiSongs",
            shazam={"title": "Kaleido Girl", "artist": "Yuzuki Ame"},
            itunes=None,                              # not catalog-confirmed
        ))
        self.assertEqual(r["verdict"], COVER)
        self.assertEqual(r["proposed"]["artist"], "YukiSongs")

    def test_reupload_keeps_original_artist_not_folder(self):
        # Re-upload of the official song by a YouTube channel: folder is just the
        # uploader, but Shazam+iTunes confirm the REAL artist. Must NOT become a
        # COVER tagged with the uploader — keep the original artist.
        r = verify(ev(
            file_duration=200,
            folder_name="AnimeRipVault",              # uploader channel, not an artist
            filename="Yuzuki Ame & Sora Kanata - First Light",
            shazam={"title": "First Light",
                    "artist": "Yuzuki Ame & Sora Kanata"},
            itunes={"title": "First Light",
                    "artist": "Yuzuki Ame & Sora Kanata", "album": "Prism", "duration": 202},
        ))
        self.assertNotEqual(r["verdict"], COVER)
        self.assertNotEqual(r["proposed"]["artist"], "AnimeRipVault")
        self.assertIn("Sora Kanata", r["proposed"]["artist"])

    def test_no_fingerprint_no_match(self):
        r = verify(ev(filename="mystery clip", shazam=None, acoustid=None))
        self.assertEqual(r["verdict"], NO_MATCH)
        self.assertIsNone(r["proposed"])

    def test_likely_without_acoustid(self):
        # Shazam + iTunes confirm, durations match, filename corroborates, but no
        # independent fingerprint agreement (AcoustID missing) → LIKELY, not VERIFIED.
        r = verify(ev(
            file_duration=200,
            folder_name="Sora Kanata",
            filename="Sora Kanata - Reverie",
            shazam={"title": "Reverie", "artist": "Sora Kanata"},
            acoustid=None,
            itunes={"title": "Reverie", "artist": "Sora Kanata",
                    "album": "Reverie", "duration": 195},
        ))
        self.assertEqual(r["verdict"], LIKELY)
        self.assertFalse(r["signals"]["S1"]["pass"])
        self.assertTrue(r["signals"]["S2"]["pass"])

    # --- regression tests for bugs found during real test runs -----------------

    def test_strong_acoustid_only_verifies(self):
        # Instrumental OST track: Shazam can't hear it, AcoustID is 0.999 sure,
        # duration matches, filename has the title. Must VERIFY (not UNVERIFIED).
        r = verify(ev(
            file_duration=114.2,
            folder_name="Prism Studio_Aoi Rhythm",
            filename="1-03 First Light",
            shazam=None,
            acoustid={"title": "First Light", "artist": "Prism Studio & Aoi Rhythm",
                      "duration": 114.2, "score": 0.999, "album": "Astral Voyage OST"},
            itunes=None,
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        self.assertEqual(r["proposed"]["artist"], "Prism Studio & Aoi Rhythm")

    def test_wrong_itunes_does_not_poison(self):
        # AcoustID is right; iTunes returns a DIFFERENT track. Its duration/album
        # must be ignored (S3 uses AcoustID's 114.2==114.2), not break the verdict.
        r = verify(ev(
            file_duration=114.2,
            folder_name="Prism Studio",
            filename="1-40 Reverie",
            shazam=None,
            acoustid={"title": "Reverie", "artist": "Prism Studio & Aoi Rhythm",
                      "duration": 114.2, "score": 0.999, "album": "Astral Voyage OST"},
            itunes={"title": "Unrelated Track", "artist": "Someone Else", "album": "Wrong Album",
                    "duration": 300, "art_url": "http://wrong/art.jpg"},
        ))
        self.assertTrue(r["signals"]["S3"]["pass"])          # used AcoustID duration
        self.assertNotEqual(r["proposed"]["album"], "Wrong Album")
        self.assertNotEqual(r["proposed"]["art_url"], "http://wrong/art.jpg")

    def test_cover_keyword_inside_brackets(self):
        # 【歌ってみた】 / 【ENGLISH COVER】 must be detected even though clean_title
        # strips bracket content for the other signals.
        r = verify(ev(
            file_duration=200, folder_name="MapleUta",
            filename="星空メモリー／MapleUta",
            filename_original="星空メモリー／MapleUta【歌ってみた】",
            shazam={"title": "星空メモリー", "artist": "Yuzuki Ame"},
        ))
        self.assertTrue(r["signals"]["S5"]["pass"])
        self.assertIn("歌ってみた", r["signals"]["S5"]["value"])

    def test_cover_substring_not_a_false_positive(self):
        # A band named "Silver Coverage" must NOT be flagged as a cover.
        r = verify(ev(
            file_duration=225, folder_name="Silver Coverage",
            filename="12 Neon Lullaby",
            shazam={"title": "Neon Lullaby", "artist": "Silver Coverage"},
            acoustid={"title": "Neon Lullaby", "artist": "Silver Coverage",
                      "duration": 224, "score": 0.98, "album": "Prism"},
            itunes={"title": "Neon Lullaby", "artist": "Silver Coverage",
                    "album": "Prism", "duration": 224},
        ))
        self.assertFalse(r["signals"]["S5"]["pass"])
        self.assertEqual(r["verdict"], VERIFIED)


class TestReviewOptions(unittest.TestCase):
    """§8 — the four always-present scenario options + trusted-title/art rules.
    All fixtures are synthetic (fictional artists/songs/albums); they reproduce the
    evidence shapes the option builder must handle, not any real library data."""

    def test_all_agree_suggests_matched_with_itunes_album(self):
        # §8.1 — Shazam+AcoustID+iTunes all agree → VERIFIED, option 1 suggested,
        # album/art from the trusted iTunes hit.
        r = verify(ev(
            file_duration=191.232, folder_name="Mochizuki Rei",
            filename='Aigata (From "Astral Voyage the Movie")',
            shazam={"title": 'Aigata (From "Astral Voyage the Movie")', "artist": "Mochizuki Rei",
                    "album": 'Aigata (From "Astral Voyage the Movie") - Single',
                    "art_url": "http://x/sz.jpg"},
            acoustid={"title": 'Aigata (From "Astral Voyage the Movie")', "artist": "Mochizuki Rei",
                      "duration": 191.214, "score": 0.985},
            itunes={"title": 'Aigata (From "Astral Voyage the Movie")', "artist": "Mochizuki Rei",
                    "album": 'Aigata (From "Astral Voyage the Movie") - Single',
                    "duration": 191.2, "art_url": "http://x/it.jpg",
                    "preview_url": "http://x/p.m4a"},
        ))
        self.assertEqual(r["verdict"], VERIFIED)
        o = r["options"]
        self.assertEqual(o["suggested"], "matched")
        self.assertEqual(o["matched"]["album"], 'Aigata (From "Astral Voyage the Movie") - Single')
        self.assertEqual(o["matched"]["album_source"], "iTunes")
        self.assertEqual(o["matched"]["art_url"], "http://x/it.jpg")
        self.assertFalse(o["matched"]["itunes_rejected"])
        self.assertIsNone(o["acoustid_alt"])

    def test_acoustid_miscredit_offers_extra_option(self):
        # §8.2 — Shazam+iTunes say Mochizuki Rei, AcoustID says nekomelody. Option 1
        # stays Mochizuki Rei (LIKELY, no independent fingerprint agreement), and the
        # disagreement is offered as a one-click "It's nekomelody".
        r = verify(ev(
            file_duration=212.328, folder_name="MapleUta (Mochizuki Rei)",
            filename="Prismatic Hearts - KALEIDO - ENGLISH Ver - Mochizuki Rei",
            shazam={"title": 'Kaleido Girl (From "Prismatic Hearts")', "artist": "Mochizuki Rei",
                    "album": 'Kaleido Girl (From "Prismatic Hearts") - Single', "art_url": "http://x/sz.jpg"},
            acoustid={"title": 'Kaleido Girl (From "Prismatic Hearts")', "artist": "nekomelody",
                      "duration": 213.0, "score": 0.9787},
            itunes={"title": 'Kaleido Girl (From "Prismatic Hearts")', "artist": "Mochizuki Rei",
                    "album": 'Kaleido Girl (From "Prismatic Hearts") - Single', "duration": 212.4,
                    "art_url": "http://x/it.jpg", "preview_url": "http://x/p.m4a"},
        ))
        o = r["options"]
        self.assertEqual(o["matched"]["artist"], "Mochizuki Rei")
        self.assertIsNotNone(o["acoustid_alt"])
        self.assertEqual(o["acoustid_alt"]["artist"], "nekomelody")

    def test_itunes_wrong_song_rejected_and_flagged(self):
        # §8.3 — iTunes returned a *different* song than the fingerprint; its
        # album/art must be dropped from option 1 and the rejection made visible.
        r = verify(ev(
            file_duration=179.232, folder_name="nekomelody",
            filename="Neon Syndicate - STARLIT (feat. Nova Lark)",
            shazam={"title": "Starlit (feat. Nova Lark & Deep Tide)",
                    "artist": "nekomelody", "album": "Starlit - Single",
                    "art_url": "http://x/sz.jpg"},
            itunes={"title": "Villain Vibes", "artist": "Somebody Else",
                    "album": "Villain Vibes - Single", "duration": 180,
                    "art_url": "http://x/wrong.jpg"},
        ))
        o = r["options"]
        self.assertTrue(o["matched"]["itunes_rejected"])
        # album/art come from Shazam, never the wrong iTunes hit
        self.assertNotEqual(o["matched"]["album"], "Villain Vibes - Single")
        self.assertNotEqual(o["matched"]["art_url"], "http://x/wrong.jpg")
        self.assertEqual(o["matched"]["album_source"], "Shazam")

    def test_garbage_fingerprint_uses_title_from_filename(self):
        # §8.4 — Shazam+iTunes both echo an unrelated 1-hour video. The fingerprint
        # title is garbage (not trusted), so options 2/3 fall back to the filename
        # title, and option 1 is NOT suggested.
        garbage = "\"Ascending to Heaven' a ONE Hour Musical Journey to the Afterlife"
        r = verify(ev(
            file_duration=230.55, folder_name="Mochizuki Rei",
            filename="Starfall OST - Aigata ENGLISH ver Mochizuki Rei",
            shazam={"title": garbage, "artist": "Random Uploader", "album": garbage,
                    "art_url": "http://x/dark.jpg"},
            itunes={"title": garbage, "artist": "Random Uploader", "album": garbage,
                    "duration": 3992.9, "art_url": "http://x/dark.jpg"},
        ))
        o = r["options"]
        self.assertFalse(o["title_trusted"])
        self.assertNotEqual(o["cover"]["title"], garbage)
        self.assertEqual(o["cover"]["title"], o["title_from_filename"])
        self.assertEqual(o["original"]["title"], o["title_from_filename"])
        self.assertIn("Aigata", o["title_from_filename"])
        self.assertNotEqual(o["suggested"], "matched")

    def test_cover_by_folder_misfire_suggests_matched(self):
        # §8.5 — S1 pass + S2 fail + folder mismatch → option 1 suggested, never cover.
        r = verify(ev(
            file_duration=240, folder_name="FanChannel99",
            filename="Prismatic Hearts OP1 - First Light",
            shazam={"title": "First Light", "artist": "Hoshino Rin"},
            acoustid={"title": "First Light", "artist": "Hoshino Rin",
                      "duration": 240, "score": 0.96},
            itunes=None,
        ))
        self.assertEqual(r["options"]["suggested"], "matched")

    def test_cover_and_original_never_borrow_art(self):
        # §8.6 — options 2/3 have art_url None even when iTunes/Shazam art exists.
        r = verify(ev(
            file_duration=250, folder_name="Lumina",
            filename="星空メモリー 歌ってみた",
            shazam={"title": "星空メモリー", "artist": "Yuzuki Ame", "album": "Astralis",
                    "art_url": "http://x/sz.jpg"},
            itunes={"title": "星空メモリー", "artist": "Yuzuki Ame", "duration": 258,
                    "art_url": "http://x/it.jpg"},
        ))
        o = r["options"]
        self.assertIsNone(o["cover"]["art_url"])
        self.assertIsNone(o["original"]["art_url"])
        # the matched (option 1) reading still keeps art
        self.assertTrue(o["matched"]["art_url"])
        # and the legacy COVER proposal no longer borrows the original's art (§5)
        self.assertIsNone(r["proposed"]["art_url"])

    def test_title_from_filename_units(self):
        # §8.7
        self.assertEqual(title_from_filename("【歌ってみた】星空メモリー", "Lumina"), "星空メモリー")
        self.assertEqual(title_from_filename("Neon Lullaby ENGLISH Ver - Lumina", "Lumina"), "Neon Lullaby")
        self.assertEqual(
            title_from_filename("FanChannel99 - First Light [Cover]", "FanChannel99"), "First Light")
        self.assertEqual(title_from_filename("星空メモリー／Yuzuki Ame", "Yuzuki Ame"), "星空メモリー")
        self.assertEqual(
            title_from_filename("Kaleido Girl (English Cover)「Aigata OP」【HoshiCovers】",
                                "HoshiCovers"), "Kaleido Girl")
        # parenthetical uploader alias is stripped from either form
        self.assertNotIn("Lumina", title_from_filename("Song X - Lumina", "MapleUta (Lumina)"))
        self.assertEqual(title_from_filename("", None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
