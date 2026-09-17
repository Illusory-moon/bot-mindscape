# -*- coding: utf-8 -*-
"""mindscape_memory —— 认知层：滑动窗口式长期记忆注入

问题：bot 的「记忆」如果只靠会话历史，一旦清理就失忆；
      如果每轮把整个记忆文件塞进 prompt，token 又会爆炸。

方案：把长期记忆写成人类可读的 Markdown 文件（由 diary 模块生成），
      每轮请求只注入「最近 max_chars 字」，并在条目边界截断，绝不切半句话。
      这样记忆独立于会话，且体积恒定。
"""
import os

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg

DEFAULT_MAX_CHARS = 2500
DEFAULT_MIN_CHARS = 50
DEFAULT_PEOPLE_CHARS = 800
SECTION_TITLE = "## 你的长期记忆"
HEADER_MARK = "## "


def read_recent(path, max_chars):
    """取最近的记忆，严格不超过 max_chars（从最新条目向前累计）。

    做法：从文件尾部往前扫，按「条目 / 标题」为单位累加，
    一旦加入下一段会超预算就停 —— 保证返回长度是硬上限，且不切半句话。
    """
    if not path or not os.path.exists(path) or max_chars <= 0:
        return ""
    size = os.path.getsize(path)
    # 预算的 4 倍足够容纳多字节字符；再多读一点保证能拿到完整条目
    read_from = max(0, size - max_chars * 6)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if read_from > 0:
            f.seek(read_from)
            f.readline()                       # 丢掉被截断的半行
        tail = f.read()

    lines = tail.splitlines()
    # 从后往前，以「条目块」为单位累加（块 = 连续的非空行，遇到 ## 标题另起一块）
    blocks = []
    cur = []
    for ln in reversed(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith(HEADER_MARK):
            if cur:
                blocks.append(list(reversed(cur)))
                cur = []
            blocks.append([ln])
        else:
            cur.append(ln)
    if cur:
        blocks.append(list(reversed(cur)))

    picked = []
    used = 0
    for blk in blocks:
        text = "\n".join(blk).strip()
        if not text:
            continue
        add = len(text) + (1 if picked else 0)
        if used + add > max_chars:
            # 单块就超预算：整块丢弃（宁缺勿断）
            break
        picked.insert(0, text)
        used += add
    return "\n".join(picked)


def _resolve(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def read_people(path, max_chars):
    """读人物画像文件，只保留条目行（跳过标题和更新时间）。"""
    if not path or not os.path.exists(path):
        return ""
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                if line.startswith("- "):
                    lines.append(line)
    except Exception:
        return ""
    out = "\n".join(lines)
    return out[:max_chars]


class MemoryMixin:
    def setup(self, context):

        self.m_cfg = cfg.section("memory")
        logger.info(
            "[mindscape_memory] loaded | %d bot(s) 配置了记忆",
            len(cfg.bot_entries()),
        )

    def _find_bot(self, self_id):
        for b in cfg.bot_entries():
            if str(b.get("self_id", "")) == str(self_id):
                return b
        return None

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            bot = self._find_bot(event.get_self_id())
            if not bot:
                return
            path = _resolve(bot.get("diary"))
            max_chars = int(bot.get("memory_chars") or self.m_cfg.get("max_chars") or DEFAULT_MAX_CHARS)
            min_chars = int(self.m_cfg.get("min_chars") or DEFAULT_MIN_CHARS)

            mem = read_recent(path, max_chars)
            if len(mem) < min_chars:
                return

            old = getattr(request, "system_prompt", "") or ""
            if SECTION_TITLE in old:
                return

            label = bot.get("name") or "你"
            block = (
                "\n\n" + SECTION_TITLE + "\n"
                "以下是你自己记下来的往事，是你亲身经历的，可以自然地提起，"
                "但不要照本宣科地念，也不要说「根据我的记忆」这种话。\n\n"
                + mem
            )

            # 人物画像（可选）：让 bot 认得群里的人
            people_path = bot.get("people")
            if not people_path and path:
                people_path = path.rsplit(".", 1)[0] + ".people.md"
            people_path = _resolve(people_path)
            p_chars = int(bot.get("people_chars") or self.m_cfg.get("people_chars") or DEFAULT_PEOPLE_CHARS)
            people = read_people(people_path, p_chars)
            if people:
                block += (
                    "\n\n## 你认识的人\n"
                    "这些是你记住的群友，聊天时可以自然地认得他们；"
                    "没在名单里的人，就当第一次见。\n\n"
                    + people
                )

            request.system_prompt = old + block
            logger.info("[mindscape_memory] %s 注入 %d 字记忆 / %d 字人物",
                        label, len(mem), len(people))
        except Exception as e:
            logger.warning("[mindscape_memory] 注入失败: %s", str(e)[:120])