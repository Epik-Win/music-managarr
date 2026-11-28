# The library and what leaves it

## One library

Every user shares one library, bind-mounted at `MUSIC_BASE_DIR` (`/music`):

```
/music
└── albums/    <Artist>/<Album>/<Title>.mp3    every song (+ <Title>.lrc lyrics)
```

`albums/` is Lidarr's root folder (see [lidarr.md](lidarr.md)). Point the
Plex music library at the folder that contains it (`/music` on the host
side); nothing else under `/music` needs to exist.

Which songs are on probation or soon to go is kept outside `/music`, as
relative symlinks, so neither Plex nor Lidarr nor a lyrics tool ever lists a
song twice (`library_state.py`):

```
$DATA_DIR/library
├── probation/    <Artist>/<Album>/<Title>.mp3 -> ../../../../../../music/albums/…
├── soon_to_go/   <Artist>/<Album>/<Title>.mp3 -> …
└── index.json    each link's inode
```

- The links are relative, so they resolve from the host as well as from the
  container, as long as the data folder and the music folder sit side by
  side the same way on both (e.g. host `/srv/media/app/data` next to
  `/srv/media/music`, mounted as `/app/data` and `/music`).
- A link's own modification time is when the song entered that state.
- A song that moves (Fix Metadata filing it, Lidarr renaming it) keeps its
  inode, and its link is re-pointed to the new path; a move MusicAI makes
  itself also carries the song's `.lrc` lyrics file along.
- A link whose song is gone is dropped. The state folders never cause a
  song to be deleted on their own.

## A song's life

**Downloaded.** A song MusicAI picks on its own (trending, Spotify's popular
editorial playlists, songs picked from the user's taste, album completion)
arrives **on probation**. A song someone asked for (a Discord request, live or
queued; an Exportify import) arrives as a permanent member of the library.
Lidarr's own downloads are Lidarr's and permanent.

**Scored.** Each user's cycle scores every track for that user
(`app.calculate_engagement_score`, 0–1):

| Signal | Weight |
|---|---|
| plays (`viewCount`, up to 100) | 0.4 |
| few skips (`skipCount`, 0–10) | 0.3 |
| star rating (or the album's) | 0.2 |
| played in the last 7 days | +0.1 |

A song nobody has played or rated scores 0. Played once without skips is
about 0.3 (0.4 within the week); a high rating adds up to 0.2. A song's
score is the **best of all users'**: a song one person loves stays even if
nobody else plays it. This score decides what stays; which songs are picked
for a user comes from their taste ([taste.md](taste.md)), where recent plays
count most.

**Judged only on what listeners did.** The review judges a song only by the
users who played it, skipped it (stopped before the end) or rated it. A song
nobody has played, skipped or rated has said nothing about anyone's taste: it
is neither graduated nor deleted for its score, and stays on probation until
someone hears it. For a listener, a rating speaks louder than the score: 1★
judges it 0, 2★ at most 0.1, 4★ or more at least `GRADUATION_SCORE`. A song
skipped and never played to the end is judged 0.

**Reviewed, once per full pass.** A pass is one download cycle for every
user in turn (`SCAN_INTERVAL`). When the last user's cycle is done, the
manager runs `library_review.py` (`--when end`), when every user's scores
are fresh:

1. **Graduation.** A probationary song judged `GRADUATION_SCORE` (0.55) or
   more by a listener (or rated 4★) leaves probation: it stays for good.
2. **End of probation.** A probationary song that has been in the library
   for a whole pass (downloaded before the pass that just ran), that someone
   has heard, and that every listener judges below the **probation bar** is
   deleted. The bar follows the free space:
   with `DISK_PLENTY_GB` (100) or more free it is `PROBATION_DELETE_SCORE`
   (0.15), so only songs nobody took to are deleted; as free space falls to
   `DISK_LOW_GB` (10) it rises evenly to `PROBATION_DELETE_SCORE_LOW` (0.45),
   so a filling disk keeps only the probationary songs people play (at 55 GB
   free it is 0.30). It never goes above `GRADUATION_SCORE`. A song from
   the pass that just ran waits for the next review, so everyone gets a full
   cycle to listen to it (each user's "Trending Now for You" and "You Might
   Like" playlists hold that cycle's new songs).
3. **Disk space.** When free space is below `DISK_LOW_GB` (10), songs are
   deleted until `DISK_TARGET_GB` (20) is free, at most `DISK_DELETE_MAX`
   (250) per review:
   - songs on the soon-to-go list first, oldest first;
   - then every other song, lowest judgment first (0 for a song nobody has
     heard), where a probationary song's counts `PROBATION_PENALTY` (0.3)
     lower, so it goes before a permanent song judged the same.

   The review also runs when a pass starts (`--when start`), for disk space
   only, so a full disk is cleared before new downloads.

**Soon to go.** A song two users have rated one star or less goes on the
soon-to-go list. It stays playable and is deleted first when disk space runs
low — not before. Each user has a "Soon to go" Plex
playlist holding exactly that list; rating a song on it four stars or more
(`SOON_TO_GO_RESCUE_RATING`, Plex's 0–10 scale) takes it off.

**The admin's Delete playlist.** The admin has a Plex playlist named
"Delete" (`DELETE_PLAYLIST_NAME`; created empty when missing, and only the
admin sees it). Once a day the metadata daemon deletes every song in it, no
matter what: ratings, requests, probation and Lidarr don't keep it
(`delete_playlist.py`). When Lidarr has the file, Lidarr deletes it and its
album is unmonitored, so Lidarr doesn't download the song again; the album's
other songs then stay like any other song (deleted only for disk space, after
lower-scoring songs). The song leaves the playlist with its file, is
blacklisted for every user, and the admin gets a notice listing what was
deleted. A song whose file the container can't reach stays in the playlist.

**The admin's one star** is off unless the admin switches it on (the
`/one_star_delete` command in Discord; `ONE_STAR_DELETE` is the default, off).
While it is on, a track the admin rates exactly one star is deleted within a
minute (`one_star_watcher.py`). Only the admin's one star deletes anything, and
only a rating given while deletion is on: the time it was switched on is kept
(`ONE_STAR_DELETE_SINCE` in `DATA_DIR/settings.json`), and songs rated one
star before that, such as while it was off, stay when it is switched back
on. To delete one of them, clear its rating and rate it one star again.

**Blacklisting.** A rating is easy to give by mistake, so no rating and no
one-star deletion blacklists a song; it can always be requested or
downloaded again. Only the review's deletions below and the admin's Delete
playlist blacklist a song, for every user. Asking for a song again (a Discord request) takes it off every
blacklist: an explicit request wins over an earlier deletion.

**Never deleted** by the review: files of albums Lidarr monitors (Lidarr
would download them again — they leave probation instead), songs scoring
`GRADUATION_SCORE` or more, songs Plex has not scanned yet, and songs added
during the pass in progress. Nothing is deleted while `ENABLE_AUTO_DELETE`
is off or while Lidarr can't be asked which songs it keeps.

**Deleting** removes the song with its lyrics files, an album folder left
with only artwork, and an artist folder left empty. The song is blacklisted
in every user's database so no cycle downloads it again, and the deletion
is logged in each database's `deletions` table.

To see what a review would do without changing anything:

```bash
docker exec -w /app musicaio python3 library_review.py --when end --dry-run
```

## Settings

| Variable | Default | Description |
|---|---|---|
| `GRADUATION_SCORE` | `0.55` | Best score at which a probationary song stays for good; songs at or above it are never deleted for space. |
| `PROBATION_DELETE_SCORE` | `0.15` | The probation bar with plenty of free space: below it after a whole pass, a probationary song is deleted. |
| `PROBATION_DELETE_SCORE_LOW` | `0.45` | The probation bar at `DISK_LOW_GB` free or less. |
| `DISK_PLENTY_GB` | `100` | Free space from which the probation bar is at its lowest; below it the bar rises evenly towards `PROBATION_DELETE_SCORE_LOW`. |
| `PROBATION_PENALTY` | `0.3` | Subtracted from a probationary song's score when choosing what to delete for space. |
| `DISK_LOW_GB` | `10` | Free space below which songs are deleted. |
| `DISK_TARGET_GB` | `20` | Free space a disk-space review stops at. |
| `DISK_DELETE_MAX` | `250` | Most songs one review deletes for space: other data on the same disk can fill it, and the library should not pay for all of it. |
| `SOON_TO_GO_RESCUE_RATING` | `8` | Plex rating (0–10) that takes a song off the soon-to-go list. |
| `ENABLE_AUTO_DELETE` | `true` | `false`: the review deletes nothing. |
| `ONE_STAR_DELETE` | `false` | Default for the admin's one-star deletion; `/one_star_delete on\|off` changes it at runtime (kept in `DATA_DIR/settings.json`). |
| `ONE_STAR_POLL_SECONDS` | `60` | How often the one-star watcher checks Plex. |
| `DELETE_PLAYLIST_NAME` | `Delete` | The admin's playlist whose songs the daily pass deletes. |
| `LIBRARY_STATE_DIR` | `$DATA_DIR/library` | Where the state links live. |
