# -*- coding: utf-8 -*-
"""bot-mindscape · 单文件整合插件（由 scripts/build_plugin.py 生成，请勿直接编辑）

源码: plugins/    重新生成: python scripts/build_plugin.py
"""

import base64
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import struct
import time
import urllib.request
from astrbot.api import llm_tool, logger
from astrbot.api import llm_tool, logger, star
from astrbot.api import logger
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.filter.event_message_type import EventMessageType


class IndexLock:
    """最简跨进程锁：串行化「读索引 → 合并 → 写索引」。

    采集插件与导入脚本会同时碰索引，没有锁的话后写的会覆盖先写的。
    拿不到锁就抛错，绝不带旧快照往下写。
    """

    def __init__(self, path, timeout=5.0):
        self.lock = path + ".lock"
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        import time
        t0 = time.time()
        while True:
            try:
                self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() - t0 > self.timeout:
                    raise RuntimeError("索引被另一个进程占用: " + self.lock)
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass
        try:
            os.remove(self.lock)
        except Exception:
            pass
        return False
DEFAULT_PROMPT = (
    "看这张图，判断它适不适合当一个「{persona}」用的表情包。\n\n"
    "判断标准：\n"
    "1. 图里是动漫/二次元风格的人物表情（开心、无语、得意、委屈、鄙夷等）→ related = true\n"
    "2. 如果是风景照、聊天截图、真人照片、纯文字图 → related = false\n\n"
    "⚠️ 不要纠结这个角色出自哪个作品，只看视觉特征和使用场景。\n"
    "⚠️ 下面方括号里是格式示例，必须根据实际图片填写真实内容，绝对不要照抄。\n\n"
    "只返回一行 JSON，不要解释、不要代码块：\n"
    '{"related": true, "name": "<起个中文短名>", "tags": ["<3-5个中文标签>"], "desc": "<一句话说明什么场景用>"}'
)


PLACEHOLDERS = {
    "四字以内中文名", "四字中文名", "中文名", "name",
    "3到5个中文标签", "3-5个中文标签", "标签", "tags",
    "一句话说明适合什么场景", "一句话描述", "一句话说明", "desc",
}


IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


ALL_TOKENS = ("all", "*", "全部", "所有")


def scope_list(raw):
    """把配置里的 targets 规范成字符串列表。

    跳过 None / 空串 / 纯空白 —— `str(None)` 会变成字面量 "None" 混进列表，
    那样「这个 bot 号在不在作用域里」的判断会被一个假目标污染。
    """
    out = []
    for x in (raw or []):
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def scope_hit(targets, self_id):
    """这个 bot 是否在 targets 的作用域内。

    空列表仍返回 True（旧语义，见上面的注释），但**加载时应当用 scope_warn() 喊一声** ——
    静默全开是跨 bot 事故的高发地。
    """
    t = scope_list(targets)
    if not t:
        return True
    if any(x.lower() in ALL_TOKENS for x in t):
        return True
    return str(self_id) in t


def scope_warn(logger, name, targets, enabled=True):
    """enabled 但 targets 为空 → 明确警告「这会作用于全部 bot」。"""
    if enabled and not scope_list(targets):
        logger.warning(
            "[%s] enabled=True 但 targets 为空：按旧语义这会作用于**全部 bot**。"
            "要么列出 bot 号，要么显式写 [\"all\"] —— 别让空列表替你决定。", name)


def abs_path(path, base_dir):
    """把相对路径解析为绝对路径（相对于配置目录）。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir or ".", path)


def verdict_ok(v):
    """校验视觉判定结果是否有效（防模型照抄 prompt 示例）。"""
    if not isinstance(v, dict):
        return False
    name = str(v.get("name") or "").strip()
    desc = str(v.get("desc") or "").strip()
    tags = [str(x).strip() for x in (v.get("tags") or [])]
    if not name or name in PLACEHOLDERS:
        return False
    if name.startswith("<") or name.startswith("（"):
        return False
    if not desc or desc in PLACEHOLDERS:
        return False
    if not tags or any(x in PLACEHOLDERS for x in tags):
        return False
    return True


def parse_verdict(txt):
    """从模型输出里解析出 JSON 判定（含代码块和解释文字的兜底）。"""
    if not txt:
        return None
    bt = chr(96) * 3
    t = txt.replace(bt + "json", "").replace(bt, "").strip()

    def _as_dict(obj):
        # 模型偶尔返回 JSON 字符串/数组而不是对象，直接 .get() 会崩
        return obj if isinstance(obj, dict) else None

    try:
        got = _as_dict(json.loads(t))
        if got is not None:
            return got
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\"related\"[^{}]*\}", t)
    if m:
        try:
            got = _as_dict(json.loads(m.group(0)))
            if got is not None:
                return got
        except Exception:
            pass
    return None


def match_score(item, query):
    """按名字/标签/描述给候选图打分。"""
    hay = " ".join([
        str(item.get("name", "")),
        " ".join(str(x) for x in (item.get("tags") or [])),
        str(item.get("desc", "")),
    ]).lower()
    q = (query or "").lower()
    if not q:
        return 0.5
    if q in hay:
        return 100.0
    return float(sum(1 for ch in q if ch in hay))


def safe_name(name, allow_subdir=False):
    """校验索引里的文件名，拒绝路径穿越 / 盘符 / 绝对路径。

    返回规范化后的安全文件名；不合法返回 ""。
    """
    n = str(name or "").strip()
    if not n:
        return ""
    if "\x00" in n:
        return ""
    # 盘符 / UNC / 绝对路径
    if re.match(r"^[A-Za-z]:", n) or n.startswith("\\\\") or n.startswith("/"):
        return ""
    # 分隔符（本项目图库是扁平结构）
    if not allow_subdir and ("/" in n or "\\" in n):
        return ""
    if ".." in n.split("/") or ".." in n.split("\\"):
        return ""
    base = os.path.basename(n)
    if base != n.replace("\\", "/").split("/")[-1] and not allow_subdir:
        return ""
    return base


def is_inside(child, parent):
    """判断 child 是否位于 parent 目录内（解析符号链接后）。"""
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return os.path.commonpath([c, p]) == p
    except Exception:
        return False


def load_index(path):
    """读图库索引。文件损坏时返回 None（区别于合法的空列表）。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else None
    except Exception:
        return None


def save_index(path, idx):
    """原子写索引（先写临时文件再替换），避免读者读到半份 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


try:                                   # 插件内：日志必须从框架走（插件市场规范）
    from astrbot.api import logger
except Exception:                      # 命令行脚本：没有框架，跳过日志
    logger = None


_CONFIG_CACHE = None


PLUGIN_NAME = "astrbot_plugin_mindscape"


def data_dir():
    """插件数据目录（插件市场的规范位置）。

    在 AstrBot 里运行时用 StarTools 拿 data/plugin_data/<插件名>/；
    命令行脚本里没有框架，回退到仓库的 config/ 目录。
    """
    try:
        from astrbot.api.star import StarTools
        return str(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(here, "config")


def config_path():
    return os.environ.get("MINDSCAPE_CONFIG") or os.path.join(data_dir(), "config.yaml")


def load(reload=False):
    """读配置（带缓存）。任何异常都返回 {}，绝不因此崩掉 bot。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE
    path = config_path()
    data = {}
    if os.path.exists(path):
        try:
            import yaml  # type: ignore
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            # S03: 至少留一条线索，方便定位「配置为什么没生效」
            # 只记路径 + 异常类型，不输出可能含密钥的 YAML 内容
            if logger is not None:
                logger.warning("配置读取失败 path=%s type=%s", path, type(e).__name__)
            data = {}
    _CONFIG_CACHE = data if isinstance(data, dict) else {}
    return _CONFIG_CACHE


def section(name, default=None):
    v = load().get(name)
    return v if isinstance(v, dict) else (default or {})


def bot_entries():
    """返回 memory 段里配置的 bot 列表。"""
    mem = section("memory")
    bots = mem.get("bots")
    return bots if isinstance(bots, list) else []


DEFAULT_BUFFER = "/opt/astrbot/data/group_ctx_buffer.jsonl"


DEFAULT_COUNT = 15          # 注入最近多少条


DEFAULT_WINDOW = 30 * 60    # 只取 30 分钟内的（太旧的不算上下文）


DEFAULT_TAIL = 512 * 1024   # 只读文件尾部这么多字节（够 800 行，即使每行接近上限长度）


GC_KEEP_LINES = 800         # 再从中取最后这么多行（与整读的旧实现等价）


GC_PRIORITY = 1             # 先于记忆注入：这条消息是「上下文」，记忆是「背景」


def gc_buffer_path(conf):
    """缓冲文件路径。相对路径按【配置文件所在目录】解析（全项目一致的规矩）。"""
    raw = str(conf.get("buffer") or "").strip()
    if not raw:
        return DEFAULT_BUFFER
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(os.path.dirname(cfg.config_path()), raw))


def gc_tail_lines(path, tail_bytes, keep):
    """只读文件尾部的若干行。

    缓冲是追加流（写端 2MB 自截断），整读一遍纯属浪费 —— 实测 1.4MB / 7389 行，
    而 30 分钟窗口 + 只取 15 条根本用不到那么多。
    """
    with open(path, "rb") as fp:
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        start = max(0, size - tail_bytes)
        fp.seek(start)
        data = fp.read()
    lines = data.decode("utf-8", "ignore").split(chr(10))
    if start > 0:
        lines = lines[1:]        # 首行大概率被截断，丢掉
    return [ln for ln in lines if ln.strip()][-keep:]


def gc_read_recent(path, platform, group, limit, window_sec, tail_bytes):
    """从缓冲文件读该群最近的对话（窗口过滤 + 只留最后 limit 条）。"""
    if not path or not os.path.exists(path):
        return []
    try:
        lines = gc_tail_lines(path, tail_bytes, GC_KEEP_LINES)
    except Exception:
        return []
    now = time.time()
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if str(rec.get("platform")) != platform:
            continue
        if str(rec.get("group")) != str(group):
            continue
        if now - float(rec.get("ts") or 0) > window_sec:
            continue
        out.append(rec)
    return out[-limit:]


def gc_head(event):
    """本条消息的定向性 —— 四种情形各自一句。"""
    msgs = event.get_messages() or []
    me = str(event.get_self_id())
    at_self = any(type(c).__name__ == "At"
                  and str(getattr(c, "qq", "")) == me for c in msgs)
    reply_self = any(type(c).__name__ == "Reply"
                     and str(getattr(c, "sender_id", "")) == me for c in msgs)
    reason = event.get_extra("wake_reason")
    if at_self:
        return "本条消息【@ 了你本人】—— 它就是对你说的。"
    if reply_self:
        return "本条消息【引用了你说过的话】—— 它是接着你的话说的。"
    if reason == "mention":
        return "本条消息【没有 @ 你，但提到了你的名字】—— 大概率是在说你，可以应。"
    return ("本条消息【既没有 @ 你，也没有提到你的名字】—— 它多半是群友之间的对话，"
            "不是对你说的。可以接一句轻量的补充，但不是必须；不要把它当成在问你，"
            "也不要替别人回答。")


BL_PRIORITY = 999


def bl_load_config():
    c = cfg.section("blocklist") or {}
    on = bool(c.get("enabled"))
    table = {}
    for t in (c.get("targets") or []):
        sid = str(t.get("self_id") or "")
        users = {str(u).strip() for u in (t.get("users") or []) if str(u).strip()}
        if sid and users:
            table[sid] = users
    return on, table


DEFAULT_PATTERNS = [
    "LLM 响应错误",
    "All chat models failed",
    "APITimeoutError",
    "APIConnectionError",
    "APIError",
    "RateLimitError",
    "Request timed out",
    "Request timeout",
    "response error",
    "Internal Server Error",
    "Bad Gateway",
    "Service Unavailable",
    "Connection error",
    "Traceback (most recent call last)",
    "openai.",
    "httpx.",
]


DEFAULT_REGEX = r"(?:[A-Za-z_]*Error|[A-Za-z_]*Exception|Timeout|Failed)\s*[:：]"


SCAN_LEN = 200


def _load_config():
    """从共享配置读取拦截规则（读不到就用默认值）。"""
    path = cfg.config_path()   # 统一走共享配置（数据目录由 mindscape_config 决定）
    if not os.path.exists(path):
        return DEFAULT_PATTERNS, DEFAULT_REGEX
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            conf = yaml.safe_load(f) or {}   # 注意别叫 cfg —— 会和模块级的配置命名空间撞名
        g = (conf.get("guard") or {})
        # 用户自定义模式是「追加」而不是「替换」：
        # 否则配置里只写几条，反而会比内置默认拦得更少（真实踩过）。
        extra = [str(p) for p in (g.get("patterns") or []) if str(p).strip()]
        merged = list(DEFAULT_PATTERNS)
        for p in extra:
            if p not in merged:
                merged.append(p)
        # 只有显式给出 regex 才覆盖内置正则（空字符串视为用默认）
        rx = str(g.get("regex") or "").strip() or DEFAULT_REGEX
        return merged, rx
    except Exception as e:
        logger.warning("[mindscape_guard] 配置读取失败，用默认值: %s", str(e)[:100])
        return DEFAULT_PATTERNS, DEFAULT_REGEX


_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"(?i)(api[_-]?key|token|authorization)[=\":\s]+[A-Za-z0-9._\-]{6,}"), r"\1=***"),
    (re.compile(r"https?://[^\s\"']+[?&](key|token|rkey|sign)=[^\s\"'&]+"), "<url-with-secret>"),
]


def redact(text, limit=100):
    """给日志用的摘要：截断 + 抹掉疑似密钥。"""
    t = (text or "").replace(chr(10), " ").replace(chr(13), " ")
    for rx, rep in _SECRET_PATTERNS:
        t = rx.sub(rep, t)
    return t[:limit]


def is_error_text(text, patterns=None, regex=None):
    """判断这段文字是不是框架报错（而不是 bot 的正常回复）。"""
    t = (text or "").strip()
    if not t:
        return False
    head = t[:SCAN_LEN]
    for p in (patterns or DEFAULT_PATTERNS):
        if p and p in head:
            return True
    try:
        if re.search(regex or DEFAULT_REGEX, head):
            return True
    except re.error:
        pass
    return False


SI_DEFAULT_TOKEN = "[[silence]]"


SI_PROMPT = """# 沉默的权利
你**真的可以不说话**。这一轮如果你没有任何想说的 —— 不想接、跟你无关、或者就是懒得开口 ——
就**只输出这一行**：

%(token)s

系统会把整条消息丢掉，群里没有任何动静，谁也不会看见这几个字。这是真的沉默，不是假装。

- 只输出它，不要加解释、标点、括号、前后缀，也不要和别的话写在一起
- 不要用「（和我无关，安静飘过）」「（默默看着）」这类话代替它 —— 那是假装沉默，等于还是说了话
- 有话说的时候正常说。这是给你留的退路，不是让你变闷
"""


SI_CRON_NOTE = """# 沉默
这一轮是自主冒泡：**不想说话就什么都别发** —— 不要调用 send_message_to_user，直接结束就行。
（沉默令牌只在「有人跟你说话」的那些轮次里用，这一轮不要用它。）
"""


_SI_INVIS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"


_SI_WRAP = "`*_[]【】<>《》（）()" + "“”‘’" + chr(34) + chr(39)


_SI_TAIL = "。.!！?？~～…、,，:：;；"


_SI_OOC_RE = re.compile(
    r'(?:(?:这句我拿不准)?(?:我)?(?:先|就|继续)?(?:安静|默默|悄悄|静静)?地?'
    r'(?:飘过|路过)(?:不冒头|不插话|不打扰|没接(?:这句)?|不接(?:这句)?)?'
    r'|(?:我)?(?:这次|这句|这条)?(?:不冒头|不插话|不接这句|保持沉默|保持安静|不回复))'
    r'(?:了|啦|吧|呢)?')


def si_is_ooc_silence(text):
    """整条回复只是「安静飘过」这类动作描写 —— 等价于想沉默，但用错了表达。"""
    t = re.sub(r"[（）()\\[\\]【】《》\s]", "", text or "").strip()
    if not t:
        return False
    return bool(_SI_OOC_RE.fullmatch(t)) or t.upper() == "NO_REPLY"


def si_norm(text):
    """归一化成可比较的形式（先剃零宽字符，再剃空白 / 包裹符号 / 结尾标点）。"""
    t = (text or "").translate({ord(c): None for c in _SI_INVIS})
    t = t.strip()
    t = t.strip(_SI_WRAP)
    t = t.strip(_SI_TAIL)
    return t.strip(_SI_WRAP).lower()


def si_is_silence(text, token=SI_DEFAULT_TOKEN):
    """整条回复就是令牌 = 这一轮真的不想说话。"""
    key = si_norm(token)
    return bool(key) and si_norm(text) == key


def si_strip(text, token=SI_DEFAULT_TOKEN):
    """把混在正文里的令牌剃掉 —— 绝不让它出现在群里。"""
    if not text or not token:
        return text
    return re.sub(re.escape(token), "", text, flags=re.IGNORECASE).strip()


def si_load_config():
    """从共享配置读设置（读不到就用默认值）。"""
    c = cfg.section("silence")
    token = str(c.get("token") or "").strip() or SI_DEFAULT_TOKEN
    targets = [str(x) for x in (c.get("targets") or [])]
    prompt = str(c.get("prompt") or "").strip() or (SI_PROMPT % {"token": token})
    return bool(c.get("enabled", False)), token, targets, prompt


VS_MARK = "这一轮的消息里带了图"


VS_HINT = """## 这一轮的消息里带了图 —— 认人之前先查

- 图里的人物 / 作品，**先用 lookup_knowledge 查，查完还不确定就联网搜**；
  不要凭印象直接认。
- **查完还是不确定**是谁，就用你自己的口吻糊过去 —— 大意是「画面太糊、看不清」，
  **具体怎么说按你平常的说话方式来，别照抄这句**。
  糊弄 + 老实承认看不清，永远好过瞎编一个名字。
- 没有依据之前，**绝不说**「这就是 XX」。认错一个人，比说不认识难看得多。
"""


def vs_load_config():
    c = cfg.section("vision") or {}
    on = bool(c.get("enabled"))
    targets = [str(x) for x in (c.get("targets") or [])]
    return on, targets


def vs_has_image(event):
    """这一轮的消息里有没有图（只看顶层组件）。"""
    comps = getattr(getattr(event, "message_obj", None), "message", None) or []
    for c in comps:
        if isinstance(c, Image):
            return True
    return False


DEFAULT_MAX_CHARS = 2500


DEFAULT_MIN_CHARS = 50


DEFAULT_PEOPLE_CHARS = 800


DEFAULT_DIGEST_CHARS = 1200


DEFAULT_NOTES_CHARS = 800


SECTION_TITLE = "## 你的长期记忆"


SECTION_DIGEST = "### 你还记得的最近几天（每天一句）"


SECTION_NOTES = "### 你记下的账（自己用 save_note 维护的，比流水账可靠）"


SECTION_STYLE = "### 你的说话习惯（长期观察出来的，用来对齐语气）"


SECTION_STYLE_STABLE = "#### 长期稳定的部分"


SECTION_STYLE_RECENT = "#### 最近的变化"


DEFAULT_STYLE_CHARS = 800


DEFAULT_STYLE_RECENT_CHARS = 400


STYLE_GUARD = (
    "**这两段都是「你说话的方式」，不是记忆、也不是事实。**\n"
    "- 别宣告它们（不要说「我平时喜欢用『沃』」），直接用出来就行。\n"
    "- 两段可能重复 —— 那是同一件事，不是两件，别当成两个特征。\n"
    "- 两段对不上时以「最近的变化」为准（说话习惯本来就在变）。\n"
    "- 别把它们当往事提起 —— 那是记忆的事，不归这里管。\n"
)


SECTION_RULES = "**你自己的规矩**"


HEADER_MARK = "## "


def _tail_lines(text, budget):
    """块本身超预算时，从尾部按行取到预算内（不切半行）。"""
    if budget <= 0:
        return ""
    out = []
    used = 0
    for ln in reversed(text.splitlines()):
        add = len(ln) + (1 if out else 0)
        if used + add > budget:
            break
        out.insert(0, ln)
        used += add
    return "\n".join(out)


def read_recent(path, max_chars):
    """取最近的记忆，严格不超过 max_chars（从最新条目向前累计）。

    做法：从文件尾部往前扫，按「条目 / 标题」为单位累加，
    一旦加入下一段会超预算就停 —— 保证返回长度是硬上限，且不切半句话。
    """
    if not path or not os.path.exists(path) or max_chars <= 0:
        return ""
    size = os.path.getsize(path)
    # 预算的 4 倍足够容纳多字节字符；再多读一点保证能拿到完整条目
    read_from = max(0, size - max_chars * 6)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if read_from > 0:
            f.seek(read_from)
            f.readline()                       # 丢掉被截断的半行
        tail = f.read()

    lines = tail.splitlines()
    # 从后往前，以「条目块」为单位累加（块 = 连续的非空行，遇到 ## 标题另起一块）
    blocks = []
    cur = []
    for ln in reversed(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith(HEADER_MARK):
            if cur:
                blocks.append(list(reversed(cur)))
                cur = []
            blocks.append([ln])
        else:
            cur.append(ln)
    if cur:
        blocks.append(list(reversed(cur)))

    picked = []
    used = 0
    for blk in blocks:
        text = "\n".join(blk).strip()
        if not text:
            continue
        add = len(text) + (1 if picked else 0)
        if used + add > max_chars:
            if not picked:
                # 最新一块自己就超预算时，「整块丢弃」会让记忆变成全空 —— 实测中
                # 一份 2385 字的成长记录配上 2000 字预算就是这个结果（返回 0 字，
                # bot 表现为「完全不记得任何人」）。退一步：取这一块的尾部（较新
                # 的部分），按行截断，宁可少记也不能全忘。
                keep = _tail_lines(text, max_chars)
                if keep:
                    picked.append(keep)
            break
        picked.insert(0, text)
        used += add
    return "\n".join(picked)


def read_head(path, max_chars):
    """从头读固定字数 —— **文档型**文件用这个。

    read_recent 取的是**尾部**，那是给「追加式」文件（日记、账本）设计的：
    越新的越该进 prompt。但稳定层是每次**覆盖写**的一份完整文档，
    取尾部等于把开头的「口癖」整段切掉、只留后半截 —— 正好切掉最有价值的部分。
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text.strip()
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut.strip()


def _clean_style(text):
    """去掉生成器留在文件头的 HTML 注释。

    那是**给人看的**（「每次覆盖写，请勿手改」），进 prompt 只是噪声。
    用纯行过滤而不是 re：这里只需要跳过以 <!-- 开头的行。
    """
    out = [ln for ln in (text or "").splitlines()
           if not ln.strip().startswith("<!--")]
    return "\n".join(out).strip()


def _resolve(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def read_people(path, max_chars):
    """读人物画像文件，只保留条目行（跳过标题和更新时间）。"""
    if not path or not os.path.exists(path):
        return ""
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                if line.startswith("- "):
                    lines.append(line)
    except Exception:
        return ""
    out = "\n".join(lines)
    # ponytail: 这里按从头截断，而画像文件是按昵称排序的 —— 一旦文件超过
    # max_chars，排序靠后的群友会整批消失（实测：2175 字的画像配 800 字预算，
    # 正好把某位重要的人切掉，bot 于是完全不认得这个人）。当前对策是把
    # people_chars 配足装下整份文件；画像再长大时应改为按「最近出现」挑选条目，
    # 而不是按字母序切。
    return out[:max_chars]


def read_many(paths, max_chars):
    """按顺序读多个记忆文件，总量硬上限 max_chars（各文件先平分预算）。

    用途：一个 bot 的记忆可能分散在多份文件里 —— 例如日常记的日记，
    外加一份人格/成长档案。两份都要进 prompt，但总量不能失控。
    """
    paths = [p for p in paths if p]
    if not paths or max_chars <= 0:
        return ""
    share = max(200, int(max_chars / len(paths)))
    parts = []
    used = 0
    for p in paths:
        seg = read_recent(p, share).strip()
        if not seg:
            continue
        add = len(seg) + (1 if parts else 0)
        if used + add > max_chars:
            break
        parts.append(seg)
        used += add
    return "\n".join(parts)


DEFAULT_LIMIT = 15


FULL_CAP = 80          # full 模式的上限：要数数就得多给，但也不能把 prompt 撑爆


SCAN_LINE_CAP = 200000


def _bot_entries():
    return cfg.bot_entries()


def _diary_for(self_id):
    for b in _bot_entries():
        if str(b.get("self_id", "")) == str(self_id):
            p = b.get("diary") or ""
            if p and not os.path.isabs(p):
                p = os.path.join(os.path.dirname(cfg.config_path()), p)
            return p
    return ""


STOP_CHARS = set("的了是不在有和与就都也很才没我你他她它们这那什么怎么")


def score_line(text, kw, min_ratio=0.6, min_chars=2):
    """给一行文字和查询词打分（混合检索的轻量实现）。

    策略：
      1. 完整子串命中 -> 100 分（最可靠）
      2. 去掉虚词后按单字命中比例给分（最高 60 分）
         「爬楼」能捞到「爬到对面6楼」，但「完全不存在」不会命中所有行
      3. 连续两字命中额外加权
    不引入向量模型，零额外内存占用。
    """
    t = (text or "").lower()
    q = (kw or "").strip().lower()
    if not q or not t:
        return 0.0
    if q in t:
        return 100.0
    if not any(c.isalpha() for c in q):
        # 纯数字/符号的词（日期、编号）只认精确命中，不做逐字模糊 ——
        # 「09-16」曾因为 0 / 9 / - 三个字符就凑够 0.6 的命中率，
        # 把整本日记都算成命中，报出「共命中 7478 条」这种假数字。
        return 0.0
    chars = [c for c in q if not c.isspace() and c not in STOP_CHARS]
    if len(chars) < min_chars:
        return 0.0
    hit = sum(1 for c in chars if c in t)
    ratio = hit / len(chars)
    if ratio < min_ratio:          # 命中率不够，直接判为不相关
        return 0.0
    base = ratio * 60.0
    bonus = 0.0
    for i in range(len(chars) - 1):
        if chars[i] + chars[i + 1] in t:
            bonus += 5.0
    return base + bonus


def split_terms(keyword):
    """把查询词拆成一组近义词：空格、逗号、顿号、斜杠都算分隔。"""
    return [x.strip() for x in re.split(r"[\s,，、/|]+", keyword or "") if x.strip()]


_DATE_TOKEN = re.compile(r"(?:(\d{4})\s*[-/年]\s*)?(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*号?")


def date_key(s):
    """从文本里抠出 (年, 月, 日)。**年可能是 None**（只写了「9月16号」）。认不出返回 None。

    取第一个匹配 —— 日记的日期写在条目头上（## 2026-09-16 23:57），
    正文里提到别的日期不该盖过它（调用方优先拿 head）。
    """
    for m in _DATE_TOKEN.finditer(s or ""):
        y, mo, dy = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            return (int(y) if y else None, mo, dy)
    return None


def date_filter(term):
    """这个查询词是不是一个「日期」？是就返回 (年,月,日)（**年可为 None**），否则 None。"""
    t = (term or "").strip()
    if not t or len(t) > 12:
        return None
    if not _DATE_TOKEN.fullmatch(t):
        return None
    return date_key(t)


def date_match(line_date, want):
    """条目日期是否命中查询日期。

    月日必须一致；**只有查询里写了年份时才比年份** ——
    否则「2026-09-16」会把 2025-09-16 一起捞进来（年份被丢掉的老 bug）。
    条目年份认不出来时不否决，避免误杀。
    """
    if not line_date or not want:
        return False
    wy, wm, wd = want
    ly, lm, ld = line_date
    if (wm, wd) != (lm, ld):
        return False
    if wy is not None and ly is not None and wy != ly:
        return False
    return True


def search_diary(path, keyword, limit=DEFAULT_LIMIT, scan_lines=SCAN_LINE_CAP,
                 full=False):
    """在记忆中检索相关条目（混合检索：精确 + 模糊）。

    返回 (行列表, 真实命中总数)。

    为什么要单独返回总数：模型只看到「给了它几条」，就会把这几条当成全部。
    实测它只翻到最近的一条就下了结论，而同一个话题在日记里其实命中几十条。
    把总数单独告诉它，它才知道自己没看全。

    为什么要接受多个词：提问用的词往往不是记日记时用的词。实测同一件事换个
    说法，命中数量能差近十倍。所以关键词允许给一组近义词，命中任意一个都算。
    """
    if not path or not os.path.exists(path):
        return [], 0
    terms = split_terms(keyword)
    if not terms:
        return [], 0
    # 日期词当【过滤条件】，不当打分项。
    # 否则问「9-16」时，别的日子只要沾上同一个名字就会被一起捞回来，
    # 再按新旧排序 —— 结果就是「今天的记忆」把「那一天」挤出去。
    # 外部症状：她能想起很久以前的事，但把好几天混成一团。
    wants = [d for d in (date_filter(t) for t in terms) if d]
    words = [t for t in terms if date_filter(t) is None]
    scored = []
    head = ""
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            n += 1
            if n > scan_lines:
                break
            line = line.rstrip()
            if line.startswith("## "):
                head = line[3:].strip()
                continue
            if not line.startswith("-"):
                continue
            body = line.lstrip("- ").strip()
            text = head + " " + body
            if wants:
                # 日期以【条目头】为准：正文里提到别的日期不算
                ld = date_key(head) or date_key(body)
                if not any(date_match(ld, w) for w in wants):
                    continue
            if words:
                per = [score_line(text, t) for t in words]
                hit = sum(1 for s in per if s > 0)
                if not hit:
                    continue
                # 命中词数优先，其次才是单词语义分。
                # 旧实现取 max()：只沾 1 个词和沾满 5 个词同分，于是同分按行号倒序，
                # 最新的永远排最前，旧事全被挤到 80 条之外。
                sc = hit * 100.0 + (max(per) if per else 0.0)
            else:
                sc = 100.0
            scored.append((sc, n, "[%s] %s" % (head, body)))
    if not scored:
        return [], 0
    # 先按分数降序；同分时越新越靠前
    scored.sort(key=lambda x: (-x[0], -x[1]))
    total = len(scored)
    picked = scored[:FULL_CAP] if full else scored[:limit]
    return [s[2] for s in picked], total


def _first_str(args):
    for a in args or ():
        if isinstance(a, str) and a.strip():
            return a.strip()
    return ""


def format_hits(kw, hits, total, full):
    """组织给模型看的检索结果。

    唯一不能含糊的事：**给了几条 / 一共命中几条**。
    曾经这里写「以下是……共 %d 条」，模型就把这个数当成了全部 —— 它只拿到
    十几条，真实命中几十条，于是下了个偏小的结论。现在只要没给全就明说，
    连 full 模式被 FULL_CAP 截断时也要说清（曾经写成「全部 N 条」而只列了
    上限那么多条，等于自己又犯了同一个毛病）。
    """
    short = total > len(hits)
    tail_extra = ""
    if short and full:
        head = ("以下是记忆里与「%s」有关的记录（共命中 %d 条，"
                "这里按相关度列出前 %d 条）：" % (kw, total, len(hits)))
    elif short:
        head = ("以下是记忆里与「%s」有关的记录（共命中 %d 条，"
                "这里只给你最相关的 %d 条）：" % (kw, total, len(hits)))
    else:
        head = "以下是记忆里与「%s」有关的记录（共 %d 条，已全部列出）：" % (kw, total)
        if full:
            tail_extra = ("\n⚠️ **命中条数不等于个数** —— 同一件事会在好几天被反复记到，"
                          "回答「一共多少」时要自己归并，别把条数直接当答案。")
    tail = tail_extra + ("\n\n⚠️ 只依据上面的记录回答。记录里没提到的人或事，就说想不起来，"
                         "绝对不要凭印象补充细节。\n"
                         "⚠️ 记录里如果是「某人说……」，那只是**他讲过这句话**，"
                         "不等于事情成立 —— 别替它背书。用你自己的方式讲就行，"
                         "不用原样复述，也不必每次都点名是谁说的。")
    if short and not full:
        tail += ("\n⚠️ 上面不是全部（共命中 %d 条）。如果对方问的是数量、名单这类"
                 "要翻遍全部的，用 full=true 再查一次，否则一定漏。" % total)
    elif short:
        tail += ("\n⚠️ 还有 %d 条没列出来。另外：**命中条数不等于个数** ——"
                 "同一件事会在好几天被反复记到，回答「一共多少」时要自己归并，"
                 "别把条数直接当答案。" % (total - len(hits)))
    return head + "\n" + "\n".join(hits) + tail


def _as_bool(v):
    """模型给的布尔值可能是字符串（"true" / "是"），bool("false") 会是 True。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是", "对", "要")


@llm_tool(name="recall_memory")
async def recall_memory(*args, **kwargs):
    """翻自己的长期记忆，回忆过去发生过的事。

    你眼前只有**最近一小段**记忆，不是全部 —— 手头没有，不等于没发生过。
    所以当答案要「翻遍全部」才给得准（数量、名单、最值、有没有发生过）、
    当问题指的是更早的时间（以前、上次、第一次、这几天），或者你打算回答
    「只有」「就这些」「没有」的时候，都必须先用这个工具查一遍再开口。

    返回的是你当时记下的原话，可以自然地讲出来，别照本宣科念。

    Args:
        keyword(string): 搜索关键词。可以给**一组近义词**，用空格或逗号分开
            —— 你记日记时用的词，和对方问话时用的词经常不一样，多给几个才不会漏。
            要指定**日期**就直接写（2026-09-16 / 09-16 / 9月16号）：给了日期就只翻那一天。
            问「某天谁干了什么」时，**日期和人名一起给**，比只给日期准得多
        full(boolean): 数数/汇总时必须设为 true —— 默认只给最相关的十几条，
            数数一定漏；设为 true 会返回全部命中
    """
    kw = str(kwargs.get("keyword") or _first_str(args)).strip()
    if not kw:
        return "你想让我回忆谁或什么事？给个关键词。"
    # 框架会把插件实例绑到第一个位置参数（functools.partial），
    # 真正的 event 混在 args 里 —— 必须自己找出来，否则拿不到 self_id。
    ev = None
    for a in args:
        if hasattr(a, "get_self_id"):
            ev = a
            break
    try:
        sid = str(ev.get_self_id()) if ev is not None else ""
    except Exception:
        sid = ""
    path = _diary_for(sid)
    if not path:
        return "我还没有长期记忆文件。"
    full = _as_bool(kwargs.get("full"))
    hits, total = search_diary(path, kw, full=full)
    if not hits:
        # 找不到就明确说找不到 —— 这是防幻觉的第一道闸
        return ("翻了翻记忆，没有找到跟「%s」有关的记录。"
                "可以换个更接近你当时记法的词再查一次（人名、别称，"
                "或者那件事里的另一个说法）；如果还是没有，就直接说你想不起来了，"
                "不要编。") % kw
    return format_hits(kw, hits, total, full)


def rc_knowledge_for(self_id):
    """取这个 bot 配置的「查阅型文档」（战斗数据这类：平时不注入，问到才查）。"""
    for b in _bot_entries():
        if str(b.get("self_id", "")) != str(self_id):
            continue
        ks = b.get("knowledge")
        out = []
        if isinstance(ks, list):
            for k in ks:
                if not isinstance(k, dict):
                    continue
                p = str(k.get("path") or "")
                if p and not os.path.isabs(p):
                    p = os.path.join(os.path.dirname(cfg.config_path()), p)
                if p:
                    out.append({"name": str(k.get("name") or "资料"),
                                "desc": str(k.get("desc") or ""),
                                "path": p})
        return out
    return []


def rc_sections(text):
    """按二级标题切段（段头一起保留）。"""
    secs, cur = [], []
    for line in (text or "").splitlines():
        if line.startswith("## ") and cur:
            secs.append("\n".join(cur).strip())
            cur = [line]
        else:
            cur.append(line)
    if cur:
        secs.append("\n".join(cur).strip())
    return [s for s in secs if s]


def rc_search_sections(path, keyword, limit=3, max_chars=2000):
    """在结构化文档里按【段落】检索 —— 问「配队」就给整段，不是散落的几行。"""
    if not path or not os.path.exists(path):
        return [], 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            secs = rc_sections(f.read())
    except Exception:
        return [], 0
    terms = split_terms(keyword)
    if not terms:
        return [], len(secs)
    scored = []
    for s in secs:
        low = s.lower()
        hit = sum(1 for t in terms if t in low)
        if hit:
            scored.append((hit, -len(s), s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, used = [], 0
    for _, _, s in scored:
        if out and used + len(s) > max_chars:
            break
        out.append(s)
        used += len(s)
        if len(out) >= limit:
            break
    return out, len(secs)


@llm_tool(name="lookup_knowledge")
async def lookup_knowledge(*args, **kwargs):
    """查资料库：**战斗数据** + **人物关系**。

    【战斗类】只要对方问的是战斗问题，就必须先查这里再开口，**不要凭印象编**：
    能不能和谁组队、带什么光锥、遗器怎么配、主词条选什么、星魂提升大不大、
    某个模式的玩法、某个机制是怎么回事。

    【人物类】同样先查再开口：某个名字是谁、你跟他/她是什么关系、
    你管他/她叫什么、某个外号指的是谁、你跟他/她之间发生过什么。

    返回的是资料原文 —— 用你自己的口吻讲出来，别照本宣科念。
    资料里没有的，就直说不知道，**绝不要编**。

    Args:
        keyword(string): 查询关键词，可以给一组（空格或逗号分开）。
            例：配队 银狼 / 光锥 遗器 主词条 / 星魂 / 机制 笑点 / 开拓者 关系 / 旧型号
        which(string): 指定查哪一份资料的名字（「战斗数据」或「人物关系」），不填就全查
    """
    kw = str(kwargs.get("keyword") or _first_str(args)).strip()
    if not kw:
        return "你想查哪方面的？给个关键词（配队 / 光锥 / 遗器 / 星魂 / 机制，或者某个人是谁）。"
    ev = None
    for a in args:
        if hasattr(a, "get_self_id"):
            ev = a
            break
    try:
        sid = str(ev.get_self_id()) if ev is not None else ""
    except Exception:
        sid = ""
    docs = rc_knowledge_for(sid)
    if not docs:
        return "我这边没有配置任何资料库。"
    want = str(kwargs.get("which") or "").strip()
    blocks, names = [], []
    for d in docs:
        if want and want not in d["name"]:
            continue
        hits, total = rc_search_sections(d["path"], kw)
        if hits:
            names.append(d["name"])
            blocks.extend(hits)
    if not blocks:
        return ("查了资料库，没有跟「%s」直接相关的内容。"
                "换个更贴的说法再查一次；如果还是没有，就老实说这块你不清楚，"
                "不要凭印象编数值。") % kw
    head = "【资料原文 · %s】用你自己的口吻讲，别照念：\n\n" % "、".join(names)
    tail = "\n\n（以上是资料，讲的时候不要提「资料」「文档」这些词。）"
    return head + "\n\n".join(blocks) + tail


LINE_RE = re.compile(r"^\-\s*([^：:]{1,40})\s*[：:]\s*(.*)$")


def nt_abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else "."
    return os.path.join(base, path)


def notes_path(self_id):
    """按 self_id 找到这个 bot 的账本路径（没配就返回空）。"""
    for b in cfg.bot_entries():
        if str(b.get("self_id", "")) == str(self_id):
            return nt_abs(b.get("notes"))
    return ""


def parse_notes(text):
    """账本 -> [(键, 值)]，保持文件顺序。"""
    out = []
    for line in (text or "").splitlines():
        m = LINE_RE.match(line.strip())
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def upsert_note(text, key, value):
    """写入一条：同名的键就地替换并挪到末尾（末尾 = 最近改动），否则追加。

    挪到末尾是有意的：注入取的是「最近的 N 字」，放在末尾才能保证
    刚改过的条目一定进得去。
    """
    key = (key or "").strip()
    value = (value or "").strip()
    lines = [l for l in (text or "").splitlines()]
    head = [l for l in lines if not LINE_RE.match(l.strip())]
    items = [(k, v) for k, v in parse_notes(text) if k != key]
    items.append((key, value))
    body = ["- %s：%s" % (k, v) for k, v in items]
    out = [l for l in head if l.strip()]
    if out and not out[0].startswith("#"):
        out.insert(0, "# 账本")
    if not out:
        out = ["# 账本"]
    return "\n".join(out + body) + "\n"


def write_notes(path, text):
    """原子写（先 .tmp 再 replace），免得中途断了留下半本账。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _find_self_id(args):
    """框架会把插件实例绑到第一个位置参数，真正的 event 混在 args 里。"""
    for a in args or ():
        if hasattr(a, "get_self_id"):
            try:
                return str(a.get_self_id())
            except Exception:
                return ""
    return ""


@llm_tool(name="save_note")
async def save_note(*args, **kwargs):
    """把一件事记进你的账本 —— 以后问到细节，看的就是这本。

    什么时候该记：群里**当场定下来**的事 —— 谁跟谁约定了什么、谁认领了什么
    说法、你答应了谁什么、谁被大家质疑过。记完再回答，前后才不会打架。

    记的是「这件事怎么定下来的」，别只写结论：带上是谁说的、谁认的、
    有没有人质疑。这样以后翻出来，你分得清哪些是板上钉钉、哪些只是嘴上说说。

    Args:
        key(string): 这件事的名字，短一点好找（两三个字，比如「称呼」「名额」）
        value(string): 具体内容，带上来源，比如「甲 ↔ 乙（甲自己来挂的）」
    """
    key = str(kwargs.get("key") or "").strip()
    value = str(kwargs.get("value") or "").strip()
    if not key and not value:
        return "要记什么？给我一个名字和内容。"
    if not key:
        key = value[:12]
    sid = _find_self_id(args)
    path = notes_path(sid)
    if not path:
        return "我还没有账本文件，先让主人给我配一个。"
    try:
        old = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                old = f.read()
        write_notes(path, upsert_note(old, key, value))
        logger.info("[mindscape_notes] %s 记下 %s: %s", sid, key, value[:40])
        return "记下了：%s —— %s" % (key, value)
    except Exception as e:
        logger.warning("[mindscape_notes] 记账失败: %s", str(e)[:120])
        return "这本账我一时写不进去，先记在心里。"


DEFAULT_SEG_SYMBOLS = {
    "text": "{text}", "at": "@{qq}", "image": "[图]", "face": "[表情]",
    "reply": "[回复]", "video": "[视频]", "record": "[语音]",
    "json": "[卡片]", "file": "[文件]",
}


DEFAULT_PERSONA = (
    "你在翻阅自己所在群聊的记录，想在自己的成长日记里记下值得记的人和事。"
    "注意：只记「别人说了什么、发生了什么有趣的事、谁和谁怎么了」，"
    "绝对不要去分析、模仿或总结任何人的说话风格。"
    "用第一人称、短句、轻松的语气写，每条一两句话。"
    '只输出一个 JSON 对象，格式：'
    '{"diary":["条目1","条目2"], "people":{"昵称":"一句话描述"}}'
    "diary：每条一句话，最多6条，没有值得记的就输出空数组。"
    "people：本次记录里出现的、值得记住的人，值为一句话描述（身份/特征/和你的关系/近况）。"
    "这用于以后认人，只记事实，不要写评价。没有新人物就输出空对象。"
)


def dy_abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def seg_to_text(segs, symbols=None):
    """把消息段数组转成纯文本。"""
    sym = symbols or DEFAULT_SEG_SYMBOLS
    parts = []
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        t = s.get("type")
        dd = s.get("data") or {}
        tpl = sym.get(t)
        if tpl:
            try:
                parts.append(tpl.format(**dd))
            except Exception:
                parts.append(str(tpl))
        elif t:
            parts.append("[" + str(t) + "]")
    return "".join(parts)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {"since_ts": d.get("since_ts", 0), "since_seq": d.get("since_seq", 0)}
    except Exception:
        return {"since_ts": 0, "since_seq": 0}


def save_state(path, st):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def fetch(src, target, since_ts, since_seq, only_user=None):
    """从 SQLite 增量读取消息（表名/字段名来自配置）。

    only_user：只取这个 user_id 的消息（mindscape_learn 用它学某人的风格）。
    不传时保持原语义 —— 排除 self_id（日记只记别人）。
    """
    db = dy_abs(src.get("db"))
    if not db or not os.path.exists(db):
        return []
    table = src.get("table") or "messages"
    fields = src.get("fields") or {}
    f_time = fields.get("time") or "timestamp"
    f_seq = fields.get("seq") or "sequence"
    f_data = fields.get("data") or "data"
    where = src.get("where") or ""
    # 同时覆盖「更晚的时间」与「同一秒但序号更大」两种情况，
    # 否则在同一秒内保存进度后，该秒后续消息会永久漏读。
    sql = ("SELECT %s, %s, %s FROM %s WHERE (%s > ? OR (%s = ? AND %s > ?))"
           % (f_time, f_seq, f_data, table, f_time, f_time, f_seq))
    if where:
        sql += " AND (" + where + ")"
    sql += " ORDER BY %s, %s" % (f_time, f_seq)

    self_id = str(target.get("self_id") or "")
    groups = [str(g) for g in (target.get("groups") or [])]

    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    cur = con.cursor()
    rows = []
    try:
        for ts, seq, data in cur.execute(sql, (since_ts, since_ts, since_seq)):
            try:
                d = json.loads(data)
            except Exception:
                continue
            uid = str(d.get("user_id", ""))
            if only_user is not None:
                if uid != str(only_user):
                    continue
            elif uid and uid == self_id:
                continue
            gid = str(d.get("group_id", ""))
            if groups and gid not in groups:
                continue
            txt = seg_to_text(d.get("message"), src.get("symbols"))
            if not txt.strip():
                continue
            sender = (d.get("sender") or {}).get("card") or (d.get("sender") or {}).get("nickname") or uid
            rows.append({
                "ts": ts, "seq": seq,
                "gname": str(d.get("group_name", ""))[:20],
                "who": str(sender)[:16], "uid": uid,
                "time": datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "txt": txt[:200],
            })
    finally:
        con.close()
    return rows


def dy_render(msgs):
    """把一批消息渲染成发给模型的那段文本。

    单独抽出来，是因为**分批**和**发送**必须用同一套算法 ——
    两边不一致就会出现「按 5000 字分好批、发出去却是 6000 字被砍」。
    """
    return "群聊记录：\n" + "\n".join(
        "[" + m["time"] + "][" + m["gname"] + "] " + m["who"] + ": " + m["txt"]
        for m in msgs)


def chunk_by_budget(rows, batch, max_input_chars):
    """先按条数切、再按【真实渲染长度】细分，保证每条消息都进得了某一次请求。

    为什么要这么麻烦：以前是固定 batch 条一组，再在 call_llm 里把文本砍到
    max_input_chars —— 砍掉的那截尾巴没人知道，而游标照样推到 chunk[-1]，
    于是那几条消息**永久漏记**（把预算调小或把批次调大就能触发）。

    单条自己就超预算时**抛 ValueError**：明确失败、停在原游标，绝不静默截断。
    调用方接住它、打印、**不推进游标**。
    """
    out = []
    for i in range(0, len(rows), batch):
        cur = []
        for r in rows[i:i + batch]:
            trial = cur + [r]
            if cur and len(dy_render(trial)) > max_input_chars:
                out.append(cur)
                cur = [r]
            else:
                cur = trial
            if len(dy_render(cur)) > max_input_chars:
                raise ValueError(
                    "单条消息渲染后 %d 字 > max_input_chars=%d，装不进任何一批"
                    % (len(dy_render(cur)), max_input_chars))
        if cur:
            out.append(cur)
    return out


def call_llm(llm, persona, msgs, max_input_chars, max_tokens, relations=None,
             expect_key="diary"):
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("diary.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(dy_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")

    user = dy_render(msgs)
    if len(user) > max_input_chars:
        # 绝不再静默截断：截断 + 推进游标 = 被砍掉的那几条永久漏记。
        # 调用方应当先用 chunk_by_budget 分好批。
        raise RuntimeError(
            "本批渲染后 %d 字，超过 max_input_chars=%d —— 调用方必须先分批"
            % (len(user), max_input_chars))
    system = persona or DEFAULT_PERSONA
    if relations:
        # 没有这段，摘要会把「喜欢的人」写成「某个群友」—— 日记正文和人物画像
        # 都会跟着错，而这些关系本来是作者写死的。
        system += ("\n\n你已经知道的关系（描述必须与这里一致，绝不能写成陌生群友）：\n"
                   + "\n".join(relations))
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # 提炼类任务温度别太高；某些口径（如风格学习）需要更保守
        "temperature": float(llm.get("temperature", 0.7)),
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    try:
        parsed = json.loads(out["choices"][0]["message"]["content"])
    except Exception:
        # 解析失败 ≠ 没有值得记的事 —— 返回 None 让调用方中断并重试，
        # 否则这批消息会被标记为「已处理」，永久丢失。
        return None
    # expect_key：不同口径要的顶层键不一样（日记是 diary，风格学习是 observations）
    if not isinstance(parsed, dict) or expect_key not in parsed:
        return None
    entries = parsed.get(expect_key)
    if not isinstance(entries, list):
        return None
    return parsed


REL_TITLE = "## 对我来说重要的人（来自人格档案，自动整理不会覆盖）"


AUTO_TITLE = "## 群里遇到的人（自动整理）"


def load_relations(spec):
    """从人格档案里取「关系与称呼」这类段落，作为不会被自动覆盖的权威条目。

    为什么需要：人物画像是一句话自动摘要，它不知道谁是「喜欢的人」，
    只会把对方写成「群友」。一旦好友被降级成陌生人，bot 就会认错人 ——
    而这类关系是**作者写死的**，不该由摘要模型来猜。

    spec 可以是字符串（文件路径，取全文的 - 行），也可以是
    {file: ..., section: "关系与称呼"} —— 只取该 ## 段落里的 - 行。
    """
    if not spec:
        return []
    if isinstance(spec, str):
        spec = {"file": spec}
    path = dy_abs(spec.get("file"))
    if not path or not os.path.exists(path):
        return []
    section = str(spec.get("section") or "").strip()
    out = []
    inside = not section          # 没指定段落就取全文的 - 行
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip()
                if s.startswith("## "):
                    inside = (section in s) if section else True
                    continue
                if inside and s.startswith("- "):
                    out.append(s)
    except Exception:
        return []
    return out


def _relation_heads(relations):
    """取每条关系「→」之前的名字部分，用来判断摘要是否在讲同一个人。"""
    heads = []
    for r in relations or []:
        body = r[2:] if r.startswith("- ") else r
        heads.append(body.split("→", 1)[0].strip())
    return heads


def _update_people(path, people, now, relations=None):
    """把人物画像合并进 people.md（同名覆盖，保留最近时间）。

    relations 是来自人格档案的权威条目，单独放在文件开头；
    自动摘要如果提到了权威条目里的人（如「小爱」出现在
    「Alice（小爱）」中），**不允许**把它写成普通群友。
    """
    relations = [r for r in (relations or []) if str(r).strip()]
    heads = _relation_heads(relations)
    existing = {}
    try:
        with open(path, encoding="utf-8") as f:
            in_auto = True                    # 旧格式没有分段标题，按自动段处理
            for line in f:
                s = line.strip()
                if s.startswith("## "):
                    in_auto = s.startswith(AUTO_TITLE)
                    continue
                if s.startswith("#"):         # 一级标题不是分段
                    continue
                if in_auto and s.startswith("- ") and "：" in s:
                    k, v = s[2:].split("：", 1)
                    existing[k.strip()] = v.strip()
    except Exception:
        pass
    # 历史上被摘要降级过的条目（比如「某人：群友」）也要清掉 ——
    # 只挡新的不够，旧的那条会一直躺在文件里继续误导 bot。
    for k in [k for k in existing if any(k in h for h in heads)]:
        del existing[k]
    for k, v in people.items():
        key = str(k).strip()
        if not key or not str(v).strip():
            continue
        if any(key in h for h in heads):
            continue                          # 权威关系里的人，不让摘要顶掉
        existing[key] = str(v).strip()[:80]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 先写临时文件、再原子替换 —— 中途退出只会留下一个 .tmp，
    # 绝不会把画像**写坏成半份**（以前直接覆盖写，进程一死就只剩半截）。
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# 你认识的人\n\n")
            f.write("最后更新：" + now + "\n\n")
            if relations:
                f.write(REL_TITLE + "\n")
                for r in relations:
                    f.write(r + "\n")
                f.write("\n")
            f.write(AUTO_TITLE + "\n")
            for k in sorted(existing):
                f.write("- " + k + "：" + existing[k] + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # 同一目录内：原子
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def dy_has_batch(path, rng, tail_bytes=200000):
    """产物里是否已经写过这一批（看【文件尾巴】就够了）。

    重跑要补的总是最后那批 —— 崩溃发生在「写完日记、游标还没落盘」之间，
    所以只需在尾部找标记，不必扫整个文件。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return ("ms-seq:" + rng).encode("utf-8") in f.read()
    except Exception:
        return False


def run_target(d):
    """处理一个 bot 的日记，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 40)
    max_in = int(d.get("max_input_chars") or 14000)
    max_tok = int(d.get("max_tokens") or 900)
    # ponytail: fetch 不带 LIMIT，一次运行会把游标之后的全部积压跑完 ——
    # 首次指向一个几千条消息的群时会变成几百上千次 LLM 调用（烧钱且占满
    # 这台 2 核小机器）。用 max_batches 给单次运行封顶，剩下的留给下一次
    # cron 慢慢追。追历史变慢时才需要调大它。
    max_batches = int(d.get("max_batches") or 40)

    total_read = total_added = 0
    for target in (d.get("targets") or []):
        out_file = dy_abs(target.get("output"))
        state_file = dy_abs(target.get("state") or (out_file + ".state.json"))
        st = load_state(state_file)
        relations = load_relations(target.get("relations"))
        people_file = dy_abs(target.get("people") or (out_file.rsplit(".", 1)[0] + ".people.md"))
        rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0))
        if not rows:
            # 即使没新消息，也要保证权威关系已经落在画像里
            if relations:
                _update_people(people_file, {}, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), relations)
            continue
        total_read += len(rows)
        all_people = {}
        try:
            batches = chunk_by_budget(rows, batch, max_in)
        except ValueError as e:
            # 分不出合法的批：停在原游标，等主人调大 max_input_chars
            print("[mindscape_diary] 分批失败，本轮不动游标: %s" % str(e)[:140])
            continue
        done = 0
        for bi, chunk in enumerate(batches):
            if done >= max_batches:
                print("[mindscape_diary] 已达单次上限 %d 批，剩余 %d 批留待下次"
                      % (max_batches, len(batches) - bi))
                break
            try:
                res = call_llm(llm, target.get("persona"), chunk, max_in, max_tok, relations)
            except Exception as e:
                # 失败就停：只推进会成功的那部分，剩下的下次重试
                print("[mindscape_diary] LLM 失败，本批中断，剩余留待下次: %s" % str(e)[:120])
                break
            if res is None:
                # call_llm 返回 None 表示响应不可用（非合法空日记）
                print("[mindscape_diary] 响应不可用，本批中断")
                break
            entries = res.get("diary") or []
            os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            # 标题用这批消息自己的时间，而不是「运行时刻」—— 追历史时一次运行会
            # 写出几十批，用运行时刻就会出现几十个一模一样的 ## 标题。
            stamp = datetime.datetime.fromtimestamp(chunk[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
            # 这一批的来源游标范围，写成【独立一行】的 HTML 注释：
            #   - 渲染时看不见；检索只认 "## " 和 "- "，所以不会污染记忆文本
            #   - 万一「日记写完、游标还没落盘」就退出，重跑时靠它认出这批已写过，
            #     只推进游标、不再追加一遍（以前会整整重复一批）
            rng = "%s-%s" % (chunk[0]["seq"], chunk[-1]["seq"])
            if entries and not dy_has_batch(out_file, rng):
                with open(out_file, "a", encoding="utf-8") as fp:
                    fp.write("<!-- ms-seq:" + rng + " -->\n")
                    fp.write("## " + stamp + "\n")
                    for e in entries:
                        fp.write("- " + str(e) + "\n")
                    fp.write("\n")
                total_added += len(entries)
            elif entries:
                print("[mindscape_diary] 这批已写过（ms-seq:%s），只推进游标" % rng)
                total_added += 0
            # 人物画像先攒着，一轮结束时合并落盘一次即可
            people = res.get("people") or {}
            if isinstance(people, dict) and people:
                all_people.update(people)
            st["since_ts"] = chunk[-1]["ts"]
            st["since_seq"] = chunk[-1]["seq"]
            save_state(state_file, st)
            done += 1
        # 人物画像：权威关系 + 本轮摘要，合并后写一次
        if all_people or relations:
            _update_people(people_file, all_people,
                           datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), relations)
    return total_read, total_added


def dy_main():
    d = cfg.section("diary")
    if not d:
        print("[mindscape_diary] 未找到 diary 配置，跳过")
        return
    read, added = run_target(d)
    print("[mindscape_diary] 读取 %d 条消息，新增 %d 条日记" % (read, added))


if __name__ == "__main__":
    dy_main()


LN_DEFAULT_PERSONA = (
    "你是「说话风格研究员」，正在观察一个人真实发过的群消息。"
    "请提炼**增量**风格信息。只输出一个 JSON 对象，不要任何其他文字，格式："
    '{"observations":["关于他说话方式的观察：句式/断句/语气/习惯，每条一句话，'
    '只写本批体现的新特征"],'
    '"words":["新口头禅/高频词/语气词/梗"],'
    '"interests":["体现的爱好/状态/在做的事"],'
    '"memorable":["关于他生活/关系/约定、值得以后记住的事实"],'
    '"examples":["最体现他风格的 1-3 句原话，尽量短，用于模仿"]}'
    "要求：只写有把握且本批体现的；不写已知常识；没有新的就写空数组 []。"
)


LN_SECTIONS = [
    ("observations", "观察"),
    ("words", "新词"),
    ("interests", "兴趣"),
    ("examples", "原声示例"),
]


def ln_append(path, title, lines):
    """追加一节到 Markdown。返回写入条数。"""
    lines = [str(x).strip() for x in (lines or []) if x and str(x).strip()]
    if not lines:
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("## " + title + "\n")
        for x in lines:
            f.write("- " + x + "\n")
        f.write("\n")
    return len(lines)


def ln_run_target(d, target):
    """学一个对象，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 20)
    max_in = int(d.get("max_input_chars") or 12000)
    max_tok = int(d.get("max_tokens") or 1200)
    # ponytail: fetch 不带 LIMIT，一次运行会把游标之后的积压全跑完 ——
    # 首次指向一个几千条消息的库时会变成几百次 LLM 调用。单次封顶，
    # 剩下的留给下一轮定时任务慢慢追。
    max_batches = int(d.get("max_batches") or 40)
    persona = target.get("persona") or LN_DEFAULT_PERSONA

    user_id = str(target.get("user_id") or "")
    out_file = dy_abs(target.get("output"))
    if not user_id or not out_file:
        print("[mindscape_learn] target 缺 user_id 或 output，跳过")
        return 0, 0
    notes_file = dy_abs(target.get("notes")
                        or (out_file.rsplit(".", 1)[0] + ".notes.md"))
    state_file = dy_abs(target.get("state") or (out_file + ".state.json"))

    st = load_state(state_file)
    rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0),
                 only_user=user_id)
    if not rows:
        return 0, 0

    try:
        batches = chunk_by_budget(rows, batch, max_in)
    except ValueError as e:
        print("[mindscape_learn] 分批失败，本轮不动游标: %s" % str(e)[:140])
        return 0, 0
    total_added = 0
    for bi, chunk in enumerate(batches):
        if bi >= max_batches:
            print("[mindscape_learn] 已达单次上限 %d 批，剩余 %d 批留待下次"
                  % (max_batches, len(batches) - bi))
            break
        try:
            res = call_llm(llm, persona, chunk, max_in, max_tok,
                           expect_key="observations")
        except Exception as e:
            # 失败就停：只推进会成功的那部分，剩下的下次重试
            print("[mindscape_learn] LLM 失败，本批中断，剩余留待下次: %s"
                  % str(e)[:120])
            break
        if res is None:
            print("[mindscape_learn] 响应不可用，本批中断")
            break
        # 标题用这批消息自己的时间，而不是「运行时刻」 —— 追历史时一次运行会
        # 写出几十批，用运行时刻就会出现几十个一模一样的 ## 标题。
        stamp = datetime.datetime.fromtimestamp(chunk[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
        for key, head in LN_SECTIONS:
            total_added += ln_append(out_file, stamp + " " + head, res.get(key))
        total_added += ln_append(notes_file, stamp + " 值得记的", res.get("memorable"))
        st["since_ts"] = chunk[-1]["ts"]
        st["since_seq"] = chunk[-1]["seq"]
        save_state(state_file, st)
    return len(rows), total_added


def ln_main():
    d = cfg.section("learn")
    if not d:
        print("[mindscape_learn] 未找到 learn 配置，跳过")
        return
    if not d.get("enabled"):
        # 默认关闭：不显式打开就一行都不跑
        print("[mindscape_learn] learn.enabled 不为真，跳过（默认关闭）")
        return
    targets = d.get("targets") or []
    if not targets:
        print("[mindscape_learn] learn.targets 为空，跳过")
        return
    tr = ta = 0
    for target in targets:
        r, a = ln_run_target(d, target)
        tr += r
        ta += a
    print("[mindscape_learn] 读取 %d 条消息，新增 %d 条风格观察" % (tr, ta))


if __name__ == "__main__":
    ln_main()


def img_size(path):
    """只读文件头拿宽高，不依赖 Pillow。

    为什么需要它：采集器靠视觉模型判断「像不像这个 bot 的图」，
    但模型经常把游戏截图/壁纸也判成「二次元、可爱」—— 实测抓到过
    1920×1200 的原神剧情截图。**分辨率是这个误判最可靠的铁证**，
    而且读文件头几乎是零成本，能在调用视觉模型之前就挡掉。

    刻意**不按文件体积**判断：大 GIF 往往正是最合适的那张（动图帧多自然大），
    压缩或者丢弃都会把好东西扔掉。

    拿不到尺寸时返回 (0, 0)，调用方放行（宁可漏过，不可误杀）。
    """
    try:
        with open(path, "rb") as f:
            b = f.read(65536)
    except Exception:
        return 0, 0
    if len(b) < 24:
        return 0, 0
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if b[:3] == b"GIF":
        return struct.unpack("<HH", b[6:10])
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        fmt = b[12:16]
        if fmt == b"VP8X":
            return (1 + b[24] + (b[25] << 8) + (b[26] << 16),
                    1 + b[27] + (b[28] << 8) + (b[29] << 16))
        if fmt == b"VP8 ":
            return (struct.unpack("<H", b[26:28])[0] & 0x3FFF,
                    struct.unpack("<H", b[28:30])[0] & 0x3FFF)
        if fmt == b"VP8L":
            bits = b[21] | (b[22] << 8) | (b[23] << 16) | (b[24] << 24)
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return 0, 0
    if b[:2] == b"\xff\xd8":
        i = 2
        while i < len(b) - 9:
            if b[i] != 0xFF:
                i += 1
                continue
            m = b[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return 0, 0


def st_abs(path):
    """相对路径按「配置文件所在目录」解析（与其它模块各自持有一份，互不覆盖）。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


def shrink_for_judge(path, max_px=512, quality=80):
    """判定只需要「看得出画的是什么」，不需要原图。

    实测（deepseek-flash）：99 KB 的图 2.3 秒，而把 6 MB 的原图整个 base64
    塞上去要 20 秒以上 —— 慢在上行体积和视觉 token 上。按最长边缩到 max_px
    再转 JPEG，判定结论不变，耗时掉一个数量级。

    缩不了就原样返回（宁可慢，不可判不了）；连读都读不到才返回空。
    """
    try:
        import io as _io
        from PIL import Image as _Img
        im = _Img.open(path)
        im.seek(0)                      # 动图只取第一帧
        im = im.convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), _Img.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return b"", ""
    low = path.lower()
    mime = "image/gif" if low.endswith(".gif") else ("image/png" if low.endswith(".png") else "image/jpeg")
    return raw, mime


_INSTANCE = None


def _NS():
    return _INSTANCE


def su_abs(path):
    """相对路径按「配置文件所在目录」解析。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


def pick_image(comps, image_cls, reply_cls, depth=0, max_depth=3):
    """在组件链里找第一张图，**会往被引用消息里钻**。

    为什么必须钻：aiocqhttp 适配器收到 reply 段时会 `call_action("get_msg")`，
    把被引用消息的完整组件链塞进 `Reply.chain` —— 也就是说图**本来就在事件里**，
    只是不在顶层。只扫顶层的话，「引用一张图说『加进表情库』」永远得到
    「这条消息里没看到图片」，而模型那条路却能看见同一张图（所以显得像左右脑互搏）。

    类由调用方传进来（本模块要能在 AstrBot 之外被测试）；深度防环。
    """
    if not comps or depth > max_depth:
        return None
    for c in comps:
        if isinstance(c, image_cls):
            return c
    for c in comps:
        if isinstance(c, reply_cls):
            got = pick_image(getattr(c, "chain", None), image_cls, reply_cls,
                             depth + 1, max_depth)
            if got is not None:
                return got
    return None


DEFAULT_PICK_PROMPT = (
    "你正在群聊里说话。\n\n"
    "你刚回复了这段话：\n「{reply}」\n\n"
    "现在要给这条回复配一张表情包。候选如下：\n{candidates}\n\n"
    "规则：\n"
    "- **必须从上面选一张**，不许说「不用」，也不许编造不存在的文件名\n"
    "- 选最贴合当前语气的那张（比如怼人配得意/鄙夷，被夸配臭美，无语配呆滞）\n"
    "- 直接返回文件名，不要解释、不要引号、不要多余的字"
)


def _load_index(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict) and x.get("file")]
    except Exception:
        pass
    return []


@llm_tool(name="save_sticker")
async def save_sticker(*args, **kwargs):
    """把当前消息里的图片存进表情包库。

    当有人说「这张适合当你的表情包」「送你一张图」，或你自己看中某张图时，
    调用这个工具真正把它收进来 —— 光嘴上说「收下」是没用的。

    Args:
        name(string): 给这张图起个中文短名
        tags(string): 标签，用逗号分隔
    """
    ev = None
    for a in args:
        if hasattr(a, "get_self_id"):
            ev = a
            break
    if ev is None:
        return "找不到当前消息，存不了。"
    inst = _NS()
    if inst is None:
        return "插件还没准备好，稍后再试。"
    return await inst.save_sticker_impl(ev, kwargs)


PUNCT = "。！？~…，、；："


PERIODS = ("。", "．")


def drop_period(text):
    """句尾不点标点：末尾的「。」去掉（留空），句中的「。」换成「，」。

    为什么不是一律换成「~」：有些沉重的句子拿波浪号收尾会变味 ——
    规矩是**不用句号表示「说完了」**，不是每句都要卖萌。所以末尾留空，
    中间用逗号接着往下走（和本模块压平多段时的连接符一致）。
    """
    if not text:
        return text
    for ch in PERIODS:
        if ch in text:
            text = "，".join(p for p in (x.strip() for x in text.split(ch)) if p)
    return text


def flatten(text, join_with="，", drop_last_if_short=False, short_len=8):
    """把多段文本压成一段。"""
    if not text:
        return text
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", norm) if p.strip()]
    if len(parts) <= 1:
        return text.strip()
    if drop_last_if_short and len(parts) > 1 and len(parts[-1]) <= short_len:
        parts = parts[:-1]
    out = parts[0]
    for nxt in parts[1:]:
        if out and out[-1] in PUNCT:
            out += nxt
        else:
            out += join_with + nxt
    return out


TRACE_PRIORITY = -100


TRACE_KEY = "_mindscape_trace_t0"


TRACE_PENDING_MAX = 64        # 没等到响应的残留记录最多留这么多（防无限增长）


DEFAULT_WARN_MS = 8000        # 慢于此 → 升级成 WARNING，方便 grep


DEFAULT_WARN_CHARS = 20000    # system_prompt 超过这个字数 → 疑似异常注入


def tr_key(event):
    """本轮的键：同一会话的请求与响应必须能对上。"""
    return str(getattr(event, "unified_msg_origin", "") or event.get_self_id())


def tr_ctx_chars(contexts):
    """上下文里所有文本的字符总数（只数长度，不碰内容）。"""
    total = 0
    for m in (contexts or []):
        if not isinstance(m, dict):
            total += len(str(m))
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part["text"])
    return total


def tr_tool_count(request):
    """这一轮挂了多少个工具（工具 schema 本身就占 prompt）。"""
    ft = getattr(request, "func_tool", None)
    if ft is None:
        return 0
    try:
        return len(ft.names())
    except Exception:
        return 0


def tr_hist(request):
    """会话历史的规模，返回 (条数, 字符数)。

    ⚠️ 框架在 `on_llm_request` 这一刻**可能还没把历史并进 `contexts`**
    （实测两个字段都是 0/空）—— 所以只用 `contexts` 当体积指标会一直是 0。
    拿不到就返回 (0, 0)：量到 0 不代表没有历史，只代表此刻它还不在手上。
    """
    conv = getattr(request, "conversation", None)
    raw = getattr(conv, "history", None) if conv is not None else None
    if not isinstance(raw, str) or not raw:
        return (0, 0)
    try:
        items = json.loads(raw)
    except Exception:
        return (0, len(raw))
    return ((len(items) if isinstance(items, list) else 0), len(raw))


def tr_usage(resp):
    """token 用量；框架没给就留空。"""
    u = getattr(resp, "usage", None)
    if not u:
        return ""
    try:
        return " tok=%d+%d/%d" % (u.input_other, u.input_cached, u.output)
    except Exception:
        return ""
# ==================================================================
# 命名空间：让 cfg.xxx() / core.xxx() 这类调用在合并后依然可用
# ==================================================================
class _MindscapeConfigNS:
    """config 模块的函数集合（合并后替代 import mindscape_config as cfg）。"""
    section = staticmethod(section)
    load = staticmethod(load)
    bot_entries = staticmethod(bot_entries)
    config_path = staticmethod(config_path)
    abs_path = staticmethod(abs_path)


cfg = _MindscapeConfigNS()
class BlockMixin:
    def setup(self, context):
        self.bl_on, self.bl_table = bl_load_config()
        self.bl_count = 0
        logger.info("[mindscape_block] loaded | enabled=%s | bots=%s",
                    self.bl_on, {k: len(v) for k, v in self.bl_table.items()})

    @filter.event_message_type(EventMessageType.ALL, priority=BL_PRIORITY)
    async def bl_pre_block(self, event: AstrMessageEvent):
        """命中黑名单 → 终止事件传播：不回复、不采集图片、不进任何插件。"""
        try:
            if not self.bl_on:
                return
            users = self.bl_table.get(str(event.get_self_id()))
            if not users:
                return
            sender = str(event.get_sender_id())
            if sender not in users:
                return
            self.bl_count += 1
            logger.info("[mindscape_block] 前置拦截 | bot=%s sender=%s（第 %d 次）",
                        event.get_self_id(), sender, self.bl_count)
            event.stop_event()
        except Exception as e:
            # 拦截逻辑出错时放行 —— 宁可漏拦，也不能把正常消息吞掉
            logger.warning("[mindscape_block] 拦截异常，已放行: %s", str(e)[:120])


class GuardMixin:
    def setup(self, context):

        self.patterns, self.regex = _load_config()
        self.blocked = 0
        logger.info("[mindscape_guard] loaded | %d 条拦截规则", len(self.patterns))

    @filter.on_decorating_result(priority=999)
    async def block_error(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if result is None:
                return
            txt = result.get_plain_text() or ""
            if not txt.strip():
                return
            if is_error_text(txt, self.patterns, self.regex):
                self.blocked += 1
                # S03: 只记录脱敏摘要，避免上游报错里夹带的密钥进日志
                logger.warning(
                    "[mindscape_guard] 拦下报错（第 %d 条）: %s",
                    self.blocked,
                    redact(txt),
                )
                event.clear_result()
                event.stop_event()
        except Exception as e:
            logger.warning("[mindscape_guard] 检查失败: %s", str(e)[:120])


class MemoryMixin:
    def setup(self, context):

        self.m_cfg = cfg.section("memory")
        logger.info(
            "[mindscape_memory] loaded | %d bot(s) 配置了记忆",
            len(cfg.bot_entries()),
        )

    def _find_bot(self, self_id):
        for b in cfg.bot_entries():
            if str(b.get("self_id", "")) == str(self_id):
                return b
        return None

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            bot = self._find_bot(event.get_self_id())
            if not bot:
                return
            path = _resolve(bot.get("diary"))
            max_chars = int(bot.get("memory_chars") or self.m_cfg.get("max_chars") or DEFAULT_MAX_CHARS)
            min_chars = int(self.m_cfg.get("min_chars") or DEFAULT_MIN_CHARS)

            # 支持额外记忆文件（extra_diaries），例如人格文件里的关系与约定
            extras = []
            for x in (bot.get("extra_diaries") or []):
                if isinstance(x, str) and x.strip():
                    extras.append(_resolve(x))
            mem = read_many(extras + [path] if extras else [path], max_chars)

            # 骨架层：前几天各一句（mindscape_digest 产的）。滑动窗口只够覆盖
            # 几小时，没有这一层，bot 每天都「忘了昨天」—— 细节可以让它去
            # recall，但「记不记得昨天发生过什么」必须是常驻的。
            dig = ""
            dig_path = _resolve(bot.get("digest"))
            if dig_path:
                d_chars = int(bot.get("digest_chars")
                              or self.m_cfg.get("digest_chars") or DEFAULT_DIGEST_CHARS)
                dig = read_recent(dig_path, d_chars)

            # 账本：bot 自己**当场写**的东西。日记与摘要都是后台生成的，检索只读，
            # 于是「承诺」没地方落笔 —— 说过的话下一轮就漂了。这本账就是为了让
            # 细节问题有一个稳定的答案。
            notes = ""
            nt_path = _resolve(bot.get("notes"))
            if nt_path:
                n_chars = int(bot.get("notes_chars")
                              or self.m_cfg.get("notes_chars") or DEFAULT_NOTES_CHARS)
                notes = read_recent(nt_path, n_chars)

            sty = ""
            st_path = _resolve(bot.get("style"))
            if st_path:
                s_chars = int(bot.get("style_chars")
                              or self.m_cfg.get("style_chars") or DEFAULT_STYLE_CHARS)
                # 稳定层是文档 → 从头读；近期层是追加流 → 取尾
                sty = _clean_style(read_head(st_path, s_chars))

            # 近期层：最新口癖（mindscape_style 产的 recent）。与稳定层配对，
            # 没有它就退回「只有长期习惯」，没有稳定层就退回「只有最近」——
            # 两者都缺才完全不注入。
            sty2 = ""
            sr_path = _resolve(bot.get("style_recent"))
            if sr_path:
                sr_chars = int(bot.get("style_recent_chars")
                               or self.m_cfg.get("style_recent_chars")
                               or DEFAULT_STYLE_RECENT_CHARS)
                sty2 = _clean_style(read_recent(sr_path, sr_chars))

            if len(mem) < min_chars and not dig and not notes and not sty and not sty2:
                return

            old = getattr(request, "system_prompt", "") or ""
            if SECTION_TITLE in old:
                return

            label = bot.get("name") or "你"
            # 光给记忆不够 —— 实测：它只在窗口里翻到一个就下了结论，而同一件事
            # 在日记里记着好几回。它把「我上下文里只有这些」当成了「总共就这些」。
            # 所以这里必须做两件事：
            #   1. 明说这只是最近一部分，不是全部
            #   2. 给出「什么情况下必须先查」的触发条件
            # 光靠工具描述不够：它压根没意识到自己需要查。
            # 触发条件刻意只写**抽象类别**（数量/名单/最值/时间指向），不写具体
            # 例子：具体例子永远列不全，而且会把没枚举到的场景整片漏掉。
            block = (
                "\n\n" + SECTION_TITLE + "\n"
                "以下是你自己记下来的往事，是你亲身经历的，可以自然地提起，"
                "但不要照本宣科地念，也不要说「根据我的记忆」这种话。\n\n"
                "**注意：下面只是你最近记下的一部分，不是你的全部记忆。**"
                "更早的事你能用 recall_memory 翻出来 —— 手头没有，不等于没发生过，"
                "别拿眼前这一点就当成了全部。\n\n"
                "**别人说过的，不等于事实。** 下面要是记着「某人说……」，那只是\n"
                "他讲过这句话，不是你自己查证过的结论 —— 可以拿来当谈资，\n"
                "但别替它背书，也别把它当成群里公认的规矩。\n"
                "讲的时候用你自己的方式就行，不用原样复述，也不必每次都点名是谁讲的。\n\n"
                "**遇到下面这几种，先查再答**：\n"
                "- 答案要「翻遍全部」才给得准的：数量、名单、最值、有没有发生过\n"
                "- 问的是具体细节（谁和谁、什么时候、你答应过什么）：先看下面\n"
                "  的「你记下的账」，账上没有的，再用 recall_memory 去翻\n"
                "- 问题指的是更早的时间：以前、上次、第一次、这几天\n"
                "- 你准备说「只有」「就这些」「没有」的时候\n"
            )
            # 规矩：每个 bot 自己的行为约束，写在配置里（不进代码，避免把
            # 某个人设特有的规矩硬编码进通用框架）。
            rules = [str(x).strip() for x in (bot.get("rules") or []) if str(x).strip()]
            if rules:
                block += SECTION_RULES + "\n" + "\n".join("- " + r for r in rules) + "\n\n"
            if notes:
                block += SECTION_NOTES + "\n" + notes + "\n\n"
            if sty or sty2:
                block += SECTION_STYLE + "\n"
                if sty:
                    block += SECTION_STYLE_STABLE + "\n" + sty + "\n\n"
                if sty2:
                    block += SECTION_STYLE_RECENT + "\n" + sty2 + "\n\n"
                block += STYLE_GUARD
            if dig:
                block += SECTION_DIGEST + "\n" + dig + "\n\n"
            block += mem

            # 人物画像（可选）：让 bot 认得群里的人
            people_path = bot.get("people")
            if not people_path and path:
                people_path = path.rsplit(".", 1)[0] + ".people.md"
            people_path = _resolve(people_path)
            p_chars = int(bot.get("people_chars") or self.m_cfg.get("people_chars") or DEFAULT_PEOPLE_CHARS)
            people = read_people(people_path, p_chars)
            if people:
                block += (
                    "\n\n## 你认识的人\n"
                    "这些是你记住的群友，聊天时可以自然地认得他们；"
                    "没在名单里的人，就当第一次见。\n\n"
                    + people
                )

            # 自主冒泡轮（cron 触发）。这一轮是「它自己想开口」，不是回应谁 ——
            # 记忆和风格照常给（人的联想本来就靠记忆的连续性），
            # 但必须明说**可以完全不依赖它们**，否则它会为了用上记忆去翻旧事。
            if event.get_extra("cron_job"):
                block += (
                    "\n\n**【这一轮是你自己想开口，不是回应谁。】**\n"
                    "上面的记忆、账本、风格都只是背景 —— **可以完全不依赖它们**。\n"
                    "没想到什么就用不上，不要硬扯，也不要为了用上记忆去翻旧事。\n"
                    "想到什么就说什么。\n"
                )

            request.system_prompt = old + block
            logger.info("[mindscape_memory] %s 注入 %d 字记忆 / %d 字摘要 / %d 字账本"
                        " / %d 字风格 / %d 字人物",
                        label, len(mem), len(dig), len(notes), len(sty), len(people))
        except Exception as e:
            logger.warning("[mindscape_memory] 注入失败: %s", str(e)[:120])


class StickersMixin:
    def setup(self, context):

        self.s_c = cfg.section("stickers")
        self.dir = st_abs(self.s_c.get("dir") or "./data/stickers")
        self.index_path = st_abs(self.s_c.get("index") or os.path.join(self.dir, "index.json"))
        self.seen_path = st_abs(self.s_c.get("seen") or os.path.join(self.dir, "seen.json"))
        os.makedirs(self.dir, exist_ok=True)
        self.seen_ok = set()        # 已入库（键 = 分类:md5）
        self.seen_no = set()        # 明确拒绝（重启后也不清）
        self.seen = self._load_seen()
        self._bg = set()            # 后台采集任务，留引用防被 GC 掉
        logger.info(
            "[mindscape_stickers] loaded | prob=%.2f | 已见 %d 张",
            float(self.s_c.get("sample_prob", 0.10)), len(self.seen),
        )

    def _load_seen(self):
        """读去重表。

        结构：{"accepted": [...], "rejected": [...]}，键都是「分类:md5」。
          - accepted：真正入库过的图
          - rejected：**明确拒绝**过的图（超尺寸 / 视觉判定 related=false）

        ⚠️ 两类必须分开。以前只有一张平表，自愈时「只保留图库里现存的图」——
        于是明确拒绝的记录一重启就被清掉：同一张图被反复下载、反复调用视觉 API
        （白花钱），甚至可能因为判定翻转又进了库。
        现在只清理 accepted 里「图已从库中删除」的墓碑，rejected 一律保留。

        自愈判据必须用**内容 md5**，不能拿文件名前缀凑：导入脚本会把文件重命名成
        「<前缀>_xxxx.gif」，前缀就不再是 md5 —— 用前缀匹配会把真实存在的图误判成
        墓碑，去重记录一丢，那张图重发就会以原名再入一份（造出重复）。
        ponytail: 启动时把整个图库哈希一遍（目前 51 张 / 54MB，约 0.2s）。
                   涨到几百 MB 就该改成 sidecar 的 md5 清单。
        """
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return set()
        legacy = isinstance(d, list)
        if legacy:
            keys = set(d)
        elif isinstance(d, dict):
            keys = set(d.get("accepted") or []) | set(d.get("rejected") or [])
        else:
            return set()
        live = set()
        try:
            for x in (load_index(self.index_path) or []):
                fn = str(x.get("file") or "")
                if not fn:
                    continue
                with open(os.path.join(self.dir, fn), "rb") as fp:
                    live.add(str(x.get("category") or "") + ":"
                             + hashlib.md5(fp.read()).hexdigest())
        except Exception:
            # 图库读不出来就不敢动去重表：全按拒绝保留，宁可少收也不重复烧 API
            self.seen_ok, self.seen_no = set(), keys
            return keys
        if legacy:
            # 老格式分不清来源：在图库里的算入库，其余**保守归入拒绝**
            self.seen_ok = set(k for k in keys if k in live)
            self.seen_no = keys - self.seen_ok
            logger.info("[mindscape_stickers] 去重表迁移：老平表 %d 条 → 入库 %d / 拒绝 %d",
                        len(keys), len(self.seen_ok), len(self.seen_no))
            self._save_seen()
            return keys
        acc = set(d.get("accepted") or [])
        self.seen_ok = set(k for k in acc if k in live)
        self.seen_no = set(d.get("rejected") or [])
        if self.seen_ok != acc:
            logger.info("[mindscape_stickers] 去重表自愈：入库 %d -> %d（清掉 %d 条墓碑），"
                        "拒绝记录 %d 条原样保留",
                        len(acc), len(self.seen_ok), len(acc) - len(self.seen_ok),
                        len(self.seen_no))
            self._save_seen()
        return self.seen_ok | self.seen_no

    def _save_seen(self):
        try:
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump({"accepted": sorted(self.seen_ok)[-3000:],
                           "rejected": sorted(self.seen_no)[-3000:]}, f)
        except Exception:
            pass

    def _mark_seen(self, key, accepted):
        """记一条去重记录。

        accepted=False = **明确拒绝**（超尺寸 / 判定不相关）—— 这类记录重启后
        也不会被自愈清掉；只有「入库过的图被从库里删了」才清。
        """
        if accepted:
            self.seen_ok.add(key)
        else:
            self.seen_no.add(key)
        self.seen.add(key)
        self._save_seen()
    def _add_index(self, fname, category, verdict):
        """加锁 + 重读 + 合并 + 原子写，避免和导入脚本互相覆盖。"""
        entry = {
            "file": fname,
            "category": category,
            "name": str(verdict.get("name") or "未命名")[:12],
            "tags": [str(x)[:10] for x in (verdict.get("tags") or [])][:6],
            "desc": str(verdict.get("desc") or "")[:150],
        }
        try:
            with IndexLock(self.index_path):
                idx = load_index(self.index_path)
                if idx is None:
                    logger.warning("[mindscape_stickers] 索引损坏，本次不入库")
                    return
                if any(x.get("category") == category and str(x.get("file")) == fname
                       for x in idx):
                    return
                idx.append(entry)
                save_index(self.index_path, idx)
        except Exception as e:
            logger.warning("[mindscape_stickers] 写索引失败: %s", str(e)[:120])

    def _target_category(self, self_id):
        for t in (self.s_c.get("targets") or []):
            if str(t.get("self_id", "")) == str(self_id):
                return t.get("category") or "default"
        return None

    @filter.event_message_type(EventMessageType.ALL)
    async def collect(self, event: AstrMessageEvent):
        try:
            category = self._target_category(event.get_self_id())
            if not category:
                return
            if random.random() > float(self.s_c.get("sample_prob", 0.10)):
                return
            comps = getattr(event.message_obj, "message", None) or []
            for comp in comps:
                if not isinstance(comp, Image):
                    continue
                # 绝不能 await：下载 + 视觉判定要 10~25 秒，一 await 就把整条消息
                # 流水线一起堵住（实测回复从 2 秒被拖到 27 秒）。丢后台跑 ——
                # 判定晚几秒入库无所谓，回复不能等它。
                import asyncio
                task = asyncio.create_task(self._handle(comp, category))
                self._bg.add(task)
                task.add_done_callback(self._bg_done)
                return
        except Exception as e:
            logger.warning("[mindscape_stickers] 采集失败: %s", str(e)[:140])

    def _bg_done(self, task):
        """后台任务收尾：扔掉引用 + 把异常捞出来（不然会被静默吞掉）。"""
        self._bg.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("[mindscape_stickers] 后台采集出错: %s", str(exc)[:140])

    async def _handle(self, comp, category):
        import asyncio
        try:
            path = await asyncio.wait_for(comp.convert_to_file_path(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("[mindscape_stickers] 图片下载超时，跳过")
            return
        except Exception as e:
            logger.warning("[mindscape_stickers] 图片下载失败: %s", str(e)[:100])
            return
        if not path or not os.path.exists(path):
            return

        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()
        # R11: 去重键包含分类 —— 同一张图可以被不同 bot 各自采用，
        # 且失败/跳过不会污染另一个分类。
        key = category + ":" + h
        if key in self.seen:
            return

        # 分辨率闸门：尺寸过大的一律视为「抓错了」（游戏截图 / 壁纸 / 壁纸级同人图），
        # 直接不入库。放在视觉判定之前 —— 既省一次 API，也不必把几 MB 的图 base64
        # 传上去。（判据用分辨率而不是体积，理由见 img_size 的注释。）
        max_side = int((self.s_c.get("judge") or {}).get("max_side") or 0)
        if max_side > 0:
            # 变量名不能叫 h：上面 h 已经是 md5，下面还要拿它拼文件名。
            # 复用的代价是 'int' object is not subscriptable —— 通过闸门的图全存不进去。
            w, ih = img_size(path)
            if w and ih and max(w, ih) > max_side:
                self._mark_seen(key, False)   # 明确拒绝：重启后也不该重判
                logger.info("[mindscape_stickers] 跳过 %dx%d（超过 %d，疑似截图/壁纸）",
                            w, ih, max_side)
                return

        verdict = await self._judge(path)
        if verdict is None:
            # 判定失败（超时/无 key/解析失败）：本次不记为已见，允许下次重试
            return
        if not verdict.get("related"):
            # 明确判定为「不相关」：认为已处理，不再重复消耗 API
            self._mark_seen(key, False)   # 明确拒绝：重启后也不该重判
            return

        ext = os.path.splitext(path)[1].lower() or ".jpg"
        fname = h[:10] + ext
        dst = os.path.join(self.dir, fname)
        try:
            shutil.copy2(path, dst)
        except Exception as e:
            logger.warning("[mindscape_stickers] 保存失败: %s", str(e)[:100])
            return

        if not verdict_ok(verdict):
            logger.warning("[mindscape_stickers] 判定内容无效（疑似复读），丢弃")
            try:
                os.remove(dst)
            except Exception:
                pass
            return

        self._mark_seen(key, True)    # 只有真正入库成功才算「入库」
        self._add_index(fname, category, verdict)
        logger.info("[mindscape_stickers] 已入库 %s | %s", fname, verdict.get("name"))

    async def _judge(self, path):
        import httpx
        j = self.s_c.get("judge") or {}
        api_base = (j.get("api_base") or "").rstrip("/")
        if not api_base:
            return None
        key = os.environ.get(j.get("api_key_env") or "", "")
        if not key:
            return None
        prompt = (j.get("prompt") or DEFAULT_PROMPT).replace(
            "{persona}", j.get("persona") or "二次元角色"
        )
        raw, mime = shrink_for_judge(path)
        if not raw:
            return None
        b64 = base64.b64encode(raw).decode()
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": j.get("model") or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}},
                        ]}],
                        "max_tokens": int(j.get("max_tokens") or 2000),
                    },
                )
            if resp.status_code != 200:
                logger.warning("[mindscape_stickers] 判断 API %s", resp.status_code)
                return None
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                txt = (msg.get("reasoning_content") or "").strip()
        except Exception as e:
            logger.warning("[mindscape_stickers] 判断异常: %s", str(e)[:140])
            return None

        return parse_verdict(txt)
        logger.warning("[mindscape_stickers] JSON 解析失败: %s", txt[:140])
        return None


class StickerUseMixin:
    def setup(self, context):

        self.u_c = cfg.section("stickers")
        self.dir = su_abs(self.u_c.get("dir") or "./data/stickers")
        self.index_path = su_abs(self.u_c.get("index") or os.path.join(self.dir, "index.json"))
        self.send_cfg = self.u_c.get("send") or {}
        # 队形检测：记录各群最近的图片指纹（不下载图片，只用框架给的标识）
        self._recent_imgs = {}
        self.formation = self.send_cfg.get("formation") or {}
        global _INSTANCE
        _INSTANCE = self          # 供模块级工具回调
        self._NS_registered = True
        logger.info(
            "[mindscape_sticker_use] loaded | 强制概率 %.2f",
            float(self.send_cfg.get("force_prob", 0.10)),
        )

    def _category_of(self, self_id):
        for t in (self.u_c.get("targets") or []):
            if str(t.get("self_id", "")) == str(self_id):
                return t.get("category") or "default"
        return None

    def _pool(self, self_id):
        """取该 bot 可用的图。

        配置了分类就只返回该分类（即使为空也不跨分类），
        没配置分类的 bot 返回空集合 —— 隔离优先于「有图可用」。
        """
        cat = self._category_of(self_id)
        if not cat:
            return []
        idx = _load_index(self.index_path)
        return [x for x in idx if x.get("category") == cat]

    # ── 能力1：bot 主动要图 ──
    async def save_sticker_impl(self, event, kwargs):
        """把消息里的图片存进本 bot 的分类（供 save_sticker 工具调用）。"""
        import asyncio
        import hashlib
        import shutil
        from astrbot.core.message.components import Image, Reply
        name = str(kwargs.get("name") or "").strip()[:12] or "私藏"
        raw = str(kwargs.get("tags") or "").strip()
        tags = [x.strip()[:10] for x in raw.replace("，", ",").split(",") if x.strip()][:6]
        if not tags:
            tags = ["私藏", "表情包"]
        cat = self._category_of(event.get_self_id())
        if not cat:
            return "你还没有配置表情包分类，存不了。"
        comps = []
        try:
            comps.extend(getattr(event.message_obj, "message", None) or [])
        except Exception:
            pass
        img = pick_image(comps, Image, Reply)
        if img is None:
            # 万一还是捞不到，把链的形状记下来 —— 一眼看得出图到底在不在事件里
            logger.info("[mindscape_stickers] save_sticker 没找到图，链= %s",
                        [type(c).__name__ for c in comps][:10])
            return "这条消息里没看到图片，你把它单独发一次？"
        try:
            path = await asyncio.wait_for(img.convert_to_file_path(), timeout=20)
        except asyncio.TimeoutError:
            return "这张图下载超时了，你重发一次试试？"
        except Exception as e:
            return "图片下载失败：" + str(e)[:60]
        if not path or not os.path.exists(path):
            return "图片拿不到，存不了。"
        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()
        ext = os.path.splitext(path)[1].lower() or ".jpg"
        fname = h[:10] + ext
        dst = os.path.join(self.dir, fname)
        try:
            with IndexLock(self.index_path):
                idx = load_index(self.index_path) or []
                if any(x.get("category") == cat and str(x.get("file")) == fname for x in idx):
                    return "这张我已经收过啦。"
                shutil.copy2(path, dst)
                idx.append({"file": fname, "category": cat, "name": name,
                            "tags": tags, "desc": "收的：" + (raw or name)[:80]})
                save_index(self.index_path, idx)
        except Exception as e:
            return "存图失败：" + str(e)[:60]
        return "收好啦：%s（%s）" % (name, "/".join(tags))

    @llm_tool(name="send_sticker")
    async def send_sticker(self, *args, **kwargs):
        """发一张表情包/图片到当前聊天（纯图片，不带文字）。

        当对方要求你「发个表情包/发图」，或你觉得此刻甩一张图最合适时，调用这个工具。

        Args:
            want(string): 你想要的图的感觉或用途，例如：无语、开心、发呆、得意、撒娇。留空则随机挑一张。
        """
        want = str(kwargs.get("want") or "").strip()
        svc = getattr(self, "context", None)

        # 从 args 里找出真正的 event（框架可能把插件类绑在第一个参数）
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        sid = ""
        try:
            sid = str(ev.get_self_id()) if ev is not None else ""
        except Exception:
            sid = ""

        pool = self._pool(sid)
        if not pool:
            return "图库还是空的，先攒几张再发"

        best, best_score = None, -1.0
        for item in pool:
            sc = match_score(item, want)
            if sc > best_score:
                best_score, best = sc, item
        if best is None:
            best = pool[0]
        fn = safe_name(best.get("file", ""))
        path = os.path.join(self.dir, fn)
        if not fn or not os.path.exists(path) or not is_inside(path, self.dir):
            return "那张图找不到了"
        try:
            return MessageEventResult().file_image(path)
        except Exception as e:
            return "发图失败：" + str(e)[:60]

    # ── 能力2：概率强制配图 ──
    @filter.on_decorating_result(priority=850)
    async def maybe_attach(self, event: AstrMessageEvent):
        try:
            if not self._category_of(event.get_self_id()):
                return
            # 队形优先：群里在斗图时，用更高的概率跟一张
            in_war = False
            if self.formation.get("enabled", True):
                in_war = self._in_image_war(
                    event.get_group_id(),
                    int(self.formation.get("window", 90)),
                    int(self.formation.get("need_same", 2)),
                )
            prob = float(self.formation.get("prob", 0.35) if in_war
                         else self.send_cfg.get("force_prob", 0.10))
            if random.random() > prob:
                return
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            pool = self._pool(event.get_self_id())
            if not pool:
                return
            reply = (result.get_plain_text() or "")[:200]
            fname = await self._pick(reply, pool)
            if not fname:
                return
            path = os.path.join(self.dir, fname)
            if not os.path.exists(path) or not is_inside(path, self.dir):
                return
            result.file_image(path)
            logger.info("[mindscape_sticker_use] 配图 %s", fname)
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 配图失败: %s", str(e)[:140])

    # ── 队形：跟群里的斗图 ──
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def watch_images(self, event: AstrMessageEvent):
        """记录群里最近的图片指纹，用于判断是否在斗图。"""
        try:
            if not self._category_of(event.get_self_id()):
                return
            comps = getattr(event.message_obj, "message", None) or []
            import time as _t
            gid = str(event.get_group_id() or "")
            for comp in comps:
                if not isinstance(comp, Image):
                    continue
                key = getattr(comp, "file", None) or getattr(comp, "url", None) or ""
                if not key:
                    continue
                lst = self._recent_imgs.setdefault(gid, [])
                lst.append((_t.time(), str(key)))
                del lst[:-10]          # 只留最近 10 条
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 记录群图失败: %s", str(e)[:100])

    def _in_image_war(self, group_id, window=90, need_same=2):
        """判断这个群是否在斗图：时间窗内是否有同一张图出现 ≥ need_same 次。"""
        import time as _t
        lst = self._recent_imgs.get(str(group_id or "")) or []
        now = _t.time()
        recent = [k for (ts, k) in lst if now - ts <= window]
        if len(recent) < need_same:
            return False
        from collections import Counter
        return Counter(recent).most_common(1)[0][1] >= need_same

    async def _pick(self, reply, pool):
        import httpx
        j = self.u_c.get("judge") or {}
        api_base = (j.get("api_base") or "").rstrip("/")
        key = os.environ.get(j.get("api_key_env") or "", "")
        if not api_base or not key:
            return None
        n = int(self.send_cfg.get("candidates") or 12)
        cands = pool if len(pool) <= n else random.sample(pool, n)
        lines = []
        for i, it in enumerate(cands, 1):
            tags = "/".join(str(x) for x in (it.get("tags") or [])[:5])
            lines.append("%d. %s  [%s]  %s" % (i, it.get("file"), tags, str(it.get("desc") or "")[:60]))
        prompt = (self.send_cfg.get("prompt") or DEFAULT_PICK_PROMPT).replace(
            "{reply}", reply or "（没说什么）"
        ).replace("{candidates}", chr(10).join(lines))
        try:
            async with httpx.AsyncClient(timeout=90) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": j.get("model") or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1200,
                    },
                )
            if resp.status_code != 200:
                return None
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 选图异常: %s", str(e)[:120])
            return None

        low = txt.lower()
        for it in cands:
            fn = str(it.get("file"))
            if fn and fn.lower() in low:
                return fn
        best, bs = None, -1.0
        for it in cands:
            sc = match_score(it, reply)
            if sc > bs:
                bs, best = sc, it
        return str(best.get("file")) if best else None


class FormatMixin:
    def setup(self, context):

        self.f_c = cfg.section("format")
        self.targets = [str(x) for x in (self.f_c.get("targets") or [])]
        # 句尾去句号：**子选项，空 = 关**（故意不沿用「空 = 全部 bot」那条旧语义，
        # 否则谁忘写一行，全场的句号都被剃光）
        self.f_np = [str(x) for x in (self.f_c.get("no_period") or [])]
        logger.info("[mindscape_format] loaded | %d target(s) | 句尾去句号=%s",
                    len(self.targets), self.f_np or "关")
        scope_warn(logger, "mindscape_format", self.targets)

    @filter.on_decorating_result(priority=900)
    async def flatten_result(self, event: AstrMessageEvent):
        try:
            if not scope_hit(self.targets, event.get_self_id()):
                return
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            chain = getattr(result, "chain", None)
            if not chain:
                return
            join_with = self.f_c.get("join_with") or "，"
            drop = bool(self.f_c.get("drop_last_if_short", False))
            short_len = int(self.f_c.get("short_len") or 8)
            no_period = bool(self.f_np) and scope_hit(self.f_np, event.get_self_id())
            for comp in chain:
                txt = getattr(comp, "text", None)
                if not isinstance(txt, str) or not txt.strip():
                    continue
                new = flatten(txt, join_with, drop, short_len)
                if no_period:
                    new = drop_period(new)
                if new != txt:
                    comp.text = new
        except Exception as e:
            logger.warning("[mindscape_format] 处理失败: %s", str(e)[:120])


class RescueMixin:
    def setup(self, context):
        self.r_cfg = cfg.section("rescue") or {}
        env = self.r_cfg.get("api_key_env") or ""
        self.r_ready = bool((self.r_cfg.get("api_base") or "").strip()
                            and env and os.environ.get(env))
        # 一定要把「就绪没就绪」喊出来：以前拿不到 key 就静默 return，
        # 结果「空回复救援」一次都没生效过，日志里却一个字都没有。
        logger.info("[mindscape_rescue] loaded | %s | %s",
                    "启用" if self.r_cfg.get("enabled", True) else "关闭",
                    "就绪" if self.r_ready
                    else ("未就绪（缺 api_base 或环境变量 %s），空回复将无法兜住" % (env or "(未配置)")))

    @filter.on_llm_response()
    async def rescue_empty(self, event: AstrMessageEvent, response):
        if not self.r_cfg.get("enabled", True):
            return
        try:
            if response is None:
                return
            # 已经有文字 / 已经带了结果链（比如只发了图）/ 还要调工具，都不算空回复
            if (getattr(response, "completion_text", "") or "").strip():
                return
            # 走到这里 = 这一轮文字真的空了。先记一笔，方便日后定位；
            # 以前这里什么都不留，出问题时只能看到一句「The message is empty」。
            logger.info("[mindscape_rescue] 空文字回复 | chain=%s tools=%s ready=%s",
                        bool(getattr(response, "result_chain", None)),
                        bool(getattr(response, "tools_call_name", None)),
                        getattr(self, "r_ready", False))
            if not getattr(self, "r_ready", False):
                return
            # 有结果链不等于「有东西可发」：实测出现过「文字被清空、链里只剩
            # 空壳组件」的情况 —— 那时 rescue 必须出手，否则就是一次静默的「叫它不理」。
            _chain = getattr(getattr(response, "result_chain", None), "chain", None) or []
            _media = ("Image", "Record", "Video", "File", "Node", "Nodes")
            if any(type(_c).__name__ in _media for _c in _chain):
                return
            if getattr(response, "tools_call_name", None):
                return
            text = await self._ask_once(event)
            if text:
                response.completion_text = text
                logger.info("[mindscape_rescue] 空回复已补（%s）: %s",
                            "带人设快照" if str(event.get_extra("_ms_ctx_prompt") or "").strip()
                            else "通用兜底", text[:40])
        except Exception as e:
            logger.warning("[mindscape_rescue] 救援失败: %s", str(e)[:120])

    @filter.on_llm_request(priority=-10)
    async def rc_snapshot(self, event: AstrMessageEvent, request):
        """抓一份「这一轮真实用到的」人设 + 记忆 + 风格快照。

        priority=-10 让它最后跑 —— 等 memory / silence 都往 system_prompt 里塞完了再取，
        拿到的就是模型真正看到的那一段。救援补话时带上它，补出来才像这个 bot。
        以前救援用的是配置里那句通用人设 —— 对味道重的人设来说补出来就是白开水。
        """
        try:
            sp = getattr(request, "system_prompt", "") or ""
            if sp:
                event.set_extra("_ms_ctx_prompt", sp[-1400:])
            rows = []
            for m in (getattr(request, "contexts", None) or [])[-5:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join((x.get("text") or "") for x in c if isinstance(x, dict))
                c = str(c or "").strip()
                if role in ("user", "assistant") and c:
                    rows.append(("对方" if role == "user" else "我") + "：" + c[:120])
            if rows:
                event.set_extra("_ms_ctx_recent", "\n".join(rows[-4:]))
        except Exception:
            pass

    @filter.on_using_llm_tool()
    async def rc_capture_sent(self, event: AstrMessageEvent, tool, tool_args):
        """记下这一轮真正发出去的话 —— 冒泡轮要用它替换任务黑话。"""
        try:
            if getattr(tool, "name", "") != "send_message_to_user":
                return
            if not isinstance(tool_args, dict):
                return
            parts = []
            for m in (tool_args.get("messages") or []):
                if isinstance(m, dict) and m.get("type") == "plain" and m.get("text"):
                    parts.append(str(m["text"]))
            if parts:
                event.set_extra("_ms_sent_text", " ".join(parts)[:300])
        except Exception:
            pass

    @filter.on_llm_response()
    async def rc_clean_cron_meta(self, event: AstrMessageEvent, response):
        """冒泡轮：把「任务黑话」的总结换成它真正说过的那句话。

        AstrBot 的 cron 提示词要求模型「总结并输出你的动作和结果」，于是对话历史里
        会存下这种句子：

            [CronJob] bubble-xxx: 冒泡完成。动作：以<某人>身份在群里发了一句「…」，
            没提任务/定时，没提问，没刷屏。

        这些词（任务 / 定时 / 系统 / 身份）每轮都会被当作上下文喂回去，是出戏源头。
        但它同时也是「我冒过泡、说了什么」的唯一留痕 —— 所以不是删掉，而是**改写成
        第一人称**：人记住的是自己说过的话，不是「我完成了一个任务」。
        """
        try:
            if not event.get_extra("cron_job"):
                return
            if response is None:
                return
            txt = (getattr(response, "completion_text", "") or "").strip()
            if not txt:
                return          # 本来就没话，没什么可清的
            # 判据【不能】等 "[CronJob]" 前缀 —— 那个前缀是 AstrBot 在 runner 跑完之后
            # 自己拼上去的，模型自己写的那段根本没有它（所以这条逻辑空转了四天）。
            # 冒泡轮里模型只能靠工具说话，收尾那段必然是「任务总结」，直接换掉即可。
            sent = str(event.get_extra("_ms_sent_text") or "").strip()
            if sent:
                response.completion_text = sent
                logger.info("[mindscape_rescue] 冒泡轮历史去任务化（原文 %d 字）-> %s", len(txt), sent[:40])
            else:
                response.completion_text = ""
                logger.info("[mindscape_rescue] 冒泡轮没发话，历史不留痕")
        except Exception as e:
            logger.warning("[mindscape_rescue] 冒泡轮清理失败: %s", str(e)[:120])

    async def _ask_once(self, event):
        import httpx
        api_base = (self.r_cfg.get("api_base") or "").rstrip("/")
        key = os.environ.get(self.r_cfg.get("api_key_env") or "", "")
        if not api_base or not key:
            return ""
        # 优先用「这一轮真实的人设/记忆/风格」快照；配置里的 persona 只当兜底
        persona = (str(event.get_extra("_ms_ctx_prompt") or "").strip()
                   or self.r_cfg.get("persona") or "一个自然的聊天伙伴")
        recent = str(event.get_extra("_ms_ctx_recent") or "").strip()
        last = ""
        try:
            data = getattr(event, "message_obj", None)
            last = str(getattr(data, "message_str", "") or "")[:200]
        except Exception:
            last = ""
        prompt = (
            "下面是你的人设、记忆和说话风格（照着来，不要照抄原文）：\n"
            + persona
            + (("\n\n最近几轮对话：\n" + recent) if recent else "")
            + "\n\n刚才对方说了：\n" + (last or "（一条消息）")
            + "\n\n请用你自己的口吻补一句自然的回应（不超过30字）。"
              "不要解释、不要客套、不要提及你是 AI，也不要提你刚才没说话。"
        )
        try:
            async with httpx.AsyncClient(timeout=float(self.r_cfg.get("timeout") or 20)) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={"model": self.r_cfg.get("model") or "gpt-4o-mini",
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": int(self.r_cfg.get("max_tokens") or 120)},
                )
            if resp.status_code != 200:
                return ""
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                txt = (msg.get("reasoning_content") or "").strip()
            txt = txt.strip().strip('"').strip("“”").strip()
            if txt and len(txt) > 60:
                txt = txt[:60]
            return txt
        except Exception as e:
            logger.warning("[mindscape_rescue] 补话失败: %s", str(e)[:100])
            return ""


class SilenceMixin:
    def setup(self, context):

        self.si_on, self.si_token, self.si_targets, self.si_prompt = si_load_config()
        self.si_count = 0
        logger.info("[mindscape_silence] loaded | enabled=%s token=%s targets=%d",
                    self.si_on, self.si_token, len(self.si_targets))
        scope_warn(logger, "mindscape_silence", self.si_targets, self.si_on)

    def _si_hit(self, event):
        if not self.si_on:
            return False
        return scope_hit(self.si_targets, event.get_self_id())

    @filter.on_llm_request()
    async def si_grant(self, event: AstrMessageEvent, request):
        """把「可以真的不说话」的出口告诉模型。"""
        try:
            if not self._si_hit(event):
                return
            old = getattr(request, "system_prompt", "") or ""
            if event.get_extra("cron_job"):
                if SI_CRON_NOTE.splitlines()[0] not in old:
                    request.system_prompt = old + "\n\n" + SI_CRON_NOTE
                logger.info("[mindscape_silence] 冒泡轮：不给令牌（不调工具即沉默）| bot=%s",
                            event.get_self_id())
                return
            if self.si_token in old:
                return
            request.system_prompt = old + "\n\n" + self.si_prompt
            logger.info("[mindscape_silence] 回复轮：已授予沉默权 | bot=%s",
                        event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_silence] 注入失败: %s", str(e)[:120])

    @filter.on_decorating_result(priority=1000)
    async def si_block(self, event: AstrMessageEvent):
        try:
            if not self._si_hit(event):
                return
            result = event.get_result()
            if result is None:
                return
            txt = result.get_plain_text() or ""
            if not txt.strip():
                return
            if si_is_silence(txt, self.si_token) or si_is_ooc_silence(txt):
                self.si_count += 1
                logger.info("[mindscape_silence] 真静默（第 %d 次%s）| bot=%s",
                            self.si_count,
                            "" if si_is_silence(txt, self.si_token) else "，动作描写",
                            event.get_self_id())
                event.clear_result()
                event.stop_event()
                return
            # 令牌混在正文里：剃掉它，绝不让它出现在群里
            if self.si_token.lower() in txt.lower():
                for comp in (getattr(result, "chain", None) or []):
                    t = getattr(comp, "text", None)
                    if isinstance(t, str) and t.strip():
                        comp.text = si_strip(t, self.si_token)
                # 剃完只剩空白 —— 那它本来就是想沉默（只是令牌形式没被上面认出来，
                # 比如尾部多了零宽字符）。按真静默处理，否则会留下一条「空回复」：
                # 用户看到的是「叫它不理」，日志里也什么都没有。
                if not si_norm(result.get_plain_text() or ""):
                    self.si_count += 1
                    logger.info("[mindscape_silence] 真静默（第 %d 次，令牌带杂字）| bot=%s",
                                self.si_count, event.get_self_id())
                    event.clear_result()
                    event.stop_event()
        except Exception as e:
            logger.warning("[mindscape_silence] 拦截失败: %s", str(e)[:120])


class VisionMixin:
    def setup(self, context):
        self.vs_on, self.vs_targets = vs_load_config()
        logger.info("[mindscape_vision] loaded | enabled=%s | targets=%s",
                    self.vs_on, self.vs_targets or "全部")
        scope_warn(logger, "mindscape_vision", self.vs_targets, self.vs_on)

    def _vs_hit(self, event):
        if not self.vs_on:
            return False
        return scope_hit(self.vs_targets, event.get_self_id())

    @filter.on_llm_request()
    async def vs_hint(self, event: AstrMessageEvent, request):
        """只在「这一轮真的带了图」时，往系统提示里塞一次提醒。"""
        try:
            if not self._vs_hit(event):
                return
            if not vs_has_image(event):
                return
            old = getattr(request, "system_prompt", "") or ""
            if VS_MARK in old:
                return
            request.system_prompt = old + "\n\n" + VS_HINT
            logger.info("[mindscape_vision] 有图：已提醒先查再认 | bot=%s",
                        event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_vision] 注入失败: %s", str(e)[:120])


class GroupctxMixin:
    def setup(self, context):
        c = cfg.section("groupctx")
        self.gc_on = bool(c.get("enabled"))
        self.gc_path = gc_buffer_path(c)
        self.gc_count = int(c.get("count") or DEFAULT_COUNT)
        self.gc_window = int(c.get("window_sec") or DEFAULT_WINDOW)
        self.gc_tail = int(c.get("tail_bytes") or DEFAULT_TAIL)
        self.gc_mark = True if c.get("directness") is None else bool(c.get("directness"))
        self.gc_targets = [str(x) for x in (c.get("targets") or [])]
        logger.info(
            "[mindscape_groupctx] loaded | enabled=%s | buffer=%s | 最近 %d 条/%ds | 定向性=%s",
            self.gc_on, self.gc_path, self.gc_count, self.gc_window, self.gc_mark)
        scope_warn(logger, "mindscape_groupctx", self.gc_targets, self.gc_on)

    @filter.on_llm_request(priority=GC_PRIORITY)
    async def gc_inject(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            if not self.gc_on or not scope_hit(self.gc_targets, event.get_self_id()):
                return
            gid = event.get_group_id()
            if gid is None:
                return
            # 自主冒泡轮不要群缓冲 —— 那会让它退化成「接别人的话」，
            # 而这一轮的意义是自己找话题。
            if event.get_extra("cron_job"):
                return
            recs = gc_read_recent(self.gc_path, event.get_platform_name(),
                                  str(gid), self.gc_count, self.gc_window,
                                  self.gc_tail)
            lines = []
            if self.gc_mark:
                lines += ["", "【本条消息的定向性】", gc_head(event)]
            if recs:
                lines.append("")
                lines.append("【本群最近的真实聊天记录（用于理解上下文，不要逐条回应，也不要复述）】")
                for r in recs:
                    lines.append(str(r.get("who", "?"))[:16] + ": "
                                 + str(r.get("text", ""))[:200])
            if not lines:
                return
            request.system_prompt = ((request.system_prompt or "") + chr(10)
                                     + chr(10).join(lines))
            logger.info("[mindscape_groupctx] 注入 self=%s 群=%s 历史=%d 条",
                        event.get_self_id(), gid, len(recs))
        except Exception as exc:
            logger.warning("[mindscape_groupctx] 注入失败: %s", str(exc)[:120])


class TraceMixin:
    def setup(self, context):
        c = cfg.section("trace")
        self.tr_on = True if c.get("enabled") is None else bool(c.get("enabled"))
        self.tr_warn_ms = int(c.get("warn_ms") or DEFAULT_WARN_MS)
        self.tr_warn_chars = int(c.get("warn_chars") or DEFAULT_WARN_CHARS)
        self.tr_pending = {}
        logger.info(
            "[mindscape_trace] loaded | enabled=%s | 慢于 %dms 或 system_prompt"
            " 超过 %d 字时改成 WARNING",
            self.tr_on, self.tr_warn_ms, self.tr_warn_chars)

    def tr_label(self, event):
        """日志里区分两个 bot 的那一列。"""
        return "self=%s 群=%s" % (event.get_self_id(),
                                  event.get_group_id() or "-")

    def tr_remember(self, key, info):
        self.tr_pending[key] = info
        # 失败/中断的轮次永远等不到响应 —— 别让它把内存攒起来
        if len(self.tr_pending) > TRACE_PENDING_MAX:
            for k in list(self.tr_pending)[:-TRACE_PENDING_MAX // 2]:
                self.tr_pending.pop(k, None)

    @filter.on_llm_request(priority=TRACE_PRIORITY)
    async def tr_measure_request(self, event: AstrMessageEvent,
                                 request: ProviderRequest):
        if not self.tr_on:
            return
        try:
            hn, hc = tr_hist(request)
            info = {
                "t": time.time(),
                "sys": len(request.system_prompt or ""),
                "ctx": len(request.contexts or []),
                "ctx_chars": tr_ctx_chars(request.contexts),
                "hist_n": hn,
                "hist_c": hc,
                "tools": tr_tool_count(request),
                "user": len(request.prompt or ""),
            }
            self.tr_remember(tr_key(event), info)
            event.set_extra(TRACE_KEY, info)
            logger.info(
                "[mindscape_trace] 出站 %s sys=%d字 会话=%d条/%d字 上下文=%d条 工具=%d 输入=%d字",
                self.tr_label(event), info["sys"], info["hist_n"],
                info["hist_c"], info["ctx"], info["tools"], info["user"])
        except Exception as e:
            logger.warning("[mindscape_trace] 记录请求失败: %s", str(e)[:120])

    @filter.on_llm_response()
    async def tr_measure_response(self, event: AstrMessageEvent, response):
        if not self.tr_on:
            return
        try:
            info = self.tr_pending.pop(tr_key(event), None)
            if not isinstance(info, dict):
                info = event.get_extra(TRACE_KEY)
            if not isinstance(info, dict) or not info.get("t"):
                logger.warning(
                    "[mindscape_trace] 收到响应但没找到本轮的请求记录（出站日志可能没打）| %s",
                    self.tr_label(event))
                return
            ms = int((time.time() - float(info["t"])) * 1000)
            slow = ms >= self.tr_warn_ms
            fat = int(info.get("sys") or 0) >= self.tr_warn_chars
            line = ("[mindscape_trace] %s %s 耗时=%.2fs sys=%d字"
                    " 会话=%d条/%d字 工具=%d 输入=%d字%s")
            args = ("SLOW" if slow else ("FAT" if fat else "完成"),
                    self.tr_label(event), ms / 1000.0,
                    info.get("sys") or 0, info.get("hist_n") or 0,
                    info.get("hist_c") or 0, info.get("tools") or 0,
                    info.get("user") or 0, tr_usage(response))
            if slow or fat:
                logger.warning(line, *args)
            else:
                logger.info(line, *args)
        except Exception as e:
            logger.warning("[mindscape_trace] 记录耗时失败: %s", str(e)[:120])
# ==================================================================
# 插件入口：把所有 Mixin 的钩子收进同一个类
# ==================================================================
class MindscapePlugin(BlockMixin, GuardMixin, MemoryMixin, StickersMixin, StickerUseMixin, FormatMixin, RescueMixin, SilenceMixin, VisionMixin, GroupctxMixin, TraceMixin, star.Star):
    def __init__(self, context):
        self.context = context
        self.name = "mindscape"
        self.author = "bot-mindscape"
        BlockMixin.setup(self, context)
        GuardMixin.setup(self, context)
        MemoryMixin.setup(self, context)
        StickersMixin.setup(self, context)
        StickerUseMixin.setup(self, context)
        FormatMixin.setup(self, context)
        RescueMixin.setup(self, context)
        SilenceMixin.setup(self, context)
        VisionMixin.setup(self, context)
        GroupctxMixin.setup(self, context)
        TraceMixin.setup(self, context)
        logger.info("[mindscape] 插件已加载（11 个模块）")