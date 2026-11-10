"""A Shazam request: the page is blocked, so the share caption carries the track."""
from types import SimpleNamespace

import link_extractor
from downloader import Downloader


SHARE = ("Meet Me in the Afterlife (Power Glove Remix) [feat. Power Glove] by Amy Lee "
         "https://www.shazam.com/track/876626608/meet-me-in-the-afterlife-power-glove-remix-feat-power-glove?referrer=share")


def test_share_caption_parsed_into_title_and_artist():
    assert link_extractor._parse_shared_track_text(SHARE) == (
        'Meet Me in the Afterlife (Power Glove Remix) [feat. Power Glove]', 'Amy Lee')
    assert link_extractor._parse_shared_track_text('https://www.shazam.com/track/1/x') == ('', '')
    assert link_extractor._parse_shared_track_text('just a comment') == ('', '')


def test_shazam_falls_back_to_share_caption_when_page_is_blocked(monkeypatch):
    # Shazam answers 405 to datacenter addresses, so the page yields nothing.
    monkeypatch.setattr(link_extractor.requests, 'get',
                        lambda *a, **k: SimpleNamespace(status_code=405, text=''))
    meta = link_extractor.extract_shazam_metadata(
        'https://www.shazam.com/track/876626608/meet-me-in-the-afterlife-power-glove-remix-feat-power-glove',
        message_text=SHARE,
    )
    assert meta['artist'] == 'Amy Lee'
    assert meta['title'].startswith('Meet Me in the Afterlife')


def test_request_context_artist_is_not_polluted_by_the_link():
    d = Downloader.__new__(Downloader)
    context = (f"source=shazam; requested_track=Unknown Artist - Meet Me In The Afterlife; "
               f"url=https://www.shazam.com/track/876626608/x; message={SHARE}")
    meta = d._resolve_effective_download_metadata(
        artist='Unknown Artist', title='Meet Me In The Afterlife', album='', request_context=context)
    assert meta['artist'] == 'Amy Lee'


def test_catalogue_pages_are_searched_not_downloaded():
    d = Downloader.__new__(Downloader)
    assert d._is_catalogue_page('https://www.shazam.com/track/876626608/x') is True
    assert d._is_catalogue_page('https://open.spotify.com/track/abc') is True
    assert d._is_catalogue_page('https://music.apple.com/us/album/x/1') is True
    assert d._is_catalogue_page('https://www.youtube.com/watch?v=dQw4w9WgXcQ') is False
    assert d._is_catalogue_page('https://soundcloud.com/artist/track') is False

    seen = {}
    d._download_with_yt_dlp = lambda query, target_dir, **kw: seen.setdefault('query', query) and (False, None)
    d._is_download_match = lambda *a, **k: False
    # A Shazam link must become a search, not a download target.
    d._try_tool_download('yt-dlp', 'Amy Lee', 'Meet Me In The Afterlife',
                         'https://www.shazam.com/track/876626608/x', '/tmp', None, None)
    assert seen['query'] == 'Amy Lee Meet Me In The Afterlife official'

    seen.clear()
    d._try_tool_download('yt-dlp', 'Amy Lee', 'Meet Me In The Afterlife',
                         'https://www.youtube.com/watch?v=dQw4w9WgXcQ', '/tmp', None, None)
    assert seen['query'] == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


def test_spelled_out_variant_name_is_preserved():
    d = Downloader.__new__(Downloader)
    context = ("source=shazam; requested_track=Amy Lee - Meet Me in the Afterlife "
               f"(Power Glove Remix) [feat. Power Glove]; message={SHARE}")
    meta = d._resolve_effective_download_metadata(
        artist='Amy Lee',
        title='Meet Me in the Afterlife (Power Glove Remix) [feat. Power Glove]',
        album='', request_context=context)
    # The remix is named in full rather than collapsed to a bare "(Remix)";
    # the featured-artist bracket is dropped for a clean filename.
    assert meta['title'] == 'Meet Me in the Afterlife (Power Glove Remix)'


def test_variant_label_is_not_appended_twice(tmp_path):
    d = Downloader.__new__(Downloader)
    src = tmp_path / 'in.mp3'
    src.write_bytes(b'x')
    target = tmp_path / 'lib'
    out = d._organize_downloaded_file(
        str(src), str(target),
        {'artist': 'Amy Lee', 'title': 'Meet Me in the Afterlife (Remix)', 'album': 'Single'},
        'Amy Lee', 'Meet Me in the Afterlife (Remix)', 'Single',
        'source=shazam; remix requested', {})
    assert out.endswith('Meet Me in the Afterlife (Remix).mp3')
