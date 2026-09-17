# -*- coding: utf-8 -*-
"""mindscape_rescue —— 空回复救援

背景：推理模型（如 deepseek-flash）有时把全部输出放在 reasoning_content 里，
      content 为空；若这一次又没有 tool_calls，框架就会得到一条完全空的回复，
      然后静默 skip —— 用户看到的是「bot 没反应」。

本模块在「发送前」检测这种完全空回复，用一次极轻量的调用补一句符合人设的话，
避免出现「叫了不理」的观感。代价：仅在空回复时多一次请求。

配置（可选）：
    rescue:
      enabled: true
      api_base: "https://api.deepseek.com/v1"
      api_key_env: "MINDSCAPE_API_KEY"
      model: "your-model"
      persona: "一句话描述你的人设"
      max_tokens: 120
      timeout: 20
"""
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image

import mindscape_config as cfg

DEFAULT_TEXT = "……（刚才走神了，你再说一遍？）"


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else "."
    return os.path.join(base, path)


class RescueMixin:
    def setup(self, context):
        self.r_cfg = cfg.section("rescue") or {}
        self.r_done = set()          # 防止同一条回复反复救援
        logger.info("[mindscape_rescue] loaded | %s",
                    "启用" if self.r_cfg.get("enabled", True) else "关闭")

    @staticmethod
    def _is_empty_result(result):
        """完全没有文字、也没有图片，才算「空回复」。"""
        try:
            if (result.get_plain_text() or "").strip():
                return False
        except Exception:
            return False
        try:
            for c in (getattr(result, "chain", None) or []):
                if isinstance(c, Image):
                    return False
        except Exception:
            pass
        return True

    @filter.on_decorating_result(priority=800)
    async def rescue_empty(self, event: AstrMessageEvent):
        if not self.r_cfg.get("enabled", True):
            return
        try:
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            if not self._is_empty_result(result):
                return
            text = await self._ask_once(event)
            if text:
                from astrbot.core.message.components import Plain
                result.chain.append(Plain(text))
                logger.info("[mindscape_rescue] 空回复已补: %s", text[:40])
        except Exception as e:
            logger.warning("[mindscape_rescue] 救援失败: %s", str(e)[:120])

    async def _ask_once(self, event):
        import httpx
        api_base = (self.r_cfg.get("api_base") or "").rstrip("/")
        key = os.environ.get(self.r_cfg.get("api_key_env") or "", "")
        if not api_base or not key:
            return ""
        persona = self.r_cfg.get("persona") or "一个自然的聊天伙伴"
        last = ""
        try:
            data = getattr(event, "message_obj", None)
            last = str(getattr(data, "message_str", "") or "")[:200]
        except Exception:
            last = ""
        prompt = (
            "你是" + persona + "。刚才群友说了：\n"
            + (last or "（一条消息）")
            + "\n\n请用一句话自然回应（不超过30字），不要解释、不要客套、不要提及你是 AI。"
        )
        try:
            async with httpx.AsyncClient(timeout=float(self.r_cfg.get("timeout") or 20)) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={"model": self.r_cfg.get("model") or "gpt-4o-mini",
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": int(self.r_cfg.get("max_tokens") or 120)},
                )
            if resp.status_code != 200:
                return ""
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                txt = (msg.get("reasoning_content") or "").strip()
            # 极简清洗：去掉可能的引号和前缀
            txt = txt.strip().strip('"').strip("“”").strip()
            if txt and len(txt) > 60:
                txt = txt[:60]
            return txt
        except Exception as e:
            logger.warning("[mindscape_rescue] 补话失败: %s", str(e)[:100])
            return ""
