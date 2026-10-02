# -*- coding: utf-8 -*-
"""mindscape_groupctx —— 上下文补齐：把「没唤醒时群友说的话」也带进这一轮

问题：唤醒判定发生在**框架的消息分发阶段**，比插件执行更早 ——
没被唤醒的群消息根本不进 LLM 上下文。于是群友 A 说「我吃了 KFC」（没叫 bot）、
B 说「带我去吃呗」（叫了 bot）时，bot 只看到后半句，回一句「吃什么？」。
上下文不全，回复必然错位。

分工：框架侧补丁（`patches/astrbot`）负责把【所有】群消息落一份到磁盘；
本模块在这一轮真的调 LLM 之前，把该群最近的对话 + 本条消息的「定向性」注进 system_prompt。
两半各管一段，是因为插件在唤醒阶段之后才拿得到消息。

定向性：群里 bot 最常见的两种错 ——
  1) 把群友之间的对话当成对自己说的（尤其低概率冒泡唤醒时）；
  2) 被 @ 了却看不出 —— 框架构建 message_str 时会把「@ 自己」剥掉，
     历史记录里那条只剩「发送者: 内容」。
所以直接把「这句话到底是不是对你说的」写进 prompt。

⚠️ **默认关闭**：它依赖 `patches/astrbot`（写缓冲 + 记 wake_reason）。
   没打补丁就打开，定向性会退化成「这句不是对你说的」—— **比不注入更糟**。
"""
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

# 与 patches/astrbot 里补丁的落盘路径一致（补丁硬编码了这个绝对路径）
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


def gc_quote(event):
    """本条消息引用了什么 —— 返回 (有没有引用, 引用的是不是我自己, 引用里有没有图)。

    为什么必须单独说一句：aiocqhttp 会 get_msg 把**被引用那条的完整组件链**塞进
    Reply.chain，框架还会把引用里的图渲染成 [Image Attachment in quoted message: …]
    混进**本条消息的正文**。模型只看正文，**看不出这张图是谁发的** ——
    实测：有人引用了 bot 自己发的表情包，bot 回头对着自己的图说「诶，这不是我嘛~」。
    但反过来也要说清：**引用别人的图，正是「让她看图」的正规入口之一**
    （先发图、再引用 + @ 她）—— 那种图就该看、该回应，不能一律当成「旧图，别理」。
    """
    msgs = event.get_messages() or []
    me = str(event.get_self_id())
    quoted = mine = has_img = False
    for c in msgs:
        if type(c).__name__ != "Reply":
            continue
        quoted = True
        mine = str(getattr(c, "sender_id", "") or "") == me
        for x in (getattr(c, "chain", None) or []):
            if type(x).__name__ == "Image":
                has_img = True
    if not has_img:                      # 兜底：链里拿不到，就看框架渲染进正文的那句标记
        try:
            if "Image Attachment in quoted message" in str(event.message_str or ""):
                has_img = True
        except Exception:
            pass
    return (quoted, mine, has_img)


def gc_quote_note(mine):
    """引用里那张图是谁发的 —— 只给方向，不写台词。"""
    if mine:
        return ("⚠️ 本条消息**引用的是你自己之前那条（带图或表情包）**：那张图是**你自己发的**，"
                "你本来就知道它长什么样 —— 不用对着它认图、点评、问「这是什么」，"
                "除非有人明确让你聊这张图。")
    return ("⚠️ 本条消息**引用的是别人发的图**：这张图是**别人发出来给你看的**，"
            "该看就看、该接话就接话 —— 别当成你自己发过的东西。")


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
            q_quoted = q_mine = q_img = False
            if self.gc_mark:
                lines += ["", "【本条消息的定向性】", gc_head(event)]
                q_quoted, q_mine, q_img = gc_quote(event)
                if q_quoted and q_img:
                    lines.append(gc_quote_note(q_mine))
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
            logger.info("[mindscape_groupctx] 注入 self=%s 群=%s 历史=%d 条 引用=%s",
                        event.get_self_id(), gid, len(recs),
                        ("自己发的图" if (q_quoted and q_img and q_mine) else
                         "别人的图" if (q_quoted and q_img) else "无"))
        except Exception as exc:
            logger.warning("[mindscape_groupctx] 注入失败: %s", str(exc)[:120])
