# Lidarr integration

Lidarr is optional, but with it MusicAI's library is organized and upgraded
the way Lidarr does it. MusicAI uses one Lidarr instance (the first one
configured) as the owner of the library, plus, per user, the instance that
follows that user's top artists.

## What MusicAI uses Lidarr for

| Role | What happens | Code |
|---|---|---|
| **Library owner** | Lidarr's only root folder is the library, `/music/albums`. Every song lives there, including songs on probation, so Lidarr can rename, re-tag and upgrade (to FLAC) anything it manages. | `lidarr_catalog.py` |
| **Filing** | A song Lidarr knows (its album and track on Lidarr's selected release) is filed with Lidarr's folder, file naming, album and track titles and track number, then Lidarr is asked to rescan the folder so it takes the file as its own. A later upgrade replaces the file instead of adding a copy. A file Lidarr already owns is never moved by MusicAI. | `fix_metadata._file_away`, `_lidarr_place` |
| **Duplicate albums** | A second copy of an album ("D-12 World" next to Lidarr's "D12 World") is folded into Lidarr's folder, renamed Lidarr's way. Duplicate songs keep Lidarr's copy. | `album_artist_merger.lidarr_reconcile`, `song_dedup.py` |
| **Metadata** | The daily repair asks Lidarr for an unknown album first (an official, non-compilation release), before any fingerprinting. | `metadata_repair.py` |
| **Top artists** | Each user's `TOP_ARTIST_LIMIT` most-played recent artists are added and monitored, so their new releases arrive on their own — one Lidarr serves every user. Every other artist is kept unmonitored, and the root folder's monitoring defaults are held at *None*. | `app.sync_top_artists_to_lidarr`, `enforce_top_artist_monitoring` |
| **Album requests** (optional) | With `LIDARR_USE_AS_DOWNLOADER=true`, album requests go to Lidarr (it searches and downloads the whole album) instead of song by song. A Discord playlist that is really one album is handed to Lidarr either way. Album completion fetches only the missing songs, so it never goes through Lidarr. | `downloader.LidarrClient` |
| **Deletion safety** | Files of albums Lidarr monitors are never deleted by the library review (Lidarr would download them again), and nothing is deleted while Lidarr can't be asked. | `library_review.py` |

Why monitoring is restricted: Lidarr adds an artist whenever it recognises
that artist's files in its root folder, and "monitored" means "download
everything this artist releases". With the whole library in Lidarr's root,
the default monitoring would start downloading the discography of every
artist in it. So an artist MusicAI adds to fetch one album is added
unmonitored, and only the top artists are monitored.

## Setting up Lidarr

1. **Mount the same library.** Lidarr must see the files MusicAI sees. The
   simplest is the same path in both containers:

   ```yaml
   services:
     lidarr:
       image: lscr.io/linuxserver/lidarr:latest
       volumes:
         - /path/to/lidarr/config:/config
         - /srv/media/downloads:/downloads
         - /srv/media/music:/music          # same as musicai's /music
   ```

   If Lidarr sees the library under another path, tell MusicAI with
   `LIDARR_PATH_MAP=<lidarr prefix>=<musicai prefix>`, e.g.
   `LIDARR_PATH_MAP=/data/music=/music`.

   MusicAI runs as root (the VPN needs it), so it hands every song and
   folder it writes to the library's owner — `PUID`/`PGID`, or whoever owns
   `/music/albums` — before Lidarr touches them. Run Lidarr as that same
   user; otherwise Lidarr's renames and tag writes fail with "Access to the
   path is denied".

2. **Root folder.** *Settings → Media Management → Root Folders*: add
   `/music/albums` and nothing else. Set the root folder's **Monitor** to
   *None* and **Monitor New Albums** to *None* (MusicAI holds them there
   anyway). Pick the quality and metadata profiles new artists should get.

3. **Naming.** *Settings → Media Management*: turn on *Rename Tracks*. Any
   track format works; MusicAI reads it from Lidarr. A common one:

   ```
   {Album Title} ({Release Year})/{Artist Name} - {Album Title} - {track:00} - {Track Title}
   ```

   Lidarr applies it only to files it imports from then on; files it
   already had keep their names. Rename those once (*Library → Mass Editor*,
   select all, *Rename Files*), with Plex's *Empty trash automatically
   after every scan* off until Plex has scanned the moves, so ratings and
   play counts follow the files. MusicAI never moves a file Lidarr owns: a
   cleanup asks Lidarr to move its own files instead.

4. **Tags.** *Settings → Metadata → Write Audio Tags*. Lidarr re-tags the
   files it manages from MusicBrainz on its disk scans. Leave it on to let
   MusicBrainz names win for those files (MusicAI then treats Lidarr's names
   as correct), or set it to *Never* to keep MusicAI's tags. If Lidarr's
   *scrub* option is on, it strips embedded art; MusicAI also writes a
   `cover.jpg` into each album folder, which keeps the cover.

5. **API key.** *Settings → General → Security → API Key*.

6. **Point MusicAI at it** (`docker-compose.yml` / `.env`):

   ```yaml
   environment:
     - LIDARR_URLS=http://192.168.1.10:8686
     - LIDARR_API_KEYS=${LIDARR_API_KEY}
     - LIDARR_QUALITY_PROFILE_ID=1        # profiles for artists MusicAI adds
     - LIDARR_METADATA_PROFILE_ID=1
   ```

   MusicAI's container runs its traffic through the VPN, which replaces the
   container's DNS resolver, so address Lidarr by IP (or an `extra_hosts`
   entry), not by its container name.

## Settings

| Variable | Default | Description |
|---|---|---|
| `LIDARR_URLS`, `LIDARR_API_KEYS` | — | Parallel comma-separated lists, one entry per instance (usually one, shared by every user). `LIDARR_URL` / `LIDARR_API_KEY` work for a single instance. |
| `LIDARR_PATH_MAP` | — | `lidarr_prefix=musicai_prefix` when Lidarr sees the library under another path. |
| `LIDARR_ROOT_FOLDER` | `/music/albums` | Root folder for artists MusicAI adds. |
| `LIDARR_QUALITY_PROFILE_ID`, `LIDARR_METADATA_PROFILE_ID` | `1` | Profiles for artists MusicAI adds. |
| `TOP_ARTIST_LIMIT` | `5` | Artists per user kept monitored. |
| `LIDARR_USE_AS_DOWNLOADER` | `false` | Hand album requests to Lidarr. |
| `LIDARR_USE_AS_METADATA` | `false` | Count Lidarr's album names as a metadata source for downloads. |
| `MERGER_DRY_RUN` | — | `true`: the daily album reconcile only reports. |

**Several users.** One Lidarr follows every user's top artists: user *N*
(in the order of the user mapping, admin first) uses entry *N* of
`LIDARR_URLS` when there is one, and the first instance otherwise. With
four users and one Lidarr, up to `4 × TOP_ARTIST_LIMIT` artists are
monitored. When an artist drops out of one user's top list it stays
monitored as long as another user on the same instance still follows it.
The library itself always belongs to the first entry.

## Checking it

```bash
# Lidarr answers and has the expected root folder
curl -s -H "X-Api-Key: $LIDARR_API_KEY" http://192.168.1.10:8686/api/v1/rootfolder | jq '.[].path'

# MusicAI can read the catalogue
docker exec -w /app musicaio python3 -c "from lidarr_catalog import LidarrCatalog as C; c = C.from_env().load(); print(len(c.albums), 'albums,', len(c.root_folders), 'root folder(s)')"

# What the album reconcile would change, for one artist
docker exec -w /app musicaio python3 album_artist_merger.py --pass lidarr --artist "D12" --dry-run
```

Artists whose path is outside the root folder (left over from an import from
another folder) can be moved in Lidarr's *Mass Editor* to `/music/albums`
without moving files, then rescanned.

## When Lidarr is down

MusicAI keeps working without it: songs are filed in MusicAI's own layout,
the album reconcile is skipped, and top artists' recent releases are
downloaded directly. The
library review deletes nothing until Lidarr can say which songs it keeps.
