# -*- coding: utf-8 -*-
"""mindscape_recall —— 认知层：按需检索长期记忆

注入只能覆盖「最近」一段记忆，更早的事靠检索：
给 bot 一个工具，当有人问「几天前」「上次」的事，它自己去翻日记。
"""
import os
import re

from astrbot.api import llm_tool, logger

import mindscape_config as cfg

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


def split_terms(keyword):
    """把查询词拆成一组近义词：空格、逗号、顿号、斜杠都算分隔。"""
    return [x.strip() for x in re.split(r"[\s,，、/|]+", keyword or "") if x.strip()]


def search_diary(path, keyword, limit=DEFAULT_LIMIT, scan_lines=SCAN_LINE_CAP,
                 full=False):
    """在记忆中检索相关条目（混合检索：精确 + 模糊）。

    返回 (行列表, 真实命中总数)。

    为什么要单独返回总数：模型只看到「给了它几条」，就会把这几条当成全部。
    实测群里问「现在有几对纯爱」，它只翻到最近的一条就答「一对」——
    而「情侣」在日记里命中 37 条。把总数单独告诉它，它才知道自己没看全。

    为什么要接受多个词：提问用的词往往不是记日记时用的词。实测同一件事，
    「纯爱」只命中 4 条，「情侣」命中 37 条。所以关键词允许给一组近义词，
    命中任意一个都算。
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


def _as_bool(v):
    """模型给的布尔值可能是字符串（"true" / "是"），bool("false") 会是 True。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是", "对", "要")


@llm_tool(name="recall_memory")
async def recall_memory(*args, **kwargs):
    """翻自己的长期记忆，回忆过去发生过的事。

    你眼前只有**最近一小段**记忆，不是全部 —— 手头没有，不等于没发生过。
    所以要「数数/汇总」（几对、几个、都有谁、一共几次、谁最……）、要「追溯」
    （以前、上次、第一次、这几天、有没有过），或者你打算回答「只有」「就这些」
    「没有」的时候，都必须先用这个工具查一遍再开口。

    返回的是你当时记下的原话，可以自然地讲出来，别照本宣科念。

    Args:
        keyword(string): 搜索关键词。可以给**一组近义词**，用空格或逗号分开
            （比如「纯爱 情侣 登记」）—— 你记日记时用的词，和对方问话时用的词
            经常不一样，多给几个才不会漏
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
    if full:
        head = ("以下是记忆里**全部** %d 条与「%s」有关的记录（按相关度排序）："
                % (total, kw))
    elif total > len(hits):
        # 关键的一句：让模型知道自己没看全，而不是把「看到的」当成「全部」
        head = ("以下是记忆里与「%s」有关的记录（共命中 %d 条，这里只给你最相关的 %d 条）："
                % (kw, total, len(hits)))
    else:
        head = "以下是记忆里与「%s」有关的记录（共 %d 条，已全部列出）：" % (kw, total)
    tail = ("\n\n⚠️ 只依据上面的记录回答。记录里没提到的人或事，就说想不起来，"
            "绝对不要凭印象补充细节。")
    if not full and total > len(hits):
        tail += ("\n⚠️ 上面不是全部（共命中 %d 条）。如果对方问的是「几对/几个/都有谁」"
                 "这种要数数的，用 full=true 再查一次，否则一定数漏。" % total)
    return head + "\n" + "\n".join(hits) + tail