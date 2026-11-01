"""Renaming and import of tagged files into the library layout."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from chordarr.config import settings
from chordarr.models import Artist, Release
from chordarr.services import importer
from tests.helpers import make_flac, make_mp3


def test_sanitize_and_format():
    assert importer.sanitize('AC/DC: "Back" <in> Black?') == "AC-DC- -Back- -in- Black-"
    assert importer.format_name("{nn} - {title}", nn="01", title="Intro") == "01 - Intro"
    assert importer.format_name("{nn} - {title}{missing}", nn="01", title="X") == "01 - X"


def test_read_tags_mp3_and_flac(tmp_path):
    mp3 = make_mp3(tmp_path / "03 - Song.mp3", title="Song", artist="Damso", album="QALF", tracknumber="3/12", rg="rg-1")
    t = importer.read_tags(mp3)
    assert (t.title, t.artist, t.album, t.track, t.rg_mbid) == ("Song", "Damso", "QALF", 3, "rg-1")
    flac = make_flac(tmp_path / "x.flac", title="Yo", artist="Damso", album="QALF", tracknumber="2", discnumber="1")
    t = importer.read_tags(flac)
    assert (t.title, t.track, t.disc) == ("Yo", 2, 1)
    untagged = tmp_path / "07 - Mystery track.flac"
    untagged.write_bytes(flac.read_bytes()[:42])  # not even parseable
    t = importer.read_tags(untagged)
    assert t.track == 7 and t.title == "Mystery track"


@pytest.fixture
def damso():
    artist = Artist(id=1, mbid="a", name="Damso", sort_name="Damso")
    release = Release(id=10, artist_id=1, mbid="rg-qalf", title="QALF Infinity", kind="own", first_release_date=date(2021, 4, 30), track_count=3)
    return artist, release


class FakeSession:
    """Just enough of AsyncSession for import_folder: add_event + flush + no media servers."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def get(self, model, pk):
        return None

    async def scalars(self, q):
        class R:
            def all(self):
                return []

        return R()


def test_import_hardlinks_and_renames(tmp_path, damso, monkeypatch):
    artist, release = damso
    monkeypatch.setattr(settings, "root_folder", tmp_path / "music")
    src = tmp_path / "dl" / "Damso - QALF Infinity (2021) FLAC"
    make_flac(src / "b.flac", title="Vantablack", artist="Damso", album="QALF Infinity", tracknumber="2/3", rg="rg-qalf")
    make_flac(src / "a.flac", title="MOROSE", artist="Damso", album="QALF Infinity", tracknumber="1/3", rg="rg-qalf")
    make_flac(src / "c.flac", title="Rose Marthe's Love", artist="Damso", album="QALF Infinity", tracknumber="3/3", rg="rg-qalf")
    (src / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    (src / "sample").mkdir()
    make_flac(src / "sample" / "s.flac", title="nope")

    session = FakeSession()
    result = asyncio.run(importer.import_folder(session, src, release, artist, mode="hardlink"))
    dest = tmp_path / "music" / "Damso" / "QALF Infinity (2021)"
    assert result.destination == dest
    assert sorted(p.name for p in dest.iterdir()) == ["01 - MOROSE.flac", "02 - Vantablack.flac", "03 - Rose Marthe's Love.flac", "cover.jpg"]
    assert (dest / "01 - MOROSE.flac").stat().st_nlink == 2  # hard link, source kept for seeding
    assert release.status == "have" and release.file_count == 3 and release.path == str(dest)
    assert any(getattr(e, "type", None) == "release_imported" for e in session.added)

    # Re-import is idempotent.
    result = asyncio.run(importer.import_folder(session, src, release, artist, mode="hardlink"))
    assert result.skipped == 3


def test_import_refuses_other_release_unless_forced(tmp_path, damso, monkeypatch):
    artist, release = damso
    monkeypatch.setattr(settings, "root_folder", tmp_path / "music")
    src = tmp_path / "dl" / "something"
    make_mp3(src / "01.mp3", title="Pinocchio", artist="Booba", album="Nero Nemesis", albumartist="Booba", tracknumber="1")
    with pytest.raises(importer.ImportError_, match="Nero Nemesis"):
        asyncio.run(importer.import_folder(FakeSession(), src, release, artist, mode="copy"))
    result = asyncio.run(importer.import_folder(FakeSession(), src, release, artist, mode="copy", force=True))
    assert result.warnings and (result.destination / "01 - Pinocchio.mp3").exists()


def test_import_untagged_files_numbered_in_order(tmp_path, damso, monkeypatch):
    artist, release = damso
    monkeypatch.setattr(settings, "root_folder", tmp_path / "music")
    monkeypatch.setattr(settings, "track_format", "{track:02d}. {title}")
    src = tmp_path / "dl" / "qalf"
    for name in ("B - second.flac", "A - first.flac"):
        make_flac(src / name, title=name[4:-5])
    result = asyncio.run(importer.import_folder(FakeSession(), src, release, artist, mode="move"))
    assert sorted(p.name for p in result.destination.iterdir()) == ["01. first.flac", "02. second.flac"]
    assert not src.exists() or not list(src.iterdir())
    assert release.status == "partial"  # 2 of 3 tracks


def test_multi_disc_gets_disc_prefix(tmp_path, damso, monkeypatch):
    artist, release = damso
    monkeypatch.setattr(settings, "root_folder", tmp_path / "music")
    src = tmp_path / "dl" / "qalf"
    make_flac(src / "cd1" / "1.flac", title="One", album="QALF Infinity", tracknumber="1", discnumber="1")
    make_flac(src / "cd2" / "1.flac", title="Uno", album="QALF Infinity", tracknumber="1", discnumber="2")
    result = asyncio.run(importer.import_folder(FakeSession(), src, release, artist, mode="copy"))
    assert sorted(p.name for p in result.destination.iterdir()) == ["1-01 - One.flac", "2-01 - Uno.flac"]


def test_effective_mode():
    assert importer.effective_mode("torrent") == "hardlink"
    assert importer.effective_mode("usenet") == "move"


def test_map_path():
    from chordarr.models import DownloadClient
    from chordarr.services.downloadclients import map_path

    c = DownloadClient(name="q", implementation="qbittorrent", url="http://x", remote_path="/downloads", local_path="/mnt/dl")
    assert map_path(c, "/downloads/chordarr/Album") == Path("/mnt/dl/chordarr/Album")
    assert map_path(c, "/elsewhere/Album") == Path("/elsewhere/Album")
    assert map_path(DownloadClient(name="q", implementation="qbittorrent", url="http://x"), "/data/x") == Path("/data/x")


def test_same_album_edition_words_only():
    from chordarr.services.importer import format_name, same_album

    assert same_album("qalf", "qalf")
    assert same_album("qalf deluxe edition 2021", "qalf")
    assert not same_album("qalf infinity", "qalf")
    assert not same_album("alive", "live")
    assert format_name("{album} ({year})", album="Title", year="") == "Title"
    assert format_name("{album} ({year})", album="Title", year=2020) == "Title (2020)"


def test_move_import_removes_the_emptied_source_folder(tmp_path, monkeypatch):
    from chordarr.config import settings
    from chordarr.models import Artist, Release
    from chordarr.services.importer import _import_files

    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(settings, "root_folder", root)
    src = tmp_path / "dl" / "Bekar - Alba (2025) [FLAC]"
    (src / "__artwork").mkdir(parents=True)
    (src / "01. Bekar - Intro.flac").write_bytes(b"fLaC" + b"\0" * 64)
    (src / "__artwork" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    artist = Artist(id=1, mbid="a", name="Bekar", sort_name="Bekar", path=str(root / "Bekar"))
    release = Release(id=1, artist_id=1, mbid="rg", title="Alba", kind="own")
    result = _import_files(src, release, artist, "move", force=True)
    assert len(result.files) == 1 and result.files[0].exists()
    assert not src.exists(), "the emptied download folder should be gone"
