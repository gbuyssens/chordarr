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
    assert not matches_release(c, "Damso", "QALF")  # "infinity" is a real extra word
    assert not matches_release(c, "Damso", "QALF", other_titles=("QALF infinity", "Ipséité"))
    deluxe = Candidate(title="Damso - QALF (Deluxe Edition) FLAC", guid="g2", download_url="u", indexer_id=1, indexer_name="i", protocol="torrent")
    assert matches_release(deluxe, "Damso", "QALF", other_titles=("QALF infinity",))


def test_extra_words_tolerates_formats_and_group_tags():
    from chordarr.services.acquisition import extra_words, matches_release
    from chordarr.services.indexers import Candidate

    def cand(t):
        return Candidate(title=t, guid="g", download_url="u", indexer_id=1, indexer_name="i", protocol="torrent")

    assert extra_words("Damso - QALF [2020] [FLAC][16Bit 44.1kHz]", "Damso", "QALF") == []
    assert extra_words("Damso.QALF.infinity.2021.FLAC16bit.44.1kHz.Hugo00F", "Damso", "QALF") == ["infinity"]
    assert matches_release(cand("Linkin Park - Meteora (20th Anniversary Edition) WEB FLAC"), "Linkin Park", "Meteora")
    assert matches_release(cand("Orelsan - Civilisation 2021 FLAC 24Bits - EICHBAUM"), "Orelsan", "Civilisation")
    assert not matches_release(cand("Orelsan - Civilisation Edition Ultime 2022 FLAC"), "Orelsan", "Civilisation")


def test_appearances_search_the_credited_artist_whole_record():
    from chordarr.models import Release
    from chordarr.services.acquisition import credited_artist, matches_release
    from chordarr.services.indexers import Candidate

    feat = Release(id=1, mbid="rg", title="MAGIC / DEVANT LA MER", kind="appearance", artist_credit="twinsmatic")
    own = Release(id=2, mbid="rg2", title="Alba", kind="own", artist_credit="Bekar")
    comp = Release(id=3, mbid="rg3", title="Planète Rap 2024", kind="appearance", artist_credit="Various Artists")
    assert credited_artist(feat, "Bekar") == "twinsmatic"
    assert credited_artist(own, "Bekar") == "Bekar"
    assert credited_artist(comp, "Bekar") == ""
    c = Candidate(title="twinsmatic - MAGIC / DEVANT LA MER (2026) [Deezer FLAC]", guid="g", download_url="u", indexer_id=0, indexer_name="Deezer", protocol="direct")
    assert matches_release(c, credited_artist(feat, "Bekar"), feat.title)
    assert not matches_release(c, "Bekar", feat.title)
    va = Candidate(title="Various Artists - Planete Rap 2024 FLAC", guid="g", download_url="u", indexer_id=0, indexer_name="x", protocol="torrent")
    assert matches_release(va, credited_artist(comp, "Bekar"), comp.title)
