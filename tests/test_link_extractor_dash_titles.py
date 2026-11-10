import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import link_extractor as le


class TestDashSeparatorHandling:
    """An em-dash (—) separates "Artist — Title" as well as " - " does:
    "Rauf & Faik — детство (Official video)" is split into artist and title
    even when YouTube's own metadata shortens the duo to "Rauf", so the title
    does not carry the artist into every tool's post-download check.
    """

    def test_has_dash_separator_detects_em_and_en_dash(self):
        assert le._has_dash_separator('Rauf & Faik — детство') is True
        assert le._has_dash_separator('Rauf & Faik – детство') is True
        assert le._has_dash_separator('Rauf & Faik - детство') is True
        assert le._has_dash_separator('детство') is False
        # A bare hyphen inside a word (no surrounding spaces) is not a separator
        assert le._has_dash_separator('Re-Recorded') is False

    def test_normalize_dash_separators_converts_to_ascii_hyphen(self):
        assert le._normalize_dash_separators('Rauf & Faik — детство') == 'Rauf & Faik - детство'
        assert le._normalize_dash_separators('Rauf & Faik – детство') == 'Rauf & Faik - детство'
        assert le._normalize_dash_separators('Rauf & Faik - детство') == 'Rauf & Faik - детство'

    def test_songnamesplit_resolves_em_dash_title_once_normalized(self):
        # SongNameSplit itself raises NonStandardSongTitle for em/en-dash input
        # (verified directly against the library) — _try_songnamesplit must
        # normalize before calling it, or every non-Latin-convention title
        # silently fails to split.
        result = le._try_songnamesplit('Rauf & Faik — детство (Official video)')
        assert result is not None
        assert result['artist'] == 'Rauf & Faik'
        assert result['songname'] == 'детство'

    def test_songnamesplit_still_rejects_truly_nonstandard_titles(self):
        # Sanity check: normalization shouldn't make SongNameSplit accept
        # everything — a title with no separator at all should still fail.
        result = le._try_songnamesplit('детство')
        assert result is None
