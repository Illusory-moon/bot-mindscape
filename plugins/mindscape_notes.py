# -*- coding: utf-8 -*-
"""mindscape_notes —— 认知层：可写的账本

问题：日记是后台任务写的、摘要是后台生成的、检索只读 —— bot 能「承诺」
      （「牌子给你挂上了」），却没有地方**落笔**。于是承诺只活在那一句话里，
      窗口一换就漂了：同一件细节事，问六次能给六个答案。

      而「总结」之所以看起来好，是因为它每次都能重新检索、重新概括；
      「细节」要的是**唯一且稳定**的答案，那必须有个地方存着。

方案：给每个 bot 一本自己能写的 Markdown 账本，条目形如

        - 键：值（来源）

      群里定下什么就当场记；注入时按「最近改动」排在前面。
      这样「总结」继续靠检索，「细节」改看这本账 —— 两件事各用各的入口。

为什么条目要带来源：账本如果只是一堆结论，它自己就会变成新的幻觉源
（把别人的口嗨记成事实）。所以约定记「谁说的、谁认的、有没有人质疑」，
记的是**这件事是怎么定下来的**，而不是「事实是什么」。
"""
import os
import re

from astrbot.api import llm_tool, logger

import mindscape_config as cfg

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
