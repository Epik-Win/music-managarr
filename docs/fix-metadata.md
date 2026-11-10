# Fixing metadata

`fix_metadata_daemon.py` runs next to the download cycle, on its own
schedule (`FIX_METADATA_INTERVAL_SECONDS`, one day by default), because a
cycle may run only once a month. Each run:

1. processes every user's **Fix Metadata** Plex playlist: a user drops a
   mistagged song in it, and it is identified, retagged, refiled and taken
   off the playlist (`fix_metadata.py`);
2. scans the library for songs with an unknown album (or the placeholder
   "Miscellaneous"), an unknown artist, or a Various Artists album without a
   track artist, and repairs them from Lidarr first, then by audio
   fingerprint (`metadata_repair.py`). The same scan repairs mojibake and
   malformed names (an unclosed bracket, a video title's " | " in an
   artist), fetches missing artwork and finds albums Plex files under the
   wrong artist. A tagged album is only kept when the identified song's
   sources name the same album *and version*: "RISE (instrumental)" is not
   "RISE" for the song with vocals;
3. puts whatever neither could identify in the admin's **Manual Fix**
   playlist, removes songs from it once they are complete, and keeps one
   short notice about it in the admin's Discord thread (`manual_fix.py`);
4. reconciles albums with Lidarr: folds second copies of an album into
   Lidarr's version, removes duplicate songs and merges albums Plex split in
   two (`album_artist_merger.lidarr_reconcile`).

Each job keeps its own last-run time in `DATA_DIR/fix_metadata_last_run_<key>.json`,
so a restart only runs what is due. `METADATA_REPAIR_DRY_RUN=true` (the
default) makes steps 1–3 log what they would change and skips the users'
playlists; set it to `false` to repair. `MERGER_DRY_RUN=true` does the same
for step 4.

## Identifying a song

A fingerprint answer is evidence, not truth. The rules, in the order they
apply:

- **The ISRC first.** deemix writes the recording's ISRC. A wrong fix can
  rewrite the names but leaves the ISRC, so Deezer's track for that ISRC,
  when its length matches the file, names the recording.
- **Several slices, majority vote** (`audio_id.identify_by_votes`): Shazam
  on three or four 20 s slices, ACRCloud on two, AcoustID on the whole file.
  One sample often matches an intro or a sampled beat. Two or more agreeing
  votes confirm an answer; a lone vote is a suggestion.
- **Lyrics when nothing knows the audio** (`lyrics_id.py`). Whisper
  transcribes up to three 45 s clips. With fewer than 12 real words (an
  instrumental, or Whisper's "thanks for watching" hallucinations) nothing is
  searched. Up to eight distinctive sung lines are searched on Genius
  (`GENIUS_ACCESS_TOKEN`), and only when Genius finds nothing on SearXNG
  (at most 3 searches a song, `LYRICS_ID_SEARCHES_PER_DAY` a day, 15 s apart,
  none for an hour after SearXNG reports its engines suspended). Each
  candidate's lyrics come from LRCLIB and are compared run by run with what
  was heard: the best must cover 25% of the heard words and lead the next
  by 10 points. A match within 6 s of the file's length counts as two votes.
  When no lyrics can be compared (LRCLIB lacks them; Genius's API has none),
  Genius's top result for a sung line whose own title is sung at least twice
  — the only such song — is an unconfirmed suggestion, with Genius's album
  (else the title, as a single) and song art. The daily repair only shows it
  in Manual Fix; a song someone put in **Fix Metadata** gets it applied when
  it has no artist and its own name agrees ("Unidentified – Unknown ♫Rain♫"
  becomes Maddhouse – "Rain").
- **Manual Fix keeps a song** until its names change from the ones it was
  handed over with (or, handed over for missing artwork, until it has some):
  complete-looking names aren't proof of a fix — a doubtful artist is complete.
- **Re-uploads.** An answer with the file's title under an alias nobody
  follows, when the tagged artist is a real act, is a copy of the same
  audio, not a correction. Censored spellings compare letter for letter
  ("Sh*t" = "S**t").
- **The file's tags are the current names** (`known_meta`), not Plex's
  (Plex can keep a wrong artist). Placeholders ("Miscellaneous", "Unknown",
  an album repeating the artist) count as blanks; a missing title comes from
  the file name ("Artist - Title (Official Video).mp3"). An artist tag
  holding "Artist - Title" is split; a title naming another act gives the
  artist when Deezer lists the song by that act; a title repeating the
  artist as a prefix loses it; a rights-society suffix ("Name (BMI)") is a
  songwriter credit, not an artist.
- **The audio before the names** (`_replace_wrong_audio`). The names can be
  right while the audio isn't the song: a music-video rip with its intro, a
  truncated download, or another recording entirely. The true recording is
  Deezer's release of the title by the artist (most played first, skipping
  radio specials, commentary, karaoke and re-recordings the tags don't
  name), or the artist's own upload for songs only on YouTube. A file more
  than 5 s off is replaced by it, and the old file goes to
  `DATA_DIR/fixmeta_quarantine/replaced_audio/`. If the download fails, the
  song goes to the admin untouched: the services' names for the wrong audio
  are never written instead. Talk, interview and trailer videos never count
  as a recording, and a version must be the same version, remixer included.
- **Junk answers are dropped:** an artist containing the song's own title is
  a cover upload's name.
- **The answer that fits the tags goes first.** When a service confirms the
  names, only what is missing is filled; a second service naming another
  song is a disagreement.
- **Sampled beats.** Votes for another song are a sample when the tagged
  song is released at the file's length and theirs is not (the other song's
  length comes from its ISRC or ACRCloud).
- **One unrelated vote loses to a same-length release** of the tagged song
  (or the artist's own upload) within 3 s.
- **An unrelated song from one service alone goes to the admin** as a
  suggestion, unless it is the same song under other names (artist and title
  swapped) or a second service agrees on the title.
- **Artist.** One act's name stays whole when Lidarr or Deezer know it
  ("Earth, Wind & Fire" is not "Earth"); otherwise the first credited artist.
  An act that now releases under a shorter form of the tagged name is
  written under it ("YBN Cordae" → "Cordae"), unless the tagged name is a
  known act itself.
- **Title.** The tagged one is kept when a service returned it exactly
  (services shorten long titles). A featured credit the tags don't have is
  not added.
- **Album**, in order: the tagged album when any source names it; one Lidarr
  confirms as an official, non-compilation release; Deezer's first listing
  of the same song at the same length; then the fingerprint services'
  albums. "(Remastered 2009)" is dropped. With no album nothing is written:
  the song goes to the admin with what was identified. The local AI may only
  choose among found albums, never name one of its own.
- **YouTube-only singles** that nothing contradicts (a song on probation, or
  an album tag that is a video title) are filed as `Artist/Title/Title.mp3`.
  A rip nothing identifies is proven by the video it came from: the same
  title word for word and the same length to the second; its uploader is the
  artist for channels that make their own content, but not when the title
  names the performer (a concert or a label's upload).
- **A flagged song always has something wrong.** When the names hold, the
  rest is checked against the release — its own title and album, the
  artist's current name, the cover — but only for a recording proven to be
  the song. If everything matches, the admin is told so.

## Artwork

`artwork.ensure_artwork` runs on every fix. It embeds a folder cover in the
file (so the art survives a move), or fetches one, in order: a cover link
from an answer naming these very names, the Cover Art Archive's cover of the
file's own MusicBrainz release, Deezer, iTunes, Spotify, the album on
Deezer, YouTube Music's song entry, the artist's own SoundCloud upload, and
the YouTube video thumbnail (letterbox trimmed, centre square). A result
counts only if the title, its version labels, a part number and the artist
match, within 30 s of the file's length; a service's no-cover placeholder is
no cover. The art is saved as `cover.jpg` (which Plex uses most reliably for
an album) and embedded. When a fix changes a song's identity, its old cover
is replaced, or removed when none is found. A song nothing has a cover for
goes to Manual Fix.

Text is repaired too: a reply decoded with the wrong charset is read as
UTF-8 (`audio_id._json_utf8`), and mojibake already in the library
("MontrÃ©al") is reversed (`text_repair.fix_mojibake`).

## Filing

Every song lives in `albums/`, Lidarr's root folder (`fix_metadata._file_away`):

- a file Lidarr owns stays where it is; if a fix finds it is another song, it
  moves out so Lidarr sees its track missing;
- a song Lidarr knows gets Lidarr's folder, naming, titles and track number,
  and Lidarr rescans the folder to take it;
- otherwise the song joins the album the artist already has under its own
  spelling, or goes to `Artist/Album/Title`;
- a folder a fix empties has its leftover cover set aside in
  `DATA_DIR/fixmeta_quarantine/leftover_art/`, so no empty album stays in
  Plex;
- a move carries the song's probation/soon-to-go state and its lyrics file.

Fixes write the album artist too (Plex lists a track under it) and refresh
the track, album and artist in Plex.

## Albums under the wrong artist

Plex decides a local album's artist once, from the file that created it,
and keeps it: re-tagging or moving the files doesn't move the album. And
Plex doesn't file a song whose artist is missing or "Unknown" under an
unknown artist — it attaches it to some real one, which fills a few "sink"
artists with unrelated albums. So a song nothing can identify is tagged
**Unidentified** (grouped where the admin sees it, and still looked up
again).

`plex_refile.py` finds local albums whose Plex artist none of their files
credit. An artist holding misfiled albums from two or more artists is a
sink: those albums are re-filed. An album whose files name nobody is looked
up song by song. A single stray album is fingerprinted to decide which side
is wrong. Re-filing moves the files into a hidden staging folder (with a
`.plexignore`), lets Plex drop the old items, and moves them back so Plex
files them by their tags; every account's ratings, play counts and playlist
entries, and the old added dates, are put back. It only re-files with
`PLEX_REFILE_ENABLED=true`; otherwise it reports.

## Duplicates

**Albums** (`album_artist_merger.lidarr_reconcile`): every album folder is
fuzzy-matched to a Lidarr album (punctuation, brackets and a year suffix
ignored; the folder name must agree as well as the tags; titles with
different numbers never match). Tracks of a matching copy move into
Lidarr's folder with its naming. Albums Plex split are merged into the item
in Lidarr's folder. Loose audio in the root of `albums/` is filed into
`Artist/Album/` first.

**Songs** (`song_dedup.py`): the same recording filed twice — artist and
title match, version labels match ("Live", "Remix", "Acoustic" … are other
songs), lengths within 3 s, and Chromaprint fingerprints agree (≥ 0.80).
The copy that stays: Lidarr's, then the better quality group, the higher
bitrate, the older file. Files Lidarr owns are never removed. Removed copies
go to `DATA_DIR/merge_quarantine/<stamp>/` with their original path. Don't
rate one copy one star to remove it: Plex keeps ratings per song, not per
file, so every copy gets the rating.

## Lyrics files

[LRCGET](https://github.com/tranxuanthang/lrcget) writes `<Title>.lrc` next
to each song from LRCLIB; Plex reads them with *Prefer local metadata* on.
`docker-compose.example.yml` has a service for it (a desktop in the browser
on port 5800). Point it at `/music`, turn on auto-export of `.lrc`, and
leave embedding off so it never rewrites the audio files. MusicAI moves and
deletes the `.lrc` files with their songs.

## Settings

| Variable | Default | Description |
|---|---|---|
| `FIX_METADATA_INTERVAL_SECONDS` | `86400` | How often each job runs. |
| `METADATA_REPAIR_DRY_RUN` | `true` | Log only; `false` to repair. |
| `METADATA_REPAIR_MAX_TRACKS` | `100` | Library tracks repaired per run (bounds fingerprinting API use). |
| `PLEX_REFILE_ENABLED` | `false` | Re-file albums under the wrong artist instead of reporting them. |
| `MERGER_DRY_RUN` | — | `true`: the album reconcile only reports. |
| `ACOUSTID_API_KEY`, `ACRCLOUD_ACCESS_KEY`, `ACRCLOUD_ACCESS_SECRET`, `ACRCLOUD_HOST`, `AUDD_API_TOKEN` | — | Fingerprinting services. |
| `LYRICS_ID_ENABLED` | `true` | Identify by sung lyrics when fingerprinting finds nothing. |
| `GENIUS_ACCESS_TOKEN` | — | Genius API client access token. |
| `LYRICS_ID_SEARCHES_PER_DAY` | `30` | SearXNG searches allowed per day. |
| `WHISPER_URL`, `SEARXNG_URL` | — | Whisper (`/v1/audio/transcriptions`) and SearXNG; unset, containers named `litellm` (port 4000) and `searxng` (8080) are looked up on the Docker network. |
| `WHISPER_MODEL` | `whisper-1` | Transcription model; a large Whisper (large-v3-turbo) hears singing over loud music that small ones miss. |
