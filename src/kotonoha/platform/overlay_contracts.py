"""Contracts describing platform features available to the overlay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass

@dataclass(frozen=True, slots=True)
class OverlayCapabilities:
    """Platform features and the reasons individual features are unavailable."""

    layer_shell: bool
    blur: bool
    input_region: bool = False
    output_rebinding: bool = False
    layer_shell_reason: str | None = None
    blur_reason: str | None = None
    input_region_reason: str | None = None
    output_rebinding_reason: str | None = None
    # Whether the client can place its own window. Layer Shell positions by anchor
    # and X11 by the window manager; a Wayland compositor without Layer Shell
    # ignores a client-side move of a toplevel, and no readback can tell — Qt
    # reports the requested position either way, measured on KWin — so this is
    # stated from the protocol rather than observed.
    client_positioning: bool = True
    client_positioning_reason: str | None = None
    # Whether setting the window's opacity does anything. Wayland has no client-side
    # window-opacity protocol, so animating it there only logs "plugin does not
    # support setting window opacity" once per frame. Which session this is remains a
    # platform fact: presentation asks, the adapter answers.
    window_opacity: bool = True
    window_opacity_reason: str | None = None

    @classmethod
    def from_controller(cls, controller: LayerShellBridge, *, window_opacity: bool = True) -> OverlayCapabilities:
        layer_shell = controller.available
        blur = controller.blur_available
        return cls(
            layer_shell=layer_shell,
            blur=blur,
            input_region=layer_shell,
            output_rebinding=layer_shell,
            layer_shell_reason=controller.disabled_reason,
            # The controller already reports which cause it is — session, bridge,
            # protocol or build — and the UI translates that. Replacing it with one
            # sentence here would collapse four distinct situations into one.
            blur_reason=controller.blur_disabled_reason,
            # Both ride on Layer Shell, so they are unavailable for the same reason
            # it is. Leaving these None gave the UI a disabled capability it could
            # not explain, which is the one thing this value object exists to avoid.
            input_region_reason=None if layer_shell else (controller.disabled_reason or "Layer Shell is unavailable."),
            output_rebinding_reason=None
            if layer_shell
            else (controller.disabled_reason or "Layer Shell is unavailable."),
            window_opacity=window_opacity,
            window_opacity_reason=None if window_opacity else _NO_WINDOW_OPACITY,
        )


_NO_WINDOW_OPACITY = "Wayland has no client-side window-opacity protocol."


@dataclass(frozen=True, slots=True)
class WindowPoint:
    """A screen or window-local point without a GUI toolkit dependency."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class WindowRectangle:
    """A rectangle used for window and output geometry."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Output:
    """A connected output described without a GUI toolkit dependency."""

    name: str
    geometry: WindowRectangle


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    """Toolkit-neutral window flags and input attributes."""

    transparent_for_input: bool = False
    does_not_accept_focus: bool = False
    show_without_activating: bool = False
    mouse_events_transparent: bool = False
    recreate_surface: bool = True


@dataclass(frozen=True, slots=True)
class OverlayOperationResult:
    """Result of a platform operation, including an actionable failure reason."""

    succeeded: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.succeeded and self.reason is not None:
            raise ValueError("Successful results cannot contain a failure reason")
        if not self.succeeded and not self.reason:
            raise ValueError("Failed results must contain a reason")

    @classmethod
    def success(cls) -> OverlayOperationResult:
        return cls(succeeded=True)

    @classmethod
    def failure(cls, reason: str) -> OverlayOperationResult:
        return cls(succeeded=False, reason=reason)


class DragMode(Enum):
    """Movement mechanism selected for one press gesture."""

    SYSTEM = "system"
    MANUAL = "manual"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DragStartResult:
    """Result of starting a drag."""

    mode: DragMode
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode is DragMode.UNAVAILABLE and not self.reason:
            raise ValueError("Unavailable drag results must include a reason")
        if self.mode is not DragMode.UNAVAILABLE and self.reason is not None:
            raise ValueError("Available drag results cannot include a failure reason")


class WindowHost(Protocol):
    """Toolkit-neutral surface used by platform adapters."""

    def apply_window_policy(self, policy: WindowPolicy) -> None: ...
    def is_alive(self) -> bool: ...
    def native_window_pointer(self) -> int | None: ...
    def geometry(self) -> WindowRectangle: ...
    def window_position(self) -> WindowPoint | None: ...
    def screen_geometry(self) -> WindowRectangle | None: ...
    def bind_output(self, output: WindowRectangle) -> None: ...
    def hide_window(self) -> None: ...
    def destroy_surface(self) -> None: ...
    def move_window(self, position: WindowPoint) -> None: ...
    # Shape where an ordinary window accepts input. Two calls rather than one
    # nullable argument: the public operation uses None to mean "click through
    # everywhere", and a mask of None conventionally means "no shaping at all",
    # which is its opposite. Keeping them apart leaves nothing to interpret.
    def set_input_mask(self, region: WindowRectangle) -> None: ...
    def clear_input_mask(self) -> None: ...
    def refresh(self) -> None: ...


class OverlayDragStrategy(Protocol):
    """Optional strategy boundary for platform-specific dragging."""

    def begin_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> DragStartResult: ...
    def update_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> OverlayOperationResult: ...
    def end_drag(self) -> None: ...
    def set_position(self, position: WindowPoint) -> None: ...


class OverlayPlatform(Protocol):
    """Platform capability and lifecycle contract used by the overlay widget."""

    @property
    def capabilities(self) -> OverlayCapabilities: ...
    def prepare(self) -> OverlayOperationResult: ...
    def activate(self) -> OverlayOperationResult: ...
    def set_input_region(self, region: WindowRectangle | None) -> OverlayOperationResult: ...
    def set_blur_region(self, region: WindowRectangle | None, radius: int = 0) -> OverlayOperationResult: ...
    def move_to(self, position: WindowPoint) -> OverlayOperationResult: ...
    def rebind_output(self, output: WindowRectangle) -> OverlayOperationResult: ...
    # The handler returns whether a surface was actually rebuilt: activation can
    # fail on the returning output, and retiring the pending rebuild then leaves
    # the overlay hidden with nothing to try again.
    def set_output_handler(self, handler: Callable[[Output], bool]) -> None: ...
    def set_active_output(self, output: Output | None) -> None: ...
    def move_to_output(self, output: Output) -> OverlayOperationResult: ...
    def output_removed(self, output: Output, connected: tuple[Output, ...], configured_name: str | None) -> None: ...
    def output_added(self, connected: tuple[Output, ...], configured_name: str | None) -> None: ...
    def begin_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> DragStartResult: ...
    def update_drag(self, local_position: WindowPoint, global_position: WindowPoint) -> OverlayOperationResult: ...
    def end_drag(self) -> None: ...


class OverlayPlatformFactory(Protocol):
    """Factory for an adapter bound to one window host."""

    def __call__(self, host: WindowHost) -> OverlayPlatform: ...


class LayerShellBridge(Protocol):
    """What an overlay adapter needs from the native bridge.

    Adapters and the provider registry depend on this rather than on
    ``LayerShellController`` itself, so a session can be described in a test
    without constructing the real ctypes wrapper."""

    @property
    def available(self) -> bool: ...

    @property
    def blur_available(self) -> bool: ...

    @property
    def disabled_reason(self) -> str | None: ...

    @property
    def blur_disabled_reason(self) -> str | None: ...

    def make_overlay(self, window_ptr: int) -> None: ...

    def set_passthrough(self, window_ptr: int, enabled: bool) -> None: ...

    def set_input_rect(self, window_ptr: int, x: int, y: int, w: int, h: int) -> None: ...

    def set_anchor_position(self, window_ptr: int, x: int, y: int) -> None: ...

    def set_blur_region(self, window_ptr: int, x: int, y: int, w: int, h: int, radius: int) -> None: ...

    def clear_blur(self, window_ptr: int) -> None: ...
