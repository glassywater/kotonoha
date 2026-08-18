"""Shared test setup.

Create a single QApplication for the whole session before any test runs. The Qt
GUI tests otherwise create it lazily via module-scoped fixtures, which leaves
QObject (e.g. LyricsState) lifetimes tied to whichever test first spun Qt up and
made the aiohttp receiver tests flaky once enough tests accumulated. One long
lived app keeps those lifetimes stable and deterministic.
"""

from __future__ import annotations

import os

# Assignment, not setdefault: these tests assert what an offscreen platform does —
# that it is not Wayland, that it has no blur protocol, that it can set window
# opacity. A session that already exported QT_QPA_PLATFORM used to win, so running
# the suite from a Wayland desktop failed four settings-dialog tests against the
# real compositor while CI, whose environment is bare, saw nothing. WAYLAND_DISPLAY
# goes with it: the blur bridge reads the session, not the Qt platform name.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("WAYLAND_DISPLAY", None)

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def _session_qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_ui_language():
    # strings.set_language is a module-global: a test that flips it (e.g.
    # test_strings) would otherwise leak the new language into every later test.
    # Restore whatever the language was when this test started, so the suite is
    # order-independent.
    from kotonoha.strings import current_language, set_language

    previous = current_language()
    yield
    set_language(previous)
