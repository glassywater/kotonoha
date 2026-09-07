
import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QLabel

from kotonoha.app.intents import SearchLyrics, SelectLyrics
from kotonoha.config import Config
from kotonoha.display.models import LyricsDisplayStatus
from kotonoha.lyrics.artifact import LyricsArtifact
from kotonoha.lyrics.match import MatchConfidence
from kotonoha.lyrics.models import LyricsCacheState, LyricsOrigin
from kotonoha.lyrics.search import (
    LyricsSearchQuery,
    LyricsSearchResponse,
    LyricsSearchResult,
    LyricsSearchUnavailable,
)
from kotonoha.strings import Translator
from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog
from kotonoha.ui.settings.lyrics_status import LyricsStatusBand

_COLOURS = {c: "#888888" for c in MatchConfidence}

@pytest.mark.parametrize(
    ("language", "expected"),
    [("zh-Hans", "QQ 音乐, Cider"), ("zh-Hant", "QQ 音樂, Cider"), ("ja", "QQ 音楽, Cider")],
)
def test_status_line_lists_only_source_names(language: str, expected: str) -> None:
    """Localized source labels show the Cider name without a bundled qualifier."""
    # The reasons are sentences; joining them into the one-line footer is what
    # pushed the dialog wider than the screen and clipped the result count.
    from kotonoha.ui.settings.lyrics_search_model import format_unavailable_sources

    formatted = format_unavailable_sources(
        (
            LyricsSearchUnavailable("qqmusic", "search.unavailable.qqmusic"),
            LyricsSearchUnavailable("cider", "search.unavailable.cider"),
        ),
        Translator(language),
    )

    assert formatted == expected

def test_status_tooltip_translates_every_reason() -> None:
    """Unavailable reasons use localized descriptions and the plain Cider name."""
    # Reasons used to be English sentences pasted into a localized dialog.
    from kotonoha.ui.settings.lyrics_search_model import format_unavailable_details

    detail = format_unavailable_details(
        (
            LyricsSearchUnavailable("qqmusic", "search.unavailable.qqmusic"),
            LyricsSearchUnavailable("cider", "search.unavailable.cider"),
        ),
        Translator("zh-Hans"),
    )

    assert detail == "QQ 音乐：不支持按元数据搜索，需精确歌曲 ID\nCider：仅提供当前播放的曲目"
    # An untranslated key renders as itself, which is how the raw string would leak.
    assert "search.unavailable." not in detail

def test_search_dialog_enter_search_stays_open(qapp) -> None:
    dialog = LyricsSearchDialog(Config(), LyricsSearchQuery("Song", "Artist"))
    intents: list[object] = []
    dialog.intent_requested.connect(intents.append)
    dialog.show()
    qapp.processEvents()
    # Showing the window runs the query it was opened with; Enter runs it again.
    assert len(intents) == 1

    key_event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(dialog._title_edit, key_event)
    qapp.processEvents()

    assert dialog.isVisible()
    assert len(intents) == 2
    assert all(isinstance(intent, SearchLyrics) for intent in intents)
    dialog.close()

def test_search_dialog_meta_surface_uses_frosted_theme_background(qapp) -> None:
    from kotonoha.config import ThemeMode

    dialog = LyricsSearchDialog(
        Config(theme=ThemeMode.LIGHT),
        LyricsSearchQuery("Song", "Artist"),
    )
    dialog._frosted = True
    dialog._apply_surface_style()

    # The property, not the number: under frost the card has to stay translucent
    # enough for the blur behind it to read, and the exact value is tuned by eye.
    import re

    alphas = [
        int(match)
        for match in re.findall(r"background: rgba\(255, 255, 255, (\d+)\)", dialog.styleSheet())
    ]
    assert alphas, "the frosted card set no translucent background"
    assert min(alphas) < 128
    dialog.close()

def test_status_band_exposes_localized_source_and_acquisition_facts() -> None:
    band = LyricsStatusBand(
        LyricsDisplayStatus(
            playback_source="mpris",
            lyrics_source_id="netease",
            lyrics_source_name="netease",
            origin=LyricsOrigin.NETWORK,
            cache_state=LyricsCacheState.FROM_CACHE,
        ),
        Translator("zh-Hans"),
    )

    values = [label.text() for label in band.findChildren(QLabel) if label.objectName() == "metaValue"]

    assert values == ["网易云", "网络查询", "MPRIS", "来自缓存"]

def test_duration_reads_like_a_player_clock() -> None:
    from kotonoha.ui.settings.lyrics_search_model import format_duration

    translator = Translator("zh-Hant")
    # "243 秒" cannot be compared at a glance with the 4:03 a player shows.
    assert format_duration(243, translator) == "4:03"
    assert format_duration(59, translator) == "0:59"
    assert format_duration(3723, translator) == "1:02:03"
    assert format_duration(None, translator) == "未知"

def test_version_column_names_granularity_and_keeps_the_encoding() -> None:
    from kotonoha.lyrics.search import LyricsVersion
    from kotonoha.ui.settings.lyrics_search_model import format_version, format_version_detail

    translator = Translator("zh-Hant")
    # "LRC" and "YRC" say nothing to someone choosing between two candidates; what
    # separates them in use is whether the lyrics advance by line or by character.
    assert format_version(LyricsVersion("lrc"), translator) == "逐行歌詞"
    # A translated sheet is what most readers choose on, so the mark is short
    # enough to survive a narrow column rather than eliding away.
    assert format_version(LyricsVersion("yrc", True), translator) == "逐字歌詞 · 翻譯"
    assert format_version(LyricsVersion("krc"), translator) == "逐字歌詞"
    # An unknown encoding must not be claimed to be either one.
    assert format_version(LyricsVersion("mystery"), translator) == "歌詞"
    # The encoding itself stays reachable for anyone who needs it.
    assert format_version_detail(LyricsVersion("yrc"), translator) == "YRC"

def _result(title: str, duration: float | None, confidence: MatchConfidence) -> LyricsSearchResult:
    from kotonoha.lyrics.search import LyricsVersion

    artifact = LyricsArtifact("netease", title, title, "artist", "album", duration, {}, (), confidence)
    return LyricsSearchResult(artifact, LyricsVersion("lrc"))

def test_sorting_ranks_by_match_and_length_not_by_their_labels(qapp):
    from PyQt6.QtCore import Qt

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_model import (
        LyricsSearchSortModel,
        LyricsSearchTableModel,
    )

    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((
        _result("medium", 63.0, MatchConfidence.MEDIUM),
        _result("high", 600.0, MatchConfidence.HIGH),
        _result("none", None, MatchConfidence.NONE),
    ))
    proxy = LyricsSearchSortModel(model)

    # "High" sorts under "Medium" as text, and "10:00" under "1:03".
    proxy.sort(6, Qt.SortOrder.DescendingOrder)
    def _titles() -> list[str]:
        rows = [proxy.result_at(row) for row in range(proxy.rowCount())]
        assert all(row is not None for row in rows)
        return [row.artifact.title for row in rows if row is not None]

    assert _titles() == ["high", "medium", "none"]
    proxy.sort(4, Qt.SortOrder.AscendingOrder)
    # An unknown length sorts below every known one instead of counting as zero.
    assert _titles() == ["none", "medium", "high"]

def test_high_match_filter_hides_rows_without_discarding_them(qapp):
    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_model import (
        LyricsSearchSortModel,
        LyricsSearchTableModel,
    )

    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((
        _result("high", 100.0, MatchConfidence.HIGH),
        _result("medium", 100.0, MatchConfidence.MEDIUM),
    ))
    proxy = LyricsSearchSortModel(model)

    proxy.set_high_only(True)
    assert proxy.rowCount() == 1
    # The row a reader clicks must still resolve to the result behind it.
    kept = proxy.result_at(0)
    assert kept is not None
    assert kept.artifact.title == "high"
    proxy.set_high_only(False)
    assert proxy.rowCount() == 2

def test_apply_uses_the_row_on_screen_after_sorting(qapp):
    from kotonoha.config import Config
    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    query = LyricsSearchQuery("Song", "Artist")
    dialog = LyricsSearchDialog(Config(), query)
    intents = []
    dialog.intent_requested.connect(intents.append)
    # Arrival order puts the weakest candidate first; sorting moves it away.
    dialog.set_results(query, LyricsSearchResponse((
        _result("weak", 100.0, MatchConfidence.NONE),
        _result("strong", 100.0, MatchConfidence.HIGH),
    ), ()))

    dialog._table.selectRow(0)
    dialog._request_apply()

    # Selecting the first visible row must apply the result shown there, not the
    # first result the providers happened to return.
    applied = intents[-1]
    assert isinstance(applied, SelectLyrics)
    assert applied.result.artifact.title == "strong"

def test_title_column_lifts_the_qualifier_out_of_the_title(qapp):
    from PyQt6.QtCore import Qt

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_model import VERSION_LABEL_ROLE, LyricsSearchTableModel

    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((
        _result("Realize (TV Size)", 90.0, MatchConfidence.MEDIUM),
        _result("Realize", 243.0, MatchConfidence.HIGH),
    ))
    title = model.index(0, 1)

    # Rows of one song differ only in the qualifier, so repeating it inside the
    # title leaves every row reading the same; it is drawn beside the title.
    assert model.data(title, Qt.ItemDataRole.DisplayRole) == "Realize"
    assert model.data(title, VERSION_LABEL_ROLE) == ("TV Size",)
    assert model.data(model.index(1, 1), VERSION_LABEL_ROLE) == ()
    # Only the title column carries qualifiers; a chip beside an album would lie.
    assert model.data(model.index(0, 3), VERSION_LABEL_ROLE) == ()

def test_version_chip_is_drawn_after_the_title_not_over_it(qapp):
    from PyQt6.QtCore import QRect, Qt
    from PyQt6.QtGui import QFontMetrics, QPainter, QPixmap
    from PyQt6.QtWidgets import QStyleOptionViewItem

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.delegates import SelectionBarDelegate
    from kotonoha.ui.settings.lyrics_search_model import LyricsSearchTableModel

    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((_result("Realize (TV Size)", 90.0, MatchConfidence.MEDIUM),))
    canvas = QPixmap(400, 30)
    canvas.fill(Qt.GlobalColor.black)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 400, 30)
    option.font = qapp.font()
    painter = QPainter(canvas)
    SelectionBarDelegate("#4CC38A", "#4CC38A", "#FFFFFF", "#4CC38A").paint(painter, option, model.index(0, 1))
    painter.end()

    image = canvas.toImage()
    tinted = [
        x
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).green() > image.pixelColor(x, y).red() + 12
    ]

    # The delegate is handed an option whose text is not populated yet. Measuring
    # that instead of the model's string put every chip on top of the title.
    assert tinted, "no chip was drawn"
    assert min(tinted) >= QFontMetrics(option.font).horizontalAdvance("Realize")

def test_a_field_of_only_unrepresentable_characters_is_not_content(qapp):
    from PyQt6.QtCore import Qt

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.lyrics.search import LyricsSearchResult, LyricsVersion
    from kotonoha.ui.settings.lyrics_search_model import LyricsSearchTableModel

    # lrclib stores album names that are nothing but U+FFFD. Six replacement
    # glyphs in a column read as a rendering failure of ours.
    broken = LyricsArtifact("lrclib", "1", "t", "a", "�" * 6, 1.0, {}, (), MatchConfidence.HIGH)
    partly = LyricsArtifact("lrclib", "2", "t", "a", "小�王", 1.0, {}, (), MatchConfidence.HIGH)
    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((
        LyricsSearchResult(broken, LyricsVersion("lrc")),
        LyricsSearchResult(partly, LyricsVersion("lrc")),
    ))

    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == ""
    # A field that lost one character still carries the rest of its name.
    assert model.data(model.index(1, 3), Qt.ItemDataRole.DisplayRole) == "小�王"

def test_unavailable_sources_do_not_ride_along_with_the_result_count(qapp):
    from kotonoha.config import Config
    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.lyrics.search import LyricsSearchUnavailable
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    query = LyricsSearchQuery("Song", "Artist")
    dialog = LyricsSearchDialog(Config(), query)
    dialog.set_results(query, LyricsSearchResponse(
        (_result("Song", 100.0, MatchConfidence.HIGH),),
        (LyricsSearchUnavailable("cider", "search.unavailable.cider"),),
    ))

    # The count answers "how many did I get"; which sources were silent is a
    # footnote about the search, and it used to push the count off the window.
    assert "Cider" not in dialog._status.full_text()
    assert "Cider" in dialog._unavailable.full_text()

def test_a_track_without_an_artist_shows_no_placeholder_for_one(qapp):
    from PyQt6.QtWidgets import QLabel

    from kotonoha.config import Config
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    # YouTube reports a title and nothing else; a dash where a name goes reads as
    # a field that failed rather than one the player never sent.
    dialog = LyricsSearchDialog(Config(), LyricsSearchQuery("二零三", "", "", 226.0))
    shown = [label.text() for label in dialog.findChildren(QLabel) if label.objectName().startswith("track")]

    assert "二零三" in shown
    assert "-" not in shown

def test_an_open_window_follows_a_theme_change(qapp):
    from kotonoha.config import Config, ThemeMode
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    dialog = LyricsSearchDialog(Config(theme=ThemeMode.DARK), LyricsSearchQuery("Song", "Artist"))
    before = dialog.styleSheet()

    # Only the settings window restyled itself, so a search window left open kept
    # the palette it was born with while the window that changed it repainted.
    dialog.retheme(Config(theme=ThemeMode.LIGHT))

    assert dialog.styleSheet() != before
    assert dialog._theme == ThemeMode.LIGHT.value

def _mark_ink(label: QLabel) -> str:
    """Return the colour a rasterized mark actually drew with."""
    pixmap = label.pixmap()
    image = pixmap.toImage()
    drawn = [
        image.pixelColor(x, y)
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 128
    ]
    assert drawn, "the mark drew nothing"
    return drawn[len(drawn) // 2].name()

def test_sorting_tells_the_header_its_own_text_changed(qapp):
    from PyQt6.QtCore import Qt

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_model import (
        LyricsSearchSortModel,
        LyricsSearchTableModel,
    )

    model = LyricsSearchTableModel(Translator("en"), _COLOURS)
    model.set_results((
        _result("a", 63.0, MatchConfidence.MEDIUM),
        _result("b", 600.0, MatchConfidence.HIGH),
    ))
    proxy = LyricsSearchSortModel(model)
    proxy.sort(6, Qt.SortOrder.DescendingOrder)
    sections: list[int] = []
    proxy.headerDataChanged.connect(
        lambda orientation, first, last: sections.append(first)
        if orientation is Qt.Orientation.Horizontal
        else None
    )

    proxy.sort(4, Qt.SortOrder.AscendingOrder)

    # headerData() writes the direction mark into the sorted column's own name, so
    # both the column losing the mark and the one gaining it hold stale text. Qt
    # refetches header data only when told, and nothing else here tells it.
    assert sorted(sections) == [4, 6]

def test_the_marks_this_window_rasterized_follow_the_theme(qapp):
    from kotonoha.config import Config, ThemeMode
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    dialog = LyricsSearchDialog(Config(theme=ThemeMode.DARK), LyricsSearchQuery("Song", "Artist"))

    query, results = _mark_ink(dialog._query_mark), _mark_ink(dialog._results_mark)
    dialog.retheme(Config(theme=ThemeMode.LIGHT))

    # A stylesheet reapplies itself; a pixmap rendered from the palette does not.
    # These two were drawn once at construction and never again, so a window left
    # open through a theme change kept near-white glyphs on a white surface.
    assert _mark_ink(dialog._query_mark) != query
    assert _mark_ink(dialog._results_mark) != results

def test_the_search_window_leaf_follows_an_applied_accent(qapp):
    from kotonoha.config import Config
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    dialog = LyricsSearchDialog(
        Config(accent_start="#FF5EB5"), LyricsSearchQuery("Song", "Artist")
    )
    before = _mark_ink(dialog._logo_badge)

    dialog.retheme(Config(accent_start="#4FACFE"))

    # The badge is tinted at render time, so the window showed the previous accent
    # beside controls the new one had already restyled.
    assert _mark_ink(dialog._logo_badge) != before

def test_the_match_column_says_its_rating_in_colour(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor

    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_model import LyricsSearchTableModel

    colours = {MatchConfidence.HIGH: "#2E9E6B", MatchConfidence.MEDIUM: "#A8761B",
               MatchConfidence.NONE: "#6B7079"}
    model = LyricsSearchTableModel(Translator("en"), colours)
    model.set_results((
        _result("best", 100.0, MatchConfidence.HIGH),
        _result("rest", 100.0, MatchConfidence.NONE),
    ))

    # The rating is one word, so its colour carries the whole of the emphasis and
    # the table names no colour of its own.
    assert model.data(model.index(0, 6), Qt.ItemDataRole.ForegroundRole) == QColor("#2E9E6B")
    assert model.data(model.index(1, 6), Qt.ItemDataRole.ForegroundRole) == QColor("#6B7079")
    assert model.data(model.index(0, 1), Qt.ItemDataRole.ForegroundRole) is None

def test_opening_the_window_answers_the_query_it_was_opened_with(qapp):
    from kotonoha.config import Config
    from kotonoha.display.models import LyricsDisplayStatus
    from kotonoha.lyrics.match import MatchConfidence
    from kotonoha.ui.settings.lyrics_search_dialog import LyricsSearchDialog

    query = LyricsSearchQuery("Song", "Artist")
    status = LyricsDisplayStatus(lyrics_source_id="netease", lyrics_song_id="2")
    dialog = LyricsSearchDialog(Config(), query, status=status)
    intents: list[object] = []
    dialog.intent_requested.connect(intents.append)

    dialog.show()
    qapp.processEvents()

    # The fields are already filled from the track and the lyrics in use, so
    # asking the reader to press Search is a click that answers nothing new.
    assert [type(intent).__name__ for intent in intents] == ["SearchLyrics"]

    dialog.set_results(query, LyricsSearchResponse((
        _result("other", 100.0, MatchConfidence.HIGH),
        _result("2", 100.0, MatchConfidence.HIGH),
    ), ()))

    # The lyrics already playing are the row to compare the rest against, so the
    # window opens with them selected rather than with nothing.
    model = dialog._table.selectionModel()
    assert model is not None
    selected = model.selectedRows()
    assert selected
    chosen = dialog._sorted.result_at(selected[0].row())
    assert chosen is not None
    assert chosen.artifact.provider_song_id == "2"
    dialog.close()
