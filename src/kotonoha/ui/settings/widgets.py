"""Reusable Qt widgets and presentation helpers for the settings pages."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPixmap,
    QRegion,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFontComboBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QSizePolicy,
    QTableView,
    QWidget,
)

from .delegates import ComboItemDelegate, FontNameDelegate


@dataclass(frozen=True, slots=True)
class PopupSkin:
    """Everything a dropdown popup needs to match the window that owns it."""

    stylesheet: str
    background: str
    accent: str
    text: str
    on_accent: str


FONT_FALLBACKS = (
    "Noto Sans CJK SC", "Noto Sans CJK TC", "Noto Sans CJK JP", "Source Han Sans SC",
    "Microsoft YaHei", "PingFang SC", "Noto Sans", "DejaVu Sans",
)

# Lyrics are primarily rendered for Chinese text, so use Qt's glyph coverage
# query instead of assuming a fixed distribution-specific family list.
_TARGET_WRITING_SYSTEM = QFontDatabase.WritingSystem.SimplifiedChinese

PLAYER_ROW_MAX_CHARS = 60

# Slow enough to read while it moves; the hold is long enough to read an end.
_SCROLL_INTERVAL_MS = 33

_SCROLL_STEP_PX = 1

_SCROLL_HOLD_TICKS = 45

def elide_player_row(text: str) -> str:
    """Keep a player summary compact enough for the settings combo box."""
    return text if len(text) <= PLAYER_ROW_MAX_CHARS else text[: PLAYER_ROW_MAX_CHARS - 1] + "..."

class RoundedTableView(QTableView):
    """A table whose corners are actually round.

    A stylesheet radius reaches the frame but not the viewport, and the rows are
    painted into the viewport, so the corners came back square wherever the table
    had a surface of its own to show. A mask cuts the widget itself.
    """

    def __init__(self, radius: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radius = radius

    def resizeEvent(self, e: QResizeEvent | None) -> None:
        """Recut the rounded mask whenever the table changes size."""
        super().resizeEvent(e)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

def _constrain_combo_popup(combo: QComboBox) -> None:
    """Keep a content-sized Qt popup within its owning combo-box width."""
    view = combo.view()
    if view is None:
        return
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    width = combo.width()
    view.setFixedWidth(width)
    popup = view.window()
    if popup is not None and popup is not view:
        popup.setFixedWidth(width)

def _skin_combo_popup(combo: QComboBox, skin: PopupSkin) -> None:
    """Give one combo's popup its colours, once per theme.

    Not on every open: changing a view's stylesheet makes the combo schedule a
    rebuild of its popup container, and that rebuild lands after showPopup()
    returns — taking the item delegate installed in between with it.
    """
    view = combo.view()
    if view is None:
        return
    view.setObjectName("settingsComboPopup")
    view.setStyleSheet(skin.stylesheet)
    viewport = view.viewport()
    if viewport is not None:
        viewport.setStyleSheet(f"background: {skin.background};")
    popup = view.window()
    if popup is not None and popup is not view:
        popup.setObjectName("settingsComboPopupFrame")
        popup.setStyleSheet(skin.stylesheet)


def _paint_combo_rows(
    combo: QComboBox, skin: PopupSkin | None, font_preview: bool
) -> ComboItemDelegate | None:
    """Install the delegate that draws this popup's selected row, and return it.

    A platform style paints the item panel from the desktop colour scheme and
    reads neither `::item:selected` nor the palette highlight, so on KDE every
    dropdown lit up in the system blue while the window carried the accent. The
    container is built inside showPopup(), so the delegate has to be installed
    after that call rather than once at construction — and the caller has to keep
    the returned object, which a view does not own.
    """
    view = combo.view()
    if skin is None or view is None:
        return None
    factory = FontNameDelegate if font_preview else ComboItemDelegate
    delegate = factory(skin.accent, skin.text, skin.on_accent, view)
    view.setItemDelegate(delegate)
    return delegate


class SettingsComboBox(QComboBox):
    """Combo box whose popup follows the stable width of its field."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._popup_skin: PopupSkin | None = None
        # Retained: a view does not own the delegate it is given, so dropping the
        # only reference here would put the platform's own painting back.
        self._row_delegate: ComboItemDelegate | None = None

    def set_popup_skin(self, skin: PopupSkin) -> None:
        """Adopt the colours this window's popups are drawn in."""
        self._popup_skin = skin
        _skin_combo_popup(self, skin)

    def showPopup(self) -> None:
        """Open the popup, then paint its rows and constrain its frame."""
        super().showPopup()
        _constrain_combo_popup(self)
        self._row_delegate = _paint_combo_rows(self, self._popup_skin, font_preview=False)


class SettingsFontComboBox(QFontComboBox):
    """Font picker with the same bounded popup policy as other settings combos."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._popup_skin: PopupSkin | None = None
        # Retained for the same reason as the plain combo above.
        self._row_delegate: ComboItemDelegate | None = None

    def set_popup_skin(self, skin: PopupSkin) -> None:
        """Adopt the colours this window's popups are drawn in."""
        self._popup_skin = skin
        _skin_combo_popup(self, skin)

    def showPopup(self) -> None:
        """Open the font list, then preview each family and constrain the frame."""
        super().showPopup()
        _constrain_combo_popup(self)
        self._row_delegate = _paint_combo_rows(self, self._popup_skin, font_preview=True)


class IconStrip(QListWidget):
    """Icon grid whose height follows the rows produced by Qt's layout."""

    def resizeEvent(self, e: QResizeEvent | None) -> None:
        super().resizeEvent(e)
        self._refit_height()

    def _refit_height(self) -> None:
        if self.count() == 0:
            return
        last = self.visualItemRect(self.item(self.count() - 1))
        wanted = last.bottom() + 8
        if self.height() != wanted:
            self.setFixedHeight(wanted)

def resolve_font_family(
    font_family: str,
    installed_families: Collection[str] | None = None,
    *,
    supported_families: Collection[str] | None = None,
    desktop_family: str | None = None,
) -> str:
    """Choose a usable family from a configured fallback chain.

    Omitted inventories are read from Qt. Callers that supply an inventory can
    resolve deterministically without consulting the host font installation;
    when no separate writing-system set is supplied, every injected family is
    treated as target-capable.
    """
    if installed_families is None:
        installed = set(QFontDatabase.families())
        supported = (
            set(QFontDatabase.families(_TARGET_WRITING_SYSTEM))
            if supported_families is None
            else set(supported_families)
        )
    else:
        installed = set(installed_families)
        supported = set(installed if supported_families is None else supported_families)
    supported.intersection_update(installed)
    desktop = QApplication.font().family() if desktop_family is None else desktop_family
    requested = [name.strip().strip("'\"") for name in font_family.split(",")]
    for name in requested:
        if name and name in installed:
            return name
    for fallback in FONT_FALLBACKS:
        if fallback in supported:
            return fallback
    if desktop in supported:
        return desktop
    if supported:
        return min(supported)
    if desktop in installed:
        return desktop
    if installed:
        return min(installed)
    if desktop:
        return desktop
    return next((name for name in requested if name), "")

def available_font_styles(family: str) -> list[str]:
    """Return the real styles advertised by one installed family."""
    styles = QFontDatabase.styles(family)
    if not styles:
        return ["Regular"]
    return sorted(styles, key=lambda style: (0 if style in ("Regular", "Book", "Normal") else 1, style))

def no_tint_icon(pixmap: QPixmap) -> QIcon:
    """Reuse the normal pixmap for selected states so Qt adds no blue tint."""
    icon = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Selected, QIcon.Mode.Active):
        icon.addPixmap(pixmap, mode)
    return icon

class ScrollingLabel(QLabel):
    """Show a line too long for its box by moving it, and hold still otherwise.

    Motion is expensive attention, so it is spent only where the text cannot be
    read any other way: a title that fits does not move at all, and one that does
    not fit pauses at each end so both can actually be read.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._offset = 0
        self._hold = _SCROLL_HOLD_TICKS
        self._back = False
        self._timer = QTimer(self)
        self._timer.setInterval(_SCROLL_INTERVAL_MS)
        self._timer.timeout.connect(self._step)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, a0: str | None) -> None:
        """Replace the line and restart from its beginning."""
        self._full_text = a0 or ""
        self._offset = 0
        self._back = False
        self._hold = _SCROLL_HOLD_TICKS
        super().setText(self._full_text)
        self._retime()

    def full_text(self) -> str:
        """Return the whole line, whatever part of it is currently visible."""
        return self._full_text

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        """Start or stop the motion when the space for the line changes."""
        super().resizeEvent(a0)
        self._retime()

    def _overflow(self) -> int:
        """Return how much of the line does not fit, or zero when it all does."""
        metrics = QFontMetrics(self.font())
        return max(0, metrics.horizontalAdvance(self._full_text) - self.contentsRect().width())

    def _retime(self) -> None:
        """Run the timer only while there is something off the end to reach."""
        if self._overflow() > 0:
            if not self._timer.isActive():
                self._timer.start()
            return
        self._timer.stop()
        self._offset = 0
        self.update()

    def _step(self) -> None:
        """Advance one step, holding at each end before turning back."""
        overflow = self._overflow()
        if overflow <= 0:
            self._retime()
            return
        if self._hold > 0:
            self._hold -= 1
            return
        self._offset += -_SCROLL_STEP_PX if self._back else _SCROLL_STEP_PX
        if self._offset >= overflow or self._offset <= 0:
            self._offset = max(0, min(overflow, self._offset))
            self._back = not self._back
            self._hold = _SCROLL_HOLD_TICKS
        self.update()

    def paintEvent(self, a0: QPaintEvent | None) -> None:
        """Draw the line shifted by however far it has travelled."""
        del a0
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        box = self.contentsRect().adjusted(-self._offset, 0, 0, 0)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._full_text)
        painter.end()

class ElidingLabel(QLabel):
    """Show one line that shrinks with its container instead of widening it.

    A plain QLabel makes a layout at least as wide as its whole string, so a long
    status message either forces the dialog wider than the screen or is clipped
    with no sign that text is missing.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The unelided string; self.text() only ever holds what currently fits.
        self._full_text = ""
        # Ignored makes the layout disregard both size hints, so this line neither
        # widens its container nor sets a width the container cannot shrink below.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, a0: str | None) -> None:
        """Store the full text and display as much of it as currently fits."""
        self._full_text = a0 or ""
        self._elide()

    def full_text(self) -> str:
        """Return the text as set, which the displayed line may have shortened."""
        return self._full_text

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        """Re-elide against the new width so the ellipsis tracks the layout."""
        super().resizeEvent(a0)
        self._elide()

    def _elide(self) -> None:
        """Display the widest prefix of the full text that fits, plus an ellipsis."""
        metrics = QFontMetrics(self.font())
        super().setText(
            metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width())
        )

class ClearableLineEdit(QLineEdit):
    """A text field owning a themed clear action that appears only when needed.

    Qt's built-in clear button keeps one colour, which disappears on a light
    field. The action belongs to the field so its visibility follows a bound
    method here: PyQt holds a lambda strongly and would keep firing it into a
    deleted C++ object.
    """

    def __init__(self, text: str, glyph: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        action = self.addAction(glyph, QLineEdit.ActionPosition.TrailingPosition)
        if action is None:
            raise RuntimeError("a query field could not take a clear action")
        self._clear_action = action
        self._clear_action.setVisible(bool(text))
        self._clear_action.triggered.connect(self.clear)
        self.textChanged.connect(self._follow_text)

    def set_clear_glyph(self, glyph: QIcon) -> None:
        """Redraw the clear mark after a theme change."""
        self._clear_action.setIcon(glyph)

    def _follow_text(self, text: str) -> None:
        """Offer the clear action only while there is something to clear."""
        self._clear_action.setVisible(bool(text))

__all__ = [
    "PopupSkin",
    "ClearableLineEdit",
    "FONT_FALLBACKS",
    "ElidingLabel",
    "IconStrip",
    "RoundedTableView",
    "ScrollingLabel",
    "SettingsComboBox",
    "SettingsFontComboBox",
    "available_font_styles",
    "elide_player_row",
    "no_tint_icon",
    "resolve_font_family",
]
