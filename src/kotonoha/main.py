from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
from typing import Any, cast

# Guard against accidental PyQt5 import conflicts before importing PyQt6.
cast(dict[str, Any], sys.modules)["PyQt5"] = None
os.environ.setdefault("QT_API", "pyqt6")


def _build_app_objects(app, config):
    """Wire state, receiver, overlay and tray together. Returns the controller."""
    from .controller import AppController

    return AppController(app, config)


async def _run(app) -> None:
    from .config import load_config

    close_event = asyncio.Event()
    app.aboutToQuit.connect(close_event.set)

    controller = _build_app_objects(app, load_config())
    await controller.start()
    try:
        await close_event.wait()
    finally:
        await controller.stop()


async def _cancel_pending(loop) -> None:
    current = asyncio.current_task(loop=loop)
    pending = [t for t in asyncio.all_tasks(loop) if t is not current and not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _single_instance_lock(path: str | None = None):
    """Return a held QLockFile, or None if another Kotonoha instance owns it.

    Prevents the stacked tray icons / duplicate overlays from launching it twice."""
    from PyQt6.QtCore import QLockFile

    if path is None:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        path = os.path.join(runtime, "kotonoha.lock")
    lock = QLockFile(path)
    lock.setStaleLockTime(30_000)  # reclaim after 30s if a previous instance crashed
    return lock if lock.tryLock(50) else None


def entry_point() -> int:
    parser = argparse.ArgumentParser(description="Kotonoha desktop lyrics overlay")
    parser.add_argument("--port", "-p", type=int, default=None, help="Override WebSocket receiver port")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # qasync logs every thread-pool callback at DEBUG — including the full args, so a
    # cached LyricsArtifact dumps the entire lyric text on each store. Keep -v about
    # Kotonoha's own logs and mute that third-party firehose.
    logging.getLogger("qasync").setLevel(logging.INFO)

    # Single instance: a second launch would just stack another tray icon + overlay.
    instance_lock = _single_instance_lock()  # noqa: F841 - held for the process lifetime
    if instance_lock is None:
        logging.getLogger(__name__).warning("Kotonoha is already running; exiting.")
        return 0

    # Pin the device pixel ratio to 1 so the layer-shell surface's pixel geometry
    # (set_input_rect / set_anchor_position work in physical pixels) matches what we
    # paint. QT_AUTO_SCREEN_SCALE_FACTOR is a dead Qt5 knob under Qt6 — dropped.
    os.environ.setdefault("QT_SCALE_FACTOR", "1")

    import qasync
    from PyQt6.QtCore import QCoreApplication, Qt
    from PyQt6.QtWidgets import QApplication

    if hasattr(Qt.HighDpiScaleFactorRoundingPolicy, "PassThrough"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # Set the app identity BEFORE QApplication() constructs the windowing stack.
    # On KDE Wayland the platformtheme plugin (platformtheme_kde) snapshots the
    # application name when QApplication initialises and builds a KConfig from it
    # (default file: <appname>rc). If the name is still empty at that point KConfig
    # falls back to the bare file "rc" and, if it cannot be written, KDE pops
    # "Configuration file ...rc not writable". These are static on QCoreApplication,
    # so they take effect before the first QWidget/canvas exists.
    QCoreApplication.setOrganizationName("kotonoha")
    QCoreApplication.setApplicationName("kotonoha")

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationDisplayName("Kotonoha")
    app.setQuitOnLastWindowClosed(False)  # overlay close should not kill the tray
    app.setProperty("xdg_current_desktop", os.environ.get("XDG_CURRENT_DESKTOP", ""))
    app.setProperty("cli_port", args.port)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(_run(app))  # noqa: F841

    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(_cancel_pending(loop))
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(entry_point())
