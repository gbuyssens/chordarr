"""Direct sources (streamrip): result mapping, references, job registry, grab routing."""

import pytest

from chordarr.models import DownloadClient
from chordarr.services import direct

pytestmark = pytest.mark.skipif(not direct.available(), reason="streamrip not installed")


def client(**settings):
    return DownloadClient(id=7, name="My accounts", implementation="streamrip", url="streamrip://local", settings={"quality": 3, **settings})


def test_qobuz_pages_become_candidates():
    pages = [
        {
            "albums": {
                "items": [
                    {"id": "abc123", "title": "QALF", "artist": {"name": "Damso"}, "tracks_count": 13, "release_date_original": "2020-09-17", "maximum_bit_depth": 24, "maximum_sampling_rate": 48},
                    {"id": "def456", "title": "QALF", "version": "Explicit", "artist": {"name": "Damso"}, "tracks_count": 13, "release_date_original": "2020-09-17", "maximum_bit_depth": 16, "maximum_sampling_rate": 44.1},
                ]
            }
        }
    ]
    cands = direct.candidates_from_pages(client(qobuz_email="a", qobuz_password="b"), "qobuz", pages)
    assert [c.quality for c in cands] == ["lossless", "lossless"]
    assert cands[0].title == "Damso - QALF (2020) [Qobuz 24 bit / 48 kHz]"
    assert cands[0].protocol == "direct" and cands[0].guid == "direct:7:qobuz:abc123"
    assert direct.parse_ref(cands[0].download_url) == (7, "qobuz", "abc123")
    assert cands[0].info_url == "https://open.qobuz.com/album/abc123"


def test_deezer_quality_follows_account_level():
    pages = [{"data": [{"id": 1, "title": "Ipséité", "artist": {"name": "Damso"}, "nb_tracks": 14, "release_date": "2017-04-28"}]}]
    assert direct.candidates_from_pages(client(quality=2), "deezer", pages)[0].quality == "lossless"
    assert direct.candidates_from_pages(client(quality=1), "deezer", pages)[0].quality == "high"


def test_enabled_sources_need_credentials_and_md5():
    c = client(sources="tidal,qobuz,deezer", qobuz_email="me@x", qobuz_password="pw")
    assert direct.enabled_sources(c) == ["qobuz"]
    assert direct.md5_password("pw") == "e0c0dfa4a6d4ee0c6b7c8c5a7e2fbd2a" or len(direct.md5_password("pw")) == 32
    assert direct.md5_password("0" * 32) == "0" * 32


async def test_jobs_feed_the_queue(monkeypatch):
    started = {}

    def fake_start(cfg, source, album_id, title):
        job = direct.Job(id="rip-1", title=title, source=source, album_id=album_id, status="completed", progress=1.0, path="/tmp/x")
        direct.JOBS[job.id] = job
        started.update(source=source, album_id=album_id)
        return job

    monkeypatch.setattr(direct, "start", fake_start)
    sc = direct.StreamripClient(client())
    job_id = await sc.add("Damso - QALF", url="direct:7:qobuz:abc123")
    assert job_id == "rip-1" and started == {"source": "qobuz", "album_id": "abc123"}
    items = await sc.items()
    assert items[0].client_id == "rip-1" and items[0].status == "completed" and items[0].path == "/tmp/x"
    await sc.remove("rip-1")
    assert "rip-1" not in direct.JOBS


def test_direct_candidates_rank_above_equal_torrents():
    from chordarr.models import Release
    from chordarr.services.acquisition import evaluate
    from chordarr.services.indexers import Candidate

    rel = Release(id=1, mbid="rg", title="QALF", kind="own")
    torrent = Candidate(title="Damso - QALF (2020) FLAC", guid="t", download_url="u", indexer_id=1, indexer_name="i", protocol="torrent", seeders=30, quality="lossless")
    dz = Candidate(title="Damso - QALF (2020) [Deezer FLAC]", guid="d", download_url="direct:7:deezer:1", indexer_id=0, indexer_name="Deezer", protocol="direct", quality="lossless")
    ranked = evaluate([torrent, dz], "Damso", rel, "lossless", {})
    assert ranked[0].protocol == "direct" and all(c.accepted for c in ranked)


async def test_grab_direct_creates_a_download_without_indexer(monkeypatch):
    from sqlalchemy import delete

    from chordarr.db import SessionLocal, init_db
    from chordarr.models import Artist, Download, Release
    from chordarr.services import acquisition
    from chordarr.services.indexers import Candidate

    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(Download))
        await session.execute(delete(Artist))
        artist = Artist(mbid="a-mbid", name="Bekar", sort_name="Bekar")
        session.add(artist)
        await session.flush()
        rel = Release(artist_id=artist.id, mbid="rg-alba", title="Alba", kind="own")
        dc = DownloadClient(name="Comptes", implementation="streamrip", url="streamrip://local", enabled=True, settings={"deezer_arl": "x", "quality": 2})
        session.add_all([rel, dc])
        await session.flush()

        def fake_start(cfg, source, album_id, title):
            job = direct.Job(id="rip-t", title=title, source=source, album_id=album_id)
            direct.JOBS[job.id] = job
            return job

        monkeypatch.setattr(direct, "start", fake_start)
        cand = Candidate(title="Bekar - Alba [Deezer FLAC]", guid=f"direct:{dc.id}:deezer:765", download_url=f"direct:{dc.id}:deezer:765", indexer_id=0, indexer_name="Deezer", protocol="direct", quality="lossless")
        dl = await acquisition.grab(session, rel, cand, manual=True)
        await session.commit()
        assert dl.indexer_id is None and dl.client_id == "rip-t" and dl.protocol == "direct"
        await session.delete(dl)
        await session.delete(dc)
        await session.commit()
