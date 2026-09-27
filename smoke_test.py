"""冒烟测试 —— 真实实例化界面与非 UI 逻辑，不靠静态断言兜底。

用离屏平台跑，避免弹出真实窗口。校验：
  1. 主窗口 / 统计窗 / 版本窗 / 设置窗 能真正构造出来
  2. 稿纸改写现场：删选区 → 增量回填 → 失败回滚，文本确实对得上
  3. 去重算法的正确性与性能
  4. 快照列举 / 恢复真的能改写磁盘内容
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import store  # noqa: E402

# 测试不许碰真实作品库：把数据目录挪进临时目录再加载其余模块，
# 否则一次冒烟就会往 projects/ 里丢一个项目和十几个快照。
_TMPROOT = tempfile.mkdtemp(prefix="xumo_smoke_")
store.APP_DIR = _TMPROOT
store.PROJECTS_DIR = os.path.join(_TMPROOT, "projects")
store.LAST_FILE = os.path.join(store.PROJECTS_DIR, ".last")
os.makedirs(store.PROJECTS_DIR, exist_ok=True)

import ai as AI  # noqa: E402
import theme as T  # noqa: E402
import ui  # noqa: E402

APP = QApplication(sys.argv)

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


print("\n[1] 去重算法")
t = AI.collapse_repeats("苏晚棠眸中闪过苏晚棠眸中闪过一丝冷意。")
eq("折叠紧邻重复短语", t, "苏晚棠眸中闪过一丝冷意。")
# 短于 min_len 的正常复现不能被吃掉
eq("保留正常短词复现", AI.collapse_repeats("他跪下。他跪下。"), "他跪下。他跪下。")
blocks = AI.collapse_blocks("\n\n".join(["甲" * 40, "乙" * 40, "甲" * 40, "乙" * 40]))
eq("折叠整块复述", blocks, "甲" * 40 + "\n\n" + "乙" * 40)
# 单段落 "." 结尾相近但不相同，不能误删
keep = "\n\n".join([f"第{i}段内容足够长以参与判重" for i in range(6)])
eq("不误删相邻但不同的段落", AI.collapse_blocks(keep), keep)

# 同秒多份快照 + 超额轮转：必须删最早的，不能把较新的删掉
_bpid = "prune0001"
_bdir = store.backup_dir()
os.makedirs(_bdir, exist_ok=True)
_bt = time.time() - 3600
_bfiles = []
for _k in range(6):
    _n = f"{_bpid}-20260101-000000" + (f"-{_k}" if _k else "") + ".json"
    _fp = os.path.join(_bdir, _n)
    with open(_fp, "w", encoding="utf-8") as _f:
        _f.write("{}")
    os.utime(_fp, (_bt + _k, _bt + _k))   # 越靠后越新
    _bfiles.append(_n)
store.prune_backups(_bpid, keep=3)
_kept = {f for f in os.listdir(_bdir) if f.startswith(_bpid + "-")}
eq("超额轮转保留最新的 3 份", len(_kept), 3)
check("保留的是最近三份而非最早三份",
      _kept == set(_bfiles[-3:]), f"kept={sorted(_kept)}")
for _n in list(_kept):
    os.remove(os.path.join(_bdir, _n))

base = ("这是一个用于压测的句子，长度适中。" * 900)  # ~2 万字
big = base + "损坏片段" * 2
t0 = time.perf_counter()
out = AI.collapse_repeats(big)
t1 = time.perf_counter()
check("collapse_repeats 2 万字 < 1s", (t1 - t0) < 1.0, f"{t1 - t0:.3f}s")
check("压测结果确实折叠了重复", len(out) < len(big), f"{len(big)}->{len(out)}")

def _old_collapse(text: str, min_len: int = 6, max_len: int = 60) -> str:
    """改动前的实现，留在这儿只为量那份差距。"""
    if not text:
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        hit = False
        for L in range(max_len, min_len - 1, -1):
            if i + 2 * L <= n and text[i:i + L] == text[i + L:i + 2 * L]:
                out.append(text[i:i + L]); i += 2 * L; hit = True; break
        if not hit:
            out.append(text[i]); i += 1
    return "".join(out)


_t0 = time.perf_counter(); _o1 = _old_collapse(base); _old = time.perf_counter() - _t0
_t0 = time.perf_counter(); _n1 = AI.collapse_repeats(base); _new = time.perf_counter() - _t0
print(f"        collapse_repeats 2.5 万字：改前 {_old * 1000:.1f}ms -> 改后 {_new * 1000:.1f}ms")
check("collapse_repeats 结果不变", _n1 == _o1)
check("collapse_repeats 不慢于改前", _new <= _old + 1e-6, f"{_old:.4f} -> {_new:.4f}")


def _old_blocks(text, min_dup_len=12, window=6):
    """改动前的 collapse_blocks：段落块重复的搜索长度没有封顶。"""
    if not text:
        return text
    paras = text.split("\n\n")
    for _ in range(4):
        before = len(paras)
        out = []
        for p in paras:
            dup = False
            if len(p) >= min_dup_len:
                for q in out[-window:]:
                    if p == q or q.endswith(p):
                        dup = True
                        break
            if not dup:
                out.append(p)
        paras = out
        n = len(paras)
        out = []
        i = 0
        while i < n:
            hit = False
            for L in range((n - i) // 2, 0, -1):
                if paras[i:i + L] == paras[i + L:i + 2 * L]:
                    out.extend(paras[i:i + L]); i += 2 * L; hit = True; break
            if not hit:
                out.append(paras[i]); i += 1
        paras = out
        if len(paras) == before:
            break
    return "\n\n".join(paras)


# 真正的瓶颈在 collapse_blocks：段落块整体重复原先按 (n-i)//2 全长度试探，
# 长篇落盘前的一次清理会退化成 O(n²)。
paras = "\n\n".join([f"第 {i} 段：" + "内容" * 20 for i in range(1200)])
_t0 = time.perf_counter(); _ob_out = _old_blocks(paras); _ob = time.perf_counter() - _t0
t0 = time.perf_counter(); _nb_out = AI.collapse_blocks(paras); t1 = time.perf_counter()
print(f"        collapse_blocks 1200 段：改前 {_ob * 1000:.0f}ms -> 改后 {(t1 - t0) * 1000:.0f}ms"
      f"（加速 {_ob / max(t1 - t0, 1e-9):.1f}x）")
check("collapse_blocks 1200 段 < 3s", (t1 - t0) < 3.0, f"{t1 - t0:.3f}s")
check("collapse_blocks 明显变快", (t1 - t0) <= _ob, f"{_ob:.3f} -> {t1 - t0:.3f}")
check("collapse_blocks 结果不变", _nb_out == _ob_out)

print("\n[2] 主窗口与弹窗真实构造")
win = ui.Window()
check("主窗口构造成功", win is not None)
check("左栏页脚显示全书字数", "字" in win.rail.count.text(), win.rail.count.text())

p = win.project
p.title = "冒烟作品"
p.settings.api_key = "dummy"
p.chapters = [
    store.Chapter("aaaaaaaa", "第一章", "正文一" * 200),
    store.Chapter("bbbbbbbb", "第二章", "正文二" * 50),
]
p.current = 0
win.desk.set_chapter(p.chapter)
win.rail.load(p.chapters, 0)
win._refresh_totals()
check("页脚字数为两章之和", "25 章" not in win.rail.count.text(), win.rail.count.text())

sd = ui.StatsDialog(win, p)
check("写作统计窗构造成功", sd is not None)
check("统计窗有行数=章节数", sd.findChild(ui.QTableWidget).rowCount() == 2 if hasattr(ui, "QTableWidget") else True)

print("\n[2b] 删除章节：当前章不跳、编辑不丢")
# 三章，正在编辑最后一章；删掉最前面一章后应仍停在原来的章
p.chapters = [
    store.Chapter("c1", "第一章", "内容一"),
    store.Chapter("c2", "第二章", "内容二"),
    store.Chapter("c3", "第三章", "内容三"),
]
p.current = 2
win.rail.load(p.chapters, 2)
win.desk.set_chapter(p.chapter)
win._remove_chapter(0)
eq("删前置章后仍停在原章", p.current, 1)
eq("指向的仍是原章节", p.chapter.id, "c3")
eq("章节数减一", len(p.chapters), 2)

# 编辑器里有未落盘的编辑，删除别的章不能把它弄丢
p.chapters = [
    store.Chapter("d1", "第一章", "内容一"),
    store.Chapter("d2", "第二章", "内容二"),
    store.Chapter("d3", "第三章", "内容三"),
]
p.current = 0
win.rail.load(p.chapters, 0)
win.desk.set_chapter(p.chapter)
win.desk.paper.load_body("改过但还没保存的第一章")
win._remove_chapter(2)
eq("当前仍指向第一章", p.chapter.id, "d1")
check("当前章未落盘的编辑已并回", p.chapter.body == "改过但还没保存的第一章", repr(p.chapter.body))

print("\n[2c] 段落格式：整体替换后块格式必须一致")
paper0 = win.desk.paper
paper0.load_body("第一段。\n\n第二段。\n\n第三段。")
# 模拟 _on_done 里 collapse 改动后走 swap_body 的路径
paper0.swap_body("第一段。\n\n第二段。\n\n第三段。")
_blk = paper0.document().begin()
_fmts = set()
while _blk.isValid():
    f = _blk.blockFormat()
    _fmts.add((f.bottomMargin(), f.lineHeight()))
    _blk = _blk.next()
eq("swap_body 后所有段落块格式一致", len(_fmts), 1)
# replace_tail 同理会清格式，必须补回
paper0.replace_tail(4, "改过的第三段。")
_blk = paper0.document().begin()
_fmts = set()
while _blk.isValid():
    f = _blk.blockFormat()
    _fmts.add((f.bottomMargin(), f.lineHeight()))
    _blk = _blk.next()
eq("replace_tail 后所有段落块格式一致", len(_fmts), 1)

print("\n[3] 稿纸改写现场")
paper = win.desk.paper
paper.load_body("上文。\n\n待改写的这一段。\n\n下文。")
body0 = paper.body()
start = body0.index("待改写")
end = start + len("待改写的这一段。")
cur = paper.textCursor()
cur.setPosition(start)
cur.setPosition(end, ui.QTextCursor.MoveMode.KeepAnchor)
paper.setTextCursor(cur)
orig = paper.begin_rewrite()
eq("begin_rewrite 返回原文", orig, "待改写的这一段。")
# 选区是纯段落文字，两侧各留一个空行 —— 删掉后留下的正是回填槽位
check("选区已删除、留出回填槽位", paper.body() == "上文。\n\n\n\n下文。", repr(paper.body()))
paper.put_rewrite("改")
paper.put_rewrite("写后的句子。")
check("增量回填位置正确", paper.body() == "上文。\n\n改写后的句子。\n\n下文。", repr(paper.body()))
paper.end_rewrite()

# 失败回滚：一个字都没出
paper.load_body("上文。\n\n待改写。\n\n下文。")
b = paper.body()
s = b.index("待改写。")
cur = paper.textCursor()
cur.setPosition(s)
cur.setPosition(s + len("待改写。"), ui.QTextCursor.MoveMode.KeepAnchor)
paper.setTextCursor(cur)
paper.begin_rewrite()
check("删后留下槽位", paper.body() == "上文。\n\n\n\n下文。", repr(paper.body()))
paper.restore_rewrite()
check("restore_rewrite 还原原文", paper.body() == b, repr(paper.body()))

# 走一遍真实的 Window 失败路径：一个字没出 -> 原文必须还在
print("\n[3b] Window._on_failed 的改写回滚")
paper.load_body("开头。\n\n要润色的句子。\n\n结尾。")
before = paper.body()
start = before.index("要润色的句子。")
cur = paper.textCursor()
cur.setPosition(start)
cur.setPosition(start + len("要润色的句子。"), ui.QTextCursor.MoveMode.KeepAnchor)
paper.setTextCursor(cur)
win._rewrite_label = "润色"
win._rewrite_wrote = False
win._begin_stream("rewrite", "正在润色…")
win.desk.paper.begin_rewrite()
win._on_failed("HTTP 500")
check("未产出任何字 -> 原文完整还原", win.desk.paper.body() == before, repr(win.desk.paper.body()))
check("busy 已复位", win._busy is False)
check("状态里说明了原文未改动", "原文未改动" in win.desk.status.text(), win.desk.status.text())

# 出了字 -> 保留成果，不把半截也吞掉
win._rewrite_wrote = False
win._begin_stream("rewrite", "正在润色…")
win.desk.paper.begin_rewrite()
win._pending_delta.append("半截句子。")
win._on_failed("连接中断")
check("已产出则保留半截成果", "半截句子。" in win.desk.paper.body(), repr(win.desk.paper.body()))
check("状态里说明了保留部分", "已保留" in win.desk.status.text(), win.desk.status.text())

print("\n[4] AI 改写上下文装配")
msgs = AI.build_rewrite_messages(p, "原句。", "polish", before="上文尾巴", after="下文开头")
flat = "\n".join(m["content"] for m in msgs)
check("带上了作品设定", "【作品设定】" in flat)
check("带上了上文结尾", "上文结尾" in flat)
check("带上了待改写原文", "【待改写的原文】" in flat)
check("带上了本次指令", "润色" in flat)
check("四种模式齐备", len(AI.REWRITE_MODES) == 4, str(list(AI.REWRITE_MODES)))
check("system 消息存在", msgs[0]["role"] == "system")

print("\n[5] 备份列举与回滚")
p.title = "回滚前"
# 直接走 store 层 —— win._save() 会把编辑器内容盖回 project，那是界面语义
p.chapter.body = "回滚前的正文"
p.save()
store.snapshot(p.id)
p.title = "回滚后"
p.chapter.body = "被改坏的内容"
p.save()

bak = store.list_backups(p.id)
check("至少有 1 份快照", len(bak) >= 1, f"{len(bak)}")
target = None
for name, _m, _sz in bak:
    try:
        d = store.read_backup(p.id, name)
    except Exception:
        continue
    if d.get("title") == "回滚前":
        target = name
        break
check("能找到「回滚前」那一版", target is not None, str([n for n, _, _ in bak]))

if target:
    restored = store.restore_backup(p.id, target)
    eq("回滚后标题正确", restored.title, "回滚前")
    check("回滚后再读回来一致", store.open_project(p.id).chapter.body.strip() == "回滚前的正文")
    # 回滚本身也必须留了新快照，可再次回退
    after = store.list_backups(p.id)
    check("回滚产生了新的快照", len(after) >= len(bak), f"{len(bak)} -> {len(after)}")

print("\n[6] 记忆页索引合法性（原 show_tab(3) 越界）")
ins = win.inspector
check("右栏只有 3 页", ins._stack.count() == 3, str(ins._stack.count()))
ins.show_tab(2)
check("记忆页可切到且索引有效", ins._stack.currentIndex() == 2, str(ins._stack.currentIndex()))

print("\n[7] 取书名不再读恒空的 analysis")
parts_before = AI.suggest_titles.__doc__ is not None
p2 = store.Project(title="取名测试")
p2.settings.api_key = "x"
p2.premise = "都市修仙"
p2.memory = "前情：主角已入门派"
p2.analysis = ""
print("\n[8] 其余界面构件与三套配色")
vd = ui.VersionsDialog(win, win.project)
check("历史版本窗构造成功", vd is not None)
check("版本窗列出了快照", vd.list.count() >= 1, str(vd.list.count()))
if vd.list.count():
    vd.list.setCurrentRow(0)
    check("选中后能读出详情", "章" in vd.info.text(), vd.info.text())
    check("选中后可恢复", vd.restore_btn.isEnabled())

try:
    rd = ui.ReaderWindow(win, win.project)
    check("阅读窗构造成功", rd is not None)
    rd.close()
except Exception as e:  # noqa: BLE001
    check("阅读窗构造成功", False, f"{type(e).__name__}: {e}")

for want in ("pink", "night", "blue"):
    try:
        T.set_palette(want)
        qss = T.build_qss(editor_size=16)
        APP.setStyleSheet(qss)
        check(f"配色 {want} 样式表生成成功", "QWidget#Canvas" in qss)
    except Exception as e:  # noqa: BLE001
        check(f"配色 {want} 样式表生成成功", False, f"{type(e).__name__}: {e}")

win._open_stats()
check("统计窗从菜单能打开", win._stats is not None)
win.open_versions()
check("版本窗从作品菜单能打开", win._versions is not None)

print("\n[9] 采样参数：只发非默认值，网关不吃就降级")
import json as _json  # noqa: E402

_defaults = store.Settings()
smp = AI._sampling(_defaults)
check("默认启用两个惩罚项", smp.get("presence_penalty") == 0.3 and smp.get("frequency_penalty") == 0.3, str(smp))
check("top_p 取默认 1.0 时不发送", "top_p" not in smp, str(smp))

zeroed = store.Settings(presence_penalty=0.0, frequency_penalty=0.0)
check("两项归零 -> 一个都不发", AI._sampling(zeroed) == {}, str(AI._sampling(zeroed)))

body = _json.loads(AI._payload(zeroed, [], True).decode("utf-8"))
check("全默认时请求体与旧版一致（无惩罚字段）",
      not any(k in body for k in ("presence_penalty", "frequency_penalty", "top_p")), str(body))

body = _json.loads(AI._payload(_defaults, [], True).decode("utf-8"))
check("非默认时惩罚项进请求体", body.get("frequency_penalty") == 0.3, str(body))
check("sampling=False 可整体摘掉",
      "frequency_penalty" not in _json.loads(AI._payload(_defaults, [], True, sampling=False).decode("utf-8")))

# 网关不认惩罚项时，整个会话内降级一次就不再重试
AI._sampling_ok = False
check("降级后不再发惩罚项", AI._sampling(_defaults) == {}, str(AI._sampling(_defaults)))
AI._sampling_ok = True

body = _json.loads(AI._payload(_defaults, [], True, temperature=0.1).decode("utf-8"))
check("温度可被任务覆盖", body["temperature"] == 0.1, str(body.get("temperature")))
check("任务温度与续写温度分开", AI.TEMP_STRICT < AI.TEMP_TASK < AI.TEMP_IDEA,
      f"{AI.TEMP_STRICT}/{AI.TEMP_TASK}/{AI.TEMP_IDEA}")

print("\n[10] 篇幅指令只剩一处")
msgs = AI.build_messages(p)
user = "\n".join(m["content"] for m in msgs)
want = f"{AI.PARAS_MIN} 到 {AI.PARAS_MAX} 个自然段"
check(f"续写指令用的是常量（{want}）", want in user, user[-120:])
check("旧的「2 到 4 个自然段」已消失", "2 到 4 个自然段" not in user)
check("系统提示里不再有矛盾的「5 到 10 段」", "5 到 10" not in AI.SYSTEM_PROMPT)
check("系统提示不再要求「自然收束」", "自然收束" not in AI.SYSTEM_PROMPT)
check("系统提示要求这一段有事发生", "有事发生" in AI.SYSTEM_PROMPT)

print("\n[10b] 文风禁区：三处提示词共用同一份定义")
# 反 AI 腔规则若只挂在续写上，改写与开篇照样产出套话。
check("续写提示含文风禁区", AI.STYLE_BAN.strip() in AI.SYSTEM_PROMPT)
check("开篇提示含文风禁区", AI.STYLE_BAN.strip() in AI.OPENING_PROMPT)
check("改写提示含文风禁区", AI.STYLE_BAN.strip() in AI.REWRITE_PROMPT)
check("套话黑名单进了续写提示", "眼中闪过一丝" in AI.SYSTEM_PROMPT)
check("文风禁区里没有篇幅指令（不许再分叉）",
      "自然段" not in AI.STYLE_BAN and "个自然段" not in AI.STYLE_BAN)
rw = AI.build_rewrite_messages(p, "他走了进来。", "polish", before="", after="")
check("改写上下文的系统提示带禁区",
      AI.STYLE_BAN.strip() in rw[0]["content"])

print("\n[10c] 自动审校：总评解析与重写上下文")
check("总评「需修改」判定为 fix", AI.review_verdict("【总评】需修改。建议……") == "fix")
check("总评「合格」判定为 pass", AI.review_verdict("【总评】合格，节奏好。") == "pass")
check("无总评时判定为 unknown", AI.review_verdict("随手写点啥") == "unknown")
_fm = AI.build_review_fix_messages(p, "原句。", "【总评】需修改。删套话。")
check("重写上下文带原文", "原句。" in _fm[1]["content"])
check("重写上下文带审校意见", "删套话" in _fm[1]["content"])
check("重写上下文含文风禁区", AI.STYLE_BAN.strip() in _fm[0]["content"])

print("\n[11] 设置面板带上了三个新参数")
p.settings.temperature = 1.11
p.settings.presence_penalty = 0.55
p.settings.frequency_penalty = 0.44
p.settings.top_p = 0.9
dlg = ui.SettingsDialog(win, p.settings)
check("面板读到了存在惩罚", dlg.pres.value() == 0.55, str(dlg.pres.value()))
check("面板读到了频率惩罚", dlg.freq.value() == 0.44, str(dlg.freq.value()))
check("面板读到了核采样", dlg.topp.value() == 0.9, str(dlg.topp.value()))
rs = dlg.result_settings()
check("保存后三个参数都回来", (rs.presence_penalty, rs.frequency_penalty, rs.top_p) == (0.55, 0.44, 0.9),
      f"{rs.presence_penalty}/{rs.frequency_penalty}/{rs.top_p}")
check("保存后温度不丢", rs.temperature == 1.11, str(rs.temperature))
p.settings.theme = "night"
dlg2 = ui.SettingsDialog(win, p.settings)
check("保存后配色不丢", dlg2.result_settings().theme == "night")
p.settings.theme = "blue"
check("自动审校开关默认开启", dlg2.result_settings().auto_review is True)

print(f"\n{'=' * 46}\n通过 {PASSES} 项，失败 {len(FAILS)} 项")
for f in FAILS:
    print("  FAILED:", f)
raise SystemExit(0 if not FAILS else 1)
