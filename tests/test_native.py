"""Unit tests for LayerShellController's platform gates and runtime probe.

These cover the fallback logic that decides whether the overlay drives the
wlr-layer-shell bridge or degrades to a top-most ordinary window, without
needing a live Wayland compositor. The gate order under test:

    non-Wayland  ->  library found  ->  runtime probe

The compositor answers for itself: the probe decides whenever the bridge exports
it, and the GNOME desktop-name check is only the fallback for a bridge too old to
have the symbol. Background blur is gated separately, so the library stays loaded
past the layer-shell gate and a compositor without layer-shell can still frost
the panel.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kotonoha
from kotonoha.platform import native
from kotonoha.platform.native import LayerShellController, default_package_dir


class _FakeLib:
    """Stand-in for the ctypes CDLL returned by _load, for probe tests."""

    def __init__(self, *, has_layer_shell: bool | None, has_blur: bool | None = None) -> None:
        # None -> the symbol is absent entirely (older .so).
        if has_layer_shell is not None:
            self.koto_has_layer_shell = lambda: (1 if has_layer_shell else 0)
        if has_blur is not None:
            self.koto_has_blur = lambda: (1 if has_blur else 0)


@pytest.fixture
def stub_load(monkeypatch):
    """Make __init__ find a library and return a chosen fake lib from _load."""

    def _apply(lib: object) -> None:
        monkeypatch.setattr(native, "find_layer_shell_library", lambda _pkg: "/fake/libkoto-layer.so")
        monkeypatch.setattr(LayerShellController, "_load", staticmethod(lambda _path: lib))

    return _apply


def test_non_wayland_session_disables_before_touching_the_library(monkeypatch):
    # X11/xcb must bail out first: the .so would dlopen but every call no-ops on
    # an xcb surface, so we refuse it and take the top-most-window path instead.
    def _boom(_pkg):  # find_layer_shell_library must never be reached
        raise AssertionError("library lookup must not run on a non-Wayland session")

    monkeypatch.setattr(native, "find_layer_shell_library", _boom)
    ctl = LayerShellController("/pkg", "xcb", "KDE")
    assert ctl.available is False
    assert ctl.disabled_reason is not None
    assert "non-wayland" in ctl.disabled_reason.lower()


def test_gnome_wayland_is_disabled_by_the_name_check(stub_load):
    stub_load(_FakeLib(has_layer_shell=None))
    ctl = LayerShellController("/pkg", "wayland", "ubuntu:GNOME")
    assert ctl.available is False
    assert "gnome" in (ctl.disabled_reason or "").lower()


def test_gnome_wayland_keeps_the_bridge_for_background_blur(stub_load):
    # Mutter has no layer-shell but does speak ext-background-effect-v1, so the
    # library must stay loaded past the layer-shell gate for the frosted panel.
    stub_load(_FakeLib(has_layer_shell=None, has_blur=True))
    ctl = LayerShellController("/pkg", "wayland", "ubuntu:GNOME")
    assert ctl.available is False
    assert ctl.blur_available is True


def test_blur_needs_the_compositor_not_the_desktop_name(stub_load):
    # KDE with layer-shell but no blur protocol (Plasma 6.7 dropped the private
    # one): the frosted options must read as unavailable rather than silently
    # falling back to a bare translucent panel.
    stub_load(_FakeLib(has_layer_shell=True, has_blur=False))
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.available is True
    assert ctl.blur_available is False


def test_older_bridge_without_the_blur_symbol_reports_no_blur(stub_load):
    stub_load(_FakeLib(has_layer_shell=True))
    assert LayerShellController("/pkg", "wayland", "KDE").blur_available is False


def test_non_wayland_session_has_no_blur(monkeypatch):
    monkeypatch.setattr(native, "find_layer_shell_library", lambda _pkg: "/fake/libkoto-layer.so")
    assert LayerShellController("/pkg", "xcb", "KDE").blur_available is False


def test_missing_library_disables_with_a_hint(monkeypatch):
    monkeypatch.setattr(native, "find_layer_shell_library", lambda _pkg: None)
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.available is False
    assert "libkoto-layer.so" in (ctl.disabled_reason or "")


def test_runtime_probe_absent_protocol_disables(stub_load):
    # KDE name, library present, but the compositor does not advertise
    # zwlr_layer_shell_v1 -> degrade despite the desktop name.
    stub_load(_FakeLib(has_layer_shell=False))
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.available is False
    assert "layer-shell" in (ctl.disabled_reason or "").lower()


def test_runtime_probe_present_protocol_enables(stub_load):
    stub_load(_FakeLib(has_layer_shell=True))
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.available is True
    assert ctl.disabled_reason is None


def test_older_bridge_without_probe_symbol_still_loads(stub_load):
    # An older .so lacking koto_has_layer_shell must fall through to available,
    # relying on the name check that already passed above it.
    stub_load(_FakeLib(has_layer_shell=None))
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.available is True


def test_default_package_dir_prefers_the_loaded_source_dir(monkeypatch):
    # Regression: default_package_dir used to fall back to the site-packages
    # dir even when running from source, which could pick a stale RPM-installed
    # bridge built against a different Qt minor (ABI mismatch, layer-shell dead).
    # With PYTHONPATH=src the loaded package dir (the package root, holding the
    # freshly built bridge) must win over installed_dir.
    source_dir = str(Path(kotonoha.__file__).parent)

    def _pick(pkg: Path | str) -> str | None:
        if str(pkg) == source_dir:
            return source_dir + "/libkoto-layer.so"
        return None  # installed dir has no bridge

    monkeypatch.setattr(native, "find_layer_shell_library", _pick)
    assert native.default_package_dir() == source_dir


def test_default_package_dir_falls_back_when_source_has_no_bridge(monkeypatch, tmp_path):
    # If the loaded dir was, say, a bare checkout that hasn't built the bridge
    # yet, keep the old behavior of looking at the installed site-packages copy.
    source_dir = str(Path(kotonoha.__file__).parent)
    platlib_root = str(tmp_path / "site-packages")
    installed_dir = f"{platlib_root}/kotonoha"

    def _pick(pkg: Path) -> str | None:
        # Collapse both to strings for the value assertions below.
        if str(pkg) != source_dir and str(pkg) == installed_dir:
            return installed_dir + "/libkoto-layer.so"
        return None

    monkeypatch.setattr(native, "find_layer_shell_library", _pick)
    # default_package_dir computes installed_dir = platlib / package_name.
    monkeypatch.setattr(native.sysconfig, "get_path", lambda *a, **k: platlib_root)
    assert native.default_package_dir() == installed_dir


# The bridge is built into the package root, not beside this module: after the
# move that made the path platform/libkoto-layer.so, so the test skipped even
# on a full build and the Qt ABI handshake went uncovered.
_REAL_BRIDGE = Path(kotonoha.__file__).parent / "libkoto-layer.so"


@pytest.mark.skipif(not _REAL_BRIDGE.exists(), reason="native bridge not built")
def test_load_real_bridge_declares_argtypes_and_handshake():
    # Integration: the actually-built .so loads, exposes the ABI symbols, and
    # passes the Qt handshake against the running PyQt6.
    try:
        lib = LayerShellController._load(str(_REAL_BRIDGE))
    except OSError as exc:
        # A manually pip-installed PyQt6 (e.g. .venv-ruby) bundles libQt6Gui
        # that lacks Qt_<ver>_PRIVATE_API, which the *system* LayerShellQt
        # library dlopens; the loader then fails before our code runs. That is
        # an environment mismatch, not a regression. A distro/uv install pairs
        # one system Qt and passes. Skip so CI with a matching environment runs.
        if "not found" in str(exc) and "PRIVATE_API" in str(exc):
            pytest.skip("PyQt6's bundled Qt libs can't satisfy the system LayerShellQt (PyPI Qt mismatch)")
        raise
    assert lib.make_overlay.argtypes is not None
    assert hasattr(lib, "koto_layer_qt_version")
    assert hasattr(lib, "koto_has_layer_shell")
    assert hasattr(lib, "koto_has_blur")


def test_the_runtime_probe_outranks_the_desktop_name(stub_load):
    # A session branded GNOME that does advertise zwlr_layer_shell_v1 must get it.
    # The name check decided this before the probe ever ran, so the probe could not
    # overrule it — the capability came from the environment variable, not the
    # compositor.
    stub_load(_FakeLib(has_layer_shell=True))
    ctl = LayerShellController("/pkg", "wayland", "ubuntu:GNOME")
    assert ctl.available is True
    assert ctl.disabled_reason is None


def test_the_desktop_name_still_gates_a_bridge_without_the_probe(stub_load):
    # Older bridge, no symbol to ask: the name check is the only thing left.
    stub_load(_FakeLib(has_layer_shell=None))
    ctl = LayerShellController("/pkg", "wayland", "ubuntu:GNOME")
    assert ctl.available is False
    assert "gnome" in (ctl.disabled_reason or "").lower()


def test_blur_reports_which_cause_made_it_unavailable(stub_load):
    stub_load(_FakeLib(has_layer_shell=True, has_blur=False))
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.blur_available is False
    assert ctl.blur_disabled_reason == "protocol"

    stub_load(_FakeLib(has_layer_shell=True, has_blur=True))
    ready = LayerShellController("/pkg", "wayland", "KDE")
    assert ready.blur_available is True
    assert ready.blur_disabled_reason is None


def test_blur_availability_is_read_live(stub_load):
    # The capabilities event fires again when the answer changes; a cached Python
    # answer kept the UI wrong until the process restarted.
    lib = _FakeLib(has_layer_shell=True, has_blur=True)
    stub_load(lib)
    ctl = LayerShellController("/pkg", "wayland", "KDE")
    assert ctl.blur_available is True

    lib.koto_has_blur = lambda: 0  # the compositor withdrew it
    assert ctl.blur_available is False
    assert ctl.blur_disabled_reason == "protocol"


def test_a_non_wayland_session_is_not_reported_as_a_broken_bridge(stub_load):
    # X11 skips loading the bridge on purpose. Calling that a load failure told
    # every X11 user their install was broken.
    stub_load(_FakeLib(has_layer_shell=True))
    ctl = LayerShellController("/pkg", "xcb", "KDE")
    assert ctl.blur_available is False
    assert ctl.blur_disabled_reason == "session"


# conftest pins QT_QPA_PLATFORM to offscreen unless the caller set it, so this runs
# only when someone points it at a real session: QT_QPA_PLATFORM=wayland uv run pytest
@pytest.mark.skipif(
    not os.environ.get("WAYLAND_DISPLAY") or os.environ.get("QT_QPA_PLATFORM") != "wayland",
    reason="needs a live Wayland session (QT_QPA_PLATFORM=wayland); CI runs offscreen",
)
def test_blur_objects_do_not_accumulate_across_surface_rebuilds():
    """Live lifecycle check against whatever compositor is running.

    Unit tests use a fake library, so nothing here covered the real registry, the
    capability event, or what happens to a compositor-side object when its surface
    is destroyed. This drives real surfaces: the objects are keyed by wl_surface
    and a rebuilt surface gets a new address, so one left behind can never be found
    again and would show up as a rising count.
    """
    import PyQt6.sip as sip
    from PyQt6.QtWidgets import QApplication, QWidget

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    controller = LayerShellController(
        default_package_dir(), app.platformName(), os.environ.get("XDG_CURRENT_DESKTOP", "")
    )
    if controller.blur_object_count < 0:
        pytest.skip("this bridge does not report its blur objects")
    if not controller.blur_available:
        pytest.skip("this compositor advertises no blur protocol")

    widget = QWidget()
    widget.resize(200, 80)
    widget.show()
    app.processEvents()
    try:
        for _ in range(5):
            widget.winId()
            handle = widget.windowHandle()
            assert handle is not None
            pointer = sip.unwrapinstance(handle)
            assert pointer is not None
            controller.set_blur_region(pointer, 0, 0, 200, 80, 8)
            app.processEvents()
            assert controller.blur_object_count == 1, "one live surface should hold one blur object"
            controller.clear_blur(pointer)
            widget.hide()
            handle = widget.windowHandle()
            if handle is not None:
                handle.destroy()
            widget.show()
            app.processEvents()
        assert controller.blur_object_count == 0, "a rebuilt surface left its blur object behind"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
