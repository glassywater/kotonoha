"""Ordered overlay-platform provider selection and output lifecycle."""

from __future__ import annotations

from kotonoha.platform.layer_shell import LayerShellAnchorDragStrategy, LayerShellPlatform, NiriLayerShellDragStrategy
from kotonoha.platform.overlay_contracts import DragMode, Output, WindowPoint, WindowPolicy, WindowRectangle
from kotonoha.platform.qt_window import OrdinaryWindowDragStrategy, QtWindowPlatform
from kotonoha.platform.window_platform import DefaultOverlayPlatformFactory, _LayerShellProvider


class _FakeController:
    """Stands in for the native bridge, so a session can be described without ctypes."""

    def __init__(self, available: bool, blur_available: bool = False) -> None:
        self.available = available
        self.blur_available = blur_available
        self.disabled_reason = None if available else "Fake compositor rejected Layer Shell."
        self.blur_disabled_reason = None if blur_available else "protocol"
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def make_overlay(self, window_ptr: int) -> None:
        self.calls.append(("make_overlay", (window_ptr,)))

    def set_passthrough(self, window_ptr: int, enabled: bool) -> None:
        self.calls.append(("set_passthrough", (window_ptr, int(enabled))))

    def set_input_rect(self, window_ptr: int, x: int, y: int, w: int, h: int) -> None:
        self.calls.append(("set_input_rect", (window_ptr, x, y, w, h)))

    def set_anchor_position(self, window_ptr: int, x: int, y: int) -> None:
        self.calls.append(("set_anchor_position", (window_ptr, x, y)))

    def set_blur_region(self, window_ptr: int, x: int, y: int, w: int, h: int, radius: int) -> None:
        self.calls.append(("set_blur_region", (window_ptr, x, y, w, h, radius)))

    def clear_blur(self, window_ptr: int) -> None:
        self.calls.append(("clear_blur", (window_ptr,)))


class _FakeHost:
    def __init__(self) -> None:
        self.masks: list[object] = []
        self.policies: list[WindowPolicy] = []
        self.lifecycle: list[str] = []
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def apply_window_policy(self, policy: WindowPolicy) -> None:
        self.policies.append(policy)

    def set_input_mask(self, region: WindowRectangle) -> None:
        self.masks.append(region)

    def clear_input_mask(self) -> None:
        self.masks.append("cleared")

    def native_window_pointer(self) -> int | None:
        return 1

    def geometry(self) -> WindowRectangle:
        return WindowRectangle(0, 0, 100, 50)

    def window_position(self) -> WindowPoint | None:
        return WindowPoint(0, 0)

    def screen_geometry(self) -> WindowRectangle | None:
        return WindowRectangle(0, 0, 1920, 1080)

    def bind_output(self, output: WindowRectangle) -> None:
        del output

    def hide_window(self) -> None:
        self.lifecycle.append("hide")

    def destroy_surface(self) -> None:
        self.lifecycle.append("destroy")

    def move_window(self, position: WindowPoint) -> None:
        del position

    def refresh(self) -> None:
        pass


class _MovingHost(_FakeHost):
    def __init__(self) -> None:
        self.position = WindowPoint(100, 200)
        self.moves: list[WindowPoint] = []

    def window_position(self) -> WindowPoint:
        return self.position

    def move_window(self, position: WindowPoint) -> None:
        self.position = position
        self.moves.append(position)


def _assert_measures_global_pointer(platform, controller) -> None:
    """The niri model: the surface follows the global pointer reading.

    The two Layer Shell strategies differ in exactly which reading they measure the
    displacement from, and that is observable — hold the local position still and
    move the global one, and only this model commits a new anchor. Asserting the
    concrete strategy object would tie the test to a private field instead.
    """
    platform.begin_drag(WindowPoint(10, 10), WindowPoint(110, 210))
    platform.update_drag(WindowPoint(10, 10), WindowPoint(115, 213))
    anchors = [call for call in controller.calls if call[0] == "set_anchor_position"]
    assert anchors and anchors[-1][1][1:] == (5, 3), f"global displacement was not applied: {anchors}"


def _assert_measures_local_pointer(platform, controller) -> None:
    """The default model: the surface follows the press-relative local reading."""
    platform.begin_drag(WindowPoint(10, 10), WindowPoint(110, 210))
    platform.update_drag(WindowPoint(10, 10), WindowPoint(115, 213))
    anchors = [call for call in controller.calls if call[0] == "set_anchor_position"]
    assert anchors and anchors[-1][1][1:] == (0, 0), f"a still pointer moved the surface: {anchors}"


def test_provider_order_selects_layer_shell_before_fallbacks() -> None:
    platform = DefaultOverlayPlatformFactory(
        _FakeController(available=True, blur_available=True), platform_name="wayland", current_desktop="KDE"
    )(_FakeHost())

    assert isinstance(platform, LayerShellPlatform)
    assert platform.capabilities.layer_shell
    assert platform.capabilities.blur


def test_x11_provider_claims_without_layer_shell() -> None:
    platform = DefaultOverlayPlatformFactory(_FakeController(False), platform_name="xcb")(_FakeHost())

    assert isinstance(platform, QtWindowPlatform)
    assert not platform.capabilities.layer_shell
    assert platform.capabilities.layer_shell_reason == "X11 has no Layer Shell overlay capability."


def test_wayland_fallback_explains_rejected_layer_shell() -> None:
    platform = DefaultOverlayPlatformFactory(
        _FakeController(False), platform_name="wayland", current_desktop="GNOME"
    )(_FakeHost())

    assert isinstance(platform, QtWindowPlatform)
    assert platform.capabilities.layer_shell_reason == "Wayland compositor does not provide Layer Shell."
    # Blur is a separate capability, so the reason comes from the bridge rather
    # than from the window being an ordinary one.
    assert platform.capabilities.blur is False
    assert platform.capabilities.blur_reason


def test_a_wayland_fallback_keeps_blur_when_the_compositor_offers_it() -> None:
    # Mutter has no Layer Shell and does speak a blur protocol. Hardcoding blur off
    # in the fallback dropped the frosted panel on exactly that compositor.
    controller = _FakeController(False, blur_available=True)
    platform = DefaultOverlayPlatformFactory(
        controller, platform_name="wayland", current_desktop="GNOME"
    )(_FakeHost())

    assert isinstance(platform, QtWindowPlatform)
    assert platform.capabilities.blur is True
    assert platform.capabilities.blur_reason is None

    result = platform.set_blur_region(WindowRectangle(0, 0, 10, 10), 4)

    assert result.succeeded, result.reason
    assert any(call[0] == "set_blur_region" for call in controller.calls)


def test_generic_provider_claims_unknown_platform_with_reason() -> None:
    platform = DefaultOverlayPlatformFactory(_FakeController(False), platform_name="offscreen")(_FakeHost())

    assert isinstance(platform, QtWindowPlatform)
    assert platform.capabilities.layer_shell_reason == "Layer Shell is unavailable on this platform."


def test_the_fallback_shapes_its_input_region_to_the_rectangle() -> None:
    # Only the whole-window pass-through switch was applied before, so an unlocked
    # ordinary window kept accepting clicks across its whole transparent area and
    # swallowed input meant for the window behind it.
    host = _FakeHost()
    platform = QtWindowPlatform(host, reason="no Layer Shell here")

    assert platform.set_input_region(WindowRectangle(4, 6, 40, 20)).succeeded

    # Unlocked: input is confined to the rectangle, and the window is not made
    # transparent to the pointer.
    assert host.masks == [WindowRectangle(4, 6, 40, 20)]
    assert host.policies[-1].mouse_events_transparent is False

    assert platform.set_input_region(None).succeeded

    # Locked: click-through is carried by the policy flag, and the shaping is
    # cleared rather than set to nothing — the two must not disagree.
    assert host.masks[-1] == "cleared"
    assert host.policies[-1].mouse_events_transparent is True


def test_layer_shell_operations_report_failure_when_the_capability_is_off() -> None:
    # The bridge no-ops silently when Layer Shell is unavailable, so reporting
    # success told the caller an update had happened that had not.
    platform = LayerShellPlatform(_FakeHost(), _FakeController(available=False))

    for result in (
        platform.set_input_region(WindowRectangle(0, 0, 10, 10)),
        platform.move_to(WindowPoint(1, 2)),
        platform.rebind_output(WindowRectangle(0, 0, 800, 600)),
    ):
        assert not result.succeeded
        assert result.reason


def test_a_wayland_fallback_reports_that_it_cannot_place_its_own_window() -> None:
    # Wayland gives a client no way to place its own toplevel, and no readback can
    # tell: measured on KWin, Qt reports the requested position whether or not the
    # compositor applied it. So this is stated from the protocol. Reporting success
    # let the caller persist a position the visible window never took.
    platform = DefaultOverlayPlatformFactory(
        _FakeController(False), platform_name="wayland", current_desktop="GNOME"
    )(_FakeHost())

    assert platform.capabilities.client_positioning is False
    result = platform.move_to(WindowPoint(120, 40))

    assert not result.succeeded
    assert result.reason


def test_an_x11_fallback_can_place_its_own_window() -> None:
    # A window manager honours a client move on X11, so the same adapter reports
    # the opposite there.
    platform = DefaultOverlayPlatformFactory(
        _FakeController(False), platform_name="xcb", current_desktop="KDE"
    )(_FakeHost())

    assert platform.capabilities.client_positioning is True
    assert platform.move_to(WindowPoint(120, 40)).succeeded
def test_layer_shell_registry_selects_and_exercises_anchor_strategy() -> None:
    host = _MovingHost()
    controller = _FakeController(available=True)
    platform = DefaultOverlayPlatformFactory(controller, platform_name="wayland", current_desktop="KDE")(host)

    assert isinstance(platform, LayerShellPlatform)
    # The anchor call below is what distinguishes this strategy from the ordinary
    # one, which moves the window instead. Asserting the concrete strategy object
    # would only restate the selection the behaviour already proves.
    assert platform.begin_drag(WindowPoint(10, 10), WindowPoint(110, 210)).mode is DragMode.MANUAL
    assert platform.update_drag(WindowPoint(15, 13), WindowPoint(115, 213)).succeeded
    assert controller.calls[-1] == ("set_anchor_position", (1, 5, 3))
    platform.end_drag()


def test_layer_shell_registry_selects_niri_strategy() -> None:
    host = _MovingHost()
    controller = _FakeController(available=True)
    platform = DefaultOverlayPlatformFactory(controller, platform_name="wayland", current_desktop="niri")(host)

    assert isinstance(platform, LayerShellPlatform)
    _assert_measures_global_pointer(platform, controller)


def test_layer_shell_registry_keeps_default_strategy_for_kde() -> None:
    host = _MovingHost()
    controller = _FakeController(available=True)
    # The session is stated, not inherited: with NIRI_SOCKET exported by whoever
    # launched the suite, this used to select niri's model and fail.
    platform = DefaultOverlayPlatformFactory(
        controller, platform_name="wayland", current_desktop="KDE", niri_socket_present=False
    )(host)

    assert isinstance(platform, LayerShellPlatform)
    _assert_measures_local_pointer(platform, controller)


def test_layer_shell_registry_selects_niri_from_session_desktop(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.setenv("XDG_SESSION_DESKTOP", "niri")
    controller = _FakeController(available=True)
    platform = DefaultOverlayPlatformFactory(controller, platform_name="wayland")(_MovingHost())

    assert isinstance(platform, LayerShellPlatform)
    _assert_measures_global_pointer(platform, controller)


def test_niri_strategy_integrates_global_pointer_displacement() -> None:
    host = _MovingHost()
    controller = _FakeController(available=True)
    strategy = NiriLayerShellDragStrategy(host, controller)
    strategy.set_position(WindowPoint(100, 200))

    assert strategy.begin_drag(WindowPoint(10, 10), WindowPoint(110, 210)).mode is DragMode.MANUAL
    assert strategy.update_drag(WindowPoint(10, 10), WindowPoint(115, 213)).succeeded
    assert controller.calls[-1] == ("set_anchor_position", (1, 105, 203))
    assert strategy.update_drag(WindowPoint(99, 99), WindowPoint(108, 205)).succeeded
    assert controller.calls[-1] == ("set_anchor_position", (1, 98, 195))
    strategy.end_drag()


def test_ordinary_window_strategy_moves_from_local_anchor() -> None:
    host = _MovingHost()
    strategy = OrdinaryWindowDragStrategy(host)

    assert strategy.begin_drag(WindowPoint(10, 10), WindowPoint(110, 210)).mode is DragMode.MANUAL
    assert strategy.update_drag(WindowPoint(25, 17), WindowPoint(125, 217)).succeeded
    assert host.moves == [WindowPoint(115, 207)]
    strategy.end_drag()


def test_anchor_drag_does_not_oscillate_when_the_surface_follows_the_pointer() -> None:
    # On a compositor that applies the move immediately, the surface follows the
    # pointer, so the pointer's local position re-settles to where the press
    # landed. The anchor must stay at that press point: advancing it makes the
    # next settled report read as an equal and opposite delta, which is the
    # jitter-then-runaway drag #7 and #9 were about.
    controller = _FakeController(available=True)
    strategy = LayerShellAnchorDragStrategy(_FakeHost(), controller)
    strategy.set_position(WindowPoint(100, 100))
    strategy.begin_drag(WindowPoint(10, 10), WindowPoint(0, 0))

    strategy.update_drag(WindowPoint(30, 10), WindowPoint(0, 0))   # pointer moves right
    for _ in range(3):                                             # surface caught up
        strategy.update_drag(WindowPoint(10, 10), WindowPoint(0, 0))

    moves = [(x, y) for name, (_ptr, x, y) in controller.calls if name == "set_anchor_position"]
    assert moves[0] == (120, 100), "the first delta should move the surface"
    assert all(move == (120, 100) for move in moves[1:]), f"surface oscillated: {moves}"


def test_the_ordinary_window_drag_measures_every_delta_from_the_press_point() -> None:
    # The window follows the pointer, so the pointer's local position re-settles
    # toward where the press landed. Advancing that anchor counts the settling
    # twice: after one move the window snapped back to where it started.
    host = _RecordingHost()
    strategy = OrdinaryWindowDragStrategy(host)
    strategy.set_position(WindowPoint(100, 100))
    strategy.begin_drag(WindowPoint(10, 10), WindowPoint(0, 0))

    strategy.update_drag(WindowPoint(30, 10), WindowPoint(0, 0))   # pointer moves right
    for _ in range(3):                                            # window caught up
        strategy.update_drag(WindowPoint(10, 10), WindowPoint(0, 0))

    assert host.moves[0] == WindowPoint(120, 100), "the first delta should move the window"
    assert all(move == WindowPoint(120, 100) for move in host.moves[1:]), f"window oscillated: {host.moves}"


class _RecordingHost(_FakeHost):
    def __init__(self, position: WindowPoint | None = None) -> None:
        super().__init__()
        self.moves: list[WindowPoint] = []
        self._position = position or WindowPoint(100, 100)

    def window_position(self) -> WindowPoint | None:
        return self._position

    def move_window(self, position: WindowPoint) -> None:
        self.moves.append(position)


def test_a_wayland_fallback_drag_reports_that_nothing_moved() -> None:
    # Reported success on a compositor that ignores the move is the same defect as
    # move_to reporting it, and the drag path had its own route to the host.
    host = _RecordingHost()
    platform = QtWindowPlatform(host, client_positioning=False)

    platform.begin_drag(WindowPoint(10, 10), WindowPoint(0, 0))
    result = platform.update_drag(WindowPoint(30, 10), WindowPoint(0, 0))

    assert not result.succeeded
    assert result.reason == platform.capabilities.client_positioning_reason
    assert host.moves == [], "the window must not be moved when the compositor ignores it"

def _output(name: str, width: int = 1920) -> Output:
    return Output(name, WindowRectangle(0, 0, width, 1080))


def test_layer_shell_ignores_vanishing_output_that_is_not_active() -> None:
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    platform.set_active_output(active)

    platform.output_removed(_output("DP-1"), (), None)

    assert host.lifecycle == []


def test_layer_shell_rebuilds_on_returning_output_after_release() -> None:
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    restored: list[Output] = []
    platform.set_active_output(active)
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)
    platform.output_removed(active, (), None)
    platform._resurface_timer.setInterval(0)
    platform.output_added((active,), None)
    platform._resurface_timer.timeout.emit()

    assert host.lifecycle == ["hide", "destroy"]
    assert restored == [active]


def test_layer_shell_configured_output_wins_when_outputs_return() -> None:
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    wanted = _output("DP-2", 5120)
    other = _output("HDMI-A-2")
    restored: list[Output] = []
    platform.set_active_output(active)
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)
    platform.output_removed(active, (), None)
    platform._resurface_timer.setInterval(0)
    platform.output_added((other, wanted), "DP-2")
    platform._resurface_timer.timeout.emit()

    assert restored == [wanted]


def test_layer_shell_falls_back_to_output_still_connected() -> None:
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    lost = _output("DP-2")
    live = _output("HDMI-A-1")
    restored: list[Output] = []
    platform.set_active_output(lost)
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)
    platform.output_removed(lost, (live,), None)
    platform._resurface_timer.timeout.emit()

    assert restored == [live]


def test_qt_window_output_events_are_explicitly_ignored() -> None:
    platform = QtWindowPlatform(_FakeHost())
    platform.output_removed(_output("DP-1"), (), None)
    platform.output_added((_output("DP-1"),), "DP-1")


def test_the_blur_object_is_released_before_its_surface_is_destroyed() -> None:
    # The bridge keys the compositor-side effect on the wl_surface. A rebuilt
    # surface gets a new address, so one left behind can never be found again and
    # leaks for the life of the process — once per output change.
    host = _FakeHost()
    controller = _FakeController(available=True, blur_available=True)
    platform = LayerShellPlatform(host, controller)
    active = _output("HDMI-A-1")
    platform.set_active_output(active)

    platform.output_removed(active, (), None)

    cleared = [call for call in controller.calls if call[0] == "clear_blur"]
    assert cleared, f"the surface was destroyed with its effect still registered: {controller.calls}"
    assert host.lifecycle.index("destroy") > 0


def test_moving_to_another_output_rebuilds_the_surface() -> None:
    # A layer surface binds its output when it is created, so a drag released on
    # another monitor must destroy it and build a new one there. Recording the
    # output alone leaves the panel drawn on the output it was dragged away from.
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    restored: list[Output] = []
    platform.set_active_output(_output("HDMI-A-1"))
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)

    target = _output("DP-1", 2560)
    result = platform.move_to_output(target)

    assert result.succeeded
    assert host.lifecycle == ["hide", "destroy"]
    assert restored == [target]


def test_a_returning_output_does_not_rebuild_a_closed_overlay() -> None:
    # The rebuild is deferred by a timer, so an output can come back after the
    # overlay is gone. Calling the handler then reaches a deleted widget, which is
    # how this project has produced segfaults before.
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    restored: list[Output] = []
    platform.set_active_output(active)
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)
    platform.output_removed(active, (), None)
    platform.output_added((active,), None)

    host.alive = False  # the overlay closed while the rebuild was pending
    platform._resurface_timer.timeout.emit()

    assert restored == []


def test_a_second_output_vanishing_before_the_rebuild_leaves_one_owed() -> None:
    # Two outputs going away in quick succession: the first removal schedules a
    # rebuild on the survivor, and the survivor disappears inside the delay. The
    # scheduled rebuild then finds nothing to build on, and without a record that
    # one is still owed the overlay never comes back. Reported in review on #19.
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    survivor = _output("DP-1")
    restored: list[Output] = []
    platform.set_active_output(active)
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)

    platform.output_removed(active, (survivor,), None)   # schedules a rebuild on DP-1
    platform.output_removed(survivor, (), None)          # DP-1 goes too, before the timer
    platform._resurface_timer.timeout.emit()             # the scheduled rebuild runs

    assert restored == [], "rebuilt on an output that is gone"

    platform.output_added((active,), None)               # something comes back
    platform._resurface_timer.timeout.emit()

    assert restored == [active], "nothing remembered that a rebuild was owed"


def test_a_returning_output_that_cannot_be_rebuilt_stays_owed() -> None:
    # Activation can fail on the returning output. Retiring the pending rebuild
    # then leaves the overlay hidden with nothing that will try again.
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    platform.set_active_output(active)
    platform.set_output_handler(lambda output: False)  # nothing was rebuilt

    platform.output_removed(active, (), None)
    platform.output_added((active,), None)
    platform._resurface_timer.timeout.emit()

    assert platform._pending_resurface is True

    rebuilt: list[Output] = []
    platform.set_output_handler(lambda output: bool(rebuilt.append(output)) or True)
    platform.output_added((active,), None)
    platform._resurface_timer.timeout.emit()

    assert rebuilt == [active]
    assert platform._pending_resurface is False


def test_the_blur_object_is_released_even_when_blur_arrived_after_startup() -> None:
    # The release consulted a construction-time snapshot while the capability is a
    # live probe by contract: a compositor that gained the blur protocol after
    # startup had its effect object destroyed with the surface it was keyed on, and
    # a compositor that withdrew it would refuse the release for the same reason.
    host = _FakeHost()
    controller = _FakeController(available=True, blur_available=False)
    platform = LayerShellPlatform(host, controller)
    active = _output("HDMI-A-1")
    platform.set_active_output(active)
    controller.blur_available = True

    platform.output_removed(active, (), None)

    assert [call for call in controller.calls if call[0] == "clear_blur"], (
        f"the effect outlived the surface it was keyed on: {controller.calls}"
    )


def test_a_failed_output_move_stays_owed_so_a_later_event_retries() -> None:
    # move_to_output clears the debt and records the target before destroying the
    # old surface. When the rebuild then failed, nothing was owed and the active
    # output already matched, so the next output event returned early and the
    # overlay stayed hidden for the rest of the session.
    host = _FakeHost()
    platform = LayerShellPlatform(host, _FakeController(available=True))
    active = _output("HDMI-A-1")
    target = _output("DP-1")
    platform.set_active_output(active)
    platform.set_output_handler(lambda _output: False)

    result = platform.move_to_output(target)

    assert not result.succeeded
    assert host.lifecycle == ["hide", "destroy"], "the old surface is already gone"

    restored: list[Output] = []
    platform.set_output_handler(lambda output: bool(restored.append(output)) or True)
    platform._resurface_timer.setInterval(0)
    platform.output_added((active, target), None)
    platform._resurface_timer.timeout.emit()

    assert restored, "a destroyed surface was never rebuilt"


def test_a_wayland_session_reports_that_window_opacity_does_nothing() -> None:
    # Wayland has no client-side window-opacity protocol, so setting it only logs
    # "plugin does not support setting window opacity" once per frame. The settings
    # window used to decide that from the Qt platform name itself.
    layer_shell = DefaultOverlayPlatformFactory(
        _FakeController(available=True), platform_name="wayland", current_desktop="KDE"
    )(_FakeHost())
    fallback = DefaultOverlayPlatformFactory(
        _FakeController(False), platform_name="wayland", current_desktop="GNOME"
    )(_FakeHost())
    x11 = DefaultOverlayPlatformFactory(_FakeController(False), platform_name="xcb")(_FakeHost())

    assert layer_shell.capabilities.window_opacity is False
    assert fallback.capabilities.window_opacity is False
    assert layer_shell.capabilities.window_opacity_reason
    assert x11.capabilities.window_opacity is True
    assert x11.capabilities.window_opacity_reason is None


def test_the_settings_window_gets_the_same_adapter_the_session_selects() -> None:
    # The settings window was handed a Layer Shell adapter unconditionally, so on
    # X11 it reported no window opacity — a Wayland fact — and dropped its own
    # fade-in. It is a window on the same session as the overlay, and the registry
    # is what knows which session that is.
    controller = _FakeController(available=False)

    settings = DefaultOverlayPlatformFactory(controller, platform_name="xcb")(_FakeHost())

    assert isinstance(settings, QtWindowPlatform)
    assert settings.capabilities.window_opacity is True


def test_layer_shell_registry_selects_niri_from_its_socket() -> None:
    # niri sets XDG_CURRENT_DESKTOP=niri only when it runs as a session; started
    # nested or without the session wrapper it leaves the parent's value, so a real
    # niri can present itself as KDE. It always exports NIRI_SOCKET to its clients.
    controller = _FakeController(available=True)
    host = _FakeHost()

    platform = DefaultOverlayPlatformFactory(
        controller, platform_name="wayland", current_desktop="KDE", niri_socket_present=True
    )(host)

    assert isinstance(platform, LayerShellPlatform)
    _assert_measures_global_pointer(platform, controller)


def test_an_empty_drag_provider_tuple_is_not_a_missing_one() -> None:
    # `drag_providers or (...)` reinstalled the defaults when a caller passed an
    # empty tuple, so a test that meant to isolate the platform from every provider
    # silently got niri's back and depended on the ambient session again.
    controller = _FakeController(available=True)
    provider = _LayerShellProvider(controller, drag_providers=())

    # A niri desktop, so a reinstalled niri provider would be selected and measure
    # the global reading instead.
    platform = provider.select("wayland", "niri", _MovingHost())

    assert isinstance(platform, LayerShellPlatform)
    _assert_measures_local_pointer(platform, controller)
