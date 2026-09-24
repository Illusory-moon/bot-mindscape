# -*- coding: utf-8 -*-
"""mindscape_silence —— 沉浸层：给 bot「真的可以不说话」的权利

问题：提示词里写着「你可以不回复」，但它实际上没有这个权利 ——
      框架总要把一条回复发出去，于是「没话说」只能演出来，变成
      「（和我无关，安静飘过）」「（默默看着）」这类假装沉默的废话。
      说了话，还多一层出戏。

方案：给它一个真的出口 —— 不想说话时只输出一个令牌（默认 [[silence]]），
      本模块在发送前把整条回复清空，群里一点动静都没有。

分工：本模块只管【回复轮】（有人说话、bot 被唤醒后要不要接）。
      自主冒泡轮不需要它 —— 那一轮只能靠调用工具说话，不调用工具本身就是沉默。
      所以冒泡轮【不发】令牌，只提醒一句「不想说话就别发」。
"""
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg

SI_DEFAULT_TOKEN = "[[silence]]"

SI_PROMPT = """# 沉默的权利
你**真的可以不说话**。这一轮如果你没有任何想说的 —— 不想接、跟你无关、或者就是懒得开口 ——
就**只输出这一行**：

%(token)s

系统会把整条消息丢掉，群里没有任何动静，谁也不会看见这几个字。这是真的沉默，不是假装。

- 只输出它，不要加解释、标点、括号、前后缀，也不要和别的话写在一起
- 不要用「（和我无关，安静飘过）」「（默默看着）」这类话代替它 —— 那是假装沉默，等于还是说了话
- 有话说的时候正常说。这是给你留的退路，不是让你变闷
"""

# 冒泡轮专用：令牌在这条路上会绕过装饰阶段、直接漏进群里，所以不许用
SI_CRON_NOTE = """# 沉默
这一轮是自主冒泡：**不想说话就什么都别发** —— 不要调用 send_message_to_user，直接结束就行。
（沉默令牌只在「有人跟你说话」的那些轮次里用，这一轮不要用它。）
"""

# 包裹符号 + 结尾标点：模型常多吐这些，比对前先剃掉
# 零宽 / 不可见字符：模型时不时会吐出来，会让令牌「看起来一样却匹配不上」
_SI_INVIS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"

_SI_WRAP = "`*_[]【】<>《》（）()" + "“”‘’" + chr(34) + chr(39)
_SI_TAIL = "。.!！?？~～…、,，:：;；"

# 「假装沉默」的动作描写：整条只有这类话 = 它其实不想说话，只是不会用令牌。
# （从旧的 soul_silence 插件合并过来 —— 那套已经停用，但这条正则值得留。）
_SI_OOC_RE = re.compile(
    r'(?:(?:这句我拿不准)?(?:我)?(?:先|就|继续)?(?:安静|默默|悄悄|静静)?地?'
    r'(?:飘过|路过)(?:不冒头|不插话|不打扰|没接(?:这句)?|不接(?:这句)?)?'
    r'|(?:我)?(?:这次|这句|这条)?(?:不冒头|不插话|不接这句|保持沉默|保持安静|不回复))'
    r'(?:了|啦|吧|呢)?')


def si_is_ooc_silence(text):
    """整条回复只是「安静飘过」这类动作描写 —— 等价于想沉默，但用错了表达。"""
    t = re.sub(r"[（）()\\[\\]【】《》\s]", "", text or "").strip()
    if not t:
        return False
    return bool(_SI_OOC_RE.fullmatch(t)) or t.upper() == "NO_REPLY"


def si_norm(text):
    """归一化成可比较的形式（先剃零宽字符，再剃空白 / 包裹符号 / 结尾标点）。"""
    t = (text or "").translate({ord(c): None for c in _SI_INVIS})
    t = t.strip()
    t = t.strip(_SI_WRAP)
    t = t.strip(_SI_TAIL)
    return t.strip(_SI_WRAP).lower()


def si_is_silence(text, token=SI_DEFAULT_TOKEN):
    """整条回复就是令牌 = 这一轮真的不想说话。"""
    key = si_norm(token)
    return bool(key) and si_norm(text) == key


def si_strip(text, token=SI_DEFAULT_TOKEN):
    """把混在正文里的令牌剃掉 —— 绝不让它出现在群里。"""
    if not text or not token:
        return text
    return re.sub(re.escape(token), "", text, flags=re.IGNORECASE).strip()


def si_load_config():
    """从共享配置读设置（读不到就用默认值）。"""
    c = cfg.section("silence")
    token = str(c.get("token") or "").strip() or SI_DEFAULT_TOKEN
    targets = [str(x) for x in (c.get("targets") or [])]
    prompt = str(c.get("prompt") or "").strip() or (SI_PROMPT % {"token": token})
    return bool(c.get("enabled", False)), token, targets, prompt


class SilenceMixin:
    def setup(self, context):

        self.si_on, self.si_token, self.si_targets, self.si_prompt = si_load_config()
        self.si_count = 0
        logger.info("[mindscape_silence] loaded | enabled=%s token=%s targets=%d",
                    self.si_on, self.si_token, len(self.si_targets))

    def _si_hit(self, event):
        if not self.si_on:
            return False
        if not self.si_targets:
            return True
        return str(event.get_self_id()) in self.si_targets

    @filter.on_llm_request()
    async def si_grant(self, event: AstrMessageEvent, request):
        """把「可以真的不说话」的出口告诉模型。"""
        try:
            if not self._si_hit(event):
                return
            old = getattr(request, "system_prompt", "") or ""
            if event.get_extra("cron_job"):
                if SI_CRON_NOTE.splitlines()[0] not in old:
                    request.system_prompt = old + "\n\n" + SI_CRON_NOTE
                logger.info("[mindscape_silence] 冒泡轮：不给令牌（不调工具即沉默）| bot=%s",
                            event.get_self_id())
                return
            if self.si_token in old:
                return
            request.system_prompt = old + "\n\n" + self.si_prompt
            logger.info("[mindscape_silence] 回复轮：已授予沉默权 | bot=%s",
                        event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_silence] 注入失败: %s", str(e)[:120])

    @filter.on_decorating_result(priority=1000)
    async def si_block(self, event: AstrMessageEvent):
        try:
            if not self._si_hit(event):
                return
            result = event.get_result()
            if result is None:
                return
            txt = result.get_plain_text() or ""
            if not txt.strip():
                return
            if si_is_silence(txt, self.si_token) or si_is_ooc_silence(txt):
                self.si_count += 1
                logger.info("[mindscape_silence] 真静默（第 %d 次%s）| bot=%s",
                            self.si_count,
                            "" if si_is_silence(txt, self.si_token) else "，动作描写",
                            event.get_self_id())
                event.clear_result()
                event.stop_event()
                return
            # 令牌混在正文里：剃掉它，绝不让它出现在群里
            if self.si_token.lower() in txt.lower():
                for comp in (getattr(result, "chain", None) or []):
                    t = getattr(comp, "text", None)
                    if isinstance(t, str) and t.strip():
                        comp.text = si_strip(t, self.si_token)
                # 剃完只剩空白 —— 那它本来就是想沉默（只是令牌形式没被上面认出来，
                # 比如尾部多了零宽字符）。按真静默处理，否则会留下一条「空回复」：
                # 用户看到的是「叫它不理」，日志里也什么都没有。
                if not si_norm(result.get_plain_text() or ""):
                    self.si_count += 1
                    logger.info("[mindscape_silence] 真静默（第 %d 次，令牌带杂字）| bot=%s",
                                self.si_count, event.get_self_id())
                    event.clear_result()
                    event.stop_event()
        except Exception as e:
            logger.warning("[mindscape_silence] 拦截失败: %s", str(e)[:120])
