# -*- coding: utf-8 -*-
"""mindscape_guard —— 沉浸层：拦截 LLM 报错，绝不让它发出去

问题：模型全部失败时，框架会把错误文本当成「回复」发给用户，例如：
        LLM 响应错误: All chat models failed: APITimeoutError: Request timed out.
      这一条发出去，role-play 当场出戏，AI 身份暴露。

方案：在回复发送前检查内容，命中错误特征就清空整条回复（什么都不发）。
      对用户来说，bot 只是「这次没说话」，而不是「吐了一句报错」。
"""
import os
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg

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
    return t[:limit]


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
                try:
                    hit = check(txt)
                except Exception:
                    hit = False
                if hit:
                    logger.warning(
                        "[mindscape_guard] 拦下直接发送的报错（这条不走结果管线）: %s",
                        redact(txt))
                    return None
            return await _orig(self, message, **kw)

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


class GuardMixin:
    def setup(self, context):

        self.patterns, self.regex = _load_config()
        self.blocked = 0
        g = cfg.section("guard")
        self.g_send = True if g.get("send_guard") is None else bool(g.get("send_guard"))
        armed = self._ms_arm_send_guard() if self.g_send else 0
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