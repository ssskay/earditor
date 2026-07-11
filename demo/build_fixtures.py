#!/usr/bin/env python3
"""
build_fixtures.py — generate demo/fixtures.json for `review.py --demo`.

Everything here is SYNTHETIC. Fictional uploaders, songs, and artists are fed
as evidence through the *real* verify.verify() pipeline, so the resulting rows
have authentic signal/verdict/option shapes with zero real-library data. Nothing
below maps to a real channel, utaite, or anything in anyone's actual library.

Run:  python3 demo/build_fixtures.py    # rewrites demo/fixtures.json
"""

import base64
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import verify  # noqa: E402


# --- self-contained album art (data: URIs, so the demo needs no network) -------
def art(hue, glyph="♪"):
    """A small gradient square with a glyph — a stand-in for cover art, inline."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="hsl({hue},62%,58%)"/>'
        f'<stop offset="1" stop-color="hsl({(hue + 40) % 360},58%,38%)"/>'
        f'</linearGradient></defs>'
        f'<rect width="300" height="300" fill="url(#g)"/>'
        f'<text x="150" y="192" font-size="150" text-anchor="middle" '
        f'fill="rgba(255,255,255,.92)" font-family="Georgia,serif">{glyph}</text>'
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


ART_ASTRALIS = art(212)     # Yuzuki Ame — Astralis
ART_NEON = art(286)         # Sora Kanata — Neon Lullaby
ART_KALEIDO = art(28)       # Nagi Serizawa — Kaleido Girl
ART_STARLIT = art(340)      # wrong-song iTunes art (unverified)
ART_MIDNIGHT = art(258)     # Della Vaughn — Midnight Radio (the original's cover art)


def shazam(title, artist, album=None, art_url=None, genre="J-Pop"):
    return {"title": title, "artist": artist, "album": album, "art_url": art_url,
            "genre": genre, "apple_music_url": None, "shazam_key": "demo0000"}


def acoustid(title, artist, duration, album=None, score=0.97, date=2023):
    return {"score": score, "recording_id": "00000000-0000-4000-8000-000000000000",
            "title": title, "artist": artist, "duration": duration, "album": album,
            "releases": [{"title": album or title, "date": date}]}


def itunes(title, artist, album, duration, art_url, preview_url=""):
    return {"title": title, "artist": artist, "album": album, "art_url": art_url,
            "preview_url": preview_url, "duration": duration,
            "track_view_url": "https://example.com/track"}


# --- the fictional scenarios ---------------------------------------------------
# Each dict is the `evidence` verify.verify() consumes. Filepaths are fictional and
# point at nothing (demo hides the local player). Designed so the deterministic
# engine lands each row in the intended tier — the tiers below are assertions, the
# engine is the source of truth.
SCENARIOS = [
    # 1 — VERIFIED via the Topic-channel fast-pass (official artist upload).
    {
        "tier": "VERIFIED",
        "filepath": "/demo/Yuzuki Ame - Topic/Astralis/Hoshizora Memory.m4a",
        "file_duration": 214, "folder_name": "Yuzuki Ame", "folder_raw": "Yuzuki Ame - Topic",
        "filename": "Hoshizora Memory.m4a",
        "shazam": shazam("Hoshizora Memory", "Yuzuki Ame", "Astralis", ART_ASTRALIS),
        "acoustid": acoustid("Hoshizora Memory", "Yuzuki Ame", 214, "Astralis", 0.98),
        "itunes": itunes("Hoshizora Memory", "Yuzuki Ame", "Astralis", 214.3, ART_ASTRALIS),
    },
    # 2 — VERIFIED the normal way: S1 (fingerprint agreement) + S3 (duration) pass.
    {
        "tier": "VERIFIED",
        "filepath": "/demo/Sora Kanata/Neon Lullaby/Sora Kanata - Neon Lullaby.mp3",
        "file_duration": 199, "folder_name": "Sora Kanata",
        "filename": "Sora Kanata - Neon Lullaby.mp3",
        "shazam": shazam("Neon Lullaby", "Sora Kanata", "First Light EP", ART_NEON),
        "acoustid": acoustid("Neon Lullaby", "Sora Kanata", 199, "First Light EP", 0.96),
        "itunes": itunes("Neon Lullaby", "Sora Kanata", "First Light EP", 199.5, ART_NEON),
    },
    # 3 — LIKELY: iTunes catalog + duration + filename agree, but no independent
    #     fingerprint (AcoustID had no match), so it can't reach VERIFIED.
    {
        "tier": "LIKELY",
        "filepath": "/demo/Nagi Serizawa/Nagi Serizawa - Kaleido Girl.mp3",
        "file_duration": 227, "folder_name": "Nagi Serizawa",
        "filename": "Nagi Serizawa - Kaleido Girl.mp3",
        "shazam": shazam("Kaleido Girl", "Nagi Serizawa", "Prism", ART_KALEIDO),
        "acoustid": None,
        "itunes": itunes("Kaleido Girl", "Nagi Serizawa", "Prism", 227.0, ART_KALEIDO),
    },
    # 4 — LIKELY: a strong lone AcoustID (no Shazam) with filename corroboration,
    #     but the duration is off (S3 fails) — so LIKELY, not VERIFIED.
    {
        "tier": "LIKELY",
        "filepath": "/demo/Mochizuki Rei/Mochizuki Rei - Yumeutsutsu.flac",
        "file_duration": 300, "folder_name": "Mochizuki Rei",
        "filename": "Mochizuki Rei - Yumeutsutsu.flac",
        "shazam": None,
        "acoustid": acoustid("Yumeutsutsu", "Mochizuki Rei", 244, "Reverie", 0.94),
        "itunes": None,
    },
    # 5a — COVER (plain-English, sorts first): an uploader's cover of an English song.
    #      The "cover" keyword fires S5; the title is trustworthy from the filename;
    #      the catalog doesn't confirm the uploader → artist reassigned to them.
    {
        "tier": "COVER",
        "filepath": "/demo/Jesse Cormac/Runaway Sunday (Jesse Cormac cover).mp3",
        "file_duration": 218, "folder_name": "Jesse Cormac",
        "filename": "Runaway Sunday (Jesse Cormac cover).mp3",
        "shazam": shazam("Runaway Sunday", "Della Vaughn", "Midnight Radio", ART_MIDNIGHT,
                         genre="Pop"),
        "acoustid": None,
        "itunes": None,
    },
    # 5b — COVER: an explicit cover keyword (歌ってみた), title trustworthy from the
    #     filename, catalog doesn't confirm the uploader → artist reassigned to them.
    {
        "tier": "COVER",
        "filepath": "/demo/nekomelody/【歌ってみた】Hoshizora Memory covered by nekomelody.mp3",
        "file_duration": 213, "folder_name": "nekomelody",
        "filename": "【歌ってみた】Hoshizora Memory covered by nekomelody.mp3",
        "shazam": shazam("Hoshizora Memory", "Yuzuki Ame", "Astralis", ART_ASTRALIS),
        "acoustid": None,
        "itunes": None,
    },
    # 6 — COVER inferred from the folder: no keyword, but the uploader folder differs
    #     from the fingerprint's artist, the filename carries the title (not the
    #     artist), and the catalog doesn't confirm it.
    {
        "tier": "COVER",
        "filepath": "/demo/MapleUta/Yumeutsutsu (MapleUta ver).mp3",
        "file_duration": 231, "folder_name": "MapleUta",
        "filename": "Yumeutsutsu (MapleUta ver).mp3",
        "shazam": shazam("Yumeutsutsu", "Mochizuki Rei", None, None),
        "acoustid": None,
        "itunes": None,
    },
    # 7 — UNVERIFIED source conflict: Shazam and AcoustID name DIFFERENT artists for
    #     the same title → the "It's {artist}" one-click + red AcoustID agreement chip.
    {
        "tier": "UNVERIFIED",
        "filepath": "/demo/Yuzuki Ame/Aigata.mp3",
        "file_duration": 205, "folder_name": "Yuzuki Ame",
        "filename": "Aigata.mp3",
        "shazam": shazam("Aigata", "Yuzuki Ame", None, ART_ASTRALIS),
        "acoustid": acoustid("Aigata", "Mochizuki Rei", 205, "Reverie", 0.91),
        "itunes": None,
    },
    # 8 — UNVERIFIED generic: iTunes returned a DIFFERENT song (amber), filename
    #     doesn't corroborate, nothing independent confirms — candidates shown.
    {
        "tier": "UNVERIFIED",
        "filepath": "/demo/YukiSongs/track_047_final_mix.mp3",
        "file_duration": 188, "folder_name": "YukiSongs",
        "filename": "track_047_final_mix.mp3",
        "shazam": shazam("Starlit", "HoshiCovers", None, None),
        "acoustid": None,
        "itunes": itunes("Starlight Express", "The Broadway Cast", "Overtures",
                         242.0, ART_STARLIT),
    },
    # 9 — NO_MATCH: no fingerprint from any source. (Recorded as no_match; never
    #     rescanned, and not shown in the review queue — same as production.)
    {
        "tier": "NO_MATCH",
        "filepath": "/demo/Unsorted/unknown_upload_2019.wav",
        "file_duration": 176, "folder_name": "Unsorted",
        "filename": "unknown_upload_2019.wav",
        "shazam": None, "acoustid": None, "itunes": None,
    },
    # 10 — ALREADY_TAGGED: a file that already carried complete tags. Triage retires
    #      it without fingerprinting; it sits at the bottom of the queue for a glance.
    {
        "tier": "ALREADY_TAGGED",
        "filepath": "/demo/Sora Kanata/First Light EP/First Light.m4a",
        "file_duration": 208, "folder_name": "Sora Kanata",
        "filename": "First Light.m4a",
        "tagged": {"title": "First Light", "artist": "Sora Kanata", "album": "First Light EP"},
    },
]


def build_row(sc):
    """Turn one scenario into a stored-row dict via the real verify() pipeline."""
    filepath = sc["filepath"]
    display_name = Path(filepath).name
    if sc["tier"] == "ALREADY_TAGGED":
        return {
            "filepath": filepath, "tagged": sc["tagged"], "verdict": "ALREADY_TAGGED",
            "folder_name": sc["folder_name"], "display_name": display_name,
            "duration": sc.get("file_duration"),
        }
    evidence = {
        "file_duration": sc.get("file_duration"),
        "folder_name": sc.get("folder_name"),
        "folder_raw": sc.get("folder_raw"),
        "filename": sc.get("filename"),
        "filename_original": sc.get("filename"),
        "shazam": sc.get("shazam"),
        "acoustid": sc.get("acoustid"),
        "itunes": sc.get("itunes"),
    }
    result = verify.verify(evidence)
    got = result["verdict"]
    if got != sc["tier"]:
        raise SystemExit(
            f"tier mismatch for {display_name!r}: wanted {sc['tier']}, engine said {got}")
    return {
        "filepath": filepath,
        "verdict": got,
        "folder_name": sc.get("folder_name"),
        "display_name": display_name,
        "duration": sc.get("file_duration"),
        "proposed": result.get("proposed"),
        "shazam": sc.get("shazam"),
        "acoustid": sc.get("acoustid"),
        "itunes": sc.get("itunes"),
        "candidates": result.get("candidates"),
        "options": result.get("options"),
        "signals": result.get("signals"),
        "error": None,
    }


def main():
    rows = [build_row(sc) for sc in SCENARIOS]
    out = {
        "_comment": ("All data here is synthetic. Fictional uploaders, songs, and "
                     "artists were run through the real verify.py pipeline to produce "
                     "authentic signal/verdict shapes with zero real-library data. "
                     "Regenerate with: python3 demo/build_fixtures.py"),
        "rows": rows,
    }
    dest = HERE / "fixtures.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tiers = {}
    for r in rows:
        tiers[r["verdict"]] = tiers.get(r["verdict"], 0) + 1
    print(f"wrote {dest} — {len(rows)} rows: " +
          ", ".join(f"{k}×{v}" for k, v in sorted(tiers.items())))


if __name__ == "__main__":
    main()
