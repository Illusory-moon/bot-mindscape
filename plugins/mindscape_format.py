# -*- coding: utf-8 -*-
"""mindscape_format —— 沉浸层：输出规范化

问题：prompt 里写了「不要分两段」，但模型不稳定遵守，经常吐出一段空行分段的回复。
方案：发送前做一次硬处理，把多段压成一段，用中文标点连接。
"""
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg

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
