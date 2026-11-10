"""Fix Metadata repairs text and artwork, not only names:

* an ACRCloud reply is `text/plain` without a charset, so `requests` would
  decode its UTF-8 as Latin-1; the reply is read as UTF-8, and an Arabic title
  ("ما أجمل الحياة") is written as such, not as mojibake;
* a track whose only artwork is the cover.jpg next to it keeps it when moved,
  and a flagged track with the right names but no art gets its art fixed.
"""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import artwork
import fix_metadata
import metadata_repair as mr
from text_repair import fix_mojibake, looks_garbled

ARABIC_TITLE = "ما أجمل الحياة"
ARABIC_ALBUM = "بالعربي 1"
GARBLED_TITLE = ARABIC_TITLE.encode("utf-8").decode("latin-1")
GARBLED_ALBUM = ARABIC_ALBUM.encode("utf-8").decode("latin-1")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096


@pytest.fixture
def mp3(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = tmp_path / "in" / "song.mp3"
    path.parent.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-q:a", "9", str(path)], check=True)
    return path


class TestText:
    def test_real_mojibake_is_repaired(self):
        assert GARBLED_TITLE == "Ù\x85Ø§ Ø£Ø¬Ù\x85Ù\x84 Ø§Ù\x84Ø\xadÙ\x8aØ§Ø©"   # as Plex showed it
        assert fix_mojibake(GARBLED_TITLE) == ARABIC_TITLE
        assert fix_mojibake(GARBLED_ALBUM) == ARABIC_ALBUM
        assert fix_mojibake("Orchestre symphonique de MontrÃ©al") == "Orchestre symphonique de Montréal"

    def test_clean_text_is_left_alone(self):
        for clean in (ARABIC_TITLE, "Montréal", "Beyoncé", "Mötley Crüe", "RISE (feat. The Glitch Mob)", "À la claire"):
            assert not looks_garbled(clean)

    def test_acrcloud_reply_is_decoded_as_utf8(self):
        import json
        from audio_id import _json_utf8
        body = json.dumps({"title": ARABIC_TITLE}, ensure_ascii=False).encode("utf-8")
        # What requests does with `Content-Type: text/plain` and no charset:
        resp = SimpleNamespace(content=body, encoding="ISO-8859-1")
        assert json.loads(body.decode("ISO-8859-1"))["title"] == GARBLED_TITLE   # what a plain decode gives
        assert _json_utf8(resp)["title"] == ARABIC_TITLE


class TestArtworkMatching:
    def test_rise_matches_its_own_release_not_the_remix(self):
        mine = ("The Word Alive", "RISE (feat. The Glitch Mob)", "RISE")
        # Deezer credits the song's contributors; an album that only repeats
        # the title is no evidence on its own.
        assert artwork.result_matches(*mine, ["League Of Legends", "The Word Alive"], "RISE", "RISE")
        assert not artwork.result_matches(*mine, ["League Of Legends"], "RISE (Remix)", "RISE (Remix)")
        assert not artwork.result_matches(*mine, ["ZZang KARAOKE"],
                                          "RISE (Feat. The Glitch Mob) (Instrumental Karaoke Version)", "Vol.133")

    def test_arabic_title_matches_its_artist_not_a_namesake(self):
        mine = ("Rachid Aseyakhe", ARABIC_TITLE, ARABIC_ALBUM)
        assert artwork.result_matches(*mine, ["Rachid Aseyakhe"], ARABIC_TITLE, ARABIC_ALBUM)
        assert not artwork.result_matches(*mine, ["Nour Daher"], ARABIC_TITLE, ARABIC_TITLE)


class TestArtworkStorage:
    def test_cover_travels_with_a_moved_file(self, mp3, tmp_path):
        (mp3.parent / "cover.jpg").write_bytes(JPEG)
        base = tmp_path / "albums"
        new = fix_metadata._reorganize_file(str(mp3), {"artist": "League of Legends", "album": "RISE", "title": "RISE"},
                                            base_dir=str(base))
        assert Path(new).parent == base / "League of Legends" / "RISE"
        assert (base / "League of Legends" / "RISE" / "cover.jpg").exists()
        assert not mp3.parent.exists()           # emptied old folder is gone

    def test_folder_cover_gets_embedded(self, mp3):
        (mp3.parent / "cover.jpg").write_bytes(JPEG)
        assert not artwork.has_embedded_art(str(mp3))
        assert artwork.ensure_artwork(str(mp3), {}) == "embedded folder cover"
        assert artwork.has_embedded_art(str(mp3))

    def test_missing_art_is_fetched_saved_and_embedded(self, mp3, monkeypatch):
        monkeypatch.setattr(artwork, "find_artwork", lambda *a, **k: (JPEG, "Deezer"))
        assert artwork.ensure_artwork(str(mp3), {"artist": "A", "title": "T"}) == "fetched from Deezer"
        assert (mp3.parent / "cover.jpg").read_bytes() == JPEG
        assert artwork.has_embedded_art(str(mp3))

    def test_dry_run_writes_nothing(self, mp3, monkeypatch):
        monkeypatch.setattr(artwork, "find_artwork", lambda *a, **k: (JPEG, "Deezer"))
        assert artwork.ensure_artwork(str(mp3), {"artist": "A", "title": "T"}, dry_run=True) == "fetched from Deezer"
        assert not (mp3.parent / "cover.jpg").exists() and not artwork.has_embedded_art(str(mp3))


class _Playlist:
    title = "Fix Metadata"

    def __init__(self, item):
        self._items = [item]

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        self._items = []


def _run(monkeypatch, flagged, fingerprint, art_result="present"):
    import audio_id
    playlist = _Playlist(flagged)
    user_plex = SimpleNamespace(playlists=lambda: [playlist], library=SimpleNamespace(section=lambda n: object()))
    written, art_calls = [], []
    monkeypatch.setattr(fix_metadata, "_file_path", lambda t: "/music/x.mp3")
    monkeypatch.setattr(fix_metadata, "_write_tags", lambda fp, meta: written.append(meta) or True)
    monkeypatch.setattr(fix_metadata, "_reorganize_file", lambda fp, meta, **kw: fp)
    monkeypatch.setattr(fix_metadata, "_refresh_in_plex", lambda t: None)
    monkeypatch.setattr(artwork, "ensure_artwork", lambda fp, meta, hints=(), **k: art_calls.append(meta) or art_result)
    monkeypatch.setattr(audio_id, "identify_track_by_audio_with_alternatives", lambda fp: fingerprint)
    stats = fix_metadata.run_fix_metadata(SimpleNamespace(switchUser=lambda n: user_plex), "bob", "Music")
    return stats, playlist, written, art_calls


class TestFlaggedTracks:
    def test_arabic_song_gets_its_real_title_not_mojibake(self, monkeypatch):
        flagged = SimpleNamespace(ratingKey="69196", title=GARBLED_TITLE, parentTitle=GARBLED_ALBUM,
                                  grandparentTitle="RaChid ASEYAKHE I", originalTitle="")
        acrcloud = {"artist": "Rachid Aseyakhe", "title": GARBLED_TITLE, "album": GARBLED_ALBUM}   # as it arrived
        shazam = {"artist": "Rachid Aseyakhe", "title": ARABIC_TITLE, "album": ""}
        stats, playlist, written, _ = _run(monkeypatch, flagged, (acrcloud, shazam))
        assert written == [{"artist": "Rachid Aseyakhe", "title": ARABIC_TITLE, "album": ARABIC_ALBUM}]
        assert stats["fixed"] == 1 and playlist.items() == []

    def test_garbled_tags_are_repaired_even_without_a_fingerprint(self, monkeypatch):
        flagged = SimpleNamespace(ratingKey="1", title=GARBLED_TITLE, parentTitle=GARBLED_ALBUM,
                                  grandparentTitle="Rachid Aseyakhe", originalTitle="")
        stats, _, written, _ = _run(monkeypatch, flagged, (None, None))
        assert written == [{"artist": "Rachid Aseyakhe", "title": ARABIC_TITLE, "album": ARABIC_ALBUM}]
        assert stats["unresolved"] == []

    def test_right_names_but_no_art_is_fixed_not_sent_to_admin(self, monkeypatch):
        flagged = SimpleNamespace(ratingKey="69198", title="RISE (feat. The Glitch Mob)", parentTitle="RISE",
                                  grandparentTitle="The Word Alive", originalTitle="")
        same = {"artist": "The Word Alive", "title": "RISE (feat. The Glitch Mob)", "album": "RISE"}
        stats, playlist, written, art_calls = _run(monkeypatch, flagged, (same, None), art_result="fetched from Deezer")
        assert written == [] and stats["fixed"] == 1 and stats["unresolved"] == []
        assert playlist.items() == [] and art_calls

    def test_right_names_and_art_present_still_goes_to_admin(self, monkeypatch):
        flagged = SimpleNamespace(ratingKey="7", title="T", parentTitle="Al", grandparentTitle="Ar", originalTitle="")
        stats, _, _, _ = _run(monkeypatch, flagged, ({"artist": "Ar", "title": "T", "album": "Al"}, None))
        assert stats["unresolved"] and stats["fixed"] == 0

    def test_every_fix_checks_artwork(self, monkeypatch):
        flagged = SimpleNamespace(ratingKey="8", title="Wrong", parentTitle="Wrong", grandparentTitle="Wrong",
                                  originalTitle="")
        answer = {"artist": "A", "title": "Title", "album": "Al"}
        _, _, written, art_calls = _run(monkeypatch, flagged, (answer, dict(answer)))
        assert written and art_calls == [written[0]]


class TestPlexRefresh:
    def test_track_album_and_artist_are_all_refreshed(self):
        refreshed = []
        node = lambda name: SimpleNamespace(refresh=lambda: refreshed.append(name))
        track = SimpleNamespace(refresh=lambda: refreshed.append("track"),
                                album=lambda: node("album"), artist=lambda: node("artist"))
        fix_metadata._refresh_in_plex(track)
        assert refreshed == ["track", "album", "artist"]


class TestLibraryScanDetection:
    def _track(self, **kw):
        base = dict(ratingKey="1", title="T", parentTitle="Al", grandparentTitle="Ar", originalTitle="",
                    parentThumb="/thumb")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_garbled_names_and_missing_art_are_detected(self):
        assert mr.needs_repair(self._track(title=GARBLED_TITLE)) == mr.GARBLED
        assert mr.needs_repair(self._track(grandparentTitle="Orchestre symphonique de MontrÃ©al")) == mr.GARBLED
        assert mr.needs_repair(self._track(parentThumb=None)) == mr.NO_ARTWORK
        assert mr.needs_repair(self._track()) is None

    def test_garbled_track_is_repaired_without_a_lookup(self, monkeypatch):
        track = self._track(title=GARBLED_TITLE, parentTitle=GARBLED_ALBUM, grandparentTitle="Rachid Aseyakhe")
        monkeypatch.setattr(mr, "find_candidates", lambda *a: [(track, mr.GARBLED)])
        monkeypatch.setattr(fix_metadata, "_file_path", lambda t: "/music/x.mp3")
        monkeypatch.setattr(mr, "known_meta", lambda t, fp: {"artist": "Rachid Aseyakhe", "title": GARBLED_TITLE,
                                                               "album": GARBLED_ALBUM})
        monkeypatch.setattr(mr, "resolve", lambda *a: (_ for _ in ()).throw(AssertionError("no lookup needed")))
        applied = []
        monkeypatch.setattr(mr, "apply_fix", lambda t, fp, meta: applied.append(meta) or True)
        stats, unresolved = mr.repair_library(None, set(), 10)
        assert applied == [{"artist": "Rachid Aseyakhe", "title": ARABIC_TITLE, "album": ARABIC_ALBUM}]
        assert stats["fixed"] == 1 and unresolved == []

    def test_missing_art_found_or_escalated(self, monkeypatch):
        track = self._track(parentThumb=None)
        monkeypatch.setattr(mr, "find_candidates", lambda *a: [(track, mr.NO_ARTWORK)])
        monkeypatch.setattr(fix_metadata, "_file_path", lambda t: "/music/x.mp3")
        monkeypatch.setattr(mr, "known_meta", lambda t, fp: {"artist": "Ar", "title": "T", "album": "Al"})
        monkeypatch.setattr(mr, "_refresh_in_plex", lambda t: None)
        monkeypatch.setattr(mr, "ensure_artwork", lambda fp, meta, dry_run=False: "fetched from Deezer")
        assert mr.repair_library(None, set(), 10)[0]["fixed"] == 1
        monkeypatch.setattr(mr, "ensure_artwork", lambda fp, meta, dry_run=False: "not found")
        stats, unresolved = mr.repair_library(None, set(), 10)
        assert stats["fixed"] == 0 and "none found online" in unresolved[0]["reason"]
