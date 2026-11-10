"""Soundtrack covers that must not split into look-alike albums:

* a short title-cased title next to an artist with a lowercase word ("Anime
  de Japan" – "Battle Ignition") is not taken for swapped fields: the shape of
  the strings alone proves nothing;
* loose "Artist - Title.mp3" files in the root of albums/ (a deemix download
  that finished after its timeout) are filed into Artist/Album/.
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import album_artist_merger as aam
from album_artist_merger import AlbumArtistMerger
from downloader import Downloader


def _dl(known=()):
    d = Downloader.__new__(Downloader)
    d._is_known_artist = lambda name: name in known
    return d


class TestSwap:
    def test_real_artist_with_a_small_word_is_not_flipped(self):
        d = _dl(known={"Anime de Japan"})
        for title in ("Battle Ignition", "Enemy Unseen", "Peaceful Afternoon", "Creeping Shadows", "Comical World"):
            assert d._clean_request_names("Anime de Japan", title) == ("Anime de Japan", title)

    def test_swapped_fields_are_still_flipped_when_the_title_is_the_artist(self):
        d = _dl(known={"Evanescence"})
        assert d._clean_request_names("Bring Me to Life", "Evanescence") == ("Evanescence", "Bring Me to Life")

    def test_shape_alone_never_flips(self):
        # Neither name confirmed as an artist (lookups unavailable): keep the order.
        assert _dl()._clean_request_names("Anime de Japan", "Battle Ignition") == ("Anime de Japan", "Battle Ignition")


class TestLateDeemix:
    def test_a_download_finishing_after_the_timeout_is_parked_outside_the_library(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        library = tmp_path / "albums"
        library.mkdir()
        done = threading.Event()
        d = Downloader.__new__(Downloader)
        d._DEEMIX_TRACK_TIMEOUT = 0.2

        def slow_deemix(query, target_dir, *a):
            time.sleep(0.6)
            f = Path(target_dir) / "Shiro Sagisu - Number One (Vocal Version).mp3"
            f.write_bytes(b"x")
            return True, str(f), False

        d._download_with_deemix = slow_deemix
        real_park = d._park_late_download

        def park(path, query):
            real_park(path, query)
            done.set()

        d._park_late_download = park
        assert d._download_with_deemix_timeout("q", str(library), None, "Shiro Sagisu", "Number One") == (False, None, False)
        assert done.wait(5)
        assert list(library.iterdir()) == []                                   # nothing loose in albums/
        assert list((tmp_path / "data" / "late_downloads").rglob("*.mp3"))

    def test_a_download_in_time_is_returned_normally(self, tmp_path):
        d = Downloader.__new__(Downloader)
        d._DEEMIX_TRACK_TIMEOUT = 5
        d._download_with_deemix = lambda *a: (True, "/music/albums/A/B/c.mp3", False)
        assert d._download_with_deemix_timeout("q", str(tmp_path), None, "A", "c") == (True, "/music/albums/A/B/c.mp3", False)


class TestLooseTracks:
    def _merger(self, tmp_path, monkeypatch, tags, dry_run=False):
        def read(path):
            base = {"title": "", "album": "", "albumartist": "", "track": None, "disc": None,
                    "year": None, "albumid": "", "releasegroupid": ""}
            base.update(tags.get(Path(path).name, {}))
            return base
        monkeypatch.setattr(aam, "_read_track_tags", read)
        return AlbumArtistMerger(album_dir=str(tmp_path / "albums"), db_path=str(tmp_path / "m.db"),
                                 dry_run=dry_run)

    def test_loose_files_are_filed_into_existing_folders(self, tmp_path, monkeypatch):
        albums = tmp_path / "albums"
        (albums / "ORANGE RANGE" / "＊~アスタリスク~").mkdir(parents=True)
        (albums / "Shiro Sagisu" / "TV Animation BLEACH Original Soundtrack 1").mkdir(parents=True)
        (albums / "Orange Range - _Asterisk.mp3").write_bytes(b"x")
        (albums / "Shiro Sagisu - never meant to belong.mp3").write_bytes(b"x")
        (albums / "Taylor Swift - Babylon.mp3").write_bytes(b"x")
        merger = self._merger(tmp_path, monkeypatch, {
            "Orange Range - _Asterisk.mp3": {"albumartist": "Orange Range", "album": "* Asterisk", "title": "*Asterisk"},
            "Shiro Sagisu - never meant to belong.mp3": {"albumartist": "Shiro Sagisu", "title": "never meant to belong",
                                                        "album": "TV Animation BLEACH Original Soundtrack 1"},
            "Taylor Swift - Babylon.mp3": {"albumartist": "Taylor Swift", "album": "X", "title": "Babylon"},
        })
        report = aam.MergeReport()
        merger._pass_file_loose_tracks(report)
        assert (albums / "ORANGE RANGE" / "_ Asterisk").is_dir()          # joins the existing artist folder ('*' is sanitized)
        assert not (albums / "Orange Range").exists()
        assert (albums / "Shiro Sagisu" / "TV Animation BLEACH Original Soundtrack 1" / "never meant to belong.mp3").exists()
        assert (albums / "Taylor Swift" / "X" / "Babylon.mp3").exists()
        assert report.loose_filed == 3
        assert not [p for p in albums.iterdir() if p.is_file()]

    def test_a_bracketed_release_is_not_filed_into_the_plain_album(self, tmp_path, monkeypatch):
        albums = tmp_path / "albums"
        (albums / "Rie fu" / "Life Is Like A Boat").mkdir(parents=True)
        (albums / "Rie fu - Boat (Classics).mp3").write_bytes(b"x")
        merger = self._merger(tmp_path, monkeypatch, {"Rie fu - Boat (Classics).mp3": {
            "albumartist": "Rie fu", "album": "Life is Like a Boat (Classics Tokyo Sessions)",
            "title": "Life is Like a Boat (Classics Tokyo Sessions)"}})
        merger._pass_file_loose_tracks(aam.MergeReport())
        assert (albums / "Rie fu" / "Life is Like a Boat (Classics Tokyo Sessions)").is_dir()
        assert list((albums / "Rie fu" / "Life Is Like A Boat").iterdir()) == []

    def test_untagged_loose_file_and_dry_run_stay_put(self, tmp_path, monkeypatch):
        albums = tmp_path / "albums"
        albums.mkdir()
        (albums / "mystery.mp3").write_bytes(b"x")
        (albums / "Rie fu - Boat.mp3").write_bytes(b"x")
        merger = self._merger(tmp_path, monkeypatch, {"Rie fu - Boat.mp3": {"albumartist": "Rie fu", "title": "Boat"}},
                              dry_run=True)
        report = aam.MergeReport()
        merger._pass_file_loose_tracks(report)
        assert (albums / "mystery.mp3").exists() and (albums / "Rie fu - Boat.mp3").exists()
        assert report.loose_filed == 1                                        # counted, not moved
