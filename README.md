# MusicAI

A self-hosted music curator for Plex. MusicAI watches what each member of a
Plex Home listens to, learns their taste, downloads new music that fits it,
keeps everything in one clean `Artist/Album/Title` library (organized by
Lidarr when you have it), and removes what nobody listens to. Members request
songs, albums and playlists by pasting a YouTube, Spotify, TikTok or Shazam
link in a Discord channel.

It runs as one Docker container next to Plex, with all download traffic
going through a Mullvad VPN tunnel inside the container.

## Features

- **Per-user taste.** Every Plex Home user is scored from their own plays,
  skips, ratings and recency. Automatic downloads start for a user once their
  taste is clear enough (`PREFERENCE_CONFIDENCE_THRESHOLD`).
- **New music that fits.** Each cycle completes albums in the library,
  pulls in trending music (Billboard by genre, TikTok, Spotify), fulfils
  queued requests, follows each user's top artists in Lidarr, and finds songs
  similar to what they play. Every user gets two playlists of their last
  cycle's new songs: "Trending Now for You" (the trending songs) and "You
  Might Like" (songs picked from their taste: similar tracks, albums they
  love). A playlist is removed when that cycle brought nothing for it.
- **A library that cleans itself.** Songs MusicAI picks on its own start on
  probation. A song someone loves graduates; one nobody plays by the end of
  the next cycle is deleted; when the disk runs low, the least-liked songs go
  first. Songs two users dislike are deleted first. See
  [docs/library.md](docs/library.md).
- **Requests over Discord**, each user in their own private thread, with the
  channel itself showing only the download-cycle status. When the local AI
  is only guessing which search result is the requested song, the requester
  picks it from a list (or "None of them match", which tries the next
  source). See
  [docs/discord.md](docs/discord.md).
- **Metadata that gets fixed.** Drop a mistagged song in a Plex playlist
  called *Fix Metadata* and it is fingerprinted (or identified by its sung
  lyrics), retagged, refiled and given cover art. A daily scan repairs the
  whole library. See [docs/fix-metadata.md](docs/fix-metadata.md).
- **Lidarr as the librarian** — its folders, naming, upgrades and new
  releases. See [docs/lidarr.md](docs/lidarr.md).
- **The admin's one star deletes:** a song the admin rates exactly one star
  is deleted within a minute, without blacklisting it (a mistaken rating is
  easy to undo by requesting the song again). The admin turns this on and off
  with `/one_star_delete`.
- **No passwords.** Only the Plex admin's token is configured; each Plex Home
  user's token is derived from it, so each user's history is read as them.

## How it works

`docker/entrypoint.sh` starts the Mullvad daemon and hands off to
`entrypoint.py`, the manager, which runs:

| Process | File | Purpose |
|---|---|---|
| Manager | `entrypoint.py` | Reads the users, finds their Plex tokens, runs the cycle schedule and restarts companion processes that exit. |
| Cycle worker | `app.py cycle-once` | One short-lived process per user, in turn: a full analysis and download cycle with that user's token and database. |
| Library review | `library_review.py` | After every user's cycle of a pass: graduation, end of probation, disk space. |
| Discord bot | `discord_bot.py` | Requests, slash commands, the cycle-status message, admin notices. |
| Metadata daemon | `fix_metadata_daemon.py` | Daily: Fix Metadata playlists, library repair, Manual Fix, album reconcile. |
| One-star watcher | `one_star_watcher.py` | Deletes songs the admin rates exactly one star. |
| ARL page | `arl_server.py` | Renews the Deezer login; listens only while it has expired. |

**A pass** is one cycle for every user, one after another (admin first),
every `SCAN_INTERVAL`. The schedule is kept on disk
(`DATA_DIR/cycle_schedule.json`), so a restart neither restarts the countdown
nor starts a new pass; an interrupted pass resumes with the users that had
not finished. A user's cycle:

1. **Analyse** — score every track for this user.
2. **Skip inactive users** — no play in `USER_ACTIVITY_WINDOW_DAYS` days
   (from Plex's last-played dates, not old counts or ratings) and the cycle
   is skipped. Plex being unreachable is not a skip: the worker exits so the
   manager retries.
3. **Album completion**, **trending**, **queued Discord requests**,
   **popular Spotify playlists**, **top artists** (synced to Lidarr), and
   **similar tracks** (Last.fm, Deezer, Spotify). Trending charts and popular
   playlists leave out artists the user clearly doesn't like: one of their
   songs rated 1★ or less and none 3★ or more, or five or more of their songs
   in the library for a month and none ever played. Album completion is for
   albums the user clearly loves — `ALBUM_COMPLETION_MIN_LOVED` (3) songs
   scoring `ALBUM_COMPLETION_SCORE` (0.55) or rated 4★ — and downloads only
   the songs missing from Deezer's track list of that release (the one
   holding the songs already here), at most two albums a cycle.
4. **Finalise** — songs two users dislike go on the soon-to-go list, the
   user's "Soon to go", "Trending Now for You" and "You Might Like"
   playlists are refreshed, and folders of one album under an artist ("21",
   "21 (Deluxe Edition)") are merged — never albums of different artists that
   share a name. The two cycle playlists are emptied when a cycle starts;
   at the end, the trending charts' and popular playlists' songs go in
   "Trending Now for You", the similar tracks' and album completion's in
   "You Might Like" (a user's own requests go in neither). Duplicate copies
   are deleted, and a playlist the cycle brought nothing for — or every
   playlist of a user whose cycle is skipped — is removed, so neither ever
   shows an older cycle's songs.

Progress goes to `DATA_DIR/cycle_running_<user>.json`, which the bot turns
into the status message.

**Users** come from one file, `WIZARR_MAPPING_FILE`: a JSON object of
`{"discord_handle": "plex_username"}` (Wizarr maintains it as people are
invited; you can also write it by hand). The first entry is the admin, whose
token is `PLEX_TOKEN`; `DISCORD_TO_PLEX_MAPPING_ADMIN` overrides that. It is
re-read every cycle and whenever someone joins the Discord server.

## Requirements

- Docker with Compose, on the host or LAN of a Plex server with a music
  library and Plex Home users
- A Mullvad account, and a Deezer account (its `arl` cookie)
- An OpenAI-compatible LLM endpoint (llama.cpp, LM Studio, vLLM, Ollama …)
- Optional: Lidarr, a Discord bot, a Whisper endpoint and SearXNG (lyrics
  identification), API keys for Last.fm, Discogs, AcoustID, ACRCloud, Genius

## Quick start

```bash
git clone <this repository> musicai && cd musicai
cp .env.example .env                                   # secrets and per-site values
cp docker-compose.example.yml docker-compose.yml       # host paths, IPs, services
$EDITOR .env docker-compose.yml
docker compose pull musicai
docker compose up -d
docker logs -f musicaio
```

The `musicai` service uses the published
[`ghcr.io/epik-win/music-managarr`](https://github.com/Epik-Win/music-managarr/pkgs/container/music-managarr)
image. GitHub publishes `latest` from `main`; the repository's Dockerfile is
still available for local development and custom builds.

Then:

1. **Plex token.** Plex Web → any item → ⋯ → *Get Info* → *View XML*: the
   URL ends with `X-Plex-Token=…`. Use the server owner's token.
2. **Paths.** Mount the music folder at `/music` (the library is
   `/music/albums`) and a data folder at `/app/data`, side by side on the
   host (e.g. `/srv/media/music` and `/srv/media/app/data`). Set
   `HOST_MUSIC_BASE_PATH` to the folder containing `music/` as Plex sees it
   (`/srv/media`). Point Plex's music library at `/srv/media/music`.
3. **Users.** Create the mapping file (or let Wizarr) and mount its folder
   at `/data/wizarr`:

   ```json
   { "alice": "alice_plex", "bob": "bob_plex" }
   ```

4. **Discord**, **Lidarr** and **lyrics**: see
   [docs/discord.md](docs/discord.md), [docs/lidarr.md](docs/lidarr.md) and
   [docs/fix-metadata.md](docs/fix-metadata.md).
5. **First run.** `CYCLE_ON_STARTUP=false` waits `SCAN_INTERVAL` before the
   first pass; set it to `true` for the very first start, or start one by
   hand (below). Metadata repair starts in dry-run mode
   (`METADATA_REPAIR_DRY_RUN=true`): read its log, then turn it off.

Every setting is listed in [docs/configuration.md](docs/configuration.md).

## Operating it

```bash
docker logs -f musicaio                       # everything; one user: grep 'USER: alice'

# start the next pass at the next check (within a minute)
docker exec -w /app musicaio python3 -c "import cycle_schedule as c; d = c.load(); d['next_due'] = c.now().isoformat(); c.save(d)"

# one user's cycle right now (0 = admin)
docker exec -w /app musicaio python3 -c "import entrypoint; m = entrypoint.MultiUserManager(); m.validate_configuration(); m.build_worker_plan(); m.run_user_cycle(0)"

# what the library review would do
docker exec -w /app musicaio python3 library_review.py --when end --dry-run
```

CLI tools, inside the container (`docker exec -w /app musicaio …`):

```
python3 cli.py cleanup-albums [--dry-run]
    Tidy the library: every pass of album_artist_merger.py (what
    /clean_library runs); --dry-run only logs what would change.
python3 cli.py download-exportify [csv_file] [target_dir]
    Download every track of a Spotify Exportify CSV.
python3 cli.py clear-db [--force] [--song <path>] [--drop]
    Reset a user's scores, forget one file or folder, or drop the database.
python3 album_artist_merger.py [--pass lidarr|songs|plex|…] [--artist NAME] [--dry-run]
    Run the album reconcile passes by hand.
```

## Project layout

```
├── src/                 the application (copied flat into /app in the image)
├── tests/               pytest suite: fakes only, no network or services
├── docker/entrypoint.sh container entrypoint (Mullvad, then entrypoint.py)
├── docs/                guides
├── Dockerfile
├── docker-compose.example.yml
├── .env.example
└── requirements.txt / requirements-dev.txt
```

| Module | Role |
|---|---|
| `entrypoint.py` | The manager: configuration, Plex tokens, schedule, child processes. |
| `app.py` | `MusicAIAnalyzer` (scoring, discovery sources, Lidarr top artists) and the cycle (`_run_cycle`, `_stage_*`). |
| `library_state.py`, `library_review.py` | Probation and soon-to-go state; what stays and what goes. |
| `downloader.py`, `downloader_manager.py` | The download pipeline, `LidarrClient`, the VPN; the shared download lock and Deezer login state. |
| `metadata_proxy.py`, `library_owner.py` | Metadata lookups outside the VPN (downloads stay in it); library files handed to the library's owner. |
| `discord_bot.py`, `link_extractor.py`, `admin_notify.py` | The bot; links → tracks; private notices for the admin. |
| `fix_metadata_daemon.py`, `fix_metadata.py`, `metadata_repair.py`, `manual_fix.py` | Daily metadata work. |
| `audio_id.py`, `lyrics_id.py` | Fingerprinting (AcoustID, ACRCloud, audd, Shazam) and identification by lyrics. |
| `artwork.py`, `text_repair.py` | Cover art; mojibake. |
| `lidarr_catalog.py`, `album_artist_merger.py`, `song_dedup.py`, `plex_refile.py` | Lidarr's catalogue; duplicate albums and songs; albums under the wrong artist. |
| `one_star_watcher.py` | The admin's one-star deletions. |
| `geo_relay.py`, `spotify_web.py`, `rate_limit.py` | Region locks; Spotify without its API; API rate limits. |
| `cycle_schedule.py`, `users.py`, `plex_auth.py`, `paths.py`, `naming.py` | Schedule, user mapping, Plex tokens, host ↔ container paths, artist-name parsing. |
| `arl_server.py`, `cli.py` | The ARL page; maintenance commands. |

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

The tests never touch Plex, Discord, the network or the AI: they use fakes,
and `tests/conftest.py` points `DATA_DIR` at a temporary folder.

## Design decisions

- **One library, many users.** Everyone shares the files; scores are per
  user, and a song stays if anyone likes it. The admin's one-star deletion
  removes the file for everyone, on purpose, for a household library.
- **Ratings never blacklist.** A star rating is too easy to give by mistake
  to make a song impossible to get back; only deletions the library review
  decides blacklist a song.
- **State outside the library.** Probation and soon-to-go are symlinks in
  `DATA_DIR`, not separate music folders: a song never moves when its state
  changes, so Plex keeps its history and Lidarr can manage it.
- **Deletion decided once per pass.** Each user has their own scores, and
  their cycles run one after another; deciding in one user's cycle would
  judge songs on the others' month-old scores.
- **The activity gate uses plays only.** Ratings and counts stay in Plex
  forever; only a play inside the window counts as activity.
- **Lidarr monitors only top artists.** "Monitored" means "download
  everything this artist releases", and with the whole library in Lidarr's
  root it would otherwise follow every artist it recognises.
- **Lidarr is album-only.** It has no single-song download, so songs always
  go through deemix, SoundCloud or yt-dlp.
- **Spotify without its API.** The developer API gates the useful endpoints
  behind quotas and premium checks; everything comes from public pages via
  `spotify_scraper`.
- **The AI is allowed to block.** When the local model is down, downloads
  wait; guessing is the one thing the pipeline must not do.
- **Tokens, not passwords.** A token can be revoked without changing the
  password, grants API access only, and works with two-factor
  authentication.
- **Fixed IPs for sidecars.** Mullvad replaces the container's resolver, so
  neighbours are addressed by IP rather than service name.
- **Relay changes are per request and undone.** The tunnel is shared by the
  whole container, so a region-lock workaround lasts one link request.
- **The folder is the album's identity.** Downloaders and metadata sources
  disagree on album names constantly; the album a file is filed under is
  written back into its tags, because that is what Plex groups by.

## Known limitations

- Only Mullvad is supported as the VPN.
- SoundCloud offers no quality choice; whatever the stream has is taken.
- TikTok trending uses TikTok's unofficial public endpoints, which change
  without notice; it is off unless `ENABLE_TIKTOK_TRENDING=true`.
- Shazam refuses datacenter addresses, so a Shazam link is only as good as
  the share caption that came with it.
- YouTube's bot wall follows the exit IP, so a request can still fail when
  every relay tried is distrusted.
- A genuine compilation filed under one artist is credited to that artist,
  not "Various Artists" (the folder decides the album artist).
- Lidarr and MusicAI both write tags; on files Lidarr manages, whichever
  wrote last wins (see [docs/lidarr.md](docs/lidarr.md)).
- A tag edit that leaves a file's size unchanged is not picked up by a Plex
  scan or a track refresh; refreshing the album makes Plex re-read it.
- Some modules have similarly named helpers (`sanitize_filename`,
  `normalize_album_name`, …) with different rules for their callers.
