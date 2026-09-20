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
import sys
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


_CONFIG_CACHE = None


def config_path():
    return os.environ.get("MINDSCAPE_CONFIG") or os.path.expanduser("~/.mindscape/config.yaml")


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
            try:
                import logging
                logging.getLogger("mindscape").warning(
                    "配置读取失败 path=%s type=%s", path, type(e).__name__)
            except Exception:
                pass
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
    path = os.environ.get("MINDSCAPE_CONFIG", os.path.expanduser("~/.mindscape/config.yaml"))
    if not os.path.exists(path):
        return DEFAULT_PATTERNS, DEFAULT_REGEX
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        g = (cfg.get("guard") or {})
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
            sc = max(score_line(text, t) for t in terms)
            if sc > 0:
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
            —— 你记日记时用的词，和对方问话时用的词经常不一样，多给几个才不会漏
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

    user = "群聊记录：\n" + "\n".join(
        "[" + m["time"] + "][" + m["gname"] + "] " + m["who"] + ": " + m["txt"] for m in msgs
    )
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
            {"role": "user", "content": user[:max_input_chars]},
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
    with open(path, "w", encoding="utf-8") as f:
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
        done = 0
        for i in range(0, len(rows), batch):
            if done >= max_batches:
                print("[mindscape_diary] 已达单次上限 %d 批，剩余 %d 条留待下次"
                      % (max_batches, len(rows) - i))
                break
            chunk = rows[i:i + batch]
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
            if entries:
                with open(out_file, "a", encoding="utf-8") as fp:
                    fp.write("## " + stamp + "\n")
                    for e in entries:
                        fp.write("- " + str(e) + "\n")
                    fp.write("\n")
                total_added += len(entries)
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

    total_added = 0
    for i in range(0, len(rows), batch):
        if (i // batch) >= max_batches:
            print("[mindscape_learn] 已达单次上限 %d 批，剩余 %d 条留待下次"
                  % (max_batches, len(rows) - i))
            break
        chunk = rows[i:i + batch]
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


DEFAULT_MAX_MB = 2.0


DEFAULT_LOG = "./data/janitor.log"


def jn_abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def log(msg, path=None):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if path:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


IDENT_RE = None


def _safe_ident(name, fallback):
    """只允许字母数字下划线，避免把奇怪的表名/字段名拼进 SQL。"""
    import re as _re
    n = str(name or "")
    return n if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) else fallback


def clean(db, table, column, max_mb, log_path=None):
    """清理含图片的会话 + 超大行。返回 (删图片数, 删超大数, 清理前MB, 清理后MB)。"""
    if not db or not os.path.exists(db):
        log("数据库不存在，跳过: %s" % db, log_path)
        return (0, 0, 0.0, 0.0)
    table = _safe_ident(table, "conversations")
    column = _safe_ident(column, "content")
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    cur = con.cursor()
    before = list(cur.execute(
        "SELECT COALESCE(SUM(length(CAST(%s AS BLOB))),0) FROM %s" % (column, table)
    ))[0][0]

    cur.execute(
        "DELETE FROM %s WHERE %s LIKE '%%data:image%%'" % (table, column)
    )
    n_img = cur.rowcount
    cur.execute(
        "DELETE FROM %s WHERE length(CAST(%s AS BLOB)) > ?" % (table, column),
        (int(max_mb * 1048576),),
    )
    n_big = cur.rowcount
    con.commit()

    after = list(cur.execute(
        "SELECT COALESCE(SUM(length(CAST(%s AS BLOB))),0) FROM %s" % (column, table)
    ))[0][0]

    if n_img or n_big:
        try:
            cur.execute("VACUUM")
            con.commit()
        except Exception as e:
            log("VACUUM 失败: %s" % str(e)[:80], log_path)
    con.close()
    return (n_img, n_big, before / 1048576.0, after / 1048576.0)


def jn_main():
    c = cfg.section("janitor")
    if not c:
        print("[mindscape_janitor] 未找到 janitor 配置，跳过")
        return
    db = jn_abs(c.get("db"))
    table = c.get("table") or "conversations"
    column = c.get("column") or "content"
    max_mb = float(c.get("max_mb") or DEFAULT_MAX_MB)
    log_path = jn_abs(c.get("log") or DEFAULT_LOG)

    n_img, n_big, before, after = clean(db, table, column, max_mb, log_path)
    if n_img or n_big:
        log("清理: 图片会话 %d | 超大行 %d | %.2f MB -> %.2f MB"
            % (n_img, n_big, before, after), log_path)
    else:
        log("无需清理 | 当前 %.2f MB" % after, log_path)


if __name__ == "__main__":
    try:
        jn_main()
    except Exception as e:
        print("[mindscape_janitor] 失败: %s" % str(e)[:200])
        sys.exit(1)
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
        self.seen = self._load_seen()
        self._bg = set()            # 后台采集任务，留引用防被 GC 掉
        logger.info(
            "[mindscape_stickers] loaded | prob=%.2f | 已见 %d 张",
            float(self.s_c.get("sample_prob", 0.10)), len(self.seen),
        )

    def _load_seen(self):
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return set()
        if not isinstance(d, list):
            return set()
        keys = set(d)
        # 自愈：去重表的条目在「图库条目被删」之后不会跟着删，于是变成**墓碑** ——
        # 那张图再发一次也不会被采集，用户看到的是「删掉以后就再也收不回来」。
        #
        # 判据必须用**内容的 md5**，不能拿文件名前缀凑：导入脚本会把文件重命名成
        # 「<前缀>_xxxx.gif」，前缀就不再是 md5 了 —— 用前缀匹配会把真实存在的图
        # 误判成墓碑，去重记录一丢，那张图重发就会以原名再入一份（造出重复）。
        # ponytail: 这里把整个图库哈希一遍（目前 51 张 / 54MB，约 0.2s，只在启动时做）。
        #            图库涨到几百 MB 就该改成 sidecar 的 md5 清单。
        try:
            live = set()
            for x in (load_index(self.index_path) or []):
                fn = str(x.get("file") or "")
                if not fn:
                    continue
                with open(os.path.join(self.dir, fn), "rb") as fp:
                    live.add(str(x.get("category") or "") + ":"
                             + hashlib.md5(fp.read()).hexdigest())
            kept = set()
            for k in keys:
                if k in live:
                    kept.add(k)
            if kept != keys:
                self.seen = kept
                self._save_seen()
                logger.info("[mindscape_stickers] 去重表自愈：%d -> %d（清掉 %d 条墓碑）",
                            len(keys), len(kept), len(keys) - len(kept))
            return kept
        except Exception:
            return keys

    def _save_seen(self):
        try:
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump(sorted(self.seen)[-3000:], f)
        except Exception:
            pass

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
                self.seen.add(key)      # 记下：同一张不必反复下载重判
                self._save_seen()
                logger.info("[mindscape_stickers] 跳过 %dx%d（超过 %d，疑似截图/壁纸）",
                            w, ih, max_side)
                return

        verdict = await self._judge(path)
        if verdict is None:
            # 判定失败（超时/无 key/解析失败）：本次不记为已见，允许下次重试
            return
        if not verdict.get("related"):
            # 明确判定为「不相关」：认为已处理，不再重复消耗 API
            self.seen.add(key)
            self._save_seen()
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

        self.seen.add(key)          # 只有真正入库成功才记为已见
        self._save_seen()
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
        logger.info("[mindscape_format] loaded | %d target(s)", len(self.targets))

    @filter.on_decorating_result(priority=900)
    async def flatten_result(self, event: AstrMessageEvent):
        try:
            if self.targets and str(event.get_self_id()) not in self.targets:
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
            for comp in chain:
                txt = getattr(comp, "text", None)
                if not isinstance(txt, str) or not txt.strip():
                    continue
                new = flatten(txt, join_with, drop, short_len)
                if new != txt:
                    comp.text = new
        except Exception as e:
            logger.warning("[mindscape_format] 处理失败: %s", str(e)[:120])


class RescueMixin:
    def setup(self, context):
        self.r_cfg = cfg.section("rescue") or {}
        logger.info("[mindscape_rescue] loaded | %s",
                    "启用" if self.r_cfg.get("enabled", True) else "关闭")

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
            if getattr(response, "result_chain", None):
                return
            if getattr(response, "tools_call_name", None):
                return
            text = await self._ask_once(event)
            if text:
                response.completion_text = text
                logger.info("[mindscape_rescue] 空回复已补: %s", text[:40])
        except Exception as e:
            logger.warning("[mindscape_rescue] 救援失败: %s", str(e)[:120])

    async def _ask_once(self, event):
        import httpx
        api_base = (self.r_cfg.get("api_base") or "").rstrip("/")
        key = os.environ.get(self.r_cfg.get("api_key_env") or "", "")
        if not api_base or not key:
            return ""
        persona = self.r_cfg.get("persona") or "一个自然的聊天伙伴"
        last = ""
        try:
            data = getattr(event, "message_obj", None)
            last = str(getattr(data, "message_str", "") or "")[:200]
        except Exception:
            last = ""
        prompt = (
            "你是" + persona + "。刚才群友说了：\n"
            + (last or "（一条消息）")
            + "\n\n请用一句话自然回应（不超过30字），不要解释、不要客套、不要提及你是 AI。"
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
# ==================================================================
# 插件入口：把所有 Mixin 的钩子收进同一个类
# ==================================================================
class MindscapePlugin(GuardMixin, MemoryMixin, StickersMixin, StickerUseMixin, FormatMixin, RescueMixin, star.Star):
    def __init__(self, context):
        self.context = context
        self.name = "mindscape"
        self.author = "bot-mindscape"
        GuardMixin.setup(self, context)
        MemoryMixin.setup(self, context)
        StickersMixin.setup(self, context)
        StickerUseMixin.setup(self, context)
        FormatMixin.setup(self, context)
        RescueMixin.setup(self, context)
        logger.info("[mindscape] 插件已加载（6 个模块）")