import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import album_artist_merger as aam
from album_artist_merger import AlbumArtistMerger
from lidarr_catalog import LidarrAlbum, LidarrCatalog, titles_match


def _catalog(tmp_path):
    """Lidarr owns D12 World, with its files on disk."""
    cat = LidarrCatalog("http://lidarr", "key")
    cat.loaded = True
    artist_path = str(tmp_path / "albums" / "D12")
    world = LidarrAlbum(id=7052, artist_id=706, artist_name="D12", artist_path=artist_path,
                        title="D12 World", year=2004,
                        release_group_id="ee032768-1965-3aec-850a-fdcf4bff51fa",
                        release_ids={"bfdc03e9-008b-34f9-90be-fdb3e8eba1fd"},
                        selected_release_id="bfdc03e9-008b-34f9-90be-fdb3e8eba1fd",
                        track_file_dir=str(tmp_path / "albums" / "D12" / "D12 World (2004)"))
    devils = LidarrAlbum(id=7053, artist_id=706, artist_name="D12", artist_path=artist_path,
                         title="Devil’s Night", year=2001, release_group_id="cdd5b9f6", track_file_dir="")
    forever = LidarrAlbum(id=7057, artist_id=706, artist_name="D12", artist_path=artist_path,
                          title="D12 Forever, Volume 1", year=2026, release_group_id="e2139643", track_file_dir="")
    for a in (world, devils, forever):
        cat.albums.append(a)
        cat._by_artist.setdefault("d12", []).append(a)
        for m in {a.release_group_id, *a.release_ids}:
            cat._by_mbid[m] = a
    cat._track_file_paths = set()
    cat.rescan = lambda folders: None
    return cat


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
    return p


def _fake_tags(monkeypatch, table):
    """Tags keyed by file name; writes are recorded instead of touching files."""
    written = {}

    def read(path):
        base = {"title": "", "album": "", "albumartist": "", "track": None, "disc": None,
                "year": None, "albumid": "", "releasegroupid": ""}
        base.update(table.get(Path(path).name, {}))
        return base

    monkeypatch.setattr(aam, "_read_track_tags", read)
    monkeypatch.setattr(aam, "_write_album_tags", lambda path, album: written.__setitem__(Path(path).name, album.title))
    return written


class TestMatching:
    def test_hyphen_and_apostrophe_variants_match_lidarr(self, tmp_path):
        cat = _catalog(tmp_path)
        assert cat.match_album("D12", ["D-12 World"]).id == 7052
        assert cat.match_album("D-12", ["Devils Night (2001)"]).id == 7053

    def test_names_in_other_scripts_keep_their_letters(self, tmp_path):
        # A non-Latin name must not reduce to "", or one Cyrillic artist's
        # album would match another's.
        from lidarr_catalog import compact
        assert compact("Детство") == "детство" and compact("Beyoncé") == "beyonce"
        cat = _catalog(tmp_path)
        best = LidarrAlbum(id=9001, artist_id=901, artist_name="Баста", artist_path=str(tmp_path / "Баста"),
                           title="Best", year=2010, release_group_id="b1", track_file_dir="")
        cat.albums.append(best)
        cat._by_artist.setdefault(compact("Баста"), []).append(best)
        assert cat.match_album("Баста", ["Best"]).id == 9001
        assert cat.match_album("Земфира", ["Best"]) is None

    def test_mbid_alone_is_not_trusted(self, tmp_path):
        # A George Benson folder whose files carry "Let It Be"'s MBID.
        cat = _catalog(tmp_path)
        assert cat.match_album("Someone Else", ["Anything"], ["bfdc03e9-008b-34f9-90be-fdb3e8eba1fd"]) is None
        assert cat.match_album("D12", ["Miscellaneous"], ["bfdc03e9-008b-34f9-90be-fdb3e8eba1fd"]) is None

    def test_mbid_tolerates_edition_noise(self, tmp_path):
        cat = _catalog(tmp_path)
        mbid = ["bfdc03e9-008b-34f9-90be-fdb3e8eba1fd"]
        assert cat.match_album("D12", ["D12 World (Bonus Track Version)"]).id == 7052  # brackets ignored
        assert cat.match_album("D12", ["D12 World Ed"]) is None                        # 0.89 < 0.92
        assert cat.match_album("D12", ["D12 World Ed"], mbid).id == 7052               # MBID lowers bar to 0.80

    def test_folder_name_must_agree_with_tags(self, tmp_path):
        cat = _catalog(tmp_path)
        walker = LidarrAlbum(id=3, artist_id=3, artist_name="Alan Walker", artist_path="/m/Alan Walker",
                             title="Walkerworld", year=2024, release_group_id="z")
        bj = LidarrAlbum(id=4, artist_id=4, artist_name="Bon Jovi", artist_path="/m/Bon Jovi",
                         title="2020", year=2020, release_group_id="w")
        assert cat.folder_agrees("Walkerworld 2.0", "Alan Walker", walker, has_mbid=True) is False
        assert cat.folder_agrees("Dance Her Home", "Cody Johnson", walker, has_mbid=True) is False
        assert cat.folder_agrees("Bon Jovi 2020", "Bon Jovi", bj, has_mbid=False) is True
        assert cat.folder_agrees("D-12 World", "D12", cat.albums[0], has_mbid=False) is True

    def test_lidarr_folder_named_for_another_album_is_not_trusted(self, tmp_path):
        # Lidarr's "Self Portrait" tracks sitting in "Another Self Portrait … Vol. 10".
        cat = _catalog(tmp_path)
        sp = LidarrAlbum(id=5, artist_id=5, artist_name="Bob Dylan", artist_path="/music/albums/Bob Dylan",
                         title="Self Portrait", year=1970, release_group_id="v",
                         track_file_dir="/music/albums/Bob Dylan/Another Self Portrait_ The Bootleg Series, Vol. 10")
        assert cat.album_dir(sp, allowed_roots=["/music/albums"]) == "/music/albums/Bob Dylan/Self Portrait (1970)"

    def test_slash_in_names_does_not_create_folders(self, tmp_path):
        cat = _catalog(tmp_path)
        acdc = LidarrAlbum(id=1, artist_id=1, artist_name="AC/DC", artist_path="/music/albums/AC+DC",
                           title="Back in Black", year=1980, release_group_id="x")
        assert cat.render_track_path(acdc, "Hells Bells", 1, 1, ".flac") == \
            "Back in Black (1980)/AC+DC - Back in Black - 01 - Hells Bells.flac"

    def test_an_artist_rooted_outside_the_library_resolves_to_the_album_root(self, tmp_path):
        # An artist imported from another folder keeps pointing there.
        cat = _catalog(tmp_path)
        cat.root_folders = [str(tmp_path / "albums")]
        adele = LidarrAlbum(id=2, artist_id=2, artist_name="Adele", artist_path=str(tmp_path / "imported" / "Adele"),
                            title="21", year=2011, release_group_id="y", track_file_dir="")
        assert cat.album_dir(adele, allowed_roots=[str(tmp_path / "albums")]) == \
            str(tmp_path / "albums" / "Adele" / "21 (2011)")
        assert cat.album_dir(adele, allowed_roots=[]) is None

    def test_different_numbers_never_match(self):
        assert titles_match("Greatest Hits Vol. 1", "Greatest Hits Vol. 2")[0] is False
        assert titles_match("D12 World", "D12 Forever, Volume 1")[0] is False
        assert titles_match("D12 World", "D-12 World")[0] is True

    def test_unrelated_album_does_not_match(self, tmp_path):
        assert _catalog(tmp_path).match_album("D12", ["Worldwide"]) is None

    def test_track_path_follows_lidarr_naming(self, tmp_path):
        cat = _catalog(tmp_path)
        world = cat.albums[0]
        assert cat.render_track_path(world, "My Band", 6, 1, ".flac") == \
            "D12 World (2004)/D12 - D12 World - 06 - My Band.flac"
        devils = cat.albums[1]
        assert cat.album_dir(devils).endswith("D12/Devil’s Night (2001)")


class TestReconcile:
    def _setup(self, tmp_path, monkeypatch, dry_run=False):
        """Lidarr's "D12 World" folder, and a second copy of the album filed
        under another spelling ("D-12 World")."""
        lidarr_dir = tmp_path / "albums" / "D12" / "D12 World (2004)"
        _touch(lidarr_dir / "D12 - D12 World - 06 - My Band.flac")
        stray = tmp_path / "albums" / "D12" / "D-12 World"
        _touch(stray / "My Band.mp3")
        _touch(stray / "Bonus Cut.mp3")
        written = _fake_tags(monkeypatch, {
            "D12 - D12 World - 06 - My Band.flac": {"title": "My Band", "album": "D12 World", "albumartist": "D12"},
            "My Band.mp3": {"title": "My Band", "album": "D-12 World", "albumartist": "D12", "track": 6},
            "Bonus Cut.mp3": {"title": "Bonus Cut", "album": "D-12 World", "albumartist": "D12", "track": 22},
        })
        merger = AlbumArtistMerger(album_dir=str(tmp_path / "albums"), db_path=str(tmp_path / "m.db"),
                                   lidarr=_catalog(tmp_path),
                                   quarantine_dir=str(tmp_path / "quarantine"), dry_run=dry_run)
        return merger, lidarr_dir, stray, written

    def test_a_copy_of_a_lidarr_track_is_left_for_the_song_pass(self, tmp_path, monkeypatch):
        # The reconcile does not decide duplicates; song_dedup does, with
        # lengths and version labels (covered in test_song_dedup).
        merger, lidarr_dir, stray, written = self._setup(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(merger, "_pass_song_dedup", lambda report: calls.append(report))
        merger.lidarr_reconcile()
        assert (stray / "My Band.mp3").exists() and "My Band.mp3" not in written
        assert (lidarr_dir / "D12 - D12 World - 06 - My Band.flac").exists()
        assert len(calls) == 1

    def test_a_track_lidarr_lacks_moves_into_its_folder_with_its_naming(self, tmp_path, monkeypatch):
        merger, lidarr_dir, stray, written = self._setup(tmp_path, monkeypatch)
        merger.lidarr_reconcile()
        assert (lidarr_dir / "D12 - D12 World - 22 - Bonus Cut.mp3").exists()
        assert not (stray / "Bonus Cut.mp3").exists()

    def test_dry_run_changes_nothing(self, tmp_path, monkeypatch):
        merger, lidarr_dir, stray, written = self._setup(tmp_path, monkeypatch, dry_run=True)
        report = merger.lidarr_reconcile()
        assert (stray / "My Band.mp3").exists() and (stray / "Bonus Cut.mp3").exists()
        assert not written
        assert report.lidarr_moves == 1


class FakePlexAlbum(SimpleNamespace):
    def tracks(self):
        return [SimpleNamespace(media=[SimpleNamespace(parts=[SimpleNamespace(file=f) for f in self.files])])]

    def merge(self, keys):
        self.merged = keys

    def editTitle(self, title):
        self.title = title


class TestPlexMerge:
    def test_lidarr_folder_album_survives_and_absorbs_the_split(self, tmp_path):
        cat = _catalog(tmp_path)
        lidarr_dir = cat.albums[0].track_file_dir
        good = FakePlexAlbum(ratingKey=67896, parentTitle="D12", title="D12 World", leafCount=23, addedAt=None,
                             files=["/srv/media" + lidarr_dir + "/x.flac"])
        split = FakePlexAlbum(ratingKey=67230, parentTitle="D12", title="D-12 World", leafCount=3, addedAt=None,
                              files=["/srv/media/music/albums/D12/D-12 World/x.mp3"])
        other = FakePlexAlbum(ratingKey=1, parentTitle="D12", title="D12 Forever, Volume 1", leafCount=15,
                              addedAt=None, files=[])
        section = SimpleNamespace(albums=lambda: [split, good, other], update=lambda: None)
        plex = SimpleNamespace(library=SimpleNamespace(section=lambda name: section))
        merger = AlbumArtistMerger(plex=plex, db_path=str(tmp_path / "m.db"), lidarr=cat)
        report = merger.plex_merge_split_albums()
        assert good.merged == ["67230"]
        assert not hasattr(split, "merged") and not hasattr(other, "merged")
        assert report.plex_album_merges == 1
