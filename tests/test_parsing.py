import json
from datetime import date
from pathlib import Path

from chordarr.metadata import musicbrainz as mb
from chordarr.services.library import normalise

DAMSO = "71ff695e-896a-4043-aea4-93eadde08d09"


def test_partial_dates():
    assert mb.parse_partial_date("2024-05-17") == (date(2024, 5, 17), "day")
    assert mb.parse_partial_date("2024-05") == (date(2024, 5, 1), "month")
    assert mb.parse_partial_date("2024") == (date(2024, 1, 1), "year")
    assert mb.parse_partial_date("") == (None, "none")
    assert mb.parse_partial_date("????") == (None, "none")


def test_credit_string_and_mbids():
    credits = [
        {"name": "Booba", "joinphrase": " feat. ", "artist": {"id": "a"}},
        {"name": "Damso", "joinphrase": " & ", "artist": {"id": "b"}},
        {"name": "Gato", "joinphrase": "", "artist": {"id": "c"}},
    ]
    assert mb.credit_string(credits) == "Booba feat. Damso & Gato"
    assert mb.credit_mbids(credits) == ["a", "b", "c"]


def test_normalise_folder_names():
    assert normalise("Civilisation (2021)") == "civilisation"
    assert normalise("La fête est finie") == "la fete est finie"
    assert normalise("QALF infinity [2021]") == "qalf infinity"
    assert normalise("Perdu d’avance") == normalise("Perdu d'avance")


def test_track_artist_browse_groups_tracks(monkeypatch):
    """One page of a real ``release?track_artist`` answer, replayed offline."""
    page = json.loads((Path(__file__).parent / "fixture_track_artist.json").read_text())

    async def fake_get_json(self, path, params=None, retries=4):
        if params.get("offset", 0) == 0:
            return page
        return {"releases": [], "release-count": page["release-count"]}

    from chordarr.metadata import http

    monkeypatch.setattr(http.Client, "get_json", fake_get_json)

    import asyncio

    apps = asyncio.run(mb.artist_appearances(DAMSO))
    assert apps, "expected appearances"
    by_title = {a.release_group.title: a for a in apps}
    nero = by_title["Nero Nemesis"]
    assert nero.release_group.artist_credit == "Booba"
    assert [t.title for t in nero.tracks] == ["Pinocchio"]  # two editions, one recording
    assert nero.tracks[0].artist_credit == "Booba feat. Damso & Gato"
    assert DAMSO in nero.tracks[0].artist_mbids
    assert all(DAMSO not in a.release_group.artist_mbids for a in apps)
