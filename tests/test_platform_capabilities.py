"""Capability values mirror the controller's existing platform probes."""

from __future__ import annotations

import pytest

from kotonoha.platform import native
from kotonoha.platform.native import LayerShellController


class _FakeLib:
    def __init__(self, *, has_layer_shell: bool | None, has_blur: bool | None = None) -> None:
        if has_layer_shell is not None:
            self.koto_has_layer_shell = lambda: int(has_layer_shell)
        if has_blur is not None:
            self.koto_has_blur = lambda: int(has_blur)


@pytest.mark.parametrize(
    ("platform_name", "desktop", "library", "expected_layer_shell", "expected_blur"),
    [
        ("xcb", "KDE", _FakeLib(has_layer_shell=True, has_blur=True), False, False),
        ("wayland", "ubuntu:GNOME", _FakeLib(has_layer_shell=None, has_blur=True), False, True),
        ("wayland", "KDE", _FakeLib(has_layer_shell=True, has_blur=False), True, False),
        ("wayland", "KDE", _FakeLib(has_layer_shell=None), True, False),
        ("wayland", "KDE", _FakeLib(has_layer_shell=False, has_blur=True), False, True),
    ],
)
def test_capabilities_match_controller_booleans(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    desktop: str,
    library: _FakeLib,
    expected_layer_shell: bool,
    expected_blur: bool,
) -> None:
    monkeypatch.setattr(native, "find_layer_shell_library", lambda _pkg: "/fake/libkoto-layer.so")
    monkeypatch.setattr(LayerShellController, "_load", staticmethod(lambda _path: library))

    controller = LayerShellController("/pkg", platform_name, desktop)
    capabilities = controller.capabilities

    assert capabilities.layer_shell is controller.available is expected_layer_shell
    assert capabilities.blur is controller.blur_available is expected_blur
    assert capabilities.layer_shell_reason == controller.disabled_reason
    assert capabilities.layer_shell_reason == controller.disabled_reason
    # The cause the controller reports, not a sentence invented here: the UI needs
    # to tell a non-Wayland session apart from a compositor with no blur protocol.
    assert capabilities.blur_reason == controller.blur_disabled_reason
    assert (capabilities.blur_reason is None) is expected_blur


def test_capabilities_match_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "find_layer_shell_library", lambda _pkg: None)

    controller = LayerShellController("/pkg", "wayland", "KDE")

    assert controller.capabilities == type(controller.capabilities)(
        layer_shell=False,
        blur=False,
        layer_shell_reason=controller.disabled_reason,
        # The library never loaded, which is a different thing to tell the user than
        # a compositor that simply has no blur protocol.
        blur_reason="bridge",
        # Both ride on Layer Shell, so the UI can say why they are off too.
        input_region_reason=controller.disabled_reason,
        output_rebinding_reason=controller.disabled_reason,
    )
