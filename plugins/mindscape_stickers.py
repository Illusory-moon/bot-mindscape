# -*- coding: utf-8 -*-
"""mindscape_stickers —— 表达层：多模态素材采集

思路：群里有大量合适的图，但 bot 不会用。
      本模块以低概率采样群消息中的图片，交给视觉模型判断「是否契合本 bot 的人设」，
      契合的入库并打标签，供后续发送模块使用。

关键设计：
  - **低概率采样**（默认 10%）—— 避免每张图都调 API
  - **分类隔离** —— 不同 bot 用不同 category，互不串味
  - **MD5 去重** —— 同一张图不会重复入库
  - **判定校验** —— 模型有时会照抄 prompt 里的示例，这种脏数据直接丢弃
"""
import base64
import hashlib
import json
import os
import random
import shutil
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image
from astrbot.core.star.filter.event_message_type import EventMessageType

import mindscape_config as cfg
from mindscape_core import (DEFAULT_PROMPT, IndexLock, abs_path, load_index,
                           match_score, parse_verdict, save_index, verdict_ok)


def st_abs(path):
    """相对路径按「配置文件所在目录」解析（与其它模块各自持有一份，互不覆盖）。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


class StickersMixin:
    def setup(self, context):

        self.s_c = cfg.section("stickers")
        self.dir = st_abs(self.s_c.get("dir") or "./data/stickers")
        self.index_path = st_abs(self.s_c.get("index") or os.path.join(self.dir, "index.json"))
        self.seen_path = st_abs(self.s_c.get("seen") or os.path.join(self.dir, "seen.json"))
        os.makedirs(self.dir, exist_ok=True)
        self.seen = self._load_seen()
        logger.info(
            "[mindscape_stickers] loaded | prob=%.2f | 已见 %d 张",
            float(self.s_c.get("sample_prob", 0.10)), len(self.seen),
        )

    def _load_seen(self):
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                return set(d)
        except Exception:
            pass
        return set()

    def _save_seen(self):
        try:
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump(sorted(self.seen)[-3000:], f)
        except Exception:
            pass

    def _add_index(self, fname, category, verdict):
        """加锁 + 重读 + 合并 + 原子写，避免和导入脚本互相覆盖。"""
        entry = {
            "file": fname,
            "category": category,
            "name": str(verdict.get("name") or "未命名")[:12],
            "tags": [str(x)[:10] for x in (verdict.get("tags") or [])][:6],
            "desc": str(verdict.get("desc") or "")[:150],
        }
        try:
            with IndexLock(self.index_path):
                idx = load_index(self.index_path)
                if idx is None:
                    logger.warning("[mindscape_stickers] 索引损坏，本次不入库")
                    return
                if any(x.get("category") == category and str(x.get("file")) == fname
                       for x in idx):
                    return
                idx.append(entry)
                save_index(self.index_path, idx)
        except Exception as e:
            logger.warning("[mindscape_stickers] 写索引失败: %s", str(e)[:120])

    def _target_category(self, self_id):
        for t in (self.s_c.get("targets") or []):
            if str(t.get("self_id", "")) == str(self_id):
                return t.get("category") or "default"
        return None

    @filter.event_message_type(EventMessageType.ALL)
    async def collect(self, event: AstrMessageEvent):
        try:
            category = self._target_category(event.get_self_id())
            if not category:
                return
            if random.random() > float(self.s_c.get("sample_prob", 0.10)):
                return
            comps = getattr(event.message_obj, "message", None) or []
            for comp in comps:
                if not isinstance(comp, Image):
                    continue
                await self._handle(comp, category)
                return
        except Exception as e:
            logger.warning("[mindscape_stickers] 采集失败: %s", str(e)[:140])

    async def _handle(self, comp, category):
        import asyncio
        try:
            path = await asyncio.wait_for(comp.convert_to_file_path(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("[mindscape_stickers] 图片下载超时，跳过")
            return
        except Exception as e:
            logger.warning("[mindscape_stickers] 图片下载失败: %s", str(e)[:100])
            return
        if not path or not os.path.exists(path):
            return

        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()
        # R11: 去重键包含分类 —— 同一张图可以被不同 bot 各自采用，
        # 且失败/跳过不会污染另一个分类。
        key = category + ":" + h
        if key in self.seen:
            return

        verdict = await self._judge(path)
        if verdict is None:
            # 判定失败（超时/无 key/解析失败）：本次不记为已见，允许下次重试
            return
        if not verdict.get("related"):
            # 明确判定为「不相关」：认为已处理，不再重复消耗 API
            self.seen.add(key)
            self._save_seen()
            return

        ext = os.path.splitext(path)[1].lower() or ".jpg"
        fname = h[:10] + ext
        dst = os.path.join(self.dir, fname)
        try:
            shutil.copy2(path, dst)
        except Exception as e:
            logger.warning("[mindscape_stickers] 保存失败: %s", str(e)[:100])
            return

        if not verdict_ok(verdict):
            logger.warning("[mindscape_stickers] 判定内容无效（疑似复读），丢弃")
            try:
                os.remove(dst)
            except Exception:
                pass
            return

        self.seen.add(key)          # 只有真正入库成功才记为已见
        self._save_seen()
        self._add_index(fname, category, verdict)
        logger.info("[mindscape_stickers] 已入库 %s | %s", fname, verdict.get("name"))

    async def _judge(self, path):
        import httpx
        j = self.s_c.get("judge") or {}
        api_base = (j.get("api_base") or "").rstrip("/")
        if not api_base:
            return None
        key = os.environ.get(j.get("api_key_env") or "", "")
        if not key:
            return None
        prompt = (j.get("prompt") or DEFAULT_PROMPT).replace(
            "{persona}", j.get("persona") or "二次元角色"
        )
        with open(path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode()
        low = path.lower()
        mime = "image/gif" if low.endswith(".gif") else ("image/png" if low.endswith(".png") else "image/jpeg")
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": j.get("model") or "gpt-4o-mini",
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}},
                        ]}],
                        "max_tokens": int(j.get("max_tokens") or 2000),
                    },
                )
            if resp.status_code != 200:
                logger.warning("[mindscape_stickers] 判断 API %s", resp.status_code)
                return None
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                txt = (msg.get("reasoning_content") or "").strip()
        except Exception as e:
            logger.warning("[mindscape_stickers] 判断异常: %s", str(e)[:140])
            return None

        return parse_verdict(txt)
        logger.warning("[mindscape_stickers] JSON 解析失败: %s", txt[:140])
        return None