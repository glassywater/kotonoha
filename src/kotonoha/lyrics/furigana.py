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

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_KATA_TO_HIRA_DELTA = 0x30A1 - 0x3041  # ァ(30A1) -> ぁ(3041)

# 常用读音覆盖：把词典（UniDic）规范读音校正为歌词语境/口语更常用的读音。
# 只放几乎无歧义、歌词高频的少数项；key 是汉字段拼写，value 是平假名。
_READING_OVERRIDES: dict[str, str] = {
    "私": "わたし",  # UniDic 字典音 わたくし；歌词几乎都用 わたし
}

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


# 长音符「ー」按前一个假名的「段」展开为对应假名里的长元音表记：
#   あ段→あ, い段→い, え段→い, う段→う, お段→う
#   拗音小假名：ゃ→あ, ゅ→う, ょ→う（きょー→きょう、しゅー→しゅう）
# UniDic 第 2 列读音把 えい→えー、おう→おー(オー)写成伸ばし棒，如 雷鳴→ライメー、
# 今日→キョー、鼓動→コドー；但注音（振り仮名）应显示表记假名（らいめい、きょう、
# こどう）。只有含汉字的词才会被注音（analyze 里 base 必须是汉字段），因此这里
# 不会误伤片假名外来语。
# 注意：这是普通 dict（用 char->char），不能用 str.maketrans —— 后者生产 int->char 表。
_CHOUON_EXPANSION = {
    # あ段
    "あ": "あ", "か": "あ", "さ": "あ", "た": "あ",
    "な": "あ", "は": "あ", "ま": "あ", "や": "あ", "ら": "あ", "わ": "あ",
    "が": "あ", "ざ": "あ", "だ": "あ", "ば": "あ", "ぱ": "あ",
    # い段
    "い": "い", "き": "い", "し": "い", "ち": "い",
    "に": "い", "ひ": "い", "み": "い", "り": "い", "ゐ": "い",
    "ぎ": "い", "じ": "い", "ぢ": "い", "び": "い", "ぴ": "い",
    # う段
    "う": "う", "く": "う", "す": "う", "つ": "う",
    "ぬ": "う", "ふ": "う", "む": "う", "ゆ": "う", "る": "う",
    "ぐ": "う", "ず": "う", "づ": "う", "ぶ": "う", "ぷ": "う",
    # え段
    "え": "い", "け": "い", "せ": "い", "て": "い",
    "ね": "い", "へ": "い", "め": "い", "れ": "い", "ゑ": "い",
    "げ": "い", "ぜ": "い", "で": "い", "べ": "い", "ぺ": "い",
    # お段
    "お": "う", "こ": "う", "そ": "う", "と": "う",
    "の": "う", "ほ": "う", "も": "う", "よ": "う", "ろ": "う", "を": "う",
    "ご": "う", "ぞ": "う", "ど": "う", "ぼ": "う", "ぽ": "う",
    # 拗音小假名
    "ゃ": "あ", "ゅ": "う", "ょ": "う",
    # 小假名（ぱ行等拗音后的长音极少出现，作稳妥兜底）
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "い", "ぉ": "う",
}


def _expand_chouon(kana: str) -> str:
    """把平假名串里的长音符 ``ー`` 展开为前音段对应的表记假名。

    例：ライメイ用ひらがな→らいめい（无ー，原样）；若上层给的读音是发音形
    （らいめー），则这里展成 ``らいめい``。展开仅追加字符、不删除原「ー」，
    保持注音只增不破坏原读音的其余部分。
    """
    if "ー" not in kana:
        return kana
    out = list(kana)
    for i, ch in enumerate(kana):
        if ch == "ー" and i > 0:
            replacement = _CHOUON_EXPANSION.get(out[i - 1])
            if replacement is not None:
                out[i] = replacement
    return "".join(out)


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
    # 常用读音覆盖：词典（UniDic）规范读音可能与歌词语境/口语习惯不同。这里只校正
    # 几乎无歧义、歌词高频的情况（如 私 的字典音为 わたくし，歌词几乎都用 わたし）。
    overridden = _READING_OVERRIDES.get(kanji)
    if overridden:
        return overridden
    okuri = "".join(t for t, k in segs if not k)
    if okuri:
        okuri_hira = _kata_to_hira(okuri)
        if okuri_hira and word_kana.endswith(okuri_hira):
            kan_kana = word_kana[: -len(okuri_hira)]
            if kan_kana:
                return _expand_chouon(_kata_to_hira(kan_kana))
    return _expand_chouon(_kata_to_hira(word_kana))


# ---------------------------------------------------------------- backends


def _unidic_dicdir() -> Path | None:
    """找一个可用的 UniDic dicdir（用户放置或系统安装）。找不到返回 None。"""
    return referenced_dicdir()


def is_unidic_dicdir(path: Path) -> bool:
    """判断 ``path`` 是否为一个可用的 UniDic 词典目录（含 mecab 所需的 dicrc/bin）。"""
    if not path:
        return False
    p = Path(path)
    # UniDic dicdir 的关键文件（sys.dic / matrix.bin 是最主要的）。
    return p.is_dir() and (p / "sys.dic").is_file() and (p / "matrix.bin").is_file()


def referenced_dicdir() -> Path | None:
    """返回当前实际引用到的 UniDic 词典目录（环境变量优先，其次候选路径）。"""
    env = os.environ.get("KOTONOHA_UNIDIC_DIR")
    if env and is_unidic_dicdir(Path(env)):
        return Path(env)
    for c in _UNIDIC_CANDIDATES:
        if is_unidic_dicdir(c):
            return c
    return None


# unidic-lite 的 PyPI 项目名与安装后 dicdir 所在的包内相对路径。
_UNIDIC_LITE_VERSION = "1.0.8"
_UNIDIC_LITE_PKG = "unidic-lite"
_UNIDIC_LITE_INNER = "unidic_lite/dicdir"  # 安装包里字典数据的相对路径
# 稳定的已知 sdist URL（PyPI 文件 URL 含内容 hash，恒定到该版本退役）。作为兜底：
# 下载按钮点击时立刻用它弹浏览器，避免联网卡 UI；后台会刷新为最新版本。
_LATEST_UNIDIC_SDIST_URL = (
    "https://files.pythonhosted.org/packages/55/2b/"
    "8cf7514cb57d028abcef625afa847d60ff1ffbf0049c36b78faa7c35046f/"
    "unidic-lite-1.0.8.tar.gz"
)


def current_unidic_sdist_url() -> str:
    """当前可用的下载 URL（可能已由后台刷新为最新版本）。点击按钮用它，零网络往返。"""
    return _LATEST_UNIDIC_SDIST_URL


def refresh_unidic_sdist_url() -> str:
    """联网查询 unidic-lite 最新 release 的 sdist URL 并缓存返回。

    应在线程里调用（PyPI 网络可达时可更新，失败时保持已有兜底 URL，不抛错）。
    """
    global _LATEST_UNIDIC_SDIST_URL
    url = f"https://pypi.org/pypi/{_UNIDIC_LITE_PKG}/json"  # 不带版本 = 最新版
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for f in data.get("urls", []):
            if f.get("filename", "").endswith(".tar.gz"):
                _LATEST_UNIDIC_SDIST_URL = f["url"]
                return f["url"]
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return _LATEST_UNIDIC_SDIST_URL


def _unidic_lite_pypi_url() -> str:
    """向 PyPI JSON API 取 unidic-lite sdist 的下载 URL（同步，默认当前版本）。"""
    url = f"https://pypi.org/pypi/{_UNIDIC_LITE_PKG}/{_UNIDIC_LITE_VERSION}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for f in data.get("urls", []):
        if f.get("filename", "").endswith(".tar.gz"):
            return f["url"]
    raise RuntimeError("unidic-lite sdist not found on PyPI")


def download_unidic(dest: Path | None = None, progress: Callable[[str], None] | None = None) -> Path:
    """下载并解压 unidic-lite 词典到 ``dest``（默认候选路径），返回 dicdir 路径。

    阻塞式（应在后台线程调用）；``progress`` 可选地收到阶段文本。下载失败抛异常。
    """
    target = Path(dest) if dest else _UNIDIC_CANDIDATES[0]
    dicdir = target if target.name == "dicdir" else target / "dicdir"
    if is_unidic_dicdir(dicdir):
        return dicdir
    if progress:
        progress("正在获取词典下载地址…")
    sdist_url = _unidic_lite_pypi_url()
    dicdir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kotonoha-unidic-") as tmp:
        if progress:
            progress("正在下载 UniDic 词典…")
        fname = os.path.basename(sdist_url) or "unidic-lite.tar.gz"
        tmp_tarball = os.path.join(tmp, fname)
        try:
            urllib.request.urlretrieve(sdist_url, tmp_tarball)  # noqa: S310 - fixed PyPI URL
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"下载词典失败: {exc}") from exc
        if progress:
            progress("正在解压词典…")
        # 解压整个 sdist，再在树里定位 unidic_lite/dicdir。
        with tarfile.open(tmp_tarball, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        found = None
        for root, dirs, _files in os.walk(tmp):
            if "dicdir" in dirs and (Path(root) / "dicdir" / "sys.dic").is_file():
                found = Path(root) / "dicdir"
                break
        if found is None:
            raise RuntimeError("词典目录未解压出来")
        if dicdir.exists():
            shutil.rmtree(dicdir)
        found.replace(dicdir)
    if progress:
        progress("词典就绪。")
    return dicdir



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
# 最近一次词典下载/探测的错误（供 UI 显示）。
_furigana_last_error: BaseException | None = None


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
                # base is not a contiguous substring of the line text. This happens
                # when a kanji run is split by okurigana written in the lyric, e.g.
                # 繰り返す → 繰 + り + 返 + す, so base="繰返" does not occur verbatim.
                # A single block can't be positioned reliably then, and drawing the
                # whole word reading over just the first kanji reads wrong (くりかえす
                # pinned on 繰). Skip it rather than mis-annotate.
                logger.debug(
                    "Skipping furigana for %r: base %r not contiguous in %r", surface, base, text
                )
                continue
            out.append(Furigana(base=base, kana=kana, pos=pos))
            search_from = pos + len(base)
        return tuple(out)
    except Exception:  # noqa: BLE001 - 任何分析异常都降级为无注音
        return ()


def clear_cache() -> None:
    """清空分析缓存（测试用）。"""
    analyze.cache_clear()
