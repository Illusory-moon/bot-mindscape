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

# 日期只认「整词就是日期」的形状，免得把「4+1」这种也当成日期。
_DATE_TOKEN = re.compile(r"(?:\d{4}\s*[-/年]\s*)?(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*号?")


def date_key(s):
    """从文本里抠出 (月, 日)。认 2026-09-16 / 09-16 / 9月16号。认不出返回 None。

    日记每条条目头上都带日期（## 2026-09-16 23:57），正文里也可能提到别的日期 ——
    取第一个（也就是头部那个）。
    """
    for m in _DATE_TOKEN.finditer(s or ""):
        mo, dy = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= dy <= 31:
            return mo, dy
    return None


def date_filter(term):
    """这个查询词是不是一个「日期」？是就返回 (月,日)，否则 None。"""
    t = (term or "").strip()
    if not t or len(t) > 12:
        return None
    if not _DATE_TOKEN.fullmatch(t):
        return None
    return date_key(t)


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
            if wants and date_key(text) not in wants:
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
