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
]

# 兜底正则：形如 "xxxError: ..." / "xxxException: ..." / "Timeout: ..."
DEFAULT_REGEX = r"(?:[A-Za-z_]*Error|[A-Za-z_]*Exception|Timeout|Failed)\s*[:：]"

# 只看开头这么长，避免正文里恰好出现 "error" 被误杀
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