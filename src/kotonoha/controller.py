"""Application controller: owns the long-lived objects and wires interactions.

Separated from main.py so the wiring is import-testable without spinning up a
real Qt event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from collections.abc import Callable

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from .config import Config, save_config
from .i18n import resolve_translation_language
from .overlay import LyricsOverlay
from .platform import (
    DefaultOverlayPlatformFactory,
    LayerShellController,
    default_package_dir,
)
from .providers.gate import SourceGate
from .providers.mpris import MprisProvider
from .receiver import LyricsReceiver
from .settings_dialog import SettingsDialog
from .state import LyricsState
from .strings import set_language
from .tray import KotonohaTray, load_icon

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, app: QApplication, config: Config) -> None:
        self._app = app
        cli_port = app.property("cli_port")
        config = config.clamped()
        if isinstance(cli_port, int):
            # Clamp the CLI port too: argparse accepts any int, and an out-of-range
            # value (e.g. --port 70000 or -1) would otherwise reach socket.bind()
            # and raise OverflowError — which is not an OSError, so the receiver's
            # and controller's `except OSError` would miss it and startup crashes.
            clamped_port = max(1, min(65535, cli_port))
            if clamped_port != cli_port:
                logger.warning("CLI --port %d is out of range 1..65535; using %d", cli_port, clamped_port)
            config.port = clamped_port
        self._config = config
        set_language(config.ui_language)  # before any UI strings are created
        self._app.setWindowIcon(load_icon(config.window_icon_name, accent=config.accent_start))

        self._state = LyricsState()
        platform_name = app.platformName()
        self._platform_name = platform_name
        desktop = str(app.property("xdg_current_desktop") or "")
        self._desktop = desktop
        self._layer_shell = LayerShellController(default_package_dir(), platform_name, desktop)
        self._overlay = LyricsOverlay(
            self._state,
            config,
            self._layer_shell,
            request_players=self.request_players,
            select_player=self.select_player,
        )
        self._gate = SourceGate()
        self._receiver = LyricsReceiver(
            self._state,
            port=config.port,
            translation_language=resolve_translation_language(config.translation_language),
            gate=self._gate,
        )
        self._mpris = MprisProvider(self._state, lyrics_sources=config.lyrics_sources, gate=self._gate)
        self._mpris.set_player_lock(config.player_lock)
        self._mpris.set_cache_enabled(config.cache_enabled)
        self._mpris.set_prefer_best(config.prefer_best_lyrics)
        self._mpris.set_fuzzy(config.fuzzy_match)
        self._settings_dialog: SettingsDialog | None = None
        self._settings_open_task: asyncio.Task[None] | None = None

        self._tray = KotonohaTray(
            icon_name=config.icon_name,
            accent=config.accent_start,
            passthrough=config.passthrough,
            on_toggle_passthrough=self._on_toggle_passthrough,
            on_open_settings=self._open_settings,
            on_recenter=self.recenter_overlay,
            on_quit=self._app.quit,
            request_players=self.request_players,
            select_player=self.select_player,
        )

        self._overlay.passthrough_toggle_requested.connect(self._toggle_passthrough)
        self._overlay.settings_requested.connect(self._open_settings)
        self._overlay.position_changed.connect(self._on_position_changed)
        self._overlay.track_offset_changed.connect(self._on_track_offset_changed)

    async def start(self) -> None:
        # Promote to a layer surface BEFORE show(): once the window is mapped as a
        # normal xdg surface, LayerShellQt can no longer convert it.
        self._overlay.activate_layer_shell()
        self._overlay.show()
        self._tray.show()
        # The Cider receiver is optional (see README): a port bind failure — a
        # stale instance or double-launch already holding 28745 — must only
        # disable the probe, not take down the overlay/tray that are already up.
        try:
            await self._receiver.start()
        except OSError as exc:
            logger.warning("Lyrics receiver unavailable: %s", exc)
        # MPRIS is best-effort: a missing session bus / dbus must not stop the app.
        try:
            await self._mpris.start()
        except Exception as exc:  # noqa: BLE001 - dbus may be unavailable
            logger.warning("MPRIS provider unavailable: %s", exc)
        logger.info("Kotonoha started on port %d", self._config.port)

    async def stop(self) -> None:
        await self._mpris.stop()
        await self._receiver.stop()

    # --- passthrough / lock ---

    def _toggle_passthrough(self) -> None:
        self._on_toggle_passthrough(not self._config.passthrough)

    def _on_toggle_passthrough(self, checked: bool) -> None:
        if checked == self._config.passthrough:
            self._overlay.set_passthrough(checked)
            return
        self._overlay.set_passthrough(checked)
        self._tray.set_passthrough_checked(checked)
        self._config.passthrough = checked
        self._persist()

    def _on_position_changed(self, margin_edge: int, margin_x: int, screen_name: str) -> None:
        self._config.margin_edge = margin_edge
        self._config.margin_x = margin_x
        self._config.screen_name = screen_name
        self._persist()

    def recenter_overlay(self) -> None:
        """Centre the overlay on its screen (tray menu action)."""
        self._overlay.center_on_screen()
        self._persist()

    def _on_track_offset_changed(self, key: str, offset_ms: int) -> None:
        self._config.track_offsets[key] = offset_ms
        self._persist()

    # --- settings ---

    def _open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        if self._settings_open_task is not None and not self._settings_open_task.done():
            return
        task = asyncio.create_task(self._open_settings_async())
        self._settings_open_task = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._settings_open_task is done:
                self._settings_open_task = None
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - surface async dialog failures
                logger.warning("Could not open settings: %s", exc)

        task.add_done_callback(finished)

    async def _open_settings_async(self) -> None:
        if self._settings_dialog is not None:
            return
        try:
            players = await self._mpris.available_players()
        except Exception as exc:  # noqa: BLE001 - player discovery is best effort
            logger.debug("MPRIS player discovery failed: %s", exc)
            players = []
        dialog = SettingsDialog(
            self._config,
            players=players,
            # Through the registry, not a hardcoded adapter: the settings window is
            # a window on the same session as the overlay, and pinning Layer Shell
            # here handed an X11 session layer-shell capabilities — it reported no
            # window opacity and dropped its own fade-in.
            platform_factory=DefaultOverlayPlatformFactory(
                self._layer_shell, platform_name=self._platform_name, current_desktop=self._desktop
            ),
        )
        dialog.applied.connect(self._apply_config)
        dialog.clear_cache_requested.connect(self._clear_lyrics_cache)
        dialog.restart_requested.connect(self._restart)
        dialog.finished.connect(lambda _: self._clear_dialog())
        self._settings_dialog = dialog
        dialog.show()

    def _clear_dialog(self) -> None:
        self._settings_dialog = None

    def _restart(self) -> None:
        # Relaunch via `python -m kotonoha` so it works whether we were started as
        # the `kotonoha` console script or with `-m`, preserving the CLI args, then
        # quit this instance so its shutdown runs cleanly and the port is released.
        QProcess.startDetached(sys.executable, ["-m", "kotonoha", *sys.argv[1:]])
        logger.info("Restarting to apply settings")
        self._app.quit()

    def _apply_config(self, config: Config) -> None:
        previous_language = resolve_translation_language(self._config.translation_language)
        config.track_offsets = self._config.track_offsets
        self._config = config
        self._overlay.apply_config(config)
        # Push new anchor/margins/passthrough through the layer-shell bridge.
        self._overlay.activate_layer_shell()
        self._tray.set_passthrough_checked(config.passthrough)
        self._app.setWindowIcon(load_icon(config.window_icon_name, accent=config.accent_start))
        self._tray.set_icon_name(config.icon_name, config.accent_start)
        self._mpris.set_lyrics_sources(config.lyrics_sources)
        self._mpris.set_player_lock(config.player_lock)
        self._mpris.set_cache_enabled(config.cache_enabled)
        self._mpris.set_prefer_best(config.prefer_best_lyrics)
        self._mpris.set_fuzzy(config.fuzzy_match)
        set_language(config.ui_language)  # affects newly-opened dialogs; UI restart for the rest

        new_language = resolve_translation_language(config.translation_language)
        if new_language != previous_language:
            self._receiver.update_translation_language(new_language)

        self._persist()

    def _clear_lyrics_cache(self) -> None:
        task = asyncio.create_task(self._mpris.clear_cache())

        def finished(done: asyncio.Task[None]) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Could not clear lyrics cache: %s", exc)

        task.add_done_callback(finished)

    def _persist(self) -> None:
        try:
            save_config(self._config)
        except OSError as exc:
            logger.warning("Could not save config: %s", exc)

    # --- live player selection (ported from waylyrics) ---

    def select_player(self, name: str | None) -> None:
        """Pin playback to a given MPRIS player bus name (None = auto)."""
        self._mpris.select_player(name)

    def request_players(self, on_result: Callable[[list[tuple[str, str]]], None]) -> None:
        """Asynchronously fetch (bus_name, Identity) players and call ``on_result``.

        Results are delivered on the Qt/asyncio event loop via a task callback.
        """
        task = asyncio.create_task(self._mpris.list_available_players())

        def finished(done: asyncio.Task[list[tuple[str, str]]]) -> None:
            try:
                players = done.result()
            except Exception:  # noqa: BLE001 - best-effort listing
                players = []
            on_result(players)

        task.add_done_callback(finished)

    # --- accessors for tests ---

    @property
    def overlay(self) -> LyricsOverlay:
        return self._overlay

    @property
    def state(self) -> LyricsState:
        return self._state

    @property
    def receiver(self) -> LyricsReceiver:
        return self._receiver
