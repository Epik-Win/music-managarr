import sqlite3
import time
from collections import Counter
from types import SimpleNamespace

import pytest

import taste

NOW = 1_800_000_000.0
DAY = 86400


def song(key, artist, title, plays=0, skips=0, rating=0, days_ago=None, added_days_ago=400, ref=None):
    return taste.Song(key=str(key), artist=artist, title=title, album_artist=artist, artist_ref=ref or f"a-{artist}",
                      plays=plays, skips=skips, rating=rating,
                      last_played=NOW - days_ago * DAY if days_ago is not None else 0.0,
                      added=NOW - added_days_ago * DAY)


def plays(key, artist, title, *days_ago):
    return [(str(key), NOW - d * DAY, artist, title) for d in days_ago]


class TestAffinity:
    def test_recent_plays_make_a_song_loved_and_old_ones_fade(self):
        assert taste.affinity(5 * taste.decay(3), plays=5) >= taste.LOVED
        assert taste.affinity(5 * taste.decay(700), plays=5) < 0.1

    def test_a_rating_speaks_louder_than_plays(self):
        assert taste.affinity(0, rating=8) >= taste.LOVED                  # 4★, never played
        assert taste.affinity(10, plays=10, rating=2) == -1.0              # 1★
        assert taste.affinity(10, plays=10, rating=4) <= -0.6              # 2★

    def test_skips_count_against_an_unrated_song(self):
        assert taste.affinity(2, plays=2, skips=8) < taste.affinity(2, plays=2) - 0.5
        assert taste.affinity(2, plays=2, skips=8, rating=10) >= taste.LOVED   # 5★ stays loved


class TestProfile:
    def test_plays_count_from_the_history_even_after_plex_re_added_the_song(self):
        songs = [song(2, 'Kendrick Lamar', 'HUMBLE.')]
        p = taste.build('u', songs, plays(1, 'Kendrick Lamar', 'HUMBLE.', 1, 2, 3, 4, 5), now=NOW)
        assert p.songs['2'] >= taste.LOVED and '2' in p.heard

    def test_plays_older_than_the_history_still_count_a_little(self):
        p = taste.build('u', [song(1, 'A', 'x', plays=40, days_ago=500)], now=NOW)
        assert 0 < p.songs['1'] < taste.LOVED

    def test_a_few_disliked_songs_do_not_undo_a_favourite_artist(self):
        songs = [song(i, 'Kendrick Lamar', f"s{i}", rating=10) for i in range(10)]
        songs += [song(100 + i, 'Kendrick Lamar', f"bad{i}", rating=2) for i in range(3)]
        p = taste.build('u', songs, now=NOW)
        assert p.artists['kendricklamar'] > 0.8

    def test_an_artist_rated_one_star_with_nothing_liked_is_disliked(self):
        p = taste.build('u', [song(1, 'Tate McRae', 'x', rating=2), song(2, 'Tate McRae', 'y', plays=1, days_ago=5)],
                        now=NOW)
        assert 'tatemcrae' in p.disliked and p.artists['tatemcrae'] < 0

    def test_seeds_are_loved_songs_loved_lately_first_two_per_artist(self):
        songs = [song(1, 'Old Fave', 'a', rating=10), song(2, 'Now', 'b'), song(3, 'Now', 'c'), song(4, 'Now', 'd')]
        events = plays(2, 'Now', 'b', 1, 2, 3, 4) + plays(3, 'Now', 'c', 1, 2, 3) + plays(4, 'Now', 'd', 1, 2, 3)
        p = taste.build('u', songs, events, now=NOW)
        assert [s.key for s in p.seeds][:2] == ['2', '3'] or [s.key for s in p.seeds][:2] == ['2', '4']
        assert sum(1 for s in p.seeds if s.artist == 'Now') == 2
        assert '1' in [s.key for s in p.seeds]

    def test_favourites_played_lately_come_first(self):
        songs = [song(i, 'Rated Long Ago', f"r{i}", rating=10) for i in range(6)]
        songs += [song(10 + i, 'Played Now', f"p{i}") for i in range(6)]
        events = [e for i in range(6) for e in plays(10 + i, 'Played Now', f"p{i}", 1, 2, 3, 4)]
        p = taste.build('u', songs, events, now=NOW)
        assert p.favourites(2) == ['playednow', 'ratedlongago']

    def test_plex_similar_artists_of_favourites_are_the_next_to_discover(self):
        songs = [song(i, 'Kendrick Lamar', f"k{i}", rating=10, ref='k') for i in range(5)]
        songs += [song(10 + i, 'Lupe Fiasco', f"l{i}", rating=10, ref='l') for i in range(5)]
        songs += [song(20, 'Drake', 'heard and not liked', plays=1, days_ago=200)]
        info = {'k': {'similar': ['J. Cole', 'Lupe Fiasco', 'Drake', 'ScHoolboy Q'], 'styles': ['West Coast Rap'],
                      'moods': ['Confident']},
                'l': {'similar': ['J. Cole', 'Common'], 'styles': ['Conscious Rap'], 'moods': ['Literate']}}
        p = taste.build('u', songs, now=NOW, artist_info=info.get)
        assert list(sorted(p.similar, key=lambda a: -p.similar[a]))[0] == 'jcole'   # two favourites point to it
        assert 'lupefiasco' not in p.similar                     # already a favourite
        assert 'drake' not in p.similar                          # heard, never came to like
        assert set(p.styles) == {'West Coast Rap', 'Conscious Rap'} and 'Confident' in p.moods
        assert p.similar_via['jcole'] == ['Kendrick Lamar', 'Lupe Fiasco']


def _profile(**extra):
    songs = [song(i, 'Kendrick Lamar', f"k{i}", rating=10, ref='k') for i in range(12)]
    songs += [song(50, 'Tate McRae', 'greedy', rating=2), song(60, 'Atmosphere', 'Mash', added_days_ago=10)]
    info = {'k': {'similar': ['ScHoolboy Q', 'Atmosphere', 'Ab-Soul'], 'styles': [], 'moods': []}}
    p = taste.build('u', songs, now=NOW, artist_info=info.get)
    for k, v in extra.items():
        setattr(p, k, v)
    return p


class TestFit:
    def test_disliked_artists_and_songs_already_here_score_nothing(self):
        p = _profile()
        assert taste.fit(p, {'artist': 'Tate McRae', 'title': 'new', 'evidence': 1}) == 0
        assert taste.fit(p, {'artist': 'Kendrick Lamar', 'title': 'k3', 'evidence': 1}) == 0

    def test_a_chart_song_needs_an_artist_the_user_likes(self):
        p = _profile()
        charts = taste.chart_candidates([{'artist': 'Some Star', 'title': 'hit'},
                                         {'artist': 'Kendrick Lamar', 'title': 'new single'},
                                         {'artist': 'ScHoolboy Q', 'title': 'Collard Greens'}], taste.TRENDING)
        assert sorted(c['title'] for c in taste.rank(p, charts, 10)) == ['Collard Greens', 'new single']

    def test_an_artist_whose_songs_here_are_unheard_scores_less(self):
        p = _profile()
        fresh = taste.fit(p, {'artist': 'Ab-Soul', 'title': 'x', 'evidence': 0.6})
        unheard = taste.fit(p, {'artist': 'Atmosphere', 'title': 'Sunshine', 'evidence': 0.6})
        assert unheard < fresh * 0.8

    def test_rank_keeps_a_few_per_artist_counting_this_cycles_downloads(self):
        p = _profile()
        cands = [{'artist': 'ScHoolboy Q', 'title': f"t{i}", 'evidence': 0.9 - i / 100, 'source': taste.SIMILAR_ARTIST}
                 for i in range(5)]
        assert len(taste.rank(p, cands, 10)) == taste.PICKS_PER_ARTIST
        assert taste.rank(p, cands, 10, taken=Counter({'schoolboyq': taste.PICKS_PER_ARTIST})) == []

    def test_no_source_takes_every_place(self):
        p = _profile()
        p.similar.update({f"x{i}": 1.0 for i in range(6)})
        graph = [{'artist': f"X{i}", 'title': 't', 'evidence': 1.0, 'source': taste.SIMILAR_ARTIST} for i in range(6)]
        songs = [{'artist': 'Ab-Soul', 'title': 'a', 'evidence': 0.6, 'source': taste.SIMILAR_SONG}]
        picked = taste.rank(p, graph + songs, 7, per_source=3)
        assert [c['source'] for c in picked][:4] == [taste.SIMILAR_ARTIST] * 3 + [taste.SIMILAR_SONG]
        assert len(picked) == 7                                   # the rest wait behind, as spares

    def test_a_source_whose_picks_went_badly_weighs_less(self):
        cand = {'artist': 'ScHoolboy Q', 'title': 'x', 'evidence': 0.3, 'source': taste.SIMILAR_SONG}
        good = taste.rank(_profile(weights={taste.SIMILAR_SONG: 1.4}), [cand], 5)
        bad = taste.rank(_profile(weights={taste.SIMILAR_SONG: 0.55}), [cand], 5)
        assert good and good[0]['confidence'] > 0.7 and bad == []


class TestCandidates:
    def test_a_song_more_loved_seeds_lead_to_has_stronger_evidence(self):
        p = _profile()
        def lookup(artist, title):
            found = [{'artist': 'ScHoolboy Q', 'title': 'Collard Greens', 'confidence': 0.9, 'source': 'lastfm_similar'}]
            if title == 'k0':
                found.append({'artist': 'Ab-Soul', 'title': 'Terrorist Threats', 'confidence': 0.9,
                              'source': 'lastfm_similar'})
            return found
        cands = {c['title']: c for c in taste.similar_song_candidates(p, lookup)}
        assert cands['Collard Greens']['evidence'] > cands['Terrorist Threats']['evidence']
        assert cands['Collard Greens']['source'] == taste.SIMILAR_SONG
        assert cands['Collard Greens']['reason'].startswith('Like Kendrick Lamar - ')

    def test_similar_artists_bring_their_top_songs_spread_over_the_favourites(self):
        p = _profile()
        p.similar = {'a1': 1.0, 'a2': 0.9, 'a3': 0.8, 'b1': 0.5}
        p.similar_via = {'a1': ['Fave A'], 'a2': ['Fave A'], 'a3': ['Fave A'], 'b1': ['Fave B']}
        p.names.update({'a1': 'A1', 'a2': 'A2', 'a3': 'A3', 'b1': 'B1'})
        top = lambda name, n: [{'artist': name, 'title': f"{name} hit {i}"} for i in range(n)]
        found = taste.artist_candidates(p, top, artists=3, per_artist=2, per_favourite=2)
        assert sorted({c['artist'] for c in found}) == ['A1', 'A2', 'B1']
        assert all(c['source'] == taste.SIMILAR_ARTIST and 'Fave' in c['reason'] for c in found)

    def test_top_songs_of_another_act_with_a_similar_name_are_left_out(self):
        p = _profile()
        p.similar, p.names['x'] = {'x': 1.0}, 'X'
        assert taste.artist_candidates(p, lambda name, n: [{'artist': 'X Tribute Band', 'title': 't'}]) == []


def _db(tmp_path, downloads=(), deletions=()):
    path = str(tmp_path / 'music_ai.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE temp_downloads (artist TEXT, track_title TEXT, reason TEXT, confidence REAL, '
                 'created_at TIMESTAMP)')
    conn.execute('CREATE TABLE deletions (title TEXT, artist TEXT)')
    conn.executemany('INSERT INTO temp_downloads VALUES (?, ?, ?, ?, ?)', downloads)
    conn.executemany('INSERT INTO deletions VALUES (?, ?)', deletions)
    conn.commit()
    conn.close()
    return path


class TestPicks:
    def test_downloads_from_before_picks_were_written_down_do_not_count(self, tmp_path):
        path = _db(tmp_path, [('Gunna', 'pop it.mp3', 'From popular playlist: RapCaviar', 0.9, '2026-10-30 18:36:22')])
        p = taste.build('u', [song(1, 'Gunna', 'pop it', rating=2)], now=NOW)
        assert taste.source_weights(path, p, now=NOW)[taste.POPULAR_PLAYLIST] == 1.0

    def test_each_source_is_weighed_by_how_its_picks_were_taken(self, tmp_path):
        path = _db(tmp_path, deletions=[('Gone Song', 'Deleted Artist')])
        long_ago = NOW - 30 * DAY
        for artist, title, source in (('Loved', 'one', taste.SIMILAR_ARTIST), ('Loved', 'two', taste.SIMILAR_ARTIST),
                                      ('Never', 'played', taste.TRENDING),
                                      ('Deleted Artist', 'Gone Song', taste.TRENDING)):
            taste.record_pick(path, artist, title, source, 0.8, 'r', when=long_ago)
        taste.record_pick(path, 'Too', 'new', taste.SIMILAR_SONG, 0.8, 'r', when=NOW - DAY)
        songs = [song(1, 'Loved', 'one', rating=10), song(2, 'Loved', 'two', rating=9), song(3, 'Never', 'played')]
        weights = taste.source_weights(path, taste.build('u', songs, now=NOW), now=NOW)
        assert weights[taste.SIMILAR_ARTIST] > 1.2 and weights[taste.TRENDING] < 0.8
        assert weights[taste.SIMILAR_SONG] == 1.0                             # nothing old enough to judge


class TestPickedForYou:
    def test_unheard_songs_that_sound_like_loved_ones_best_first(self):
        songs = [song(1, 'Fave', 'loved', rating=10), song(2, 'Fave', 'loved too', rating=9),
                 song(10, 'New', 'close'), song(11, 'New', 'far'), song(12, 'Heard', 'already', plays=1, days_ago=9),
                 song(13, 'Tate McRae', 'disliked', rating=0), song(14, 'Tate McRae', 'low', rating=2)]
        p = taste.build('u', songs, now=NOW)
        near = {'1': [('10', 0.05), ('11', 0.25), ('12', 0.01), ('13', 0.02)], '2': [('10', 0.1)]}
        assert taste.picked_for_you(p, lambda key: near.get(key, [])) == ['10', '11']

    def test_a_few_songs_per_artist_even_by_an_artist_never_played(self):
        songs = [song(1, 'Fave', 'loved', rating=10)] + [song(10 + i, 'Prolific', f"s{i}") for i in range(6)]
        p = taste.build('u', songs, now=NOW)
        assert 'prolific' in p.disliked                     # no more downloads of theirs
        picked = taste.picked_for_you(p, lambda key: [(str(10 + i), 0.01 * i) for i in range(6)])
        assert picked == ['10', '11', '12']                 # but the songs here get heard


class _Playlist:
    playlistType, smart = 'audio', False

    def __init__(self, title, items):
        self.title, self._items, self.deleted, self.summary = title, list(items), False, None

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        self._items = [i for i in self._items if i not in items]

    def addItems(self, items):
        self._items += list(items)

    def reload(self):
        pass

    def delete(self):
        self.deleted = True

    def editSummary(self, text):
        self.summary = text


def _track(key, artist, title, **kw):
    return SimpleNamespace(ratingKey=key, title=title, grandparentTitle=artist, originalTitle=None,
                           grandparentRatingKey=f"a-{artist}", viewCount=kw.get('plays', 0), skipCount=0,
                           userRating=kw.get('rating', 0), lastViewedAt=None, addedAt=None)


class _Server:
    def __init__(self, tracks, playlists=()):
        self.tracks, self.lists, self.created = tracks, list(playlists), []
        self.library = SimpleNamespace(section=lambda name: SimpleNamespace(key=1, searchTracks=lambda: self.tracks))

    def playlists(self):
        return [p for p in self.lists if not p.deleted]

    def createPlaylist(self, title, items):
        made = _Playlist(title, items)
        self.lists.append(made)
        self.created.append(made)
        return made


class TestRefreshForYou:
    @pytest.fixture
    def plex(self, monkeypatch):
        monkeypatch.setattr(taste, 'MIN_LOVED', 1)
        monkeypatch.setattr(taste, 'play_events', lambda *a, **k: [])
        monkeypatch.setattr(taste, 'account_ids', lambda *a, **k: set())
        monkeypatch.setattr(taste, 'song_neighbours', lambda server: lambda key: [('10', 0.05), ('11', 0.1)])
        tracks = [_track(1, 'Fave', 'loved', rating=10), _track(10, 'New', 'a'), _track(11, 'New', 'b')]
        return tracks

    def test_made_on_the_users_own_account(self, plex):
        admin, user = _Server(plex), _Server(plex)
        assert taste.refresh_for_you(admin, user, 'Music', 'u', is_admin=False) == 2
        assert [t.ratingKey for t in user.created[0].items()] == [10, 11] and not admin.created
        assert user.created[0].summary

    def test_refilled_in_place_so_a_pin_keeps_working(self, plex):
        old = _Playlist(taste.for_you_playlist_name(), [plex[2]])
        user = _Server(plex, [old])
        taste.refresh_for_you(_Server(plex), user, 'Music', 'u', is_admin=False)
        assert not user.created and not old.deleted and [t.ratingKey for t in old.items()] == [10, 11]

    def test_a_user_whose_taste_is_unknown_has_none(self, plex, monkeypatch):
        monkeypatch.setattr(taste, 'MIN_LOVED', 5)
        old = _Playlist(taste.for_you_playlist_name(), [plex[1]])
        user = _Server(plex, [old])
        assert taste.refresh_for_you(_Server(plex), user, 'Music', 'u', is_admin=False) == 0
        assert old.deleted and not user.created


class TestAccounts:
    def test_the_admin_is_the_owner_account_and_users_match_by_name(self):
        admin = SimpleNamespace(
            systemAccounts=lambda: [SimpleNamespace(id=1, name='owner'), SimpleNamespace(id=7, name='Kid'),
                                    SimpleNamespace(id=9, name='Someone Else')],
            myPlexAccount=lambda: SimpleNamespace(users=lambda: [SimpleNamespace(id=8, username='kid', title='Kid')]))
        assert taste.account_ids(admin, 'owner', is_admin=True) == {1}
        assert taste.account_ids(admin, 'kid', is_admin=False) == {7, 8}

    def test_plays_of_other_accounts_are_not_this_users(self):
        rows = [SimpleNamespace(accountID=7, type='track', ratingKey=5, viewedAt=None, grandparentTitle='A', title='x'),
                SimpleNamespace(accountID=9, type='track', ratingKey=6, viewedAt=None, grandparentTitle='B', title='y')]
        admin = SimpleNamespace(history=lambda **kw: rows)
        events = taste.play_events(admin, SimpleNamespace(key=1), {7}, now=NOW)
        assert [e[0] for e in events] == ['5']


class TestLiking:
    """taste.liking: everything known about how the user takes one song."""

    def heard(self, finished=0, partial=0, skipped=0, share=1.0):
        import listening
        h = listening.Heard(finished=finished, partial=partial, skipped=skipped)
        h.fractions = [share] * (finished + partial + skipped)
        return h

    def test_a_rating_still_speaks_loudest(self):
        s = song(1, 'A', 'T', plays=9, rating=2)
        kept = taste.Signals(kept=True, repeats=3, requested=True)
        assert taste.liking(9, s, kept) == -1.0
        assert taste.liking(0, song(1, 'A', 'T', rating=8)) >= taste.LOVED

    def test_tracked_early_skips_count_when_plex_saw_fewer(self):
        s = song(1, 'A', 'T', plays=1, skips=0)
        assert taste.liking(1, s, taste.Signals(heard=self.heard(finished=1, skipped=3))) < 0
        assert taste.liking(1, s) > 0

    def test_a_song_usually_left_halfway_is_liked_less(self):
        s = song(1, 'A', 'T', plays=3)
        through = taste.liking(3, s, taste.Signals(heard=self.heard(finished=3, share=1.0)))
        halfway = taste.liking(3, s, taste.Signals(heard=self.heard(partial=3, share=0.5)))
        assert halfway < through

    def test_partial_listens_count_as_some_listening(self):
        s = song(1, 'A', 'T')
        assert taste.liking(0, s, taste.Signals(heard=self.heard(partial=2, share=0.6))) > 0

    def test_a_song_on_repeat_is_liked_more(self):
        s = song(1, 'A', 'T', plays=2)
        assert taste.liking(2, s, taste.Signals(repeats=2)) == pytest.approx(taste.liking(2, s) + 0.2)

    def test_album_and_artist_ratings_lean_an_unrated_song(self):
        s = song(1, 'A', 'T', plays=1)
        loved_album = taste.Song(**{**s.__dict__, 'album_rating': 10})
        hated_album = taste.Song(**{**s.__dict__, 'album_rating': 2})
        assert taste.liking(1, hated_album) < taste.liking(1, s) < taste.liking(1, loved_album)
        assert taste.liking(1, s, taste.Signals(artist_rating=2)) < taste.liking(1, s)

    def test_kept_in_their_own_playlist_is_loved(self):
        assert taste.liking(0, song(1, 'A', 'T'), taste.Signals(kept=True)) >= taste.LOVED

    def test_asked_for_holds_until_they_skip_it_more_than_they_play_it(self):
        assert taste.liking(0, song(1, 'A', 'T'), taste.Signals(requested=True)) == taste.REQUESTED_LIKING
        skipped = song(1, 'A', 'T', plays=1, skips=4)
        assert taste.liking(1, skipped, taste.Signals(requested=True)) < 0

    def test_plays_again_within_a_day_are_repeats(self):
        events = plays(1, 'A', 'T', 10, 10.5, 30) + plays(2, 'B', 'U', 5)
        assert taste.repeats(events) == {'1': 1}


class TestProfileSignals:
    def test_listens_playlists_requests_and_artist_ratings_reach_the_profile(self):
        import listening
        songs = [song(1, 'A', 'Kept'), song(2, 'A', 'Asked'), song(3, 'B', 'Halfway'), song(4, 'C', 'Unheard', ref='c'),
                 song(5, 'C', 'Rated artist', plays=1, days_ago=1, ref='c')]
        h = listening.Heard(partial=2, artist='B', title='Halfway')
        h.fractions = [0.5, 0.5]
        p = taste.build('ann', songs, now=NOW, listens={'old-key': h}, kept={'1'},
                        requested=[('A', 'Asked')], artist_ratings={'c': 2})
        assert p.songs['1'] >= taste.LOVED and '1' in p.heard
        assert p.songs['2'] == taste.REQUESTED_LIKING and '2' not in p.heard
        assert p.songs['3'] > 0 and '3' in p.heard            # matched by artist and title (Plex re-added it)
        assert '4' not in p.heard and p.songs['4'] < 0        # its artist is rated 1★
        assert p.songs['5'] < taste.liking(1 * taste.decay(1), songs[4])

    def test_only_the_users_own_playlists_count(self):
        class PL(SimpleNamespace):
            def items(self):
                return self.songs
        server = SimpleNamespace(playlists=lambda: [
            PL(title='Road trip', playlistType='audio', smart=False, songs=[SimpleNamespace(ratingKey=1)]),
            PL(title='Soon to go', playlistType='audio', smart=False, songs=[SimpleNamespace(ratingKey=2)]),
            PL(title='You Might Like', playlistType='audio', smart=False, songs=[SimpleNamespace(ratingKey=3)]),
            PL(title='AI: calm piano', playlistType='audio', smart=False, songs=[SimpleNamespace(ratingKey=4)]),
            PL(title='Smart one', playlistType='audio', smart=True, songs=[SimpleNamespace(ratingKey=5)]),
            PL(title='Videos', playlistType='video', smart=False, songs=[SimpleNamespace(ratingKey=6)])])
        assert taste.own_playlist_songs(server) == {'1'}

    def test_requests_are_read_for_that_user(self, tmp_path):
        import json
        (tmp_path / 'discord_requests.json').write_text(json.dumps({
            'Ann': [{'artist': 'A', 'title': 'Asked'}, {'artist': 'A', 'title': ''}], 'bob': [{'artist': 'B', 'title': 'X'}]}))
        assert taste.requested_songs('ann', str(tmp_path)) == [('A', 'Asked')]


class TestLasting:
    """Whether a song deserves its place counts every play in full."""

    def test_a_song_finished_long_ago_is_no_dislike(self):
        s = song(1, 'A', 'Old favourite', plays=1, days_ago=300)
        p = taste.build('ann', [s], plays(1, 'A', 'Old favourite', 300), now=NOW)
        assert p.songs['1'] < 0.1                       # not something they play lately
        assert p.lasting['1'] >= 0.3                    # but they heard it through

    def test_three_stars_is_liked_a_little(self):
        assert taste.liking(0, song(1, 'A', 'T', rating=6)) >= taste.RATED_FAIR_LIKING
        assert taste.liking(0, song(1, 'A', 'T', rating=4)) < 0       # 2★
