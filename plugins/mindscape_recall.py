# -*- coding: utf-8 -*-
"""mindscape_recall —— 认知层：按需检索长期记忆

注入只能覆盖「最近」一段记忆，更早的事靠检索：
给 bot 一个工具，当有人问「几天前」「上次」的事，它自己去翻日记。
"""
import os

from astrbot.api import llm_tool, logger

import mindscape_config as cfg

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


# 常见虚词：模糊匹配时忽略，否则「的了是不」会把所有行都命中
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