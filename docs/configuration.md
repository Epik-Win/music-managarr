# Configuration

All settings are environment variables of the `musicai` service. Secrets go
in `.env` (see `.env.example`); `docker-compose.example.yml` passes them in.

## Required

| Variable | Description |
|---|---|
| `PLEX_URL` | Plex server, e.g. `http://192.168.1.10:32400`. |
| `PLEX_TOKEN` | The server owner's token (Plex Home users' tokens are derived from it). |
| `WIZARR_MAPPING_FILE` | The user mapping, `{"discord_handle": "plex_username"}` (default `/data/wizarr/plex_invite_targets.json`). |
| `LOCAL_AI_API_BASE`, `LOCAL_AI_MODEL` | An OpenAI-compatible endpoint (`…/v1`: LiteLLM, llama.cpp, vLLM, Ollama …) and the model to ask; `LOCAL_AI_API_KEY` is its key, if it needs one. `/ai_playlist` can use a key and model of its own ([discord.md](discord.md#ai-playlists)). |
| `MULLVAD_ACCOUNT_NUMBER`, `MULLVAD_DAEMON_ENABLED=1` | The VPN, required while `VPN_REQUIRED=true`. |
| `MUSIC_BASE_DIR`, `DATA_DIR` | Container paths of the music folder and the state (`/music`, `/app/data`). |
| `HOST_MUSIC_BASE_PATH` | The host folder that contains `music/`, as Plex sees it (e.g. `/srv/media`); unset, Plex is assumed to see `/music` too. |

## Users and Plex

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TO_PLEX_MAPPING_ADMIN` | first entry | Which mapping entry is the admin: `discord_handle:plex_username`, or one name. |
| `PLEX_MUSIC_SECTIONS` | `Music` | Name of the Plex music library. |
| `PLEX_SERVER_NAME` | — | Only when the account has several servers. |
| `USER_ACTIVITY_WINDOW_DAYS` | `30` | A user with no play in this window gets no cycle. |

## Cycle

| Variable | Default | Description |
|---|---|---|
| `SCAN_INTERVAL` | `604800` | Seconds between passes (a week). |
| `CYCLE_ON_STARTUP` | `true` | Start the first pass right away when no schedule exists yet. |
| `CYCLE_STAGGER_SECONDS` | `30` | Pause between users' cycles. |
| `PREFERENCE_CONFIDENCE_THRESHOLD` | `0.95` | Taste confidence a user needs before automatic downloads. |
| `MIN_TRACKS_FOR_CONFIDENCE` | `50` | Played or rated tracks needed for a confidence value. |
| `SCORE_THRESHOLD` | `0.3` | Score from which a track counts as liked (taste model, confidence). |
| `TOP_ARTIST_LIMIT` | `5` | Favourite artists per user followed in Lidarr (from their taste; their recent listening while it isn't known). |
| `ENABLE_TIKTOK_TRENDING` | `false` | Include TikTok in trending discovery. |
| `TRENDING_PLAYLIST_NAME` | `Trending Now for You` | Each user's playlist of their last cycle's trending songs (charts, popular playlists). A playlist titled "Trending Now" is renamed to it. |
| `SIMILAR_PLAYLIST_NAME` | `You Might Like` | Each user's playlist of their last cycle's songs picked from their taste (songs like their loved ones, similar artists' top songs, album completion). Both are removed when the cycle is skipped or brings nothing for them. |
| `ALBUM_COMPLETION_SCORE` / `ALBUM_COMPLETION_MIN_LOVED` | `0.55` / `3` | Album completion only for albums a user clearly loves: this many of its songs scoring this much (or rated 4★+). |
| `ALBUM_COMPLETION_MAX_ALBUMS` / `ALBUM_COMPLETION_MAX_TRACKS` | `2` / `30` | Albums completed per cycle; an album missing more songs than this is left alone. |
| `LASTFM_SIMILARITY_THRESHOLD` | `0.85` | Minimum Last.fm match for similar tracks (only while a user's taste isn't known yet). |

## Taste

See [taste.md](taste.md).

| Variable | Default | Description |
|---|---|---|
| `TASTE_PICKS` | `20` | Songs a cycle downloads for "You Might Like" from the user's taste. |
| `CHART_PICKS` | `10` | At most this many songs each from the trending charts and the popular playlists; with a known taste, only those that fit. |
| `TASTE_HALF_LIFE_DAYS` | `90` | A play counts half as much after this many days. |
| `TASTE_HISTORY_DAYS` | `365` | Days of Plex's play history read. |
| `TASTE_MIN_LOVED` | `10` | Loved songs a user needs before songs are picked by their taste (and before they get Picked for You). |
| `TASTE_SEEDS` | `25` | Loved songs the picks start from. |
| `TASTE_MIN_FIT` | `0.35` | Score (0–1) a candidate needs to be downloaded. |
| `TASTE_PICKS_PER_ARTIST` | `2` | Songs per artist a cycle picks at most, across all sources. |
| `ENABLE_FOR_YOU_PLAYLIST` | `true` | Each user's daily Picked for You playlist. |
| `FOR_YOU_PLAYLIST_NAME` | `Picked for You` | Its name. |
| `FOR_YOU_SIZE` | `50` | Songs in it. |
| `SONIC_MAX_DISTANCE` | `0.3` | How far (Plex's sonic distance) a song may sound from a loved one to be picked for it. |

## Library

See [library.md](library.md).

| Variable | Default | Description |
|---|---|---|
| `GRADUATION_SCORE` | `0.55` | Score at which a probationary song stays for good. |
| `PROBATION_DELETE_SCORE` / `PROBATION_DELETE_SCORE_LOW` | `0.15` / `0.45` | The probation bar with `DISK_PLENTY_GB` or more free / at `DISK_LOW_GB`: below it after a whole pass, a probationary song is deleted. |
| `DISK_PLENTY_GB` | `100` | Free space from which the probation bar is at its lowest. |
| `PROBATION_PENALTY` | `0.3` | Lowers a probationary song's score when choosing what to delete for space. |
| `DISK_LOW_GB` / `DISK_TARGET_GB` | `10` / `20` | Free space that starts / ends deleting for space. |
| `DISK_DELETE_MAX` | `250` | Most songs deleted for space per review. |
| `SOON_TO_GO_RESCUE_RATING` | `8` | Plex rating (0–10) that takes a song off the soon-to-go list. |
| `ENABLE_AUTO_DELETE` | `true` | `false`: nothing is deleted by the review. |
| `ONE_STAR_DELETE` / `ONE_STAR_POLL_SECONDS` | `false` / `60` | The admin's one-star deletion (default; `/one_star_delete` changes it at runtime) and how often it checks Plex. |
| `DELETE_PLAYLIST_NAME` | `Delete` | The admin's Plex playlist whose songs are deleted once a day. |
| `LYRICS_ENABLED` / `LYRICS_PER_PASS` / `LYRICS_RETRY_DAYS` | `true` / `1500` / `30` | Lyrics files from LRCLIB: on or off, songs looked up per day, days before a song LRCLIB lacked is asked again. See [fix-metadata.md](fix-metadata.md#lyrics-files). |
| `LIBRARY_STATE_DIR` | `$DATA_DIR/library` | Probation and soon-to-go links. |
| `ALBUM_DIR` | `/music/albums` | The library. |
| `PUID` / `PGID` | owner of `ALBUM_DIR` | Who owns the songs and folders MusicAI writes (it runs as root for the VPN; Lidarr, running as this user, must be able to rename and re-tag them). Checked every few minutes by the metadata daemon (`library_owner.py`). |

## Downloads

See [downloads.md](downloads.md).

| Variable | Default | Description |
|---|---|---|
| `DEEZER_ARL` | — | Deezer `arl` cookie for deemix. |
| `BGUTIL_POT_PROVIDER_URL` | — | yt-dlp PO-token service. |
| `YTDLP_PLAYER_CLIENTS` | — | YouTube client order override. |
| `VPN_REQUIRED` | `true` | Refuse to download without the tunnel. |
| `MULLVAD_RELAY_COUNTRY`, `MULLVAD_RELAY_CITY` | — | Pin the exit relay. |
| `LAN_SUBNETS` | — | Comma-separated LAN subnets reached past the tunnel. |
| `METADATA_PROXY` / `METADATA_PROXY_HOSTS` | — / built-in list | A forward proxy outside the VPN for metadata lookups (lyrics, tags, cover art, fingerprints), and the hosts that use it. Downloads always stay in the VPN. See [downloads.md](downloads.md#vpn). |
| `GEO_RELAY_FALLBACK` / `GEO_RELAY_MAX_COUNTRIES` | `true` / `3` | Region-lock relay switching for link requests. |
| `DOWNLOADER_LOCK_MAX_AGE_SECONDS` | `18000` | Age after which a stale download lock is broken. |
| `REQUEST_CHOICE_BELOW` | `0.1` | The AI's confidence (0–1) under which a requester picks the song from a list. |
| `REQUEST_CHOICE_TIMEOUT_SECONDS` | `900` | How long the requester has to pick (then the AI's best guess is taken); `0` never asks. |
| `EXPORTIFY_DIR` | `/music/exportify` | Where `cli.py download-exportify` looks for CSVs. |

## Lidarr

See [lidarr.md](lidarr.md): `LIDARR_URLS`, `LIDARR_API_KEYS`, `LIDARR_PATH_MAP`,
`LIDARR_ROOT_FOLDER`, `LIDARR_QUALITY_PROFILE_ID`, `LIDARR_METADATA_PROFILE_ID`,
`LIDARR_USE_AS_DOWNLOADER`, `LIDARR_USE_AS_METADATA`, `MERGER_DRY_RUN`,
`MERGER_QUARANTINE_DIR`.

## Metadata and identification

See [fix-metadata.md](fix-metadata.md).

| Variable | Default | Description |
|---|---|---|
| `FIX_METADATA_INTERVAL_SECONDS` | `86400` | How often the metadata daemon runs each job. |
| `METADATA_REPAIR_DRY_RUN` | `true` | Log only; `false` to repair. |
| `METADATA_REPAIR_MAX_TRACKS` | `100` | Library tracks repaired per run. |
| `PLEX_REFILE_ENABLED` | `false` | Re-file albums Plex lists under the wrong artist. |
| `LAST_FM_API_KEY`, `DISCOGS_TOKEN` | — | Similar tracks; album metadata and art. |
| `ACOUSTID_API_KEY` | — | AcoustID fingerprinting. |
| `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET`, `ACRCLOUD_HOST` | — | ACRCloud identification. |
| `AUDD_API_TOKEN` | — | audd identification. |
| `LYRICS_ID_ENABLED` | `true` | Identify by sung lyrics when fingerprinting finds nothing. |
| `GENIUS_ACCESS_TOKEN` | — | Genius API client access token. |
| `LYRICS_ID_SEARCHES_PER_DAY` | `30` | SearXNG searches per day. |
| `WHISPER_URL`, `SEARXNG_URL` | — | An OpenAI-compatible transcription endpoint and SearXNG (otherwise containers named `litellm` (port 4000) and `searxng` (8080) are looked up). |
| `WHISPER_MODEL` | `whisper-1` | Transcription model. Use a large Whisper (large-v3-turbo): small ones can hear nothing in singing over loud music. |
| `DEBUGGARR_URL` | — | Debugarr's address (`http://debuggarr:8095`). With it, Manual Fix songs go to Debugarr's local AI first, and the admin gets one message per batch once it is settled ([fix-metadata.md](fix-metadata.md#manual-fix-and-debugarr)). |
| `DEBUGGARR_API_SECRET` | — | Debugarr's `API_SECRET`; or `DEBUGGARR_API_SECRET_FILE`, a file holding it (Debugarr's own env file works). |

## Discord and the ARL page

See [discord.md](discord.md) and [downloads.md](downloads.md#deezer-login-arl).

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Empty disables the bot. |
| `DISCORD_MAIN_CHANNEL` | — | Requests channel id (or name). |
| `DISCORD_ENABLED` | `true` | |
| `PORT` | — | ARL page port; the page is off without it. |
| `ARL_SERVER_SECRET` | `musicai` | Passphrase of the ARL paste form — change it. |
| `ARL_PUBLIC_URL` | — | Public address of the ARL page. |
| `DEEZER_APP_ID`, `DEEZER_APP_SECRET`, `DEEZER_REDIRECT_URI` | — | Optional Deezer OAuth app. |
| `PROJECT_ENV_FILE` | `/app/data/project.env` | The mounted `.env` the ARL page updates. |
| `FLASK_SECRET` | random | Session key of the ARL page. |

## Logging

`LOG_LEVEL` (`INFO`).
