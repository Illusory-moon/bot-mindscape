# -*- coding: utf-8 -*-
"""mindscape_sticker_use —— 表达层：表情包智能调度

两个能力：
  1. **主动发送** —— 给 bot 一个工具，让它自己想发图时能发（按语境选图）
  2. **概率强制** —— chat bot 有强烈的「只发文字」惯性，光给工具它不用；
     所以每轮回复按概率强制配一张图，真正治「文字机器」。

关键设计：
  - 选图交给 LLM（给它候选列表挑最贴语气的），保证图文搭
  - 候选来自**本 bot 的 category**，不串味
  - 任何异常都不影响正常回复
"""
import json
import os
import random

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.star.filter.event_message_type import EventMessageType

import mindscape_config as cfg
from mindscape_core import (IndexLock, abs_path, is_inside, load_index, match_score,
                           safe_name, save_index)


# 模块级工具需要拿到 Mixin 实例（框架只传 event，不给 self）
_INSTANCE = None


def _NS():
    return _INSTANCE


def su_abs(path):
    """相对路径按「配置文件所在目录」解析。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


def pick_image(comps, image_cls, reply_cls, depth=0, max_depth=3):
    """在组件链里找第一张图，**会往被引用消息里钻**。

    为什么必须钻：aiocqhttp 适配器收到 reply 段时会 `call_action("get_msg")`，
    把被引用消息的完整组件链塞进 `Reply.chain` —— 也就是说图**本来就在事件里**，
    只是不在顶层。只扫顶层的话，「引用一张图说『加进表情库』」永远得到
    「这条消息里没看到图片」，而模型那条路却能看见同一张图（所以显得像左右脑互搏）。

    类由调用方传进来（本模块要能在 AstrBot 之外被测试）；深度防环。
    """
    if not comps or depth > max_depth:
        return None
    for c in comps:
        if isinstance(c, image_cls):
            return c
    for c in comps:
        if isinstance(c, reply_cls):
            got = pick_image(getattr(c, "chain", None), image_cls, reply_cls,
                             depth + 1, max_depth)
            if got is not None:
                return got
    return None

DEFAULT_PICK_PROMPT = (
    "你正在群聊里说话。\n\n"
    "你刚回复了这段话：\n「{reply}」\n\n"
    "现在要给这条回复配一张表情包。候选如下：\n{candidates}\n\n"
    "规则：\n"
    "- **必须从上面选一张**，不许说「不用」，也不许编造不存在的文件名\n"
    "- 选最贴合当前语气的那张（比如怼人配得意/鄙夷，被夸配臭美，无语配呆滞）\n"
    "- 直接返回文件名，不要解释、不要引号、不要多余的字"
)


def _load_index(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict) and x.get("file")]
    except Exception:
        pass
    return []


class StickerUseMixin:
    def setup(self, context):

        self.u_c = cfg.section("stickers")
        self.dir = su_abs(self.u_c.get("dir") or "./data/stickers")
        self.index_path = su_abs(self.u_c.get("index") or os.path.join(self.dir, "index.json"))
        self.send_cfg = self.u_c.get("send") or {}
        # 队形检测：记录各群最近的图片指纹（不下载图片，只用框架给的标识）
        self._recent_imgs = {}
        self.formation = self.send_cfg.get("formation") or {}
        global _INSTANCE
        _INSTANCE = self          # 供模块级工具回调
        self._NS_registered = True
        logger.info(
            "[mindscape_sticker_use] loaded | 强制概率 %.2f",
            float(self.send_cfg.get("force_prob", 0.10)),
        )

    def _category_of(self, self_id):
        for t in (self.u_c.get("targets") or []):
            if str(t.get("self_id", "")) == str(self_id):
                return t.get("category") or "default"
        return None

    def _pool(self, self_id):
        """取该 bot 可用的图。

        配置了分类就只返回该分类（即使为空也不跨分类），
        没配置分类的 bot 返回空集合 —— 隔离优先于「有图可用」。
        """
        cat = self._category_of(self_id)
        if not cat:
            return []
        idx = _load_index(self.index_path)
        return [x for x in idx if x.get("category") == cat]

    # ── 能力1：bot 主动要图 ──
    async def save_sticker_impl(self, event, kwargs):
        """把消息里的图片存进本 bot 的分类（供 save_sticker 工具调用）。"""
        import asyncio
        import hashlib
        import shutil
        from astrbot.core.message.components import Image, Reply
        name = str(kwargs.get("name") or "").strip()[:12] or "私藏"
        raw = str(kwargs.get("tags") or "").strip()
        tags = [x.strip()[:10] for x in raw.replace("，", ",").split(",") if x.strip()][:6]
        if not tags:
            tags = ["私藏", "表情包"]
        cat = self._category_of(event.get_self_id())
        if not cat:
            return "你还没有配置表情包分类，存不了。"
        comps = []
        try:
            comps.extend(getattr(event.message_obj, "message", None) or [])
        except Exception:
            pass
        img = pick_image(comps, Image, Reply)
        if img is None:
            # 万一还是捞不到，把链的形状记下来 —— 一眼看得出图到底在不在事件里
            logger.info("[mindscape_stickers] save_sticker 没找到图，链= %s",
                        [type(c).__name__ for c in comps][:10])
            return "这条消息里没看到图片，你把它单独发一次？"
        try:
            path = await asyncio.wait_for(img.convert_to_file_path(), timeout=20)
        except asyncio.TimeoutError:
            return "这张图下载超时了，你重发一次试试？"
        except Exception as e:
            return "图片下载失败：" + str(e)[:60]
        if not path or not os.path.exists(path):
            return "图片拿不到，存不了。"
        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()
        ext = os.path.splitext(path)[1].lower() or ".jpg"
        fname = h[:10] + ext
        dst = os.path.join(self.dir, fname)
        try:
            with IndexLock(self.index_path):
                idx = load_index(self.index_path) or []
                if any(x.get("category") == cat and str(x.get("file")) == fname for x in idx):
                    return "这张我已经收过啦。"
                shutil.copy2(path, dst)
                idx.append({"file": fname, "category": cat, "name": name,
                            "tags": tags, "desc": "收的：" + (raw or name)[:80]})
                save_index(self.index_path, idx)
        except Exception as e:
            return "存图失败：" + str(e)[:60]
        return "收好啦：%s（%s）" % (name, "/".join(tags))

    @llm_tool(name="send_sticker")
    async def send_sticker(self, *args, **kwargs):
        """发一张表情包/图片到当前聊天（纯图片，不带文字）。

        当对方要求你「发个表情包/发图」，或你觉得此刻甩一张图最合适时，调用这个工具。

        Args:
            want(string): 你想要的图的感觉或用途，例如：无语、开心、发呆、得意、撒娇。留空则随机挑一张。
        """
        want = str(kwargs.get("want") or "").strip()
        svc = getattr(self, "context", None)

        # 从 args 里找出真正的 event（框架可能把插件类绑在第一个参数）
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        sid = ""
        try:
            sid = str(ev.get_self_id()) if ev is not None else ""
        except Exception:
            sid = ""

        pool = self._pool(sid)
        if not pool:
            return "图库还是空的，先攒几张再发"

        best, best_score = None, -1.0
        for item in pool:
            sc = match_score(item, want)
            if sc > best_score:
                best_score, best = sc, item
        if best is None:
            best = pool[0]
        fn = safe_name(best.get("file", ""))
        path = os.path.join(self.dir, fn)
        if not fn or not os.path.exists(path) or not is_inside(path, self.dir):
            return "那张图找不到了"
        try:
            return MessageEventResult().file_image(path)
        except Exception as e:
            return "发图失败：" + str(e)[:60]

    # ── 能力2：概率强制配图 ──
    @filter.on_decorating_result(priority=850)
    async def maybe_attach(self, event: AstrMessageEvent):
        try:
            if not self._category_of(event.get_self_id()):
                return
            # 队形优先：群里在斗图时，用更高的概率跟一张
            in_war = False
            if self.formation.get("enabled", True):
                in_war = self._in_image_war(
                    event.get_group_id(),
                    int(self.formation.get("window", 90)),
                    int(self.formation.get("need_same", 2)),
                )
            prob = float(self.formation.get("prob", 0.35) if in_war
                         else self.send_cfg.get("force_prob", 0.10))
            if random.random() > prob:
                return
            result = event.get_result()
            if result is None or not result.is_llm_result():
                return
            pool = self._pool(event.get_self_id())
            if not pool:
                return
            reply = (result.get_plain_text() or "")[:200]
            fname = await self._pick(reply, pool)
            if not fname:
                return
            path = os.path.join(self.dir, fname)
            if not os.path.exists(path) or not is_inside(path, self.dir):
                return
            result.file_image(path)
            logger.info("[mindscape_sticker_use] 配图 %s", fname)
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 配图失败: %s", str(e)[:140])

    # ── 队形：跟群里的斗图 ──
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def watch_images(self, event: AstrMessageEvent):
        """记录群里最近的图片指纹，用于判断是否在斗图。"""
        try:
            if not self._category_of(event.get_self_id()):
                return
            comps = getattr(event.message_obj, "message", None) or []
            import time as _t
            gid = str(event.get_group_id() or "")
            for comp in comps:
                if not isinstance(comp, Image):
                    continue
                key = getattr(comp, "file", None) or getattr(comp, "url", None) or ""
                if not key:
                    continue
                lst = self._recent_imgs.setdefault(gid, [])
                lst.append((_t.time(), str(key)))
                del lst[:-10]          # 只留最近 10 条
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 记录群图失败: %s", str(e)[:100])

    def _in_image_war(self, group_id, window=90, need_same=2):
        """判断这个群是否在斗图：时间窗内是否有同一张图出现 ≥ need_same 次。"""
        import time as _t
        lst = self._recent_imgs.get(str(group_id or "")) or []
        now = _t.time()
        recent = [k for (ts, k) in lst if now - ts <= window]
        if len(recent) < need_same:
            return False
        from collections import Counter
        return Counter(recent).most_common(1)[0][1] >= need_same

    async def _pick(self, reply, pool):
        import httpx
        j = self.u_c.get("judge") or {}
        api_base = (j.get("api_base") or "").rstrip("/")
        key = os.environ.get(j.get("api_key_env") or "", "")
        if not api_base or not key:
            return None
        n = int(self.send_cfg.get("candidates") or 12)
        cands = pool if len(pool) <= n else random.sample(pool, n)
        lines = []
        for i, it in enumerate(cands, 1):
            tags = "/".join(str(x) for x in (it.get("tags") or [])[:5])
            lines.append("%d. %s  [%s]  %s" % (i, it.get("file"), tags, str(it.get("desc") or "")[:60]))
        prompt = (self.send_cfg.get("prompt") or DEFAULT_PICK_PROMPT).replace(
            "{reply}", reply or "（没说什么）"
        ).replace("{candidates}", chr(10).join(lines))
        try:
            async with httpx.AsyncClient(timeout=90) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": j.get("model") or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1200,
                    },
                )
            if resp.status_code != 200:
                return None
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
        except Exception as e:
            logger.warning("[mindscape_sticker_use] 选图异常: %s", str(e)[:120])
            return None

        low = txt.lower()
        for it in cands:
            fn = str(it.get("file"))
            if fn and fn.lower() in low:
                return fn
        best, bs = None, -1.0
        for it in cands:
            sc = match_score(it, reply)
            if sc > bs:
                bs, best = sc, it
        return str(best.get("file")) if best else None


@llm_tool(name="save_sticker")
async def save_sticker(*args, **kwargs):
    """把当前消息里的图片存进表情包库。

    当有人说「这张适合当你的表情包」「送你一张图」，或你自己看中某张图时，
    调用这个工具真正把它收进来 —— 光嘴上说「收下」是没用的。

    Args:
        name(string): 给这张图起个中文短名
        tags(string): 标签，用逗号分隔
    """
    ev = None
    for a in args:
        if hasattr(a, "get_self_id"):
            ev = a
            break
    if ev is None:
        return "找不到当前消息，存不了。"
    inst = _NS()
    if inst is None:
        return "插件还没准备好，稍后再试。"
    return await inst.save_sticker_impl(ev, kwargs)
