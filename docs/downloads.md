# Downloads

Every path that fetches a song — a cycle, a Discord request, Fix Metadata
replacing wrong audio — uses `downloader.Downloader.download_track()`.

## The pipeline

1. **Resolve the request.** Request-context overrides, a metadata lookup
   for the right album, and cleanup of video-title noise (`[Official
   Video]`, `- Topic` channels, fan and lyric channels). Official uploads
   often read "Title | Event - Artist"; the half of the title that matches
   the uploader is taken as the artist, and a trailing `| …` is dropped.
   Artist and title are swapped only when the title is a known artist (in
   Lidarr, or a Deezer artist of that exact name with ≥ 50 fans) and the
   artist is not — the shape of the strings proves nothing.
2. **The version.** A request asks for a version (live, acapella, acoustic,
   remix, extended, instrumental, cover, sped up …) when the requested title
   names it, when the linked video's own title does ("Song (Live at
   Wembley)", "Song - Acapella", "Song | Live from NYC"), or when the
   requester's message says so; otherwise it asks for the original. A word
   that is part of a song's name ("Live Your Life") is not a version. Only
   that version is accepted, from any tool: Deezer's results are filtered on
   their version label (kept apart from the title, so every version of a song
   looks alike otherwise), and when Deezer lacks it, the next tool — in the
   end the linked video itself — provides it. The local AI picks among what
   is left, seeing each candidate's version and length and the video's
   length and description. "Original Mix" and "Explicit" are not versions.
   A video's description is only for the AI to read: text rules ignore it
   ("Provided to YouTube by ITNO/Columbia" names the label, not the artist),
   and only what the requester wrote can name a performer ("… by Artist").
   A channel is named for its artist: "Joe Hisaishi Official" and
   "いきものがかり Official YouTube Channel" are filed as the artist, and
   "いきものがかり 『ブルーバード』Music Video" as the song ブルーバード
   (`naming.artist_from_channel`, `naming.song_in_quotes`).
   It decides on its own even when it is not strongly sure; only when it
   is merely guessing and the download is someone's request does it ask
   them (see below).
3. **Skip what is there.** A song already in the library is reused unless a
   specific version was asked for; songs the library review deleted
   (blacklisted) are refused, unless someone requests them. Its title must
   match closely ("Epilogue" is not "Prologue" by the same artist), and for a
   release's own track title the brackets count: "Theme of Love (ensemble)"
   is not "Theme of Love".
4. **Lidarr** takes album requests when `LIDARR_USE_AS_DOWNLOADER=true`
   (it has no single-song downloads).
5. **deemix** (Deezer: FLAC or 320 kbps as the account allows; a free
   Deezer account gets 128 kbps) → **SoundCloud** → **yt-dlp** (YouTube).
   With a YouTube link from the requester, that video comes before searching
   SoundCloud. Each result is checked against the request
   (`_is_download_match`, transliterating across scripts: "детство" matches
   "Detstvo"); a file that doesn't match is deleted and the next tool tried.
   The check reads a file's version from its tags and its name. A searched
   file that names no artist is refused, however well its title matches:
   SoundCloud is full of other people's songs called "Prologue". The video
   the requester linked is not checked at all: it is the recording they
   mean, whatever it is called. Nor is a Deezer track a playlist was matched
   to (see below). SoundCloud tracks
   offered only as a 30-second preview are skipped, and a file under half the
   length of the linked video is refused. deemix runs in a
   worker thread with a hard timeout; since a thread can't be killed, a
   download that finishes after its timeout is parked in
   `DATA_DIR/late_downloads/`, out of the library. A link to a catalogue
   page (Shazam, Spotify, Apple Music, Deezer …) is searched for by artist
   and title instead of handed to yt-dlp, which can't fetch audio from it.
   A YouTube link always becomes the one video (`/source/ID/shorts` would
   otherwise be every Short using that sound), and yt-dlp never takes more
   than one entry. Downloads run in `DATA_DIR/download_staging/`, outside the
   library, and that folder is removed whatever happens.
6. **Metadata.** deemix files keep their Deezer tags; SoundCloud and yt-dlp
   files get tags from Discogs / Last.fm / MusicBrainz / Spotify, and the
   YouTube thumbnail as art when nothing better is found. An album named for
   another version of the song ("… (instrumental)", "… (Live)") is not
   taken. Every name written — tags and folders — is checked last
   (`naming.balanced`): a list of artists is never split inside brackets,
   and an unclosed bracket is cut ("RISE (ft. The Glitch Mob" → "RISE").
7. **Filing.** `<library>/<Primary Artist>/<Album>/<Title>.<ext>` with a
   `cover.jpg`; Fix Metadata and the Lidarr reconcile file it the way Lidarr
   does later.

**Asking the requester.** For a request posted in Discord and downloaded
right away, the AI judges every source's results — even a single one — and gives its
confidence (0–1). Below `REQUEST_CHOICE_BELOW` (0.1: it is only guessing)
the requester gets a drop-down list of the results in their private
thread, plus **None of them match**, and a **Select** button that sends
the choice back: the chosen song is downloaded, or *None of them match*
moves on to the next source: Deezer → SoundCloud → YouTube, or
Deezer → the linked video → SoundCloud for a YouTube link. That source asks
again only if the AI is guessing again. With no answer within
`REQUEST_CHOICE_TIMEOUT_SECONDS` (15 min), the AI's best guess is
downloaded, as it would be without asking. The time spent waiting does not
count against deemix's timeout. Nothing downloaded in a cycle is ever asked
about — its own picks nor queued requests: it takes the best match, a
wrong one is rated down or put in the Fix Metadata playlist.
The downloader and the bot talk through `DATA_DIR/request_choices/`
(`request_choice.py`), since queued requests download in a cycle process.

**Keeping an album together.** Plex groups an album by its `album` and
`albumartist` tags, not by folder, so one track tagged differently becomes
an album of its own. So the album name never carries the artist (sources
often return "Artist - Album"); a deemix candidate on the already-known
release is preferred; an existing folder under another spelling is reused;
and after filing, `album` and `albumartist` are rewritten to the album the
file was filed under (`_align_album_tags`). Lidarr re-tags the files it
manages too, so decide which writes tags (see [lidarr.md](lidarr.md)).

**deemix's config is patched on disk** for each download: deemix reads
`downloadLocation` and `overwriteFile` from `config.json` at download time
and ignores in-memory settings. `overwriteFile='y'` stops deemix silently
skipping songs it has downloaded before.

## YouTube

yt-dlp needs a proof-of-origin token for YouTube; the `bgutil-provider`
service supplies it (`BGUTIL_POT_PROVIDER_URL`), and Deno solves YouTube's
signature challenges (installed in the image, with the `yt-dlp-ejs`
scripts). Player clients are tried in the order `mweb`, `tv_simply`,
`android_vr`, `web_embedded` (`YTDLP_PLAYER_CLIENTS` overrides it): some
videos answer every logged-out client with "Please sign in" yet play in an
embedded player. Keep the pip plugin `bgutil-ytdlp-pot-provider` on the
same major version as the `bgutil-provider` image.

## VPN

Downloads go through Mullvad, inside the container. `docker/entrypoint.sh`
starts `mullvad-daemon`, logs in with `MULLVAD_ACCOUNT_NUMBER` when the
device isn't registered yet, allows LAN access, routes each subnet in
`LAN_SUBNETS` past the tunnel through the Docker gateway (so Plex, Lidarr
and the local AI stay reachable), optionally pins a relay, and connects.
The container needs `NET_ADMIN`, `SYS_MODULE` and `/dev/net/tun`.
`VPN_REQUIRED=true` refuses to download without the tunnel.

Mullvad replaces the container's `/etc/resolv.conf`, which breaks Docker's
service-name DNS, so neighbouring containers are reached by IP (a static
address for `bgutil-provider` and `metadata-proxy`) or through `extra_hosts`.

**Only downloads need the tunnel.** With `METADATA_PROXY` set, metadata
lookups leave through the `metadata-proxy` service (tinyproxy, built from
`docker/metadata-proxy`), on the Docker network outside the VPN: lyrics
(Genius's API, LRCLIB), tags and cover art (MusicBrainz, Cover Art Archive,
Discogs, Last.fm, iTunes), fingerprints (AcoustID, ACRCloud, audd, Shazam)
and Spotify/Billboard. It is an allowlist (`metadata_proxy.py`,
`METADATA_PROXY_HOSTS` replaces it): anything not on it stays in the tunnel,
and Deezer, YouTube and SoundCloud are never on it, even for lookups, since
downloads come from them. If the proxy is down, lookups fall back to the
tunnel.

## Region locks and bot walls

A requested YouTube video the VPN's exit country can't fetch is handled by
`geo_relay.py`. If the watch page lists the countries the video is allowed
in and the current one is missing, the relay moves into an allowed country;
otherwise a "confirm you're not a bot" wall is a judgement about the exit
IP, and any other country is worth a try. The relay moves to the best
candidate (`PREFERRED_COUNTRIES` order), yt-dlp runs again, up to
`GEO_RELAY_MAX_COUNTRIES` countries, and the configured relay is restored
afterwards. The relay is shared by the whole container, so this is only done
for link requests, never for cycle downloads. `GEO_RELAY_FALLBACK=false`
turns it off.

## The local AI

An OpenAI-compatible model (`LOCAL_AI_API_BASE`, `LOCAL_AI_MODEL`, any of
llama.cpp, LM Studio, vLLM, Ollama's OpenAI API …) is the judge whenever a
text heuristic isn't enough: choosing between plausible Deezer/YouTube
candidates, deciding whether search results are good enough at all,
resolving reversed "Title - Artist" video titles, and confirming that a
YouTube uploader is the performer. If it is unreachable, the downloader
**waits** for it rather than guessing: filing the wrong recording into a
shared library is worse than a late download.

## Deezer login (ARL)

deemix authenticates with Deezer's `arl` cookie. When a login fails, a
probe checks whether the ARL is really dead (Deezer sometimes hands back an
anonymous session, which is retried). If it is:

1. `DATA_DIR/arl_stale.flag` is written and every download pauses;
2. the admin gets a private notice with the ARL page link, and the cycle
   status says Deezer downloads are paused;
3. `arl_server.py` binds `PORT` and offers "Continue with Deezer" (OAuth,
   with a Deezer developer app: `DEEZER_APP_ID`, `DEEZER_APP_SECRET`) or a
   paste form protected by `ARL_SERVER_SECRET`. Publish it behind a reverse
   proxy at `ARL_PUBLIC_URL` to reach it from outside the LAN;
4. the new ARL is written to `~/.config/deemix/.arl` and the mounted `.env`;
   the flag is cleared, the page closes again, and downloads resume where
   they stopped.

## Settings

| Variable | Default | Description |
|---|---|---|
| `DEEZER_ARL` | — | Deezer `arl` cookie (can be set later through the ARL page). |
| `PORT`, `ARL_SERVER_SECRET`, `ARL_PUBLIC_URL` | — | The ARL page: its port (bound only while the ARL is stale), the passphrase of its paste form, and its public address. |
| `DEEZER_APP_ID`, `DEEZER_APP_SECRET`, `DEEZER_REDIRECT_URI` | — | Optional Deezer OAuth app; the redirect defaults to `ARL_PUBLIC_URL/callback`. |
| `PROJECT_ENV_FILE` | `/app/data/project.env` | The mounted `.env` the ARL page updates. |
| `LOCAL_AI_API_BASE`, `LOCAL_AI_MODEL`, `LOCAL_AI_API_KEY` | — | OpenAI-compatible endpoint (`…/v1`), model and key. |
| `REQUEST_CHOICE_BELOW` | `0.1` | The AI's confidence (0–1) under which the requester is asked. |
| `REQUEST_CHOICE_TIMEOUT_SECONDS` | `900` | How long a requester has to choose; `0` never asks. |
| `BGUTIL_POT_PROVIDER_URL` | — | The PO-token service, e.g. `http://172.21.0.250:4416`. |
| `YTDLP_PLAYER_CLIENTS` | — | Override the YouTube client order. |
| `VPN_REQUIRED` | `true` | Refuse to download without the tunnel. |
| `MULLVAD_DAEMON_ENABLED` | — | `1`: start `mullvad-daemon`. |
| `MULLVAD_ACCOUNT_NUMBER` | — | Mullvad account. |
| `MULLVAD_RELAY_COUNTRY`, `MULLVAD_RELAY_CITY` | — | Pin the exit relay. |
| `LAN_SUBNETS` | — | LAN subnets reached past the tunnel, e.g. `192.168.1.0/24`. |
| `METADATA_PROXY` | — | Forward proxy for metadata lookups outside the VPN, e.g. `http://172.21.0.248:8888`; unset, everything goes through the VPN. |
| `METADATA_PROXY_HOSTS` | built-in list | Comma-separated hosts (and their subdomains) that use it, replacing the built-in list. |
| `GEO_RELAY_FALLBACK`, `GEO_RELAY_MAX_COUNTRIES` | `true`, `3` | Region-lock relay switching. |
| `DOWNLOADER_LOCK_MAX_AGE_SECONDS` | — | Age after which a stale download lock is broken. |
