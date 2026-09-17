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
import struct
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import Image
from astrbot.core.star.filter.event_message_type import EventMessageType

import mindscape_config as cfg
from mindscape_core import (DEFAULT_PROMPT, IndexLock, abs_path, load_index,
                           match_score, parse_verdict, save_index, verdict_ok)


def img_size(path):
    """只读文件头拿宽高，不依赖 Pillow。

    为什么需要它：采集器靠视觉模型判断「像不像这个 bot 的图」，
    但模型经常把游戏截图/壁纸也判成「二次元、可爱」—— 实测抓到过
    1920×1200 的原神剧情截图。**分辨率是这个误判最可靠的铁证**，
    而且读文件头几乎是零成本，能在调用视觉模型之前就挡掉。

    刻意**不按文件体积**判断：大 GIF 往往正是最合适的那张（动图帧多自然大），
    压缩或者丢弃都会把好东西扔掉。

    拿不到尺寸时返回 (0, 0)，调用方放行（宁可漏过，不可误杀）。
    """
    try:
        with open(path, "rb") as f:
            b = f.read(65536)
    except Exception:
        return 0, 0
    if len(b) < 24:
        return 0, 0
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", b[16:24])
    if b[:3] == b"GIF":
        return struct.unpack("<HH", b[6:10])
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        fmt = b[12:16]
        if fmt == b"VP8X":
            return (1 + b[24] + (b[25] << 8) + (b[26] << 16),
                    1 + b[27] + (b[28] << 8) + (b[29] << 16))
        if fmt == b"VP8 ":
            return (struct.unpack("<H", b[26:28])[0] & 0x3FFF,
                    struct.unpack("<H", b[28:30])[0] & 0x3FFF)
        if fmt == b"VP8L":
            bits = b[21] | (b[22] << 8) | (b[23] << 16) | (b[24] << 24)
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        return 0, 0
    if b[:2] == b"\xff\xd8":
        i = 2
        while i < len(b) - 9:
            if b[i] != 0xFF:
                i += 1
                continue
            m = b[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return 0, 0


def st_abs(path):
    """相对路径按「配置文件所在目录」解析（与其它模块各自持有一份，互不覆盖）。"""
    return abs_path(path, os.path.dirname(cfg.config_path()))


def shrink_for_judge(path, max_px=512, quality=80):
    """判定只需要「看得出画的是什么」，不需要原图。

    实测（deepseek-flash）：99 KB 的图 2.3 秒，而把 6 MB 的原图整个 base64
    塞上去要 20 秒以上 —— 慢在上行体积和视觉 token 上。按最长边缩到 max_px
    再转 JPEG，判定结论不变，耗时掉一个数量级。

    缩不了就原样返回（宁可慢，不可判不了）；连读都读不到才返回空。
    """
    try:
        import io as _io
        from PIL import Image as _Img
        im = _Img.open(path)
        im.seek(0)                      # 动图只取第一帧
        im = im.convert("RGB")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), _Img.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return b"", ""
    low = path.lower()
    mime = "image/gif" if low.endswith(".gif") else ("image/png" if low.endswith(".png") else "image/jpeg")
    return raw, mime


class StickersMixin:
    def setup(self, context):

        self.s_c = cfg.section("stickers")
        self.dir = st_abs(self.s_c.get("dir") or "./data/stickers")
        self.index_path = st_abs(self.s_c.get("index") or os.path.join(self.dir, "index.json"))
        self.seen_path = st_abs(self.s_c.get("seen") or os.path.join(self.dir, "seen.json"))
        os.makedirs(self.dir, exist_ok=True)
        self.seen = self._load_seen()
        self._bg = set()            # 后台采集任务，留引用防被 GC 掉
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
                # 绝不能 await：下载 + 视觉判定要 10~25 秒，一 await 就把整条消息
                # 流水线一起堵住（实测回复从 2 秒被拖到 27 秒）。丢后台跑 ——
                # 判定晚几秒入库无所谓，回复不能等它。
                import asyncio
                task = asyncio.create_task(self._handle(comp, category))
                self._bg.add(task)
                task.add_done_callback(self._bg_done)
                return
        except Exception as e:
            logger.warning("[mindscape_stickers] 采集失败: %s", str(e)[:140])

    def _bg_done(self, task):
        """后台任务收尾：扔掉引用 + 把异常捞出来（不然会被静默吞掉）。"""
        self._bg.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("[mindscape_stickers] 后台采集出错: %s", str(exc)[:140])

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

        # 分辨率闸门：尺寸过大的一律视为「抓错了」（游戏截图 / 壁纸 / 壁纸级同人图），
        # 直接不入库。放在视觉判定之前 —— 既省一次 API，也不必把几 MB 的图 base64
        # 传上去。（判据用分辨率而不是体积，理由见 img_size 的注释。）
        max_side = int((self.s_c.get("judge") or {}).get("max_side") or 0)
        if max_side > 0:
            # 变量名不能叫 h：上面 h 已经是 md5，下面还要拿它拼文件名。
            # 复用的代价是 'int' object is not subscriptable —— 通过闸门的图全存不进去。
            w, ih = img_size(path)
            if w and ih and max(w, ih) > max_side:
                self.seen.add(key)      # 记下：同一张不必反复下载重判
                self._save_seen()
                logger.info("[mindscape_stickers] 跳过 %dx%d（超过 %d，疑似截图/壁纸）",
                            w, ih, max_side)
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
        raw, mime = shrink_for_judge(path)
        if not raw:
            return None
        b64 = base64.b64encode(raw).decode()
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