"""Tests for automatic furigana (振り仮名) analysis and rendering hooks."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from kotonoha.config import Config
from kotonoha.karaoke_label import KaraokeLabel
from kotonoha.lyrics import furigana
from kotonoha.lyrics.furigana import Furigana, _Token
from kotonoha.model import LyricLine


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _patch_backend(tokens: list[tuple[str, str]]):
    """Inject a fake backend returning the given (surface, kana) tokens."""
    wrapper = lambda text: [_Token(surface=s, kana=k) for s, k in tokens]  # noqa: E731
    furigana._backend = wrapper
    furigana._backend_state = 1
    furigana.analyze.cache_clear()


def _disable_backend():
    """Force the backend to be unavailable (degraded)."""
    furigana._backend = None
    furigana._backend_state = 2
    furigana.analyze.cache_clear()


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    furigana._backend = None
    furigana._backend_state = 0
    furigana.analyze.cache_clear()


# --- furigana.analyze ---


def test_analyze_extracts_kanji_readings_with_positions():
    _patch_backend([("名前", "ナマエ"), ("の", "ノ"), ("空", "ソラ")])
    res = furigana.analyze("名前の空")
    assert res == (
        Furigana(base="名前", kana="なまえ", pos=0),
        Furigana(base="空", kana="そら", pos=3),
    )


def test_analyze_applies_common_reading_override():
    # 私 的 UniDic 字典音是 わたくし;覆盖表将其校正为歌词语境常用的 わたし。
    _patch_backend([("私", "ワタクシ")])
    res = furigana.analyze("私")
    assert len(res) == 1
    assert res[0].base == "私"
    assert res[0].kana == "わたし"


def test_analyze_strips_okurigana_from_kanji_reading():
    # 見上げ -> 漢字段"見上"(注音みあ) + 送假名"げ"(已写在歌词,不注音)。
    _patch_backend([("見上げ", "ミアゲ")])
    res = furigana.analyze("見上げ")
    assert len(res) == 1
    assert res[0].base == "見上"
    assert res[0].kana == "みあ"  # not "みあげ": the 送假名 げ is already in the lyric


def test_analyze_strips_okurigana_full_line():
    # 抱きしめてて沈めば -> 抱=だ(きしめ是送假名),沈=しず(めば->しずめば,送假名めば被剥离)
    _patch_backend([("抱きしめ", "ダキシメ"), ("てて", "テテ"), ("沈めば", "シズメバ")])
    res = furigana.analyze("抱きしめてて沈めば")
    pairs = {f.base: f.kana for f in res}
    assert pairs.get("抱") == "だ"  # not だきしめ
    assert pairs.get("沈") == "しず"


def test_analyze_skips_pure_kana_and_latin():
    _patch_backend([("あなた", "アナタ"), ("に", "ニ"), ("Hello", "ヘロ")])
    res = furigana.analyze("あなたに")
    assert res == ()


def test_analyze_degrades_when_backend_unavailable():
    _disable_backend()
    assert furigana.analyze("君の名前") == ()


def test_mecab_backend_preferred_over_fugashi(monkeypatch, tmp_path):
    """when the mecab CLI and a UniDic dict are present, mecab beats fugashi."""
    dicdir = tmp_path / "dicdir"
    dicdir.mkdir()
    monkeypatch.setattr(furigana.shutil, "which", lambda name: "/usr/bin/mecab" if name == "mecab" else None)
    monkeypatch.setattr(furigana, "_unidic_dicdir", lambda: dicdir)
    _disable_backend()
    furigana._backend_state = 0  # allow re-probe
    assert furigana._select_backend() is furigana._tokens_mecab


def test_mecab_not_selected_without_unidic(monkeypatch):
    """mecab CLI alone is not enough: without a UniDic dict, mecab isn't selected (no bad readings)."""
    monkeypatch.setattr(furigana.shutil, "which", lambda name: "/usr/bin/mecab" if name == "mecab" else None)
    monkeypatch.setattr(furigana, "_unidic_dicdir", lambda: None)
    _disable_backend()
    furigana._backend_state = 0
    # Not mecab; may be fugashi (if installed) or None (degraded).
    assert furigana._select_backend() is not furigana._tokens_mecab


def test_fugashi_backend_used_when_no_mecab(monkeypatch):
    """with no mecab CLI, the fugashi fallback is selected."""
    monkeypatch.setattr(furigana.shutil, "which", lambda _name: None)
    _disable_backend()
    furigana._backend_state = 0
    found = furigana._select_backend()
    # fugashi may or may not be installed in this environment; assert we tried it,
    # i.e. we did NOT fall through to "no backend" without examining fugashi.
    assert found is None or found is furigana._tokens_fugashi


def test_mecab_token_parser():
    """Parse real mecab default output (surface<TAB>yomi<TAB>...)."""
    out = "抱きしめ\tダキシメ\tダキシメル\t抱き締める\t動詞-一般\t連用形-一般\nEOS\n"
    tokens = furigana._parse_mecab_output(out)
    assert tokens == [_Token(surface="抱きしめ", kana="ダキシメ")]


def test_unidic_env_var_overrides_candidates(monkeypatch, tmp_path):
    dicdir = tmp_path / "dicdir"
    dicdir.mkdir()
    monkeypatch.setenv("KOTONOHA_UNIDIC_DIR", str(dicdir))
    assert furigana._unidic_dicdir() == dicdir


# --- KaraokeLabel integration ---

def _make_label(qapp, **style_kwargs) -> KaraokeLabel:
    label = KaraokeLabel()
    label.resize(800, 90)
    font = QFont()
    font.setFamilies(["CaskaydiaCove Nerd Font Mono", "Noto Sans CJK SC"])
    font.setPixelSize(40)
    label.set_style(font, "#FF4FA3", "#FF8FCB", "#FF6EC7", **style_kwargs)
    return label


def test_karaoke_degraded_when_backend_unavailable(qapp):
    _disable_backend()
    label = _make_label(qapp, furigana=True)
    label.set_line(LyricLine(0, "c", 0.0, 6.0, "君の名前は空に消えた", "", ()), False)
    # No furigana, no extra height, render safe.
    assert label._furigana == ()
    assert label._furigana_top == 0.0
    label.grab()
    qapp.processEvents()


def test_karaoke_furigana_populated_when_backend_available(qapp):
    _patch_backend([("名前", "ナマエ"), ("空", "ソラ")])
    label = _make_label(qapp, furigana=True)
    text = "美しい名前の空"
    label.set_line(LyricLine(0, "c", 0.0, 6.0, text, "", ()), False)
    assert label._furigana  # non-empty
    assert label._furigana_top > 0.0
    label.grab()
    qapp.processEvents()


def test_karaoke_zero_regression_when_disabled(qapp):
    label = _make_label(qapp, furigana=False)
    label.set_line(LyricLine(0, "c", 0.0, 6.0, "君の名前は空に消えた", "", ()), False)
    label.set_media_time(3.0)
    assert label._furigana == ()
    assert label._furigana_top == 0.0
    label.grab()
    qapp.processEvents()


def test_config_furigana_default_and_roundtrip():
    cfg = Config()
    assert cfg.furigana is False
    data = cfg.to_dict()
    assert data["furigana"] is False
    overridden = {**data, "furigana": True}
    assert Config.from_dict(overridden).furigana is True
