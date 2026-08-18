"""Qt presentation adapter for the toolkit-neutral overlay window contract."""

from __future__ import annotations

import PyQt6.sip as sip
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from .overlay_contracts import WindowPoint, WindowPolicy, WindowRectangle


class QtWindowHost:
    """Translate abstract window operations to one top-level Qt widget.

    Deliberately not inheriting WindowHost: a Protocol's empty bodies come with
    it, so a method left unimplemented becomes a silent no-op returning None
    while the caller reports success. Conformance is structural and checked
    where a host is passed; a genuine omission now raises.
    """

    def __init__(self, widget: QWidget) -> None:
        self._widget = widget

    def is_alive(self) -> bool:
        """False once the C++ widget is gone.

        A deferred rebuild would otherwise call into a deleted object, which is
        how this project has produced segfaults before."""
        return not sip.isdeleted(self._widget)

    def apply_window_policy(self, policy: WindowPolicy) -> None:
        if policy.recreate_surface:
            flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Window
            )
            if policy.transparent_for_input:
                flags |= Qt.WindowType.WindowTransparentForInput
            if policy.does_not_accept_focus:
                flags |= Qt.WindowType.WindowDoesNotAcceptFocus
            self._widget.setWindowFlags(flags)
        self._widget.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            policy.mouse_events_transparent,
        )
        self._widget.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating,
            policy.show_without_activating,
        )

    def native_window_pointer(self) -> int | None:
        self._widget.winId()
        handle = self._widget.windowHandle()
        if handle is None:
            return None
        try:
            pointer = sip.unwrapinstance(handle)
        except (RuntimeError, TypeError):
            return None
        return int(pointer) if pointer is not None else None

    def geometry(self) -> WindowRectangle:
        return self._rectangle(self._widget.geometry())

    def window_position(self) -> WindowPoint | None:
        """Where the toolkit says the window is.

        Deliberately not the last requested position: a cache of what was asked
        for makes any "did the move land" check agree with itself. Whether a move
        can land at all is a capability the adapter states, not something read
        back here — on Wayland the toolkit reports the requested position whether
        or not the compositor applied it.
        """
        geometry = self._widget.geometry()
        return WindowPoint(geometry.x(), geometry.y())

    def screen_geometry(self) -> WindowRectangle | None:
        handle = self._widget.windowHandle()
        screen = handle.screen() if handle is not None else QApplication.primaryScreen()
        return self._rectangle(screen.geometry()) if screen is not None else None

    def bind_output(self, output: WindowRectangle) -> None:
        for screen in QApplication.screens():
            if self._rectangle(screen.geometry()) == output:
                self._widget.setScreen(screen)
                handle = self._widget.windowHandle()
                if handle is not None:
                    handle.setScreen(screen)
                return
        raise RuntimeError("Requested output is not available.")

    def hide_window(self) -> None:
        self._widget.hide()

    def destroy_surface(self) -> None:
        handle = self._widget.windowHandle()
        if handle is not None:
            handle.destroy()

    def move_window(self, position: WindowPoint) -> None:
        self._widget.move(position.x, position.y)

    def set_input_mask(self, region: WindowRectangle) -> None:
        """Confine pointer input to one rectangle.

        Qt's mask is the toolkit's own input shaping: on X11 it sets the input
        shape, and the Wayland plugin turns it into wl_surface.set_input_region.
        Without it an ordinary window accepted clicks across its whole transparent
        band and swallowed input meant for the window behind it."""
        self._widget.setMask(QRegion(region.x, region.y, region.width, region.height))

    def clear_input_mask(self) -> None:
        """Remove the shaping. Click-through is carried by the window policy, so
        this never doubles as "accept nothing"."""
        self._widget.clearMask()

    def refresh(self) -> None:
        self._widget.update()

    @staticmethod
    def _rectangle(rectangle: QRect) -> WindowRectangle:
        return WindowRectangle(
            rectangle.x(), rectangle.y(), rectangle.width(), rectangle.height()
        )
