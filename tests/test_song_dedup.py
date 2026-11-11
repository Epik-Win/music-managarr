import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import one_star_watcher as w
import song_dedup as sd

YES = lambda a, b: True


def _song(path, artist, title, duration=200.0, lidarr=False, album_root=True, quality=5, bitrate=320000,
          mtime=1.0, track=None):
    f = sd.SongFile(path=str(path), artist=artist, title=title, duration=duration, track=track,
                    bitrate=bitrate, quality_rank=quality, in_album_root=album_root,
                    lidarr_owned=lidarr, mtime=mtime)
    f.key = sd.song_key(artist, title)
    return f


class TestIdentity:
    def test_versions_are_different_songs(self):
        assert sd.song_key("Queen", "Bohemian Rhapsody") != sd.song_key("Queen", "Bohemian Rhapsody (Live)")
        assert sd.song_key("Adele", "Hello") != sd.song_key("Adele", "Hello - Acoustic")
        assert sd.song_key("X", "Song") != sd.song_key("X", "Song (Sped Up)")

    def test_noise_does_not_make_a_different_song(self):
        base = sd.song_key("VEGEDREAM", "RAMENEZ LA COUPE A LA MAISON")
        assert sd.song_key("Vegedream", "Ramenez la coupe à la maison") == base
        assert sd.song_key("Vegedream feat. Someone", "Ramenez la coupe à la maison (feat. Someone)") == base
        assert sd.song_key("D12", "My Band - Remastered 2004") == sd.song_key("D-12", "My Band")

    def test_different_lengths_are_different_recordings(self):
        a = _song("/m/a/1.mp3", "A", "Song", 200)
        assert sd.same_recording(a, _song("/m/b/1.mp3", "A", "Song", 202))
        assert not sd.same_recording(a, _song("/m/b/1.mp3", "A", "Song", 240))   # radio edit vs album
        assert not sd.same_recording(a, _song("/m/b/1.mp3", "A", "Song", 0))     # unknown length: never guess

    def test_a_rip_a_few_seconds_longer_is_compared_by_sound(self):
        # A YouTube rip of "Babylon" (219.4 s) and the release (222.6 s).
        assert sd.same_recording(_song("/m/a/1.mp3", "Taylor Swift", "Babylon", 219.4),
                                 _song("/m/b/1.mp3", "Taylor Swift", "Babylon", 222.6))
        assert not sd.same_recording(_song("/m/a/1.mp3", "Taylor Swift", "Patient Zero", 225.9),
                                     _song("/m/b/1.mp3", "Taylor Swift", "Patient Zero", 312.3))

    def test_titles_in_other_scripts_are_compared(self):
        # "детство.mp3" and "детство (2).mp3", byte-identical: the title must
        # not reduce to "", or they are never compared.
        a = _song("/m/Rauf/ja ljublju tebja/детство.mp3", "Rauf", "детство", 188.4)
        b = _song("/m/Rauf/ja ljublju tebja/детство (2).mp3", "Rauf", "детство", 188.4)
        assert a.key[1] == "детство" and sd.same_recording(a, b)
        assert sd.song_key("Rauf", "детство") != sd.song_key("Rauf", "Колыбельная")

    def test_the_copy_without_a_copy_number_is_kept(self):
        # Byte-identical copies, "детство (2).mp3" downloaded first.
        first = _song("/m/Rauf/A/детство (2).mp3", "Rauf", "детство", 188.4, mtime=1.0)
        second = _song("/m/Rauf/A/детство.mp3", "Rauf", "детство", 188.4, mtime=2.0)
        assert sorted([first, second], key=sd.keeper_rank)[0] is second

    def test_same_name_numbered_tracks_in_one_folder_are_different(self):
        a = _song("/m/Album/03 Interlude.mp3", "A", "Interlude", 60, track=3)
        b = _song("/m/Album/07 Interlude.mp3", "A", "Interlude", 61, track=7)
        assert not sd.same_recording(a, b)


class TestAudioConfirmation:
    def test_mixed_is_a_version_label(self):
        # "Get By (Mixed)" from a DJ mix vs the album's "Get By".
        assert sd.song_key("Talib Kweli", "Get By (Mixed)") != sd.song_key("Talib Kweli", "Get By")

    def test_similarity_tolerates_a_small_offset_and_rejects_other_audio(self):
        import random
        rnd = random.Random(1)
        a = [rnd.getrandbits(32) for _ in range(900)]
        shifted = [rnd.getrandbits(32) for _ in range(5)] + a          # starts 5 frames later
        other = [rnd.getrandbits(32) for _ in range(900)]
        assert sd.fingerprint_similarity(a, a) == 1.0
        assert sd.fingerprint_similarity(shifted, a) == 1.0
        assert sd.fingerprint_similarity(a, other) < 0.6

    def test_nothing_is_removed_without_audio_confirmation(self):
        a = _song("/m/albums/A/X/s.flac", "A", "S", quality=6)
        b = _song("/m/albums/A/Y/s.mp3", "A", "S", quality=5)
        assert sd.plan([a, b], verify=lambda x, y: False) == []


class TestPlan:
    def test_album_copy_beats_single_and_lidarr_beats_quality(self):
        single = _song("/m/other/V/R/R.mp3", "VEGEDREAM", "RAMENEZ", album_root=False, quality=6)
        album = _song("/m/albums/V/R/R.mp3", "VEGEDREAM", "RAMENEZ", quality=4)
        lidarr = _song("/m/albums/V/Lidarr/R.mp3", "VEGEDREAM", "RAMENEZ", lidarr=True, quality=3)
        [(keeper, doomed)] = sd.plan([single, album, lidarr], verify=YES)
        assert keeper is lidarr and {f.path for f in doomed} == {single.path, album.path}

    def test_better_quality_wins_between_equals(self):
        mp3 = _song("/m/albums/A/X/s.mp3", "A", "S", quality=5)
        flac = _song("/m/albums/A/Y/s.flac", "A", "S", quality=6)
        [(keeper, doomed)] = sd.plan([mp3, flac], verify=YES)
        assert keeper is flac and doomed == [mp3]

    def test_lidarr_files_are_never_removed(self):
        a = _song("/m/albums/E/Album/s.flac", "Eminem", "Lose Yourself", lidarr=True)
        b = _song("/m/albums/E/Curtain Call/s.flac", "Eminem", "Lose Yourself", lidarr=True)
        assert sd.plan([a, b], verify=YES) == []


class FakePlexTrack(SimpleNamespace):
    def rate(self, value):
        self.userRating = value
        self.rated = True

    def markUnplayed(self):
        self.viewCount = 0
        self.unplayed = True


class TestPlexPurge:
    def test_removed_copy_with_its_own_plex_song_is_purged(self):
        dup = FakePlexTrack(guid="plex://track/single", userRating=2.0, viewCount=5)
        keeper = FakePlexTrack(guid="plex://track/album", userRating=8.0, viewCount=9)
        assert sd.purge_plex_memory(dup, keeper) == ["rating", "play history"]
        assert dup.userRating is None and dup.viewCount == 0
        assert keeper.userRating == 8.0 and keeper.viewCount == 9

    def test_shared_plex_song_keeps_its_data(self):
        dup = FakePlexTrack(guid="plex://track/same", userRating=8.0, viewCount=5)
        keeper = FakePlexTrack(guid="plex://track/same", userRating=8.0, viewCount=5)
        assert sd.purge_plex_memory(dup, keeper) == []
        assert not hasattr(dup, "rated")


class TestDedupRun:
    def test_duplicate_goes_to_quarantine_and_plex_is_refreshed(self, tmp_path, monkeypatch):
        album = tmp_path / "albums" / "V" / "Album"; album.mkdir(parents=True)
        single = tmp_path / "other" / "V" / "Single"; single.mkdir(parents=True)
        keep = album / "R.flac"; keep.write_bytes(b"x")
        dup = single / "R.mp3"; dup.write_bytes(b"x")
        (single / "cover.jpg").write_bytes(b"x")
        files = [_song(keep, "V", "R", quality=6), _song(dup, "V", "R", album_root=False, quality=5)]
        monkeypatch.setattr(sd, "scan", lambda *a, **k: files)
        monkeypatch.setattr(sd.time, "sleep", lambda s: None)
        section = SimpleNamespace(searchTracks=lambda **k: [], updated=0, emptied=0)
        section.update = lambda: setattr(section, "updated", section.updated + 1)
        section.emptyTrash = lambda: setattr(section, "emptied", section.emptied + 1)

        result = sd.dedup([str(tmp_path / "albums"), str(tmp_path / "other")], [str(tmp_path / "albums")],
                          plex_section=section, quarantine_dir=str(tmp_path / "q"), verify=YES)

        assert keep.exists() and not dup.exists()
        assert not single.exists()                                  # emptied folder went too
        assert list((tmp_path / "q").rglob("R.mp3")) and list((tmp_path / "q").rglob("cover.jpg"))
        assert result.kept_for(str(dup)) == str(keep)
        assert section.updated == 1 and section.emptied == 1

    def test_dry_run_touches_nothing(self, tmp_path, monkeypatch):
        a = tmp_path / "a.flac"; a.write_bytes(b"x")
        b = tmp_path / "b.mp3"; b.write_bytes(b"x")
        monkeypatch.setattr(sd, "scan", lambda *x, **k: [_song(a, "V", "R", quality=6), _song(b, "V", "R", quality=5)])
        result = sd.dedup([str(tmp_path)], [str(tmp_path)], dry_run=True, quarantine_dir=str(tmp_path / "q"), verify=YES)
        assert a.exists() and b.exists() and len(result.removed) == 1


class TestWatcher:
    def _track(self, path, rated, added):
        t = FakePlexTrack(ratingKey="9", grandparentTitle="VEGEDREAM", originalTitle="",
                          title="RAMENEZ LA COUPE A LA MAISON", userRating=2.0,
                          lastRatedAt=rated, addedAt=added,
                          media=[SimpleNamespace(parts=[SimpleNamespace(file=str(path))])])
        return t

    # Deletion was switched on before any of these ratings were given.
    SWITCHED_ON = dt.datetime(2026, 9, 1).timestamp()

    def _section(self, tracks):
        return SimpleNamespace(searchTracks=lambda filters=None: tracks, update=lambda: None, emptyTrash=lambda: None)

    def test_rating_restored_by_plex_is_cleared_not_acted_on(self, tmp_path, monkeypatch):
        # A rating older than the copy it sits on was restored by Plex.
        f = tmp_path / "R.mp3"; f.write_bytes(b"x")
        t = self._track(f, dt.datetime(2026, 9, 15, 17, 20), dt.datetime(2026, 9, 29, 16, 38))
        handled = set()
        assert w.process_once(self._section([t]), "admin", [str(tmp_path / "db.db")], handled, self.SWITCHED_ON) == 0
        assert f.exists() and t.userRating is None and handled == {"9"}

    def test_removing_one_version_keeps_the_other_copy(self, tmp_path, monkeypatch):
        f = tmp_path / "R.mp3"; f.write_bytes(b"x")
        t = self._track(f, dt.datetime(2026, 9, 29, 17, 0), dt.datetime(2026, 9, 29, 16, 38))
        monkeypatch.setattr(w, "remaining_copy", lambda files: "/music/albums/V/R/R.flac")
        logged = []
        monkeypatch.setattr(w, "record_deletion", lambda *a, **k: logged.append(a))
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        assert w.process_once(self._section([t]), "admin", [], set(), self.SWITCHED_ON) == 1
        assert not f.exists() and len(logged) == 1
