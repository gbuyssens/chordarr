"""Candidate parsing, quality detection and selection."""

from datetime import date
from pathlib import Path

from chordarr.models import Indexer, Release
from chordarr.services import acquisition, indexers
from chordarr.services.indexers import parse_caps, parse_feed, quality_of

FEED = (Path(__file__).parent / "fixture_torznab.xml").read_text()


def idx(**kw) -> Indexer:
    defaults = dict(id=1, name="TR4KER", implementation="Torznab", base_url="http://prowlarr:9696/1/", api_key="k", categories=[3000, 3010, 3040], minimum_seeders=1, priority=25, capabilities={})
    return Indexer(**{**defaults, **kw})


def test_parse_feed_reads_torznab_attributes():
    items = parse_feed(FEED, idx())
    assert len(items) == 4
    first = items[0]
    assert first.title.startswith("Damso - QALF Infinity")
    assert first.size == 362205550 and first.seeders == 12 and first.leechers == 14
    assert first.categories == [3000, 3040]
    assert first.download_url.startswith("http://prowlarr:9696/1/download")
    assert first.published and first.published.year == 2026
    assert first.quality == "lossless" and first.protocol == "torrent"
    assert items[1].size == 120000000  # enclosure length fallback
    assert items[1].quality == "high"


def test_parse_feed_error_document():
    import pytest

    with pytest.raises(ValueError, match="100"):
        parse_feed('<?xml version="1.0"?><error code="100" description="Incorrect user credentials"/>', idx())


def test_quality_detection():
    assert quality_of("Artist - Album (2020) FLAC") == "lossless"
    assert quality_of("Artist - Album 24bit WEB") == "lossless"
    assert quality_of("Artist.Album.MP3.320") == "high"
    assert quality_of("Damso - BeYaH.2025.MP3.320KBPS-G11") == "high"
    assert quality_of("Artist - Album [FLAC 16BITS 44.1KHZ]") == "lossless"
    assert quality_of("Artist - Album V0") == "high"
    assert quality_of("Artist - Album [MP3 192]") == "low"
    assert quality_of("Artist - Album") == "unknown"
    assert quality_of("Artist - Album", [3040]) == "lossless"


def test_parse_caps():
    caps = parse_caps(
        '<caps><searching><search available="yes" supportedParams="q"/><music-search available="yes" supportedParams="q,artist,album"/></searching>'
        '<categories><category id="3000" name="Audio"><subcat id="3040" name="Audio/Lossless"/></category></categories></caps>'
    )
    assert caps["music_params"] == ["album", "artist", "q"]
    assert caps["categories"] == {"3000": "Audio", "3040": "Audio/Lossless"}


def test_matching_ignores_accents_and_separators():
    c = indexers.Candidate(title="Damso.Ipseite.2017.FLAC-GRP", guid="g", download_url="u", indexer_id=1, indexer_name="x", protocol="torrent")
    assert acquisition.matches_release(c, "Damso", "Ipséité")
    assert not acquisition.matches_release(c, "Damso", "QALF Infinity")
    assert not acquisition.matches_release(c, "Booba", "Ipséité")


def test_evaluate_ranks_by_profile_quality_and_seeders():
    release = Release(id=1, mbid="rg", title="QALF Infinity", kind="own", first_release_date=date(2021, 4, 30))
    items = parse_feed(FEED, idx())
    ranked = acquisition.evaluate(items, "Damso", release, "lossless", {1: idx()})
    accepted = [c for c in ranked if c.accepted]
    assert [c.guid.rsplit("/", 1)[1] for c in accepted] == ["qalf-flac"]
    rejected = {c.guid.rsplit("/", 1)[1]: c.rejections for c in ranked if not c.accepted}
    assert any("profile" in r for r in rejected["qalf-mp3"])
    assert any("seeders" in r for r in rejected["qalf-24"])
    assert any("does not name" in r for r in rejected["ipseite"])

    ranked = acquisition.evaluate(items, "Damso", release, "any", {1: idx(minimum_seeders=0)})
    order = [c.guid.rsplit("/", 1)[1] for c in ranked if c.accepted]
    assert order[0] in ("qalf-flac", "qalf-24")  # lossless first
    assert order[-1] == "qalf-mp3"
    assert ranked[0].score > ranked[-1].score

    ranked = acquisition.evaluate(items, "Damso", release, "lossless", {1: idx()}, active_guids={"https://tracker.example/torrents/qalf-flac"})
    assert "already in the queue" in ranked[0].rejections or not ranked[0].accepted


def test_search_uses_text_query_when_indexer_has_no_artist_param(monkeypatch):
    calls = []

    async def fake_fetch(indexer, params, timeout=40):
        calls.append(params)
        return FEED

    monkeypatch.setattr(indexers, "fetch", fake_fetch)
    import asyncio

    asyncio.run(indexers.search(idx(capabilities={"music_params": ["q"]}), "Damso", "QALF Infinity"))
    assert calls[0]["t"] == "music" and calls[0]["q"] == "Damso QALF Infinity" and "artist" not in calls[0]
    calls.clear()
    asyncio.run(indexers.search(idx(capabilities={"music_params": ["q", "artist", "album"]}), "Damso", "QALF Infinity"))
    assert calls[0]["artist"] == "Damso" and calls[0]["album"] == "QALF Infinity"


def test_superset_title_is_another_release():
    from chordarr.services.acquisition import matches_release
    from chordarr.services.indexers import Candidate

    c = Candidate(title="Damso - QALF infinity (2021) [FLAC]", guid="g", download_url="u", indexer_id=1, indexer_name="i", protocol="torrent")
    assert matches_release(c, "Damso", "QALF infinity")
    assert matches_release(c, "Damso", "QALF")  # without context it still looks like a match…
    assert not matches_release(c, "Damso", "QALF", other_titles=("QALF infinity", "Ipséité"))  # …but not next to its sibling
    deluxe = Candidate(title="Damso - QALF (Deluxe Edition) FLAC", guid="g2", download_url="u", indexer_id=1, indexer_name="i", protocol="torrent")
    assert matches_release(deluxe, "Damso", "QALF", other_titles=("QALF infinity",))
