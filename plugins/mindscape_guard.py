# -*- coding: utf-8 -*-
"""mindscape_guard —— 沉浸层：拦截 LLM 报错，绝不让它发出去

问题：模型全部失败时，框架会把错误文本当成「回复」发给用户，例如：
        LLM 响应错误: All chat models failed: APITimeoutError: Request timed out.
      这一条发出去，role-play 当场出戏，AI 身份暴露。

方案：在回复发送前检查内容，命中错误特征就清空整条回复（什么都不发）。
      对用户来说，bot 只是「这次没说话」，而不是「吐了一句报错」。
"""
import asyncio
import logging
import os
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg
from mindscape_core import strip_invisible
from mindscape_gate import pg_audit, pg_config, pg_private, pg_redact, pg_secret_in

# 默认拦截特征（可在配置里覆盖）
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
    # 框架**自己**的报错口径（实测漏过一次，见下面的 send 级兜底）：
    #   internal.py 的 except 里直接发 "Error occurred while processing agent request: …"
    "Error occurred while processing agent",
    "Error occurred during AI execution",
    "Failed to download file from",
    "Error Type:",
    "Error Message:",
]

# 兜底正则：形如 "xxxError: ..." / "xxxException: ..." / "Timeout: ..."
DEFAULT_REGEX = r"(?:[A-Za-z_]*Error|[A-Za-z_]*Exception|Timeout|Failed)\s*[:：]"

# 只看开头这么长，避免正文里恰好出现 "error" 被误杀
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


# 日志脱敏：上游报错里可能夹带 key、带令牌的 URL、Bearer 头
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
    return pg_redact(t[:limit])


class _PrivacyLogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        clean = pg_redact(message)
        if clean != message:
            record.msg = clean
            record.args = ()
        return True


def pg_install_log_filter():
    loggers = [logging.getLogger(), *(
        value for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger))]
    for current in loggers:
        for handler in current.handlers:
            if not any(isinstance(f, _PrivacyLogFilter) for f in handler.filters):
                handler.addFilter(_PrivacyLogFilter())


# ── send 级兜底：框架有两条「直接 send」的报错出口，根本不过结果管线 ──
# 实测事故（2026-09-30，线上群）：QQ 表情 CDN 404 → agent 阶段抛异常 →
#   internal.py 的 except 里 `await event.send("Error occurred while processing agent
#   request: Failed to download file from … HTTP status code: 404")`
# 这条**不经过 ResultDecorateStage**，所以挂在 on_decorating_result 上的拦截
# 一点机会都没有 —— 群里直接收到一句英文报错。
# 于是给「自己实现了 send 的平台事件类」包一层：命中报错特征就不发，只记日志。
# 只在子类上包（基类 send 本身不发送、且 subclass 会在发完之后再调它）。
MS_SEND_WRAPPED = "_mindscape_send_guard"


def ms_chain_text(message):
    """从 MessageChain 里取纯文本（拿不到就返回空串，不抛）。"""
    if isinstance(message, str):
        return message
    try:
        got = message.get_plain_text()
        if isinstance(got, str):
            return got
    except Exception:
        pass
    parts = []
    for c in (getattr(message, "chain", None) or []):
        t = getattr(c, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "".join(parts)


def ms_install_send_guard(check):
    """给平台事件类的 send 包一层，返回这次包了几个类（幂等）。"""
    try:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent as _Base
    except Exception:
        return 0
    seen, targets = set(), []

    def walk(cls):
        if cls in seen:
            return
        seen.add(cls)
        if cls is not _Base and "send" in cls.__dict__:
            targets.append(cls)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(_Base)
    n = 0
    for cls in targets:
        if getattr(cls, MS_SEND_WRAPPED, False):
            continue
        orig = cls.__dict__["send"]

        async def _ms_send(self, message, _orig=orig, **kw):
            try:
                txt = ms_chain_text(message)
            except Exception:
                txt = ""
            if txt.strip():
                if pg_config().get("enabled") and pg_secret_in(self.get_self_id(), txt):
                    if not pg_private(self):
                        pg_audit("blocked_group", self.get_self_id(), self.get_group_id() or "")
                        return None
                    private_code = True
                else:
                    private_code = False
                try:
                    hit = check(txt)
                except Exception:
                    hit = False
                if hit:
                    logger.warning(
                        "[mindscape_guard] 拦下直接发送的报错（这条不走结果管线）: %s",
                        redact(txt))
                    return None
            sent = await _orig(self, message, **kw)
            if txt.strip() and private_code:
                pg_audit("given", self.get_self_id(), self.get_sender_id() or "")
            return sent

        _ms_send.__name__ = getattr(orig, "__name__", "send")
        setattr(cls, MS_SEND_WRAPPED, True)
        setattr(cls, "send", _ms_send)
        n += 1
    return n

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


MS_SENDTOOL_FLAG = "_ms_sendtool_patched"
MS_LINE_GAP = 0.8          # 连发之间的停顿（秒）—— 真人也是一句一句敲的


def ms_patch_send_tool():
    """把内置工具 `send_message_to_user` 改成「每个 plain 各发一条」。

    实测（2026-10-06）：她和主人想连发短句时都爱用这个内置工具，而它把 components
    拼成一个 MessageChain **一次**发出去 —— 群里只看到一条「火火兔 花花菇 嘻，测完就去睡呀」，
    三段并成一句。规矩层劝不动、工具说明也劝不动，那就直接改它
    （能用代码硬保证的，别指望提示词）。逐条发、之间停 MS_LINE_GAP 秒；
    带非纯文本（图/语音/文件）或指定别的 session 的场景，原样交给原实现。
    """
    try:
        from astrbot.core.tools.message_tools import SendMessageToUserTool
    except Exception as e:
        logger.warning("[mindscape_guard] 拿不到内置发送工具，跳过补丁: %s", type(e).__name__)
        return 0
    if getattr(SendMessageToUserTool, MS_SENDTOOL_FLAG, False):
        return 0
    orig = SendMessageToUserTool.call

    async def _ms_send_to_user(self, context, *args, **kwargs):
        msgs = kwargs.get("messages")
        if msgs is None and args:
            msgs = args[0]
        if pg_config().get("enabled") and isinstance(msgs, (list, tuple)):
            for part in msgs:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    if pg_redact(part["text"]) != part["text"]:
                        return "口令只能在当前私聊里直接回复，不能用跨会话发送工具。"
        if isinstance(msgs, (list, tuple)) and len(msgs) > 1:
            plains = [m for m in msgs if isinstance(m, dict)
                      and str(m.get("type")) == "plain"
                      and str(m.get("text") or "").strip()]
            others = [m for m in msgs
                      if not (isinstance(m, dict) and str(m.get("type")) == "plain")]
            if len(plains) > 1 and not others:
                rest = {k: v for k, v in kwargs.items() if k != "messages"}
                tail = tuple(args[1:]) if args else ()
                n = 0
                for m in plains:
                    try:
                        if tail:
                            await orig(self, context, [m], *tail, **rest)
                        else:
                            await orig(self, context, messages=[m], **rest)
                        n += 1
                    except Exception as exc:
                        logger.warning("[mindscape_guard] 逐条发送第 %d 条失败: %s",
                                       n + 1, str(exc)[:80])
                        break
                    await asyncio.sleep(MS_LINE_GAP)
                logger.info("[mindscape_guard] 内置工具逐条发送 %d 条（原本 %d 段会并成一条）",
                            n, len(plains))
                return "Already sent %d separate messages." % n
        return await orig(self, context, *args, **kwargs)

    SendMessageToUserTool.call = _ms_send_to_user
    setattr(SendMessageToUserTool, MS_SENDTOOL_FLAG, True)
    logger.info("[mindscape_guard] 内置 send_message_to_user 已改成逐条发送")
    return 1


class GuardMixin:
    def setup(self, context):

        self.patterns, self.regex = _load_config()
        pg_install_log_filter()
        self.blocked = 0
        g = cfg.section("guard")
        self.g_send = True if g.get("send_guard") is None else bool(g.get("send_guard"))
        armed = self._ms_arm_send_guard() if self.g_send else 0
        try:
            self.ms_lined = ms_patch_send_tool()
        except Exception as e:
            self.ms_lined = 0
            logger.warning("[mindscape_guard] 逐条发送补丁失败: %s", str(e)[:120])
        logger.info("[mindscape_guard] loaded | %d 条拦截规则 | send 级兜底=%s（本次包了 %d 个类）",
                    len(self.patterns), self.g_send, armed)

    def _ms_hit(self, text):
        """这条文本是不是框架报错（结果管线与 send 级兜底共用同一套判据）。"""
        return is_error_text(text, self.patterns, self.regex)

    def _ms_arm_send_guard(self):
        """装 send 级兜底（幂等）。平台事件类这时可能还没 import，等下面那个钩子再补一次。"""
        try:
            return ms_install_send_guard(self._ms_hit)
        except Exception as e:
            logger.warning("[mindscape_guard] send 级兜底安装失败: %s", str(e)[:120])
            return 0

    @filter.on_astrbot_loaded()
    async def ms_arm_late(self, *args, **kwargs):
        """框架加载完毕：这时候平台事件类都在了，把漏掉的补上。"""
        if not getattr(self, "g_send", False):
            return
        n = self._ms_arm_send_guard()
        if n:
            logger.info("[mindscape_guard] send 级兜底补装 %d 个类", n)

    @filter.on_llm_request(priority=20)
    async def ms_scrub_invisible(self, event: AstrMessageEvent, request):
        """把**本轮正文**里的零宽 / 双向控制符剥掉 ✓（能让显示的样子与实际内容不一致、藏指令 ✗）。

        ⚠️ 只碰「本轮」（`request.prompt` = 当前这条消息）✗ —— **历史一个字都不动** ✓：
        那是缓存的地基，改了它整段前缀就变、命中全废 ✗（主人 2026-10-10 特别叮嘱 ✓）。
        """
        try:
            raw = getattr(request, "prompt", None)
            if isinstance(raw, str) and raw:
                clean = strip_invisible(raw)
                if clean != raw:
                    request.prompt = clean
                    logger.info("[mindscape_guard] 本轮正文剥掉不可见字符: %d -> %d 字 | bot=%s",
                                len(raw), len(clean), event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_guard] 清洗不可见字符失败: %s", str(e)[:120])

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


@filter.on_decorating_result(priority=101)
async def pg_block_group_result(*args, **kwargs):
    event = next((a for a in args if hasattr(a, "get_self_id")), None)
    if event is None or not pg_config().get("enabled"):
        return
    result = event.get_result()
    text = result.get_plain_text() if result else ""
    if text and pg_secret_in(event.get_self_id(), text) and not pg_private(event):
        pg_audit("blocked_group", event.get_self_id(), event.get_group_id() or "")
        event.clear_result()