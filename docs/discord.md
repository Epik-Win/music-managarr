# Discord

The bot (`discord_bot.py`) takes song requests and shows the download-cycle
status. One channel (`DISCORD_MAIN_CHANNEL`) is all it needs.

## Setting up the bot

1. Create an application and bot at <https://discord.com/developers/applications>.
   Under *Bot*, turn on the **Message Content** and **Server Members**
   intents (requests are read from messages; the admin is found among the
   members).
2. Invite it with these permissions on the requests channel: *View
   Channel*, *Send Messages*, *Read Message History*, *Manage Messages*
   (it takes request links down and keeps the channel tidy), *Embed Links*,
   *Create Private Threads* and *Send Messages in Threads*.
3. Set `DISCORD_BOT_TOKEN` and `DISCORD_MAIN_CHANNEL` (the channel id; a
   channel name works too). Users are matched to Plex accounts by their
   Discord handle through the user mapping file (see the README).

## The requests channel

The channel shows **only the download-cycle status**: one message, kept
pinned at the bottom, listing every user as waiting, running (with the
stage), completed or skipped for inactivity. It turns green once everyone
is done. When Deezer's login has expired, it says downloads are paused.
On startup the bot removes everything else from the channel.

Everything else is private:

- **Requests** go in the requester's own **private thread** under the
  channel, created on their first request and reused after. Only they and
  the admin are in it (plus anyone with *Manage Threads*). Nothing in a
  thread is auto-deleted, so the request history stays.
- **Admin notices** — errors, the Deezer ARL expiring (with a link to the
  ARL page, `ARL_PUBLIC_URL`), the Manual Fix to-do note — go in the
  admin's own thread (a direct message if it can't be opened). Any process
  can queue one (`admin_notify.py`); a changed notice is posted again so the
  admin is notified, and a resolved one is removed.
- **"Which one is it?"** The local AI picks the search result that is your
  song on its own. Only when it is merely guessing (confidence below
  `REQUEST_CHOICE_BELOW`, 0.1) does the bot ask you in your thread: pick the
  song from the drop-down list — or **None of them match** — and press
  **Select**. The chosen song is downloaded; *None of them match* tries the
  next source (Deezer → SoundCloud → YouTube, or Deezer → the linked video →
  SoundCloud for a YouTube link), which asks again only if the
  AI is guessing again. Only you or the admin can answer. Without an answer
  within `REQUEST_CHOICE_TIMEOUT_SECONDS` (15 min) the AI's best guess is
  downloaded. The list keeps working across bot restarts. A settled
  question is deleted (the confirmation after Select is visible only to
  you): the thread keeps just the request's result, which says what was
  chosen ("🙋 Your choice"). Requests queued while a cycle runs are
  downloaded by the cycle, which never asks: it takes the best match.
- **Slash command replies** are visible only to the person who used the
  command.

## Making a request

- **Paste a link in the channel.** It is taken down at once and handled in
  your thread, which starts with "📨 @you requested: <link>".
- **Paste a link in your thread** to keep even the link out of the channel.
- **`/request link:<url>`** replies with a pointer to your thread and works
  there.

YouTube and YouTube Music, Spotify (tracks, albums, playlists), TikTok,
Instagram and Shazam links are understood (`link_extractor.py`). A link is resolved to artist/title/album (or every
track of a playlist), each track goes through the download pipeline with
the link's details as context (a "live version" or "Arabic cover" is
honoured), Plex is refreshed, and a playlist becomes a Plex playlist of the
same name — unless the playlist is really one album, in which case the album
is enough. Requests are permanent members of the library; they are never on
probation.

**A playlist that is an album** (`album_match.py`). A YouTube playlist named
"Final Fantasy IV: Piano Collection", fourteen videos from a fan's channel
each named only by its track, is that album: its name and its track list say
so. Deezer's albums are searched by the playlist's name. An album is taken
when its name has the playlist's words in any order and at least 70% of the
playlist's songs are on it. A song is a track when their titles agree
("Trojan Beauty" = "Troian Beauty", "Main Theme" = "Main Theme of Final
Fantasy IV"), or when they are equally long to within 3 seconds
("Theme of Love Orchestrated" = "Theme of Love (ensemble)"). A same-named
song of a different length is another recording, unless the video is a
clip under 30 seconds. Each matched song is then downloaded as that exact
Deezer track, with no search, and filed under the album's artist and album;
a song not on the album keeps its own names. Lidarr is not asked, since
Deezer has the album. No Plex playlist is made when the playlist holds at
least 80% of the album. A playlist that isn't an album Deezer has goes to
Lidarr when one is configured. Every playlist song is downloaded
with its own link: a YouTube video comes before searching SoundCloud and is
the recording meant.

**A single video named "Album - Track"**, such as "Final Fantasy VI Piano
Collections - Terra's Theme", is handled the same way (`album_for_song`).
The "artist" it names must be exactly an album's name on Deezer (any word
order), by another artist, and no real artist may go by it: a Deezer artist
of that exact name with 50 or more fans is a band, so "Blue" stays Blue. The
song is then the album's track by title, else the only track as long as the
video to the second ("Terra's Theme" is "Piano: Tina", both 3:37), and that
Deezer track is downloaded. When no track fits, only the artist and album
are taken from the album and the linked video is downloaded. Every single
link costs one Deezer search for this.

Every request and its outcome is kept in `DATA_DIR/discord_requests.json`
(`request_history.py`). The bot records how its own attempt went: `completed`
with the file, or still `pending` with the reason. A request still pending is
tried again by the next cycle, which marks it `completed` or `failed`. The bot
and the cycles change the file one at a time, each from what is on disk then.

While a download cycle or a library cleanup is running, requests wait; the
bot says so privately.

## Commands

| Command | What it does |
|---|---|
| `/request link:<url>` | Request a song, album or playlist (handled in your thread). |
| `/library_status` | How many songs the library has, how many are on probation and soon to go, and your recent requests. |
| `/clean_library [preview]` | Admin only: tidy the shared library in the background (`album_artist_merger.py`, every pass): one folder per artist and per album, Lidarr's names winning (Lidarr moves its own files), loose files filed, albums matched to Lidarr's, duplicate songs — the same recording by sound — moved to the quarantine folder, Plex's split albums joined. Band names are never split ("Earth, Wind & Fire"), different albums are never joined ("The Blueprint 3"), and nothing is deleted outright. `preview:True` only reports what would change. Reports in your thread. |
| `/one_star_delete [on\|off]` | Admin only: whether the admin's one-star rating in Plex deletes the song. Only ratings given after it is switched on count; songs rated one star while it was off stay. Without a choice, shows the setting. |

The admin commands are hidden from members without the **Manage Server**
permission. The server owner always sees them and always counts as admin. If
the admin in `DISCORD_TO_PLEX_MAPPING_ADMIN` isn't the owner, give them Manage
Server, or allow the commands for them under *Server Settings → Integrations*.

## Settings

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token; empty disables the bot. |
| `DISCORD_MAIN_CHANNEL` | Requests channel id (or name). |
| `DISCORD_ENABLED` | `true`/`false`. |
| `DISCORD_TO_PLEX_MAPPING_ADMIN` | Which mapping entry is the admin (`discord_handle:plex_username`, or one name); the first entry otherwise. |
| `ARL_PUBLIC_URL` | Address of the ARL refresh page, linked in the admin's alert. |

The bot keeps its state in `DATA_DIR`: `request_threads.json` (each user's
thread), `discord_requests.json` (request history), `cycle_status_state.json`
(the status message), `admin_notices.json` and `request_choices/` (open
"which one is it?" questions).
