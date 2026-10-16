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

        # ⭐ 把出站整形登记给 guard 的内置发送工具补丁 ✓
        #   为什么必须做：`send_message_to_user` **直接发消息**，不经过 on_decorating_result
        #   ⇒ 下面那个 flatten_result 看不到它 ⇒ 句号照发 ✗
        #   （2026-10-16 实测：她 13 次调用里 2 条带句尾句号 ✓）
        #   ⚠️ 取法是三元兜底（照本仓通用写法 ✓）：**构建产物是单一大文件**，
        #      跨模块 import 已被 build_plugin 剥掉 ⇒ 合并后它就是个模块级函数，
        #      直接查 globals() 即可；开发态（源码树里逐模块加载）才走 import ✓
        #   ⚠️ 登记的是「取实例的函数」，不是 self —— 补丁可能在 setup 之前就装好，
        #      存 self 会拿到 None；存函数每次现取 ⇒ 不会陈旧 ✓
        try:
            _ms_reg = globals().get("ms_set_text_formatter")
            if _ms_reg is None:
                import sys as _ms_sys
                for _mn, _mm in list(_ms_sys.modules.items()):
                    if _mn.endswith("mindscape_guard"):
                        _ms_reg = getattr(_mm, "ms_set_text_formatter", None)
                        if _ms_reg is not None:
                            break
            if _ms_reg is not None:

                def _ms_format_provider(_get=(lambda: self)):
                    cur = _get()
                    if cur is None:
                        return None
                    return cur.ms_format_text

                _ms_reg(_ms_format_provider)
                logger.info("[mindscape_format] 已登记出站整形（内置发送工具同口径）")
            else:
                logger.warning("[mindscape_format] 找不到 ms_set_text_formatter —— 内置发送工具不会去句号")
        except Exception as e:
            logger.warning("[mindscape_format] 登记出站整形失败: %s", str(e)[:100])

        # ⭐ 把「句尾去句号」的作用域也登记成模块级**单一真值** ✓
        #   给那些**没有 self 的模块级钩子**用（mention 的 ||| 拆分 ✓）——
        #   否则它只能自己再读一遍配置，口径早晚漂移 ✗
        try:
            if "MS_FORMAT_NP" in globals():
                globals()["MS_FORMAT_NP"] = list(self.f_np)
                logger.info("[mindscape_format] 已登记去句号作用域：%s", self.f_np or "关")
        except Exception as e:
            logger.warning("[mindscape_format] 登记去句号作用域失败: %s", str(e)[:100])

    def ms_format_text(self, context, text, kwargs):
        """给「工具直接发话」那条路用的整形：压平 +（作用域内）去掉句尾句号。

        ⚠️ 与 flatten_result **同源同规矩** ✓ —— 两个通道必须一套口径，
        否则同一句话走哪条路结果不一样（2026-10-16 就是这么漏的 ✗）。
        """
        if not isinstance(text, str) or not text.strip():
            return text
        self_id = None
        try:
            self_id = context.context.event.get_self_id()
        except Exception:
            pass
        if self_id is None:
            return text              # 拿不到身份 ⇒ 不猜，原样放行 ✓
        join_with = self.f_c.get("join_with") or "，"
        drop = bool(self.f_c.get("drop_last_if_short", False))
        short_len = int(self.f_c.get("short_len") or 8)
        new = flatten(text, join_with, drop, short_len)
        if self.f_np and scope_hit(self.f_np, self_id):
            new = drop_period(new)
        return new

    @filter.on_decorating_result(priority=900)
    async def flatten_result(self, event: AstrMessageEvent):
        try:
            if not scope_hit(self.targets, event.get_self_id()):
                return
            result = event.get_result()
            # 🔍 一次性诊断（2026-10-16 排查「漏句号」）—— 纯日志、零 token，抓完即撤 ✗
            #    目的：看清【每一轮出站】到底长什么样、走的哪种 result、
            #          以及整形前后有没有句号（grep 标记：msdiag）
            try:
                _before = result.get_plain_text() if result is not None else ""
                _tail = (result.chain[-1].__class__.__name__ if result is not None
                         and getattr(result, "chain", None) else "-")
                logger.warning("[msdiag] flatten_result 出站 self=%s 结果类=%s llm=%s 首类=%s 有句号=%s 原文=%s",
                               event.get_self_id(),
                               type(result).__name__ if result is not None else "None",
                               getattr(result, "is_llm_result", lambda: "?")() if result is not None else "-",
                               _tail,
                               any(x in _before for x in ("。", "．")),
                               _before[:200])
            except Exception:
                pass
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
