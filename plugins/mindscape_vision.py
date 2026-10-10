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
import asyncio
import base64
import io
import os
import re
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

VS_MARK = "这一轮的消息里带了图"
VS_LARGE_IMAGE_BYTES = 2_000_000
VS_HINT = """## 这一轮的消息里带了图 —— 认人之前先查

- 图里的人物 / 作品，**先用 lookup_knowledge 查，查完还不确定就联网搜**；
  不要凭印象直接认。
- **查完还是不确定**是谁，就用你自己的口吻糊过去 —— 大意是「画面太糊、看不清」，
  **具体怎么说按你平常的说话方式来，别照抄这句**。
  糊弄 + 老实承认看不清，永远好过瞎编一个名字。
- 没有依据之前，**绝不说**「这就是 XX」。认错一个人，比说不认识难看得多。
"""

# ⚠️ 2026-10-10 主人报 ✓（原判 wontfix，当天改主意要修 ✓）：有人发**纯文本**
#   `![这是一个带着小礼帽…的少女图片](img.png)`，她会**真以为自己看到了图** ✗。
#   查过日志：那一轮管线里**没有任何图**（vs_has_image=False、image_urls 为空 ✓），
#   是模型把「长得像附件标记的正文」当成了附件 ✗。
#   修法（代码判定 + 一句明确标注 ✓）：本轮没图、但正文里出现像媒体/附件的字样时，
#   由**代码**认出来并告诉她「那只是对方打的文字」—— 不改渲染、不改用户那条消息 ✗。
#   位置：VS_PRIORITY=-4（群缓冲之后、人物之前）✓ 见 mindscape_memory 文件头的缓存契约。
VS_PRIORITY = -4
VS_TEXT_MARK = "（系统提示：正文里出现了像图片/附件的字样"
# 覆盖面 = 各种「看起来是媒体」的写法（网关渲染 / 引用渲染 / 原始 CQ 码 / markdown / HTML / data URI）
VS_FAKE_RE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)|\[\s*(?:图片|视频|文件|语音|表情|动画|image|img|video|file|audio|sticker)\s*\]|image attachment|\[CQ:(?:image|video|record|file|face)|data:(?:image|video|audio)/|<img\b",
    re.I)
VS_TEXT_HINT = (
    "（系统提示：正文里出现了像图片/附件的字样（`%s`）—— 但**这一轮没有任何图片附件**，"
    "你手上没有图，那只是**对方打的文字**。不要描述图里有什么、也不要当成自己看见了；"
    "真要看图，让对方直接把图发过来。）")


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


def vs_attach(request, text, mark):
    """把「这一轮」的识图提醒挂到当前消息之后 ✓（幂等靠 mark 认领 ✓）。

    挂载失败时退回 system_prompt（带警告 ✓）—— 宁可费钱，不可丢提醒。
    """
    old = getattr(request, "system_prompt", "") or ""
    parts = getattr(request, "extra_user_content_parts", None)
    if parts is None:
        parts = []
        request.extra_user_content_parts = parts
    if mark in old or any(mark in str(getattr(p, "text", "")) for p in parts):
        return False
    try:
        from astrbot.core.agent.message import TextPart
        parts.append(TextPart(text=text))
    except Exception as e:
        logger.warning("[mindscape_vision] 挂当前消息失败（缓存会吃亏），退回 system_prompt: %s", str(e)[:90])
        request.system_prompt = old + "\n\n" + text
    return True


def vs_compact_image(ref):
    """大图只给模型传静态预览；原始消息和图片不改。"""
    try:
        if ref.startswith(("http://", "https://")):
            with urllib.request.urlopen(urllib.request.Request(
                    ref, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as response:
                size = int(response.headers.get("Content-Length") or 0)
                if size and size <= VS_LARGE_IMAGE_BYTES:
                    return None
                raw = response.read(24_000_001)
        elif os.path.isfile(ref):
            if os.path.getsize(ref) <= VS_LARGE_IMAGE_BYTES:
                return None
            with open(ref, "rb") as f:
                raw = f.read(24_000_001)
        else:
            return None
        if not VS_LARGE_IMAGE_BYTES < len(raw) <= 24_000_000:
            return None

        from PIL import Image as PilImage
        image = PilImage.open(io.BytesIO(raw))
        count = getattr(image, "n_frames", 1)
        frames = []
        for index in dict.fromkeys((0, count // 2, count - 1)):
            image.seek(index)
            frame = image.convert("RGB")
            frame.thumbnail((768, 768), PilImage.LANCZOS)
            frames.append(frame)
        preview = PilImage.new("RGB", (max(f.width for f in frames),
                                        sum(f.height for f in frames)), "white")
        top = 0
        for frame in frames:
            preview.paste(frame, (0, top))
            top += frame.height
        out = io.BytesIO()
        preview.save(out, "JPEG", quality=80)
        logger.info("[mindscape_vision] 大图预览 %d -> %d 字节 | 帧=%d",
                    len(raw), out.tell(), len(frames))
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("[mindscape_vision] 大图预览失败，保留原图: %s", str(e)[:120])
        return None


class VisionMixin:
    def setup(self, context):
        self.vs_on, self.vs_targets = vs_load_config()
        logger.info("[mindscape_vision] loaded | enabled=%s | targets=%s",
                    self.vs_on, self.vs_targets or "全部")
        scope_warn(logger, "mindscape_vision", self.vs_targets, self.vs_on)

    def _vs_hit(self, event):
        if not self.vs_on:
            return False
        return scope_hit(self.vs_targets, event.get_self_id())

    @filter.on_llm_request(priority=VS_PRIORITY)
    async def vs_hint(self, event: AstrMessageEvent, request):
        """有图 → 提醒「先查再认」；**没图但正文长得像有图** → 明确标注「那只是文字」✓。"""
        try:
            if not self._vs_hit(event):
                return
            urls = getattr(request, "image_urls", None)
            if urls:
                for index, ref in enumerate(urls):
                    preview = await asyncio.to_thread(vs_compact_image, str(ref))
                    if preview:
                        urls[index] = preview
            if vs_has_image(event):
                if vs_attach(request, VS_HINT, VS_MARK):
                    logger.info("[mindscape_vision] 有图：已提醒先查再认 | bot=%s",
                                event.get_self_id())
                return
            # 本轮**没有图**：正文里若出现像图片/附件的字样，由代码认出来并标注「那只是文字」✓
            fake = VS_FAKE_RE.search(str(getattr(event, "message_str", "") or ""))
            if not fake:
                return
            if vs_attach(request, VS_TEXT_HINT % fake.group(0)[:40], VS_TEXT_MARK):
                logger.info("[mindscape_vision] 正文像附件、本轮无图 → 已标注为文字 | bot=%s | 命中=%s",
                            event.get_self_id(), fake.group(0)[:40])
        except Exception as e:
            logger.warning("[mindscape_vision] 注入失败: %s", str(e)[:120])