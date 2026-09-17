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
import sys
import time
import urllib.request
from astrbot.api import llm_tool, logger
from astrbot.api import llm_tool, logger, star
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


SECTION_TITLE = "## 你的长期记忆"


HEADER_MARK = "## "


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
            # 单块就超预算：整块丢弃（宁缺勿断）
            break
        picked.insert(0, text)
        used += add
    return "\n".join(picked)


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
    return out[:max_chars]


DEFAULT_LIMIT = 15


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


def search_diary(path, keyword, limit=DEFAULT_LIMIT, scan_lines=SCAN_LINE_CAP):
    """在记忆中检索相关条目，按相关度排序返回（混合检索：精确 + 模糊）。

    返回 [时间] 内容 形式的字符串列表，相关度高的在前。
    """
    if not path or not os.path.exists(path):
        return []
    kw = (keyword or "").strip()
    if not kw:
        return []
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
            sc = score_line(head + " " + body, kw)
            if sc > 0:
                scored.append((sc, n, "[%s] %s" % (head, body)))
    if not scored:
        return []
    # 先按分数降序；同分时越新越靠前
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [s[2] for s in scored[:limit]]


def _first_str(args):
    for a in args or ():
        if isinstance(a, str) and a.strip():
            return a.strip()
    return ""


@llm_tool(name="recall_memory")
async def recall_memory(*args, **kwargs):
    """翻自己的长期记忆，回忆过去发生过的事。

    当有人问你「几天前」「上次」「之前」「还记得吗」的事情，
    或者你自己觉得应该知道却想不起来时，用这个工具查一查。
    返回的是你当时记下的原话，可以自然地讲出来，别照本宣科念。

    Args:
        keyword(string): 搜索关键词，比如人名、事件、话题
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
    hits = search_diary(path, kw)
    if not hits:
        # 找不到就明确说找不到 —— 这是防幻觉的第一道闸
        return ("翻了翻记忆，没有找到跟「%s」有关的记录。"
                "如果对方坚持说有，就直接说你想不起来了，不要编。") % kw
    # 第二道闸：给出记录的同时约束「只能用这些」
    return ("以下是记忆里与「%s」有关的记录（按相关度排序，共 %d 条）：\n%s\n\n"
            "⚠️ 只依据上面的记录回答。记录里没提到的人或事，就说想不起来，"
            "绝对不要凭印象补充细节。") % (kw, len(hits), "\n".join(hits))


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


def _abs(path):
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


def fetch(src, target, since_ts, since_seq):
    """从 SQLite 增量读取消息（表名/字段名来自配置）。"""
    db = _abs(src.get("db"))
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
            if uid and uid == self_id:
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


def call_llm(llm, persona, msgs, max_input_chars, max_tokens):
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("diary.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")

    user = "群聊记录：\n" + "\n".join(
        "[" + m["time"] + "][" + m["gname"] + "] " + m["who"] + ": " + m["txt"] for m in msgs
    )
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": persona or DEFAULT_PERSONA},
            {"role": "user", "content": user[:max_input_chars]},
        ],
        "temperature": 0.7,
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
    if not isinstance(parsed, dict) or "diary" not in parsed:
        return None
    entries = parsed.get("diary")
    if not isinstance(entries, list):
        return None
    return parsed


def _update_people(path, people, now):
    """把人物画像合并进 people.md（同名覆盖，保留最近时间）。"""
    existing = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("- ") and "：" in line:
                    k, v = line[2:].split("：", 1)
                    existing[k.strip()] = v.strip()
    except Exception:
        pass
    for k, v in people.items():
        key = str(k).strip()
        if key and str(v).strip():
            existing[key] = str(v).strip()[:80]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 你认识的人（自动维护）\n\n")
        f.write("最后更新：" + now + "\n\n")
        for k in sorted(existing):
            f.write("- " + k + "：" + existing[k] + "\n")


def run_target(d):
    """处理一个 bot 的日记，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 40)
    max_in = int(d.get("max_input_chars") or 14000)
    max_tok = int(d.get("max_tokens") or 900)

    total_read = total_added = 0
    for target in (d.get("targets") or []):
        out_file = _abs(target.get("output"))
        state_file = _abs(target.get("state") or (out_file + ".state.json"))
        st = load_state(state_file)
        rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0))
        if not rows:
            continue
        total_read += len(rows)
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            try:
                res = call_llm(llm, target.get("persona"), chunk, max_in, max_tok)
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
            if entries:
                with open(out_file, "a", encoding="utf-8") as fp:
                    fp.write("## " + now + "\n")
                    for e in entries:
                        fp.write("- " + str(e) + "\n")
                    fp.write("\n")
                total_added += len(entries)
            # 人物画像：单独落盘（后出现的描述覆盖旧的）
            people = res.get("people") or {}
            if isinstance(people, dict) and people:
                people_file = target.get("people") or (out_file.rsplit(".", 1)[0] + ".people.md")
                people_file = _abs(people_file)
                _update_people(people_file, people, now)
            st["since_ts"] = chunk[-1]["ts"]
            st["since_seq"] = chunk[-1]["seq"]
            save_state(state_file, st)
    return total_read, total_added


def main():
    d = cfg.section("diary")
    if not d:
        print("[mindscape_diary] 未找到 diary 配置，跳过")
        return
    read, added = run_target(d)
    print("[mindscape_diary] 读取 %d 条消息，新增 %d 条日记" % (read, added))


if __name__ == "__main__":
    main()


_INSTANCE = None


def _NS():
    return _INSTANCE


def _abs(path):
    """相对路径按「配置文件所在目录」解析。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


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


def _abs(path):
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


def main():
    c = cfg.section("janitor")
    if not c:
        print("[mindscape_janitor] 未找到 janitor 配置，跳过")
        return
    db = _abs(c.get("db"))
    table = c.get("table") or "conversations"
    column = c.get("column") or "content"
    max_mb = float(c.get("max_mb") or DEFAULT_MAX_MB)
    log_path = _abs(c.get("log") or DEFAULT_LOG)

    n_img, n_big, before, after = clean(db, table, column, max_mb, log_path)
    if n_img or n_big:
        log("清理: 图片会话 %d | 超大行 %d | %.2f MB -> %.2f MB"
            % (n_img, n_big, before, after), log_path)
    else:
        log("无需清理 | 当前 %.2f MB" % after, log_path)


if __name__ == "__main__":
    try:
        main()
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

            mem = read_recent(path, max_chars)
            if len(mem) < min_chars:
                return

            old = getattr(request, "system_prompt", "") or ""
            if SECTION_TITLE in old:
                return

            label = bot.get("name") or "你"
            block = (
                "\n\n" + SECTION_TITLE + "\n"
                "以下是你自己记下来的往事，是你亲身经历的，可以自然地提起，"
                "但不要照本宣科地念，也不要说「根据我的记忆」这种话。\n\n"
                + mem
            )

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
            logger.info("[mindscape_memory] %s 注入 %d 字记忆 / %d 字人物",
                        label, len(mem), len(people))
        except Exception as e:
            logger.warning("[mindscape_memory] 注入失败: %s", str(e)[:120])


class StickersMixin:
    def setup(self, context):

        self.s_c = cfg.section("stickers")
        self.dir = _abs(self.s_c.get("dir") or "./data/stickers")
        self.index_path = _abs(self.s_c.get("index") or os.path.join(self.dir, "index.json"))
        self.seen_path = _abs(self.s_c.get("seen") or os.path.join(self.dir, "seen.json"))
        os.makedirs(self.dir, exist_ok=True)
        self.seen = self._load_seen()
        logger.info(
            "[mindscape_stickers] loaded | prob=%.2f | 已见 %d 张",
            float(self.s_c.get("sample_prob", 0.10)), len(self.seen),
        )

    def _load_seen(self):
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                return set(d)
        except Exception:
            pass
        return set()

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
                await self._handle(comp, category)
                return
        except Exception as e:
            logger.warning("[mindscape_stickers] 采集失败: %s", str(e)[:140])

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
        with open(path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode()
        low = path.lower()
        mime = "image/gif" if low.endswith(".gif") else ("image/png" if low.endswith(".png") else "image/jpeg")
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
        self.dir = _abs(self.u_c.get("dir") or "./data/stickers")
        self.index_path = _abs(self.u_c.get("index") or os.path.join(self.dir, "index.json"))
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
        from astrbot.core.message.components import Image
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
        img = None
        for c in comps:
            if isinstance(c, Image):
                img = c
                break
        if img is None:
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
# ==================================================================
# 插件入口：把所有 Mixin 的钩子收进同一个类
# ==================================================================
class MindscapePlugin(GuardMixin, MemoryMixin, StickersMixin, StickerUseMixin, FormatMixin, star.Star):
    def __init__(self, context):
        self.context = context
        self.name = "mindscape"
        self.author = "bot-mindscape"
        GuardMixin.setup(self, context)
        MemoryMixin.setup(self, context)
        StickersMixin.setup(self, context)
        StickerUseMixin.setup(self, context)
        FormatMixin.setup(self, context)
        logger.info("[mindscape] 插件已加载（5 个模块）")