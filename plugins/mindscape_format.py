# -*- coding: utf-8 -*-
"""mindscape_format —— 沉浸层：输出规范化

问题：prompt 里写了「不要分两段」，但模型不稳定遵守，经常吐出一段空行分段的回复。
方案：发送前做一次硬处理，把多段压成一段，用中文标点连接。
"""
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

PUNCT = "。！？~…，、；："

# 只碰【全角】句号：ASCII 的「.」是数字与版本号（4.6 / 0+0 / PS5.0），
# 碰了就是事故 —— 这一条是硬底线。
PERIODS = ("。", "．")


def drop_period(text):
    """句尾不点标点：末尾的「。」去掉（留空），句中的「。」换成「，」。

    为什么不是一律换成「~」：有些沉重的句子拿波浪号收尾会变味 ——
    规矩是**不用句号表示「说完了」**，不是每句都要卖萌。所以末尾留空，
    中间用逗号接着往下走（和本模块压平多段时的连接符一致）。
    """
    if not text:
        return text
    for ch in PERIODS:
        if ch in text:
            text = "，".join(p for p in (x.strip() for x in text.split(ch)) if p)
    return text


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
        # 句尾去句号：**子选项，空 = 关**（故意不沿用「空 = 全部 bot」那条旧语义，
        # 否则谁忘写一行，全场的句号都被剃光）
        self.f_np = [str(x) for x in (self.f_c.get("no_period") or [])]
        logger.info("[mindscape_format] loaded | %d target(s) | 句尾去句号=%s",
                    len(self.targets), self.f_np or "关")
        scope_warn(logger, "mindscape_format", self.targets)

    @filter.on_decorating_result(priority=900)
    async def flatten_result(self, event: AstrMessageEvent):
        try:
            if not scope_hit(self.targets, event.get_self_id()):
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
            no_period = bool(self.f_np) and scope_hit(self.f_np, event.get_self_id())
            for comp in chain:
                txt = getattr(comp, "text", None)
                if not isinstance(txt, str) or not txt.strip():
                    continue
                new = flatten(txt, join_with, drop, short_len)
                if no_period:
                    new = drop_period(new)
                if new != txt:
                    comp.text = new
        except Exception as e:
            logger.warning("[mindscape_format] 处理失败: %s", str(e)[:120])
