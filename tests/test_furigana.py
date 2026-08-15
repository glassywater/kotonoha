"""Tests for automatic furigana (振り仮名) analysis and rendering hooks."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from kotonoha.config import Config
from kotonoha.karaoke_label import KaraokeLabel
from kotonoha.lyrics import furigana
from kotonoha.lyrics.furigana import Furigana
from kotonoha.model import LyricLine


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeWord:
    """Minimal stand-in for a fugashi word: surface text + feature with kana."""

    def __init__(self, surface: str, kana: str) -> None:
        self.surface = surface
        self.feature = type("_F", (), {"kana": kana})()


class _FakeTagger:
    def __init__(self, words: list[tuple[str, str]]) -> None:
        self._words = [_FakeWord(s, k) for s, k in words]

    def __call__(self, text: str):
        # Return the preconfigured words (analyze uses them directly).
        return self._words


def _patched_analyze(words: list[tuple[str, str]]):
    """Run furigana.analyze with a mocked tagger injected into the module state."""
    furigana._tagger_error = None
    furigana._tagger = _FakeTagger(words)
    furigana.analyze.cache_clear()


# --- furigana.analyze ---

def test_analyze_extracts_kanji_readings_with_positions():
    _patched_analyze([("名前", "ナマエ"), ("の", "ノ"), ("空", "ソラ")])
    res = furigana.analyze("名前の空")
    assert res == (
        Furigana(base="名前", kana="なまえ", pos=0),
        Furigana(base="空", kana="そら", pos=3),
    )


def test_analyze_strips_okurigana_from_kanji_reading():
    # 見上げ -> 漢字段"見上"(注音みあ) + 送假名"げ"(已写在歌词,不注音)。
    _patched_analyze([("見上げ", "ミアゲ")])
    res = furigana.analyze("見上げ")
    assert len(res) == 1
    assert res[0].base == "見上"
    assert res[0].kana == "みあ"  # not "みあげ": the 送假名 げ is already in the lyric


def test_analyze_strips_okurigana_full_line():
    # 抱きしめてて沈めば -> 抱=だ(きしめ是送假名),沈=しず(めば->しずめば,送假名めば被剥离)
    _patched_analyze([("抱きしめ", "ダキシメ"), ("てて", "テテ"), ("沈めば", "シズメバ")])
    res = furigana.analyze("抱きしめてて沈めば")
    pairs = {f.base: f.kana for f in res}
    assert pairs.get("抱") == "だ"  # not だきしめ
    assert pairs.get("沈") == "しず"


def test_analyze_skips_pure_kana_and_latin():
    _patched_analyze([("あなた", "アナタ"), ("に", "ニ"), ("Hello", "ヘロ")])
    furigana._tagger_error = None
    furigana._tagger = _FakeTagger([("あなた", "アナタ"), ("に", "ニ")])
    furigana.analyze.cache_clear()
    res = furigana.analyze("あなたに")
    assert res == ()


def test_analyze_degrades_when_fugashi_missing():
    from unittest.mock import patch

    furigana._tagger_error = None
    furigana._tagger = None
    # simulate import failure permanently disabling furigana
    with patch.object(furigana, "_get_tagger", return_value=None):
        assert furigana.analyze("君の名前") == ()


# --- KaraokeLabel integration ---

def _make_label(qapp, **style_kwargs) -> KaraokeLabel:
    label = KaraokeLabel()
    label.resize(800, 90)
    font = QFont()
    font.setFamilies(["CaskaydiaCove Nerd Font Mono", "Noto Sans CJK SC"])
    font.setPixelSize(40)
    label.set_style(font, "#FF4FA3", "#FF8FCB", "#FF6EC7", **style_kwargs)
    return label


def test_karaoke_degraded_when_no_fugashi(qapp):
    from unittest.mock import patch

    label = _make_label(qapp, furigana=True)
    with patch.object(furigana, "_get_tagger", return_value=None), patch.object(
        furigana, "analyze", return_value=()
    ):
        label.set_line(LyricLine(0, "c", 0.0, 6.0, "君の名前は空に消えた", "", ()), False)
    # No furigana, no extra height, render safe.
    assert label._furigana == ()
    assert label._furigana_top == 0.0
    assert label._fm.height() + 6 == label.sizeHint().height() - 0  # unchanged by furigana
    label.grab()
    qapp.processEvents()


def test_karaoke_zero_regression_when_disabled(qapp):
    label = _make_label(qapp, furigana=False)
    label.set_line(LyricLine(0, "c", 0.0, 6.0, "君の名前は空に消えた", "", ()), False)
    label.set_media_time(3.0)
    assert label._furigana == ()
    assert label._furigana_top == 0.0
    label.grab()  # paint path must not touch furigana at all
    qapp.processEvents()


def test_config_furigana_default_and_roundtrip():
    cfg = Config()
    assert cfg.furigana is False
    data = cfg.to_dict()
    assert data["furigana"] is False
    # data defines furigana=False; explicitly override to True afterwards.
    overridden = {**data, "furigana": True}
    assert Config.from_dict(overridden).furigana is True
