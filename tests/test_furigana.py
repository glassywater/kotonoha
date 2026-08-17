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
    # 只有存在关键文件(sys.dic/matrix.bin)的目录才算有效词典。
    fake = tmp_path / "dicdir"
    fake.mkdir()
    (fake / "sys.dic").write_bytes(b"")  # 关键文件占位，让 is_unidic_dicdir 通过
    (fake / "matrix.bin").write_bytes(b"")
    monkeypatch.setenv("KOTONOHA_UNIDIC_DIR", str(fake))
    assert furigana.referenced_dicdir() == fake
    # 空目录应被判定为无效(不是词典)。
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("KOTONOHA_UNIDIC_DIR", str(empty))
    assert not furigana.is_unidic_dicdir(empty)


def test_download_skips_when_dict_already_present(tmp_path):
    # 目标位置已是有效词典 -> download_unidic 直接返回，不碰网络。
    dicdir = tmp_path / "dicdir"
    dicdir.mkdir(parents=True, exist_ok=True)
    (dicdir / "sys.dic").write_bytes(b"x")
    (dicdir / "matrix.bin").write_bytes(b"x")
    result = furigana.download_unidic(tmp_path)
    assert result == dicdir


def test_download_url_falls_back_to_known_when_refresh_fails(monkeypatch):
    # 联网刷新失败时，current URL 保持已知兜底值。
    monkeypatch.setattr(furigana, "_LATEST_UNIDIC_SDIST_URL", "https://known/fallback.tar.gz")
    from unittest.mock import patch as _patch

    with _patch.object(furigana.urllib.request, "urlopen", side_effect=OSError("network down")):
        result = furigana.refresh_unidic_sdist_url()
    assert result == "https://known/fallback.tar.gz"
    assert furigana.current_unidic_sdist_url() == "https://known/fallback.tar.gz"


def test_download_url_updated_on_successful_refresh(monkeypatch):
    # 联网成功时缓存更新为 PyPI 返回的最新 sdist URL。
    fake_resp = type(
        "R",
        (),
        {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: None,
            "read": lambda self: __import__("json").dumps(
                {"urls": [{"filename": "x.whl", "url": "https://nope/w.whl"},
                          {"filename": "unidic-lite-9.9.tar.gz", "url": "https://new/latest.tar.gz"}]}
            ).encode(),
        },
    )()
    from unittest.mock import patch as _patch

    with _patch.object(furigana.urllib.request, "urlopen", return_value=fake_resp):
        result = furigana.refresh_unidic_sdist_url()
    assert result == "https://new/latest.tar.gz"
    assert furigana.current_unidic_sdist_url() == "https://new/latest.tar.gz"


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


# --- chouon (長音符 ー) expansion ---


@pytest.mark.parametrize(
    "kana,expected",
    [
        ("らいめー", "らいめい"),   # 雷鳴 (UniDic 发音 ライメー -> 表记 らいめい)
        ("きょー", "きょう"),        # 今日 (キョー -> きょう)
        ("こどー", "こどう"),        # 鼓動 (コドー -> こどう)
        ("とうきょー", "とうきょう"),  # 東京
        ("なまえ", "なまえ"),       # no ー -> unchanged
        ("だきしめ", "だきしめ"),    # no ー -> unchanged
    ],
)
def test_expand_chouon(kana, expected):
    assert furigana._expand_chouon(kana) == expected


@pytest.mark.parametrize(
    "raw_kana,base,expected_kana",
    [
        ("ライメー", "雷鳴", "らいめい"),
        ("キョー", "今日", "きょう"),
        ("コドー", "鼓動", "こどう"),
    ],
)
def test_analyze_expands_chouon_for_kanji_readings(raw_kana, base, expected_kana):
    # UniDic 第 2 列给出发音长音(ライメー);注音应显示表记(らいめい)。
    _patch_backend([(base, raw_kana)])
    res = furigana.analyze(base)
    assert len(res) == 1
    assert res[0].base == base
    assert res[0].kana == expected_kana


def test_furigana_renders_adjacent_blocks_without_collision(qapp):
    # 雷鳴(らいめい) と 繰り返す が隣接する実例。繰り返す の base は不連続(繰+り+返+す)
    # だが、span(繰り返す 全体)に「くりかえす」を居中させ、一文字目に押し付けない。
    _patch_backend([("鼓動", "コドー"), ("は", "ワ"), ("雷鳴", "ライメー"), ("を", "オ"), ("繰り返す", "クリカエス")])
    label = _make_label(qapp, furigana=True)
    text = "鼓動は雷鳴を繰り返す"
    label.set_line(LyricLine(0, "c", 0.0, 9.0, text, "", ()), False)
    combined = {f.base: f for f in label._furigana}
    assert combined["鼓動"].kana == "こどう"  # 長音符已展开为表记
    assert combined["雷鳴"].kana == "らいめい"
    # 繰り返す: base は不連続、span=surface 長さ(4)で全動詞を覆う。
    furi = combined["繰返"]
    assert furi.kana == "くりかえす"
    assert furi.span == 4
    label.grab()  # paints through the (overlap-avoiding) span ruby pass safely
    qapp.processEvents()


def test_analyze_spans_non_contiguous_base_over_the_whole_surface():
    # 繰り返す = 繰 + り + 返 + す (base「繰返」は文中に連続しない)。
    # 全 surface(span=4)に読みを張り、一文字目に押し付けない。
    _patch_backend([("繰り返す", "クリカエス")])
    res = furigana.analyze("繰り返す")
    assert len(res) == 1
    assert res[0].base == "繰返"
    assert res[0].kana == "くりかえす"
    assert res[0].span == 4  # covers 繰+り+返+す, not just 繰
    # 前後の漢字語には影響が出ない。
    _patch_backend([("鼓動", "コドー"), ("は", "ワ"), ("繰り返す", "クリカエス"), ("明日", "アス")])
    res = furigana.analyze("鼓動は繰り返す明日")
    assert {f.base for f in res} == {"繰返", "鼓動", "明日"}
    by_base = {f.base: f for f in res}
    assert by_base["繰返"].span == 4
    assert by_base["明日"].span is None  # 連続 base は nullptr(既定=len(base))


def test_isolate_japanese_replaces_non_japanese_with_spaces():
    # ASCII(英字/数字/半角标点/空格)一律替换为空格;日文假名/汉字/全角标点保留。
    # 长度与原文本 1:1(逐字符映射),便于 meCab 只看日文片段。
    src = "Rainy proof 君が望んだままに"
    isolated = furigana._isolate_japanese(src)
    expected = "".join(" " if ord(c) <= 0x7F else c for c in src)
    assert isolated == expected
    assert len(isolated) == len(src)
    # 片假名人名(ジョン)是日文字符,保留不被替换。
    assert furigana._isolate_japanese("ジョン君") == "ジョン君"
    # 数字同样被替换为空格(逐字符:3→空格,原有空格仍为空格)。
    mixed_src = "君の名は 3月"
    mixed = furigana._isolate_japanese(mixed_src)
    assert mixed == "".join(" " if ord(c) <= 0x7F else c for c in mixed_src)

def test_analyze_passes_isolated_text_to_backend():
    # analyze 分词前把非日文字符隔离为空格再交给 backend —— 这样 meCab 不会被
    # 前后英文诱导,把「君」误判成接尾辞(くん)。这里捕获 backend 收到的文本。
    captured = {}

    def capturing_backend(text):
        captured["text"] = text
        return [_Token(surface="君", kana="キミ")]

    furigana._backend = capturing_backend
    furigana._backend_state = 1
    furigana.analyze.cache_clear()
    res = furigana.analyze("Rainy proof 君が望んだままに")
    # backend 收到的是隔离后的文本(英文→空格,君 前无英文)。
    src = "Rainy proof 君が望んだままに"
    assert captured["text"] == "".join(" " if ord(c) <= 0x7F else c for c in src)
    # 「君」被当作代名词きみ,而非接尾辞くん。
    assert len(res) == 1
    assert res[0].base == "君"
    assert res[0].kana == "きみ"


def test_isolate_keeps_name_suffix_and_second_person_readings():
    # 隔离英文后,真正的片假名人名+君 仍会按 meCab 的原始判断注くん —— 这里用真实
    # token 模拟 meCab 行为:ジョン君→接尾辞くん,君が→代名词きみ。
    # (注:隔离的是英文;片假名ジョン保留,故 meCab 仍判接尾辞。)
    _patch_backend([("ジョン", "ジョン"), ("君", "クン")])
    res = furigana.analyze("ジョン君")
    assert {f.base: f.kana for f in res}["君"] == "くん"

