"""A single lyric line rendered with a left-to-right karaoke sweep.

The "sung" portion is filled with a pink gradient and the rest stays a dim
white; the word currently being sung is brightened with the accent-sweep colour.
The sweep boundary is computed from per-word timing when available (stopping
mid-word), or from the line's overall progress otherwise. The widget is
repainted at ~60fps by the overlay as the media clock advances.

On a line change the text fades in and rises a few pixels (``reveal`` animated
property), driven by a QPropertyAnimation rather than a QGraphicsEffect so it
does not clash with the overlay's drop-shadow glow.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict, cast

from PyQt6 import QtCore
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .karaoke import line_progress, word_fill_fraction
from .model import LyricLine

UNSUNG_COLOR = QColor(255, 255, 255, 95)
SHADOW_COLOR = QColor(0, 0, 0, 170)
SHADOW_OFFSET = 1.5
REVEAL_RISE_PX = 9.0
REVEAL_DURATION_MS = 320
# Line-change transition styles (chosen in Settings). "rise" is the calm default;
# the others trade the small upward rise for a pure fade, a larger slide, or a
# gentle zoom. "none" is expressed by the fx_animate master switch being off.
_TRANSITIONS = ("fade", "rise", "slide", "zoom")

# A long now-playing title (line id "title", no karaoke sweep) scrolls back and
# forth so the whole name is legible instead of being clipped.
_MARQUEE_SPEED_PX_S = 42.0   # travel speed
_MARQUEE_PAUSE_S = 1.6       # hold at each end before reversing

# Effect strength per intensity: glow alpha (of the accent), glow radius (px), and
# the active-word brightening (QColor.lighter percent).
class _EffectConfig(TypedDict):
    glow_alpha: float
    glow_radius: float
    pop: int


_FX: dict[str, _EffectConfig] = {
    "subtle": {"glow_alpha": 0.22, "glow_radius": 2.0, "pop": 128},
    "expressive": {"glow_alpha": 0.42, "glow_radius": 3.0, "pop": 155},
}
# Unit ring of 8 directions; scaled by the glow radius to fake a cheap bloom.
_GLOW_OFFSETS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))

class _PyqtPropertyFactory(Protocol):
    def __call__(self, type_: object, *, fget: object, fset: object) -> Any: ...


pyqt_property = cast(_PyqtPropertyFactory, cast(Any, QtCore).pyqtProperty)



def _scale_alpha(color: QColor, factor: float) -> QColor:
    out = QColor(color)
    out.setAlpha(max(0, min(255, int(color.alpha() * factor))))
    return out


class KaraokeLabel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line: LyricLine | None = None
        self._word_mode = False
        self._media_time: float | None = None
        self._font = QFont()
        self._accent_start = QColor("#FF4FA3")
        self._accent_end = QColor("#FF8FCB")
        self._accent_sweep = QColor("#FF6EC7")
        # Base (unsung) and shadow colours — overridable so a light panel can use
        # dark text with a soft light halo instead of white text on a dark shadow.
        self._base_color = QColor(UNSUNG_COLOR)
        self._shadow_color = QColor(SHADOW_COLOR)
        # Effects (set per-label by the overlay from config; off for context/plain).
        self._glow = False
        self._word_pop = False
        self._intensity = "subtle"
        self._animate = True
        self._transition = "rise"
        self._reveal = 1.0
        self._anim: QPropertyAnimation | None = None
        # Cached text measurements (rebuilt only when font/line changes, never per
        # frame) so the 60fps sweep paint stays cheap.
        self._fm = QFontMetrics(self._font)
        self._word_widths: list[float] = []
        self._space_w = 0.0
        self._total_w = 0.0
        self._max_width = 0  # 0 = unlimited; else cap the width and scroll long lines
        # Furigana (auto 振り仮名 for kanji). Off by default; enabled explicitly via
        # set_style(furigana=True). When off, _furigana stays empty and painting is
        # byte-for-byte the original single-pass drawText.
        self._furigana_on = False
        self._furigana: tuple[Any, ...] = ()
        self._ruby_font = QFont(self._font)
        self._fm_ruby = QFontMetrics(self._ruby_font)
        self._furigana_top: float = 0.0  # extra headroom height (px) for the ruby line
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # --- configuration ---

    def set_style(
        self,
        font: QFont,
        accent_start: str,
        accent_end: str,
        accent_sweep: str,
        base_color: QColor | None = None,
        shadow_color: QColor | None = None,
        furigana: bool = False,
    ) -> None:
        self._font = font
        self._furigana_on = furigana
        self._accent_start = QColor(accent_start)
        self._accent_end = QColor(accent_end)
        self._accent_sweep = QColor(accent_sweep)
        if base_color is not None:
            self._base_color = QColor(base_color)
        if shadow_color is not None:
            self._shadow_color = QColor(shadow_color)
        self._fm = QFontMetrics(self._font)
        # 注音小字体:继承主字体族/风格,仅缩小字号。
        self._ruby_font = QFont(self._font)
        self._ruby_font.setPixelSize(max(1, int(self._font.pixelSize() * 0.55)))
        self._fm_ruby = QFontMetrics(self._ruby_font)
        self._rebuild_layout()
        self.updateGeometry()
        self.update()

    def set_effects(
        self, *, glow: bool, word_pop: bool, intensity: str, animate: bool, transition: str = "rise"
    ) -> None:
        self._glow = glow
        self._word_pop = word_pop
        self._intensity = intensity if intensity in _FX else "subtle"
        self._animate = animate
        self._transition = transition if transition in _TRANSITIONS else "rise"
        self.update()

    def set_line(self, line: LyricLine | None, word_mode: bool) -> None:
        prev_id = self._line.id if self._line else None
        new_id = line.id if line else None
        self._line = line
        self._word_mode = word_mode and line is not None and line.has_word_timing
        self._rebuild_layout()
        if new_id is not None and new_id != prev_id:
            self._start_reveal()
        self.updateGeometry()
        self.update()

    def _rebuild_layout(self) -> None:
        text = self.text
        self._total_w = self._fm.horizontalAdvance(text) if text else 0.0
        self._space_w = self._fm.horizontalAdvance(" ")
        self._word_widths = (
            [self._fm.horizontalAdvance(w.text) for w in self._line.words] if self._line else []
        )
        # Furigana: analyze the displayed kanji line once when enabled. Geometry per
        # segment (base x-center) is captured in _analyze_furigana; painting stays
        # per-frame cheap. When disabled, _furigana stays empty and rendering is the
        # original single-pass drawText.
        if self._furigana_on and text:
            self._analyze_furigana()
        else:
            self._furigana = ()
            self._furigana_top = 0.0

    def _analyze_furigana(self) -> None:
        from .lyrics.furigana import analyze

        text = self.text
        # Only annotate Japanese: require at least one kana so Chinese/simplified
        # lyrics (which also contain kanji but shouldn't get Japanese readings) and
        # pure-kana lines are not mis-annotated.
        if not any(0x3040 <= ord(ch) <= 0x30FF for ch in text):
            self._furigana = ()
            self._furigana_top = 0.0
            return
        self._furigana = tuple(analyze(text))
        # 注音行高:主字上方预留一小行放注音。只有确实有注音时才增高。
        if self._furigana:
            self._furigana_top = float(self._fm_ruby.height()) + 6.0
        else:
            self._furigana_top = 0.0

    def set_media_time(self, media_time: float | None) -> None:
        self._media_time = media_time
        self.update()

    def set_max_width(self, width: int) -> None:
        """Cap the label width; longer lines scroll horizontally. 0 = unlimited."""
        width = max(0, width)
        if width != self._max_width:
            self._max_width = width
            self.updateGeometry()
            self.update()

    # --- reveal animation ---

    def _get_reveal(self) -> float:
        return self._reveal

    def _set_reveal(self, value: float) -> None:
        self._reveal = value
        self.update()

    reveal = pyqt_property(float, fget=_get_reveal, fset=_set_reveal)

    def _start_reveal(self) -> None:
        # Effects off -> show the new line immediately (no fade/rise).
        if not self._animate:
            if self._anim is not None:
                self._anim.stop()
            self._reveal = 1.0
            self.update()
            return
        # Reuse a single animation instance; creating a new one per line change
        # leaked a stopped QPropertyAnimation (parented to this label) every time.
        if self._anim is None:
            anim = QPropertyAnimation(self, b"reveal", self)
            anim.setDuration(REVEAL_DURATION_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            # OutQuint eases in fast then settles very gently, so the new line
            # glides into place instead of snapping — softer than OutCubic.
            anim.setEasingCurve(QEasingCurve.Type.OutQuint)
            self._anim = anim
        self._anim.stop()
        self._reveal = 0.0
        self._anim.start()

    # --- geometry ---

    @property
    def text(self) -> str:
        return self._line.text if self._line else ""

    def sizeHint(self) -> QSize:
        width = int(self._total_w) + 8
        if self._max_width:
            width = min(width, self._max_width)
        return QSize(max(1, width), self._fm.height() + 6 + int(self._furigana_top))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # --- sweep geometry ---

    def _compute_sweep(self, text_left: float, total_width: float) -> tuple[float, tuple[float, float] | None]:
        """Return (sweep_x, active_word_range).

        ``sweep_x`` is the absolute x up to which the line is sung. When a word
        is mid-sing, ``active_word_range`` is the (x0, x1) sub-range of that word
        already sung, to be brightened with the accent-sweep colour. Uses cached
        word widths (no per-frame text measurement).
        """
        line = self._line
        if line is None or self._media_time is None:
            return text_left, None
        t = self._media_time

        if not self._word_mode:
            return text_left + total_width * line_progress(line, t), None

        cursor = text_left
        space = self._space_w
        sung = text_left
        for i, word in enumerate(line.words):
            w = self._word_widths[i] if i < len(self._word_widths) else 0.0
            if word.start is not None and word.end is not None:
                frac = word_fill_fraction(word, t)
                if 0.0 < frac < 1.0:
                    edge = cursor + w * frac
                    return edge, (cursor, edge)
                if frac < 1.0:
                    return sung, None  # a timed word not yet reached -> stop here
            # A fully-sung timed word, or an untimed word (transparent to the
            # sweep, e.g. punctuation), extends the sung run so an untimed word
            # mid-line does not freeze the sweep for the rest of the line.
            sung = cursor + w
            cursor += w
            if i < len(line.words) - 1:
                cursor += space
                sung = cursor  # extend through the trailing space
        return sung, None

    def _is_title(self) -> bool:
        return self._line is not None and self._line.id == "title"

    def _marquee_offset(self, overflow: float) -> float:
        """A ping-pong scroll offset (0..overflow) for a long title, driven by the
        media clock: hold at the left, glide right, hold, glide back."""
        if overflow <= 0.0 or self._media_time is None:
            return 0.0
        travel = overflow / _MARQUEE_SPEED_PX_S
        cycle = 2.0 * (_MARQUEE_PAUSE_S + travel)
        phase = self._media_time % cycle
        if phase < _MARQUEE_PAUSE_S:
            return 0.0
        phase -= _MARQUEE_PAUSE_S
        if phase < travel:
            return overflow * (phase / travel)
        phase -= travel
        if phase < _MARQUEE_PAUSE_S:
            return overflow
        return overflow * (1.0 - (phase - _MARQUEE_PAUSE_S) / travel)

    def _apply_reveal_transform(self, painter: QPainter, avail: float, height: float) -> float:
        """Apply the current line-change transition to `painter` and return the alpha
        multiplier for this frame. `reveal` runs 0->1 over the animation; each style
        maps it to a different motion, all fading in together."""
        reveal = self._reveal
        inv = 1.0 - reveal
        style = self._transition
        if style == "rise":
            painter.translate(0.0, inv * REVEAL_RISE_PX)
        elif style == "slide":
            painter.translate(inv * -28.0, 0.0)  # glide in from the right
        elif style == "zoom":
            scale = 0.90 + 0.10 * reveal
            painter.translate(avail / 2.0, height / 2.0)
            painter.scale(scale, scale)
            painter.translate(-avail / 2.0, -height / 2.0)
        # "fade": opacity only, no geometric motion.
        return reveal

    # --- painting ---

    def paintEvent(self, a0: QPaintEvent | None) -> None:  # noqa: ARG002
        if not self.text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self._font)

        total_width = self._total_w
        avail = float(self.width())
        height = float(self.height())

        # Line-change transition (fade / rise / slide / zoom); `a` scales every alpha.
        a = self._apply_reveal_transform(painter, avail, height)

        # Sweep position relative to the text start (measure with text_left = 0).
        sweep_rel, active_rel = self._compute_sweep(0.0, total_width)

        if total_width <= avail:
            text_left = (avail - total_width) / 2.0  # fits -> centered
        elif self._is_title():
            # Long now-playing title: no sweep to follow, so ping-pong the whole name.
            text_left = -self._marquee_offset(total_width - avail)
            painter.setClipRect(QRectF(0.0, 0.0, avail, height))
        else:
            # Overflow: scroll so the currently-sung position stays near the centre.
            offset = max(0.0, min(sweep_rel - avail / 2.0, total_width - avail))
            text_left = -offset
            painter.setClipRect(QRectF(0.0, 0.0, avail, height))

        sweep_x = sweep_rel + text_left
        rect = QRectF(text_left, 0.0, total_width, height)
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # 0) Cheap drop shadow (single offset pass) for readability.
        painter.save()
        painter.translate(SHADOW_OFFSET, SHADOW_OFFSET)
        painter.setPen(QPen(_scale_alpha(self._shadow_color, a)))
        painter.drawText(rect, align, self.text)
        painter.restore()

        # 1) Base (unsung) text.
        painter.setPen(QPen(_scale_alpha(self._base_color, a)))
        painter.drawText(rect, align, self.text)

        # 1.5) Soft accent glow behind the sung portion (grows with the sweep).
        if self._glow and sweep_x > text_left:
            fx = _FX[self._intensity]
            painter.save()
            painter.setClipRect(QRectF(text_left, 0.0, sweep_x - text_left, height), Qt.ClipOperation.IntersectClip)
            painter.setPen(QPen(_scale_alpha(self._accent_sweep, a * fx["glow_alpha"])))
            radius = fx["glow_radius"]
            for dx, dy in _GLOW_OFFSETS:
                painter.drawText(rect.translated(dx * radius, dy * radius), align, self.text)
            painter.restore()

        # 2) Sung text, clipped to the sweep boundary, filled with the accent gradient.
        if sweep_x > text_left:
            painter.save()
            painter.setClipRect(QRectF(text_left, 0.0, sweep_x - text_left, height), Qt.ClipOperation.IntersectClip)
            gradient = QLinearGradient(text_left, 0.0, text_left + total_width, 0.0)
            gradient.setColorAt(0.0, _scale_alpha(self._accent_start, a))
            gradient.setColorAt(1.0, _scale_alpha(self._accent_end, a))
            painter.setPen(QPen(QBrush(gradient), 0))
            painter.drawText(rect, align, self.text)
            painter.restore()

        # 3) Currently-sung word: brighten its sung sub-range with the sweep colour,
        #    with an optional brighter "pop" core + glow to draw the eye to the beat.
        if active_rel is not None:
            x0 = active_rel[0] + text_left
            x1 = active_rel[1] + text_left
            if x1 > x0:
                painter.save()
                painter.setClipRect(QRectF(x0, 0.0, x1 - x0, height), Qt.ClipOperation.IntersectClip)
                color = self._accent_sweep
                if self._word_pop:
                    fx = _FX[self._intensity]
                    painter.setPen(QPen(_scale_alpha(self._accent_sweep, a * fx["glow_alpha"])))
                    radius = fx["glow_radius"]
                    for dx, dy in _GLOW_OFFSETS:
                        painter.drawText(rect.translated(dx * radius, dy * radius), align, self.text)
                    color = self._accent_sweep.lighter(fx["pop"])
                painter.setPen(QPen(_scale_alpha(color, a)))
                painter.drawText(rect, align, self.text)
                painter.restore()

        # 4) Furigana (auto 振り仮名) — drawn above the main line, colour follows the
        #    sweep so it reads in sync with the sung kanji. Skipped entirely when the
        #    line has no kanji / furigana disabled / analyzer unavailable, keeping the
        #    non-furigana path identical to the original.
        if self._furigana and self._furigana_top > 0.0:
            self._draw_furigana(painter, text_left, sweep_x, a)

    def _draw_furigana(self, painter: QPainter, text_left: float, sweep_x: float, a: float) -> None:
        """Draw the ruby line above the main text, one block per analyzed kanji word.

        Each block is centred on its base kanji. The kana colour follows the sweep
        position: blocks whose centre is sung use the accent gradient, others the
        base (unsung) colour, matching the main kanji's state.
        """
        text = self.text
        w_base_total = self._fm.horizontalAdvance(text) if text else 0.0
        rb = self._ruby_font
        for furi in self._furigana:
            base = furi.base
            kana = furi.kana
            if not base or not kana:
                continue
            x0 = text_left + self._fm.horizontalAdvance(text[: furi.pos])
            w_base = max(1.0, float(self._fm.horizontalAdvance(base)))
            w_kana = float(self._fm_ruby.horizontalAdvance(kana))
            center = x0 + w_base / 2.0
            kana_x = center - w_kana / 2.0
            # A long reading on a single kanji (e.g. 抱 -> だきしめ) can be wider than
            # the base kanji, so clamp the ruby inside the label to keep it fully
            # visible instead of being clipped at an edge (both line-start and -end).
            kana_x = max(0.0, min(kana_x, max(0.0, float(self.width()) - w_kana)))
            # Ruby sits in the headroom reserved by _furigana_top (widget's top area);
            # the widget height already accounts for it via sizeHint, so y is near the
            # top, safely inside the widget and centred under the kanji above.
            kana_rect = QRectF(kana_x, 2.0, w_kana, self._fm_ruby.height())
            sung = center <= sweep_x

            painter.save()
            painter.setFont(rb)
            # shadow for readability on any panel
            painter.save()
            painter.translate(SHADOW_OFFSET, SHADOW_OFFSET)
            painter.setPen(QPen(_scale_alpha(self._shadow_color, a)))
            painter.drawText(
                kana_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                kana,
            )
            painter.restore()
            if sung:
                grad = QLinearGradient(0, 0, w_base_total, 0)
                grad.setColorAt(0.0, _scale_alpha(self._accent_start, a))
                grad.setColorAt(1.0, _scale_alpha(self._accent_end, a))
                painter.setPen(QPen(QBrush(grad), 0))
            else:
                painter.setPen(QPen(_scale_alpha(self._base_color, a)))
            painter.drawText(
                kana_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                kana,
            )
            painter.restore()
