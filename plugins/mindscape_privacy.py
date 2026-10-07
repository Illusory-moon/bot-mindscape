# -*- coding: utf-8 -*-
"""Tell a bot where its code is when its developer asks."""
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg


class PrivacyMixin:
    def setup(self, context):
        logger.info("[mindscape_privacy] loaded")

    @filter.on_llm_request()
    async def pg_ask_hint(self, event: AstrMessageEvent, request: ProviderRequest):
        gate = cfg.section("privacy_gate")
        if not gate.get("enabled"):
            return
        if str(event.get_sender_id()) not in [str(x) for x in gate.get("developer_ids") or []]:
            return
        if gate.get("ask_from", "private") == "private" and event.get_group_id():
            return
        message = str(getattr(event.message_obj, "message_str", "") or "")
        if not any(str(word) in message for word in gate.get("ask_words") or []):
            return
        hint = str(gate.get("ask_hint") or "").strip()
        if hint:
            request.system_prompt = (request.system_prompt or "") + "\n\n" + hint
