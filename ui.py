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
import os
import threading
import time

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QColor, QCursor, QFont, QKeySequence, QTextBlockFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QSizeGrip, QSpinBox,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

import ai as AI
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Manuscript")
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setPlaceholderText("在此落笔，或按下方的「续写」，让引擎接着往下写。")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._quiet = False
        self._guard = False
        self._rhythm = QTimer(self)
        self._rhythm.setSingleShot(True)
        self._rhythm.setInterval(160)
        self._rhythm.timeout.connect(self._apply_rhythm)
        self.textChanged.connect(self._on_changed)

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
    def load_body(self, text: str) -> None:
        self._quiet = True
        self.setPlainText(text)
        self._quiet = False
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self._apply_rhythm()

    def begin_stream(self) -> None:
        self._quiet = True
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def swap_body(self, text: str) -> None:
        """流式中途换章：整体替换内容，保持静默，光标落到末尾。"""
        self._quiet = True
        self.setPlainText(text)
        cur = self.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cur)
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

    def body(self) -> str:
        return self.toPlainText()


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


# ══════════════════════════════════════════════════════════
#  小构件
# ══════════════════════════════════════════════════════════

def rule(soft: bool = False) -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
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

        row.addSpacing(T.GAP)

        # 作品名：可点击重命名 —— 层级上仅次于品牌本身
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("ProjectName")
        self.title_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_lbl.setToolTip("点击重命名")
        self.title_lbl.mousePressEvent = lambda e: self._win.rename_project()
        row.addWidget(self.title_lbl, 0, Qt.AlignmentFlag.AlignBottom)

        self.crumb = QLabel("")
        self.crumb.setObjectName("Crumb")
        row.addWidget(self.crumb, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)

        self.works_btn = QPushButton("作品")
        self.works_btn.setObjectName("ThemeToggle")
        self.works_btn.setToolTip("新建、打开、导入")
        self.works_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.works_btn.clicked.connect(self._win.works_menu)
        row.addWidget(self.works_btn)
        row.addSpacing(T.GAP_SM)

        self.read_btn = QPushButton("阅读")
        self.read_btn.setObjectName("ThemeToggle")
        self.read_btn.setToolTip("在独立窗口里通读全书")
        self.read_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.read_btn.clicked.connect(self._win.open_reader)
        row.addWidget(self.read_btn)
        row.addSpacing(T.GAP_SM)

        self.export_btn = QPushButton("导出")
        self.export_btn.setObjectName("ThemeToggle")
        self.export_btn.setToolTip("导出作品与设定")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._win.export_menu)
        row.addWidget(self.export_btn)
        row.addSpacing(T.GAP_SM)

        self.theme_btn = QPushButton(T.label_for(T.current_palette()))
        self.theme_btn.setObjectName("ThemeToggle")
        self.theme_btn.setToolTip("切换配色")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self._cycle_theme)
        row.addWidget(self.theme_btn)
        row.addSpacing(T.GAP_SM)

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

class Rail(QWidget):
    picked = pyqtSignal(int)
    added = pyqtSignal()
    renamed = pyqtSignal(int, str)
    removed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("Rail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(T.RAIL_W)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, T.GAP, 0, T.GAP_SM)
        col.setSpacing(T.GAP_SM)

        head = QHBoxLayout()
        head.setContentsMargins(16, 0, 14, 0)
        head.addWidget(section_title("章节"))
        head.addStretch(1)
        self.count = hint("")
        head.addWidget(self.count)
        col.addLayout(head)

        self.list = QListWidget()
        self.list.setObjectName("Chapters")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentRowChanged.connect(self._on_row)
        col.addWidget(self.list, 1)

        col.addWidget(rule(soft=True))
        foot = QHBoxLayout()
        foot.setContentsMargins(14, 0, 14, 0)
        b = ghost_button("＋  新建章节")
        b.clicked.connect(self.added.emit)
        foot.addWidget(b)
        foot.addStretch(1)
        col.addLayout(foot)

    def _on_row(self, row: int) -> None:
        if row >= 0:
            self.picked.emit(row)

    def load(self, chapters: list[Chapter], current: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for i, ch in enumerate(chapters, 1):
            it = QListWidgetItem(f"{i:02d}   {ch.title}")
            it.setToolTip(f"{ch.words():,} 字")
            self.list.addItem(it)
        self.list.setCurrentRow(current)
        self.list.blockSignals(False)
        self.count.setText(f"{len(chapters)} 章")

    def _menu(self, pos: QPoint) -> None:
        it = self.list.itemAt(pos)
        if it is None:
            return
        row = self.list.row(it)
        m = QMenu(self)
        a1 = QAction("重命名", self)
        a2 = QAction("删除本章", self)
        a1.triggered.connect(lambda: self._rename(row))
        a2.triggered.connect(lambda: self.removed.emit(row))
        m.addAction(a1)
        m.addAction(a2)
        m.exec(self.list.mapToGlobal(pos))

    def _rename(self, row: int) -> None:
        from PyQt6.QtWidgets import QInputDialog
        cur = self.list.item(row).text()[6:]
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

    def __init__(self):
        super().__init__()
        self.setObjectName("Desk")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._busy = False
        self._last_ctx_color = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.GAP_XL, T.GAP_LG, T.GAP_XL, T.GAP)
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

        stack.addSpacing(T.GAP_SM)

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
        center.addStretch(1)
        center.addWidget(col, 1)
        center.addStretch(1)
        outer.addLayout(center, 1)

        # ── 操作栏 ──
        outer.addSpacing(T.GAP)
        bar_col = QWidget()
        bar_col.setMaximumWidth(T.MEASURE)
        bar = QHBoxLayout(bar_col)
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

        bar_center = QHBoxLayout()
        bar_center.addStretch(1)
        bar_center.addWidget(bar_col, 1)
        bar_center.addStretch(1)
        outer.addLayout(bar_center)

    def _more_menu(self) -> None:
        m = QMenu(self)
        a_open = QAction("开篇生成", self)
        a_link = QAction("接龙续写", self)
        a_reformat = QAction("重排段落", self)
        a_open.triggered.connect(self.opening.emit)
        a_link.triggered.connect(self.link.emit)
        a_reformat.triggered.connect(self.reformat.emit)
        m.addAction(a_open)
        m.addAction(a_link)
        m.addSeparator()
        m.addAction(a_reformat)
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
        seg.setContentsMargins(14, 0, 14, 0)
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
        col.addWidget(self._stack, 1)

        # ── 页 0：语料 ──
        p0 = QWidget()
        v0 = QVBoxLayout(p0)
        v0.setContentsMargins(0, 0, 0, 0)
        v0.setSpacing(T.GAP_SM)
        v0.addLayout(self._head("语料", "add"))
        self.corpus = QListWidget()
        self.corpus.setObjectName("Corpus")
        self.corpus.setFrameShape(QFrame.Shape.NoFrame)
        self.corpus.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.corpus.customContextMenuRequested.connect(self._corpus_menu)
        v0.addWidget(self.corpus, 1)
        arow = QHBoxLayout()
        arow.setContentsMargins(14, 0, 14, 0)
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
        self.premise = self._notes(
            "写几句大方向即可，例如「都市修仙，主角是外卖员，捡到一枚会吐槽的系统」。"
            "人名、地名、门派可点下方「取书名」旁的指令框生成，也可留空让开篇生成全权发挥。"
        )
        v1.addWidget(self.premise, 1)
        nrow = QHBoxLayout()
        nrow.setContentsMargins(14, 0, 14, 0)
        nrow.setSpacing(T.GAP_SM)
        self.expand_btn = ghost_button("补全设定")
        self.expand_btn.setToolTip("依据你写的大方向，自动补全人物、境界、势力、地名等")
        self.expand_btn.clicked.connect(self.expand.emit)
        nrow.addWidget(self.expand_btn)
        self.names_btn = ghost_button("取书名")
        self.names_btn.setToolTip("依据故事方向拟 6 个候选书名，点选即可改作品名")
        self.names_btn.clicked.connect(self.names.emit)
        nrow.addWidget(self.names_btn)
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
        foot = QHBoxLayout()
        foot.setContentsMargins(14, T.GAP_SM, 6, 6)
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
        h.setContentsMargins(14, 0, 14, 0)
        h.addWidget(section_title(text))
        h.addStretch(1)
        if action == "add":
            b = ghost_button("添加文件")
            b.clicked.connect(self.add_files.emit)
            h.addWidget(b)
        return h

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
        self._pending_scroll = int(project.settings.reader_scroll or 0)
        self._scroll_tries = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._try_restore_scroll)

        self._load_list()
        self._apply_font()

        # 轮询鼠标位置驱动自动隐藏（不依赖事件在 QTextEdit 里的传递）
        self._poll = QTimer(self)
        self._poll.setInterval(120)
        self._poll.timeout.connect(self._track)
        self._poll.start()

        if self._pending_scroll > 0:
            self._scroll_timer.start()

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
        ch = self.project.chapters[row]
        self.view.setPlainText(ch.body or "（本章尚无内容）")
        self.view.moveCursor(QTextCursor.MoveOperation.Start)
        self.view.verticalScrollBar().setValue(0)
        self.crumb.setText(f"第 {row + 1} / {len(self.project.chapters)} 章 · {len(ch.body):,} 字")

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
            return
        self._scroll_tries += 1
        if self._scroll_tries > 40:      # 约 1.2 秒仍不够（如本章很短），放弃
            self._scroll_timer.stop()

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
        # 记住读到哪儿，下次打开接着看
        s = self.project.settings
        s.reader_chapter = max(0, self.list.currentRow())
        s.reader_scroll = self.view.verticalScrollBar().value()
        self.project.save()
        w = self.parent()
        if w is not None and getattr(w, "_reader", None) is self:
            w._reader = None
        super().closeEvent(e)

    def _apply_font(self) -> None:
        self.view.setStyleSheet(
            f"QTextEdit#Reader {{"
            f" background: transparent; border: none;"
            f" font-family: {T.SERIF};"
            f" font-size: {self._size}px;"
            f" line-height: 190%;"
            f" padding: {T.GAP_LG}px {T.GAP_XL}px;"
            f"}}"
        )


# ══════════════════════════════════════════════════════════
#  指令结果
# ══════════════════════════════════════════════════════════

class ResultDialog(QDialog):
    """指令模式的产出窗口。

    设定资料不该混进正文，所以单开一扇窗。读完后可一键并入作品简报，
    之后每次续写都会把它作为最高优先级的设定依据带上。
    """

    append_to_brief = pyqtSignal(str)

    def __init__(self, parent, instruction: str):
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

        self.use_btn = QPushButton("并入设定")
        self.use_btn.setObjectName("Primary")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
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
        self.use_btn.setEnabled(has)
        self.export_btn.setEnabled(has)

    def fail(self, msg: str) -> None:
        self.note.setText("失败：" + msg)

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

        note = hint("预算以「字」计。正文超出上下文预算后，旧情节会自动压缩进前情记忆。单章上限控制「一键生成」时自动开新章的字数。")
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

    def result_settings(self) -> Settings:
        return Settings(
            base_url=self.url.text().strip() or "https://api.deepseek.com/v1",
            api_key=self.key.text().strip(),
            model=self.model.text().strip() or "deepseek-chat",
            temperature=float(self.temp.value()),
            max_tokens=int(self.mtok.value()),
            context_budget=int(self.ctx.value()),
            corpus_budget=int(self.cor.value()),
            target_chars=int(self.tchars.value()),
            chapter_max=int(self.chmax.value()),
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

        # 配色先于任何控件创建，避免首帧闪色
        T.set_palette(self.project.settings.theme)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(T.build_qss())

        self.bridge = Bridge()
        self.bridge.delta.connect(self._on_delta)
        self.bridge.status.connect(self._on_status)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.settings_ready.connect(self._on_settings_ready)
        self.bridge.memory_ready.connect(self._on_memory_ready)
        self.bridge.names_ready.connect(self._on_names_ready)

        self._stop = threading.Event()
        self._handle: AI.StreamHandle | None = None
        self._busy = False
        self._dirty = False
        self._stream_target = "paper"
        self._result: ResultDialog | None = None
        self._reader: ReaderWindow | None = None

        # 一键生成状态
        self._gen_active = False
        self._auto_analyzing = False
        self._want_analyze = False
        self._gen_target = 0
        self._gen_done = 0
        self._chapter_max = 0
        self._pending_summaries: list[str] = []

        # ── 布局 ──
        root = QVBoxLayout(self)
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

        # ── 连线 ──
        self.rail.picked.connect(self._pick_chapter)
        self.rail.added.connect(self._add_chapter)
        self.rail.renamed.connect(self._rename_chapter)
        self.rail.removed.connect(self._remove_chapter)
        self.desk.write.connect(self._start_write)
        self.desk.link.connect(self._start_link)
        self.desk.reformat.connect(self._start_reformat)
        self.desk.generate.connect(self._start_generate)
        self.desk.opening.connect(self._start_opening)
        self.inspector.names.connect(self._start_names)
        self.desk.stop.connect(self._request_stop)
        self.desk.retitled.connect(self._retitle)
        self.desk.paper.textChanged.connect(self._on_text)
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
        self.autosave.timeout.connect(self._save)
        self.autosave.start()

        self._load_all(first=True)

    # ══════════════ 载入 ══════════════

    def _load_all(self, first: bool = False) -> None:
        p = self.project
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

    def _save(self) -> None:
        p = self.project
        p.chapter.title = self.desk.title.text().strip() or "未命名"
        p.chapter.body = self.desk.paper.body()
        p.memory = self.inspector.memory.toPlainText()
        p.premise = self.inspector.premise.toPlainText()
        try:
            p.save()
            self._dirty = False
        except OSError as e:
            self.desk.set_status(f"保存失败：{e}")

    def closeEvent(self, e) -> None:
        self._stop.set()
        self._save()
        super().closeEvent(e)

    # ══════════════ 章节 ══════════════

    def _stash(self) -> None:
        p = self.project
        p.chapter.title = self.desk.title.text().strip() or "未命名"
        p.chapter.body = self.desk.paper.body()

    def _pick_chapter(self, row: int) -> None:
        if row == self.project.current:
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
        """切换章节时的交叉淡入：内容换了，空间没有断裂。"""
        self.desk.paper.setGraphicsEffect(None)
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        eff = QGraphicsOpacityEffect(self.desk.paper)
        self.desk.paper.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(200)
        anim.setStartValue(0.25)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: self.desk.paper.setGraphicsEffect(None))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._fade = anim

    def _add_chapter(self) -> None:
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
        del p.chapters[row]
        p.current = max(0, min(row, len(p.chapters) - 1))
        self.rail.load(p.chapters, p.current)
        self.desk.set_chapter(p.chapter)
        self.desk.refresh_meta(p)
        self.titlebar.set_crumb(f"第 {p.current + 1} / {len(p.chapters)} 章")
        self._dirty = True

    def _rename_project(self, name: str) -> None:
        self.project.title = name
        self.titlebar.title_lbl.setText(name)
        self.titlebar.set_crumb(f"第 {self.project.current + 1} / {len(self.project.chapters)} 章")
        self._dirty = True

    def _retitle(self, name: str) -> None:
        self.project.chapter.title = name
        self.rail.load(self.project.chapters, self.project.current)
        self._dirty = True

    def _on_text(self) -> None:
        self._dirty = True
        self.desk.refresh_meta(self.project)

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

    # ══════════════ 设置 ══════════════

    # ══════════════ 作品 ══════════════

    def rename_project(self) -> None:
        from PyQt6.QtWidgets import QInputDialog
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
        from PyQt6.QtWidgets import QInputDialog
        if not self._confirm_discard():
            return
        name, ok = QInputDialog.getText(
            self, "新建作品", "作品名（可留空，之后用「AI 起名」自动命名）：", text=""
        )
        if not ok:
            return
        name = name.strip() or "未命名作品"
        # 先把旧作品落盘 —— 此刻界面里的内容仍属于它。
        # 若在切换后再 _save()，会把旧界面内容写进新项目。
        self._save()
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
        m.addAction(a_rename)
        m.addSeparator()
        m.addAction(a_new)
        m.addAction(a_open)
        m.addAction(a_delete)
        m.addSeparator()
        m.addAction(a_import)
        btn = self.titlebar.works_btn
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        from PyQt6.QtWidgets import QMessageBox
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
        if not self._confirm_discard():
            return
        items = store.list_projects()
        if not items:
            self.desk.set_status("还没有其它作品")
            return
        from PyQt6.QtWidgets import QInputDialog
        labels = [f"{t}   （{i}）" for i, t in items]
        choice, ok = QInputDialog.getItem(self, "打开作品", "选择：", labels, 0, False)
        if not ok:
            return
        pid = items[labels.index(choice)][0]
        try:
            self.project = store.open_project(pid)
        except (OSError, ValueError) as e:
            self.desk.set_status(f"打开失败：{e}")
            return
        self._after_switch()

    def delete_project(self) -> None:
        """删除一部作品（含备份）。当前作品被删则切到剩余作品或新建。"""
        from PyQt6.QtWidgets import QMessageBox

        items = store.list_projects()
        if not items:
            self.desk.set_status("还没有可删除的作品")
            return

        from PyQt6.QtWidgets import QInputDialog
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
        if not self._confirm_discard():
            return
        self._save()
        incoming.id = store._uid()
        self.project = incoming
        self._after_switch()
        self.desk.set_status(f"已导入《{incoming.title}》")

    def _after_switch(self) -> None:
        """换了作品之后，把界面整个重建一遍。"""
        T.set_palette(self.project.settings.theme)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(T.build_qss())
        self.titlebar.sync_theme_label()
        self._stream_target = "paper"
        if self._result is not None:
            self._result.close()
            self._result = None
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
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(T.build_qss())
        self.titlebar.sync_theme_label()
        self.desk.refresh_meta(self.project)
        self.desk.set_status(f"配色：{T.label_for(T.current_palette())}")
        self._dirty = True
        self._save()

    def _open_settings(self) -> None:
        d = SettingsDialog(self, self.project.settings)
        if d.exec() == QDialog.DialogCode.Accepted:
            self.project.settings = d.result_settings()
            self.desk.refresh_meta(self.project)
            if self.project.settings.target_chars:
                self.desk.set_target(self.project.settings.target_chars)
            self.desk.set_status("设置已保存")
            self._dirty = True

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
        body = self.project.chapter.body
        # 空稿（或只有零星几字）走开篇模式。
        # 用「续写」语气写开头，模型会把故事当成中段来写，于是永远没有真正的开场。
        if len(body.strip()) < 40:
            self._launch(body, renew=False, opening=True)
        else:
            self._launch(body, renew=True)

    def _start_opening(self) -> None:
        """开篇生成：按右栏「故事设定」起名、写第一章开头（仅限空白正文）。"""
        if self._busy or not self._guard_key():
            return
        self._stash()
        p = self.project
        self.inspector.premise.setPlainText(self.inspector.premise.toPlainText())
        p.premise = self.inspector.premise.toPlainText()
        # 允许全空：没有任何设定时，由 AI 从零发挥。
        if p.chapter.body.strip():
            from PyQt6.QtWidgets import QMessageBox
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
        p.premise = self.inspector.premise.toPlainText()
        self._busy = True
        self.desk.set_busy(True)
        self.desk.set_status("正在取名…")

        def work() -> None:
            try:
                out = AI.suggest_titles(p, on_step=lambda t: self.bridge.status.emit(t))
                self.bridge.names_ready.emit(out)
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

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
        self.desk.paper.textCursor().removeSelectedText()
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
        self._stop.clear()
        self._handle = AI.StreamHandle()
        self._busy = True
        self.desk.set_busy(True)
        self.desk.set_status("正在连接…")
        self._stream_target = "paper"

        if truncate_to is not None:
            body = self.desk.paper.body()[:truncate_to]
            self.desk.paper.load_body(body)
            p.chapter.body = body

        snapshot = AI.build_messages(p, tail=tail, opening=opening)

        def work() -> None:
            try:
                s = p.settings
                if renew and AI.needs_renewal(p):
                    self.bridge.status.emit("上下文接近上限 —— 正在压缩前情…")
                    AI.renew_memory(p, on_step=lambda t: self.bridge.status.emit(t))
                    snapshot[:] = AI.build_messages(p, tail=p.chapter.body)
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
                ):
                    if first:
                        self.bridge.status.emit("")
                        first = False
                    self.bridge.delta.emit(piece)
                self.bridge.done.emit()
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    # ── 流式回调 ──
    def _on_delta(self, piece: str) -> None:
        if self._stream_target in ("dialog", "reformat"):
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
        self._busy = False
        self.desk.set_busy(False)

        if self._stream_target in ("dialog", "reformat"):
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
        self._dirty = True
        self._save()
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
            try:
                for body in pending:
                    AI.summarize_chapter(p, body)
                    done += 1
            except Exception as e:  # noqa: BLE001
                # 失败的部分留在队列里，下次生成结束再试，不丢内容
                self._pending_summaries[:] = pending[done:]
                self.bridge.status.emit(f"记忆整理中断（已完成 {done} 章）：{e}")
                return
            self._pending_summaries[:] = []
            self.bridge.memory_ready.emit(p.memory)

        threading.Thread(target=work, daemon=True).start()

    def _on_memory_ready(self, text: str) -> None:
        self.inspector.show_tab(3)
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
        import re
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

        self._busy = False
        self.desk.set_busy(False)

        if not titles:
            self.desk.set_status("没能解析出书名，请重试")
            return

        from PyQt6.QtWidgets import QInputDialog
        cur = self.project.title
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
        """补全 / 分析的产出 —— 直接并入「设定」。"""
        auto = self._auto_analyzing
        self._auto_analyzing = False
        box = self.inspector.premise
        cur = box.toPlainText().strip()
        merged = (cur + "\n\n---\n\n" + text.strip()) if cur else text.strip()
        box.setPlainText(merged)
        self.project.premise = merged
        self._busy = False
        self.desk.set_busy(False)
        self.desk.set_status("设定已自动更新" if auto else "设定已更新")
        self._dirty = True
        self._save()

    def _on_failed(self, msg: str) -> None:
        self._flush_delta()
        self._busy = False
        self._gen_active = False
        self.desk.set_busy(False)
        if self._stream_target in ("dialog", "reformat"):
            if self._result is not None:
                self._result.fail(msg)
        else:
            self.desk.paper.end_stream()
            # 中断前已翻页的章节照样进记忆
            self._flush_summaries()
        self.desk.set_status(msg)

    # ══════════════ 重排段落 ══════════════

    def _backup(self) -> None:
        """落盘前先把当前项目另存一份，最多保留 20 份。"""
        import time
        folder = os.path.join(store.PROJECTS_DIR, "backup")
        try:
            os.makedirs(folder, exist_ok=True)
            self.project.save()
            with open(self.project.path(), encoding="utf-8") as f:
                data = f.read()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = os.path.join(folder, f"{self.project.id}-{stamp}.json")
            with open(dst, "w", encoding="utf-8") as f:
                f.write(data)
            # 只保留最近 20 份
            files = sorted(
                (f for f in os.listdir(folder) if f.startswith(self.project.id)),
                reverse=True,
            )
            for old in files[20:]:
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        except OSError as e:
            self.desk.set_status(f"备份失败：{e}")

    def _start_reformat(self) -> None:
        if self._busy or not self._guard_key():
            return
        self._stash()
        if len(self.project.chapter.body.strip()) < 50:
            self.desk.set_status("正文太短，无需重排")
            return

        self._busy = True
        self.desk.set_busy(True)
        self.desk.set_status("正在重排段落…")
        self._stream_target = "reformat"
        p = self.project

        self._result = ResultDialog(self, "重排段落 —— 只调整换行，不改一个字")
        self._result.use_btn.setText("替换正文")
        self._result.append_to_brief.connect(self._apply_reformat)
        self._result.show()

        def work() -> None:
            try:
                out = AI.reformat_chapter(
                    p, on_step=lambda t: self.bridge.status.emit(t)
                )
                self.bridge.delta.emit(out)
                self.bridge.done.emit()
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _apply_reformat(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self._result:
            return
        # 二次确认，因为会整体替换
        from PyQt6.QtWidgets import QMessageBox
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
        p.premise = self.inspector.premise.toPlainText()
        if not p.premise.strip():
            self.desk.set_status("先写几句故事方向，我才有依据补全")
            return
        self._busy = True
        self.desk.set_busy(True)
        self.desk.set_status("正在补全设定…")

        def work() -> None:
            try:
                out = AI.expand_settings(p, on_step=lambda t: self.bridge.status.emit(t))
                self.bridge.settings_ready.emit(out)
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

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
        self._busy = True
        self.desk.set_busy(True)
        self.desk.set_status("正在通读全文，更新设定…" if auto else "正在通读材料…")
        p = self.project

        def work() -> None:
            try:
                out = AI.analyze_corpus(p, on_step=lambda t: self.bridge.status.emit(t))
                self.bridge.settings_ready.emit(out)
            except AI.AIError as e:
                self.bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.bridge.failed.emit(f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

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
            self._stop.set()
            self.desk.set_status("正在停止…")
            return
        if ctrl and key == Qt.Key.Key_N:
            self._add_chapter()
            return
        super().keyPressEvent(e)
