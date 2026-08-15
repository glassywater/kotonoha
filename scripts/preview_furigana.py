#!/usr/bin/env python3
"""独立预览原型:自动日文注音(振り仮名)渲染效果。

不写入主代码,只用来先在本地肉眼验证「汉字上方平假名」的效果与 sweep 对齐。
带渲染 layer 逻辑:复用 kotonoha 的 LXGW 字体回退思路,把两行日文歌词
(一句已唱高亮 + 一句未唱)画成 PNG。用图片查看器打开即可核对。

用法:
    .venv-ruby/bin/python scripts/preview_furigana.py [输出.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

import fugashi
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
)

# --- 注音分析(furigana => list[Furigana]) ---

KATA_TO_HIRA_BASE = 0x30A1 - 0x3041  # ァ(30A1)->ぁ(3041)


def _kata_to_hira(s: str) -> str:
    return "".join(
        chr(ord(c) - KATA_TO_HIRA_BASE) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s
    )


def _is_kanji(c: str) -> bool:
    cp = ord(c)
    return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF


def _split_kana_kanji(orth: str) -> list[tuple[str, bool]]:
    if not orth:
        return []
    out: list[tuple[str, bool]] = []
    cur = orth[0]
    cur_kan = _is_kanji(cur)
    for c in orth[1:]:
        kan = _is_kanji(c)
        if kan == cur_kan:
            cur += c
        else:
            out.append((cur, cur_kan))
            cur, cur_kan = c, kan
    out.append((cur, cur_kan))
    return out


_tagger = None


def _get_tagger():
    global _tagger
    if _tagger is None:
        _tagger = fugashi.Tagger()
    return _tagger


def analyze_furigana(text: str) -> list[tuple[str, str]]:
    """返回 [(汉字串 base, 平假名 kana), ...]。只含汉字的词才注音。"""
    res: list[tuple[str, str]] = []
    for w in _get_tagger()(text):
        orth = w.surface
        segs = _split_kana_kanji(orth)
        if not any(kan for _, kan in segs):
            continue  # 纯假名/片假名/ASCII,不注音
        base = "".join(t for t, kan in segs if kan)
        if not base:
            continue
        res.append((base, _kata_to_hira(w.feature.kana)))
    return res


# --- 渲染 ---

ACCENT_START = QColor("#FF4FA3")
ACCENT_END = QColor("#FF8FCB")
UNSUNG = QColor(255, 255, 255, 95)
SHADOW = QColor(0, 0, 0, 170)
SIZE = 40


def _lookup_families() -> list[str]:
    """预览用自包含字体回退:选中字体 + 用户 fontconfig prefer(LXGW) + 常见 CJK。

    不求完整,只求预览效果接近真实渲染。真实程序里这就是
    overlay._font_families 的产出;这里 hardcode 以便独立于 git 分支运行。
    """
    return [
        "CaskaydiaCove Nerd Font Mono",  # 用户选中
        "霞鹜文楷 TC",  # fontconfig <prefer> 后的中文/日文回退(LXGW WenKai TC)
        "Noto Sans CJK SC",
        "Noto Sans CJK JP",
    ]


def draw_line(
    p: QPainter,
    text: str,
    sung_frac: float,
    y: float,
    families: list[str],
    main_font: QFont,
    ruby_font: QFont,
):
    """画一行:未唱部分(unsung)+ 已唱部分(accent 渐变),并叠加汉字注音。"""
    main_fm = QFontMetrics(main_font)
    ruby_fm = QFontMetrics(ruby_font)

    furi = analyze_furigana(text)

    # 段化布局:[(text, is_furigana, seg_width, kana, w_base)] (w只对furigana段有意义)
    segs: list[tuple[str, bool, float, str | None, float]] = []
    cursor = 0
    for base, kana in furi:
        if not base:
            continue
        idx = text.find(base, cursor)
        if idx < 0:
            idx = cursor
        if idx > cursor:
            plain = text[cursor:idx]
            segs.append((plain, False, main_fm.horizontalAdvance(plain), None, 0.0))
        w_base = main_fm.horizontalAdvance(base)
        w_kana = ruby_fm.horizontalAdvance(kana)
        segs.append((base, True, max(w_base, w_kana), kana, w_base))
        cursor = idx + len(base)
    if cursor < len(text):
        tail = text[cursor:]
        segs.append((tail, False, main_fm.horizontalAdvance(tail), None, 0.0))

    # 累计宽度 -> 基准 x
    total = sum(s[2] for s in segs)
    x = 40.0

    # 逐段画
    sung = 0.0
    for (seg, is_furi, w, kana, w_base) in segs:
        seg_frac = w / total if total else 0.0
        local = sung_frac - sung  # 本段内已唱比例(0..1)
        local = max(0.0, min(1.0, local / seg_frac)) if seg_frac else 1.0
        sung += seg_frac

        # 阴影
        p.save()
        p.translate(1.5, 1.5)
        p.setPen(QPen(QColor(SHADOW)))
        if not is_furi:
            p.drawText(QRectF(x, y, w, SIZE), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), seg)
        p.restore()

        if is_furi:
            # 注音块:段宽 w = max(主字宽, kana宽);主字与 kana 都在段内水平居中,
            # 共享同一中心轴,单字多假名的溢出落在段内空间,不会压到邻段。
            w_kana = ruby_fm.horizontalAdvance(kana)
            center = x + w / 2.0
            # 上方注音(kana),小字,段内居中(先画,位于汉字上方不遮字)
            p.save()
            p.setFont(ruby_font)
            rx = center - w_kana / 2.0
            p.setPen(QPen(QColor(255, 255, 255, 220)))
            p.drawText(
                QRectF(rx, y - 24, w_kana, 26),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                kana,
            )
            p.restore()
            # 主字:段内居中
            base_left = center - w_base / 2.0
            p.setPen(QPen(QColor(UNSUNG)))
            p.drawText(
                QRectF(base_left, y, w_base, SIZE),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                seg,
            )
            # 已唱部分渐变(裁剪到主字实际宽度)
            if local > 0:
                p.save()
                p.setClipRect(QRectF(base_left, y, w_base * local, SIZE), Qt.ClipOperation.IntersectClip)
                grad = QLinearGradient(0, 0, total, 0)
                grad.setColorAt(0.0, ACCENT_START)
                grad.setColorAt(1.0, ACCENT_END)
                p.setPen(QPen(grad, 0))
                p.drawText(
                    QRectF(base_left, y, w_base, SIZE),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                    seg,
                )
                p.restore()
        else:
            # 普通文本:未唱 + 已唱
            p.setPen(QPen(QColor(UNSUNG)))
            p.drawText(QRectF(x, y, w, SIZE), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), seg)
            if local > 0:
                p.save()
                p.setClipRect(QRectF(x, y, w * local, SIZE), Qt.ClipOperation.IntersectClip)
                grad = QLinearGradient(0, 0, total, 0)
                grad.setColorAt(0.0, ACCENT_START)
                grad.setColorAt(1.0, ACCENT_END)
                p.setPen(QPen(grad, 0))
                p.drawText(QRectF(x, y, w, SIZE), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), seg)
                p.restore()

        x += w
    return total


def main() -> int:
    _app = QGuiApplication.instance() or QGuiApplication([])  # keep alive (GC)
    out = sys.argv[1] if len(sys.argv) > 1 else "furigana_preview.png"
    families = _lookup_families()

    main_font = QFont()
    main_font.setFamilies(families)
    main_font.setPixelSize(SIZE)
    ruby_font = QFont(main_font)
    ruby_font.setPixelSize(int(SIZE * 0.55))

    img = QImage(1400, 360, QImage.Format.Format_ARGB32)
    img.fill(QColor(20, 20, 20, 235))

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setFont(main_font)

    # 第一行:已唱 ~65%
    draw_line(p, "君の名前は空に消えた", 0.65, 90, families, main_font, ruby_font)
    # 第二行:未唱
    draw_line(p, "無数の星が輝く夜に", 0.0, 210, families, main_font, ruby_font)

    p.end()
    img.save(out)
    print(f"已保存预览图: {out}")
    print("注音效果核对点:")
    print("  - 「君/名前/空/消」上方应有小平假名 きみ/なまえ/そら/きえ")
    print("  - 「無数/星/輝/夜」应有 むすう/ほし/かがやく/よる")
    print("  - 第一行前 ~65% 为粉色渐变(已唱),尾部白色(未唱)")
    print("请用图片查看器打开检查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
