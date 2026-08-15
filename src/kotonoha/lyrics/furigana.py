"""自动日文注音（振り仮名/furigana）。

对一行显示用的日文歌词文本做形态素分析，抽出「含汉字的词」对应的平假名读音，
供 KaraokeLabel 渲染为汉字上方的注音。

引擎后端（按优先级选可用者）：
1. **mecab**（首选，用于打包/跨 Python 分发）——subprocess 调用系统的 `mecab`
   命令（Fedora/多数发行版有 `mecab` 系统包，是纯 C 二进制，不绑定 Python ABI），
   词典优先用用户放置的 UniDic（见 ``_unidic_candidates``），否则 mecab 默认词典。
2. **fugashi**（备选，向后兼容）——若 mecab 不可用但安装了 `fugashi` + `unidic-lite`
   （``uv sync --extra furigana``），沿用原有实现。

均不可用时优雅降级：返回空列表，调用方回退纯文本渲染，不崩溃。

设计要点：
- 渲染层视图，不落到数据模型/解析器/磁盘缓存（见 specs/2026-08-15-furigana-auto-design.md）。
- 懒加载 + 缓存：后端探测只做一次；分析结果按文本 lru_cache。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_KATA_TO_HIRA_DELTA = 0x30A1 - 0x3041  # ァ(30A1) -> ぁ(3041)

# 用户放置 UniDic 词典的候选位置（按优先级）。下载 unidic-lite 后把其 dicdir 放到
# 其中任一位置即可被自动识别；也可用 KOTONOHA_UNIDIC_DIR 环境变量直接指定目录。
_UNIDIC_CANDIDATES = (
    Path.home() / ".local" / "share" / "kotonoha" / "unidic" / "dicdir",
    Path.home() / ".local" / "share" / "kotonoha" / "unidic_lite" / "dicdir",
    Path("/usr/share/kotonoha/unidic/dicdir"),
)


@dataclass(frozen=True)
class Furigana:
    base: str  # 被注音的汉字串，如 "名前"
    kana: str  # 平假名读音，如 "なまえ"
    pos: int  # base 在整行文本 text 中的起始字符偏移（用于与主文本对齐）


@dataclass(frozen=True)
class _Token:
    surface: str  # 表层形式，如 "抱きしめ"
    kana: str  # 整词读音（片假名形态），如 "ダキシメ"；未知留空


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
    if not word_kana:
        return ""
    segs = _split_kana_kanji(surface)
    kanji = "".join(t for t, k in segs if k)
    if not kanji:
        return ""
    okuri = "".join(t for t, k in segs if not k)
    if okuri:
        okuri_hira = _kata_to_hira(okuri)
        if okuri_hira and word_kana.endswith(okuri_hira):
            kan_kana = word_kana[: -len(okuri_hira)]
            if kan_kana:
                return _kata_to_hira(kan_kana)
    return _kata_to_hira(word_kana)


# ---------------------------------------------------------------- backends


def _unidic_dicdir() -> Path | None:
    """找一个可用的 UniDic dicdir（用户放置或系统安装）。找不到返回 None。"""
    env = os.environ.get("KOTONOHA_UNIDIC_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    for p in _UNIDIC_CANDIDATES:
        if p.is_dir():
            return p
    return None


def _parse_mecab_output(stdout: str) -> list[_Token]:
    """解析 mecab 输出为 (surface, kana) 列表。

    UniDic 词典的默认输出为 `surface\t読み\t...`，读音在第 2 列；其余词典可能
    无读音（kana 留空，由上层跳过）。mecab 以 ``EOS`` 表示句尾。
    """
    tokens: list[_Token] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line == "EOS" or "\t" not in line:
            continue
        cols = line.split("\t")
        surface = cols[0]
        kana = cols[1] if len(cols) > 1 else ""
        tokens.append(_Token(surface=surface, kana=kana))
    return tokens


def _tokens_mecab(text: str) -> list[_Token]:
    """用 subprocess 调系统 mecab，解析出 (surface, kana) 列表。

    mecab 命令不可用、或解析失败时抛出异常（由上层降级）。
    """
    mecab = shutil.which("mecab")
    if not mecab:
        raise RuntimeError("mecab not installed")
    args = [mecab]
    dicdir = _unidic_dicdir()
    if dicdir is not None:
        args += ["-d", str(dicdir)]
    proc = subprocess.run(
        args, input=text, capture_output=True, text=True, timeout=10.0, check=True
    )
    return _parse_mecab_output(proc.stdout)


def _tokens_fugashi(text: str) -> list[_Token]:
    """旧的后端：fugashi + unidic-lite（向后兼容已 pip 安装的用户）。"""
    import fugashi  # optional; pyproject [tool.ty] overrides

    tagger = fugashi.Tagger()
    tokens: list[_Token] = []
    for w in tagger(text):
        tokens.append(_Token(surface=w.surface, kana=w.feature.kana))
    return tokens


# 惰性后端句柄。_backend_state: 0=未探测, 1=可用(此时 _backend 是函数), 2=故障
_BackendFn = Callable[[str], list[_Token]]
_backend_state = 0
_backend: _BackendFn | None = None
_backend_error: BaseException | None = None


def _select_backend():
    """探测可用后端（mecab 优先，fugashi 兜底），只做一次并缓存结果。"""
    global _backend, _backend_state, _backend_error
    if _backend_state == 1:
        return _backend
    if _backend_state == 2:
        return None
    # mecab CLI 优先（跨 Python，供打包分发）。需要 mecab 命令 + 一个 UniDic 词典：
    # 只有 UniDic 的默认输出在第 2 列给出读音（mecab-ipadic 不是该布局），所以无
    # UniDic 时不选 mecab，避免把品词误当读音。
    if shutil.which("mecab") and _unidic_dicdir() is not None:
        _backend = _tokens_mecab
        _backend_state = 1
        return _backend
    # fugashi 兜底（向后兼容）
    try:
        import fugashi  # optional; pyproject [tool.ty] overrides

        if hasattr(fugashi, "Tagger"):
            _backend = _tokens_fugashi
            _backend_state = 1
            return _backend
    except Exception as exc:  # noqa: BLE001
        _backend_error = exc
    _backend_state = 2
    return None


def _words(text: str) -> list[_Token]:
    backend = _select_backend()
    if backend is None:
        return []
    try:
        return backend(text)
    except Exception:  # noqa: BLE001 - 后端运行失败时降级为空，不崩
        return []


def _furigana_enabled() -> bool:
    return _select_backend() is not None


@lru_cache(maxsize=512)
def analyze(text: str) -> tuple[Furigana, ...]:
    """对一行文本做注音分析，返回 Furigana 元组（可能为空）。

    仅处理含汉字的词；纯假名/片假名/ASCII 词不注音。分析失败时返回空元组。
    """
    tokens = _words(text)
    if not tokens:
        return ()
    try:
        out: list[Furigana] = []
        search_from = 0
        for tok in tokens:
            surface = tok.surface
            segs = _split_kana_kanji(surface)
            base = "".join(seg for seg, kan in segs if kan)
            if not base:
                continue
            word_kana = _kata_to_hira(tok.kana)
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
