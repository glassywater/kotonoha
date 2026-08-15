"""自动日文注音（振り仮名/furigana）。

对一行显示用的日文歌词文本做形态素分析（fugashi + unidic-lite），抽出「含汉字的
词」对应的平假名读音，供 KaraokeLabel 渲染为汉字上方的注音。

设计要点：
- **渲染层视图，不落到数据模型/解析器/磁盘缓存**（见 specs/2026-08-15-furigana-auto-design.md）。
- **懒加载 + 优雅降级**：fugashi 不可用（未安装/词典缺失）时返回空列表，调用方
  自然回退为纯文本渲染，不崩溃。这保证没装依赖的现有环境不受影响。
- `fugashi.Tagger()` 初始化较慢（~百 ms），用惰性单例只建一次。
- 分析结果按文本做 lru_cache，避免同一行反复跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

_KATA_TO_HIRA_DELTA = 0x30A1 - 0x3041  # ァ(30A1) -> ぁ(3041)


@dataclass(frozen=True)
class Furigana:
    base: str  # 被注音的汉字串，如 "名前"
    kana: str  # 平假名读音，如 "なまえ"
    pos: int  # base 在整行文本 text 中的起始字符偏移（用于与主文本对齐）


def _kata_to_hira(text: str) -> str:
    return "".join(
        chr(ord(c) - _KATA_TO_HIRA_DELTA) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text
    )


def _is_kanji(ch: str) -> bool:
    cp = ord(ch)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF


def _split_kana_kanji(orth: str) -> list[tuple[str, bool]]:
    """把词按「汉字段 / 非汉字段」切开，返回 [(文本, 是否汉字段), ...]。

    例：見上げ -> [("見上", True), ("げ", False)]
        消え   -> [("消", True), ("え", False)]
    """
    if not orth:
        return []
    out: list[tuple[str, bool]] = []
    cur = orth[0]
    cur_kan = _is_kanji(cur)
    for ch in orth[1:]:
        kan = _is_kanji(ch)
        if kan == cur_kan:
            cur += ch
        else:
            out.append((cur, cur_kan))
            cur, cur_kan = ch, kan
    out.append((cur, cur_kan))
    return out


def _kanji_reading(surface: str, word_kana: str) -> str:
    """返回应标注在汉字(surface 的汉字段)上的注音。

    送假名词(如 抱きしめ)里，「きしめ」已用假名写在歌词中，读音 = 表记，不应再注音；
    只有汉字段「抱」需要注音，且对应整词读音去掉送假名读音的部分「だ」。
    非送假名词(如 名前)则整个读音都标在汉字上。
    """
    segs = _split_kana_kanji(surface)
    kanji = "".join(t for t, k in segs if k)
    if not kanji:
        return ""
    okuri = "".join(t for t, k in segs if not k)
    if okuri:
        # 送假名读音通常 = 表记本身(转平假名)。若整词读音以送假名读音结尾,
        # 则汉字注音 = 整词读音去掉这段送假名部分。
        okuri_hira = _kata_to_hira(okuri)
        if okuri_hira and word_kana.endswith(okuri_hira):
            kan_kana = word_kana[: -len(okuri_hira)]
            if kan_kana:
                return _kata_to_hira(kan_kana)
    return _kata_to_hira(word_kana)


# 惰性单例；模块首次调用 analyze 时初始化一次
_tagger = None
_tagger_error: BaseException | None = None


def _get_tagger():
    global _tagger, _tagger_error
    if _tagger is not None or _tagger_error is not None:
        return _tagger
    try:
        import fugashi  # type: ignore[import-not-found]
    except ImportError as exc:  # 未安装 -> 永久禁用，避免每次重试导入
        _tagger_error = exc
        return None
    try:
        _tagger = fugashi.Tagger()
    except Exception as exc:  # noqa: BLE001 - 词典缺失/初始化失败等，一律降级
        _tagger_error = exc
        _tagger = None
    return _tagger


def _furigana_enabled() -> bool:
    return _get_tagger() is not None


@lru_cache(maxsize=512)
def analyze(text: str) -> tuple[Furigana, ...]:
    """对一行文本做注音分析，返回 Furigana 元组（可能为空）。

    仅处理含汉字的词；纯假名/片假名/ASCII 词不注音。分析失败时返回空元组。
    """
    tagger = _get_tagger()
    if tagger is None:
        return ()
    try:
        out: list[Furigana] = []
        search_from = 0
        for word in tagger(text):
            surface = word.surface
            segs = _split_kana_kanji(surface)
            base = "".join(seg for seg, kan in segs if kan)
            if not base:
                continue
            word_kana = _kata_to_hira(word.feature.kana)
            kana = _kanji_reading(surface, word_kana)
            if not kana:
                continue
            pos = text.find(base, search_from)
            if pos < 0:
                pos = search_from
            out.append(Furigana(base=base, kana=kana, pos=pos))
            search_from = pos + len(base)
        return tuple(out)
    except Exception:  # noqa: BLE001 - 任何分析异常都降级为无注音
        return ()


def clear_cache() -> None:
    """清空分析缓存（测试用）。"""
    analyze.cache_clear()
