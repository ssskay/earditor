#!/usr/bin/env python3
"""
tagger.py — write ID3 tags + embed cover art (based on V3 scanner.write_tags).

Writes title/artist/album and embeds artwork downloaded from a URL, then asks
Music.app to refresh + add to the Earditor playlist. Supports MP3 (ID3) and
falls back to mutagen's generic tag interface for m4a/flac.
"""

import logging
import os
import urllib.request

from mutagen.id3 import ID3, TIT2, TPE1, TALB, TIT1, APIC
from mutagen.mp3 import MP3

from itunes_bridge import refresh_and_add_to_playlist

logger = logging.getLogger("earditor.tagger")

# Teach mutagen's EasyMP4 the grouping atom (©grp) so write_generic can set
# Grouping on m4a/mp4 via the same easy interface it uses for title/artist (§6c).
try:  # pragma: no cover - registration side effect
    from mutagen.easymp4 import EasyMP4
    if "grouping" not in EasyMP4.Get:
        EasyMP4.RegisterTextKey("grouping", "\xa9grp")
except Exception as _e:  # noqa: F841
    logger.debug("EasyMP4 grouping registration skipped: %s", _e)


def _download_art(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        mime = "image/png" if url.lower().endswith(".png") else "image/jpeg"
        return data, mime
    except Exception as e:
        logger.warning("Art download failed (%s): %s", url, e)
        return None, None


def extract_embedded_art(filepath):
    """
    Return (data, mime) of a file's embedded cover art, or (None, None).
    Handles MP3 (APIC), MP4/m4a (covr), and FLAC (Picture).
    """
    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3
            frames = ID3(filepath).getall("APIC")
            if frames:
                return frames[0].data, frames[0].mime or "image/jpeg"
        elif ext in (".m4a", ".mp4", ".m4b", ".m4v"):
            from mutagen.mp4 import MP4, MP4Cover
            mp4 = MP4(filepath)
            covr = mp4.tags.get("covr") if mp4.tags else None
            if covr:
                cover = covr[0]
                mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(cover), mime
        elif ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(filepath).pictures
            if pics:
                return pics[0].data, pics[0].mime or "image/jpeg"
    except Exception as e:
        logger.debug("art extract failed for %s: %s", filepath, e)
    return None, None


def write_mp3(filepath, tags):
    audio = MP3(filepath, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    # Preserve the file's existing embedded artwork (often the YouTube thumbnail)
    # so we never blank good art — used when no new art_url is supplied (e.g. covers
    # that aren't on iTunes).
    existing_art = audio.tags.getall("APIC") if audio.tags else []
    if audio.tags is not None:
        audio.tags.clear()

    if tags.get("title"):
        audio.tags.add(TIT2(encoding=3, text=tags["title"]))
    if tags.get("artist"):
        audio.tags.add(TPE1(encoding=3, text=tags["artist"]))
    if tags.get("album"):
        audio.tags.add(TALB(encoding=3, text=tags["album"]))
    if tags.get("grouping"):
        # Grouping = ID3 TIT1 (§6c) — marks catalog-verified covers so they're
        # smart-playlist-able without touching the artist's real album/art.
        audio.tags.add(TIT1(encoding=3, text=tags["grouping"]))

    if tags.get("art_url"):
        data, mime = _download_art(tags["art_url"])
        if data:
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        elif existing_art:
            audio.tags.add(existing_art[0])       # download failed → keep original
    elif existing_art:
        audio.tags.add(existing_art[0])           # no new art → keep YouTube thumbnail
    audio.save()


def _embed_art_generic(filepath, data, mime):
    """
    Embed cover art into a non-MP3 file. m4a/mp4 use an MP4Cover atom; flac uses a
    Picture block. Returns True on success. Best-effort: unsupported containers
    (e.g. .wav) or errors just leave existing art untouched.
    """
    ext = os.path.splitext(filepath)[1].lower()
    is_png = bool(mime and "png" in mime.lower())
    try:
        if ext in (".m4a", ".mp4", ".m4b", ".m4v"):
            from mutagen.mp4 import MP4, MP4Cover
            fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
            mp4 = MP4(filepath)
            mp4["covr"] = [MP4Cover(data, imageformat=fmt)]
            mp4.save()
            return True
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            pic = Picture()
            pic.type = 3                      # front cover
            pic.mime = mime or "image/jpeg"
            pic.desc = "Cover"
            pic.data = data
            fl = FLAC(filepath)
            fl.clear_pictures()
            fl.add_picture(pic)
            fl.save()
            return True
        logger.info("No cover-art support for %s files — leaving art as-is", ext)
    except Exception as e:
        logger.warning("Art embed failed for %s: %s", os.path.basename(filepath), e)
    return False


def write_generic(filepath, tags):
    """Fallback for m4a/flac/etc via mutagen's easy interface + cover art."""
    import mutagen
    mf = mutagen.File(filepath, easy=True)
    if mf is None:
        raise ValueError(f"Unsupported audio file: {filepath}")
    if tags.get("title"):
        mf["title"] = tags["title"]
    if tags.get("artist"):
        mf["artist"] = tags["artist"]
    if tags.get("album"):
        mf["album"] = tags["album"]
    if tags.get("grouping"):
        # MP4/m4a grouping atom is ©grp; EasyMP4 exposes it as "grouping". FLAC uses
        # a GROUPING Vorbis comment. Best-effort — skip if the format rejects the key.
        try:
            mf["grouping"] = tags["grouping"]
        except Exception:
            logger.debug("grouping not supported for %s", os.path.basename(filepath))
    mf.save()
    # Embed cover art (format-specific). When no new art_url is supplied we leave
    # the file's existing embedded art untouched — never blank good art.
    if tags.get("art_url"):
        data, mime = _download_art(tags["art_url"])
        if data:
            _embed_art_generic(filepath, data, mime)


def apply_tags(filepath, tags, playlist_name=None):
    """
    Write tags + art, then refresh Music.app and add to the playlist.
    `tags`: {title, artist, album, art_url}. Returns True on success.
    """
    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".mp3":
            write_mp3(filepath, tags)
        else:
            write_generic(filepath, tags)
        logger.info("✅ Tagged: %s — %s", tags.get("artist"), tags.get("title"))
    except Exception as e:
        logger.error("❌ Tag write failed for %s: %s", os.path.basename(filepath), e)
        return False

    # Push the (now-embedded) artwork straight into Music.app. A plain `refresh`
    # re-reads text tags but NOT cached artwork, so without this Music.app keeps
    # showing the old cover. Best-effort — text tags still updated if this fails.
    art_file = None
    data, mime = extract_embedded_art(filepath)
    if data:
        import tempfile
        suffix = ".png" if (mime and "png" in mime.lower()) else ".jpg"
        tf = tempfile.NamedTemporaryFile(prefix="earditor_art_", suffix=suffix, delete=False)
        tf.write(data)
        tf.close()
        art_file = tf.name

    kwargs = {"playlist_name": playlist_name} if playlist_name else {}
    if art_file:
        kwargs["art_file"] = art_file
    try:
        refresh_and_add_to_playlist(filepath, **kwargs)
    finally:
        if art_file:
            try:
                os.remove(art_file)
            except OSError:
                pass
    return True
