"""Import lists: chart parsing, album matching, artist adding with the listed album monitored."""

from datetime import date

from chordarr.models import Artist, ImportList, Release
from chordarr.services import importlists

APPLE = {"feed": {"title": "Classement des albums", "results": [
    {"artistName": "Tiakola", "name": "WpointM", "url": "https://music.apple.com/fr/album/1"},
    {"artistName": "Mylène Farmer", "name": "ÉGRÉGORE", "url": "https://music.apple.com/fr/album/2"},
    {"artistName": "Mauvais Djo", "name": "L'undertaker, Pt.2 (Deluxe)", "url": ""},
]}}
DEEZER = {"data": [{"artist": {"name": "TeddyBear"}, "title": "Le Plus Beau", "link": "https://www.deezer.com/album/1075783372"}], "total": 1}


async def test_fetch_items_parses_both_charts(monkeypatch):
    import httpx

    class R:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    async def fake_get(self, url, params=None):
        return R(APPLE if "apple" in url else DEEZER)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    apple = await importlists.fetch_items(ImportList(name="a", type="apple_music_charts", config={"country": "fr", "count": 100}))
    assert [(i.artist, i.album) for i in apple][:2] == [("Tiakola", "WpointM"), ("Mylène Farmer", "ÉGRÉGORE")]
    deezer = await importlists.fetch_items(ImportList(name="d", type="deezer_chart", config={"count": 50}))
    assert deezer[0].album == "Le Plus Beau" and deezer[0].url.endswith("1075783372")


async def test_process_adds_artist_and_monitors_only_the_listed_album(monkeypatch):
    from sqlalchemy import delete

    from chordarr.db import SessionLocal, init_db
    from chordarr.metadata import musicbrainz as mb
    from chordarr.services import artists as artist_service

    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(Artist))
        lst = ImportList(name="Top FR", type="apple_music_charts", config={"country": "fr", "count": 10}, monitor="album")
        session.add(lst)
        await session.flush()

        async def fake_fetch(_lst):
            return [importlists.ListItem("Tiakola", "WpointM"), importlists.ListItem("Tiakola", "Unknown record"), importlists.ListItem("Nobody Here", "X")]

        async def fake_search(term, limit=10):
            return [mb.ArtistInfo(mbid="tiak-mbid", name="Tiakola")] if "Tiakola" in term else []

        async def fake_add(session_, mbid, **kw):
            a = Artist(mbid=mbid, name="Tiakola", sort_name="Tiakola", monitored=kw.get("monitor") != "none")
            session_.add(a)
            await session_.flush()
            session_.add_all([
                Release(artist_id=a.id, mbid="rg-w", title="WpointM", kind="own", primary_type="Album", monitored=False, first_release_date=date(2025, 10, 1)),
                Release(artist_id=a.id, mbid="rg-m", title="Mélo", kind="own", primary_type="Album", monitored=False, first_release_date=date(2023, 1, 1)),
            ])
            await session_.flush()
            assert kw["monitor"] == "none" and kw["with_appearances"] is False
            return a

        monkeypatch.setattr(importlists, "fetch_items", fake_fetch)
        monkeypatch.setattr(mb, "search_artists", fake_search)
        monkeypatch.setattr(artist_service, "add_artist", fake_add)
        summary = await importlists.process(session, lst)
        assert summary["artists_added"] == 1 and summary["albums_monitored"] == 1
        assert len(summary["release_ids"]) == 1
        assert any("Unknown record" in x for x in summary["not_found"]) and any("Nobody Here" in x for x in summary["not_found"])
        rel = await session.get(Release, summary["release_ids"][0])
        assert rel.title == "WpointM" and rel.monitored is True
        assert "1 artists added" in lst.last_summary
        await session.execute(delete(Artist))
        await session.delete(lst)
        await session.commit()


def test_clean_title_strips_apple_suffixes():
    assert importlists._clean_title("Instinct - EP") == "Instinct"
    assert importlists._clean_title("Sensation") == "Sensation"
