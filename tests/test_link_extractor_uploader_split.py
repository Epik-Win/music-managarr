import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import link_extractor as le


def _fake_ytdlp(payload):
    return lambda cmd, timeout=30: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')


def test_uploader_matching_right_half_makes_it_the_artist(monkeypatch):
    # "RISE (ft. …) | Worlds 2018 - League of Legends" from the official
    # League of Legends channel: the half matching the uploader is the artist
    # (SongNameSplit rejects the title as non-standard, so the plain
    # Artist - Title guess would read it backwards).
    monkeypatch.setattr(le, 'run_ytdlp', _fake_ytdlp({
        'title': 'RISE (ft. The Glitch Mob, Mako, and The Word Alive) | Worlds 2018 - League of Legends',
        'uploader': 'League of Legends',
        'categories': ['Music'],
        'duration': 200,
    }))
    result = le.extract_youtube_metadata('https://www.youtube.com/watch?v=vid00000010')
    assert result['artist'] == 'League of Legends'
    assert result['title'].startswith('RISE')
    assert 'Worlds 2018' not in result['title']


def test_names_roughly_match():
    assert le._names_roughly_match('League of Legends', 'League Of Legends')
    assert not le._names_roughly_match('League of Legends', 'RISE (ft. The Glitch Mob)')
