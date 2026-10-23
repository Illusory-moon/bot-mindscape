# -*- coding: utf-8 -*-
"""mindscape_voice —— 让她**开口说话**：把回复转成 QQ 语音消息

灰测口径（主人 2026-10-23 定 ✓）：
  * 只在**私聊**
  * 消息里出现「**语音**」两个字才触发（不是每条都发）
  * **纯语音**（不发文字）
  * **只给一个 bot 开**（`targets` 白名单）
  * 失败 / 太长 ⇒ **退回文字**（绝不静默 ✗）

⚠️ 两条硬约束：
  1. **语音模型只喂「她要说的那一句」** —— 不塞人格 / 记忆 / 上下文 ✓（主人明确要求）
  2. **音色只复刻一次**，之后一直复用 `voice_id` ⇒ 这就是「语音缓存」✓
     （每次现复刻 = 白烧钱 + 慢 ✗）

所有私域信息（域名 / workspace / voice_id / key 路径）**一律进 config** ✗ 不写进代码（铁律 §1）。
"""
import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg
from mindscape_core import scope_hit

SECTION = "voice"

# 语音块在结果管线里的位置：**排在 format(900) / sticker_use(850) 之后** ⇒ 拿到的已是最终正文 ✓
VOICE_PRIORITY = 840


def vcfg(config=None):
    """语音配置段（缺项一律给安全默认：关着 ✓）。"""
    c = config if isinstance(config, dict) else (cfg.section(SECTION) or {})
    return c or {}


def _msg_text(event):
    """取这一轮用户说的话（不同版本字段位置不同 ⇒ 逐个兜底）。"""
    try:
        t = getattr(event, "message_str", None)
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        pass
    try:
        t = event.get_message_str()
        if isinstance(t, str):
            return t
    except Exception:
        pass
    return ""


def vc_should(conf, event):
    """这一轮该不该发语音（灰度门）。"""
    if not conf or not conf.get("enabled"):
        return False
    try:
        if not scope_hit(conf.get("targets"), event.get_self_id()):
            return False
    except Exception:
        return False
    if conf.get("private_only", True):
        try:
            if event.get_group_id():
                return False
        except Exception:
            pass
    kws = [str(k) for k in (conf.get("keywords") or []) if str(k).strip()]
    if not kws:
        return False
    msg = _msg_text(event)
    return any(k in msg for k in kws)


def vc_cache_path(conf, text):
    """按「音色 + 正文」哈希命名 ⇒ 同一句只合成一次 ✓（天然的语音缓存）。"""
    d = str(conf.get("cache_dir") or "/opt/astrbot/data/voice_cache")
    os.makedirs(d, exist_ok=True)
    h = hashlib.sha1(("%s|%s" % (conf.get("voice_id") or "", text)).encode("utf-8")).hexdigest()[:20]
    return os.path.join(d, h + ".mp3")


def vc_synth(conf, text):
    """调阿里云 CosyVoice 合成 → 存 mp3 → 返回路径（失败返回 None）。

    ⚠️ 请求体里**只有 `input.text` 这一句** ✓ 没有任何人格/记忆 ✓
    """
    base = str(conf.get("api_base") or "").rstrip("/")
    vid = str(conf.get("voice_id") or "").strip()
    kf = str(conf.get("key_file") or "/opt/astrbot/data/.dashscope_key")
    if not base or not vid:
        logger.warning("[mindscape_voice] 缺 api_base / voice_id，跳过")
        return None
    try:
        key = open(kf, encoding="utf-8").read().strip()
    except Exception as e:
        logger.warning("[mindscape_voice] 读不到 key 文件: %s", str(e)[:80])
        return None
    out = vc_cache_path(conf, text)
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return out
    body = {
        "model": str(conf.get("model") or "cosyvoice-v3.5-flash"),
        "input": {
            "text": text,
            "voice": vid,
            "format": "mp3",
            "sample_rate": 24000,
        },
    }
    req = urllib.request.Request(
        base + "/services/audio/tts/SpeechSynthesizer",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("[mindscape_voice] 合成 HTTP %s: %s", e.code, e.read().decode()[:200])
        return None
    except Exception as e:
        logger.warning("[mindscape_voice] 合成异常: %s", str(e)[:120])
        return None
    url = (((d.get("output") or {}).get("audio") or {}).get("url"))
    if not url:
        logger.warning("[mindscape_voice] 没有音频 URL: %s", json.dumps(d, ensure_ascii=False)[:200])
        return None
    try:
        with urllib.request.urlopen(url, timeout=90) as rr, open(out, "wb") as f:
            f.write(rr.read())
    except Exception as e:
        logger.warning("[mindscape_voice] 下载音频失败: %s", str(e)[:120])
        return None
    if os.path.getsize(out) < 1024:
        return None
    return out


class VoiceMixin:
    def setup(self, context):
        try:
            self.v_cfg = vcfg(self.config if hasattr(self, "config") else None)
            if not self.v_cfg:
                self.v_cfg = cfg.section(SECTION) or {}
            logger.info("[mindscape_voice] loaded | enabled=%s targets=%s 关键词=%s",
                        bool(self.v_cfg.get("enabled")),
                        self.v_cfg.get("targets") or [],
                        self.v_cfg.get("keywords") or [])
        except Exception as e:
            logger.warning("[mindscape_voice] setup 失败: %s", str(e)[:100])
            self.v_cfg = {}

    @filter.on_decorating_result(priority=VOICE_PRIORITY)
    async def vc_speak(self, event: AstrMessageEvent):
        """把这一轮的正文换成语音（灰测：私聊 + 含「语音」+ 只在白名单 bot）。"""
        try:
            conf = getattr(self, "v_cfg", None) or {}
            if not vc_should(conf, event):
                return
            result = event.get_result()
            if result is None:
                return
            txt = (result.get_plain_text() or "").strip()
            if not txt:
                return
            max_chars = int(conf.get("max_chars") or 120)
            if len(txt) > max_chars:
                # QQ 语音有上限（约 60 秒）⇒ 太长就老实发文字 ✓ 绝不截断她的话 ✗
                logger.info("[mindscape_voice] 正文 %d 字 > %d，走文字 ✓ | 正文: %s",
                            len(txt), max_chars, txt[:80])
                return
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, vc_synth, conf, txt)
            if not path:
                logger.warning("[mindscape_voice] 合成失败 ⇒ 退回文字 ✓（不静默）| 正文: %s", txt[:80])
                return
            # ⚠️ **用到时才 import** —— 顶层 import 会让自检的假 astrbot 桩直接炸 ✗
            #    （而且组件模块在部分环境里未必在 api 包下 ⇒ 运行时兜底更稳 ✓）
            from astrbot.api.message_components import Record
            result.chain = [Record(file=path)]
            # ⚠️ 2026-10-23 主人要求：正文**不发群里**，但**日志里必须看得到** ⇒ 否则不可观测 ✗
            logger.info("[mindscape_voice] 语音已就绪 | %d 字 → %s | 正文: %s",
                        len(txt), os.path.basename(path), txt[:80])
        except Exception as e:
            # ⚠️ 任何异常都必须**保留原文**（不 clear）⇒ 退回文字 ✓
            logger.warning("[mindscape_voice] 语音链路出错（已保留文字）: %s", str(e)[:140])
