"""Shared themed window surface for settings-related Qt dialogs."""

from __future__ import annotations

import logging
from typing import cast

from PyQt6.QtCore import QChildEvent, QEvent, QObject, QPoint, Qt
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
)
from PyQt6.QtWidgets import QDialog, QLabel, QWidget

from ... import leaf_icon
from ...config import Config
from ...platform import OverlayPlatform, OverlayPlatformFactory, QtWindowHost, SurfaceResult, WindowRectangle
from . import theme

_RADIUS = 14
# The strip along each edge that starts a resize. It has to stay inside the
# outermost layout margin, or a drag meant for a control would resize instead.
_RESIZE_MARGIN = 6
logger = logging.getLogger(__name__)


class SettingsTitleBar(QWidget):
    """Provide the shared drag target used by frameless settings windows."""

    def mousePressEvent(self, a0: QMouseEvent | None) -> None:
        if a0 is not None and a0.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            if window is not None:
                handle = window.windowHandle()
                if handle is not None and handle.startSystemMove():
                    a0.accept()
                    return
        super().mousePressEvent(a0)


class ThemedSettingsDialog(QDialog):
    """Own the shared frameless, translucent, and blur-capable dialog surface."""

    def __init__(
        self,
        config: Config,
        parent: QWidget | None = None,
        *,
        platform_factory: OverlayPlatformFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme._resolve_theme(config.theme)
        self._accent = config.accent_start
        self._platform: OverlayPlatform | None = None
        if platform_factory is not None:
            self._platform = platform_factory(QtWindowHost(self, stay_on_top=False))
        capabilities = self._platform.capabilities if self._platform is not None else None
        self._blur_capable = capabilities is not None and capabilities.blur
        self._blur_reason = capabilities.blur_reason if capabilities is not None else "bridge"
        self._window_opacity_ok = capabilities is None or capabilities.window_opacity
        self._frosted = self._blur_capable and config.frost_window
        self._win_opacity = config.settings_opacity
        # A resize can be delivered while the base widget is being configured;
        # defer virtual style hooks until the concrete dialog owns its fields.
        self._surface_style_ready = False
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # A frameless window is given no borders to pull, so the edges have to
        # offer the resize themselves. Tracking is what makes the cursor change
        # before the reader presses, which is the only sign the edge is live.
        self.setMouseTracking(True)
        # Filtering its own events is how the dialog hears ChildAdded and can
        # follow the pointer into each control as the window is built up.
        self.installEventFilter(self)

    def retheme(self, config: Config) -> None:
        """Adopt a newly applied theme without being closed and reopened.

        Only the settings window used to restyle itself, so a search or cache
        window left open kept the palette it was born with while the dialog that
        changed it repainted — two windows of one application disagreeing about
        which theme is in effect.
        """
        self._theme = theme._resolve_theme(config.theme)
        self._accent = config.accent_start
        self._win_opacity = config.settings_opacity
        was_frosted = self._frosted
        self._frosted = self._blur_capable and config.frost_window
        if self._frosted != was_frosted:
            # The blur region is compositor state, not a stylesheet rule: turning
            # frost on or off has to reach it or the window keeps the old backdrop.
            self._apply_blur() if self._frosted else self._clear_blur()
        if self._surface_style_ready:
            self._apply_surface_style()
            self._refresh_themed_icons()
        self.update()

    def _refresh_themed_icons(self) -> None:
        """Redraw any icon whose colour came from the palette.

        A stylesheet reapplies itself; an icon does not. One rendered with the
        old palette survives a theme change looking inverted against the text
        beside it, which is the one part of the window that did not follow.
        """

    def _resize_edges_at(self, point: QPoint) -> Qt.Edge:
        """Return the edges a press at this point would pull.

        Frameless windows lose the compositor's own borders, so the edge a drag
        belongs to is decided here and handed back to the compositor, which then
        owns the drag itself.
        """
        edges = Qt.Edge(0)
        if point.x() <= _RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif point.x() >= self.width() - _RESIZE_MARGIN - 1:
            edges |= Qt.Edge.RightEdge
        if point.y() <= _RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif point.y() >= self.height() - _RESIZE_MARGIN - 1:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _resize_cursor(self, edges: Qt.Edge) -> Qt.CursorShape:
        """Name the cursor that says which way this edge moves."""
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            falling = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
            return Qt.CursorShape.SizeFDiagCursor if falling else Qt.CursorShape.SizeBDiagCursor
        if horizontal:
            return Qt.CursorShape.SizeHorCursor
        if vertical:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mouseMoveEvent(self, a0: QMouseEvent | None) -> None:
        """Show which edge is under the pointer before it is pressed."""
        if a0 is not None:
            self.setCursor(self._resize_cursor(self._resize_edges_at(a0.position().toPoint())))
        super().mouseMoveEvent(a0)

    def mousePressEvent(self, a0: QMouseEvent | None) -> None:
        """Hand a press on an edge to the compositor as a resize."""
        if a0 is not None and a0.button() == Qt.MouseButton.LeftButton:
            edges = self._resize_edges_at(a0.position().toPoint())
            handle = self.windowHandle()
            if edges != Qt.Edge(0) and handle is not None and handle.startSystemResize(edges):
                a0.accept()
                return
        super().mousePressEvent(a0)

    def leaveEvent(self, a0: QEvent | None) -> None:
        """Drop the resize cursor when the pointer leaves the window."""
        self.unsetCursor()
        super().leaveEvent(a0)

    def _follow_pointer_into(self, widget: QWidget) -> None:
        """Hear about the pointer entering this widget and anything inside it."""
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:
        """Clear the edge cursor once the pointer is over a control instead.

        The cursor is set on the dialog, and a child with no cursor of its own
        inherits it. Moving from an edge straight into a control gives the dialog
        no further mouse moves and does not leave the window, so nothing here ran
        and the resize cursor stayed over ordinary controls and the title bar.
        A child that has just been entered is the one thing that still knows.
        """
        if a1 is not None:
            kind = a1.type()
            if kind == QEvent.Type.Enter and a0 is not self:
                self.unsetCursor()
            elif kind == QEvent.Type.ChildAdded:
                child = cast("QChildEvent", a1).child()
                if isinstance(child, QWidget):
                    # Watched as the window is built, not once at the end: the
                    # pages are filled in long after the dialog exists.
                    self._follow_pointer_into(child)
        return super().eventFilter(a0, a1)

    def _paint_leaf_badge(self, badge: QLabel) -> None:
        """Tint the title-bar leaf with the accent now in effect.

        The badge is a pixmap rendered once, so a window left open through an
        accent change kept the old hue beside controls styled with the new one.
        """
        pixmap = leaf_icon.render_leaf(leaf_icon.ACCENT, self._accent, size=44)
        pixmap.setDevicePixelRatio(2.0)
        badge.setPixmap(pixmap)

    def _apply_surface_style(self) -> None:
        """Apply the current content stylesheet supplied by a concrete dialog."""
        raise NotImplementedError

    def _mark_surface_style_ready(self) -> None:
        """Allow platform callbacks to reapply the concrete dialog stylesheet."""
        self._surface_style_ready = True

    def paintEvent(self, a0: QPaintEvent | None) -> None:  # noqa: ARG002
        """Paint the theme-aware rounded window fill behind dialog children."""
        palette = theme._PALETTES[self._theme]
        rgba = cast("dict[str, tuple[int, int, int, int]]", palette)
        bg = rgba["window_bg"]
        # The setting applies in both modes, at face value. Frosted glass used to
        # paint one hardcoded alpha instead, so the control did nothing at any value
        # in the mode most people leave on; scaling it down would have been the same
        # mistake in a smaller way, since 100% means opaque and nothing else.
        bg = (bg[0], bg[1], bg[2], max(0, min(255, round(255 * self._win_opacity))))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(*bg))
        painter.setPen(QPen(QColor(*rgba["window_border"])))
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.drawRoundedRect(rect, float(_RADIUS), float(_RADIUS))

    def _clear_blur(self) -> None:
        """Drop the compositor blur region when frost is switched off."""
        blur = self._platform.blur if self._platform is not None else None
        if blur is not None:
            blur.set_blur_region(None, _RADIUS)

    def _apply_blur(self) -> None:
        """Apply the compositor blur region and fall back to an opaque surface."""
        if not self._frosted or self._platform is None:
            return
        blur = self._platform.blur
        if blur is None:
            return
        result = blur.set_blur_region(WindowRectangle(0, 0, self.width(), self.height()), _RADIUS)
        if result.succeeded:
            return
        logger.warning("Frosted glass unavailable, falling back to a solid panel: %s", result.reason)
        self._frosted = False
        if self._surface_style_ready:
            self._apply_surface_style()
            self.update()

    def hideEvent(self, a0: QHideEvent | None) -> None:
        """Release the compositor blur region when the dialog is hidden."""
        if self._frosted and self._platform is not None:
            blur = self._platform.blur
            if blur is not None:
                blur.set_blur_region(None)
        super().hideEvent(a0)

    def done(self, a0: int) -> None:
        """Finish the Qt dialog before releasing its native platform surface."""
        super().done(a0)
        surface_result = self._close_platform()
        if not surface_result.succeeded:
            logger.warning("Settings surface shutdown was incomplete: %s", surface_result.reason)

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """Let QDialog finish and release the platform surface from :meth:`done`."""
        super().closeEvent(a0)

    def _close_platform(self) -> SurfaceResult:
        """Release only the optional blur borrowed by this ordinary dialog.

        Settings is a normal Qt dialog, not the output-bound overlay surface.
        Closing the selected adapter would hide and destroy the Qt surface that
        owns this dialog, so blur is the only compositor resource released here.
        """
        if self._platform is None:
            return SurfaceResult.applied()
        blur = self._platform.blur
        if blur is None:
            return SurfaceResult.applied()
        return blur.set_blur_region(None)

    def show(self) -> None:
        """Show the normal Qt dialog without changing its native surface."""
        super().show()

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        """Keep the compositor blur region matched to the current window size."""
        super().resizeEvent(a0)
        self._apply_blur()

    def showEvent(self, a0: QShowEvent | None) -> None:
        """Apply optional blur after the normal Qt dialog is mapped."""
        super().showEvent(a0)
        self._apply_blur()

    @staticmethod
    def _log_surface_failure(operation: str, result: SurfaceResult) -> None:
        """Log a non-fatal platform lifecycle failure with its reported reason."""
        logger.warning("Settings surface %s failed: %s", operation, result.reason)


__all__ = ["SettingsTitleBar", "ThemedSettingsDialog"]
