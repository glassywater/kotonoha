"""Item-view delegates for the settings and lyric-search windows.

A delegate paints inside one row's rectangle and holds its own colours, so a
theme change has to reach it directly: reapplying a stylesheet does not.
"""

from __future__ import annotations

from PyQt6.QtCore import QModelIndex, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from .lyrics_search_model import VERSION_LABEL_ROLE

# The popup's own item metrics, so a painted row lines up with the padding the
# skin gives every other row in the same list.
_COMBO_ITEM_RADIUS = 5.0
_COMBO_ITEM_INSET = 2
# Hover is the text colour worn thin, the way the nav rows do it. A token here
# would have to be Qt's rgba() form, which QColor does not parse at all.
_COMBO_HOVER_ALPHA = 26

_CELL_PADDING = 8

_CHIP_PADDING = 7

_CHIP_GAP = 6

# How much of a cell the qualifiers may take before the title stops being legible.
# Below this the chip is dropped rather than shown next to a title elided to
# nothing, since the qualifier alone does not say which song it qualifies.
_CHIP_MAX_SHARE = 0.55

# Below this a pill holds no readable text, so nothing is drawn at all.
_CHIP_MIN_WIDTH = 42

# The strip the nav rows leave clear on their left, and the bar that stands in it.
# The rail sits on the panel edge and the tint starts just past it: a wider gap
# leaves the current row looking pushed right, since its other three sides are
# tight against the panel.
NAV_GUTTER = 0

_NAV_SURFACE_INSET = 2

_NAV_SURFACE_GAP = 3

_NAV_BAR_INSET = 7

_NAV_BAR_WIDTH = 3

_NAV_BAR_HEIGHT = 16

_NAV_TINT_ALPHA = 26

# --radius-md from the design system this pattern is taken from.
_NAV_RADIUS = 8.0

class NavIndicatorDelegate(QStyledItemDelegate):
    """Mark the current page with a short rail and a neutral tint.

    The rail is the only "you are here" signal, so it is short, sits outside the
    row's own shape, and is the only part carrying a hue: the surface behind it
    stays neutral, which keeps a row of navigation from reading as loud as the one
    control that acts.
    """

    def __init__(self, accent: str, foreground: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_colours(accent, foreground)

    def set_colours(self, accent: str, foreground: str) -> None:
        """Adopt a newly applied palette.

        The rail and the tint are QColors taken once, so a delegate built in one
        theme kept painting the current row in it after the window had moved.
        """
        # The rail takes the accent; the tint stays neutral so the row behind the
        # rail does not become a second coloured surface.
        self._rail = QColor(accent)
        self._tint = QColor(foreground)
        self._tint.setAlpha(_NAV_TINT_ALPHA)

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Paint the current row's tint and its bar, then let the row draw itself.

        Both are drawn here rather than from the stylesheet because a delegate may
        only paint inside the row it is given: a bar placed left of that rectangle
        is clipped away, so the row keeps its full width and leaves the gutter
        empty by agreement instead of by margin.
        """
        # No weight change: the label would thicken and the glyph beside it could
        # not, leaving one row carrying two weights. The surface and the rail say
        # which page is current.
        if painter is not None and option.state & QStyle.StateFlag.State_Selected:
            row = option.rect
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            # Inset vertically too: filling the row's full height put the current
            # surface against its neighbours with no gap between them.
            surface = row.adjusted(
                _NAV_SURFACE_INSET, _NAV_SURFACE_GAP, -_NAV_SURFACE_INSET, -_NAV_SURFACE_GAP
            )
            painter.setBrush(self._tint)
            painter.drawRoundedRect(surface, _NAV_RADIUS, _NAV_RADIUS)
            # Inside the surface, before the glyph: the mark belongs to the row it
            # fills, not to the strip beside it.
            bar = QRect(
                surface.left() + _NAV_BAR_INSET,
                row.top() + (row.height() - _NAV_BAR_HEIGHT) // 2,
                _NAV_BAR_WIDTH,
                _NAV_BAR_HEIGHT,
            )
            painter.setBrush(self._rail)
            painter.drawRoundedRect(bar, _NAV_BAR_WIDTH / 2.0, _NAV_BAR_WIDTH / 2.0)
            painter.restore()
        super().paint(painter, option, index)

class SelectionBarDelegate(QStyledItemDelegate):
    """Mark the selected row with an accent bar down its left edge.

    A tinted row background has to stay light enough to keep the text readable,
    which leaves it competing with the alternating row colours; the bar carries
    the selection on its own.
    """

    def __init__(
        self,
        accent: str,
        chip_fill: str,
        chip_text: str,
        chip_border: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._width = 3
        # A qualifier is a label; the accent belongs to the one control that acts.
        self._chip_fill = QColor(chip_fill)
        self._chip_text = QColor(chip_text)
        self._chip_border = QColor(chip_border)

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Paint the cell inside whatever the chips leave it, then the chips."""
        metrics = QFontMetrics(option.font)
        cell = option.rect
        # Halved, because the strip is reserved on both sides to keep the text
        # centred: spending the full share twice collapsed the title to nothing.
        budget = int((cell.width() - 2 * _CELL_PADDING) * _CHIP_MAX_SHARE / 2)
        chips: list[tuple[str, int]] = []
        for value in index.data(VERSION_LABEL_ROLE) or ():
            remaining = budget - sum(width + _CHIP_GAP for _, width in chips)
            if remaining < _CHIP_MIN_WIDTH:
                break
            label = str(value)
            width = metrics.horizontalAdvance(label) + 2 * _CHIP_PADDING
            if width > remaining:
                # Never shortened: "Live A…" names no version, and the title still
                # carries the words when the model declines to lift them.
                break
            chips.append((label, width))
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        text_width = metrics.horizontalAdvance(text)
        # Reserved on both sides, not only where the chips go: taking it from one
        # side moves the centre, and a centred title then sits left of the titles
        # in rows that carry no qualifier.
        reserved = sum(width + _CHIP_GAP for _, width in chips)
        if text_width > cell.width() - 2 * (reserved + _CELL_PADDING):
            # The title itself has to shorten, and a mark hanging off a cut-off
            # name says less than the space it takes: the row keeps the name.
            chips = []
            reserved = 0
        text_option = QStyleOptionViewItem(option)
        text_option.rect = cell.adjusted(reserved, 0, -reserved, 0)
        super().paint(painter, text_option, index)
        if painter is None:
            return
        if index.column() == 0 and option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(QRect(cell.left(), cell.top(), self._width, cell.height()), self._accent)
        if chips:
            self._paint_chips(painter, option, metrics, chips, text_width)

    def _draw_pill(
        self,
        painter: QPainter,
        metrics: QFontMetrics,
        box: QRect,
        text: str,
        fill: QColor,
        border: QColor,
        ink: QColor,
    ) -> None:
        """Draw one rounded label with its text centred on the ink, not the layout box.

        Qt centres a string inside a rectangle using the font's ascent and descent,
        which are Latin measurements: CJK glyphs sit low in that box and the pill
        reads as padded above and tight below. Centring on the baseline puts the
        glyphs where the eye expects them in either script.
        """
        painter.setBrush(fill)
        painter.setPen(border)
        radius = box.height() / 2.0
        painter.drawRoundedRect(box, radius, radius)
        painter.setPen(ink)
        baseline = box.center().y() + (metrics.ascent() - metrics.descent()) // 2
        painter.drawText(box.center().x() - metrics.horizontalAdvance(text) // 2, baseline, text)

    def _paint_chips(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        metrics: QFontMetrics,
        chips: list[tuple[str, int]],
        text_width: int,
    ) -> None:
        """Fill the reserved strip at the cell's right with one pill per qualifier."""
        cell = option.rect
        height = metrics.height()
        top = cell.top() + (cell.height() - height) // 2
        # Directly after the name, wherever the centred name ends: a mark parked at
        # the cell's edge belongs to the column, not to the title it qualifies.
        left = (cell.center().x() + text_width // 2) + _CHIP_GAP
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for label, width in chips:
            chip = QRect(left, top, width, height)
            self._draw_pill(
                painter, metrics, chip, label, self._chip_fill, self._chip_border, self._chip_text
            )
            left += width + _CHIP_GAP
        painter.restore()

class ComboItemDelegate(QStyledItemDelegate):
    """Paint a dropdown row's own selection, because the platform style will not.

    Breeze draws the item panel from the desktop colour scheme and reads neither
    `::item:selected` nor the palette highlight, so on KDE every popup row lit up
    in the system blue while the window around it carried the accent. Painting
    here and then handing the row on without its selected state is the only way
    to keep one colour: a style that insists on drawing the highlight cannot be
    told a different colour, only prevented from being asked.
    """

    def __init__(
        self,
        accent: str,
        text: str,
        on_accent: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._text = QColor(text)
        self._on_accent = QColor(on_accent)
        self._hover = QColor(text)
        self._hover.setAlpha(_COMBO_HOVER_ALPHA)

    def paint(self, painter: QPainter | None, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Fill the row, then draw it as if nothing were selected."""
        row = QStyleOptionViewItem(option)
        self.initStyleOption(row, index)
        selected = bool(row.state & QStyle.StateFlag.State_Selected)
        hovered = bool(row.state & QStyle.StateFlag.State_MouseOver)
        # Stripped before the row is handed on, so the style has no highlight to
        # draw over the one just painted.
        row.state &= ~QStyle.StateFlag.State_Selected
        row.state &= ~QStyle.StateFlag.State_MouseOver
        if painter is not None and (selected or hovered):
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent if selected else self._hover)
            painter.drawRoundedRect(
                QRectF(row.rect.adjusted(_COMBO_ITEM_INSET, 1, -_COMBO_ITEM_INSET, -1)),
                _COMBO_ITEM_RADIUS,
                _COMBO_ITEM_RADIUS,
            )
            painter.restore()
        palette = row.palette
        colour = self._on_accent if selected else self._text
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
            palette.setColor(role, colour)
        row.palette = palette
        super().paint(painter, row, index)


class FontNameDelegate(ComboItemDelegate):
    """Preview each font family in its own face in the combo popup."""

    def initStyleOption(self, option: QStyleOptionViewItem | None, index: QModelIndex) -> None:
        super().initStyleOption(option, index)
        if option is None:
            return
        family = index.data()
        if isinstance(family, str) and family:
            option.font = QFont(family)

__all__ = [
    "NAV_GUTTER",
    "ComboItemDelegate",
    "FontNameDelegate",
    "NavIndicatorDelegate",
    "SelectionBarDelegate",
]
