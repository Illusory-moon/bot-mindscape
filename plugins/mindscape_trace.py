# -*- coding: utf-8 -*-
"""mindscape_trace —— 观测层：每一轮记一条「请求多大 / 模型多久」

为什么需要这一层：「回复变慢了」是个感受，不是数据 —— 慢在记忆注入？
群上下文？工具轮？还是模型本身？事后拿日志时间戳去拼「上一行到 Prepare to send」，
拼出来的结论经不起推敲（工具轮、多段发送都会把行数打乱）。

这一层做两件事：
  1. 在所有注入模块【都跑完之后】量一次出站体积（system_prompt / 上下文 / 工具）；
  2. 在模型回来时量一次真实耗时与 token 用量。
两次各一行日志：有了「出站」却没有对应的「完成」，那一轮就是卡住或失败了的。

起始时刻存在**插件实例**里（按会话键），不只存在 event 上 —— 请求与响应拿到的
event 不保证是同一个对象，只挂 extras 会静默丢记录（本鱼实测踩过：一条日志都不出）。

只记**尺寸与耗时**，不记任何正文 —— 日志里不该出现聊天内容。
"""
import json
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg

# 数值越小越晚跑（框架按 priority 倒序派发）—— 排在所有注入模块之后，
# 量到的才是真正发出去的体积。
TRACE_PRIORITY = -100
TRACE_KEY = "_mindscape_trace_t0"
TRACE_PENDING_MAX = 64        # 没等到响应的残留记录最多留这么多（防无限增长）
DEFAULT_WARN_MS = 8000        # 慢于此 → 升级成 WARNING，方便 grep
DEFAULT_WARN_CHARS = 20000    # system_prompt 超过这个字数 → 疑似异常注入
                              # （实测这个 bot 的常态是 ~13800：人格正文 + 记忆三层 + 15 条群记录）


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
