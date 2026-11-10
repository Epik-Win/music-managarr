"""Names MusicAI writes (tags, folders) never carry a cut-off bracket.

"RISE (ft. The Glitch Mob, Mako, and The Word Alive) | Worlds 2018" must not
be split at its first comma into an artist folder "RISE (ft. The Glitch Mob".
"""
from types import SimpleNamespace

import fix_metadata
import metadata_repair
import song_dedup
from downloader import Downloader
from naming import balanced, has_unbalanced_brackets

CREDIT = "RISE (ft. The Glitch Mob, Mako, and The Word Alive) | Worlds 2018"


def test_a_list_is_not_split_inside_brackets():
    assert Downloader._primary_artist(CREDIT) == CREDIT
    assert Downloader._primary_artist("T.I., Young Dro") == "T.I."
    assert Downloader._primary_artist("Rauf & Faik") == "Rauf"            # the folder takes the first act
    assert metadata_repair.primary_artist("RISE (ft. The Glitch Mob, Mako), Other") == "RISE (ft. The Glitch Mob, Mako)"
    assert metadata_repair.primary_artist("T.I., Young Dro") == "T.I."
    assert song_dedup.primary_artist(CREDIT) != song_dedup.primary_artist("RISE (ft. The Glitch Mob")


def test_a_cut_off_bracket_is_repaired_before_it_is_written():
    assert balanced("RISE (ft. The Glitch Mob") == "RISE"
    assert balanced("Album (Deluxe Edition") == "Album"
    assert balanced("Song) Remix") == "Song Remix"
    assert balanced("Fine (Live) [2019]") == "Fine (Live) [2019]"
    assert fix_metadata._sanitize_filename("RISE (ft. The Glitch Mob") == "RISE"


def test_the_daily_repair_picks_up_malformed_names():
    def track(**names):
        base = dict(title="RISE", parentTitle="RISE", grandparentTitle="The Word Alive", originalTitle="",
                    parentThumb="/library/metadata/7/thumb/1", parentRatingKey=7)
        base.update(names)
        return SimpleNamespace(**base)
    assert metadata_repair.needs_repair(track()) is None
    assert metadata_repair.needs_repair(track(grandparentTitle="RISE (ft. The Glitch Mob")) == metadata_repair.MALFORMED_NAME
    assert metadata_repair.needs_repair(track(originalTitle=CREDIT)) == metadata_repair.MALFORMED_NAME
    assert not has_unbalanced_brackets("Interstellar (Original Motion Picture Soundtrack) [Expanded Edition]")


def test_an_album_named_for_another_version_is_not_taken(monkeypatch):
    # MusicBrainz can answer "RISE (instrumental)" for the vocal RISE.
    import musicbrainzngs
    import requests
    d = Downloader.__new__(Downloader)
    d.app_mod = SimpleNamespace()
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.delenv("LAST_FM_API_KEY", raising=False)
    monkeypatch.setattr(musicbrainzngs, "search_releases", lambda **k: {"release-list": [
        {"id": "x", "title": "RISE (instrumental)", "date": "2018"}]})
    monkeypatch.setattr(musicbrainzngs, "set_useragent", lambda *a, **k: None)
    monkeypatch.setattr(requests, "head", lambda *a, **k: SimpleNamespace(status_code=404))
    meta = d.get_metadata_from_sources("League of Legends", "RISE", "RISE (feat. The Glitch Mob)")
    assert meta["album"] != "RISE (instrumental)"
    meta = d.get_metadata_from_sources("League of Legends", "RISE (instrumental)", "RISE (Instrumental)")
    assert meta["album"] == "RISE (instrumental)"            # asked for: fine
