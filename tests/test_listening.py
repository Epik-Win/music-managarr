"""The listening tracker: how much of each song a listener hears."""
import sqlite3

import listening


def look(offset, key='k1', state='playing', duration=200.0, account=7, player='phone'):
    return {'account': account, 'user': 'ann', 'key': key, 'artist': 'An Artist', 'title': f'Song {key}',
            'album': 'An Album', 'player': player, 'device': 'Plexamp / Phone', 'duration': duration,
            'offset': offset, 'state': state}


def run(tracker, looks, start=1000.0, step=5.0):
    ended = []
    for i, sessions in enumerate(looks):
        ended += tracker.update(sessions, start + i * step)
    return ended


def test_a_song_heard_to_the_end_is_finished():
    t = listening.Tracker()
    ended = run(t, [[look(o)] for o in range(0, 200, 5)] + [[]])
    assert [p.outcome() for p in ended] == ['finished'] and ended[0].fraction() >= 0.97


def test_a_song_left_in_its_first_seconds_is_skipped_and_the_next_one_starts():
    t = listening.Tracker()
    ended = run(t, [[look(0)], [look(5)], [look(10)], [look(0, key='k2')]])
    assert len(ended) == 1 and ended[0].key == 'k1' and ended[0].outcome() == 'skipped'
    assert t.open[(7, 'phone')].key == 'k2'


def test_a_song_left_halfway_is_partial():
    t = listening.Tracker()
    ended = run(t, [[look(o)] for o in range(0, 100, 5)] + [[]])
    assert ended[0].outcome() == 'partial' and 0.45 <= ended[0].fraction() <= 0.5


def test_seeking_ahead_is_not_listening():
    t = listening.Tracker()
    ended = run(t, [[look(0)], [look(5)], [look(190)], [look(195)], []])
    play = ended[0]
    assert play.reached == 195 and play.listened == 10.0
    assert play.outcome() == 'finished'          # it reached the end, but only 10 s were played


def test_paused_time_is_not_listening():
    t = listening.Tracker()
    ended = run(t, [[look(0)], [look(5, state='paused')], [look(5, state='paused')], [look(10)], []])
    assert ended[0].listened == 10.0


def test_a_song_played_again_straight_after_is_a_second_play():
    t = listening.Tracker()
    looks = [[look(o)] for o in range(0, 200, 5)] + [[look(0)], [look(5)], []]
    ended = run(t, looks)
    assert [p.outcome() for p in ended] == ['finished', 'skipped']


def test_two_listeners_are_followed_apart():
    t = listening.Tracker()
    ended = run(t, [[look(0), look(0, key='k9', account=8, player='car')], [look(5)], []])
    assert sorted(p.account for p in ended) == [7, 8]


def test_plays_are_saved_and_read_back_per_listener(tmp_path):
    path = str(tmp_path / 'listening.db')
    t = listening.Tracker()
    ended = run(t, [[look(o)] for o in range(0, 200, 5)] + [[look(0, key='k2')], [look(5, key='k2')], []])
    ended += run(t, [[look(0, account=8)], [look(100, account=8)], []], start=5000)
    assert listening.save(ended, path) == 3
    ann = listening.heard_by([7], path)
    assert set(ann) == {'k1', 'k2'} and ann['k1'].finished == 1 and ann['k2'].skipped == 1
    assert ann['k1'].share >= 0.97 and ann['k1'].artist == 'An Artist'
    assert listening.heard_by([8], path)['k1'].plays == 1
    assert listening.heard_by([], path) == {}
    row = sqlite3.connect(path).execute('SELECT device, hour, weekday FROM listens LIMIT 1').fetchone()
    assert row[0] == 'Plexamp / Phone' and 0 <= row[1] <= 23 and 0 <= row[2] <= 6


def test_plays_whose_account_plex_did_not_give_are_found_by_name(tmp_path):
    path = str(tmp_path / 'listening.db')
    t = listening.Tracker()
    listening.save(run(t, [[look(0, account=0)], [look(5, account=0)], []]), path)
    assert listening.heard_by([7], path, names=['Ann'])['k1'].skipped == 1


def test_sessions_keep_only_songs_of_the_music_library():
    from types import SimpleNamespace as NS
    player = NS(machineIdentifier='m1', product='Plexamp', device='Pixel', title='Phone', state='playing')
    song = NS(type='track', librarySectionID=5, _userId=7, _username='ann', usernames=['ann'], ratingKey=42, title='T',
              originalTitle=None, grandparentTitle='A', parentTitle='Al', player=player, duration=200000,
              viewOffset=30000)
    other = NS(type='track', librarySectionID=9, _userId=None, usernames=['bob'], ratingKey=1, title='X',
               player=player, duration=1000, viewOffset=0)
    movie = NS(type='movie')
    server = NS(sessions=lambda: [song, other, movie])
    out = listening.sessions(server, '5')
    assert out == [{'account': 7, 'user': 'ann', 'key': '42', 'artist': 'A', 'title': 'T', 'album': 'Al',
                    'player': 'm1', 'device': 'Plexamp / Pixel / Phone', 'duration': 200.0, 'offset': 30.0,
                    'state': 'playing'}]
