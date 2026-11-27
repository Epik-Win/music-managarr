# Taste: what each user loves, and what they get next

`taste.py` reads each listener the way Plex (and Plexamp's mixes and radios)
does: from their listening history, Plex's music metadata and Plex's sonic
analysis. A cycle uses it to choose which new songs to download for a user; the
daily pass uses it to fill that user's **Picked for You** playlist with songs
already in the library that they haven't heard yet.

## The profile

Built at the start of each user's cycle, from that user's own view of the
library (their plays, skips and star ratings) and from Plex's play history,
which only the server owner can read (so it is read through the admin's
connection and filtered to the user's accounts).

**How much they like each song** (`affinity`, −1 to 1):

| Signal | Effect |
|---|---|
| Plays in Plex's history (last `TASTE_HISTORY_DAYS`) | Each play counts less the older it is: half after `TASTE_HALF_LIFE_DAYS`. A few plays this month make a song loved; plays a year ago barely count. Plays from before the history Plex keeps count by the song's play count and last-played date. |
| 4★ or more | Loved (at least 0.55), whether played or not. |
| 2★ | −0.6 or less; 1★ is −1. |
| Skips | Count against an unrated or 3★ song by how often it is skipped rather than played. |

A song scoring `0.5` or more is **loved**. A play Plex recorded for a song it
has since re-added (moved, re-tagged) is matched to the song by artist and
title.

**How much they like each artist** comes from that artist's songs: the loved
ones add, the disliked ones take off half as much. An artist with many loved
songs and a few rated low is still a favourite. **Favourites** are ordered
by that and by how much they were played lately. The first
`TOP_ARTIST_LIMIT` (5) are the artists Lidarr follows for the user; the list
is redone in each download cycle.

**Seeds** are the loved songs, the ones loved lately first, at most two per
artist (`TASTE_SEEDS`).

From Plex's metadata of the favourite artists (the 40 most liked) come their
**styles** and **moods** and Plex's **similar artists**. An artist that
several favourites list is the next one to discover. Artists already loved,
disliked, or heard without being liked are not.

**Disliked artists** get no more downloads: one of their songs rated 1★ and
none 3★ or more, or five of their songs here for a month and none played.

The profile in short (favourites, styles, moods, artists to discover, seeds,
the weight of each source) is written to `taste.json` in the user's data
folder, and the cycle logs it.

## What a cycle downloads

Every candidate is scored against the profile, 0–1:

- **evidence**: how strongly its source ties it to this user. A song
  Last.fm or Deezer finds similar to loved seeds scores higher the more seeds
  lead to it and the more they are loved. A top song of an artist Plex lists
  with the favourites scores by how strongly they are listed (and its rank
  among that artist's songs). A chart or playlist hit gets a small base
  score: it is popular, but nothing ties it to this user.
- **the artist**: how the user feels about it, or how close Plex puts it to
  their favourites.

`score = 0.55 × evidence + 0.45 × artist`, times the **source's weight**
(below). Disliked artists and songs already in the library score 0. An
artist with songs here the user has never heard scores less: those songs come
first, in Picked for You. A candidate below `TASTE_MIN_FIT` is dropped. The
best go first, at most `TASTE_PICKS_PER_ARTIST` per artist across the whole
cycle. Past 60 % of "You Might Like", a source's songs wait behind the other
sources' (as spares for songs that fail to download), so every source has
picks to be judged by.

| Stage | Without a known taste | With one (`TASTE_MIN_LOVED` loved songs) |
|---|---|---|
| Trending charts | The first `CHART_PICKS` that download | The `CHART_PICKS` that fit best; none if none fit |
| Popular playlists | The first `CHART_PICKS` that download | The `CHART_PICKS` that fit best; none if none fit |
| Songs for "You Might Like" | Last.fm songs similar to the last songs played | `TASTE_PICKS` songs: similar to the seeds, and top songs of Plex's similar artists, ranked together |
| Album completion | The missing songs of albums they love | The same |

## Learning from what was picked

Every song a cycle picks on its own is written down with its source (`picks`
table in the user's database). Once the user has had `14` days to hear one,
it counts for its source:

| The user | Counts |
|---|---|
| loves it | 1 |
| played it | 0.6 |
| never played it | 0.15 |
| skips it or rated it low; it was deleted | 0 |

A source's weight is `0.5 + (sum + 1) / (count + 2)`: 1.0 with nothing to
judge, up to 1.5 for a source whose picks are loved, down to 0.5 for one
whose picks go unheard or get deleted. The sources are similar songs, similar
artists, trending, popular playlists and album completion.

## Picked for You

Once a day (`fix_metadata_daemon.py`), every user whose taste is known gets a
**Picked for You** playlist on their own account: up to `FOR_YOU_SIZE` library
songs they have never played, rated or skipped, that sound most like their
loved songs. It uses Plex's sonic analysis: each seed's sonic neighbours
(`/library/metadata/<id>/nearest`, up to `SONIC_MAX_DISTANCE`) count by how
loved the seed is and how close they sound. At most three songs per artist;
none by an artist the user rated low or keeps skipping. Artists disliked only
because their songs were never played are in: this is where those songs get
heard. The playlist is refilled in place (a pin keeps working) and removed for
a user whose taste isn't known yet.

New songs get Plex's sonic analysis during Plex's nightly maintenance, so a
song downloaded today can be picked from tomorrow on.

Sonic analysis needs Plex Pass and the library's "sonic analysis" setting.
Without it, Picked for You stays empty and the downloads work as above.
