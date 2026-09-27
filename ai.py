"""AI 引擎 —— OpenAI 兼容协议，零第三方依赖。

负责三件事：
  1. 流式续写（SSE 逐 token 产出）
  2. 一次性调用（生成记忆摘要）
  3. 上下文装配：跨会话记忆 + 语料 + 前文窗口
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from store import Corpus, Project, Settings

TIMEOUT = 180

# ── 限流重试 ────────────────────────────────────────────
# 命中这些状态码时自动退避重试（429 限流、503 临时不可用）
RETRY_STATUS = {429, 503}
MAX_RETRIES = 5
BASE_DELAY = 2.0
MAX_DELAY = 60.0

# ── 去 AI 腔：文风禁区 ──────────────────────────────────
# 「AI 以为自己有文采」是读者认出机器文本的第一特征 —— 喻体堆砌、形容词
# 密集、排比三连、把情绪写成名词。读者不是被情节劝退的，是被这种腔调劝退的：
# 一眼看出是生成的，就不看了。
#
# 只约束表达层，不碰情节。续写、开篇、改写三处共用同一份定义 ——
# 分成三份写迟早会漂移，篇幅指令已经栽过一次（见 PARAS_MIN 处的注释）。
STYLE_BAN = """

文风禁区（以下每条都算硬性不合格 —— 违反任何一条，读者一眼就认出这是机器写的）：
1. 不许堆比喻。一段之内比喻、拟人、通感加起来最多一处，更不许
   「仿佛……像是……如同……」这样把同义的比喻串起来用。
2. 不许堆形容词和副词。能用动作交代的，就别用形容词去形容；
   一段之内形容词不超过三个。删掉「深深地」「死死地」「缓缓地」「默默地」
   这类不增加任何信息的修饰，它们只是占字数。
3. 不许排比三连。不要四个四字词连着走，不要「是……是……也是……」的句式，
   不要三句以上结构相同的句子排在一起。
4. 不许把情绪写成名词。不写「心中涌起一股难以言喻的感觉」，
   写这个人此刻做了什么、说了什么。
5. 不许替读者抒情、议论、升华。不要在段末总结这件事意味着什么，
   也不要用写景把情绪收干净。
6. 句子必须长短交错。每三个句子里至少有一句不超过十二个字；
   全是长句显得端着，全是短句显得敷衍。
7. 情节靠信息与动作推进，不靠辞藻。写不动的地方就砍掉，
   不许拿比喻或者形容词去填。

下列套话已烂在网文与生成文本里，出现任何一处即算不合格：
眼中闪过一丝、嘴角勾起一抹、空气仿佛凝固、心中一凛、不由得、不由自主、
眸光微闪、意味深长地看了一眼、说不出的滋味、五味杂陈、仿佛过了很久、
这一瞬间、仿佛整个世界都安静了、轻轻地叹了口气、缓缓开口"""


# ── 续写指令：产品语言，操作员不用看到设计评论 ──────────
SYSTEM_PROMPT = f"""你是一位职业小说家，正在为一部已成稿的长篇续写正文。

硬性要求：
1. 直接输出正文，不要任何解释、标题、序号或 Markdown 标记。
2. 严格延续既有的人称、时态、语气、句法密度与段落节奏。
3. 人物的名字、称谓、口癖、关系与设定必须与前文完全一致，不得凭空增改。
   境界、修为、身份、地名、专有名词也必须与【作品设定】和前文一致，不得前后矛盾。
4. 这一段必须「有事发生」：有人想要某样东西、撞上阻碍、做出反应，局面随之改变。
   用概述、回忆、风景或心理独白填满篇幅而不改变局面 —— 那是凑字数，不是推进。
5. 段落必须短。每个自然段只写 1 到 3 句，通常不超过 80 字。
   对话必须独立成段，不得与叙述混在同一段里。
   叙述也要勤换段：动作、心理、环境各占一段，不要堆成一坨。
6. 收尾不许总结、不许升华、不许用写景或抒情把情绪收干净。
   停在动作、悬念、反转或一句有分量的台词上 —— 让读者非看下一句不可。
7. 使用中文全角标点，对话使用中文引号。
8. 时间、地点与在场人物必须与紧邻上文完全一致：已经离场的人不得凭空出现，
   一直在场的人不得写成「刚刚赶到」；不得在同一段之内让昼夜来回跳跃。
9. 写完这一段，读者必须比读之前多知道一件事，或者多了一个想知道答案的问题。{STYLE_BAN}"""


# 单次续写的篇幅（自然段数）。
# 提示词里只许有这一处定义：从前系统提示写「5 到 10 段」、装配上下文时又写
# 「2 到 4 段」，同一次请求里塞进两个互相矛盾的篇幅指令，模型无所适从。
PARAS_MIN, PARAS_MAX = 4, 8


# ── 补全设定：把作者给的大纲扩写成完整设定 ──────────────
# 分小节生成：一次只让模型写一节。小模型在长输出时会退化复读，拆成
# 六次短请求能显著降低概率，还能让后续小节读到前面已定的名字、保持一致。
EXPAND_SECTIONS: list[tuple[str, str]] = [
    ("题材与基调", "类型、风格；用一句话说清核心冲突——主角想要什么、被什么挡住。"),
    ("人物", "主角与主要配角：姓名、身份、性格、彼此关系、各自的目标与秘密。至少 3 个有名有姓的人物。"),
    ("世界观", "时代、地点、规则设定。若含修炼 / 等级体系，从低到高把每一级列全，给出名称与大致实力刻度。"),
    ("势力与地名", "至少 4 个门派 / 组织 / 关键场景：名称、定位、与主角的关系。"),
    ("专有名词", "重要功法、器物、称号等：名称 + 具体作用。"),
    ("笔法", "建议的人称、时态、句法密度、对话习惯。"),
]

# 每次请求的系统提示：只写一节，且必须与已定的设定一致。
EXPAND_PROMPT = """你是一位小说设定顾问，负责把作者给的粗略方向补全成可用于写作的作品设定。

你每次只写一个小节，用【】标注该小节标题。

硬性要求：
1. 直接输出该小节内容，不要寒暄、不要复述要求、不要写小说正文、不要写其它小节。
2. 与作者给定方向一致；未提及处可合理补全，但不得与之矛盾。
3. 若给出了「已确定的设定」，其中的人名、地名、境界、数值必须与之完全一致，
   不得改动，也不得另起第二种叫法。
4. 命名、数值、层级必须具体且内部自洽。
5. 禁止空泛形容：不写「实力强大」「神秘莫测」这类词，
   要写清它强在哪、是什么、有什么具体表现或数值。
6. 命名风格统一，使用中文全角标点。
7. 控制篇幅：一个小节通常 150-400 字，写清要点即可，不要堆砌名词。"""


class AIError(RuntimeError):
    pass


# ── 底层：请求构造 ──────────────────────────────────────

def _headers(s: Settings) -> dict[str, str]:
    h = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream",
    }
    if s.api_key:
        h["Authorization"] = f"Bearer {s.api_key}"
    return h


# ── 采样惩罚项 ──────────────────────────────────────────
# 网关对不认识的参数态度不一：有的忽略，有的直接 400。
# 所以这里只发「非默认值」—— 全默认时请求体与旧版逐字节一致，
# 老配置不会因为升级而突然调不通。
_SAMPLING_DEFAULTS = {"presence_penalty": 0.0, "frequency_penalty": 0.0, "top_p": 1.0}
# 一旦确认网关不吃这些参数，本次会话内不再重试（见 _open）
_sampling_ok = True


def _sampling(s: Settings) -> dict:
    """取出非默认的采样项。空 dict = 一个都不发。"""
    if not _sampling_ok:
        return {}
    out: dict[str, float] = {}
    for key, dflt in _SAMPLING_DEFAULTS.items():
        try:
            v = float(getattr(s, key, dflt))
        except (TypeError, ValueError):
            continue
        if abs(v - dflt) > 1e-6:
            out[key] = round(v, 2)
    return out


def _payload(
    s: Settings,
    messages: list[dict],
    stream: bool,
    max_tokens: int | None = None,
    sampling: bool = True,
    temperature: float | None = None,
) -> bytes:
    body: dict = {
        "model": s.model,
        "messages": messages,
        "temperature": s.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or s.max_tokens,
        "stream": stream,
    }
    if sampling:
        body.update(_sampling(s))
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
    """决定下一次重试前等多久：优先听从 Retry-After，否则指数退避。"""
    hdr = None
    try:
        hdr = e.headers.get("Retry-After") if e.headers else None
    except Exception:
        hdr = None
    if hdr:
        try:
            return max(1.0, min(MAX_DELAY, float(hdr)))
        except (TypeError, ValueError):
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            try:
                when = parsedate_to_datetime(hdr)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(1.0, min(MAX_DELAY, (when - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
    return min(MAX_DELAY, BASE_DELAY * (2 ** attempt))


def _sleep_backoff(wait: float, should_stop: Callable[[], bool] | None = None) -> None:
    """分片睡眠，保证「停止」在退避等待期间也能及时生效。"""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if should_stop and should_stop():
            return
        time.sleep(min(0.5, max(0.0, end - time.monotonic())))


def _open(
    s: Settings,
    messages: list[dict],
    stream: bool,
    max_tokens: int | None = None,
    on_retry: Callable[[int, float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    temperature: float | None = None,
):
    global _sampling_ok
    data = _payload(s, messages, stream, max_tokens, temperature=temperature)

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            s.endpoint(), data=data, headers=_headers(s), method="POST"
        )
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            # 限流 / 临时不可用：退避后重试
            if e.code in RETRY_STATUS and attempt < MAX_RETRIES:
                if should_stop and should_stop():
                    raise AIError("已停止") from e
                wait = _retry_after(e, attempt)
                if on_retry:
                    on_retry(attempt + 1, wait)
                _sleep_backoff(wait, should_stop)
                continue
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8", "ignore"))
                detail = body.get("error", {}).get("message") or body.get("message") or ""
            except Exception:
                pass

            # 网关不认惩罚项：整个会话内摘掉它们重发一次，不再白试。
            # 只有当报错确实指向这些参数（或网关没给理由）时才降级，
            # 避免把「上下文超长」之类的真错误误判成参数问题。
            low = detail.lower()
            if (
                e.code == 400
                and _sampling_ok
                and _sampling(s)
                and (not detail or any(k in low for k in _SAMPLING_DEFAULTS))
            ):
                _sampling_ok = False
                data = _payload(s, messages, stream, max_tokens, sampling=False,
                                temperature=temperature)
                continue

            raise AIError(f"HTTP {e.code} {e.reason}" + (f" — {detail}" if detail else "")) from e
        except urllib.error.URLError as e:
            raise AIError(f"无法连接 {s.base_url} — {e.reason}") from e
        except (TimeoutError, OSError) as e:
            # 连接或首包读取超时：退避重试
            if attempt < MAX_RETRIES:
                if should_stop and should_stop():
                    raise AIError("已停止") from e
                wait = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
                if on_retry:
                    on_retry(attempt + 1, wait)
                _sleep_backoff(wait, should_stop)
                continue
            raise AIError(f"请求超时（超过 {TIMEOUT} 秒无响应）") from e


# ── 流句柄：让「停止」真正能停 ──────────────────────────

class StreamHandle:
    """可中断的流式句柄。

    为什么需要它：连接存活期间，读取线程会阻塞在 socket 上。
    单靠一个布尔标志位，只有在「下一行数据到达」时才有机会被检查；
    如果服务器正在思考、迟迟不发下一帧，点「停止」就完全没反应。
    这里直接持有底层 response 对象，停止时关闭它，让读取立即抛错退出。
    """

    def __init__(self):
        self._resp = None
        self._stopped = False
        self._lock = threading.Lock()

    def attach(self, resp) -> None:
        with self._lock:
            self._resp = resp
            if self._stopped:
                self._force_close()

    def detach(self) -> None:
        with self._lock:
            self._resp = None

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._force_close()

    def _force_close(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
            self._resp = None

    @property
    def stopped(self) -> bool:
        return self._stopped


# ── 公开：流式续写 ──────────────────────────────────────

# 被 max_tokens 硬截断时，最多自动接续几轮
MAX_CONTINUATIONS = 4
# 指定目标字数时的最大接续轮数（每轮约 max_tokens 上限）
MAX_TARGET_ROUNDS = 60
# 接续时，模型常会复述上一轮结尾。低于该长度的重叠视为巧合，不去重。
_MIN_OVERLAP = 8
# 整块复述的最长搜索窗口（段落数），见 collapse_blocks
MAX_DUP_BLOCK = 200

# 自动接续时发给模型的指令。措辞直接决定它是否会把上一段和下一段挤在一起。
CONTINUE_MSG = (
    "继续往下写。如果上文末尾停在一个句子中间，先把它补完整，再自然往下写。"
    "不要重复已经写过的任何内容。保持小说原有的分段节奏：叙述、动作、对话该分段就分段，"
    "对话必须独立成段，不要把几段话挤进同一段。"
)


def _overlap_len(prior: str, buf: str, cap: int = 600) -> int:
    """buf 的前缀若是 prior 的后缀，返回最长重叠长度。用于接续去重。"""
    n = min(len(buf), len(prior), cap)
    for k in range(n, 0, -1):
        if prior.endswith(buf[:k]):
            return k
    return 0


def collapse_repeats(text: str, min_len: int = 6, max_len: int = 60) -> str:
    """折叠紧邻的重复片段：把「XXX XXX」压成「XXX」。

    模型偶发退化，会连续输出两遍完全相同的短语
    （如「苏晚棠眸中闪过苏晚棠眸中闪过」）。落盘前清理一遍。

    逐初一比对中先做一次 O(1) 的首字符过滤 —— 两段要相等，首字符必然相等，
    直接切片比较在十万字正文上会退化到秒级。
    """
    if not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        hit = False
        upper = min(max_len, (n - i) // 2)
        for L in range(upper, min_len - 1, -1):
            if text[i + L] != text[i]:
                continue
            if text[i:i + L] == text[i + L:i + 2 * L]:
                out.append(text[i:i + L])
                i += 2 * L
                hit = True
                break
        if not hit:
            out.append(text[i])
            i += 1
    return "".join(out)


def collapse_blocks(text: str, min_dup_len: int = 12, window: int = 6) -> str:
    """清掉接续时产生的整块复述。

    模型在续写轮常把上一轮的某段乃至整块原样重写一遍。流里只比对结尾
    （_overlap_len）拦不住「中间整块复述」，落盘前再扫一遍：

      1. 当前段与最近 window 段中某段相同，或是它的结尾 → 丢掉当前段；
      2. 相邻段落块整体重复 [A B C D A B C D] → 只保留一份。

    短于 min_dup_len 的段落不参与判重，避免误删「跪下。」这类正常复现。
    反复执行直到不再变化。
    """
    if not text:
        return text
    paras = text.split("\n\n")
    for _ in range(4):
        before = len(paras)

        # 1) 单段复述
        out: list[str] = []
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

        # 2) 段落块整体重复
        #    搜索窗口封顶 MAX_DUP_BLOCK：真实复述永远是局部的，不封顶会让
        #    长篇（数千段）在这里退化成 O(n²) 的列表比较，整篇卡住几秒。
        n = len(paras)
        out = []
        i = 0
        while i < n:
            hit = False
            for L in range(min(MAX_DUP_BLOCK, (n - i) // 2), 0, -1):
                if paras[i:i + L] == paras[i + L:i + 2 * L]:
                    out.extend(paras[i:i + L])
                    i += 2 * L
                    hit = True
                    break
            if not hit:
                out.append(paras[i])
                i += 1
        paras = out

        if len(paras) == before:
            break
    return "\n\n".join(paras)


def degenerate_reason(text: str) -> str:
    """检测模型「复读退化」：返回原因字符串，正常则返回空串。

    小模型在长输出时会词穷，退化成一长串无标点的名词轰炸或重复片段
    （实测某次补全从「化神境」起无限堆词到 "ronnmetres ronnmetres"）。
    这种输出 token 没超、finish_reason 还是 stop，截断检测拦不住，
    必须靠内容特征识别。
    """
    t = text.strip()
    if len(t) < 200:
        return ""
    # 特征一：正常中文写作不会出现超长无标点片段
    runs = re.split(r"[，。！？；：、,.!?;:\n]", t)
    longest = max((len(x) for x in runs), default=0)
    if longest >= 100:
        return f"出现连续 {longest} 字无标点，疑似复读退化"
    # 特征二：清理重复后大幅缩水
    cleaned = collapse_blocks(collapse_repeats(t))
    if len(cleaned) < len(t) * 0.7:
        return f"重复内容过多（{len(t)} 字清理后仅剩 {len(cleaned)} 字）"
    return ""


def stream_completion(
    s: Settings,
    messages: list[dict],
    handle: StreamHandle | None = None,
    should_stop: Callable[[], bool] | None = None,
    target_chars: int = 0,
    on_retry: Callable[[int, float], None] | None = None,
    on_round: Callable[[int], None] | None = None,
    on_round_text: Callable[[str], "str | None"] | None = None,
) -> Iterator[str]:
    """逐段产出正文增量，被截断时自动接续，直到自然收尾或写满目标。

    为什么需要接续：max_tokens 会把输出从一句话中间硬切掉，表现为
    「写着写着突然停在半句」。SSE 最后一帧的 finish_reason 会告诉
    我们原因 —— "length" 就是被切了，"stop" 才是模型自己收的尾。
    检测到 "length" 就把已写内容作为助手消息回填，再请求一次。

    target_chars > 0 时进入「定量模式」：无论模型是自然收尾还是被截断，
    只要累计字数未达标就继续请求，直到写满目标或触发轮数上限。
    """
    convo = list(messages)
    total = 0
    rounds = MAX_TARGET_ROUNDS if target_chars > 0 else MAX_CONTINUATIONS
    emitted = ""  # 已产出的全部正文，用于接续时比对去重

    for round_i in range(rounds):
        if (handle and handle.stopped) or (should_stop and should_stop()):
            return
        if round_i > 0 and on_round:
            on_round(round_i)

        # urlopen 在连接阶段就会阻塞（服务器无响应时可达 20 秒以上），
        # 此期间 handle 还拿不到 response 对象，掐不断。等它返回后，
        # 如果用户已请求停止，就静默退出，不要弹「连接失败」。
        try:
            resp = _open(
                s, convo, stream=True,
                should_stop=should_stop,
                on_retry=on_retry,
            )
        except AIError:
            if (handle and handle.stopped) or (should_stop and should_stop()):
                return
            raise

        if handle:
            handle.attach(resp)

        finish_reason = None
        raw_parts: list[str] = []   # 本轮模型原始输出（用于回填对话）
        round_out: list[str] = []   # 本轮去重后真正产出的内容
        buf = ""                    # 去重缓冲
        live = round_i == 0         # 首轮无需去重，直接输出

        try:
            with resp:
                for raw in resp:
                    if (handle and handle.stopped) or (should_stop and should_stop()):
                        return
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    for choice in obj.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if not piece:
                            fr = choice.get("finish_reason")
                            if fr:
                                finish_reason = fr
                            continue
                        raw_parts.append(piece)
                        if live:
                            emit = piece
                        else:
                            buf += piece
                            k = _overlap_len(emitted, buf)
                            if k == len(buf):
                                emit = ""       # 仍在复述上一轮，先按住
                            else:
                                if k < _MIN_OVERLAP:
                                    k = 0       # 太短，多半是巧合
                                emit = buf[k:]
                                buf = ""
                                live = True
                        if emit:
                            if round_i > 0 and not round_out and not emitted.endswith("\n"):
                                emit = "\n\n" + emit
                            round_out.append(emit)
                            total += len(emit)
                            yield emit
                            if target_chars > 0 and total >= target_chars:
                                return
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
        except (TimeoutError, OSError):
            # 流读到一半超时：当作本轮被截断处理，用已收内容续下一轮，
            # 而不是让整次生成失败。已产出的文字不会丢。
            if (handle and handle.stopped) or (should_stop and should_stop()):
                return
            finish_reason = "length"
        except Exception:
            # 停止导致的读取中断是预期行为，不当错误抛
            if (handle and handle.stopped) or (should_stop and should_stop()):
                return
            raise
        finally:
            if handle:
                handle.detach()

        # 整轮都在复述上一轮（从未脱离重叠）：把缓冲吐出，避免丢内容
        if not live and buf:
            round_out.append(buf)
            total += len(buf)
            yield buf
            buf = ""

        round_text = "".join(round_out)
        # 每轮结束的回钩：可用于自动审校 —— 若返回重写后的文本，
        # 以它替换本轮产出（界面已同步做过尾部替换，见 ui._round_review）。
        if on_round_text and round_text:
            fixed = on_round_text(round_text)
            if fixed is not None:
                round_text = fixed
        emitted += round_text

        # 定量模式：只要没达标就续写，不管 finish_reason
        if target_chars > 0:
            if not raw_parts:
                return
            convo = convo + [
                {"role": "assistant", "content": round_text or "".join(raw_parts)},
                {
                    "role": "user",
                    "content": CONTINUE_MSG,
                },
            ]
            continue

        # 自然模式：模型收尾或被停止就结束，被截断才接续
        if finish_reason != "length":
            return

        convo = convo + [
            {"role": "assistant", "content": round_text or "".join(raw_parts)},
            {
                "role": "user",
                "content": CONTINUE_MSG,
            },
        ]


# ── 非创作类任务的温度 ──────────────────────────────────
# 续写要野，摘要、重排、分析要稳。这些活儿共用 s.temperature（默认 0.92）
# 时，模型会把「压缩前情」写成续写、把「只重排不改字」改成重写 ——
# 温度跟着任务走，不跟着续写设置走。
TEMP_TASK = 0.3      # 压缩记忆 / 章节摘要：求准
TEMP_STRICT = 0.1    # 重排段落：一个字都不许改
TEMP_ANALYSIS = 0.4  # 通读分析：要稳，但允许一点归纳
TEMP_SETTING = 0.5   # 补全设定：命名可以发散，但人物/境界/数值必须自洽
TEMP_IDEA = 0.8      # 拟书名：纯发散


def complete(
    s: Settings,
    messages: list[dict],
    max_tokens: int = 700,
    on_retry: Callable[[int, float], None] | None = None,
    temperature: float | None = None,
    strict: bool = False,
) -> str:
    """一次性调用（用于记忆摘要、设定补全等）。

    temperature 为 None 时用 TEMP_TASK —— 这类任务求准，不该跟着续写的
    0.92 一起飘；确实想要发散的调用方（拟书名等）自己传高温。

    strict=True 时，若输出被 max_tokens 硬截断（finish_reason == "length"）
    就抛 AIError。默认 False 是因为摘要这类短任务截断了也无伤大雅；但设定
    补全这种要求完整的长任务必须 strict —— 否则半截结果会被当成完整设定。
    """
    temp = TEMP_TASK if temperature is None else temperature
    for attempt in range(MAX_RETRIES + 1):
        resp = _open(s, messages, stream=False, max_tokens=max_tokens,
                     on_retry=on_retry, temperature=temp)
        try:
            with resp:
                raw = resp.read()
        except (TimeoutError, OSError) as e:
            # 连接已建立但读响应体超时：退避后整个请求重试
            if attempt < MAX_RETRIES:
                wait = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
                if on_retry:
                    on_retry(attempt + 1, wait)
                _sleep_backoff(wait)
                continue
            raise AIError(f"请求超时（超过 {TIMEOUT} 秒未返回完整响应）") from e
        obj = json.loads(raw.decode("utf-8", "ignore"))
        choices = obj.get("choices") or []
        if not choices:
            raise AIError("模型没有返回任何内容")
        choice0 = choices[0]
        text = (choice0.get("message") or {}).get("content", "").strip()
        if strict and choice0.get("finish_reason") == "length":
            raise AIError(
                f"输出被单次上限（{max_tokens} tokens）截断，结果不完整。"
                "请在「引擎设置 → 单次上限」调大后重试。"
            )
        return text
    raise AIError("请求失败")


# ── 上下文装配 ──────────────────────────────────────────

ANALYSIS_PROMPT = """你是小说编辑，负责从已有材料里提炼出供续写用的作品设定。

仔细阅读给定的参考语料与已写正文，按以下小节输出，用【】标注小标题：

【人物】姓名、身份、性格、彼此关系、当前处境。只写材料中确有的信息。
【世界观】时间地点、规则设定、专有名词及其确切含义。
【脉络】已经发生的关键事件，按时间顺序；指出尚未回收的伏笔。
【笔法】人称、时态、句法密度、对话习惯、意象偏好，以及必须延续的语言特征。

要求：陈述事实，不做评价，不给建议，不写"这部作品"。总长 400-700 字。"""


def expand_settings(p: Project, on_step: Callable[[str], None] | None = None) -> str:
    """把作者给的粗略方向，补全成完整的作品设定。

    分六次请求，每次只写一节 —— 一次让模型吐满六节，小模型会在后半程
    退化复读（实测从「化神境」起堆词到 "ronnmetres"）。拆短 + 每节做完
    退化检测，既降低退化概率，也保证退化时明确报错而非默默存下垃圾。
    """
    s = p.settings
    direction = p.premise.strip()
    if not direction:
        raise AIError("先写几句故事方向，我才有依据补全。")

    out: list[str] = []
    n = len(EXPAND_SECTIONS)
    for i, (title, req) in enumerate(EXPAND_SECTIONS, 1):
        if on_step:
            on_step(f"正在补全设定（{i}/{n}）：{title}…")
        parts = ["【作者给出的方向】\n" + direction]
        if out:
            parts.append(
                "【已确定的设定（人名、地名、境界、数值必须与之一致）】\n"
                + "\n\n".join(out)
            )
        parts.append(
            f"本次只写【{title}】这一节：{req}\n"
            f"直接输出【{title}】及其内容，不要写其它小节，不要复述要求。"
        )
        messages = [
            {"role": "system", "content": EXPAND_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        text = complete(
            s, messages,
            max_tokens=2000,
            temperature=TEMP_SETTING,
            strict=True,
        ).strip()
        why = degenerate_reason(text)
        if why:
            raise AIError(
                f"补全「{title}」时模型输出异常：{why}。"
                "请重试；若反复出现，说明该模型长输出不稳定，建议换用更强的模型。"
            )
        if not text.startswith("【"):
            text = f"【{title}】\n{text}"
        out.append(text)

    if on_step:
        on_step("设定补全完成")
    return "\n\n".join(out)


def analyze_corpus(p: Project, on_step: Callable[[str], None] | None = None) -> str:
    """让 AI 全面分析语料与正文，产出可复用的人物/世界/脉络/笔法设定。"""
    s = p.settings
    if on_step:
        on_step("正在通读材料…")

    parts: list[str] = []
    # 作者手写的方向也是「材料」，必须一并交给模型 —— 否则分析产出会把它
    # 整体覆盖掉，用户写的大方向就此丢失。
    if p.premise.strip():
        parts.append("【作者设定方向（须遵守）】\n" + p.premise.strip())
    digest = corpus_digest(p.corpus, s.corpus_budget)
    if digest:
        parts.append("【参考语料】\n" + digest)
    chapters = [c for c in p.chapters if c.body.strip()]
    if chapters:
        total_budget = max(6000, s.context_budget)
        per = max(600, total_budget // len(chapters))
        chunks: list[str] = []
        for i, c in enumerate(chapters, 1):
            b = c.body
            if len(b) > per:
                half = per // 2
                b = b[:half] + "\n…（中略）…\n" + b[-half:]
            chunks.append(f"第 {i} 章《{c.title}》\n{b}")
        parts.append(
            f"【《{p.title}》已写正文（共 {len(chapters)} 章）】\n" + "\n\n".join(chunks)
        )
    if p.memory.strip():
        parts.append("【既有前情记忆】\n" + p.memory.strip())

    if not parts:
        raise AIError("没有可分析的材料。先在右侧添加语料，或先写一些正文。")

    messages = [
        {"role": "system", "content": ANALYSIS_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    return complete(s, messages, max_tokens=1400, temperature=TEMP_ANALYSIS)


def corpus_digest(corpus: list[Corpus], budget: int) -> str:
    """把上传的语料压进预算内：整份优先，超预算则均分截断。"""
    if not corpus:
        return ""
    per = max(400, budget // max(1, len(corpus)))
    parts = []
    for c in corpus:
        body = c.text if len(c.text) <= per else c.text[:per] + "…（后略）"
        parts.append(f"《{c.name}》\n{body}")
    return "\n\n".join(parts)


def _recent_context(p: Project, body: str, budget: int) -> str:
    """以 body 为终点，向前跨章取最近 budget 字的正文。

    关键作用：自动分章后新章是空的，若只看当前章，模型会丢失上一章的
    人物与情节。这里回溯前一章（乃至更早），保证上下文不断裂 ——
    否则「一键生成」分到第三章时，AI 连主角叫什么都不知道了。
    """
    if len(body) >= budget:
        return body[-budget:]
    parts = [body]
    need = budget - len(body)
    idx = p.current
    while need > 0 and idx > 0:
        idx -= 1
        prev = p.chapters[idx].body if idx < len(p.chapters) else ""
        if not prev:
            continue
        parts.append(prev)
        need -= len(prev) + 2
    text = "\n\n".join(reversed(parts))
    return text[-budget:] if len(text) > budget else text


# 时间标记 → 归一化标签。从紧邻上文里取最后一个命中，作为续写的时间锚点。
_TIME_MARKERS = [
    ("凌晨", "凌晨"), ("破晓", "破晓"), ("黎明", "黎明"),
    ("清晨", "清晨"), ("天亮", "清晨"), ("早晨", "早晨"), ("晨", "早晨"),
    ("正午", "正午"), ("午时", "午时"), ("中午", "中午"),
    ("午后", "午后"), ("黄昏", "黄昏"), ("傍晚", "傍晚"),
    ("暮色", "暮色"), ("暮", "傍晚"),
    ("深夜", "深夜"), ("午夜", "午夜"), ("夜色", "夜晚"), ("夜里", "夜晚"), ("夜", "夜晚"),
]


def _scene_hint(body: str, tail: str | None = None) -> str:
    """从紧邻上文里找最后一个时间标记，给续写一个时间锚点。

    模型在长文续写时容易丢掉「现在几点」——上一段是夜，下一段突然写成清晨。
    这里只做一件事：把上文最后出现的时间词提出来，明确告诉模型别跳。
    找不到任何时间词就返回空串 —— 宁可不提示，也不瞎猜。
    """
    text = (tail if tail is not None else body)[-1500:]
    if not text.strip():
        return ""
    best_pos, best_len, best_label = -1, 0, ""
    for kw, label in _TIME_MARKERS:
        pos = text.rfind(kw)
        if pos < 0:
            continue
        if pos > best_pos or (pos == best_pos and len(kw) > best_len):
            best_pos, best_len, best_label = pos, len(kw), label
    if best_pos < 0:
        return ""
    return (
        f"【当前时间】（据紧邻上文推断）{best_label}。"
        "续写必须与这个时间保持一致，不得跳跃、倒流或凭空改换昼夜。"
    )


def build_messages(
    p: Project,
    tail: str | None = None,
    opening: bool = False,
) -> list[dict]:
    """装配一次请求的完整上下文。

    tail    —— None 时取当前章正文；传入可做续写接龙。
    opening —— 开篇模式：只给设定与语料，不续写。
    """
    s = p.settings
    ch = p.chapter
    body = ch.body if tail is None else tail

    blocks: list[str] = []

    # ── 开篇模式：只需要设定 + 语料 ──
    if opening:
        if p.premise.strip():
            blocks.append("【作品设定】（人物、世界、地名、势力等，须严格遵守）\n" + p.premise.strip())
        digest0 = corpus_digest(p.corpus, s.corpus_budget)
        if digest0:
            blocks.append("【参考语料】（世界观与设定，须严格遵守）\n" + digest0)
        if not blocks:
            blocks.append("【作品设定】（作者未提供，请自由发挥，题材、背景、主角由你决定）")
        blocks.append(f"请为《{p.title}》撰写「{ch.title}」的开头。")
        return [
            {"role": "system", "content": OPENING_PROMPT},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]

    if p.premise.strip():
        blocks.append("【作品设定】（人物、世界、地名、势力、笔法，最高优先级，须严格遵守）\n" + p.premise.strip())

    if p.memory.strip():
        blocks.append("【前情记忆】（更早章节的压缩摘要，必须当作已发生的事实）\n" + p.memory.strip())

    digest = corpus_digest(p.corpus, s.corpus_budget)
    if digest:
        blocks.append("【参考语料】（人物设定、世界观、大纲，须严格遵守）\n" + digest)

    recent = _recent_context(p, body, s.context_budget)
    if recent.strip():
        spanned = len(body) < s.context_budget and p.current > 0
        lead = "【前文（含上一章结尾，人物与情节以此为准）】" if spanned else "【紧邻的上文】"
        blocks.append(f"{lead}\n{recent}")

    hint = _scene_hint(body, tail)
    if hint:
        blocks.append(hint)

    blocks.append(
        f"现在请续写《{p.title}》的「{ch.title}」。"
        f"直接输出接下来的 {PARAS_MIN} 到 {PARAS_MAX} 个自然段正文，"
        "从紧邻上文的断点接着往下走，不要复述上文已经交代过的内容。"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


# ── 开篇模式：给设定，让模型自己起名开篇 ──────────────
OPENING_PROMPT = f"""你是一位职业小说家，正在为一部新长篇撰写开篇。

根据给定的【作品设定】写出第一章的开头。这是全书的第一段文字，读者对
这个世界、这个人一无所知 —— 你的首要任务是：在抛悬念之前，先让读者看懂
眼前正在发生什么，以及这事是怎么走到这一步的。

开头必须做到：
1. 第一段就落到一个具体场景：谁、在哪里、正在做什么、正面对什么麻烦。
   不要用天气或纯旁白空转，但也不要把背景完全藏起来 —— 该交代的交代清楚。
2. 主角在前三句内出场并进入处境。开头 300 字之内，读者必须能明白三件事：
   主角是谁（身份与处境）、这是什么世界、眼下这段麻烦会把他推向哪里。
3. 埋一个钩子 —— 一个反常的细节、一句没头没尾的话、一件正在逼近的麻烦，
   让读者想往下读。钩子可以悬着不解释，但主角眼下的处境必须说清楚。
4. 设定中点名的核心要素（如系统、金手指、关键身份、核心冲突）必须在开篇
   出现或被明确暗示，不许只当背景板。比如设定写了「系统」，开头就该见到它。
5. 场景之间必须承接：段与段、动作与动作之间用一两句过渡连起来，让读者看得
   出事情一步步是怎么发生的，不要把开头写成互不相连的画面切片。

硬性要求：
1. 直接输出正文，不要任何解释、标题、序号或 Markdown 标记。
2. 设定只给大致方向时，人物姓名、地名、门派、专有名词由你补全，取得自然、
   好听、内部自洽；但若设定已给出名字，必须沿用。
3. 段落以叙事节奏为准，一般 2 到 4 句成一段。对话仍独立成段。
   不要为了短而把每句话都拆成单独一段 —— 那读起来像提纲，不像小说。
4. 禁止这些被写烂的开场套路：主角跪在祠堂挨训、家族惨遭灭门、废柴被退婚、
   夺宝当场遭围杀。要给读者一个新鲜的切入角度。
5. 使用中文全角标点，对话使用中文引号。
6. 只写这一章的开头，在一个完整的句子处收住，不要总结、不要升华。{STYLE_BAN}"""


REFORMAT_PROMPT = """你是文本排版编辑。下面给你一段小说正文。

你的唯一任务是：**只调整换行位置，一个字符都不许增删改**。

分段规则：
1. 对话独立成段 —— 每一句引号内的直接引语单独占一段，包括引语前后的「某某说」。
2. 连续叙述按语义切分：动作、心理、环境各自成段，每段 1 到 3 句，通常不超过 80 字。
3. 场景切换、时间跳跃、视角转换处必须分段。
4. 不改变任何文字、标点、段落顺序。不添加空行分隔符。

直接输出重排后的全文，不要解释，不要前言，不要用代码块包裹。"""


def reformat_chapter(p: Project, on_step=None) -> str:
    """只重排换行，不改一个字。

    正文本身不动，只是让段落分布合理 —— 对话独立、叙述短段。
    调用方需先备份，因为这会整体替换正文。
    """
    s = p.settings
    body = p.chapter.body
    if len(body.strip()) < 50:
        raise AIError("正文太短，无需重排。")

    if on_step:
        on_step("正在重排段落…")

    # 分块处理，避免长文超出上下文
    budget = max(2000, s.context_budget)
    chunks: list[str] = []
    remaining = body
    while remaining:
        if len(remaining) <= budget:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, budget)
        if cut < budget // 2:
            cut = budget
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]

    out_parts: list[str] = []
    for i, chunk in enumerate(chunks):
        if on_step:
            on_step(f"正在重排第 {i + 1} / {len(chunks)} 段…")
        messages = [
            {"role": "system", "content": REFORMAT_PROMPT},
            {"role": "user", "content": chunk},
        ]
        result = complete(s, messages, max_tokens=s.max_tokens, temperature=TEMP_STRICT)
        out_parts.append(result.strip())

    merged = "\n\n".join(out_parts)

    # 完整性校验：字符数差得太多说明模型改字了，拒绝
    src_plain = "".join(body.split())
    out_plain = "".join(merged.split())
    if abs(len(src_plain) - len(out_plain)) > max(20, len(src_plain) * 0.02):
        raise AIError(
            f"重排后字数差异过大（原 {len(src_plain)} → 新 {len(out_plain)}），"
            "模型可能改动了内容，已放弃。请重试。"
        )

    return merged


TITLE_PROMPT = """你是小说命名顾问。根据作者给出的故事方向，拟 6 个候选书名。

硬性要求：
1. 书名要好听、好记，贴合设定的题材与气质，彼此风格有区分度。
2. 每行一个，格式严格为：书名｜一句极简说明（说明不超过 12 字）。
3. 只输出这 6 行，不要序号、不要标题、不要其它任何内容。
4. 使用中文标点。"""


def suggest_titles(p: Project, on_step: Callable[[str], None] | None = None) -> str:
    """依据故事设定（或已有正文）拟候选书名。"""
    s = p.settings
    if on_step:
        on_step("正在拟书名…")

    parts: list[str] = []
    if p.premise.strip():
        parts.append("【故事方向】\n" + p.premise.strip())
    # 注意不是 p.analysis —— 旧版的「作品简报」已在载入时一次性并入设定，
    # 那个字段此后恒为空。这里要补的是「前情记忆」里实际发生过的事。
    if p.memory.strip():
        parts.append("【前情记忆】\n" + p.memory.strip())
    digest = corpus_digest(p.corpus, s.corpus_budget)
    if digest:
        parts.append("【参考语料】\n" + digest)
    body = p.chapter.body
    if body.strip():
        parts.append("【已写正文片段】\n" + body[-2000:])
    if not parts:
        raise AIError("先写一点故事方向或添加语料，我才有依据取名。")

    messages = [
        {"role": "system", "content": TITLE_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    return complete(s, messages, max_tokens=400, temperature=TEMP_IDEA)


# ── 选中改写：四种模式 ──────────────────────────────────
# 值 = (菜单标签, 交给模型的具体指令)
REWRITE_MODES: dict[str, tuple[str, str]] = {
    "polish": (
        "润色",
        "在不改动情节、信息与篇幅的前提下打磨文字：让动词更准确、去掉重复的"
        "形容词与副词、修掉拗口的句子。意思必须与原段完全一致。",
    ),
    "expand": (
        "扩写",
        "把这段写得更充分：补足必要的动作、感官与心理细节，让场面真正落地。"
        "情节走向不得改变，篇幅约为原文的 1.5 到 2 倍。",
    ),
    "condense": (
        "缩写",
        "删掉冗余，压缩到原文一半左右的篇幅。所有关键信息、转折与人物反应"
        "都必须保留，只是写得更紧。",
    ),
    "rewrite": (
        "重写",
        "换一种写法重讲同一件事：可以调整句式、节奏与切入角度，但人物做了什么、"
        "说了什么、结果如何，必须与原段一致。",
    ),
}

REWRITE_PROMPT = f"""你是一位职业小说家，正在修改自己稿子里的一段文字。

硬性要求：
1. 直接输出改写后的正文，不要任何解释、标题、序号或 Markdown 标记。
2. 情节、信息、人物行为与说话内容必须与原文一致；只改动表达，不改动事实。
3. 严格遵守【作品设定】里的人名、称谓、关系与专有名词，不得改写错。
4. 人称、时态、语气必须与【上文结尾】和【下文开头】自然衔接 ——
   改写后的第一句要接得住上一段，最后一句要接得上下一段。
5. 段落必须短：每个自然段只写 1 到 3 句，通常不超过 80 字。
   对话必须独立成段，不得与叙述混在同一段里。
6. 使用中文全角标点，对话使用中文引号。{STYLE_BAN}"""


# ── 审校报告：生成器—审校器分离 ─────────────────────────
# 续写提示词只管「怎么写」，不负责验收；这里用一次独立调用回头检查产出，
# 把模型自己看不见的问题（违反文风禁区、情节没推进）挑出来，交给作者定夺。
REVIEW_PROMPT = f"""你是一位严格的小说编辑，负责审校一段刚写完的正文。

只做两件事，不要改写正文，也不要泛泛夸奖：

一、文风审查。逐条对照下面的文风禁区，指出违反之处。每条都要引用原文里的
问题句（可只引半句），并说明它违反了哪一条。没有违反就写「无」。
{STYLE_BAN}

二、情节审查。回答：这一段有没有「事发生」——有人想要某样东西、撞上阻碍、
做出反应，局面随之改变？若只是写景、回忆、心理独白或概述而没有推进，
指出是哪几句在凑字数。

输出格式（用【】标注，不要用 Markdown 代码块）：
【文风】逐条列出问题，或「无」
【情节】一句话结论，并引用问题句
【总评】合格 或 需修改，并给出一句最关键的修改建议。

要求：
1. 只针对给出的正文，不要臆造原文没有的内容。
2. 引用问题句时保持原样，不要改写。
3. 若正文确实写得好，就直说合格，不要为凑数硬找问题。"""


def build_rewrite_messages(
    p: Project,
    selection: str,
    mode: str,
    before: str = "",
    after: str = "",
) -> list[dict]:
    """装配一次「局部改写」的上下文。

    为什么前后都要给：只给选中段，模型不知道开头该用什么语气接上一段、
    也不知道结尾该停在哪儿才不和下一段撞车。前后各取一小段作为接缝上下文。
    """
    label, instruction = REWRITE_MODES.get(mode, REWRITE_MODES["polish"])
    blocks: list[str] = []

    if p.premise.strip():
        blocks.append("【作品设定】（最高优先级，须严格遵守）\n" + p.premise.strip()[:1200])

    if before.strip():
        blocks.append("【上文结尾】（仅供衔接，不要复述）\n……" + before.strip()[-600:])
    if after.strip():
        blocks.append("【下文开头】（务必自然衔接到这里）\n" + after.strip()[:600] + "……")

    blocks.append("【待改写的原文】\n" + selection.strip())
    blocks.append(f"【本次指令】{label}：{instruction}\n直接给出改写后的正文。")

    return [
        {"role": "system", "content": REWRITE_PROMPT},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def review_chapter(p: Project, body: str, on_step: Callable[[str], None] | None = None) -> str:
    """审校一段正文：文风是否违反禁区 + 情节是否推进。

    生成器—审校器分离：续写用的提示词只管「怎么写」，不负责验收；
    这里用一次独立调用回头检查产出。带上作品设定作为比对参照，
    正文若与设定有明显矛盾（人名、境界）也会一并指出。
    """
    s = p.settings
    body = body.strip()
    if len(body) < 50:
        raise AIError("正文太短，无需审校。")
    if on_step:
        on_step("正在审校正文…")

    parts: list[str] = []
    if p.premise.strip():
        parts.append(
            "【作品设定（供比对，若正文与之矛盾请一并指出）】\n"
            + p.premise.strip()[:2000]
        )
    parts.append("【待审校的正文】\n" + body)

    messages = [
        {"role": "system", "content": REVIEW_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    return complete(s, messages, max_tokens=2000, temperature=TEMP_ANALYSIS)


def review_verdict(report: str) -> str:
    """从审校报告里读【总评】，返回 "pass" / "fix" / "unknown"。"""
    m = re.search(r"【总评】\s*([^\n]*)", report or "")
    line = m.group(1) if m else (report or "")
    if "需修改" in line or "不合格" in line:
        return "fix"
    if "合格" in line:
        return "pass"
    return "unknown"


def build_review_fix_messages(p: Project, body: str, report: str) -> list[dict]:
    """按审校意见重写一段正文。"""
    blocks: list[str] = []
    if p.premise.strip():
        blocks.append("【作品设定】（最高优先级，须严格遵守）\n" + p.premise.strip()[:1200])
    blocks.append("【待修改的正文】\n" + body.strip())
    blocks.append("【编辑的审校意见】\n" + report.strip())
    blocks.append(
        "请按审校意见重写上面这段正文：保留原有情节、信息与大致篇幅，"
        "只修正被指出的问题（套话、比喻堆砌、没有推进等）。"
        "直接输出重写后的正文，不要解释、不要复述意见。"
    )
    return [
        {"role": "system", "content": REWRITE_PROMPT},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def review_and_fix(
    p: Project,
    body: str,
    max_fix_rounds: int = 2,
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """审校一段正文，不合格就按意见重写，最多重写 max_fix_rounds 轮。

    返回 (最终正文, 最终审校报告)。用于一键生成的「每轮自动审校」——
    早发现早重写，避免写歪一大段才发现。
    """
    report = review_chapter(p, body, on_step)
    fixed = 0
    while review_verdict(report) == "fix" and fixed < max_fix_rounds:
        fixed += 1
        if on_step:
            on_step(f"审校未过，正按意见重写（第 {fixed}/{max_fix_rounds} 次）…")
        msgs = build_review_fix_messages(p, body, report)
        body = complete(
            p.settings, msgs,
            max_tokens=max(2000, p.settings.max_tokens),
            temperature=p.settings.temperature,
        )
        report = review_chapter(p, body, on_step)
    return body, report


def context_usage(p: Project) -> float:
    """当前章正文占上下文预算的比例，用于界面上的状态提示。"""
    budget = max(1, p.settings.context_budget)
    return len(p.chapter.body) / budget


def needs_renewal(p: Project) -> bool:
    """正文是否已超出上下文预算，需要把旧内容压缩成记忆。"""
    return len(p.chapter.body) > int(p.settings.context_budget)


def renew_memory(p: Project, on_step: Callable[[str], None] | None = None) -> str:
    """把超出预算的旧正文交给模型压缩，追加进跨会话记忆。

    这就是「自动续借新会话」：记忆取代原文，上下文永远装得下。
    """
    s = p.settings
    body = p.chapter.body
    keep = s.context_budget
    overflow = body[:-keep] if len(body) > keep else ""
    if len(overflow) < 400:
        return p.memory

    if on_step:
        on_step("正在压缩前情…")

    prior = f"已有记忆：\n{p.memory.strip()}\n\n" if p.memory.strip() else ""
    messages = [
        {
            "role": "system",
            "content": (
                "你是小说编辑。把给定的正文压缩成前情提要，供后续续写使用。\n"
                "必须保留：人物姓名与关系、已发生的关键事件、未回收的伏笔、场景与时间线。\n"
                "丢弃：修辞、对话原文、环境描写。\n"
                "输出 150-300 字的连续段落，中文，不要分点、不要标题。"
            ),
        },
        {"role": "user", "content": f"{prior}待压缩的正文：\n{overflow}"},
    ]
    summary = complete(s, messages, max_tokens=700)
    p.memory = (p.memory.strip() + "\n" + summary).strip() if p.memory.strip() else summary
    return p.memory


def summarize_chapter(p: Project, body: str, on_step: Callable[[str], None] | None = None) -> str:
    """把一整章正文压缩成前情提要，追加进跨会话记忆。

    与 renew_memory 的区别：renew_memory 处理「当前章溢出的尾部」，
    这里处理「已经写完、即将翻页的整章」。一键生成翻章时调用，
    保证写过的每一章都进入记忆，下次打开不会断片。
    """
    s = p.settings
    body = body.strip()
    if len(body) < 400:
        return p.memory

    if on_step:
        on_step("正在把已完成章节压缩进前情记忆…")

    prior = f"已有记忆：\n{p.memory.strip()}\n\n" if p.memory.strip() else ""
    messages = [
        {
            "role": "system",
            "content": (
                "你是小说编辑。把给定的正文压缩成前情提要，供后续续写使用。\n"
                "必须保留：人物姓名与关系、已发生的关键事件、未回收的伏笔、场景与时间线。\n"
                "丢弃：修辞、对话原文、环境描写。\n"
                "输出 150-300 字的连续段落，中文，不要分点、不要标题。"
            ),
        },
        {"role": "user", "content": f"{prior}待压缩的正文：\n{body}"},
    ]
    summary = complete(s, messages, max_tokens=700)
    p.memory = (p.memory.strip() + "\n" + summary).strip() if p.memory.strip() else summary
    return p.memory
