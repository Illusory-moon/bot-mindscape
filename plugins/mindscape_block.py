# -*- coding: utf-8 -*-
"""mindscape_block —— 入口层：前置黑名单

需求（2026-09-29）：
  群里可能有**别的 bot**，一检测到关键词就自动 @ + 生成图片，
  本 bot 会把那些图当成表情包收进库里。要求：**不回它的消息，也不收它的图**。

为什么必须是【前置】拦截：
  「不收图」这半边靠 LLM 阶段的拦截做不到 —— 图片在**更早**的消息事件阶段
  就已经被采集器拿走了（mindscape_stickers / mindscape_sticker_use 都挂在
  @filter.event_message_type 上，priority 用默认值 0）。
  所以这里用 priority=999 + event.stop_event()，让整条消息**在任何插件看到它之前**消失。

配置（config.yaml）：
    blocklist:
      enabled: true
      targets:
        - self_id: "<bot 的 QQ 号>"      # 哪个 bot 生效（可多个，互不牵连）
          users: ["<要屏蔽的 QQ 号>"]
          note: "自动 @ + 发图的群友或 bot"
"""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.event_message_type import EventMessageType

import mindscape_config as cfg

# 越大越早执行。采集器是默认 0，所以一定排在我们后面。
BL_PRIORITY = 999


def bl_load_config():
    c = cfg.section("blocklist") or {}
    on = bool(c.get("enabled"))
    table = {}
    for t in (c.get("targets") or []):
        sid = str(t.get("self_id") or "")
        users = {str(u).strip() for u in (t.get("users") or []) if str(u).strip()}
        if sid and users:
            table[sid] = users
    return on, table


class BlockMixin:
    def setup(self, context):
        self.bl_on, self.bl_table = bl_load_config()
        self.bl_count = 0
        logger.info("[mindscape_block] loaded | enabled=%s | bots=%s",
                    self.bl_on, {k: len(v) for k, v in self.bl_table.items()})

    @filter.event_message_type(EventMessageType.ALL, priority=BL_PRIORITY)
    async def bl_pre_block(self, event: AstrMessageEvent):
        """命中黑名单 → 终止事件传播：不回复、不采集图片、不进任何插件。"""
        try:
            if not self.bl_on:
                return
            users = self.bl_table.get(str(event.get_self_id()))
            if not users:
                return
            sender = str(event.get_sender_id())
            if sender not in users:
                return
            self.bl_count += 1
            logger.info("[mindscape_block] 前置拦截 | bot=%s sender=%s（第 %d 次）",
                        event.get_self_id(), sender, self.bl_count)
            event.stop_event()
        except Exception as e:
            # 拦截逻辑出错时放行 —— 宁可漏拦，也不能把正常消息吞掉
            logger.warning("[mindscape_block] 拦截异常，已放行: %s", str(e)[:120])
