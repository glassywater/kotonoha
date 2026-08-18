"""Ordinary top-most window implementation of the overlay contract."""

from __future__ import annotations

from collections.abc import Callable

from .overlay_contracts import (
    _NO_WINDOW_OPACITY,
    DragMode,
    DragStartResult,
    LayerShellBridge,
    Output,
    OverlayCapabilities,
    OverlayDragStrategy,
    OverlayOperationResult,
    WindowHost,
    WindowPoint,
    WindowPolicy,
    WindowRectangle,
)

_NO_CLIENT_POSITIONING = "This compositor ignores a client-side move of an ordinary window."


class OrdinaryWindowDragStrategy:
    """Move an ordinary window from the press-relative local pointer anchor."""

    def __init__(self, host: WindowHost, *, client_positioning: bool = True) -> None:
        self._host = host
        self._client_positioning = client_positioning
        self._origin: WindowPoint | None = None
        self._window_origin = WindowPoint(0, 0)

    def set_position(self, position: WindowPoint) -> None:
        self._window_origin = position

    def begin_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> DragStartResult:
        del global_position
        current = self._host.window_position() or self._window_origin
        self._window_origin = current
        self._origin = local_position
        return DragStartResult(DragMode.MANUAL)

    def update_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> OverlayOperationResult:
        del global_position
        if self._origin is None:
            return OverlayOperationResult.failure("Window drag has not started")
        # move_to already refuses here; the drag path went straight to the host and
        # so reported every update as applied on a compositor that moves nothing.
        # The two paths have to answer the same question the same way.
        if not self._client_positioning:
            return OverlayOperationResult.failure(_NO_CLIENT_POSITIONING)
        position = WindowPoint(
            self._window_origin.x + local_position.x - self._origin.x,
            self._window_origin.y + local_position.y - self._origin.y,
        )
        try:
            self._host.move_window(position)
        except RuntimeError as exc:
            return OverlayOperationResult.failure(f"Window move failed: {exc}")
        # The window origin advances; the press point does not. The window follows
        # the pointer, so the pointer's local position re-settles toward where the
        # press landed — advancing that anchor too counts the settling twice and
        # the window snaps back or stalls. Same model as the Layer Shell anchor.
        self._window_origin = position
        return OverlayOperationResult.success()

    def end_drag(self) -> None:
        self._origin = None


class QtWindowPlatform:
    """Use toolkit window flags when compositor-specific overlay APIs are absent."""

    def __init__(
        self,
        host: WindowHost,
        *,
        reason: str | None = None,
        blur: LayerShellBridge | None = None,
        client_positioning: bool = True,
        window_opacity: bool = True,
    ) -> None:
        self._host = host
        self._reason = reason
        # Wayland without Layer Shell ignores a client-side move of a toplevel, and
        # the client cannot detect it: Qt reports the requested position either way.
        # So the provider states it here instead of the adapter guessing afterwards.
        self._client_positioning = client_positioning
        # Wayland has no client-side window-opacity protocol either, and the same
        # provider knows which session this is.
        self._window_opacity = window_opacity
        # Blur is a separate capability from Layer Shell: Mutter offers no
        # layer-shell and does speak ext-background-effect-v1, so hardcoding
        # blur=False here dropped the frosted panel on exactly the compositor the
        # blur work was for. When a bridge is available the answer comes from it.
        self._blur = blur
        self._drag_strategy: OverlayDragStrategy = OrdinaryWindowDragStrategy(
            host, client_positioning=client_positioning
        )

    @property
    def capabilities(self) -> OverlayCapabilities:
        blur = self._blur is not None and self._blur.blur_available
        return OverlayCapabilities(
            layer_shell=False,
            blur=blur,
            input_region=True,
            output_rebinding=False,
            layer_shell_reason=self._reason,
            blur_reason=None
            if blur
            else (
                self._blur.blur_disabled_reason
                if self._blur is not None
                else "Ordinary windows have no bridge to request compositor blur."
            ),
            output_rebinding_reason="Ordinary windows cannot rebind a mapped output.",
            client_positioning=self._client_positioning,
            client_positioning_reason=None
            if self._client_positioning
            else _NO_CLIENT_POSITIONING,
            window_opacity=self._window_opacity,
            window_opacity_reason=None if self._window_opacity else _NO_WINDOW_OPACITY,
        )

    def prepare(self) -> OverlayOperationResult:
        try:
            self._host.apply_window_policy(WindowPolicy(recreate_surface=True))
        except RuntimeError as exc:
            return OverlayOperationResult.failure(f"Window initialization failed: {exc}")
        return OverlayOperationResult.success()

    def activate(self) -> OverlayOperationResult:
        return OverlayOperationResult.success()

    def set_input_region(self, region: WindowRectangle | None) -> OverlayOperationResult:
        try:
            self._host.apply_window_policy(
                WindowPolicy(
                    does_not_accept_focus=region is None,
                    show_without_activating=region is None,
                    mouse_events_transparent=region is None,
                    recreate_surface=False,
                )
            )
            # The rectangle was ignored before: only the whole-window pass-through
            # switch was applied, so an unlocked ordinary window kept accepting
            # clicks across its entire transparent area and swallowed input meant
            # for whatever sits behind it. Click-through is carried by the policy
            # flag above, so the mask is cleared rather than set to nothing —
            # a shaping that fought the flag would be ambiguous.
            if region is None:
                self._host.clear_input_mask()
            else:
                self._host.set_input_mask(region)
            self._host.refresh()
        except RuntimeError as exc:
            return OverlayOperationResult.failure(f"Input mode update failed: {exc}")
        return OverlayOperationResult.success()

    def set_blur_region(self, region: WindowRectangle | None, radius: int = 0) -> OverlayOperationResult:
        # An ordinary window can still carry a compositor blur where the protocol
        # exists — Mutter has no Layer Shell and does speak it — so this is a real
        # operation here, not a permanent failure.
        capabilities = self.capabilities
        if not capabilities.blur or self._blur is None:
            return OverlayOperationResult.failure(capabilities.blur_reason or "Blur is unavailable.")
        pointer = self._host.native_window_pointer()
        if pointer is None:
            return OverlayOperationResult.failure("The window handle is unavailable.")
        try:
            if region is None:
                self._blur.clear_blur(pointer)
            else:
                self._blur.set_blur_region(pointer, region.x, region.y, region.width, region.height, radius)
        except (OSError, RuntimeError):
            return OverlayOperationResult.failure("Blur update failed.")
        return OverlayOperationResult.success()

    def move_to(self, position: WindowPoint) -> OverlayOperationResult:
        capabilities = self.capabilities
        if not capabilities.client_positioning:
            return OverlayOperationResult.failure(
                capabilities.client_positioning_reason or "This window cannot be positioned by the client."
            )
        try:
            self._host.move_window(position)
        except RuntimeError as exc:
            return OverlayOperationResult.failure(f"Window move failed: {exc}")
        return OverlayOperationResult.success()

    def rebind_output(self, output: WindowRectangle) -> OverlayOperationResult:
        del output
        return OverlayOperationResult.failure(
            self.capabilities.output_rebinding_reason or "Output rebinding is unavailable."
        )

    def set_output_handler(self, handler: Callable[[Output], bool]) -> None:
        del handler

    def set_active_output(self, output: Output | None) -> None:
        del output

    def move_to_output(self, output: Output) -> OverlayOperationResult:
        # A normal toplevel is not bound to an output, so there is nothing to rebuild.
        # It was recorded in an attribute nothing initialized and nothing read, which
        # only made the three siblings above look like they were forgetting something.
        del output
        return OverlayOperationResult.success()

    def output_removed(self, output: Output, connected: tuple[Output, ...], configured_name: str | None) -> None:
        del output, connected, configured_name

    def output_added(self, connected: tuple[Output, ...], configured_name: str | None) -> None:
        del connected, configured_name

    def begin_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> DragStartResult:
        return self._drag_strategy.begin_drag(local_position, global_position)

    def update_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> OverlayOperationResult:
        return self._drag_strategy.update_drag(local_position, global_position)

    def end_drag(self) -> None:
        self._drag_strategy.end_drag()
