import sqlite3

from chordarr.services.lidarr_import import read_database


def test_read_lidarr_database(tmp_path):
    db = tmp_path / "lidarr.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE ArtistMetadata (Id INTEGER PRIMARY KEY, ForeignArtistId TEXT, Name TEXT);
        CREATE TABLE Artists (Id INTEGER PRIMARY KEY, ArtistMetadataId INTEGER, Path TEXT, Monitored INTEGER);
        INSERT INTO ArtistMetadata VALUES (1, 'f59c5520-5f46-4d2c-b2c4-822eabf53419', 'Linkin Park');
        INSERT INTO ArtistMetadata VALUES (2, '71ff695e-896a-4043-aea4-93eadde08d09', 'Damso');
        INSERT INTO Artists VALUES (10, 1, '/music/Linkin Park', 1);
        INSERT INTO Artists VALUES (11, 2, '/music/Damso', 0);
        """
    )
    con.commit()
    con.close()
    rows = read_database(db)
    assert [(r.name, r.monitored, r.path) for r in rows] == [
        ("Damso", False, "/music/Damso"),
        ("Linkin Park", True, "/music/Linkin Park"),
    ]
