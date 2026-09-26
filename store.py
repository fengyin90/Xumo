"""项目存储 —— 单文件 JSON，无数据库。

一个"项目"就是一部小说：章节、语料、跨会话记忆、引擎设置。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

# 数据目录：
#   源码运行 → 项目目录下的 projects/
#   打包成 exe → exe 所在目录下的 projects/（便携式：exe 挪到哪，作品就跟到哪）
# 若用 __file__，打包后它指向 PyInstaller 的临时解压目录，作品一关就丢。
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_DIR = os.path.join(APP_DIR, "projects")
LAST_FILE = os.path.join(PROJECTS_DIR, ".last")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class Chapter:
    id: str
    title: str
    body: str = ""

    def words(self) -> int:
        """中文按字计，英文按词计——粗算即可，只为操作员掌握体量。"""
        return len(self.body.replace(" ", "").replace("\n", ""))


@dataclass
class Corpus:
    id: str
    name: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Settings:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.92
    max_tokens: int = 1600
    # 单次注入的前文上限（字）。超出部分会被压缩进记忆摘要。
    context_budget: int = 5000
    # 语料注入上限
    corpus_budget: int = 6000
    # 一键生成目标字数；0 表示不限，由模型自行收尾
    target_chars: int = 2000
    # 单章字数上限；超出后自动开启新章继续。0 表示不限
    chapter_max: int = 5000
    theme: str = "blue"
    # 编辑区正文字号（px）
    editor_size: int = 16
    # 阅读窗口正文字号
    reader_size: int = 17
    # 阅读窗口上次停留的章节序号与滚动位置（关窗时写入）
    reader_chapter: int = 0
    reader_scroll: int = 0

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass
class Project:
    id: str = field(default_factory=_uid)
    title: str = "未命名作品"
    chapters: list[Chapter] = field(default_factory=list)
    corpus: list[Corpus] = field(default_factory=list)
    memory: str = ""
    analysis: str = ""
    premise: str = ""
    settings: Settings = field(default_factory=Settings)
    current: int = 0

    # ── 便捷访问 ──
    @property
    def chapter(self) -> Chapter:
        if not self.chapters:
            self.chapters.append(Chapter(_uid(), "第一章"))
            self.current = 0
        self.current = max(0, min(self.current, len(self.chapters) - 1))
        return self.chapters[self.current]

    def path(self) -> str:
        return os.path.join(PROJECTS_DIR, f"{self.id}.json")

    # ── 序列化 ──
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        p = cls(
            id=d.get("id") or _uid(),
            title=d.get("title") or "未命名作品",
            memory=d.get("memory") or "",
            analysis=d.get("analysis") or "",
            premise=d.get("premise") or "",
            current=int(d.get("current") or 0),
        )
        # 一次性迁移：旧的「作品简报」(analysis) 并入「设定」(premise)。
        # 迁移后 analysis 清空，不再单独存在。
        if p.analysis.strip():
            merged = (
                (p.premise.strip() + "\n\n" + p.analysis.strip()).strip()
                if p.premise.strip()
                else p.analysis.strip()
            )
            p.premise = merged
            p.analysis = ""
        p.chapters = [Chapter(**c) for c in d.get("chapters", [])]
        p.corpus = [Corpus(**c) for c in d.get("corpus", [])]
        s = d.get("settings") or {}
        known = {f for f in Settings.__dataclass_fields__}
        p.settings = Settings(**{k: v for k, v in s.items() if k in known})
        if not p.chapters:
            p.chapters = [Chapter(_uid(), "第一章")]
        p.current = max(0, min(p.current, len(p.chapters) - 1))
        return p

    # 每次保存前，把旧文件滚动留档，最多保留这么多个版本
    BACKUP_KEEP = 40

    def _rotate_backup(self) -> None:
        """把当前磁盘上的版本复制进 backup/，再写新版本。

        每次保存都留一个回滚点：误删、误改、程序写坏都能退回去。
        用时间戳命名，按名倒序保留最近 BACKUP_KEEP 份。
        """
        src = self.path()
        if not os.path.exists(src):
            return
        folder = os.path.join(PROJECTS_DIR, "backup")
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = os.path.join(folder, f"{self.id}-{stamp}.json")
            # 同一秒内多次保存：追加序号，不覆盖
            n = 1
            while os.path.exists(dst):
                dst = os.path.join(folder, f"{self.id}-{stamp}-{n}.json")
                n += 1
            with open(src, encoding="utf-8") as f:
                data = f.read()
            with open(dst, "w", encoding="utf-8") as f:
                f.write(data)
            olds = sorted(
                (f for f in os.listdir(folder) if f.startswith(self.id)),
                reverse=True,
            )
            for old in olds[self.BACKUP_KEEP:]:
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        except OSError:
            pass  # 备份失败不该阻断保存

    def save(self) -> None:
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        self._rotate_backup()
        tmp = self.path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path())
        with open(LAST_FILE, "w", encoding="utf-8") as f:
            f.write(self.id)


# ── 载入 / 新建 ─────────────────────────────────────────

def new_project(title: str = "未命名作品") -> Project:
    p = Project(title=title)
    p.chapters = [Chapter(_uid(), "第一章")]
    return p


def load_last() -> Project:
    """恢复上次编辑的作品；没有就开一部新的。"""
    try:
        with open(LAST_FILE, encoding="utf-8") as f:
            pid = f.read().strip()
        fp = os.path.join(PROJECTS_DIR, f"{pid}.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                return Project.from_dict(json.load(f))
    except (OSError, ValueError):
        pass
    return new_project()


def list_projects() -> list[tuple[str, str]]:
    """返回 [(id, 标题)]，按修改时间倒序。"""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    items: list[tuple[float, str, str]] = []
    for name in os.listdir(PROJECTS_DIR):
        if not name.endswith(".json"):
            continue
        fp = os.path.join(PROJECTS_DIR, name)
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            items.append((os.path.getmtime(fp), d.get("id", ""), d.get("title", "未命名作品")))
        except (OSError, ValueError):
            continue
    items.sort(reverse=True)
    return [(i, t) for _, i, t in items]


def open_project(pid: str) -> Project:
    fp = os.path.join(PROJECTS_DIR, f"{pid}.json")
    with open(fp, encoding="utf-8") as f:
        return Project.from_dict(json.load(f))


def delete_project(pid: str) -> None:
    """删除作品文件及其备份。.last 指向它时一并清掉。"""
    fp = os.path.join(PROJECTS_DIR, f"{pid}.json")
    if os.path.exists(fp):
        os.remove(fp)
    # 备份
    folder = os.path.join(PROJECTS_DIR, "backup")
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.startswith(pid):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
    # .last 指向被删作品则清除
    try:
        with open(LAST_FILE, encoding="utf-8") as f:
            if f.read().strip() == pid:
                os.remove(LAST_FILE)
    except OSError:
        pass
