"""Tiny valid audio files for import tests (mutagen needs real headers)."""

from __future__ import annotations

import struct
from pathlib import Path

MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413  # MPEG-1 layer III, 128 kbps, 44.1 kHz


def make_mp3(path: Path, **tags) -> Path:
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * 8)
    t = ID3()
    frames = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2, "tracknumber": TRCK, "discnumber": TPOS}
    for key, value in tags.items():
        if key in frames:
            t.add(frames[key](encoding=3, text=str(value)))
        elif key == "rg":
            t.add(TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=str(value)))
    t.save(path)
    return path


def make_flac(path: Path, **tags) -> Path:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + struct.pack(">Q", (44100 << 44) | (1 << 41) | (15 << 36) | 100) + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + streaminfo)
    f = FLAC(path)
    for key, value in tags.items():
        f["musicbrainz_releasegroupid" if key == "rg" else key] = str(value)
    f.save()
    return path
