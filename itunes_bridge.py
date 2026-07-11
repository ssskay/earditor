#!/usr/bin/env python3
"""
itunes_bridge.py — AppleScript integration with Music.app (from Shazamer V3).

On accept, refresh the track so new ID3 tags show immediately, and add it to the
Earditor playlist (created on first use).
"""

import logging
import subprocess

logger = logging.getLogger("earditor.itunes_bridge")

DEFAULT_PLAYLIST = "Earditor — Tagged"
# The playlist name before the Shazamer → Earditor rename. If it still exists and the
# new one doesn't, we rename it in place (below) so its tracks are preserved.
LEGACY_PLAYLIST = "Shazamer — Tagged"


def find_track_location(title, artist):
    """
    Current POSIX path of the library track with this name + artist, or None.
    Music.app relocates files after tagging, so an accepted track's stored path can
    go stale — undo uses this to find where the file actually lives now.
    """
    if not title:
        return None
    t = title.replace('"', '\\"')
    a = (artist or "").replace('"', '\\"')
    cond = f'name is "{t}"' + (f' and artist is "{a}"' if a else "")
    script = f'''
    tell application "Music"
        try
            set trk to (first track of library playlist 1 whose {cond})
            return POSIX path of (location of trk)
        on error
            return ""
        end try
    end tell
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
        path = (r.stdout or "").strip()
        return path or None
    except Exception as e:
        logger.debug("find_track_location failed: %s", e)
        return None


def refresh_and_add_to_playlist(filepath, playlist_name=DEFAULT_PLAYLIST, art_file=None):
    """
    Refresh the track's tags in Music.app and add it to `playlist_name`
    (creating the playlist if needed). Returns True on success.

    If `art_file` (a path to an image) is given, the track's artwork is set
    explicitly from it — a plain `refresh` does NOT re-read cached artwork, so this
    is what actually makes Music.app show the new cover.
    """
    # Escape embedded double-quotes for AppleScript string literals.
    fp = filepath.replace('"', '\\"')
    pl = playlist_name.replace('"', '\\"')
    legacy = LEGACY_PLAYLIST.replace('"', '\\"')
    art_block = ""
    if art_file:
        af = art_file.replace('"', '\\"')
        art_block = f'''
            try
                set data of artwork 1 of the_track to (read (POSIX file "{af}") as picture)
            end try'''
    script = f'''
    tell application "Music"
        try
            if not (exists playlist "{pl}") then
                if (exists playlist "{legacy}") then
                    -- rename the pre-Earditor playlist so its tracks are preserved
                    set name of playlist "{legacy}" to "{pl}"
                else
                    make new playlist with properties {{name:"{pl}"}}
                end if
            end if
            set the_file to POSIX file "{fp}"
            set the_track to add the_file to library playlist 1
            delay 0.5
            refresh the_track{art_block}
            delay 0.3
            duplicate the_track to playlist "{pl}"
            return "ok"
        on error err_msg
            return "error: " & err_msg
        end try
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout.strip()
        if output == "ok":
            logger.info("  ✅ Music.app refreshed + added to playlist “%s”", playlist_name)
            return True
        logger.warning("  ⚠ iTunes bridge: %s", output or result.stderr.strip())
        return False
    except Exception as e:
        logger.error("  iTunes bridge failed: %s", e)
        return False
