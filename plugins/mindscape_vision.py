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
from mindscape_core import scope_hit, scope_warn, sys_tag

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


# ⚠️ B 层兜底（2026-10-14 真事故的第二道保险 ✓）：
#   格式闸门（A 层）治根，但**万一**还有漏进来的（比如框架自己塞的引用图、
#   或者 A 层读不动那个引用 ⇒ 剔干净了却仍然 400），就**去掉图重答一次** ✗
#   —— 宁可这一轮少看一张图，也不能让她**整轮哑掉、群里一个字都不出** ✓
VS_IMAGE_ERROR_RE = re.compile(
    r"unsupported image|invalid image|image.*(?:format|invalid_request_error)", re.I)
VS_RETRY_FLAG = "_mindscape_vision_retried"


def vs_is_image_error(text):
    """这条报错是不是"图不合规"那一类（**只认这个特征**，别的报错不许乱重试 ✗）"""
    return bool(VS_IMAGE_ERROR_RE.search(str(text or "")))


async def vs_retry_without_image(star, event, response):
    """把"图惹的祸"那一轮**去掉图重答一次** ⇒ 返回新的 completion_text ✓

    失败就原样返回（绝不吞掉她原本该有的输出路径 ✗）
    """
    try:
        umo = ""
        for getter in ("get_session_id", "unified_msg_origin"):
            value = getattr(event, getter, None)
            if callable(value):
                umo = str(value() or "")
                break
        provider = None
        context = getattr(star, "context", None)
        if context is not None:
            provider = context.get_using_provider(umo or None)
        if provider is None:
            logger.warning("[mindscape_vision] 想去图重答，但拿不到 provider ⇒ 放弃")
            return ""
        prompt = str(getattr(event, "message_str", "") or "")
        parts = getattr(getattr(event, "request", None), "extra_user_content_parts", None)
        system_prompt = str(getattr(getattr(event, "request", None), "system_prompt", "") or "")
        again = await provider.text_chat(prompt=prompt or "[图片]", image_urls=[],
                                        extra_user_content_parts=list(parts or []) or None,
                                        system_prompt=system_prompt or None,
                                        request_max_retries=1)
        text = str(getattr(again, "completion_text", "") or "")
        if text:
            logger.warning("[mindscape_vision] 图不合规 ⇒ 已去掉图重答一次（%d 字）✓", len(text))
        else:
            logger.warning("[mindscape_vision] 去图重答没有拿到内容 ✗")
        return text
    except Exception as e:                 # noqa: BLE001 —— 兜底失败也不能影响主流程
        logger.warning("[mindscape_vision] 去图重答失败: %s", str(e)[:120])
        return ""


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


# ── 图格式闸门（2026-10-14 真事故 ✗）────────────────────────────────
# 事故：群里有一轮请求**连续两次**报
#   `400 .messages[41].image[0]: You have uploaded an unsupported image.
#    Please make sure your image is valid and has one of the following formats: webp, png, jpeg, and gif`
# ⇒ 一轮里**所有**模型调用全失败 ⇒ `mindscape_guard` 又把报错拦住（不进群 ✓）
# ⇒ 从主人那边看就是「**她不回消息、也不报错**」✗
# 根因：框架把 `request.image_urls` 里的东西**一律按 `image/png` 包成 base64** 发出去
#   （`MediaResolver(..., media_type="image").to_base64_data()`）——
#   **它不看真实格式** ✗ ⇒ 只要那个 URL 指向的不是真图（QQ 偶尔给的就是这种），必 400 ✓
# ⇒ 修：**下发前验魔术字节**，认不出是 jpeg/png/gif/webp 的一律**剔掉**，
#   并留下日志（这样"她突然不回"以后一眼能查到原因 ✓）。
VS_OK_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def vs_sniff_image(raw):
    """认 magic bytes ⇒ 返回 'jpeg'/'png'/'gif'/'webp'；认不出返回 ''（= 不许下发）✓"""
    if not raw:
        return ""
    for magic, name in VS_OK_MAGIC:
        if raw.startswith(magic):
            return name
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return ""


def vs_read_head(ref, limit=64):
    """读一个图引用的前若干字节（本地路径 / http(s) / data:base64 都认 ✓）。"""
    text = str(ref or "")
    if text.startswith("data:"):
        head, _, payload = text.partition(",")
        if "base64" not in head:
            return b""
        return base64.b64decode(payload[:96] + "=" * (-len(payload[:96]) % 4),
                                validate=False)[:limit]
    if text.startswith(("http://", "https://")):
        with urllib.request.urlopen(urllib.request.Request(
                text, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as response:
            return response.read(limit)
    if os.path.isfile(text):
        with open(text, "rb") as handle:
            return handle.read(limit)
    return b""


def vs_filter_images(urls, event_self=""):
    """把 `image_urls` 过一遍格式闸门 ⇒ 返回 (留下的, 剔掉的条数) ✓

    ⚠️ 读不动 / 认不出 = **一律剔掉**（宁可这一轮少看一张图，也不能让整轮全灭 ✗）
    """
    kept, dropped = [], 0
    for ref in list(urls or []):
        try:
            kind = vs_sniff_image(vs_read_head(ref))
        except Exception as e:                    # noqa: BLE001 —— 读不动就当它不合规
            logger.warning("[mindscape_vision] 验图读不动，剔掉: %s", str(e)[:80])
            kind = ""
        if kind:
            kept.append(ref)
        else:
            dropped += 1
            logger.warning("[mindscape_vision] 剔掉一张不合规的图（不是 jpeg/png/gif/webp）"
                           " | bot=%s | 引用=%s", event_self, str(ref)[:90])
    return kept, dropped


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
    text = sys_tag(text)          # 来源标记贴末尾 ✓（不碰开头的记号 ✓）
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
            # ⚠️ 先过**格式闸门**（2026-10-14 那次"她不回消息"的真凶 ✓）：
            #    不合规的图必须在下发前剔掉，否则整轮请求 400 全灭 ✗
            if urls:
                kept, dropped = await asyncio.to_thread(
                    vs_filter_images, list(urls), event.get_self_id())
                if dropped:
                    request.image_urls = kept
                    urls = kept
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

    @filter.on_llm_response()
    async def vs_image_error_retry(self, event, response):
        """**B 层兜底**：图不合规把整轮打挂了 ⇒ 去掉图重答一次 ✗

        ⚠️ 2026-10-14 真事故：那一轮所有模型调用全 400（`unsupported image`），
           `guard` 又把报错拦住（不进群 ✓）⇒ 主人只看到「她不回消息、也不报错」✗
        ⇒ A 层（格式闸门）治根；这一层是**万一漏网时她还能开口**的保险 ✓
        ⚠️ 每条消息**只重试一次**（`VS_RETRY_FLAG`）—— 绝不允许变成循环请求 ✗
        """
        try:
            if not self.vs_on:
                return
            text = str(getattr(response, "completion_text", "") or "")
            if not vs_is_image_error(text):
                return
            if getattr(event, VS_RETRY_FLAG, False):
                logger.warning("[mindscape_vision] 图不合规，但这条已经重试过 ⇒ 不再重试 ✗")
                return
            setattr(event, VS_RETRY_FLAG, True)
            logger.warning("[mindscape_vision] 图不合规把整轮打挂了 ⇒ 去掉图重答一次 | bot=%s",
                           event.get_self_id())
            again = await vs_retry_without_image(self, event, response)
            if again:
                response.completion_text = again
        except Exception as e:
            logger.warning("[mindscape_vision] 去图重答钩子异常: %s", str(e)[:120])