# -*- coding: utf-8 -*-
"""mindscape_vision —— 表达层：有图时强制「先查再认」

需求（2026-09-29）：
  本 bot 对自己的识图**太自信**，认错概率很大。
  实例：有人发了一张双人图问「她们是同事嘛？」，**它把其中一个角色认成了自己**。

  规则：认出图里是谁之前**必须先查资料库 + 联网搜**；
  **查完还是不确定** → 用「画面太糊 / 看不清」这个**方向**糊过去。
  主人原话：**糊弄 + 诚实承认 的效果 ＞ 瞎编**。

两条刻意的设计：
  1. 触发点是「**查完还不确定**」，不是「查不到」—— 查到了但不足以确认，
     和完全查不到，对模型来说都是**不该硬认**的状态，前者更常见。
  2. **兜底那句话不写死**。只给方向（承认画面看不清），措辞交给她自己。
     写死一句原文，模型会照抄 —— 这个坑在 mindscape_stickers 里已经踩过
     （prompt 里的示例被原样抄进产出）。也符合 canon/_05 原则五：不把话写死。

为什么要有这个钩子，而不是只写进 rules：
  项目自己的教训（canon/_10）——「**能用代码硬保证的，别指望提示词**」。
  rules 是常驻的，遇到图时不一定被想起来；这个钩子**只在真有图时**注入，
  既省预算、也不容易被忽略。

配置（config.yaml）：
    vision:
      enabled: true
      targets: ["<bot 的 QQ 号>"]   # 留空 = 所有 bot 都生效
"""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image

import mindscape_config as cfg

VS_MARK = "这一轮的消息里带了图"
VS_HINT = """## 这一轮的消息里带了图 —— 认人之前先查

- 图里的人物 / 作品，**先用 lookup_knowledge 查，查完还不确定就联网搜**；
  不要凭印象直接认。
- **查完还是不确定**是谁，就用你自己的口吻糊过去 —— 大意是「画面太糊、看不清」，
  **具体怎么说按你平常的说话方式来，别照抄这句**。
  糊弄 + 老实承认看不清，永远好过瞎编一个名字。
- 没有依据之前，**绝不说**「这就是 XX」。认错一个人，比说不认识难看得多。
"""


def vs_load_config():
    c = cfg.section("vision") or {}
    on = bool(c.get("enabled"))
    targets = [str(x) for x in (c.get("targets") or [])]
    return on, targets


def vs_has_image(event):
    """这一轮的消息里有没有图（只看顶层组件）。"""
    comps = getattr(getattr(event, "message_obj", None), "message", None) or []
    for c in comps:
        if isinstance(c, Image):
            return True
    return False


class VisionMixin:
    def setup(self, context):
        self.vs_on, self.vs_targets = vs_load_config()
        logger.info("[mindscape_vision] loaded | enabled=%s | targets=%s",
                    self.vs_on, self.vs_targets or "全部")

    def _vs_hit(self, event):
        if not self.vs_on:
            return False
        if not self.vs_targets:
            return True
        return str(event.get_self_id()) in self.vs_targets

    @filter.on_llm_request()
    async def vs_hint(self, event: AstrMessageEvent, request):
        """只在「这一轮真的带了图」时，往系统提示里塞一次提醒。"""
        try:
            if not self._vs_hit(event):
                return
            if not vs_has_image(event):
                return
            old = getattr(request, "system_prompt", "") or ""
            if VS_MARK in old:
                return
            request.system_prompt = old + "\n\n" + VS_HINT
            logger.info("[mindscape_vision] 有图：已提醒先查再认 | bot=%s",
                        event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_vision] 注入失败: %s", str(e)[:120])
