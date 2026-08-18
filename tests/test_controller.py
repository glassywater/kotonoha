import asyncio
import os
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from kotonoha import controller as controller_module
from kotonoha.config import Config
from kotonoha.controller import AppController
from kotonoha.providers.mpris import MprisProvider
from kotonoha.receiver import LyricsReceiver


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeReceiver:
    async def start(self):
        raise OSError(98, "Address already in use")

    async def stop(self):
        return None


class _FakeMpris:
    def __init__(self):
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        return None


class _Signal:
    def connect(self, _slot):
        return None


class _FakeDialog:
    def __init__(self):
        self.applied = _Signal()
        self.clear_cache_requested = _Signal()
        self.restart_requested = _Signal()
        self.finished = _Signal()

    def show(self):
        return None


async def test_start_survives_optional_receiver_bind_failure(qapp):
    # A stale instance / double-launch holding port 28745 must only disable the
    # optional Cider receiver, not take down the already-shown overlay and tray.
    controller = AppController(qapp, Config())
    controller._receiver = cast(LyricsReceiver, _FakeReceiver())
    fake_mpris = _FakeMpris()
    controller._mpris = cast(MprisProvider, fake_mpris)

    await controller.start()  # must not raise

    assert fake_mpris.started is True  # reached MPRIS despite the receiver failure
    controller._overlay._render_timer.stop()
    controller._overlay.deleteLater()
    qapp.processEvents()


def test_out_of_range_cli_port_is_clamped(qapp):
    # argparse accepts any int; an unclamped 70000 reaches socket.bind() and raises
    # OverflowError (not an OSError), crashing startup. It must be clamped instead.
    qapp.setProperty("cli_port", 70000)
    try:
        controller = AppController(qapp, Config())
        assert controller._config.port == 65535
    finally:
        qapp.setProperty("cli_port", None)
        controller._overlay._render_timer.stop()
        controller._overlay.deleteLater()
        qapp.processEvents()


async def test_settings_discovery_does_not_open_two_dialogs(qapp, monkeypatch):
    controller = AppController(qapp, Config())
    started = asyncio.Event()
    release = asyncio.Event()
    created = []

    class _DeferredMpris:
        async def available_players(self):
            started.set()
            await release.wait()
            return []

    def make_dialog(*_args, **_kwargs):
        created.append(True)
        return _FakeDialog()

    controller._mpris = cast(MprisProvider, _DeferredMpris())
    monkeypatch.setattr(controller_module, "SettingsDialog", make_dialog)
    try:
        controller._open_settings()
        await started.wait()
        controller._open_settings()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(created) == 1
    finally:
        controller._overlay._render_timer.stop()
        controller._overlay.deleteLater()
        qapp.processEvents()
def test_controller_persists_track_offset(qapp, monkeypatch):
    controller = AppController(qapp, Config())
    saved = []
    monkeypatch.setattr("kotonoha.controller.save_config", lambda config: saved.append(config))
    controller._on_track_offset_changed("track", 50)
    assert controller._config.track_offsets == {"track": 50}
    assert saved == [controller._config]
    controller._overlay._render_timer.stop()
    controller._overlay.deleteLater()
    qapp.processEvents()
