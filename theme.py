"""设计令牌 —— 续墨。

视觉论点：明亮书房。浅色纸面、深色墨字、单一强调色只落在该动手的地方。

两套调色板共用同一条可读性下限：稿纸是全窗最亮的表面，两侧栏向外微微沉降，
正文与背景的对比度最大化。所有颜色、渐变、字号、间距只在此处定义。
"""

import os
import tempfile

_ARROW_CACHE: dict = {}


def _arrow(color: str, up: bool) -> str:
    """生成三角箭头的 PNG 文件，返回路径供 QSS 引用。

    Qt QSS 的 image 属性不支持 data URI，也不支持内联 SVG，
    只能落到文件再用 url() 指过去。
    """
    key = (color, up)
    if key in _ARROW_CACHE:
        return _ARROW_CACHE[key]

    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtGui import QColor, QPainter, QPixmap, QPolygon

    w, h, dpr = 8, 5, 3  # 三倍像素密度，高 DPI 下不糊
    pm = QPixmap(w * dpr, h * dpr)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    if up:
        poly = QPolygon([QPoint(0, h * dpr), QPoint(w * dpr // 2, 0), QPoint(w * dpr, h * dpr)])
    else:
        poly = QPolygon([QPoint(0, 0), QPoint(w * dpr, 0), QPoint(w * dpr // 2, h * dpr)])
    p.drawPolygon(poly)
    p.end()

    folder = os.path.join(tempfile.gettempdir(), "xumo_assets")
    os.makedirs(folder, exist_ok=True)
    fn = os.path.join(folder, f"arrow_{'up' if up else 'dn'}_{color.lstrip('#')}.png")
    pm.save(fn, "PNG")

    path = fn.replace("\\", "/")
    _ARROW_CACHE[key] = path
    return path


# ── 调色板 ──────────────────────────────────────────────
# canvas / rail / desk 为渐变停靠点；rail 顺序是「外缘 → 内缘」。

PALETTES: dict[str, dict] = {
    "blue": {
        "label": "简约蓝",
        "canvas": ("#F7F9FC", "#F1F4F9", "#EAEFF6"),
        "rail":   ("#E6EAF2", "#EFF3F8"),
        "desk":   ("#FFFFFF", "#FDFEFF", "#F9FBFD"),
        "text":       "#1B2330",
        "text_dim":   "#5A6679",
        "text_faint": "#828EA3",
        "rule":       "#DCE2EC",
        "rule_soft":  "#E9EDF4",
        "hover":      "#EDF1F8",
        "input_bg":   "#FFFFFF",
        "selected":   "#FFFFFF",
        "accent":     "#3B6FD4",
        "accent_hi":  "#5A87E0",
        "accent_dim": "#8FAEE0",
        "on_accent":  "#FFFFFF",
        "danger":     "#C4523F",
        "scrim":       "rgba(27, 35, 48, 0.022)",
        "scrim_focus": "rgba(27, 35, 48, 0.042)",
        "selection":   "rgba(59, 111, 212, 0.20)",
        "busy_text":  "#8492A6",
    },
    "pink": {
        "label": "温馨粉",
        "canvas": ("#FCF8F6", "#F8F1EE", "#F3EAE5"),
        "rail":   ("#F0E7E2", "#F8F1EE"),
        "desk":   ("#FFFDFC", "#FEFAF8", "#FBF5F1"),
        "text":       "#2E2422",
        "text_dim":   "#6B5A55",
        "text_faint": "#977F77",
        "rule":       "#EADFD9",
        "rule_soft":  "#F2E9E5",
        "hover":      "#F7EFEA",
        "input_bg":   "#FFFDFC",
        "selected":   "#FFFDFC",
        "accent":     "#B0506A",
        "accent_hi":  "#A34660",
        "accent_dim": "#C87E92",
        "on_accent":  "#FFFFFF",
        "danger":     "#B44A3C",
        "scrim":       "rgba(46, 36, 34, 0.022)",
        "scrim_focus": "rgba(46, 36, 34, 0.042)",
        "selection":   "rgba(176, 80, 106, 0.22)",
        "busy_text":  "#9A8781",
    },
}

_current = "blue"


def set_palette(name: str) -> None:
    global _current
    if name in PALETTES:
        _current = name


def current_palette() -> str:
    return _current


def label_for(name: str) -> str:
    return PALETTES.get(name, {}).get("label", name)


def next_palette() -> str:
    order = list(PALETTES)
    return order[(order.index(_current) + 1) % len(order)]


def accent_for(ratio: float) -> str:
    """上下文仪表：占用越高，颜色越靠近强调色。"""
    p = PALETTES[_current]
    if ratio > 0.85:
        return p["accent"]
    if ratio > 0.6:
        return p["accent_dim"]
    return p["text_faint"]


# ── 渐变 ────────────────────────────────────────────────

def _lin(colors: tuple, reverse: bool = False, vertical: bool = True) -> str:
    """颜色停靠点 → qlineargradient。reverse 供右栏使用（它的内缘在左侧）。"""
    seq = list(reversed(colors)) if reverse else list(colors)
    n = max(1, len(seq) - 1)
    stops = ", ".join(f"stop:{i / n:.2f} {c}" for i, c in enumerate(seq))
    axis = "x1:0, y1:0, x2:0, y2:1" if vertical else "x1:0, y1:0, x2:1, y2:0"
    return f"qlineargradient({axis}, {stops})"


# ── 字体（严格 2 族：衬线承载叙事，无衬线承载操作）──────
SERIF = '"Source Han Serif SC", "Noto Serif SC", "Songti SC", "SimSun", Georgia, serif'
SANS  = '"Inter", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'

# ── 节奏 ────────────────────────────────────────────────
GAP_XS, GAP_SM, GAP, GAP_LG, GAP_XL = 4, 8, 16, 28, 44

RAIL_W     = 232   # 左：章节栏
INSPECT_W  = 300   # 右：语料与上下文
TITLEBAR_H = 54    # 计入首屏预算

# 内容列最大宽度：视口是海报，不是文档——长行会杀死阅读
MEASURE = 680


def build_qss() -> str:
    """全局样式表。无阴影、无卡片；区块感来自亮度差与分隔线。"""
    p = PALETTES[_current]
    arrow_up = _arrow(p["text_dim"], up=True)
    arrow_dn = _arrow(p["text_dim"], up=False)

    canvas  = _lin(p["canvas"], vertical=True)
    rail    = _lin(p["rail"], vertical=False)
    inspect = _lin(p["rail"], reverse=True, vertical=False)
    desk    = _lin(p["desk"], vertical=True)

    return f"""
* {{
    font-family: {SANS};
    font-size: 13px;
    color: {p["text"]};
    outline: none;
}}

QWidget#Canvas {{ background: {canvas}; }}
QDialog#Reader {{ background: {desk}; }}
QWidget#Rail {{ background: {rail}; }}
QWidget#Inspector {{ background: {inspect}; }}
QWidget#Desk {{ background: {desk}; }}
QWidget#TitleBar {{ background: transparent; }}

/* ── 品牌：全站最突出的元素 ── */
QLabel#Brand {{
    font-family: {SERIF};
    font-size: 25px;
    font-weight: 700;
    color: {p["text"]};
    letter-spacing: 4px;
}}
QLabel#BrandLatin {{
    font-size: 10px;
    font-weight: 600;
    color: {p["accent"]};
    letter-spacing: 4px;
}}
QLabel#ProjectName {{
    font-family: {SERIF};
    font-size: 14px;
    font-weight: 600;
    color: {p["text"]};
    letter-spacing: 1px;
}}
QLabel#ProjectName:hover {{ color: {p["accent"]}; }}
QLabel#Crumb {{
    font-size: 12px;
    color: {p["text_faint"]};
    letter-spacing: 1px;
}}

/* ── 区块标题：操作员扫一眼就懂这块是干什么的 ── */
QLabel#SectionTitle {{
    font-size: 10px;
    font-weight: 700;
    color: {p["text_faint"]};
    letter-spacing: 2px;
}}
QLabel#FieldLabel {{
    font-size: 10px;
    font-weight: 600;
    color: {p["text_faint"]};
    letter-spacing: 1px;
}}
QLabel#Hint {{ font-size: 11px; color: {p["text_faint"]}; }}

/* ── 分隔线（取代卡片）── */
QFrame[role="rule"] {{ background: {p["rule"]}; border: none; }}
QFrame[role="ruleSoft"] {{ background: {p["rule_soft"]}; border: none; }}

/* ── 稿纸 ── */
QLineEdit#ChapterTitle {{
    background: transparent;
    border: none;
    font-family: {SERIF};
    font-size: 27px;
    font-weight: 700;
    color: {p["text"]};
    padding: 0;
    letter-spacing: 1px;
}}
QTextEdit#Manuscript {{
    background: transparent;
    border: none;
    font-family: {SERIF};
    font-size: 16px;
    line-height: 200%;
    color: {p["text"]};
    selection-background-color: {p["selection"]};
    selection-color: {p["text"]};
    padding: 0;
}}
QTextEdit#Manuscript[busy="true"] {{ color: {p["busy_text"]}; }}

/* ── 通用输入 ── */
QLineEdit, QPlainTextEdit {{
    background: {p["input_bg"]};
    border: 1px solid {p["rule"]};
    border-radius: 2px;
    padding: 6px 9px;
    font-size: 12px;
    color: {p["text"]};
}}
QLineEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {p["accent_dim"]};
}}

/* 批注栏：极淡底色让侧栏渐变透上来，区块感来自 2% 的亮度差，而非色块 */
QPlainTextEdit#Notes {{
    background: {p["scrim"]};
    border: 1px solid {p["rule_soft"]};
    border-left: 1px solid {p["accent_dim"]};
    border-radius: 0;
    padding: 8px 10px;
    font-size: 12px;
    line-height: 165%;
    color: {p["text_dim"]};
}}
QPlainTextEdit#Notes:focus {{
    border-color: {p["rule_soft"]};
    border-left: 1px solid {p["accent"]};
    color: {p["text"]};
    background: {p["scrim_focus"]};
}}
QLineEdit::placeholder {{ color: {p["text_faint"]}; }}

/* ── 数值输入：与文本框同族，仅右侧多出步进区 ── */
QSpinBox, QDoubleSpinBox {{
    background: {p["input_bg"]};
    border: 1px solid {p["rule"]};
    border-radius: 2px;
    padding: 6px 22px 6px 9px;
    font-size: 12px;
    color: {p["text"]};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {p["accent_dim"]}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    background: transparent;
    border-left: 1px solid {p["rule"]};
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    background: transparent;
    border-left: 1px solid {p["rule"]};
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {p["hover"]};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{arrow_up}");
    width: 8px; height: 5px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{arrow_dn}");
    width: 8px; height: 5px;
}}
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled,
QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
    image: none;
}}

/* ── 按钮：幽灵态为默认，实心强调色全局仅一枚 ── */
QPushButton {{
    background: transparent;
    border: 1px solid {p["rule"]};
    border-radius: 2px;
    padding: 7px 14px;
    font-size: 12px;
    font-weight: 600;
    color: {p["text_dim"]};
    letter-spacing: 1px;
}}
QPushButton:hover {{ border-color: {p["text_faint"]}; color: {p["text"]}; }}
QPushButton:pressed {{ background: {p["hover"]}; }}
QPushButton:disabled {{ color: {p["text_faint"]}; border-color: {p["rule_soft"]}; }}

QPushButton#Primary {{
    background: {p["accent"]};
    border: 1px solid {p["accent"]};
    color: {p["on_accent"]};
    font-weight: 700;
    padding: 8px 22px;
}}
QPushButton#Primary:hover {{ background: {p["accent_hi"]}; border-color: {p["accent_hi"]}; }}
QPushButton#Primary:disabled {{
    background: {p["rule"]}; border-color: {p["rule"]}; color: {p["text_faint"]};
}}

QPushButton#Ghost {{ border: none; color: {p["text_dim"]}; padding: 6px 8px; }}
QPushButton#Ghost:hover {{ color: {p["accent"]}; }}

/* ── 分段切换：右栏一次只呈现一块 ── */
QPushButton#Seg {{
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    color: {p["text_faint"]};
    letter-spacing: 1px;
}}
QPushButton#Seg:hover {{ color: {p["text"]}; }}
QPushButton#Seg:checked {{
    color: {p["accent"]};
    border-bottom: 2px solid {p["accent"]};
}}

QPushButton#WinBtn {{
    border: none; border-radius: 0; padding: 0;
    font-size: 15px; color: {p["text_faint"]};
}}
QPushButton#WinBtn:hover {{ background: {p["hover"]}; color: {p["text"]}; }}
QPushButton#WinClose:hover {{ background: {p["danger"]}; color: #FFFFFF; }}

QPushButton#ThemeToggle {{
    border: 1px solid {p["rule"]};
    border-radius: 2px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
    color: {p["text_dim"]};
    letter-spacing: 1px;
}}
QPushButton#ThemeToggle:hover {{ border-color: {p["accent_dim"]}; color: {p["accent"]}; }}

/* ── 章节列表：列表即布局，没有卡片 ── */
QListWidget#Chapters {{
    background: transparent;
    border: none;
    outline: none;
    padding: 0;
}}
QListWidget#Chapters::item {{
    padding: 9px 14px 9px 16px;
    border: none;
    color: {p["text_dim"]};
    font-size: 13px;
}}
QListWidget#Chapters::item:hover {{ background: {p["hover"]}; color: {p["text"]}; }}
QListWidget#Chapters::item:selected {{
    background: {p["selected"]};
    color: {p["text"]};
    border-left: 2px solid {p["accent"]};
}}

/* ── 语料行 ── */
QListWidget#Corpus {{
    background: transparent; border: none; outline: none;
}}
QListWidget#Corpus::item {{
    padding: 7px 2px;
    color: {p["text_dim"]};
    border-bottom: 1px solid {p["rule_soft"]};
    font-size: 12px;
}}
QListWidget#Corpus::item:hover {{ color: {p["text"]}; }}
QListWidget#Corpus::item:selected {{ color: {p["accent"]}; background: transparent; }}

/* ── 滚动条：细、静、不抢戏 ── */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p["rule"]}; min-height: 40px; border-radius: 0; margin: 2px 3px;
}}
QScrollBar::handle:vertical:hover {{ background: {p["text_faint"]}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {p["rule"]}; min-width: 40px; margin: 3px 2px; }}

/* ── 其它 ── */
QToolTip {{
    background: {p["input_bg"]}; color: {p["text"]};
    border: 1px solid {p["rule"]}; padding: 4px 7px; font-size: 11px;
}}
QMenu {{
    background: {p["input_bg"]}; border: 1px solid {p["rule"]}; padding: 4px;
}}
QMenu::item {{ padding: 6px 22px 6px 12px; font-size: 12px; color: {p["text_dim"]}; }}
QMenu::item:selected {{ background: {p["hover"]}; color: {p["text"]}; }}
QCheckBox {{ font-size: 12px; color: {p["text_dim"]}; spacing: 7px; }}
QCheckBox::indicator {{
    width: 13px; height: 13px; border: 1px solid {p["rule"]}; background: {p["input_bg"]};
}}
QCheckBox::indicator:checked {{ background: {p["accent"]}; border-color: {p["accent"]}; }}
"""


def label_of_palette(name: str) -> str:
    return PALETTES.get(name, {}).get("label", name)
