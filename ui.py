"""界面 —— 续墨。

构图：无边框窗口，三栏。左栏是目录，中栏是全出血的稿纸（视觉锚点），
右栏是语料与上下文。品牌居于左上角，是全站最突出的元素。

动效（全部为功能性，无装饰）：
  1. 流式续写 —— 正文逐字浮现，稿纸底部保持锚定，存在感来自"有人正在写"。
  2. 章节切换 —— 内容交叉淡出淡入，左栏琥珀游标跟随，空间关系不断裂。
  3. 上下文仪表 —— 预算接近上限时数字转为琥珀并启动记忆压缩，状态先于错误。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections.abc import Callable

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QRectF,
    QSize, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QColor, QConicalGradient, QCursor, QPainter, QPainterPath, QPen,
    QRegion, QTextBlockFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QCheckBox, QComboBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizeGrip, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

import ai as AI
import presets
import store
import theme as T
from store import Chapter, Corpus, Project, Settings, _uid


# ══════════════════════════════════════════════════════════
#  稿纸
# ══════════════════════════════════════════════════════════

class Manuscript(QTextEdit):
    """正文编辑器。负责一件事：让文字以正确的节奏呈现。

    行高与段距通过块格式施加，新段落自动继承；整篇重排只在载入时做一次。
    """

    MAX_RHYTHM_DOC = 120_000

    # 改写请求：参数为模式键（见 ai.REWRITE_MODES）
    rewrite_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Manuscript")
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setPlaceholderText("在此落笔，或按下方的「续写」，让引擎接着往下写。")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._quiet = False
        self._guard = False
        # 改写现场：起点、当前写入位置、被替下的原文
        self._rw_start = 0
        self._rw_pos = 0
        self._rw_orig = ""
        self._rhythm = QTimer(self)
        self._rhythm.setSingleShot(True)
        self._rhythm.setInterval(160)
        self._rhythm.timeout.connect(self._apply_rhythm)
        self.textChanged.connect(self._on_changed)

    def _menu(self, pos: QPoint) -> None:
        """右键菜单：常规编辑动作 + 选中时的 AI 改写。"""
        m = QMenu(self)
        cur = self.textCursor()
        has_sel = cur.hasSelection()
        for label, obj, enabled in (
            ("撤销", "undo", self.document().isUndoAvailable()),
            ("重做", "redo", self.document().isRedoAvailable()),
        ):
            a = QAction(label, self)
            a.triggered.connect(getattr(self, obj))
            a.setEnabled(enabled)
            m.addAction(a)
        m.addSeparator()
        for label, obj in (("剪切", "cut"), ("复制", "copy"), ("粘贴", "paste")):
            a = QAction(label, self)
            a.triggered.connect(getattr(self, obj))
            a.setEnabled(True if obj == "paste" else has_sel)
            m.addAction(a)
        m.addSeparator()
        sub = m.addMenu("AI 改写")
        sub.setEnabled(has_sel)
        for mode, (label, _) in AI.REWRITE_MODES.items():
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, k=mode: self.rewrite_requested.emit(k))
            sub.addAction(a)
        m.exec(self.mapToGlobal(pos))

    # ── 节奏 ──
    @staticmethod
    def _block_format() -> QTextBlockFormat:
        f = QTextBlockFormat()
        f.setLineHeight(195, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        f.setBottomMargin(13)
        f.setTopMargin(0)
        return f

    def _apply_rhythm(self) -> None:
        if self._guard or self.document().characterCount() > self.MAX_RHYTHM_DOC:
            return
        # 施加块格式本身会触发 textChanged —— 它永远是一次程序改动，不是作者
        # 敲的。不罩住它，每次载入章节都会在收尾这里补一枪：textChanged 落到
        # _on_text 上被当成用户编辑，8 秒后 autosave 白存一次盘、顺便轮转掉
        # 一份备份。所有调用点都受益，不必在每个 load_* 里各自记得收尾。
        prior = self._quiet
        self._quiet = True
        try:
            self._apply_rhythm_inner()
        finally:
            self._quiet = prior

    def _apply_rhythm_inner(self) -> None:
        self._guard = True
        try:
            cur = self.textCursor()
            pos, anchor = cur.position(), cur.anchor()
            cur.select(QTextCursor.SelectionType.Document)
            cur.mergeBlockFormat(self._block_format())
            cur.clearSelection()
            cur.setPosition(anchor)
            cur.setPosition(pos, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cur)
        finally:
            self._guard = False

    def _on_changed(self) -> None:
        if not self._guard and not self._quiet:
            self._rhythm.start()

    # ── 对外 ──
    #
    # 静默作用域：setPlainText 会重建文档并触发 textChanged，这类写入必须全程
    # 罩住 _quiet，否则会被当成用户编辑。进来之前先记住原值、出去时原样放回 ——
    # 单纯赋值 True/False 会埋雷：某条路径提前抛错，或调用方记错了当前状态，
    # _quiet 就永久停在 True，之后作者敲的字全都不算改动，autosave 静默失效，
    # 直到关窗才发现稿子没存上。
    def load_body(self, text: str) -> None:
        prior = self._quiet
        self._quiet = True
        try:
            self.setPlainText(text)
            self.moveCursor(QTextCursor.MoveOperation.Start)
            self._apply_rhythm()
        finally:
            self._quiet = prior

    def begin_stream(self) -> None:
        self._quiet = True
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def swap_body(self, text: str) -> None:
        """流式中途换章：整体替换内容，光标落到末尾。"""
        prior = self._quiet
        self._quiet = True
        try:
            self.setPlainText(text)
            cur = self.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cur)
            # setPlainText 重建文档会清掉块格式，必须重新施加，否则换章后
            # 段落间距与其它章不一致。
            self._apply_rhythm()
        finally:
            self._quiet = prior
        self.ensureCursorVisible()

    def append_delta(self, text: str) -> None:
        """流式追加。先把块格式套在当前块上，新段落自动继承，无需整篇重排。"""
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self._guard = True
        cur.setBlockFormat(self._block_format())
        self._guard = False
        self.setTextCursor(cur)
        self.insertPlainText(text)
        self.ensureCursorVisible()

    def end_stream(self) -> None:
        self._quiet = False
        self._apply_rhythm()

    def replace_tail(self, n_remove: int, new_text: str) -> None:
        """把正文末尾 n_remove 个字替换成 new_text（流式期间自动审校用）。"""
        full = self.toPlainText()
        if n_remove > len(full):
            n_remove = len(full)
        keep = full[: len(full) - n_remove] if n_remove else full
        prior = self._quiet
        self._quiet = True
        try:
            self.setPlainText(keep + new_text)
            cur = self.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cur)
            # setPlainText 清掉了块格式：不补回来，被重写的这段就没有行距/段距，
            # 与前面的正文格式对不上（截图里「间距忽大忽小」即由此而来）。
            self._apply_rhythm()
        finally:
            self._quiet = prior
        self.ensureCursorVisible()

    def body(self) -> str:
        return self.toPlainText()

    def is_quiet(self) -> bool:
        """载入 / 流式写入期间为真 —— 此时的 textChanged 不代表用户编辑。"""
        return self._quiet

    # ── 就地改写 ──
    def begin_rewrite(self) -> str:
        """腾出位置：删掉选中段，记下插入点与原文，返回原文。

        原文要先取走再删 —— 选中的可能是好几段，界面上一旦删除，
        这份文本就只剩下我们手里这个副本。失败时要靠它退回去。
        """
        cur = self.textCursor()
        start, end = cur.selectionStart(), cur.selectionEnd()
        orig = self.toPlainText()[start:end]
        self._rw_start = self._rw_pos = start
        self._rw_orig = orig
        self._quiet = True
        cur.setPosition(start)
        cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cur.setBlockFormat(self._block_format())
        cur.removeSelectedText()
        return orig

    def put_rewrite(self, text: str) -> None:
        """把改写增量接到正在生长的那一段末尾。"""
        cur = self.textCursor()
        cur.setPosition(self._rw_pos)
        cur.insertText(text)
        self._rw_pos += len(text)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def end_rewrite(self) -> None:
        self._quiet = False
        self._apply_rhythm()

    def restore_rewrite(self) -> None:
        """一字未出就失败了：把原文放回原位，等于什么都没发生。"""
        if not self._rw_orig:
            return
        cur = self.textCursor()
        cur.setPosition(self._rw_start)
        cur.setPosition(self._rw_pos, QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(self._rw_orig)
        self._rw_orig = ""
        self._quiet = False
        self._apply_rhythm()


# ══════════════════════════════════════════════════════════
#  线程桥
# ══════════════════════════════════════════════════════════

class Bridge(QObject):
    delta = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal()
    failed = pyqtSignal(str)
    settings_ready = pyqtSignal(str)
    memory_ready = pyqtSignal(str)
    names_ready = pyqtSignal(str)
    fix_tail = pyqtSignal(int, str)   # (删除末尾字数, 重写正文)


# ══════════════════════════════════════════════════════════
#  小构件
# ══════════════════════════════════════════════════════════

def rule(soft: bool = False) -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setProperty("role", "ruleSoft" if soft else "rule")
    return f


def vrule(soft: bool = False, height: int = 0) -> QFrame:
    f = QFrame()
    f.setFixedWidth(1)
    if height:
        f.setFixedHeight(height)
    f.setProperty("role", "ruleSoft" if soft else "rule")
    return f


def section_title(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("SectionTitle")
    return lb


def field_label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("FieldLabel")
    return lb


def _find_break(text: str, target: int) -> int:
    """在 target 附近找自然切分点：段落边界 > 句末标点 > 换行 > 硬切。

    自动分章的切点若落在句子中间，就会出现「上一章末尾半句、下一章开头半句」。
    """
    lo = int(target * 0.7)
    hi = min(target, len(text))
    seg = text[:hi]
    pos = seg.rfind("\n\n", lo, hi)
    if pos > lo:
        return pos + 2
    for ch in "。！？…":
        pos = seg.rfind(ch, lo, hi)
        if pos > lo:
            return pos + 1
    pos = seg.rfind("\n", lo, hi)
    if pos > lo:
        return pos + 1
    return hi


def hint(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("Hint")
    return lb


def ghost_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("Ghost")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class FlowLayout(QLayout):
    """自动换行的水平布局。

    Qt 没有内置这个 —— QHBoxLayout 会把控件一路挤出边界。预设标签有几十个，
    必须按容器宽度折行。
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt 接口)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        """逐行摆放；apply=False 时只算高度不实际移动。"""
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_h = 0
        for it in self._items:
            w = it.sizeHint().width()
            h = it.sizeHint().height()
            if x + w > right and line_h > 0:
                x = rect.x() + m.left()
                y += line_h + self._spacing
                line_h = 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self._spacing
            line_h = max(line_h, h)
        return y + line_h - rect.y() + m.bottom()


# ══════════════════════════════════════════════════════════
#  流光呼吸边框
# ══════════════════════════════════════════════════════════

# 七彩流光色带：红 → 橙 → 黄 → 绿 → 青 → 蓝 → 靛 → 紫 → 粉，首尾闭环
RAINBOW = [
    "#FF3B30", "#FF9500", "#FFCC00", "#34C759", "#00C7B8",
    "#0A84FF", "#5856D6", "#AF52DE", "#FF2D9B",
]

# 单色模式的预设调色板
GLOW_PALETTE = [
    "#FF3B30", "#FF9500", "#FFCC00", "#34C759",
    "#00C7B8", "#3B6FD4", "#8E6BFF", "#FF2D9B",
]


class GlowBorder(QWidget):
    """窗口边缘的流光 + 呼吸描边。

    覆盖在窗口最上层的透明控件，只负责画一圈渐变描边：
      · 流光 —— QConicalGradient 的起始角随时间推进，颜色沿边框环行。
      · 呼吸 —— 描边透明度按正弦起伏，整体明暗有节律。

    两种取色模式：
      · 七彩渐变（默认）—— 九色色带闭环，绕边框流转。
      · 单色 —— 取调色板色，派生出亮/暗三阶，同色系内流动。

    鼠标事件一律穿透，不影响任何交互。
    """

    FRAME_MS = 33          # ≈30fps，够顺滑又不与流式渲染抢 CPU
    THICKNESS = 5.0        # 描边粗细（逻辑像素）
    CORNER = 16.0          # 圆角半径 —— 与窗口遮罩同值，描边才贴得住

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GlowBorder")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._angle = 0.0
        self._phase = 0.0
        self._on = False      # 未启用时不绘制
        self._rainbow = True
        self._color = "#3B6FD4"
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def configure(self, rainbow: bool, color: str) -> None:
        """设置取色模式与单色值。"""
        self._rainbow = bool(rainbow)
        self._color = color or "#3B6FD4"
        self.update()

    def _stops(self) -> list[str]:
        """渐变停靠点。首尾同色，闭环才不会有接缝。"""
        if self._rainbow:
            return RAINBOW + [RAINBOW[0]]
        c = QColor(self._color)
        hi = c.lighter(145).name()
        lo = c.darker(135).name()
        return [hi, c.name(), lo, hi]

    def start(self) -> None:
        self._on = True
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._on = False
        self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 1.6) % 360.0
        self._phase += 0.075
        self.update()

    def paintEvent(self, _e) -> None:
        if not getattr(self, "_on", False):
            return
        stops = self._stops()
        if not stops:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w, h = self.width(), self.height()
        t = self.THICKNESS
        m = t / 2
        rect = QRectF(m, m, w - t, h - t)

        # 呼吸：透明度在 0.6 ~ 1.0 之间摆动
        breathe = 0.8 + 0.2 * math.sin(self._phase)

        grad = QConicalGradient(rect.center(), -self._angle)
        n = len(stops) - 1
        for i, c in enumerate(stops):
            col = QColor(c)
            col.setAlphaF(breathe)
            grad.setColorAt(i / n, col)

        pen = QPen()
        pen.setBrush(grad)
        pen.setWidthF(t)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.setPen(pen)
        p.drawRoundedRect(rect, self.CORNER - t / 2, self.CORNER - t / 2)
        p.end()


# ══════════════════════════════════════════════════════════
#  标题栏
# ══════════════════════════════════════════════════════════

class TitleBar(QWidget):
    def __init__(self, win: "Window"):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(T.TITLEBAR_H)
        self._win = win
        self._drag: QPoint | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(T.GAP_LG, 0, 0, 0)
        row.setSpacing(T.GAP)

        brand = QLabel("续墨")
        brand.setObjectName("Brand")
        row.addWidget(brand, 0, Qt.AlignmentFlag.AlignVCenter)

        row.addSpacing(T.GAP_LG)

        # 作品名：可点击重命名 —— 层级上仅次于品牌本身
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("ProjectName")
        self.title_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_lbl.setToolTip("点击重命名")
        self.title_lbl.mousePressEvent = lambda e: self._win.rename_project()
        row.addWidget(self.title_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        self.crumb = QLabel("")
        self.crumb.setObjectName("Crumb")
        row.addWidget(self.crumb, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        self.works_btn = QPushButton("作品")
        self.works_btn.setObjectName("ThemeToggle")
        self.works_btn.setToolTip("新建、打开、导入")
        self.works_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.works_btn.clicked.connect(self._win.works_menu)
        row.addWidget(self.works_btn)
        row.addSpacing(T.GAP)

        self.read_btn = QPushButton("阅读")
        self.read_btn.setObjectName("ThemeToggle")
        self.read_btn.setToolTip("在独立窗口里通读全书")
        self.read_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.read_btn.clicked.connect(self._win.open_reader)
        row.addWidget(self.read_btn)
        row.addSpacing(T.GAP)

        self.export_btn = QPushButton("导出")
        self.export_btn.setObjectName("ThemeToggle")
        self.export_btn.setToolTip("导出作品与设定")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._win.export_menu)
        row.addWidget(self.export_btn)
        row.addSpacing(T.GAP)

        self.theme_btn = QPushButton(T.label_for(T.current_palette()))
        self.theme_btn.setObjectName("ThemeToggle")
        self.theme_btn.setToolTip("切换配色")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self._cycle_theme)
        row.addWidget(self.theme_btn)
        row.addSpacing(T.GAP_SM)

        row.addSpacing(T.GAP)
        row.addWidget(vrule(True, 22), 0, Qt.AlignmentFlag.AlignVCenter)

        for label, obj, slot in (
            ("—", "WinBtn", self._win.showMinimized),
            ("▢", "WinBtn", self._toggle_max),
            ("✕", "WinClose", self._win.close),
        ):
            b = QPushButton(label)
            b.setObjectName(obj)
            b.setFixedSize(46, T.TITLEBAR_H)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            row.addWidget(b)

    def set_crumb(self, text: str, title: str = "") -> None:
        if title:
            self.title_lbl.setText(title)
        self.crumb.setText(text)

    def _cycle_theme(self) -> None:
        self._win.cycle_theme()

    def sync_theme_label(self) -> None:
        self.theme_btn.setText(T.label_for(T.current_palette()))

    def _toggle_max(self) -> None:
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    # ── 拖动 ──
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            if self._win.isMaximized():
                self._win.showNormal()
                self._drag = QPoint(self._win.width() // 2, T.TITLEBAR_H // 2)
            self._win.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None

    def mouseDoubleClickEvent(self, e) -> None:
        self._toggle_max()


# ══════════════════════════════════════════════════════════
#  左栏：目录
# ══════════════════════════════════════════════════════════

class ChapterList(QListWidget):
    """章节列表：支持拖拽排序。

    drop 完成后回读顺序，而非监听 model 的 rowsMoved —— QListWidget 的
    内部移动在部分实现里以「插入 + 删除」完成，rowsMoved 不一定触发。
    dropEvent 期间抑制 currentRowChanged，避免拖拽引发的选中变化被当成「切换章节」。
    """

    reordered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.suppressing = False

    def dropEvent(self, e) -> None:
        self.suppressing = True
        try:
            super().dropEvent(e)
        finally:
            self.suppressing = False
        self.reordered.emit()


class Rail(QWidget):
    picked = pyqtSignal(int)
    added = pyqtSignal()
    renamed = pyqtSignal(int, str)
    removed = pyqtSignal(int)
    reordered = pyqtSignal(list)
    merged = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.setObjectName("Rail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(T.RAIL_W)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, T.GAP, 0, T.GAP_SM)
        col.setSpacing(T.GAP_SM)

        head = QHBoxLayout()
        head.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        head.addWidget(section_title("章节"))
        head.addStretch(1)
        self.count = hint("")
        head.addWidget(self.count)
        col.addLayout(head)
        col.addWidget(rule(soft=True))

        self.list = ChapterList()
        self.list.setObjectName("Chapters")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setSpacing(1)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentRowChanged.connect(self._on_row)
        self.list.reordered.connect(self._on_reorder)
        col.addWidget(self.list, 1)

        col.addWidget(rule(soft=True))
        foot = QHBoxLayout()
        foot.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        b = ghost_button("＋  新建章节")
        b.clicked.connect(self.added.emit)
        foot.addWidget(b)
        foot.addStretch(1)
        col.addLayout(foot)

    def _on_row(self, row: int) -> None:
        if row >= 0 and not self.list.suppressing:
            self.picked.emit(row)

    def _on_reorder(self) -> None:
        """拖拽结束后，按列表当前顺序回读章节 id。"""
        ids = [
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
        ]
        self.reordered.emit(ids)

    def load(self, chapters: list[Chapter], current: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for i, ch in enumerate(chapters, 1):
            it = QListWidgetItem(f"{i:02d}   {ch.title}")
            it.setToolTip(f"{ch.words():,} 字")
            it.setData(Qt.ItemDataRole.UserRole, ch.id)
            it.setFlags(
                it.flags()
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            self.list.addItem(it)
        self.list.setCurrentRow(current)
        self.list.blockSignals(False)
        self.set_totals(len(chapters), sum(c.words() for c in chapters))

    def set_totals(self, chapters: int, words: int) -> None:
        """左栏页脚：章节数与全书字数。写长篇时真正想盯的是总量。"""
        self.count.setText(f"{chapters} 章 · {words:,} 字")

    def _menu(self, pos: QPoint) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        row = self.list.row(it)
        last = self.list.count() - 1
        m = QMenu(self)
        a1 = QAction("重命名", self)
        a2 = QAction("删除本章", self)
        a1.triggered.connect(lambda: self._rename(row))
        a2.triggered.connect(lambda: self.removed.emit(row))
        m.addAction(a1)
        m.addAction(a2)
        if row > 0 or row < last:
            m.addSeparator()
        if row > 0:
            a3 = QAction("并入上一章", self)
            a3.triggered.connect(lambda: self.merged.emit(row - 1, row))
            m.addAction(a3)
        if row < last:
            a4 = QAction("并入下一章", self)
            a4.triggered.connect(lambda: self.merged.emit(row, row + 1))
            m.addAction(a4)
        m.exec(self.list.mapToGlobal(pos))

    def _rename(self, row: int) -> None:
        cur = self.list.item(row).text().split("   ", 1)[-1]
        name, ok = QInputDialog.getText(self, "重命名章节", "标题：", text=cur)
        if ok and name.strip():
            self.renamed.emit(row, name.strip())


# ══════════════════════════════════════════════════════════
#  中栏：稿纸
# ══════════════════════════════════════════════════════════

class Desk(QWidget):
    write = pyqtSignal()
    link = pyqtSignal()
    reformat = pyqtSignal()
    generate = pyqtSignal(int)
    opening = pyqtSignal()
    stop = pyqtSignal()
    retitled = pyqtSignal(str)
    stats = pyqtSignal()
    rewrite = pyqtSignal(str)
    review = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Desk")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._busy = False
        self._last_ctx_color = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.GAP_XL, T.GAP_XL, T.GAP_XL, T.GAP)
        outer.setSpacing(0)

        # ── 文本列（视口是海报，正文列必须窄）──
        col = QWidget()
        col.setMaximumWidth(T.MEASURE)
        stack = QVBoxLayout(col)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)

        self.title = QLineEdit()
        self.title.setObjectName("ChapterTitle")
        self.title.setPlaceholderText("第一章")
        self.title.editingFinished.connect(lambda: self.retitled.emit(self.title.text().strip() or "未命名"))
        stack.addWidget(self.title)

        stack.addSpacing(T.GAP)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(T.GAP)
        self.words = hint("")
        self.ctx = hint("")
        meta.addWidget(self.words)
        meta.addWidget(self.ctx)
        meta.addStretch(1)
        stack.addLayout(meta)

        stack.addSpacing(T.GAP_SM)
        stack.addWidget(rule(soft=True))
        stack.addSpacing(T.GAP)

        self.paper = Manuscript()
        stack.addWidget(self.paper, 1)

        center = QHBoxLayout()
        center.addStretch(0)
        center.addWidget(col, 1)
        center.addStretch(0)
        outer.addLayout(center, 1)

        # ── 操作栏（rule 收进 MEASURE 宽度，与标题下横线对齐）──
        outer.addSpacing(T.GAP_LG)
        bar_col = QWidget()
        bar_col.setMaximumWidth(T.MEASURE)
        bar_col_v = QVBoxLayout(bar_col)
        bar_col_v.setContentsMargins(0, 0, 0, 0)
        bar_col_v.setSpacing(T.GAP_SM)
        bar_col_v.addWidget(rule(soft=True))

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(T.GAP_SM)

        self.cta = QPushButton("续  写")
        self.cta.setObjectName("Primary")
        self.cta.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cta.setMinimumWidth(120)
        self.cta.clicked.connect(self._on_cta)
        bar.addWidget(self.cta)

        self.target = QSpinBox()
        self.target.setRange(200, 100000)
        self.target.setSingleStep(500)
        self.target.setValue(2000)
        self.target.setFixedWidth(96)
        self.target.setToolTip("一键生成的目标字数")
        bar.addWidget(self.target)

        self.gen_btn = ghost_button("一键生成")
        self.gen_btn.setToolTip("连续写作，直到写满目标字数；超出单章上限自动开新章")
        self.gen_btn.clicked.connect(lambda: self.generate.emit(self.target.value()))
        bar.addWidget(self.gen_btn)

        self.more_btn = ghost_button("更多 ⋯")
        self.more_btn.setToolTip("开篇生成 / 接龙续写 / 重排段落")
        self.more_btn.clicked.connect(self._more_menu)
        bar.addWidget(self.more_btn)

        bar.addStretch(1)
        self.status = hint("")
        bar.addWidget(self.status)
        bar_col_v.addLayout(bar)

        bar_center = QHBoxLayout()
        bar_center.addStretch(0)
        bar_center.addWidget(bar_col, 1)
        bar_center.addStretch(0)
        outer.addLayout(bar_center)

    def _more_menu(self) -> None:
        m = QMenu(self)
        a_open = QAction("开篇生成", self)
        a_link = QAction("接龙续写", self)
        a_reformat = QAction("重排段落", self)
        a_review = QAction("审校本章", self)
        a_review.setToolTip("检查文风与情节，给出一份审校报告（不改正文）")
        a_open.triggered.connect(self.opening.emit)
        a_link.triggered.connect(self.link.emit)
        a_reformat.triggered.connect(self.reformat.emit)
        a_review.triggered.connect(self.review.emit)
        m.addAction(a_open)
        m.addAction(a_link)
        m.addSeparator()
        m.addAction(a_reformat)
        m.addAction(a_review)
        m.addSeparator()
        a_stats = QAction("写作统计", self)
        a_stats.triggered.connect(self.stats.emit)
        m.addAction(a_stats)

        # AI 改写只在选中文字时可用 —— 没有选区就置灰，比点了再报错清楚
        has_sel = self.paper.textCursor().hasSelection()
        sub = m.addMenu("AI 改写")
        sub.setEnabled(has_sel)
        for mode, (label, _) in AI.REWRITE_MODES.items():
            a = QAction(label, self)
            a.setEnabled(has_sel)
            a.triggered.connect(lambda _=False, k=mode: self.rewrite.emit(k))
            sub.addAction(a)

        btn = self.more_btn
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _on_cta(self) -> None:
        if self._busy:
            self.stop.emit()
        else:
            self.write.emit()

    # ── 状态 ──
    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.cta.setText("停  止" if busy else "续  写")
        self.gen_btn.setEnabled(not busy)
        self.more_btn.setEnabled(not busy)
        self.target.setEnabled(not busy)
        self.paper.setProperty("busy", "true" if busy else "false")
        self.paper.style().unpolish(self.paper)
        self.paper.style().polish(self.paper)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_chapter(self, ch: Chapter) -> None:
        self.title.setText(ch.title)
        self.paper.load_body(ch.body)

    def set_target(self, n: int) -> None:
        self.target.setValue(max(200, min(100000, n)))

    def refresh_meta(self, p: Project) -> None:
        n = len(p.chapter.body)
        self.words.setText(f"{n:,} 字")
        ratio = AI.context_usage(p)
        pct = int(ratio * 100)
        self.ctx.setText(f"上下文 {pct}%")
        color = T.accent_for(ratio)
        if color != self._last_ctx_color:
            self._last_ctx_color = color
            self.ctx.setStyleSheet(f"color: {color};")


# ══════════════════════════════════════════════════════════
#  右栏：语料与上下文
# ══════════════════════════════════════════════════════════

class Inspector(QWidget):
    add_files = pyqtSignal()
    analyze = pyqtSignal()
    expand = pyqtSignal()
    names = pyqtSignal()
    drop_corpus = pyqtSignal(int)
    preset_applied = pyqtSignal(str)   # 并把并好的整段设定交出去，由主窗标记脏


    def __init__(self):
        super().__init__()
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(T.INSPECT_W)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, T.GAP, 0, 0)
        col.setSpacing(T.GAP_SM)

        # ── 分段切换：一屏只呈现一块，减少同时可见的内容 ──
        self._stack = QStackedWidget()
        self._seg_group = QButtonGroup(self)
        self._seg_group.setExclusive(True)
        self._seg_btns: list[QPushButton] = []
        seg = QHBoxLayout()
        seg.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        seg.setSpacing(T.GAP_XS)
        for i, name in enumerate(("语料", "设定", "记忆")):
            b = QPushButton(name)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, idx=i: self._stack.setCurrentIndex(idx))
            self._seg_group.addButton(b, i)
            seg.addWidget(b)
            self._seg_btns.append(b)
        seg.addStretch(1)
        col.addLayout(seg)
        col.addWidget(rule(soft=True))
        col.addWidget(self._stack, 1)

        # ── 页 0：语料 ──
        p0 = QWidget()
        v0 = QVBoxLayout(p0)
        v0.setContentsMargins(0, 0, 0, 0)
        v0.setSpacing(T.GAP_SM)
        v0.addLayout(self._head("", "add"))
        self.corpus = QListWidget()
        self.corpus.setObjectName("Corpus")
        self.corpus.setFrameShape(QFrame.Shape.NoFrame)
        self.corpus.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.corpus.customContextMenuRequested.connect(self._corpus_menu)
        v0.addWidget(self.corpus, 1)
        arow = QHBoxLayout()
        arow.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        arow.setSpacing(T.GAP_SM)
        self.analyze_btn = ghost_button("全面分析")
        self.analyze_btn.setToolTip("让 AI 通读语料与正文，产出人物、世界、脉络、笔法简报")
        self.analyze_btn.clicked.connect(self.analyze.emit)
        arow.addWidget(self.analyze_btn)
        arow.addStretch(1)
        v0.addLayout(arow)
        self._stack.addWidget(p0)

        # ── 页 1：故事设定 ──
        p1 = QWidget()
        v1 = QVBoxLayout(p1)
        v1.setContentsMargins(0, 0, 0, 0)
        v1.setSpacing(T.GAP_SM)
        v1.addLayout(self._head("故事设定", None))

        # ── 预设标签：点一下把该条并入设定框（追加，不覆盖）──
        # 几十个标签会挤掉设定框，这里限高并允许内部滚动。
        self._preset_wrap = QWidget()
        self._preset_wrap.setObjectName("PresetWrap")
        self._preset_flow = FlowLayout(self._preset_wrap, spacing=5)

        preset_scroll = QScrollArea()
        preset_scroll.setObjectName("PresetScroll")
        preset_scroll.setWidgetResizable(True)
        preset_scroll.setFrameShape(QFrame.Shape.NoFrame)
        preset_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        preset_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        preset_scroll.setMaximumHeight(132)
        preset_scroll.setWidget(self._preset_wrap)
        v1.addWidget(preset_scroll)
        self.reload_presets()

        self.premise = self._notes(
            "写几句大方向即可，例如「都市修仙，主角是外卖员，捡到一枚会吐槽的系统」。"
            "点上方标签可一键并入常见设定；也可留空让开篇生成全权发挥。"
        )
        v1.addWidget(self.premise, 1)
        nrow = QHBoxLayout()
        nrow.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        nrow.setSpacing(T.GAP_SM)
        self.expand_btn = ghost_button("补全设定")
        self.expand_btn.setToolTip("依据你写的大方向，自动补全人物、境界、势力、地名等")
        self.expand_btn.clicked.connect(self.expand.emit)
        nrow.addWidget(self.expand_btn)
        self.names_btn = ghost_button("取书名")
        self.names_btn.setToolTip("依据故事方向拟 6 个候选书名，点选即可改作品名")
        self.names_btn.clicked.connect(self.names.emit)
        nrow.addWidget(self.names_btn)
        self.save_preset_btn = ghost_button("存为预设")
        self.save_preset_btn.setToolTip("把当前设定整段存成一个标签，以后一键复用")
        self.save_preset_btn.clicked.connect(self._save_preset)
        nrow.addWidget(self.save_preset_btn)
        nrow.addStretch(1)
        v1.addLayout(nrow)
        self._stack.addWidget(p1)

        # ── 页 2：前情记忆 ──
        p3 = QWidget()
        v3 = QVBoxLayout(p3)
        v3.setContentsMargins(0, 0, 0, 0)
        v3.setSpacing(T.GAP_SM)
        v3.addLayout(self._head("前情记忆", None))
        self.memory = self._notes("跨会话记忆。正文超出上下文预算时自动压缩旧情节追加于此。")
        v3.addWidget(self.memory, 1)
        self._stack.addWidget(p3)

        self._seg_btns[0].setChecked(True)

        # ── 页脚 ──
        col.addWidget(rule(soft=True))
        foot = QHBoxLayout()
        foot.setContentsMargins(T.SIDE_PAD, T.GAP_SM, 6, 6)
        self.engine_btn = ghost_button("引擎设置")
        foot.addWidget(self.engine_btn)
        foot.addStretch(1)
        foot.addWidget(QSizeGrip(self))
        col.addLayout(foot)

    def show_tab(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        if 0 <= index < len(self._seg_btns):
            self._seg_btns[index].setChecked(True)

    def _head(self, text: str, action: str | None) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(T.SIDE_PAD, 0, T.SIDE_PAD, 0)
        if text:
            h.addWidget(section_title(text))
        h.addStretch(1)
        if action == "add":
            b = ghost_button("添加文件")
            b.clicked.connect(self.add_files.emit)
            h.addWidget(b)
        return h

    # ── 预设标签 ──
    def reload_presets(self) -> None:
        """重建标签栏：内置在前、自定义在后。"""
        flow = self._preset_flow
        while flow.count():
            it = flow.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        builtin, user = presets.all_presets()
        for name, text in builtin:
            self._add_preset_btn(name, text, custom=False)
        for name, text in user:
            self._add_preset_btn(name, text, custom=True)

    def _add_preset_btn(self, name: str, text: str, custom: bool) -> None:
        b = QPushButton(name)
        b.setObjectName("PresetCustom" if custom else "Preset")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(text)
        b.clicked.connect(lambda: self._apply_preset(name, text))
        if custom:
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda pos, n=name: self._preset_menu(n, b.mapToGlobal(pos))
            )
        self._preset_flow.addWidget(b)

    def _preset_menu(self, name: str, gpos) -> None:
        m = QMenu(self)
        a = QAction(f"删除预设「{name}」", self)
        a.triggered.connect(lambda: self._drop_preset(name))
        m.addAction(a)
        m.exec(gpos)

    def _drop_preset(self, name: str) -> None:
        if presets.remove_user(name):
            self.reload_presets()

    def _save_preset(self) -> None:
        """把设定框当前内容存成一个自定义预设。"""
        text = self.premise.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "存为预设", "设定框还是空的，先写点内容。")
            return
        name, ok = QInputDialog.getText(self, "存为预设", "标签名（2-4 字最好记）：")
        if not ok or not name.strip():
            return
        if presets.add_user(name.strip(), text):
            self.reload_presets()

    def _apply_preset(self, name: str, text: str) -> None:
        """把预设追加进设定框 —— 追加而非替换，多条可以叠加。"""
        cur = self.premise.toPlainText().rstrip()
        merged = (cur + "\n" + text) if cur else text
        self.premise.setPlainText(merged)
        cur_txt = self.premise.textCursor()
        cur_txt.movePosition(QTextCursor.MoveOperation.End)
        self.premise.setTextCursor(cur_txt)
        self.preset_applied.emit(merged)

    def _notes(self, placeholder: str) -> QPlainTextEdit:
        e = QPlainTextEdit()
        e.setObjectName("Notes")
        e.setPlaceholderText(placeholder)
        e.setFrameShape(QFrame.Shape.NoFrame)
        e.setMinimumHeight(70)
        return e

    # ── 语料表 ──
    def load_corpus(self, items: list[Corpus]) -> None:
        self.corpus.clear()
        for c in items:
            it = QListWidgetItem(f"{c.name}   ·   {c.chars:,} 字")
            it.setToolTip(c.name)
            self.corpus.addItem(it)
        if not items:
            it = QListWidgetItem("尚未添加语料")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.corpus.addItem(it)

    def _corpus_menu(self, pos: QPoint) -> None:
        it = self.corpus.itemAt(pos)
        if it is None or not (it.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        row = self.corpus.row(it)
        m = QMenu(self)
        a = QAction("移除此文件", self)
        a.triggered.connect(lambda: self.drop_corpus.emit(row))
        m.addAction(a)
        m.exec(self.corpus.mapToGlobal(pos))


# ══════════════════════════════════════════════════════════
#  阅读窗口
# ══════════════════════════════════════════════════════════

class ReaderWindow(QDialog):
    """专注阅读：左栏目录，右栏正文。可调字号，调整后写回项目设置。"""

    SIZE_MIN, SIZE_MAX = 14, 24
    TOP_H = 48      # 顶栏展开高度
    EDGE = 8        # 触发滑出的边缘热区宽度

    def __init__(self, parent, project: Project):
        super().__init__(parent)
        self.setObjectName("Reader")
        self.setWindowTitle(f"阅读 · {project.title}")
        self.setModal(False)
        self.resize(940, 780)
        # 让系统标题栏带上最小化 / 最大化 / 关闭
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.project = project
        self._size = max(self.SIZE_MIN, min(self.SIZE_MAX, int(project.settings.reader_size or 17)))

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # ── 顶栏：默认展开，鼠标离开后自动收起，移到顶部边缘滑出 ──
        self.top = QWidget()
        self.top.setObjectName("ReaderBar")
        self.top.setMaximumHeight(self.TOP_H)
        top_l = QVBoxLayout(self.top)
        top_l.setContentsMargins(0, 0, 0, 0)
        top_l.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(T.GAP_LG, T.GAP_SM, T.GAP, T.GAP_SM)
        bar.setSpacing(T.GAP_SM)
        name = QLabel(project.title)
        name.setObjectName("ProjectName")
        bar.addWidget(name)
        self.crumb = QLabel("")
        self.crumb.setObjectName("Crumb")
        bar.addWidget(self.crumb)

        self.progress = QLabel("")
        self.progress.setObjectName("FieldLabel")
        bar.addWidget(self.progress)
        bar.addStretch(1)

        self.smaller = ghost_button("A－")
        self.smaller.setToolTip("缩小字号")
        self.smaller.clicked.connect(lambda: self._bump(-1))
        bar.addWidget(self.smaller)

        self.size_lbl = QLabel(str(self._size))
        self.size_lbl.setObjectName("FieldLabel")
        self.size_lbl.setFixedWidth(24)
        self.size_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.size_lbl)

        self.bigger = ghost_button("A＋")
        self.bigger.setToolTip("放大字号")
        self.bigger.clicked.connect(lambda: self._bump(1))
        bar.addWidget(self.bigger)
        top_l.addLayout(bar)
        top_l.addWidget(rule(soft=True))
        col.addWidget(self.top)

        # ── 主体：左目录（可滑出）+ 右正文 ──
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.side = QWidget()
        self.side.setObjectName("ReaderSide")
        self.side.setMaximumWidth(T.RAIL_W)
        side_l = QHBoxLayout(self.side)
        side_l.setContentsMargins(0, 0, 0, 0)
        side_l.setSpacing(0)

        self.list = QListWidget()
        self.list.setObjectName("Chapters")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.currentRowChanged.connect(self._show)
        side_l.addWidget(self.list, 1)
        side_l.addWidget(rule(soft=True))
        body.addWidget(self.side)

        # 正文列限宽居中 —— 视口是海报不是文档，长行会杀死阅读
        right = QWidget()
        right_l = QHBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)

        wrap = QWidget()
        wrap.setMaximumWidth(T.MEASURE)
        wrap_l = QVBoxLayout(wrap)
        wrap_l.setContentsMargins(0, 0, 0, 0)
        wrap_l.setSpacing(0)

        self.view = QTextEdit()
        self.view.setObjectName("Reader")
        self.view.setReadOnly(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        wrap_l.addWidget(self.view, 1)

        right_l.addStretch(0)
        right_l.addWidget(wrap, 1)
        right_l.addStretch(0)
        body.addWidget(right, 1)
        col.addLayout(body, 1)

        self._top_open = True
        self._side_open = True
        self._grace = time.monotonic() + 2.0   # 刚打开时给 2 秒宽限，不立刻收起

        # 动画对象常驻复用 —— 不用 DeleteWhenStopped，避免 C++ 对象被删后
        # Python 引用仍指向它、再次 stop() 崩溃。
        self._top_anim = QPropertyAnimation(self.top, b"maximumHeight", self)
        self._top_anim.setDuration(150)
        self._top_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._side_anim = QPropertyAnimation(self.side, b"maximumWidth", self)
        self._side_anim.setDuration(150)
        self._side_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 滚动位置需等文档布局完成才能落 —— 布局就绪前 scrollbar 上限是 0。
        # 用定时轮询而非布局信号：后者触发时机在不同平台不稳。
        self._scroll_mem: dict[int, int] = {}
        self._cur_row = -1
        self._pending_scroll = 0
        self._scroll_tries = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._try_restore_scroll)

        # 上次读到的那一章连同它的滚动位置，一并放进记忆
        if project.settings.reader_scroll:
            first = max(0, min(int(project.settings.reader_chapter or 0), len(project.chapters) - 1))
            self._scroll_mem[first] = int(project.settings.reader_scroll)

        self._load_list()
        self._apply_font()

        # 键盘翻页 / 翻章 —— 不必再回到滚轮
        self.view.installEventFilter(self)
        self.view.verticalScrollBar().valueChanged.connect(self._update_progress)

        # 轮询鼠标位置驱动自动隐藏（不依赖事件在 QTextEdit 里的传递）
        self._poll = QTimer(self)
        self._poll.setInterval(120)
        self._poll.timeout.connect(self._track)
        self._poll.start()

    def _load_list(self) -> None:
        p = self.project
        s = p.settings
        self.list.blockSignals(True)
        self.list.clear()
        for i, ch in enumerate(p.chapters, 1):
            it = QListWidgetItem(f"{i:02d}   {ch.title}")
            it.setToolTip(f"{len(ch.body):,} 字")
            self.list.addItem(it)
        self.list.blockSignals(False)
        # 恢复上次读到的章节（而非编辑器当前章）
        row = max(0, min(int(s.reader_chapter or 0), len(p.chapters) - 1))
        self.list.setCurrentRow(row)

    def _show(self, row: int) -> None:
        if row < 0 or row >= len(self.project.chapters):
            return
        # 离开上一章前，先记住它读到哪儿
        if self._cur_row >= 0:
            self._scroll_mem[self._cur_row] = self.view.verticalScrollBar().value()
        self._cur_row = row

        ch = self.project.chapters[row]
        self.view.setPlainText(ch.body or "（本章尚无内容）")
        self._apply_rhythm()
        self.view.moveCursor(QTextCursor.MoveOperation.Start)
        self.crumb.setText(f"第 {row + 1} / {len(self.project.chapters)} 章 · {len(ch.body):,} 字")

        # 恢复这一章上次的阅读位置；没有就从头
        self._pending_scroll = int(self._scroll_mem.get(row, 0) or 0)
        self._scroll_tries = 0
        if self._pending_scroll > 0:
            self.view.verticalScrollBar().setValue(0)
            self._scroll_timer.start()
        else:
            self.view.verticalScrollBar().setValue(0)
            self._update_progress()

    def _apply_rhythm(self) -> None:
        """给正文施加行距与段距。

        QSS 的 line-height 对 QTextEdit 无效，行距只能靠块格式施加 ——
        否则长篇正文挤成单倍行距，读起来很累。
        """
        cur = self.view.textCursor()
        cur.select(QTextCursor.SelectionType.Document)
        f = QTextBlockFormat()
        f.setLineHeight(190, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        f.setBottomMargin(16)
        f.setTopMargin(0)
        cur.mergeBlockFormat(f)
        cur.clearSelection()

    def _update_progress(self) -> None:
        bar = self.view.verticalScrollBar()
        if bar.maximum() <= 0:
            self.progress.setText("")
            return
        pct = round(bar.value() / bar.maximum() * 100)
        self.progress.setText(f"已读 {pct}%")

    def _try_restore_scroll(self) -> None:
        """轮询直到滚动条上限足够，放下上次的滚动位置，然后停。"""
        if self._pending_scroll <= 0:
            self._scroll_timer.stop()
            return
        bar = self.view.verticalScrollBar()
        if bar.maximum() >= self._pending_scroll:
            bar.setValue(self._pending_scroll)
            self._pending_scroll = 0
            self._scroll_timer.stop()
            self._update_progress()
            return
        self._scroll_tries += 1
        if self._scroll_tries > 40:      # 约 1.2 秒仍不够（如本章很短），放弃
            self._scroll_timer.stop()

    # ── 键盘 ──
    def eventFilter(self, obj, e) -> bool:
        if obj is not self.view or e.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, e)
        key = e.key()
        page = self.view.verticalScrollBar().pageStep()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_PageDown):
            self._scroll_by(page)
            return True
        if key in (Qt.Key.Key_PageUp,):
            self._scroll_by(-page)
            return True
        if key == Qt.Key.Key_Down:
            self._scroll_by(60)
            return True
        if key == Qt.Key.Key_Up:
            self._scroll_by(-60)
            return True
        if key == Qt.Key.Key_Right:
            self._step_chapter(1)
            return True
        if key == Qt.Key.Key_Left:
            self._step_chapter(-1)
            return True
        if key == Qt.Key.Key_Home:
            self.view.verticalScrollBar().setValue(0)
            return True
        if key == Qt.Key.Key_End:
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())
            return True
        return super().eventFilter(obj, e)

    def _scroll_by(self, delta: int) -> None:
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.value() + delta)

    def _step_chapter(self, delta: int) -> None:
        row = max(0, min(self.list.currentRow() + delta, self.list.count() - 1))
        if row != self.list.currentRow():
            self.list.setCurrentRow(row)

    def _bump(self, delta: int) -> None:
        new = max(self.SIZE_MIN, min(self.SIZE_MAX, self._size + delta))
        if new == self._size:
            return
        self._size = new
        self.size_lbl.setText(str(new))
        self._apply_font()
        self.project.settings.reader_size = new
        self.project.save()

    # ── 自动隐藏 ──
    def _track(self) -> None:
        if not self.isVisible():
            return
        if time.monotonic() < self._grace:
            return
        pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(pos):
            self._set_top(False)
            self._set_side(False)
            return
        # 顶栏：展开时只要鼠标还在栏内就保持；收起时靠近顶部边缘才滑出
        if self._top_open:
            want_top = pos.y() < self.TOP_H
        else:
            want_top = pos.y() < self.EDGE
        # 左栏：同理，用其当前宽度判定
        if self._side_open:
            want_side = pos.x() < self.side.width() + self.EDGE
        else:
            want_side = pos.x() < self.EDGE
        self._set_top(want_top)
        self._set_side(want_side)

    def _set_top(self, open_: bool) -> None:
        if self._top_open == open_:
            return
        self._top_open = open_
        a = self._top_anim
        a.stop()
        a.setStartValue(self.top.maximumHeight())
        a.setEndValue(self.TOP_H if open_ else 0)
        a.start()

    def _set_side(self, open_: bool) -> None:
        if self._side_open == open_:
            return
        self._side_open = open_
        a = self._side_anim
        a.stop()
        a.setStartValue(self.side.maximumWidth())
        a.setEndValue(T.RAIL_W if open_ else 0)
        a.start()

    def closeEvent(self, e) -> None:
        self._poll.stop()
        self._scroll_timer.stop()
        # 记住读到哪儿（当前章的滚动位置），下次打开接着看
        s = self.project.settings
        row = max(0, self.list.currentRow())
        if self._cur_row >= 0:
            self._scroll_mem[self._cur_row] = self.view.verticalScrollBar().value()
        s.reader_chapter = row
        s.reader_scroll = int(self._scroll_mem.get(row, 0) or 0)
        self.project.save()
        w = self.parent()
        if w is not None and getattr(w, "_reader", None) is self:
            w._reader = None
        super().closeEvent(e)

    def _apply_font(self) -> None:
        # 行距不能靠 QSS（对 QTextEdit 无效），改由块格式施加，见 _apply_rhythm。
        self.view.setStyleSheet(
            f"QTextEdit#Reader {{"
            f" background: transparent; border: none;"
            f" font-family: {T.SERIF};"
            f" font-size: {self._size}px;"
            f" padding: {T.GAP_LG}px {T.GAP_XL}px;"
            f"}}"
        )
        self._apply_rhythm()


# ══════════════════════════════════════════════════════════
#  指令结果
# ══════════════════════════════════════════════════════════

class ResultDialog(QDialog):
    """指令模式的产出窗口。

    设定资料不该混进正文，所以单开一扇窗。读完后可一键并入作品简报，
    之后每次续写都会把它作为最高优先级的设定依据带上。
    """

    append_to_brief = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent, instruction: str, use_label: str | None = "并入设定"):
        super().__init__(parent)
        self.setWindowTitle("指令结果")
        self.setModal(False)
        self.resize(720, 560)

        col = QVBoxLayout(self)
        col.setContentsMargins(T.GAP_LG, T.GAP_LG, T.GAP_LG, T.GAP)
        col.setSpacing(T.GAP_SM)

        col.addWidget(section_title("指令"))
        echo = hint(instruction)
        echo.setWordWrap(True)
        col.addWidget(echo)

        col.addSpacing(T.GAP_XS)

        self.body = QPlainTextEdit()
        self.body.setObjectName("Notes")
        self.body.setReadOnly(True)
        col.addWidget(self.body, 1)

        bar = QHBoxLayout()
        self.note = hint("正在生成…")
        bar.addWidget(self.note)
        bar.addStretch(1)

        self.copy_btn = ghost_button("复制")
        self.copy_btn.clicked.connect(self._copy)
        bar.addWidget(self.copy_btn)

        self.export_btn = ghost_button("导出")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)
        bar.addWidget(self.export_btn)

        # use_label 为 None 时不提供「并入设定」——审校报告之类的产出不该混进设定
        self._has_use = use_label is not None
        self.use_btn = QPushButton(use_label or "")
        self.use_btn.setObjectName("Primary")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        self.use_btn.setVisible(self._has_use)
        bar.addWidget(self.use_btn)

        close = ghost_button("关闭")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        col.addLayout(bar)

    def append(self, piece: str) -> None:
        self.body.moveCursor(QTextCursor.MoveOperation.End)
        self.body.insertPlainText(piece)
        self.body.moveCursor(QTextCursor.MoveOperation.End)

    def finish(self) -> None:
        self.note.setText("完成")
        has = bool(self.body.toPlainText().strip())
        self.use_btn.setEnabled(has and self._has_use)
        self.export_btn.setEnabled(has)

    def fail(self, msg: str) -> None:
        self.note.setText("失败：" + msg)

    def closeEvent(self, e) -> None:
        # 通知主窗口清空引用 —— 留着指向已隐藏窗口的句柄没有意义
        self.closed.emit()
        super().closeEvent(e)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.body.toPlainText())

    def _use(self) -> None:
        self.append_to_brief.emit(self.body.toPlainText())

    def _export(self) -> None:
        text = self.body.toPlainText()
        if not text.strip():
            return
        start = os.path.join(os.path.expanduser("~"), "指令结果.md")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", start, "Markdown (*.md);;文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.note.setText(f"已导出 → {os.path.basename(path)}")
        except OSError as e:
            self.note.setText(f"导出失败：{e}")


# ══════════════════════════════════════════════════════════
#  写作统计
# ══════════════════════════════════════════════════════════

class StatsDialog(QDialog):
    """全书体量与各章分布。

    字数口径与左栏目录一致 —— 中文按字计，去空白与换行。
    """

    closed = pyqtSignal()

    def __init__(self, parent, project: Project):
        super().__init__(parent)
        self.setWindowTitle(f"写作统计 · {project.title}")
        self.setModal(False)
        self.resize(500, 620)

        counts = [ch.words() for ch in project.chapters]
        total = sum(counts)
        corpus_chars = sum(c.chars for c in project.corpus)

        col = QVBoxLayout(self)
        col.setContentsMargins(T.GAP_LG, T.GAP_LG, T.GAP_LG, T.GAP)
        col.setSpacing(T.GAP_SM)

        col.addWidget(section_title("总览"))
        facts = QGridLayout()
        facts.setContentsMargins(0, 0, 0, 0)
        facts.setHorizontalSpacing(T.GAP_LG)
        facts.setVerticalSpacing(T.GAP_XS)
        rows = [
            ("全书字数", f"{total:,}"),
            ("章节数", str(len(counts))),
            ("平均每章", f"{total // len(counts):,}" if counts else "—"),
            ("最长章节", f"{max(counts):,}" if counts else "—"),
            ("设定字数", f"{len(project.premise.strip()):,}"),
            ("前情记忆", f"{len(project.memory.strip()):,}"),
            ("参考语料", f"{len(project.corpus)} 份 · {corpus_chars:,} 字"),
        ]
        for i, (k, v) in enumerate(rows):
            facts.addWidget(field_label(k), i, 0, Qt.AlignmentFlag.AlignVCenter)
            lb = QLabel(v)
            lb.setObjectName("StatValue")
            facts.addWidget(lb, i, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        facts.setColumnStretch(0, 1)
        col.addLayout(facts)

        col.addSpacing(T.GAP_SM)
        col.addWidget(section_title("各章字数"))

        table = QTableWidget(len(counts), 3)
        table.setHorizontalHeaderLabels(["章节", "字数", "占比"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 78)
        table.setColumnWidth(2, 120)

        for i, (ch, w) in enumerate(zip(project.chapters, counts)):
            table.setItem(i, 0, QTableWidgetItem(f"{i + 1:02d}   {ch.title}"))
            cnt = QTableWidgetItem(f"{w:,}")
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(i, 1, cnt)
            share = (w / total * 100) if total else 0.0
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(share))
            bar.setFormat(f"{share:.1f}%")
            table.setCellWidget(i, 2, bar)
        col.addWidget(table, 1)

        bar_row = QHBoxLayout()
        bar_row.addStretch(1)
        close = ghost_button("关闭")
        close.clicked.connect(self.close)
        bar_row.addWidget(close)
        col.addLayout(bar_row)

    def closeEvent(self, e) -> None:
        self.closed.emit()
        super().closeEvent(e)


# ══════════════════════════════════════════════════════════
#  历史版本
# ══════════════════════════════════════════════════════════

class VersionsDialog(QDialog):
    """把 projects/backup 里的快照，变成可查看、可回退的版本列表。

    列举时只读文件名与 stat，不解析 JSON —— 40 份大项目一起解析会卡住界面，
    正文详情等到选中某一行时才按需读入。
    """

    restored = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent, project: Project):
        super().__init__(parent)
        self.setWindowTitle(f"历史版本 · {project.title}")
        self.setModal(False)
        self.resize(560, 520)

        self._pid = project.id
        self._files: list[str] = []

        col = QVBoxLayout(self)
        col.setContentsMargins(T.GAP_LG, T.GAP_LG, T.GAP_LG, T.GAP)
        col.setSpacing(T.GAP_SM)

        head = QHBoxLayout()
        head.addWidget(section_title("历史版本"))
        head.addStretch(1)
        self.count = hint("")
        head.addWidget(self.count)
        col.addLayout(head)

        self.list = QListWidget()
        self.list.setObjectName("Plain")
        self.list.currentRowChanged.connect(self._detail)
        col.addWidget(self.list, 1)

        col.addWidget(section_title("详情"))
        self.info = QLabel("尚未选择版本")
        self.info.setObjectName("Hint")
        self.info.setWordWrap(True)
        self.info.setMinimumHeight(76)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        col.addWidget(self.info)

        bar = QHBoxLayout()
        self.note = hint("")
        bar.addWidget(self.note)
        bar.addStretch(1)
        self.restore_btn = ghost_button("恢复此版本")
        self.restore_btn.setEnabled(False)
        self.restore_btn.setToolTip("回滚到选中的版本。当前状态会先存一份快照，可再次回退。")
        self.restore_btn.clicked.connect(self._restore)
        bar.addWidget(self.restore_btn)
        close = ghost_button("关闭")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        col.addLayout(bar)

        self._reload()

    def _reload(self) -> None:
        rows = store.list_backups(self._pid)
        self._files = [name for name, _, _ in rows]
        self.list.clear()
        for name, mtime, size in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            self.list.addItem(f"{stamp}    ·    {size / 1024:.0f} KB")
        self.count.setText(f"{len(rows)} 份")
        self.info.setText("尚未选择版本" if rows else "还没有历史版本 —— 每次保存都会自动留档。")
        self.restore_btn.setEnabled(False)

    def _detail(self, row: int) -> None:
        if not (0 <= row < len(self._files)):
            self.info.setText("尚未选择版本")
            self.restore_btn.setEnabled(False)
            return
        self.restore_btn.setEnabled(True)
        try:
            p = store.Project.from_dict(store.read_backup(self._pid, self._files[row]))
        except (OSError, ValueError, TypeError) as e:
            self.info.setText(f"这个版本读不出来，可能已损坏：{e}")
            self.restore_btn.setEnabled(False)
            return
        words = sum(ch.words() for ch in p.chapters)
        self.info.setText(
            f"《{p.title}》  ·  {len(p.chapters)} 章  ·  {words:,} 字\n"
            f"设定 {len(p.premise.strip()):,} 字  ·  "
            f"记忆 {len(p.memory.strip()):,} 字  ·  "
            f"语料 {len(p.corpus)} 份"
        )

    def _restore(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._files)):
            return
        name = self._files[row]
        stamp = self.list.item(row).text().split("    ·    ")[0]
        r = QMessageBox.warning(
            self, "恢复历史版本",
            f"将把当前作品回滚到 {stamp} 这一版。\n\n"
            "当前状态会先存一份快照，之后仍可从本面板再次回退。确定吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            store.restore_backup(self._pid, name)
        except (OSError, ValueError) as e:
            self.note.setText(f"恢复失败：{e}")
            return
        self.note.setText("已恢复，正在重新载入…")
        self.restored.emit(name)

    def closeEvent(self, e) -> None:
        self.closed.emit()
        super().closeEvent(e)


# ══════════════════════════════════════════════════════════
#  引擎设置
# ══════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    def __init__(self, parent, s: Settings):
        super().__init__(parent)
        self.setWindowTitle("引擎设置")
        self.setModal(True)
        self.setMinimumWidth(460)
        s = Settings(**{k: getattr(s, k) for k in Settings.__dataclass_fields__})
        self.s = s

        g = QGridLayout(self)
        g.setContentsMargins(T.GAP_LG, T.GAP_LG, T.GAP_LG, T.GAP)
        g.setHorizontalSpacing(T.GAP)
        g.setVerticalSpacing(T.GAP_SM)
        g.setColumnStretch(1, 1)

        r = 0
        def add(label, widget):
            nonlocal r
            g.addWidget(field_label(label), r, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(widget, r, 1)
            r += 1

        self.url = QLineEdit(s.base_url)
        self.url.setPlaceholderText("https://api.deepseek.com/v1")
        add("接口地址", self.url)

        self.key = QLineEdit(s.api_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("sk-…")
        add("API Key", self.key)

        self.model = QLineEdit(s.model)
        self.model.setPlaceholderText("deepseek-chat")
        add("模型", self.model)

        self.temp = QDoubleSpinBox()
        self.temp.setRange(0.0, 2.0)
        self.temp.setSingleStep(0.05)
        self.temp.setDecimals(2)
        self.temp.setValue(s.temperature)
        add("温度", self.temp)

        self.topp = QDoubleSpinBox()
        self.topp.setRange(0.05, 1.0)
        self.topp.setSingleStep(0.05)
        self.topp.setDecimals(2)
        self.topp.setValue(s.top_p)
        add("核采样", self.topp)

        self.pres = QDoubleSpinBox()
        self.pres.setRange(-2.0, 2.0)
        self.pres.setSingleStep(0.05)
        self.pres.setDecimals(2)
        self.pres.setValue(s.presence_penalty)
        add("存在惩罚", self.pres)

        self.freq = QDoubleSpinBox()
        self.freq.setRange(-2.0, 2.0)
        self.freq.setSingleStep(0.05)
        self.freq.setDecimals(2)
        self.freq.setValue(s.frequency_penalty)
        add("频率惩罚", self.freq)

        self.mtok = QSpinBox()
        self.mtok.setRange(128, 8192)
        self.mtok.setSingleStep(128)
        self.mtok.setValue(s.max_tokens)
        add("单次上限", self.mtok)

        self.ctx = QSpinBox()
        self.ctx.setRange(800, 60000)
        self.ctx.setSingleStep(500)
        self.ctx.setValue(s.context_budget)
        add("上下文预算", self.ctx)

        self.cor = QSpinBox()
        self.cor.setRange(800, 60000)
        self.cor.setSingleStep(500)
        self.cor.setValue(s.corpus_budget)
        add("语料预算", self.cor)

        self.chmax = QSpinBox()
        self.chmax.setRange(0, 200000)
        self.chmax.setSingleStep(500)
        self.chmax.setSpecialValueText("不限")
        self.chmax.setValue(s.chapter_max)
        add("单章上限", self.chmax)

        self.tchars = QSpinBox()
        self.tchars.setRange(0, 200000)
        self.tchars.setSingleStep(500)
        self.tchars.setSpecialValueText("不限")
        self.tchars.setValue(s.target_chars)
        add("默认生成字数", self.tchars)

        self.autorev = QCheckBox("一键生成时自动审校，不合格按意见重写")
        self.autorev.setChecked(bool(getattr(s, "auto_review", True)))
        g.addWidget(self.autorev, r, 0, 1, 2)
        r += 1

        self.font = QSpinBox()
        self.font.setRange(12, 28)
        self.font.setSingleStep(1)
        self.font.setValue(getattr(s, "editor_size", 16) or 16)
        add("正文字号", self.font)

        self.glow = QCheckBox("窗口流光边框（呼吸 + 流动描边）")
        self.glow.setChecked(bool(getattr(s, "glow_border", False)))
        g.addWidget(self.glow, r, 0, 1, 2)
        r += 1

        # 边框取色：七彩渐变 / 单色，单色时右侧出现调色板
        self.glow_rainbow = QCheckBox("七彩渐变")
        self.glow_rainbow.setChecked(bool(getattr(s, "glow_rainbow", True)))
        self.glow_rainbow.toggled.connect(self._sync_glow_row)

        self.glow_color = QComboBox()
        for c in GLOW_PALETTE:
            self.glow_color.addItem(c)
            self.glow_color.setItemData(
                self.glow_color.count() - 1, QColor(c), Qt.ItemDataRole.DecorationRole
            )
            self.glow_color.setItemData(
                self.glow_color.count() - 1, c, Qt.ItemDataRole.UserRole
            )
        self.glow_color.setToolTip("单色流光的主色")
        cur = getattr(s, "glow_color", GLOW_PALETTE[0])
        idx = GLOW_PALETTE.index(cur) if cur in GLOW_PALETTE else 0
        self.glow_color.setCurrentIndex(idx)

        grow = QHBoxLayout()
        grow.setContentsMargins(0, 0, 0, 0)
        grow.setSpacing(T.GAP_SM)
        grow.addWidget(self.glow_rainbow)
        grow.addStretch(1)
        self._glow_lbl = field_label("主色")
        grow.addWidget(self._glow_lbl)
        grow.addWidget(self.glow_color)
        g.addLayout(grow, r, 0, 1, 2)
        r += 1
        self._sync_glow_row()

        note = hint(
            "预算以「字」计。正文超出上下文预算后，旧情节会自动压缩进前情记忆。"
            "单章上限控制「一键生成」时自动开新章的字数。\n"
            "存在惩罚压低「已经出现过的词」再次中选的概率，逼模型换说法、换话题；"
            "频率惩罚按出现次数累加，专治车轱辘话。两者 0.3 起步，"
            "写得散、跑题就往下调。核采样 1.0 为不限制。"
        )
        note.setWordWrap(True)
        g.addWidget(note, r, 0, 1, 2)
        r += 1

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = ghost_button("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        bar.addWidget(cancel)
        bar.addWidget(ok)
        g.addLayout(bar, r, 0, 1, 2)

    def _sync_glow_row(self) -> None:
        """七彩模式下隐藏调色板；单色模式才需要选主色。"""
        single = not self.glow_rainbow.isChecked()
        self._glow_lbl.setVisible(single)
        self.glow_color.setVisible(single)

    def result_settings(self) -> Settings:
        # 保留原样、本对话框不涉及的字段：theme / editor_size / reader_*。
        # 直接构造 Settings 会把这些重置为默认值 —— 例如保存设置后配色丢失。
        keep = {
            k: getattr(self.s, k)
            for k in Settings.__dataclass_fields__
            if k not in {
                "base_url", "api_key", "model", "temperature", "max_tokens",
                "context_budget", "corpus_budget", "target_chars", "chapter_max",
                "editor_size", "top_p", "presence_penalty", "frequency_penalty",
                "auto_review", "glow_border", "glow_rainbow", "glow_color",
            }
        }
        return Settings(
            **keep,
            base_url=self.url.text().strip() or "https://api.deepseek.com/v1",
            api_key=self.key.text().strip(),
            model=self.model.text().strip() or "deepseek-chat",
            temperature=float(self.temp.value()),
            top_p=float(self.topp.value()),
            presence_penalty=float(self.pres.value()),
            frequency_penalty=float(self.freq.value()),
            max_tokens=int(self.mtok.value()),
            context_budget=int(self.ctx.value()),
            corpus_budget=int(self.cor.value()),
            target_chars=int(self.tchars.value()),
            chapter_max=int(self.chmax.value()),
            editor_size=int(self.font.value()),
            auto_review=bool(self.autorev.isChecked()),
            glow_border=bool(self.glow.isChecked()),
            glow_rainbow=bool(self.glow_rainbow.isChecked()),
            glow_color=str(self.glow_color.currentData() or GLOW_PALETTE[0]),
        )


# ══════════════════════════════════════════════════════════
#  主窗口
# ══════════════════════════════════════════════════════════

TEXT_EXT = {".txt", ".md", ".markdown", ".text", ".log", ".json", ".csv"}


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("续墨")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1040, 660)
        self.resize(1340, 860)
        self.setAcceptDrops(True)

        self.project: Project = store.load_last()

        # 后台线程（整理记忆、流式续写）与主线程 autosave 都会动同一个 Project，
        # 写盘必须串行 —— 否则 autosave 可能把写了一半的记忆落盘。
        self._plock = threading.RLock()

        # 配色先于任何控件创建，避免首帧闪色
        T.set_palette(self.project.settings.theme)
        self._apply_qss()

        self.bridge = Bridge()
        self.bridge.delta.connect(self._on_delta)
        self.bridge.status.connect(self._on_status)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.settings_ready.connect(self._on_settings_ready)
        self.bridge.memory_ready.connect(self._on_memory_ready)
        self.bridge.names_ready.connect(self._on_names_ready)
        self.bridge.fix_tail.connect(self._on_fix_tail)

        self._stop = threading.Event()
        self._handle: AI.StreamHandle | None = None
        self._busy = False
        self._dirty = False
        self._loading = False   # 载入画面期间：控件自身的 textChanged 不算改动
        self._stream_target = "paper"
        self._result: ResultDialog | None = None
        self._reader: ReaderWindow | None = None
        self._versions: VersionsDialog | None = None
        self._stats: StatsDialog | None = None
        self._rewrite_wrote = False
        self._rewrite_label = ""
        self._renewed_memory = False   # 本次续写是否压缩过记忆（需回填右栏）
        self._bg_memory = False        # 后台线程是否正在写 memory
        self._fade_effect = None       # 切章淡入：效果与动画各建一次复用
        self._fade_anim = None
        self._tail_done = threading.Event()   # 自动审校：等待主线程替换稿纸尾部

        # 一键生成状态
        self._gen_active = False
        self._auto_analyzing = False
        self._want_analyze = False
        self._gen_target = 0
        self._gen_done = 0
        self._chapter_max = 0
        self._pending_summaries: list[str] = []

        # ── 布局 ──
        self._root = QVBoxLayout(self)
        root = self._root
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar(self)
        root.addWidget(self.titlebar)
        root.addWidget(rule(soft=True))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.rail = Rail()
        self.desk = Desk()
        self.inspector = Inspector()

        body.addWidget(self.rail)
        body.addWidget(rule(soft=True))
        body.addWidget(self.desk, 1)
        body.addWidget(rule(soft=True))
        body.addWidget(self.inspector)
        root.addLayout(body, 1)

        # ── 流光呼吸边框（覆盖层，默认关闭）──
        self.glow = GlowBorder(self)
        self.glow.setGeometry(self.rect())
        self.glow.raise_()
        self._apply_round_mask()
        self._sync_glow()

        # ── 连线 ──
        self.rail.picked.connect(self._pick_chapter)
        self.rail.added.connect(self._add_chapter)
        self.rail.renamed.connect(self._rename_chapter)
        self.rail.removed.connect(self._remove_chapter)
        self.rail.reordered.connect(self._reorder_chapters)
        self.rail.merged.connect(self._merge_chapters)
        self.desk.write.connect(self._start_write)
        self.desk.link.connect(self._start_link)
        self.desk.reformat.connect(self._start_reformat)
        self.desk.review.connect(self._start_review)
        self.desk.generate.connect(self._start_generate)
        self.desk.opening.connect(self._start_opening)
        self.desk.stats.connect(self._open_stats)
        self.desk.rewrite.connect(self._start_rewrite)
        self.desk.paper.rewrite_requested.connect(self._start_rewrite)
        self.inspector.names.connect(self._start_names)
        self.desk.stop.connect(self._request_stop)
        self.desk.retitled.connect(self._retitle)
        self.desk.paper.textChanged.connect(self._on_text)
        # 右栏设定与记忆同样是可编辑的手稿：不标脏的话，autosave 看不见它们，
        # 而切换作品前的「还有未保存的改动」也会直接放行 —— 稿子就这么丢了。
        self.inspector.premise.textChanged.connect(self._on_notes)
        self.inspector.preset_applied.connect(self._on_notes)
        self.inspector.memory.textChanged.connect(self._on_notes)
        self.inspector.add_files.connect(self._pick_files)
        self.inspector.analyze.connect(self._start_analysis)
        self.inspector.expand.connect(self._start_expand)
        self.inspector.drop_corpus.connect(self._drop_corpus)
        self.inspector.engine_btn.clicked.connect(self._open_settings)

        # ── 流式增量缓冲：把高频小 chunk 合并成低频大块，避免事件队列堆积 ──
        self._pending_delta: list[str] = []
        self._delta_timer = QTimer(self)
        self._delta_timer.setInterval(45)
        self._delta_timer.timeout.connect(self._flush_delta)
        self._delta_timer.start()
        self._round_status = False

        # ── 自动保存 ──
        self.autosave = QTimer(self)
        self.autosave.setInterval(8000)
        self.autosave.timeout.connect(self._autosave)
        self.autosave.start()

        # 全书字数要遍历所有章节，每次按键都算一遍太贵 —— 收敛成末次触发
        self._meta_timer = QTimer(self)
        self._meta_timer.setSingleShot(True)
        self._meta_timer.setInterval(600)
        self._meta_timer.timeout.connect(self._refresh_totals)

        self._load_all(first=True)

    # ══════════════ 载入 ══════════════

    def _load_all(self, first: bool = False) -> None:
        p = self.project
        # 载入期间各控件会触发 textChanged —— 那是程序写的，不是作者敲的。
        # 不挡住就会像旧版那样：一启动 8 秒后白存一次盘。
        self._loading = True
        try:
            self._load_all_inner(p, first)
        finally:
            self._loading = False

    def _load_all_inner(self, p: Project, first: bool) -> None:
        self.rail.load(p.chapters, p.current)
        self.desk.set_chapter(p.chapter)
        self.desk.refresh_meta(p)
        self.inspector.load_corpus(p.corpus)
        self.inspector.memory.setPlainText(p.memory)
        self.inspector.premise.setPlainText(p.premise)
        self.titlebar.set_crumb(
            f"第 {p.current + 1} / {len(p.chapters)} 章", title=p.title
        )
        self.desk.set_target(p.settings.target_chars or 2000)
        if first:
            self.desk.set_status("就绪" if p.settings.api_key else "先在右下角填写 API Key")

    def _autosave(self) -> None:
        """定时兜底保存。无改动时跳过 —— 否则每 8 秒都触发一次备份轮转，
        40 份历史回滚点实际只覆盖几分钟。"""
        if self._dirty:
            self._save()

    # ══════════════ 公共设施 ══════════════

    def _apply_qss(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(
                T.build_qss(editor_size=self.project.settings.editor_size)
            )

    def _worker(self, fn: Callable[[], None]) -> None:
        """统一的后台线程入口。

        曾经每个 AI 动作都要重抄一遍「起线程 → try/except → AIError 与其它异常
        分别转成失败信号」这六行样板。抽走之后，每个动作只剩下它自己的业务。
        """
        def wrapped() -> None:
            try:
                fn()
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=wrapped, daemon=True).start()

    def _save(self, force_backup: bool = False) -> None:
        """落盘。

        force_backup=True 时无条件留一份快照（跳过自动备份的节流）——
        一次生成写完 / 被打断都算得上有意义的还原点，值得单独存档。
        """
        self._stash()
        try:
            with self._plock:
                self.project.save(force_backup=force_backup)
            self._dirty = False
        except OSError as e:
            self.desk.set_status(f"保存失败：{e}")

    def _apply_round_mask(self) -> None:
        """把窗口裁成圆角。

        无边框窗口本身是方的 —— 圆角描边画上去，四角仍是直角。
        用遮罩把四角切掉，窗口才真正是圆的。
        """
        r = int(GlowBorder.CORNER)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), r, r)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_round_mask()
        if getattr(self, "glow", None) is not None:
            self.glow.setGeometry(self.rect())
            self.glow.raise_()

    def closeEvent(self, e) -> None:
        # 完整的停止流程（含掐断 socket），否则关闭后连接还会挂到超时
        if self._busy:
            self._request_stop()
        self._save()
        super().closeEvent(e)

    def _set_busy(self, busy: bool) -> None:
        """统一的忙碌开关。

        除了切 menubar 状态，还要锁住换章与换作品：流式产出的文字总是写在
        「当前正在看的那一章」，中途切走的话，剩下的输出会落进另一个作品 /
        另一章里 —— 而且是流式追加，混进去很难再挑出来。
        """
        self._busy = busy
        self.desk.set_busy(busy)
        self.rail.list.setEnabled(not busy)
        self.titlebar.works_btn.setEnabled(not busy)

    def _guard_idle(self) -> bool:
        """换章 / 换作品前的拦截。理由见 _set_busy。"""
        if self._busy:
            self.desk.set_status("正在生成中 —— 先按「停止」再切换")
            return False
        return True

    # ══════════════ 章节 ══════════════

    def _stash(self) -> None:
        """把界面上的编辑并回 project。

        凡是要以 project 为依据做判断的动作 —— 续写装配上下文、统计字数、
        导出、切换作品 —— 都必须先跑这一遍。autosave 每 8 秒才写一次盘，
        少了这一步，判断用的是上一次存盘的版本：刚敲进去的字不会进上下文，
        模型照着旧稿往下接，看起来就是"它又重复了一遍"。
        """
        p = self.project
        p.chapter.title = self.desk.title.text().strip() or "未命名"
        p.chapter.body = self.desk.paper.body()
        p.premise = self.inspector.premise.toPlainText()
        # memory 可能正被后台线程改写（压缩前情 / 整理整章）。此时若用右栏
        # 的旧文本盖回去，后台累积的摘要会丢失 —— 跳过，等后台结果回填。
        if not self._bg_memory and not self._busy:
            p.memory = self.inspector.memory.toPlainText()

    def _pick_chapter(self, row: int) -> None:
        if row == self.project.current or not self._guard_idle():
            return
        self._stash()
        self.project.current = row
        ch = self.project.chapter
        self.desk.set_chapter(ch)
        self.desk.refresh_meta(self.project)
        self.titlebar.set_crumb(
            f"第 {row + 1} / {len(self.project.chapters)} 章"
        )
        self._crossfade()
        self._dirty = True

    def _crossfade(self) -> None:
        """切换章节时的交叉淡入：内容换了，空间没有断裂。

        效果与动画各建一次、反复复用 —— 原先每次切章都新建一对，动画以
        self 为父不会被回收，长会话里频繁翻章会持续累积 QObject。
        动画结束不摘掉 effect（常驻、opacity 归 1 无副作用），下次直接重跑。
        """
        if self._fade_effect is None:
            self._fade_effect = QGraphicsOpacityEffect(self.desk.paper)
            self.desk.paper.setGraphicsEffect(self._fade_effect)
            self._fade_anim = QPropertyAnimation(self._fade_effect, b"opacity", self)
            self._fade_anim.setDuration(200)
            self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.25)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def _add_chapter(self) -> None:
        if not self._guard_idle():
            return
        self._stash()
        p = self.project
        finished = p.chapter.body
        if finished.strip():
            self._pending_summaries.append(finished)
        p.chapters.append(Chapter(_uid(), f"第{p.current + 2}章"))
        p.current = len(p.chapters) - 1
        self.rail.load(p.chapters, p.current)
        self.desk.set_chapter(p.chapter)
        self.desk.refresh_meta(p)
        self.desk.title.setFocus()
        self.desk.title.selectAll()
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self._dirty = True

    def _rename_chapter(self, row: int, name: str) -> None:
        self.project.chapters[row].title = name
        self.rail.load(self.project.chapters, self.project.current)
        if row == self.project.current:
            self.desk.title.setText(name)
        self._dirty = True

    def _remove_chapter(self, row: int) -> None:
        p = self.project
        if len(p.chapters) <= 1:
            self.desk.set_status("至少要保留一章")
            return
        if not self._guard_idle():
            return
        if not (0 <= row < len(p.chapters)):
            return
        # 先把编辑中的内容并回 project —— 否则 set_chapter 会用旧数据盖掉
        # 当前章尚未落盘的编辑，等于丢稿。
        self._stash()
        cur = p.current
        del p.chapters[row]
        # 删的是当前章 → 落到同位置；删的是它前面的章 → 索引前移一位。
        if cur == row:
            p.current = min(row, len(p.chapters) - 1)
        elif cur > row:
            p.current = cur - 1
        else:
            p.current = cur
        p.current = max(0, min(p.current, len(p.chapters) - 1))
        self.rail.load(p.chapters, p.current)
        self.desk.set_chapter(p.chapter)
        self.desk.refresh_meta(p)
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self._dirty = True

    def _reorder_chapters(self, ids: list) -> None:
        """按列表当前顺序（id 序列）重排章节，保持正在编辑的那一章不变。"""
        p = self.project
        if len(ids) != len(p.chapters):
            return
        if not self._guard_idle():
            return
        by_id = {ch.id: ch for ch in p.chapters}
        if any(i not in by_id for i in ids):
            return
        cur_id = p.chapters[p.current].id
        p.chapters = [by_id[i] for i in ids]
        p.current = next((k for k, ch in enumerate(p.chapters) if ch.id == cur_id), 0)
        self.rail.load(p.chapters, p.current)
        self.desk.refresh_meta(p)
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self.desk.set_status("章节顺序已调整")
        self._dirty = True
        self._save()

    def _merge_chapters(self, keep_row: int, drop_row: int) -> None:
        """把 drop_row 的正文接到 keep_row 末尾，然后删除 drop_row。"""
        p = self.project
        n = len(p.chapters)
        if keep_row == drop_row or not (0 <= keep_row < n and 0 <= drop_row < n):
            return
        if not self._guard_idle():
            return
        keep_title = p.chapters[keep_row].title
        drop_title = p.chapters[drop_row].title
        r = QMessageBox.question(
            self, "合并章节",
            f"把「{drop_title}」的正文并入「{keep_title}」末尾，并删除前者。\n\n"
            "原稿会先备份到 projects/backup/。确定吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        self._stash()
        self._backup()

        cur = p.current
        keep = p.chapters[keep_row]
        drop = p.chapters[drop_row]
        head = keep.body.rstrip()
        tail = drop.body.strip()
        if tail:
            keep.body = (head + "\n\n" + tail) if head else tail
        del p.chapters[drop_row]

        if cur == drop_row:
            p.current = keep_row
        elif cur > drop_row:
            p.current = cur - 1
        else:
            p.current = cur
        p.current = max(0, min(p.current, len(p.chapters) - 1))

        self.rail.load(p.chapters, p.current)
        # 当前章的内容可能变了（被并入 或 就是被删的那章），重载编辑器
        if cur == keep_row or cur == drop_row:
            self.desk.set_chapter(p.chapter)
        self.desk.refresh_meta(p)
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self.desk.set_status(f"已把「{drop_title}」并入「{keep_title}」")
        self._dirty = True
        self._save()

    def _retitle(self, name: str) -> None:
        self.project.chapter.title = name
        self.rail.load(self.project.chapters, self.project.current)
        self._dirty = True

    def _refresh_totals(self) -> None:
        """左栏页脚的全书字数。

        先把编辑中的内容并回 project —— 否则统计的是上次存盘的版本，
        正在敲的这一章会漏掉。
        """
        self._stash()
        self.rail.set_totals(
            len(self.project.chapters),
            sum(c.words() for c in self.project.chapters),
        )

    def _on_text(self) -> None:
        # 载入正文、流式回填都会触发 textChanged，但它们不是用户编辑 ——
        # 若在此标脏，程序一启动 8 秒后就会白存一次盘。
        if self.desk.paper.is_quiet():
            return
        self._dirty = True
        self.desk.refresh_meta(self.project)
        self._meta_timer.start()

    def _on_notes(self, *_ignored) -> None:
        """右栏「设定 / 记忆」被改动。"""
        if self._loading:
            return
        self._dirty = True

    # ══════════════ 语料 ══════════════

    def _add_paths(self, paths: list[str]) -> None:
        added = 0
        skipped = []
        for fp in paths:
            try:
                ext = os.path.splitext(fp)[1].lower()
                if ext not in TEXT_EXT:
                    skipped.append(os.path.basename(fp))
                    continue
                text = None
                for enc in ("utf-8", "utf-8-sig", "gbk", "big5"):
                    try:
                        with open(fp, encoding=enc) as f:
                            text = f.read()
                        break
                    except UnicodeDecodeError:
                        continue
                if not text:
                    skipped.append(os.path.basename(fp))
                    continue
                self.project.corpus.append(Corpus(_uid(), os.path.basename(fp), text))
                added += 1
            except OSError:
                skipped.append(os.path.basename(fp))
        self.inspector.load_corpus(self.project.corpus)
        msg = f"已加入 {added} 份语料" if added else ""
        if skipped:
            msg += ("，" if msg else "") + f"跳过 {len(skipped)} 个非文本文件"
        self.desk.set_status(msg or "没有可读取的文件")
        self._dirty = True

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加语料", "", "文本文件 (*.txt *.md *.markdown *.json *.csv);;所有文件 (*.*)"
        )
        if paths:
            self._add_paths(paths)

    def _drop_corpus(self, row: int) -> None:
        if 0 <= row < len(self.project.corpus):
            del self.project.corpus[row]
            self.inspector.load_corpus(self.project.corpus)
            self._dirty = True

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._add_paths(paths)
            e.acceptProposedAction()

    # ══════════════ 阅读 ══════════════

    def open_reader(self) -> None:
        self._stash()
        if self._reader is not None:
            self._reader.close()
        self._reader = ReaderWindow(self, self.project)
        self._reader.show()

    def _open_stats(self) -> None:
        """写作统计。先 stash —— 统计要算当前正在编辑的这一章。"""
        self._stash()
        if self._stats is not None:
            self._stats.close()
        self._stats = StatsDialog(self, self.project)
        self._stats.closed.connect(self._forget_stats)
        self._stats.show()

    def open_versions(self) -> None:
        """历史版本：误删、误改、AI 改坏了都能退回去。"""
        self._save()
        if self._versions is not None:
            self._versions.close()
        self._versions = VersionsDialog(self, self.project)
        self._versions.closed.connect(self._forget_versions)
        self._versions.restored.connect(self._on_version_restored)
        self._versions.show()

    def _forget_stats(self) -> None:
        self._stats = None

    def _forget_versions(self) -> None:
        self._versions = None

    def _forget_result(self) -> None:
        self._result = None

    def _close_panels(self) -> None:
        """换作品 / 删作品之前，先把挂着旧项目的窗口收掉。

        以前只在切换后把引用置 None —— Qt 仍然持有这些窗口，于是统计窗、
        版本窗、阅读窗继续挂在屏幕上，显示的却是上一部作品的内容；主窗口
        已经换人了，看着像同一个程序在同时讲两个故事。

        必须在动磁盘**之前**调用：阅读窗关闭时会写下阅读进度，它持有的是旧
        项目对象，若这时那部作品已经被删掉，这一存又会把它写回来。
        """
        for attr in ("_result", "_stats", "_versions", "_reader"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                setattr(self, attr, None)   # 先断引用，再关：closeEvent 里的
                dlg.close()                 # 回调重复置 None 也无害

    def _on_version_restored(self, name: str) -> None:
        """快照已写回磁盘，把界面整个重建成那一版的样子。"""
        try:
            self.project = store.open_project(self.project.id)
        except (OSError, ValueError) as e:
            self.desk.set_status(f"重新载入失败：{e}")
            return
        self._close_panels()
        self._after_switch()
        self.desk.set_status(f"已回滚到 {name.split('-', 1)[-1].replace('.json', '')} 这一版")

    # ══════════════ 设置 ══════════════

    # ══════════════ 作品 ══════════════

    def rename_project(self) -> None:
        cur = self.project.title
        name, ok = QInputDialog.getText(self, "重命名作品", "作品名：", text=cur)
        if not ok:
            return
        name = name.strip()
        if not name or name == cur:
            return
        self.project.title = name
        self.titlebar.title_lbl.setText(name)
        self._dirty = True
        self._save()
        self.desk.set_status(f"已更名为《{name}》")

    def prompt_new_project(self) -> None:
        if not self._guard_idle() or not self._confirm_discard():
            return
        name, ok = QInputDialog.getText(
            self, "新建作品", "作品名（可留空，之后用「AI 起名」自动命名）：", text=""
        )
        if not ok:
            return
        name = name.strip() or "未命名作品"
        # 先把旧作品落盘 —— 此刻界面里的内容仍属于它。
        # 若在切换后再 _save()，会把旧界面内容写进新项目。
        self._close_panels()
        self._save(force_backup=True)
        old = self.project.settings
        self.project = store.new_project(name)
        self.project.settings = store.Settings(
            **{k: getattr(old, k) for k in store.Settings.__dataclass_fields__}
        )
        self._after_switch()

    def works_menu(self) -> None:
        m = QMenu(self)
        a_rename = QAction("重命名作品", self)
        a_new = QAction("新建作品…", self)
        a_open = QAction("打开作品…", self)
        a_delete = QAction("删除作品…", self)
        a_import = QAction("导入备份…", self)
        a_rename.triggered.connect(self.rename_project)
        a_new.triggered.connect(self.prompt_new_project)
        a_open.triggered.connect(self.open_project)
        a_delete.triggered.connect(self.delete_project)
        a_import.triggered.connect(self.import_backup)
        a_versions = QAction("历史版本…", self)
        a_versions.triggered.connect(self.open_versions)
        m.addAction(a_rename)
        m.addSeparator()
        m.addAction(a_new)
        m.addAction(a_open)
        m.addAction(a_delete)
        m.addSeparator()
        m.addAction(a_import)
        m.addAction(a_versions)
        # 报 issue 时第一个要问的就是「哪一版」，放在菜单里免得起 exe 名
        v = QAction(f"续墨 v{store.APP_VERSION}", self)
        v.setEnabled(False)
        m.addSeparator()
        m.addAction(v)
        btn = self.titlebar.works_btn
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        r = QMessageBox.question(
            self, "还有未保存的改动",
            "当前作品有改动尚未写入磁盘。继续将先保存它。",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
        )
        if r == QMessageBox.StandardButton.Cancel:
            return False
        self._save()
        return True

    def open_project(self) -> None:
        if not self._guard_idle() or not self._confirm_discard():
            return
        items = store.list_projects()
        if not items:
            self.desk.set_status("还没有其它作品")
            return
        labels = [f"{t}   （{i}）" for i, t in items]
        choice, ok = QInputDialog.getItem(self, "打开作品", "选择：", labels, 0, False)
        if not ok:
            return
        pid = items[labels.index(choice)][0]
        self._close_panels()
        try:
            self.project = store.open_project(pid)
        except (OSError, ValueError) as e:
            self.desk.set_status(f"打开失败：{e}")
            return
        self._after_switch()

    def delete_project(self) -> None:
        """删除一部作品（含备份）。当前作品被删则切到剩余作品或新建。"""
        if not self._guard_idle():
            return

        items = store.list_projects()
        if not items:
            self.desk.set_status("还没有可删除的作品")
            return

        labels = [f"{t}   （{i}）" for i, t in items]
        choice, ok = QInputDialog.getItem(self, "删除作品", "选择要删除的作品：", labels, 0, False)
        if not ok:
            return
        pid = items[labels.index(choice)][0]
        title = next(t for i, t in items if i == pid)

        r = QMessageBox.warning(
            self, "删除作品",
            f"将永久删除《{title}》及其全部备份，无法恢复。\n\n确定删除吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        # 删当前作品前先落盘，避免 _dirty 触发无谓的重新保存
        if pid == self.project.id:
            self._dirty = False
        self._close_panels()      # 必须早于 delete：详见 _close_panels 的说明
        store.delete_project(pid)

        remaining = store.list_projects()
        if pid == self.project.id:
            if remaining:
                self.project = store.open_project(remaining[0][0])
            else:
                old = self.project.settings
                self.project = store.new_project("未命名作品")
                self.project.settings = store.Settings(
                    **{k: getattr(old, k) for k in store.Settings.__dataclass_fields__}
                )
            self._after_switch()
        self.desk.set_status(f"已删除《{title}》")

    def import_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入项目备份", os.path.expanduser("~"), "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            incoming = store.Project.from_dict(data)
        except (OSError, ValueError) as e:
            self.desk.set_status(f"导入失败：{e}")
            return
        if not self._guard_idle() or not self._confirm_discard():
            return
        self._close_panels()
        self._save(force_backup=True)
        incoming.id = store._uid()
        self.project = incoming
        self._after_switch()
        self.desk.set_status(f"已导入《{incoming.title}》")

    def _after_switch(self) -> None:
        """换了作品之后，把界面整个重建一遍。"""
        # 先关掉指向旧作品的窗口。只把引用置 None 是不够的：Qt 仍持有它们，
        # 于是统计窗、版本窗、阅读窗会继续挂在屏幕上，显示上一部作品的内容，
        # 而主窗口已经换人了 —— 看着像同一个程序同时在讲两个故事。
        self._close_panels()
        T.set_palette(self.project.settings.theme)
        self._apply_qss()
        self.titlebar.sync_theme_label()
        self._stream_target = "paper"
        self._result = None
        self._stats = None
        self._versions = None
        self._load_all(first=True)
        self.project.save()
        self._dirty = False

    # ══════════════ 导出 ══════════════

    def export_menu(self) -> None:
        self._stash()
        m = QMenu(self)
        a_all = QAction("全部章节   ·   txt", self)
        a_one = QAction("当前章节   ·   txt", self)
        a_notes = QAction("设定资料   ·   md", self)
        a_json = QAction("项目备份   ·   json", self)
        a_all.triggered.connect(self._export_all)
        a_one.triggered.connect(self._export_chapter)
        a_notes.triggered.connect(self._export_notes)
        a_json.triggered.connect(self._export_backup)
        m.addAction(a_all)
        m.addAction(a_one)
        m.addSeparator()
        m.addAction(a_notes)
        m.addAction(a_json)
        btn = self.titlebar.export_btn
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _ask_path(self, title: str, default_name: str, filt: str) -> str:
        start = os.path.join(os.path.expanduser("~"), default_name)
        path, _ = QFileDialog.getSaveFileName(self, title, start, filt)
        return path or ""

    def _write(self, path: str, text: str, ok_msg: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self.desk.set_status(f"导出失败：{e}")
            return
        self.desk.set_status(f"{ok_msg} → {os.path.basename(path)}")

    @staticmethod
    def _safe(name: str) -> str:
        for c in '\\/:*?"<>|':
            name = name.replace(c, "_")
        return name.strip() or "未命名"

    @staticmethod
    def _clean(text: str) -> str:
        """导出前清一遍接续产生的重复段，历史正文也能受益。"""
        return AI.collapse_blocks(AI.collapse_repeats(text))

    def _export_all(self) -> None:
        p = self.project
        path = self._ask_path("导出全部章节", f"{self._safe(p.title)}.txt", "文本文件 (*.txt)")
        if not path:
            return
        blocks = []
        for i, ch in enumerate(p.chapters, 1):
            title = ch.title.strip() or f"第{i}章"
            blocks.append(f"{title}\n\n{self._clean(ch.body).strip()}")
        self._write(path, "\n\n\n".join(blocks), f"已导出 {len(p.chapters)} 章")

    def _export_chapter(self) -> None:
        p = self.project
        ch = p.chapter
        title = ch.title.strip() or f"第{p.current + 1}章"
        path = self._ask_path(
            "导出当前章节", f"{self._safe(p.title)}-{self._safe(title)}.txt", "文本文件 (*.txt)"
        )
        if not path:
            return
        self._write(path, f"{title}\n\n{self._clean(ch.body).strip()}", "已导出本章")

    def _export_notes(self) -> None:
        p = self.project
        parts = [f"# {p.title}\n"]
        if p.premise.strip():
            parts.append("## 作品设定\n\n" + p.premise.strip() + "\n")
        if p.memory.strip():
            parts.append("## 前情记忆\n\n" + p.memory.strip() + "\n")
        if p.corpus:
            parts.append("## 参考语料\n")
            for c in p.corpus:
                parts.append(f"### {c.name}\n\n{c.text.strip()}\n")
        if len(parts) == 1:
            self.desk.set_status("还没有可导出的设定")
            return
        path = self._ask_path("导出设定资料", f"{self._safe(p.title)}-设定.md", "Markdown (*.md)")
        if not path:
            return
        self._write(path, "\n".join(parts), "已导出设定")

    def _export_backup(self) -> None:
        p = self.project
        path = self._ask_path("导出项目备份", f"{self._safe(p.title)}.json", "JSON (*.json)")
        if not path:
            return
        try:
            p.save()
            src = p.path()
            with open(src, encoding="utf-8") as f:
                data = f.read()
        except OSError as e:
            self.desk.set_status(f"导出失败：{e}")
            return
        self._write(path, data, "已导出备份")

    def cycle_theme(self) -> None:
        T.set_palette(T.next_palette())
        self.project.settings.theme = T.current_palette()
        self._apply_qss()
        self._sync_glow()
        self.titlebar.sync_theme_label()
        self.desk.refresh_meta(self.project)
        self.desk.set_status(f"配色：{T.label_for(T.current_palette())}")
        self._dirty = True
        self._save()

    def _open_settings(self) -> None:
        d = SettingsDialog(self, self.project.settings)
        if d.exec() == QDialog.DialogCode.Accepted:
            self.project.settings = d.result_settings()
            self._apply_qss()
            self.desk.refresh_meta(self.project)
            if self.project.settings.target_chars:
                self.desk.set_target(self.project.settings.target_chars)
            self._sync_glow()
            self.desk.set_status("设置已保存")
            self._dirty = True

    def _sync_glow(self) -> None:
        """把设置（开关 / 取色模式 / 单色值）同步给边框，并让出描边占用的边距。"""
        s = self.project.settings
        self.glow.configure(
            rainbow=bool(getattr(s, "glow_rainbow", True)),
            color=str(getattr(s, "glow_color", GLOW_PALETTE[0]) or GLOW_PALETTE[0]),
        )
        # 描边画在窗口边缘内侧，会压住贴边的控件 —— 开启时给内容留出同等边距。
        pad = int(GlowBorder.THICKNESS) + 2 if s.glow_border else 0
        self._root.setContentsMargins(pad, pad, pad, pad)
        if s.glow_border:
            self.glow.show()
            self.glow.raise_()
            self.glow.start()
        else:
            self.glow.stop()
            # 仅停表不够 —— 控件仍可见、仍画最后一帧，于是"关了还有边框"
            self.glow.hide()

    # ══════════════ 续写 ══════════════

    def _request_stop(self) -> None:
        """停止：既要通知循环，也要掐断 socket。

        只设标志位不够 —— 服务器不发下一帧时，读取线程会一直阻塞，
        永远看不到标志位。关闭底层连接才能立即让它退出。
        """
        self._stop.set()
        if self._handle is not None:
            self._handle.stop()
        self.desk.set_status("正在停止…")

    def _guard_key(self) -> bool:
        if not self.project.settings.api_key:
            self.desk.set_status("尚未填写 API Key —— 右下角「引擎设置」")
            self._open_settings()
            return False
        return True

    def _start_write(self) -> None:
        if self._busy or not self._guard_key():
            return
        # 先并回编辑器里的东西：autosave 是 8 秒一跳，直接读 project.chapter.body
        # 拿到的是上一次存盘的版本 —— 刚敲的字不会进上下文，也不会进"是否走
        # 开篇模式"的判断（<40 字会被误判成空稿，用续写语气写开篇）。
        self._stash()
        body = self.project.chapter.body
        # 空稿（或只有零星几字）走开篇模式。
        # 用「续写」语气写开头，模型会把故事当成中段来写，于是永远没有真正的开场。
        if len(body.strip()) < 40:
            self._launch(body, renew=False, opening=True)
        else:
            self._launch(body, renew=True)

    def _begin_stream(self, target: str, status: str) -> None:
        """起一次流式任务：清停止信号、建句柄、切忙碌态。"""
        self._stop.clear()
        self._handle = AI.StreamHandle()
        self._pending_delta.clear()   # 上一任务若未排净，别串到这次的内容里
        self._set_busy(True)
        self.desk.set_status(status)
        self._stream_target = target
        self._rewrite_wrote = False

    def _start_opening(self) -> None:
        """开篇生成：按右栏「故事设定」起名、写第一章开头（仅限空白正文）。"""
        if self._busy or not self._guard_key():
            return
        self._stash()
        p = self.project
        # 允许全空：没有任何设定时，由 AI 从零发挥。
        if p.chapter.body.strip():
            r = QMessageBox.question(
                self, "开篇生成",
                "当前章节已有正文。开篇生成会从光标处续入，可能与前文风格不一。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._launch(p.chapter.body, renew=False, opening=True)

    def _start_names(self) -> None:
        """AI 起名：依据故事方向生成命名方案，填入「故事设定」。"""
        if self._busy or not self._guard_key():
            return
        self._stash()
        p = self.project
        self._set_busy(True)
        self.desk.set_status("正在取名…")

        def work() -> None:
            out = AI.suggest_titles(p, on_step=lambda t: self.bridge.status.emit(t))
            self.bridge.names_ready.emit(out)

        self._worker(work)

    def _start_generate(self, n: int) -> None:
        """一键生成：连续写作到目标字数，单章超限自动开新章。"""
        if self._busy or not self._guard_key():
            return
        self._stash()
        n = max(200, int(n))
        self.project.settings.target_chars = n
        self._gen_active = True
        self._gen_target = n
        self._gen_done = 0
        self._chapter_max = int(self.project.settings.chapter_max or 0)
        self._pending_summaries = []
        body = self.project.chapter.body
        blank = len(body.strip()) < 40
        self._launch(body, renew=not blank, target_chars=n, opening=blank)

    def _start_link(self) -> None:
        """接龙：以光标所在位置之前的内容为起点，覆盖其后文字。"""
        if self._busy or not self._guard_key():
            return
        cur = self.desk.paper.textCursor()
        pos = cur.selectionStart() if cur.hasSelection() else cur.position()
        tail = self.desk.paper.body()[:pos]
        if len(tail.strip()) < 20:
            self.desk.set_status("把光标放在要接续的位置之后")
            return
        self._launch(tail, renew=False, truncate_to=pos)

    def _launch(
        self,
        tail: str,
        renew: bool,
        truncate_to: int | None = None,
        target_chars: int = 0,
        opening: bool = False,
    ) -> None:
        p = self.project
        self._begin_stream("paper", "正在连接…")

        if truncate_to is not None:
            body = self.desk.paper.body()[:truncate_to]
            self.desk.paper.load_body(body)
            p.chapter.body = body

        snapshot = AI.build_messages(p, tail=tail, opening=opening)

        def work() -> None:
            s = p.settings
            if renew and AI.needs_renewal(p):
                self.bridge.status.emit("上下文接近上限 —— 正在压缩前情…")
                with self._plock:
                    AI.renew_memory(p, on_step=lambda t: self.bridge.status.emit(t))
                snapshot[:] = AI.build_messages(p, tail=p.chapter.body)
                self._renewed_memory = True
                self.bridge.status.emit("记忆已更新，继续续写…")

            self.bridge.status.emit("正在续写…")
            first = True
            for piece in AI.stream_completion(
                s, snapshot, handle=self._handle,
                should_stop=self._stop.is_set, target_chars=target_chars,
                on_retry=lambda n, w: self.bridge.status.emit(
                    f"接口限流，第 {n} 次重试（{w:.0f}s 后）…"
                ),
                on_round=lambda n: self._mark_round(n),
                on_round_text=self._round_review,
            ):
                if first:
                    self.bridge.status.emit("")
                    first = False
                self.bridge.delta.emit(piece)
            self.bridge.done.emit()

        self._worker(work)

    def _on_fix_tail(self, n_remove: int, new_text: str) -> None:
        """主线程：自动审校判定需改，把稿纸末尾 n_remove 个字换成重写正文。

        先把缓冲里还没写进稿纸的增量 flush 掉 —— 否则稿纸末尾比本轮产出短，
        按字数删尾会删错位置。
        """
        self._flush_delta()
        self.desk.paper.replace_tail(n_remove, new_text)
        self._tail_done.set()

    def _round_review(self, round_text: str) -> "str | None":
        """一键生成时，每写完一轮自动审校；不合格按意见重写。

        在后台线程被 stream_completion 回调，会阻塞到主线程完成尾部替换。
        审校出任何错都不该打断写作，故整段包进 try。
        """
        if not getattr(self.project.settings, "auto_review", False):
            return None
        if not self._gen_active or len(round_text.strip()) < 60:
            return None
        try:
            fixed, _report = AI.review_and_fix(
                self.project, round_text,
                on_step=lambda t: self.bridge.status.emit(t),
            )
        except Exception:  # noqa: BLE001
            return None
        if fixed.strip() == round_text.strip():
            self.bridge.status.emit("本段审校通过")
            return None
        self._tail_done.clear()
        self.bridge.fix_tail.emit(len(round_text), fixed)
        self._tail_done.wait(timeout=15)
        self.bridge.status.emit("本段审校未过，已按意见重写")
        return fixed

    def _start_rewrite(self, mode: str) -> None:
        """就地改写选中的一段：润色 / 扩写 / 缩写 / 重写。

        为什么不是一个新窗口：改写是「改稿」，眼睛必须盯着上下文。文字在原地
        一句句长出来，才知道改完是否接得住上一段。
        """
        if self._busy or not self._guard_key():
            return
        paper = self.desk.paper
        cur = paper.textCursor()
        if not cur.hasSelection():
            self.desk.set_status("先选中一段文字，再走右键菜单或「更多 → AI 改写」")
            return

        body = paper.body()
        start, end = cur.selectionStart(), cur.selectionEnd()
        selection = body[start:end].strip()
        if len(selection) < 8:
            self.desk.set_status("选中的文字太少，改写没有意义")
            return

        p = self.project
        p.chapter.title = self.desk.title.text().strip() or "未命名"
        p.chapter.body = body

        label, _ = AI.REWRITE_MODES.get(mode, AI.REWRITE_MODES["polish"])
        self._rewrite_label = label
        self._begin_stream("rewrite", f"正在{label}…")

        # 上下文与「腾位置」都在发起前算好：一旦删掉选区，原文就只剩下面
        # Manuscript 里留的那一份副本。
        messages = AI.build_rewrite_messages(
            p, selection, mode, before=body[:start], after=body[end:]
        )
        paper.begin_rewrite()

        def work() -> None:
            for piece in AI.stream_completion(
                p.settings, messages, handle=self._handle,
                should_stop=self._stop.is_set,
                on_retry=lambda n, w: self.bridge.status.emit(
                    f"接口限流，第 {n} 次重试（{w:.0f}s 后）…"
                ),
            ):
                self.bridge.delta.emit(piece)
            self.bridge.done.emit()

        self._worker(work)

    # ── 流式回调 ──
    def _on_delta(self, piece: str) -> None:
        if self._stream_target in ("dialog", "reformat", "review"):
            if self._result is not None:
                self._result.append(piece)
            return
        self._pending_delta.append(piece)

    def _mark_round(self, n: int) -> None:
        """接续轮开始时给出可见反馈 —— 模型在复述旧内容期间界面不该像死了。"""
        self._round_status = True
        if not self._gen_active:
            self.desk.set_status(f"正在接续（第 {n + 1} 段）…")

    def _flush_delta(self) -> None:
        """把缓冲区里的增量一次性写入稿纸。空则立即返回，几乎零成本。"""
        if not self._pending_delta:
            return
        text = "".join(self._pending_delta)
        self._pending_delta.clear()
        if self._stream_target == "rewrite":
            self.desk.paper.put_rewrite(text)
            self._rewrite_wrote = True
            return
        if self._round_status and not self._gen_active:
            self._round_status = False
            self.desk.set_status("")
        self.desk.paper.append_delta(text)
        if self._gen_active:
            self._gen_done += len(text)
            self._maybe_split_chapter()
            if self._gen_target:
                self.desk.set_status(
                    f"一键生成中 · {self._gen_done:,} / {self._gen_target:,} 字"
                )

    def _maybe_split_chapter(self) -> None:
        """一键生成时，当前章写满上限就落章、开新章，流式继续。

        切点回退到段落/句子边界，避免把句子劈成两半。
        超出的部分不丢弃，作为新章的开头。
        """
        if not self._chapter_max:
            return
        body = self.desk.paper.body()
        if len(body) < self._chapter_max:
            return
        cut = _find_break(body, self._chapter_max)
        head = body[:cut].rstrip()
        tail = body[cut:].lstrip("\n")
        if len(head) < 200:
            return  # 切点太靠前，等下一轮再切
        p = self.project
        p.chapter.title = self.desk.title.text().strip() or "未命名"
        p.chapter.body = head
        if head.strip():
            self._pending_summaries.append(head)
        p.chapters.append(Chapter(_uid(), f"第{p.current + 2}章"))
        p.current = len(p.chapters) - 1
        p.chapter.body = tail
        self.rail.load(p.chapters, p.current)
        self.desk.title.setText(p.chapter.title)
        self.desk.paper.swap_body(tail)
        self.desk.refresh_meta(p)
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self._dirty = True
        self._save()

    def _on_status(self, text: str) -> None:
        if self._gen_active and text:
            return
        self.desk.set_status(text)

    def _on_done(self) -> None:
        self._flush_delta()
        self._set_busy(False)

        if self._stream_target == "rewrite":
            self.desk.paper.end_rewrite()
            self.project.chapter.body = self.desk.paper.body()
            self.desk.refresh_meta(self.project)
            self.rail.load(self.project.chapters, self.project.current)
            self.desk.set_status(f"{self._rewrite_label}完成")
            self._dirty = True
            self._save(force_backup=True)
            return

        if self._stream_target in ("dialog", "reformat", "review"):
            if self._result is not None:
                self._result.finish()
            self.desk.set_status("指令完成")
            return

        self.desk.paper.end_stream()
        body = AI.collapse_blocks(AI.collapse_repeats(self.desk.paper.body()))
        if body != self.desk.paper.body():
            self.desk.paper.swap_body(body)
        self.project.chapter.body = body
        self.desk.refresh_meta(self.project)
        was_gen = self._gen_active
        if was_gen:
            self.desk.set_status(f"一键生成完成 · 共 {self._gen_done:,} 字")
        else:
            self.desk.set_status("已写入")
        self._gen_active = False
        # 续写途中压缩过记忆：把后台写入的新记忆回填右栏，
        # 否则紧接着的 _save 会拿右栏旧文本把它盖掉。
        if self._renewed_memory:
            self._renewed_memory = False
            self.inspector.memory.setPlainText(self.project.memory)
        self._dirty = True
        self._save(force_backup=True)
        self._flush_summaries()
        # 一键生成写了大批内容 —— 自动通读一遍，把新出现的人物/设定并进设定。
        # 若还有章节要压缩进记忆，等它完成再分析，避免两个后台任务同时写项目。
        if was_gen and self.project.settings.api_key:
            if self._pending_summaries:
                self._want_analyze = True
            else:
                self._auto_analyze()

    def _flush_summaries(self) -> None:
        """把生成过程中翻页留下的整章，压缩进前情记忆。"""
        if not self._pending_summaries:
            return
        p = self.project
        if not p.settings.api_key:
            self._pending_summaries.clear()
            return
        pending = list(self._pending_summaries)
        self.desk.set_status(f"正在整理 {len(pending)} 章前情记忆…")

        def work() -> None:
            done = 0
            self._bg_memory = True   # 期间禁止 autosave 用界面旧值盖 memory
            try:
                for body in pending:
                    # 与主线程 autosave 争同一个 Project，必须排它
                    with self._plock:
                        AI.summarize_chapter(p, body)
                    done += 1
            except Exception as e:  # noqa: BLE001
                # 失败的部分留在队列里，下次生成结束再试，不丢内容
                self._pending_summaries[:] = pending[done:]
                self.bridge.status.emit(f"记忆整理中断（已完成 {done} 章）：{e}")
                return
            finally:
                self._bg_memory = False
            self._pending_summaries[:] = []
            self.bridge.memory_ready.emit(p.memory)

        self._worker(work)

    def _on_memory_ready(self, text: str) -> None:
        # 右栏是「语料 / 设定 / 记忆」三页，索引从 0 起 —— 记忆是第 2 页。
        self.inspector.show_tab(2)
        self.inspector.memory.setPlainText(text)
        self.project.memory = text
        self.desk.set_status("前情记忆已更新")
        self._dirty = True
        self._save()
        # 记忆整理完毕，接着做延后的自动分析
        if self._want_analyze:
            self._want_analyze = False
            self._auto_analyze()

    def _on_names_ready(self, text: str) -> None:
        """解析候选书名，弹窗让作者点选，选中即改作品名。"""
        titles: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 去掉可能的序号前缀，取「｜」前的书名部分
            line = re.sub(r"^[0-9]+[.、)）]?\s*", "", line)
            name = re.split(r"[｜|]", line, 1)[0].strip()
            name = name.strip("《》「」【】 ").strip()
            if name and name not in titles:
                titles.append(name)

        self._set_busy(False)

        if not titles:
            self.desk.set_status("没能解析出书名，请重试")
            return

        choice, ok = QInputDialog.getItem(
            self, "选择书名", "候选书名：", titles, 0, False
        )
        if ok and choice:
            self.project.title = choice
            self.titlebar.title_lbl.setText(choice)
            self.titlebar.set_crumb(
                f"第 {self.project.current + 1} / {len(self.project.chapters)} 章"
            )
            self.desk.set_status(f"作品名已改为《{choice}》")
            self._dirty = True
            self._save()
        else:
            self.desk.set_status("未改动作品名")

    def _on_settings_ready(self, text: str) -> None:
        """补全 / 分析的产出 —— 整体替换「设定」。

        不再追加：补全设定与全面分析产出的都是完整设定，旧内容已被吸收进
        结果里（分析也会读到作者的设定方向）。若在旧文本后再叠一坨，点几次
        就积出好几份互相打架的设定，续写时被整段当作「最高优先级」灌给
        模型，剧情自然前后矛盾、看不懂。
        """
        auto = self._auto_analyzing
        self._auto_analyzing = False
        merged = text.strip()
        box = self.inspector.premise
        box.setPlainText(merged)
        self.project.premise = merged
        self._set_busy(False)
        self.desk.set_status("设定已自动更新" if auto else "设定已更新")
        self._dirty = True
        self._save()

    def _on_failed(self, msg: str) -> None:
        self._flush_delta()
        self._set_busy(False)
        self._gen_active = False

        if self._stream_target in ("dialog", "reformat", "review"):
            if self._result is not None:
                self._result.fail(msg)
            self.desk.set_status(msg)
            return

        if self._stream_target == "rewrite":
            # 失败也得让稿面回到可用状态：一个字没出就把原文放回去，
            # 出了字则保留已有成果 —— 半截版本也胜过把选段凭空弄丢。
            if self._rewrite_wrote:
                self.desk.paper.end_rewrite()
                note = f"{self._rewrite_label}中断：{msg}（已保留已生成的部分）"
            else:
                self.desk.paper.restore_rewrite()
                note = f"{self._rewrite_label}失败：{msg}（原文未改动）"
            self.project.chapter.body = self.desk.paper.body()
            self.desk.refresh_meta(self.project)
            self._dirty = True
            self._save(force_backup=True)
            self.desk.set_status(note)
            return

        self.desk.paper.end_stream()
        # 中断前已翻页的章节照样进记忆
        self._flush_summaries()
        self.desk.set_status(msg)

    # ══════════════ 重排段落 ══════════════

    def _backup(self) -> None:
        """替换正文这类高风险操作前，先手动留一个快照。

        与自动轮转共用 store.snapshot —— 两处各写一份规则早晚会漂移。
        """
        try:
            self.project.save()
            store.snapshot(self.project.id)
        except OSError as e:
            self.desk.set_status(f"备份失败：{e}")

    def _start_reformat(self) -> None:
        if self._busy or not self._guard_key():
            return
        self._stash()
        if len(self.project.chapter.body.strip()) < 50:
            self.desk.set_status("正文太短，无需重排")
            return

        self._set_busy(True)
        self.desk.set_status("正在重排段落…")
        self._stream_target = "reformat"
        p = self.project

        self._result = ResultDialog(self, "重排段落 —— 只调整换行，不改一个字")
        self._result.use_btn.setText("替换正文")
        self._result.append_to_brief.connect(self._apply_reformat)
        self._result.closed.connect(self._forget_result)
        self._result.show()

        def work() -> None:
            out = AI.reformat_chapter(p, on_step=lambda t: self.bridge.status.emit(t))
            self.bridge.delta.emit(out)
            self.bridge.done.emit()

        self._worker(work)

    def _start_review(self) -> None:
        """审校本章：检查文风与情节，产出报告（不改正文）。

        生成器—审校器分离：续写用提示词约束「怎么写」，但模型不一定遵守；
        这里用一次独立调用回头验收，问题逐条列出，改不改由作者决定。
        """
        if self._busy or not self._guard_key():
            return
        self._stash()
        p = self.project
        if len(p.chapter.body.strip()) < 50:
            self.desk.set_status("正文太短，无需审校")
            return
        self._set_busy(True)
        self.desk.set_status("正在审校正文…")
        self._stream_target = "review"

        # 报告不并入设定，因此不给「并入设定」按钮
        self._result = ResultDialog(self, "审校报告 —— 文风与情节", use_label=None)
        self._result.closed.connect(self._forget_result)
        self._result.show()

        def work() -> None:
            out = AI.review_chapter(p, p.chapter.body,
                                    on_step=lambda t: self.bridge.status.emit(t))
            self.bridge.delta.emit(out)
            self.bridge.done.emit()

        self._worker(work)

    def _apply_reformat(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self._result:
            return
        # 二次确认，因为会整体替换
        r = QMessageBox.question(
            self, "替换正文",
            "将用重排后的版本替换当前章节正文。原稿会先备份到 projects/backup/。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        self._backup()
        self.project.chapter.body = text
        self.desk.paper.load_body(text)
        self.desk.refresh_meta(self.project)
        self._dirty = True
        self._save()
        self.desk.set_status("已替换，原稿已备份")
        if self._result:
            self._result.close()
            self._result = None

    # ══════════════ 补全设定 / 全面分析 ══════════════

    def _start_expand(self) -> None:
        """补全设定：按作者给的大方向，扩写成人/境界/势力齐全的完整设定。"""
        if self._busy or not self._guard_key():
            return
        self._stash()
        p = self.project
        if not p.premise.strip():
            self.desk.set_status("先写几句故事方向，我才有依据补全")
            return
        self._set_busy(True)
        self.desk.set_status("正在补全设定…")

        def work() -> None:
            out = AI.expand_settings(p, on_step=lambda t: self.bridge.status.emit(t))
            self.bridge.settings_ready.emit(out)

        self._worker(work)

    def _start_analysis(self) -> None:
        if self._busy or not self._guard_key():
            return
        self._stash()
        self._run_analysis()

    def _auto_analyze(self) -> None:
        """一键生成结束后自动通读全文，把新内容并进设定。"""
        self._run_analysis(auto=True)

    def _run_analysis(self, auto: bool = False) -> None:
        if self._busy:
            return
        self._auto_analyzing = auto
        self._set_busy(True)
        self.desk.set_status("正在通读全文，更新设定…" if auto else "正在通读材料…")
        p = self.project

        def work() -> None:
            out = AI.analyze_corpus(p, on_step=lambda t: self.bridge.status.emit(t))
            self.bridge.settings_ready.emit(out)

        self._worker(work)

    # ══════════════ 窗口事件 ══════════════

    def keyPressEvent(self, e) -> None:
        mod = e.modifiers()
        key = e.key()
        ctrl = mod & Qt.KeyboardModifier.ControlModifier

        if ctrl and key == Qt.Key.Key_S:
            self._save()
            self.desk.set_status("已保存")
            return
        if ctrl and key == Qt.Key.Key_Return:
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._start_link()
            else:
                self._start_write()
            return
        if key == Qt.Key.Key_Escape and self._busy:
            # 走完整的停止流程：只置标志位掐不断 socket，服务器不发下一帧时
            # 读取线程会一直阻塞，Esc 看起来就像没反应。
            self._request_stop()
            return
        if ctrl and key == Qt.Key.Key_N:
            self._add_chapter()
            return
        super().keyPressEvent(e)
