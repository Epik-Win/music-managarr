from types import SimpleNamespace

import pytest

import app

PAGE = """<html><body>
<ul class="o-chart-results-list-row"><li>1</li><li></li><li></li>
  <li><h3 id="title-of-a-story">Choosin' Texas</h3><span class="c-label">Ella Langley</span>
      <ul><li>-</li></ul></li></ul>
<ul class="o-chart-results-list-row"><li>2</li><li></li><li></li>
  <li><h3 id="title-of-a-story">Dracula</h3><span class="c-label">Tame Impala <a>&amp;</a> JENNIE</span></li></ul>
</body></html>"""


def test_a_chart_is_read_without_the_columns_that_change_form(monkeypatch):
    urls = []
    monkeypatch.setattr(app.requests, 'get', lambda url, **kw: urls.append(url) or SimpleNamespace(
        text=PAGE, raise_for_status=lambda: None))
    songs = app._billboard_chart('rap-song')
    assert [(s.rank, s.artist, s.title) for s in songs] == [(1, 'Ella Langley', "Choosin' Texas"),
                                                            (2, 'Tame Impala & JENNIE', 'Dracula')]
    assert urls == ['https://www.billboard.com/charts/rap-song/']


def test_a_page_without_songs_is_an_error(monkeypatch):
    monkeypatch.setattr(app.requests, 'get', lambda url, **kw: SimpleNamespace(
        text='<html></html>', raise_for_status=lambda: None))
    with pytest.raises(ValueError):
        app._billboard_chart('hot-100')


def test_plex_genres_lead_to_their_song_charts_each_read_once(monkeypatch):
    read = []
    monkeypatch.setattr(app, '_billboard_chart', lambda name: read.append(name) or [
        SimpleNamespace(rank=1, artist='A', title=name)])
    analyzer = app.MusicAIAnalyzer.__new__(app.MusicAIAnalyzer)
    found = analyzer.get_billboard_top_by_genres([('Rap, Electronic', 9, 0.5), ('Pop/Rock', 5, 0.4),
                                                  ('Rap', 3, 0.4), ('Stage & Screen', 2, 0.3)])
    assert read == ['rap-song', 'pop-songs', 'hot-100']
    assert [r['title'] for r in found] == read
