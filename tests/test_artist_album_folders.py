from pathlib import Path

import pytest

import album_artist_merger as aam
import library_state
import song_dedup


def _song(path: Path, data: bytes = b"audio") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class _Lidarr:
    """The few LidarrCatalog calls the folder passes make."""

    def __init__(self, folders, owned=()):
        self.folders, self.owned, self.moved = folders, {str(p) for p in owned}, []

    def load(self):
        return self

    def artist_folders(self):
        return {str(f): i for i, f in enumerate(self.folders)}

    def track_file_paths(self):
        return self.owned

    def move_files_home(self, paths):
        self.moved += sorted(paths)
        return len(self.moved)


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(library_state, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(library_state, "_root", lambda: str(tmp_path / "albums"))
    monkeypatch.setattr(aam.library_owner, "adopt", lambda root=None: 0)
    (tmp_path / "albums").mkdir()
    return tmp_path / "albums"


def _merger(library, lidarr=None):
    return aam.AlbumArtistMerger(album_dir=str(library), db_path=str(library.parent / "m.db"),
                                 lidarr=lidarr, quarantine_dir=str(library.parent / "quarantine"))


class TestArtistKey:
    def test_band_names_are_not_split(self):
        assert aam._artist_folder_key("Earth, Wind & Fire") != aam._artist_folder_key("Earth")
        assert aam._artist_folder_key("Simon & Garfunkel") != aam._artist_folder_key("Simon")
        assert aam._artist_folder_key("Dana and the Wolf") != aam._artist_folder_key("Dana")
        assert aam._artist_folder_key("Daft Punk") == "daftpunk"

    def test_spellings_of_one_artist_share_a_key(self):
        assert aam._artist_folder_key("LINKIN PARK") == aam._artist_folder_key("Linkin Park")
        assert aam._artist_folder_key("Orchestre symphonique de MontrÃ©al") == \
            aam._artist_folder_key("Orchestre symphonique de Montréal")
        assert aam._artist_folder_key("Taylor Swift feat. Ed Sheeran") == aam._artist_folder_key("Taylor Swift")
        assert aam._artist_folder_key("RISE (ft. The Glitch Mob") == aam._artist_folder_key("RISE")


class TestArtistPass:
    def test_lidarrs_folder_wins_and_lidarr_moves_its_own_files(self, library):
        lidarrs = _song(library / "LINKIN PARK" / "Hybrid Theory (2000)" / "In the End.mp3")
        mine = _song(library / "LINKIN PARK" / "Meteora" / "Numb.mp3")
        lidarr = _Lidarr(folders=[library / "Linkin Park"], owned=[lidarrs])
        _merger(library, lidarr)._pass_normalise_artist_dirs(aam.MergeReport())
        assert (library / "Linkin Park" / "Meteora" / "Numb.mp3").exists() and not mine.exists()
        assert lidarrs.exists() and lidarr.moved == [str(lidarrs)]          # left for Lidarr to move

    def test_the_cleanly_encoded_name_with_most_songs_wins(self, library):
        _song(library / "Orchestre symphonique de MontrÃ©al" / "A" / "1.mp3")
        _song(library / "Orchestre symphonique de MontrÃ©al" / "A" / "2.mp3")
        _song(library / "Orchestre symphonique de Montréal" / "B" / "3.mp3")
        _merger(library)._pass_normalise_artist_dirs(aam.MergeReport())
        assert sorted(p.name for p in library.iterdir()) == ["Orchestre symphonique de Montréal"]

    def test_two_lidarr_artists_of_one_name_stay_apart(self, library):
        _song(library / "Passenger" / "All the Little Lights" / "Let Her Go.mp3")
        _song(library / "PASSENGER" / "Passenger" / "Hollywood.mp3")
        lidarr = _Lidarr(folders=[library / "Passenger", library / "PASSENGER"])
        _merger(library, lidarr)._pass_normalise_artist_dirs(aam.MergeReport())
        assert sorted(p.name for p in library.iterdir()) == ["PASSENGER", "Passenger"]

    def test_earth_wind_and_fire_is_left_alone(self, library):
        _song(library / "Earth, Wind & Fire" / "Gratitude" / "Shining Star.mp3")
        _song(library / "Earth" / "Something" / "Else.mp3")
        _merger(library)._pass_normalise_artist_dirs(aam.MergeReport())
        assert sorted(p.name for p in library.iterdir()) == ["Earth", "Earth, Wind & Fire"]


class TestFolderMerge:
    def test_a_different_song_of_the_same_name_stays_where_it_is(self, library, monkeypatch):
        monkeypatch.setattr(song_dedup, "same_audio", lambda a, b: False)
        src = _song(library / "A" / "Album" / "Intro.mp3", b"one")
        dst = _song(library / "B" / "Album" / "Intro.mp3", b"two")
        _merger(library)._merge_dir_into(library / "A", library / "B", aam.MergeReport(), "t", "r", owned=set())
        assert src.read_bytes() == b"one" and dst.read_bytes() == b"two"

    def test_the_same_recording_goes_to_the_quarantine_not_the_bin(self, library, monkeypatch):
        monkeypatch.setattr(song_dedup, "same_audio", lambda a, b: True)
        src = _song(library / "A" / "Album" / "Song.mp3", b"copy")
        dst = _song(library / "B" / "Album" / "Song.mp3", b"keep")
        monkeypatch.setattr(aam, "_file_quality_rank", lambda p: 1)          # equal quality: dst stays
        _merger(library)._merge_dir_into(library / "A", library / "B", aam.MergeReport(), "t", "r", owned=set())
        assert dst.read_bytes() == b"keep" and not src.exists()
        assert [p.read_bytes() for p in (library.parent / "quarantine").rglob("Song.mp3")] == [b"copy"]

    def test_a_song_moves_with_its_lyrics(self, library):
        src = _song(library / "A" / "Album" / "Song.mp3")
        (src.parent / "Song.lrc").write_text("[00:01] la")
        _merger(library)._merge_dir_into(library / "A", library / "B", aam.MergeReport(), "t", "r", owned=set())
        assert (library / "B" / "Album" / "Song.lrc").exists() and not (library / "A").exists()


class TestAlbumKey:
    @pytest.mark.parametrize("a, b", [("The Eminem Show", "The Eminem Show (2002)"),
                                      ("Been By Now", "Been By Now - Single"),
                                      ("Hollywood's Bleeding", "Hollywood’s Bleeding (2019)"),
                                      ("Thriller", "Thriller (25th Anniversary Edition)"),
                                      ("F*CK LOVE 3+: OVER YOU", "F_CK LOVE 3+_ OVER YOU")])
    def test_editions_of_one_album(self, a, b):
        assert aam._album_folder_key(a) == aam._album_folder_key(b)

    @pytest.mark.parametrize("a, b", [("The Blueprint", "The Blueprint 3"),
                                      ("Bleach, Vol. 1", "Bleach, Vol. 2"),
                                      ("A Single Woman", "See-Line Woman (Mochakk Mix) - Single"),
                                      ("Life Is Like A Boat", "Life is Like a Boat (Classics Tokyo Sessions)")])
    def test_different_albums(self, a, b):
        assert aam._album_folder_key(a) != aam._album_folder_key(b)
