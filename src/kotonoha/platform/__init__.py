"""Platform integration for overlay capabilities and native bridges."""

from .layer_shell import LayerShellPlatform
from .native import LayerShellController, default_package_dir
from .overlay_contracts import (
    OverlayCapabilities,
    OverlayOperationResult,
    OverlayPlatform,
    OverlayPlatformFactory,
    WindowHost,
    WindowPoint,
    WindowPolicy,
    WindowRectangle,
)
from .qt_host import QtWindowHost
from .window_platform import DefaultOverlayPlatformFactory

__all__ = [
    "DefaultOverlayPlatformFactory",
    "LayerShellController",
    "OverlayCapabilities",
    "OverlayOperationResult",
    "OverlayPlatform",
    "OverlayPlatformFactory",
    "WindowHost",
    "WindowPoint",
    "WindowPolicy",
    "WindowRectangle",
    "QtWindowHost",
    "LayerShellPlatform",
    "default_package_dir",
]
