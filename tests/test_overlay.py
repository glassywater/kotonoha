import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QGuiApplication, QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

from kotonoha.config import Config
from kotonoha.overlay import LyricsOverlay
from kotonoha.platform.native import LayerShellController
from kotonoha.platform.overlay_contracts import Output, OverlayOperationResult, WindowRectangle
from kotonoha.state import LyricsState


class UnavailableController(LayerShellController):
    def __init__(self) -> None:
        super().__init__("", "wayland", "GNOME")


class LayerShellStub(LayerShellController):
    """Takes the layer-shell code path; every bridge call stays a no-op (no .so).

    The registry picks an adapter from the Qt platform name, which is "offscreen"
    under test, so an overlay built with this stub is given the ordinary-window
    adapter. Tests that exercise the layer-shell paths use `layer_shell_platform`
    to put the real adapter in place.
    """

    def __init__(self) -> None:
        super().__init__("", "wayland", "KDE")

    @property
    def available(self) -> bool:
        return True


def layer_shell_platform(overlay):
    """Give the overlay the Layer Shell adapter the registry cannot select offscreen."""
    from kotonoha.platform.layer_shell import LayerShellPlatform

    overlay._platform = LayerShellPlatform(overlay._host, overlay._controller)
    return overlay._platform


class FakeScreen:
    def __init__(self, name: str, x: int, y: int, width: int, height: int) -> None:
        self._name = name
        self._geometry = QRect(x, y, width, height)

    def name(self) -> str:
        return self._name

    def geometry(self) -> QRect:
        return self._geometry


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_fixed_panel_pins_pill_width_independent_of_text(qapp):
    overlay = LyricsOverlay(
        LyricsState(),
        Config(panel_width_mode="fixed", panel_width=680),
        UnavailableController(),
    )
    overlay.apply_config(overlay._config)
    # The container is pinned to (about) the configured width, so it does not grow
    # or shrink with the line length.
    assert overlay._container.maximumWidth() <= 680
    assert overlay._container.minimumWidth() == overlay._container.maximumWidth()
    # Fit mode releases the pin so the pill hugs its content again.
    overlay.apply_config(Config(panel_width_mode="fit"))
    assert overlay._container.maximumWidth() > 5000
    overlay.deleteLater()
    qapp.processEvents()


def test_font_fallback_chain_keeps_cjk_after_a_latin_family(qapp):
    overlay = LyricsOverlay(LyricsState(), Config(font_family="Inter"), UnavailableController())
    families = overlay._font_families()
    assert families[0] == "Inter"  # the chosen family leads
    assert any("CJK" in name for name in families)  # CJK fallback still present
    overlay.deleteLater()
    qapp.processEvents()


def test_fontconfig_prefer_rules_prefix_builtin_fallback(qapp):
    """fontconfig's own sorted fallback chain (what crossfont calls font_sort)
    should lead the built-in CJK safety net, so a user <alias>/<prefer> rule
    (e.g. "CaskaydiaCove Nerd Font Mono" -> "霞鹜文楷 TC") is honoured instead of
    being shadowed by the hardcoded list."""
    from kotonoha.overlay import _FALLBACK_FAMILIES, _fontconfig_fallback_families

    _fontconfig_fallback_families.cache_clear()
    # Simulate fc-match -s: the chosen family first, then the user's preferred
    # LXGW WenKai, then ordinary faces — before the built-in CJK chain would start.
    fake_out = (
        "CaskaydiaCove Nerd Font Mono\n"
        "霞鹜文楷 TC,LXGW WenKai TC\n"
        "Noto Sans\n"
        "DejaVu Sans\n"
    )
    with patch("kotonoha.overlay.subprocess.run") as run:
        run.return_value.stdout = fake_out
        run.return_value.returncode = 0
        overlay = LyricsOverlay(
            LyricsState(), Config(font_family="CaskaydiaCove Nerd Font Mono"), UnavailableController()
        )
        families = overlay._font_families()

    run.assert_called_once()
    assert families[0] == "CaskaydiaCove Nerd Font Mono"
    # The user's fontconfig <prefer> (LXGW) must be picked ahead of the built-in CJK net.
    assert families.index("霞鹜文楷 TC") < min(
        i for i, n in enumerate(families) if n in _FALLBACK_FAMILIES
    )
    # Only the canonical family name of each fc line is used, but the built-in
    # safety net is still appended so tofu is avoided when fontconfig can't help.
    assert "Noto Sans CJK SC" in families
    assert "Noto Sans" in families
    assert "LXGW WenKai TC" not in families  # an alias of 霞鹜文楷 TC, not a separate family
    overlay.deleteLater()
    qapp.processEvents()
    _fontconfig_fallback_families.cache_clear()


def test_fontconfig_unavailable_falls_back_to_builtin_chain(qapp):
    """If fc-match is missing/fails, the built-in CJK chain still prevents tofu."""
    from kotonoha.overlay import _fontconfig_fallback_families

    _fontconfig_fallback_families.cache_clear()
    with patch("kotonoha.overlay.subprocess.run", side_effect=OSError("no fc-match")):
        overlay = LyricsOverlay(LyricsState(), Config(font_family="Inter"), UnavailableController())
        families = overlay._font_families()
    assert families[0] == "Inter"
    assert any("CJK" in name for name in families)
    overlay.deleteLater()
    qapp.processEvents()
    _fontconfig_fallback_families.cache_clear()


def test_idle_shows_default_text_so_the_panel_is_not_empty(qapp):
    from kotonoha.model import EMPTY_SNAPSHOT

    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._on_snapshot(EMPTY_SNAPSHOT)  # nothing playing
    assert overlay._current.text  # a default line is shown, not a blank box
    assert "♪" in overlay._current.text
    overlay.deleteLater()
    qapp.processEvents()


def test_effects_apply_to_current_line_only_and_paint_safely(qapp):
    from kotonoha.model import LyricLine, LyricsSnapshot, LyricWord

    overlay = LyricsOverlay(
        LyricsState(),
        Config(fx_glow=True, fx_word_pop=True, fx_intensity="expressive"),
        UnavailableController(),
    )
    # Effects land on the main line; the translation stays plain.
    assert overlay._current._glow is True and overlay._current._word_pop is True
    assert overlay._translation._glow is False and overlay._translation._word_pop is False
    # A word-timed line paints (glow + pop path) without raising.
    line = LyricLine(
        index=1, id="c", start=0.0, end=6.0, text="あの日の 空へ", translation="",
        words=(LyricWord(0.0, 3.0, "あの日の"), LyricWord(3.0, 6.0, "空へ")),
    )
    overlay._on_snapshot(LyricsSnapshot(found=True, current=line, current_time=2.0, is_playing=True, timing="Word"))
    overlay._current.set_media_time(2.0)
    overlay._current.grab()  # force a paint pass through the effect code
    overlay.deleteLater()
    qapp.processEvents()


def test_current_line_only_hides_context_and_keeps_current_translation(qapp):
    from kotonoha.model import LyricLine, LyricsSnapshot

    previous = LyricLine(0, "p", 0.0, 2.0, "previous", "", ())
    current = LyricLine(1, "c", 2.0, 4.0, "current", "译文", ())
    next_line = LyricLine(2, "n", 4.0, 6.0, "next", "", ())
    snapshot = LyricsSnapshot(
        found=True,
        current=current,
        previous=previous,
        next=next_line,
        current_time=2.5,
        is_playing=True,
    )
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._on_snapshot(snapshot)
    full_height = overlay._band_height()
    assert overlay._prev_label.text() == "previous"
    assert overlay._next_label.text() == "next"
    assert overlay._translation.text == "译文"

    overlay.apply_config(Config(current_line_only=True))
    assert overlay._prev_label.isHidden() is True
    assert overlay._next_label.isHidden() is True
    assert overlay._current.text == "current"
    assert overlay._translation.text == "译文"
    assert overlay._band_height() < full_height

    overlay.apply_config(Config())
    assert overlay._prev_label.isHidden() is False
    assert overlay._next_label.isHidden() is False
    assert overlay._prev_label.text() == "previous"
    assert overlay._next_label.text() == "next"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_long_title_marquee_scrolls_then_holds(qapp):
    from kotonoha.karaoke_label import _MARQUEE_PAUSE_S, _MARQUEE_SPEED_PX_S, KaraokeLabel
    from kotonoha.model import LyricLine

    label = KaraokeLabel()
    label.resize(100, 40)
    label.set_line(LyricLine(0, "title", 0.0, 1e9, "A very very very long now-playing title", "", ()), False)
    overflow = 300.0  # pretend the text is 300px wider than the 100px label
    # The opening pause holds the title at the left...
    label.set_media_time(0.0)
    assert label._marquee_offset(overflow) == 0.0
    # ...then it glides partway...
    travel = overflow / _MARQUEE_SPEED_PX_S
    label.set_media_time(_MARQUEE_PAUSE_S + travel / 2.0)
    assert 0.0 < label._marquee_offset(overflow) < overflow
    # ...and reaches the far end fully scrolled.
    label.set_media_time(_MARQUEE_PAUSE_S + travel)
    assert label._marquee_offset(overflow) == overflow
    # Holds at the far end through the second pause...
    label.set_media_time(_MARQUEE_PAUSE_S + travel + _MARQUEE_PAUSE_S / 2.0)
    assert label._marquee_offset(overflow) == overflow
    # ...then glides back on the return leg (partway back, not at either end).
    label.set_media_time(2 * _MARQUEE_PAUSE_S + travel + travel / 2.0)
    assert 0.0 < label._marquee_offset(overflow) < overflow
    # No media clock yet (truly idle) -> no scrolling.
    label.set_media_time(None)
    assert label._marquee_offset(overflow) == 0.0
    assert label._is_title() is True
    label._total_w = 400.0
    label.grab()  # paints through the title/marquee branch without raising
    label.deleteLater()
    qapp.processEvents()


def test_transition_styles_paint_without_raising(qapp):
    from kotonoha.karaoke_label import KaraokeLabel
    from kotonoha.model import LyricLine

    label = KaraokeLabel()
    label.resize(200, 40)
    for style in ("fade", "rise", "slide", "zoom"):
        label.set_effects(glow=False, word_pop=False, intensity="subtle", animate=True, transition=style)
        assert label._transition == style
        label.set_line(LyricLine(0, style, 0.0, 3.0, "line", "", ()), False)
        label._reveal = 0.4  # mid-transition
        label.grab()
    label.deleteLater()
    qapp.processEvents()


def test_disabling_animations_reveals_lines_instantly(qapp):
    from kotonoha.karaoke_label import KaraokeLabel
    from kotonoha.model import LyricLine

    label = KaraokeLabel()
    label.set_effects(glow=False, word_pop=False, intensity="subtle", animate=False)
    label.set_line(LyricLine(0, "a", 0.0, 3.0, "x", "", ()), False)
    label.set_line(LyricLine(1, "b", 0.0, 3.0, "y", "", ()), False)  # a line change
    assert label._reveal == 1.0  # animations off -> shown immediately, no fade/rise
    label.deleteLater()
    qapp.processEvents()


def test_white_panel_flips_text_and_context_shadow_to_light(qapp):
    from PyQt6.QtWidgets import QGraphicsDropShadowEffect

    overlay = LyricsOverlay(LyricsState(), Config(panel_style="white"), UnavailableController())
    base, shadow, context_css = overlay._text_colors()
    assert base.lightness() < 90  # dark lyric text on the near-white slab
    assert shadow.lightness() > 160  # light halo, not a black smudge
    effect = overlay._prev_label.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect)
    assert effect.color().lightness() > 160  # context halo follows the panel too
    # A dark panel keeps light text with a dark halo.
    overlay.apply_config(Config(panel_style="pill"))
    assert overlay._text_colors()[0].lightness() > 160
    effect = overlay._prev_label.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect)
    assert effect.color().lightness() < 100
    overlay.deleteLater()
    qapp.processEvents()


def test_untimed_word_does_not_freeze_sweep(qapp):
    from PyQt6.QtGui import QFont

    from kotonoha.karaoke_label import KaraokeLabel
    from kotonoha.model import LyricLine, LyricWord

    label = KaraokeLabel()
    label.set_style(QFont(), "#FF4FA3", "#FF8FCB", "#FF6EC7")
    line = LyricLine(
        index=0, id="L", start=0.0, end=3.0, text="? word", translation="",
        words=(LyricWord(None, None, "?"), LyricWord(1.0, 2.0, "word")),
    )
    label.set_line(line, True)
    label.set_media_time(1.5)  # halfway through the *timed* word

    sweep_x, active = label._compute_sweep(0.0, label._total_w)

    # Before the fix, the leading untimed word froze the sweep at text_left (0.0).
    assert sweep_x > 0.0
    assert active is not None  # the timed word is actively sweeping
    label.deleteLater()
    qapp.processEvents()


def test_panel_visibility_follows_style_not_lock(qapp):
    # Locking must not force-hide the panel; that is the panel-style setting's job.
    locked_pill = LyricsOverlay(
        LyricsState(), Config(passthrough=True, panel_style="pill"), UnavailableController()
    )
    assert locked_pill._should_paint_panel() is True  # Glass panel stays while locked
    locked_text = LyricsOverlay(
        LyricsState(), Config(passthrough=True, panel_style="text"), UnavailableController()
    )
    assert locked_text._should_paint_panel() is False  # Text-only is immersive
    for overlay in (locked_pill, locked_text):
        overlay._render_timer.stop()
        overlay.deleteLater()
    qapp.processEvents()


def test_lyric_script_converts_displayed_line(qapp):
    from kotonoha.model import LyricLine, LyricWord

    line = LyricLine(0, "L", 0.0, 3.0, "简体字", translation="翻译", words=(LyricWord(0.0, 1.0, "简"),))
    converted = LyricsOverlay(
        LyricsState(), Config(lyrics_script="zh-Hant"), UnavailableController()
    )
    out = converted._convert_line(line)
    assert out is not None
    assert out.text == "簡體字"  # display converted to Traditional
    assert out.words[0].text == "簡"  # words converted too (for the karaoke sweep)
    off = LyricsOverlay(LyricsState(), Config(lyrics_script="off"), UnavailableController())
    assert off._convert_line(line) is line  # untouched when disabled
    for overlay in (converted, off):
        overlay._render_timer.stop()
        overlay.deleteLater()
    qapp.processEvents()


def test_accent_tinted_black_panel_uses_accent_hue(qapp):
    from PyQt6.QtGui import QColor

    overlay = LyricsOverlay(
        LyricsState(),
        Config(panel_style="pill", panel_accent_tint=True, accent_start="#FF4FA3"),
        UnavailableController(),
    )
    colour = overlay._panel_base_color()
    assert colour != QColor(15, 17, 22)  # not the flat near-black
    assert colour.red() > colour.blue()  # tinted toward the pink accent
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_frosted_panel_paints_and_keeps_window_opaque(qapp):
    overlay = LyricsOverlay(
        LyricsState(), Config(panel_style="frost", opacity=0.6), UnavailableController()
    )
    assert overlay._should_paint_panel() is True  # frosted panel is drawn
    assert overlay.windowOpacity() == pytest.approx(1.0, abs=0.01)  # text stays crisp
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_panel_alpha_tracks_opacity(qapp):
    overlay = LyricsOverlay(
        LyricsState(),
        Config(panel_style="pill", opacity=1.0),
        UnavailableController(),
    )
    assert overlay._panel_alpha() == 255  # 100% -> solid, not the old 150 cap
    overlay.apply_config(Config(panel_style="pill", opacity=0.3))
    assert overlay._panel_alpha() == round(255 * 0.3)
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_window_stays_opaque_and_frost_uses_its_own_opacity(qapp):
    # Opacity is the panel's own fill (window always opaque so text stays crisp),
    # and the black and frosted panels keep independent opacity values.
    black = LyricsOverlay(
        LyricsState(), Config(panel_style="pill", opacity=0.0, frost_opacity=0.6), UnavailableController()
    )
    assert black.windowOpacity() == pytest.approx(1.0, abs=0.01)
    assert black._panel_alpha() == 0  # black panel can go fully transparent
    frost = LyricsOverlay(
        LyricsState(), Config(panel_style="frost", opacity=0.0, frost_opacity=0.6), UnavailableController()
    )
    assert frost.windowOpacity() == pytest.approx(1.0, abs=0.01)
    assert frost._panel_alpha() == round(255 * 0.6)  # frost uses frost_opacity, not opacity
    for overlay in (black, frost):
        overlay._render_timer.stop()
        overlay.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("event_type", (QEvent.Type.Move, QEvent.Type.Resize))
def test_container_geometry_change_schedules_surface_repaint(qapp, event_type):
    overlay = LyricsOverlay(
        LyricsState(),
        Config(passthrough=False, panel_style="pill"),
        UnavailableController(),
    )

    with patch.object(overlay, "update") as update:
        overlay.eventFilter(overlay._container, QEvent(event_type))

    update.assert_called_once_with()
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_drag_crosses_output_without_recreating_the_layer_surface(qapp):
    source = FakeScreen("HDMI-A-1", 0, 0, 2048, 1152)
    target = FakeScreen("DP-1", 2048, 0, 1920, 1080)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._active_screen = source
    overlay._layer_pos = QPoint(1900, 100)
    overlay._dragging = True
    overlay._drag_local = QPoint(20, 20)

    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(200, 20),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    with patch.object(QGuiApplication, "screens", return_value=[source, target]):
        overlay.mouseMoveEvent(event)

    assert overlay._layer_pos == QPoint(2080, 100)
    assert overlay._active_screen is source
    assert LyricsOverlay._screen_for_global_point(QPoint(2280, 120), [source, target], source) is target
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_click_without_motion_does_not_persist_a_new_horizontal_offset(qapp):
    overlay = LyricsOverlay(
        LyricsState(), Config(margin_x=37), UnavailableController()
    )
    emitted: list[tuple[int, int, str]] = []
    overlay.position_changed.connect(lambda edge, margin_x, name: emitted.append((edge, margin_x, name)))
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(10, 10),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    overlay.mousePressEvent(press)
    overlay.mouseReleaseEvent(release)

    assert overlay._config.margin_x == 37
    assert emitted == []
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_offset_buttons_shift_sweep_and_hide_with_lock(qapp):
    from kotonoha.model import LyricLine, LyricsSnapshot

    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    snapshot = LyricsSnapshot(
        found=True, title="Song", artist="Artist", duration_s=180.0,
        current=LyricLine(0, "line", 0.0, 4.0, "line", "", ()), current_time=1.0, is_playing=True,
    )
    overlay._on_snapshot(snapshot)
    overlay._clock.sync(1.0, True)
    _freeze_media_clock(overlay, 1.0)
    overlay._render_tick()
    before = overlay._current._media_time
    assert before is not None
    overlay._earlier_btn.click()
    assert overlay._config.track_offsets[overlay._track_key] == 50
    assert overlay._current._media_time == pytest.approx(before + 0.05)
    assert overlay._current.text == "Sync offset: +50 ms"
    overlay.set_passthrough(True)
    assert overlay._earlier_btn.isHidden() is True
    assert overlay._later_btn.isHidden() is True
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_track_without_offset_uses_global_lead(qapp):
    from kotonoha.model import LyricLine, LyricsSnapshot

    overlay = LyricsOverlay(LyricsState(), Config(lead_ms=120), UnavailableController())
    overlay._clock.sync(1.0, True)
    _freeze_media_clock(overlay, 1.0)
    overlay._on_snapshot(LyricsSnapshot(
        found=True, title="Song", artist="Artist",
        current=LyricLine(0, "line", 0.0, 4.0, "line", "", ()), current_time=1.0, is_playing=True,
    ))
    overlay._render_tick()
    assert overlay._current._media_time == pytest.approx(1.12)
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_released_cross_output_keeps_margin_x_and_records_output(qapp):
    source = FakeScreen("HDMI-A-1", 0, 0, 2048, 1152)
    target = FakeScreen("DP-1", 2048, 0, 1920, 1080)
    overlay = LyricsOverlay(
        LyricsState(), Config(margin_x=37), UnavailableController()
    )
    overlay._active_screen = source
    overlay._layer_pos = QPoint(2100, 100)  # global x = 2100, on DP-1
    overlay._drag_local = QPoint(100, 40)
    emitted: list[tuple[int, int, str]] = []
    overlay.position_changed.connect(lambda edge, margin_x, name: emitted.append((edge, margin_x, name)))

    with patch.object(QGuiApplication, "screens", return_value=[source, target]), patch.object(
        overlay, "_window_size", return_value=(500, 140)
    ):
        overlay._commit_drag_position(QPoint(100, 40))

    assert overlay._config.margin_x == -658
    assert overlay._config.screen_name == "DP-1"
    assert overlay._layer_pos == QPoint(52, 100)
    assert emitted == [(100, -658, "DP-1")]
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_release_at_horizontal_edge_keeps_the_configured_offset(qapp):
    screen = FakeScreen("HDMI-A-1", 0, 0, 2048, 1152)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._active_screen = screen
    overlay._layer_pos = QPoint(-1100, 100)
    overlay._drag_local = QPoint(20, 40)

    with patch.object(QGuiApplication, "screens", return_value=[screen]), patch.object(
        overlay, "_window_size", return_value=(1100, 140)
    ):
        overlay._commit_drag_position(QPoint(20, 40))

    assert overlay._layer_pos == QPoint(-1020, 100)
    assert overlay._config.margin_x == -1494
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_drag_keeps_the_original_vertical_bottom_range(qapp):
    screen = FakeScreen("HDMI-A-1", 0, 0, 2048, 1152)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._active_screen = screen
    overlay._layer_pos = QPoint(400, 1000)
    overlay._dragging = True
    overlay._drag_local = QPoint(20, 20)

    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(20, 200),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    with patch.object(QGuiApplication, "screens", return_value=[screen]):
        overlay.mouseMoveEvent(event)

    assert overlay._layer_pos == QPoint(400, 1180)
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_visible_area_ratio_measures_the_on_screen_fraction(qapp):
    from kotonoha.overlay import MIN_VISIBLE_FRACTION

    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    screen = FakeScreen("DP-2", 1080, 480, 2560, 1440)

    # The stale saved position from a changed monitor layout: only a sliver
    # (80x60) of a 1008x221 surface remains inside the 2560x1440 frame.
    off = overlay._visible_area_ratio(QPoint(2480, 1380), 1008, 221, screen)
    assert off < MIN_VISIBLE_FRACTION
    # A centred placement is fully on-screen.
    on = overlay._visible_area_ratio(QPoint((2560 - 1008) // 2, 64), 1008, 221, screen)
    assert on > 0.99
def test_placeholder_screen_is_never_adopted_while_every_output_is_gone(qapp):
    # Qt stands in a placeholder screen with empty geometry between the last output
    # leaving and the first one returning; binding to it sizes the surface to 0x0.
    placeholder = FakeScreen("", 0, 0, 0, 0)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())

    with patch.object(QGuiApplication, "screens", return_value=[placeholder]), patch.object(
        overlay, "screen", return_value=placeholder
    ), patch.object(QApplication, "primaryScreen", return_value=placeholder):
        assert overlay._target_screen() is None

    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_geometry_recenters_a_nearly_off_screen_saved_position(qapp):
    from kotonoha.overlay import RECENTER_EDGE_MARGIN

    screen = FakeScreen("DP-2", 1080, 480, 2560, 1440)
    # Record the output + geometry in config so the drag park (stored as a large
    # off-screen margin) is honoured as same_output (allow_partial), leaving it
    # off-screen for the recenter guard to snap back.
    overlay = LyricsOverlay(
        LyricsState(),
        Config(
            anchor_top=True, margin_edge=1380, margin_x=1704, panel_width_mode="fixed", panel_width=960,
            screen_name="DP-2", screen_width=2560, screen_height=1440,
        ),
        UnavailableController(),
    )
    with patch.object(overlay, "_target_screen", return_value=screen), patch.object(
        overlay, "_bind_widget_screen", lambda _s, s=None: None
    ):
        overlay._apply_window_geometry(reset_position=True)

    assert overlay._layer_pos == QPoint((2560 - 1008) // 2, RECENTER_EDGE_MARGIN)
def test_a_locked_overlay_is_click_through_on_the_fallback_platform(qapp):
    # The ordinary-window path only positioned the window, so set_input_region was
    # never called and a config with passthrough on stayed clickable — the locked
    # overlay swallowed the pointer.
    overlay = LyricsOverlay(LyricsState(), Config(passthrough=True), UnavailableController())
    regions: list[object] = []
    with patch.object(overlay._platform, "set_input_region", lambda region: regions.append(region) or _ok()):
        overlay.activate_layer_shell()

    assert regions == [None], "the locked overlay never asked for a click-through region"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_an_activated_surface_reports_a_rejected_placement(qapp, caplog):
    # Activation succeeding says the surface is mapped, not that the saved position
    # was applied. Dropping the placement result left the overlay at the compositor's
    # default anchor and said nothing about why it was not where the user put it.
    overlay = LyricsOverlay(LyricsState(), Config(), LayerShellStub())
    layer_shell_platform(overlay)
    with patch.object(
        overlay._platform, "move_to", lambda position: OverlayOperationResult.failure("margins rejected")
    ), caplog.at_level("WARNING"):
        activated = overlay.activate_layer_shell()

    assert activated is True, "the surface is mapped; only its position was refused"
    assert "margins rejected" in caplog.text
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_a_failed_activation_falls_back_and_says_why(qapp, caplog):
    # The capability is there but activation fails — a missing handle, or the bridge
    # raising. Falling through silently left an already-mapped window unpositioned
    # with no input region and no diagnostic.
    overlay = LyricsOverlay(LyricsState(), Config(), LayerShellStub())
    layer_shell_platform(overlay)
    positioned: list[bool] = []
    with patch.object(
        overlay._platform, "activate", lambda: OverlayOperationResult.failure("no window handle")
    ), patch.object(overlay, "_fallback_position", lambda: positioned.append(True)), caplog.at_level("WARNING"):
        overlay.activate_layer_shell()

    assert positioned == [True], "activation failed and nothing positioned the window"
    assert "no window handle" in caplog.text
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def _freeze_media_clock(overlay, value: float) -> None:
    """Pin the media clock so an offset assertion is not racing wall time.

    MediaClock.now is a method and the value it returns comes from wall time, so
    there is no seam to inject; replacing the attribute is the only way to freeze
    it, and ty rejects assigning over a method. The suppression is confined here
    rather than repeated at each call site, and it disappears if MediaClock ever
    takes an injected time source.
    """
    overlay._clock.now = lambda: value  # ty: ignore[invalid-assignment]


def _ok():
    from kotonoha.platform.overlay_contracts import OverlayOperationResult

    return OverlayOperationResult.success()


def test_the_qt_host_shapes_and_clears_the_real_input_mask(qapp):
    # The production host has to implement the shaping the contract describes, or
    # the ordinary-window path calls a method nothing provides.
    from kotonoha.platform.overlay_contracts import WindowRectangle
    from kotonoha.platform.qt_host import QtWindowHost

    widget = QWidget()
    host = QtWindowHost(widget)

    host.set_input_mask(WindowRectangle(3, 4, 20, 10))
    assert widget.mask().boundingRect() == QRect(3, 4, 20, 10)

    host.clear_input_mask()
    assert widget.mask().isEmpty()
    widget.deleteLater()
    qapp.processEvents()


def test_the_qt_host_implements_every_method_the_contract_names():
    # The host used to inherit the Protocol, so a method it forgot became a silent
    # no-op: the adapter reported success while nothing happened.
    from kotonoha.platform.overlay_contracts import WindowHost
    from kotonoha.platform.qt_host import QtWindowHost

    required = {name for name in vars(WindowHost) if not name.startswith("_")}
    missing = {name for name in required if not callable(getattr(QtWindowHost, name, None))}
    assert not missing, f"QtWindowHost does not implement: {sorted(missing)}"


def test_a_failed_activation_positions_as_an_ordinary_window(qapp):
    # The Layer Shell adapter is still in place when activation fails, so asking it
    # to move set a native anchor on a surface that was never promoted — no real
    # fallback happened.
    overlay = LyricsOverlay(LyricsState(), Config(), LayerShellStub())
    platform = layer_shell_platform(overlay)
    moves: list[tuple[str, object]] = []
    with patch.object(platform, "activate", lambda: OverlayOperationResult.failure("no handle")), patch.object(
        platform, "move_to", lambda position: moves.append(("anchor", position)) or OverlayOperationResult.success()
    ), patch.object(overlay._host, "move_window", lambda position: moves.append(("host", position))):
        overlay.activate_layer_shell()

    assert [kind for kind, _ in moves] == ["host"], f"positioned through the wrong path: {moves}"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_a_drag_is_not_persisted_where_the_window_cannot_be_placed(qapp):
    # Wayland without Layer Shell ignores a client-side move, so saving the dragged
    # position would leave the config describing somewhere the window never went.
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    committed: list[object] = []
    unplaceable = replace(
        overlay._platform.capabilities, client_positioning=False, client_positioning_reason="no"
    )
    with patch.object(
        type(overlay._platform), "capabilities", property(lambda self: unplaceable)
    ), patch.object(overlay, "_commit_drag_position", lambda cursor=None: committed.append(cursor)):
        overlay._dragging = True
        overlay._drag_moved = True
        overlay.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(10, 10),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    assert committed == [], "a position the window never took was saved"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_a_drag_whose_update_failed_is_not_persisted(qapp):
    # The strategy fails when the window handle is gone or the native call raises.
    # Discarding that result meant the release still saved the new position while
    # the visible surface stayed where it was.
    overlay = LyricsOverlay(LyricsState(), Config(), LayerShellStub())
    committed: list[object] = []

    def _fail(local, glob):
        from kotonoha.platform.overlay_contracts import OverlayOperationResult

        return OverlayOperationResult.failure("no window handle")

    with patch.object(overlay._platform, "update_drag", _fail), patch.object(
        overlay, "_commit_drag_position", lambda cursor=None: committed.append(cursor)
    ), patch.object(overlay, "_target_screen", return_value=qapp.primaryScreen()):
        overlay.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(10, 10),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        overlay.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(40, 10),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        overlay.mouseReleaseEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(40, 10),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    assert committed == [], "a drag that never took effect was saved"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_saved_position_from_a_larger_output_stays_fully_visible(qapp):
    # A margin dragged on a wide output must not push the panel off a smaller one.
    # The partial bounds a drag uses would keep only 80x60 px of it on screen.
    screen = FakeScreen("HDMI-A-1", 0, 0, 4096, 1152)
    overlay = LyricsOverlay(
        LyricsState(), Config(margin_x=2518, margin_edge=1092, anchor_top=True), UnavailableController()
    )
    overlay._active_screen = screen

    with patch.object(QGuiApplication, "screens", return_value=[screen]), patch.object(
        overlay, "_window_size", return_value=(1100, 170)
    ):
        pos = overlay._compute_layer_pos(1100, 170)

    assert 0 <= pos.x() <= 4096 - 1100
    assert 0 <= pos.y() <= 1152 - 170
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_a_parked_position_survives_the_next_geometry_pass(qapp):
    # Releasing at the right-hand edge is stored as a large negative x, because the
    # surface is wider than the visible pill. Re-applying the geometry — a settings
    # apply, a re-show, a restart — must leave the panel where it was released;
    # clamping it fully on screen there teleports it, which is what a user sees as
    # the panel flying away after a drag.
    screen = FakeScreen("HDMI-A-1", 0, 0, 2048, 1152)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    overlay._active_screen = screen
    overlay._layer_pos = QPoint(-1100, 100)
    overlay._drag_local = QPoint(20, 40)

    with patch.object(QGuiApplication, "screens", return_value=[screen]), patch.object(
        overlay, "_window_size", return_value=(1100, 140)
    ), patch.object(overlay._platform, "move_to_output"):
        overlay._commit_drag_position(QPoint(20, 40))
        parked = overlay._layer_pos
        reloaded = overlay._compute_layer_pos(1100, 140)

    assert overlay._config.screen_width == 2048
    assert overlay._config.screen_height == 1152
    assert reloaded == parked, f"the panel jumped from {parked} to {reloaded}"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_a_parked_position_is_not_trusted_on_a_different_output_of_the_same_size(qapp):
    # Two monitors of the same model have the same geometry, so size alone cannot
    # say the saved offset was measured here. Honouring it on the other one puts
    # the panel off screen, which is the failure the clamp exists to prevent.
    other = FakeScreen("DP-1", 0, 0, 1920, 1080)
    overlay = LyricsOverlay(
        LyricsState(),
        Config(
            screen_name="HDMI-A-1",
            screen_width=1920,
            screen_height=1080,
            margin_x=-1800,
            margin_edge=100,
        ),
        UnavailableController(),
    )
    overlay._active_screen = other

    with patch.object(QGuiApplication, "screens", return_value=[other]), patch.object(
        overlay, "_window_size", return_value=(1100, 140)
    ):
        pos = overlay._compute_layer_pos(1100, 140)

    assert 0 <= pos.x() <= 1920 - 1100, f"panel parked off screen at x={pos.x()}"
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()


def test_geometry_keeps_a_fully_visible_position(qapp):
    screen = FakeScreen("DP-2", 1080, 480, 2560, 1440)
    overlay = LyricsOverlay(
        LyricsState(),
        Config(anchor_top=True, margin_edge=20, margin_x=0, panel_width_mode="fixed", panel_width=960),
        UnavailableController(),
    )
    with patch.object(overlay, "_target_screen", return_value=screen), patch.object(
        overlay, "_bind_widget_screen", lambda _s, s=None: None
    ):
        overlay._apply_window_geometry(reset_position=True)

    assert overlay._layer_pos == QPoint((2560 - 1008) // 2, 20)
    overlay.deleteLater()
    qapp.processEvents()



def test_center_on_screen_places_overlay_in_the_primary_middle(qapp):
    primary = FakeScreen("DP-1", 0, 0, 1920, 1080)
    secondary = FakeScreen("HDMI-A-1", 0, 0, 2560, 1440)
    overlay = LyricsOverlay(
        LyricsState(), Config(anchor_top=True), UnavailableController()
    )
    # Simulate the overlay currently bound to the secondary screen's output.
    overlay._active_screen = secondary
    overlay._layer_pos = QPoint(1500, 900)  # wherever it was dragged
    expected = Output("DP-1", WindowRectangle(0, 0, 1920, 1080))
    with patch.object(QApplication, "primaryScreen", return_value=primary), patch.object(
        overlay, "_window_size", return_value=(500, 140)
    ), patch.object(
        overlay._platform, "move_to_output", return_value=OverlayOperationResult.success()
    ) as move_to_output, patch.object(
        overlay, "_bind_widget_screen", lambda _s, s=None: None
    ):
        overlay.center_on_screen()

    # Centred on the PRIMARY screen (not the one it was on).
    assert overlay._layer_pos == QPoint((1920 - 500) // 2, (1080 - 140) // 2)
    assert overlay._config.screen_name == "DP-1"
    assert overlay._config.margin_x == 0
    assert move_to_output.call_args_list == [((expected,), {})]  # rebind onto the primary
    overlay.deleteLater()
    qapp.processEvents()


def test_center_on_screen_when_already_on_primary_repositions_in_place(qapp):
    primary = FakeScreen("DP-1", 0, 0, 1920, 1080)
    overlay = LyricsOverlay(
        LyricsState(), Config(anchor_top=True), UnavailableController()
    )
    overlay._active_screen = primary  # already bound to the primary output
    with patch.object(QApplication, "primaryScreen", return_value=primary), patch.object(
        overlay, "_window_size", return_value=(400, 120)
    ), patch.object(overlay._platform, "move_to_output") as move_to_output:
        overlay.center_on_screen()

    assert overlay._layer_pos == QPoint((1920 - 400) // 2, (1080 - 120) // 2)
    move_to_output.assert_not_called()
    overlay.deleteLater()
    qapp.processEvents()
def test_a_returning_output_is_matched_by_name_not_by_its_old_mode(qapp):
    # The geometry recorded when the screen appeared can be a mode Qt has since
    # replaced: screenAdded and geometryChanged are separate signals, and a mode
    # change does not fire the former again. Full Output equality therefore
    # rejected the very output the rebuild was waiting for, and the surface that
    # had already been destroyed was never rebuilt.
    live = FakeScreen("DP-1", 0, 0, 3840, 2160)
    overlay = LyricsOverlay(LyricsState(), Config(), UnavailableController())
    stale = Output("DP-1", WindowRectangle(0, 0, 1920, 1080))

    with patch.object(QGuiApplication, "screens", return_value=[live]), patch.object(
        overlay, "_bind_widget_screen"
    ), patch.object(overlay, "activate_layer_shell", return_value=True), patch.object(overlay, "show"):
        rebuilt = overlay._restore_output(stale)

    assert rebuilt is True, "the returning output was rejected for changing mode"
    assert overlay._active_screen is live
    overlay._render_timer.stop()
    overlay.deleteLater()
    qapp.processEvents()
