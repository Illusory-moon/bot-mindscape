# -*- coding: utf-8 -*-
"""mindscape_voice —— 让她**自己决定**要不要用语音说话（2026-10-23）

机制**照抄表情包「她自己决定」那条路** ✓（`send_sticker` 的形状）：
  给她一个 **`say_voice` 工具** —— 想用声音说的那一刻就调它，不想就用文字 ✓
  ⚠️ **没有**表情包那条「每 N 条按概率强制发一次」—— 主人明确不要 ✗
  ⚠️ **也不额外问一次模型** —— 判断由**主模型**在同一回合里完成 ✓（主人 2026-10-23 指出）

三条硬约束：
  1. **语音模型只喂「她要说的那一句」** —— 不塞人格 / 记忆 / 上下文
  2. **音色只复刻一次**，之后复用 voice_id（= 语音缓存）；同一句按哈希只合成一次
  3. 私域信息（域名 / workspace / voice_id / key 路径）**全在 config** ✗ 不写进代码（铁律 §1）
"""
import asyncio
import datetime
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from astrbot.api import llm_tool, logger, star
from astrbot.core.message.components import Record
from astrbot.core.message.message_event_result import MessageEventResult

import mindscape_config as cfg
from mindscape_core import scope_hit

SECTION = "voice"


def vcfg(config=None):
    c = config if isinstance(config, dict) else (cfg.section(SECTION) or {})
    return c or {}


def va_archive_target(ev):
    """这条会话该写哪个发言档案 ⇒ (path, label)；找不到就 (None, None)。

    ⚠️ **全从 config 的 `archive.targets` 读**（铁律 §1：路径 / 群号 / 人设名一律不进代码 ✗）
    """
    try:
        arch = cfg.section("archive") or {}
        targets = arch.get("targets") or []
        sid = str(ev.get_self_id())
        umo = str(getattr(ev, "unified_msg_origin", "") or "")
        for t in targets:
            if not isinstance(t, dict):
                continue
            if str(t.get("self_id")) != sid:
                continue
            prefix = str(t.get("prefix") or "")
            if not prefix or not umo.startswith(prefix):
                continue
            path = str(t.get("file") or "")
            if not path:
                continue
            gid = umo.split(":")[-1]
            labels = t.get("labels") or {}
            return path, str(labels.get(gid) or gid)
    except Exception:
        pass
    return None, None


def va_archive_voice(ev, text):
    """把**语音的正文**追加进发言档案（2026-10-25 主人定）。

    为什么需要：语音是 `Record` 部件、**没有 `text`** ⇒ janitor 的 `_arch_text()` 看不见它 ✗
              ⇒ 那条回复不进档案 ⇒ `recall_memory` 取不回她"说过"的语音 ✗
    ⚠️ 纯文件追加、零 token、异常全吞；行格式与 janitor **逐字一致**（只把 label 标上「·语音」）✓
    ⚠️ 档案是**查阅型**（recall 按需读）⇒ 写它**不影响 prompt 缓存** ✓
    """
    try:
        txt = " ".join(str(text or "").split())
        if not txt:
            return
        path, label = va_archive_target(ev)
        if not path:
            return
        line = "- [%s][%s·语音] %s" % (
            datetime.datetime.now().strftime("%m-%d %H:%M"), label, txt)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        logger.warning("[mindscape_voice] 语音档案写入失败: %s", str(e)[:80])


def vc_cache_path(conf, text):
    """按「音色 + 正文」哈希命名 ⇒ 同一句只合成一次 ✓（天然的语音缓存）。"""
    d = str(conf.get("cache_dir") or "/opt/astrbot/data/voice_cache")
    os.makedirs(d, exist_ok=True)
    h = hashlib.sha1(("%s|%s" % (conf.get("voice_id") or "", text)).encode("utf-8")).hexdigest()[:20]
    return os.path.join(d, h + ".mp3")


def vc_synth(conf, text):
    """调云端 TTS 合成 → 存 mp3 → 返回路径（失败返回 None）。

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
        "model": str(conf.get("model") or ""),
        "input": {"text": text, "voice": vid, "format": "mp3", "sample_rate": 24000},
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
            self.v_cfg = vcfg()
            # 诊断（2026-10-31）：线上出现"有配置但说缺 api_base"，先把它读的**是哪份**打出来
            try:
                import os as _os
                _p = cfg.config_path()
                _mt = time.strftime("%m-%d %H:%M", time.localtime(_os.path.getmtime(_p))) \
                    if _os.path.exists(_p) else "不存在"
                logger.info("[mindscape_voice] 配置来源 | path=%s | mtime=%s | api_base=%s | voice_id=%s",
                            _p, _mt, bool(str(self.v_cfg.get("api_base") or "").strip()),
                            bool(str(self.v_cfg.get("voice_id") or "").strip()))
            except Exception as _e:
                logger.warning("[mindscape_voice] 诊断失败: %s", str(_e)[:80])
            logger.info("[mindscape_voice] loaded | enabled=%s targets=%s 只私聊=%s 上限=%s字",
                        bool(self.v_cfg.get("enabled")),
                        self.v_cfg.get("targets") or [],
                        bool(self.v_cfg.get("private_only", True)),
                        int(self.v_cfg.get("max_chars") or 120))
        except Exception as e:
            logger.warning("[mindscape_voice] setup 失败: %s", str(e)[:100])
            self.v_cfg = {}

    def _v_gate(self, ev, conf):
        """能不能说语音 ⇒ (ok, 拒绝理由)。**群聊一律拒**（主人 2026-10-23：先只私聊）✓"""
        if not conf.get("enabled"):
            return False, "语音现在没开"
        if ev is None:
            return False, "拿不到会话，说不了语音"
        try:
            if not scope_hit(conf.get("targets"), ev.get_self_id()):
                return False, "这个号不能说语音"
            gid = str(ev.get_group_id() or "")
            if conf.get("private_only", True) and gid:
                return False, "只在私聊能说语音，群里用文字"
            # ⚠️ 群白名单（显式 ✓ 空表**不等于**全开）：只在这些群才允许说语音
            gl = [str(x) for x in (conf.get("groups") or []) if str(x).strip()]
            if gid and gl and gid not in gl:
                return False, "这个群不开语音，用文字"
        except Exception:
            return False, "判断不了当前会话"
        return True, ""

    @llm_tool(name="say_voice")
    async def say_voice(self, *args, **kwargs):
        """用**语音**把这句话说出来（纯语音，不带文字）。

        什么时候用：你想用**声音**说的时刻 —— 撒娇、逗人、笑出声、突然正经地关心他、
        压低声音说重话，或者对方明确让你说句话 / 发条语音。

        什么时候**不要**用：纯信息、解释、长句、要点多的内容（那些用文字别人才看得清）；
        也别连着用 —— 偶尔来一下才有意思。

        Args:
            text(string): 要念出来的那句话。只写你要说出口的话，不要写括号里的动作或心理描写。
        """
        text = str(kwargs.get("text") or "").strip()
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        conf = getattr(self, "v_cfg", None) or {}
        ok, why = self._v_gate(ev, conf)
        if not ok:
            return why + " —— 用文字说吧"
        if not text:
            return "要念的话是空的 —— 把想说的话写进 text"
        max_chars = int(conf.get("max_chars") or 120)
        if len(text) > max_chars:
            return "这句 %d 字，超过 %d 就念不动了 —— 用文字说，或者缩短了再说" % (len(text), max_chars)
        try:
            loop = asyncio.get_event_loop()
            path = await loop.run_in_executor(None, vc_synth, conf, text)
        except Exception as e:
            logger.warning("[mindscape_voice] 合成异常: %s", str(e)[:120])
            path = None
        if not path:
            return "语音没合成出来 —— 还是用文字说吧"
        # ⚠️ 2026-10-23 主人要求：正文**不发群里**，但**日志里必须看得到** ⇒ 否则不可观测 ✗
        #    2026-10-25 改：**不截断**（原来 text[:80] ⇒ 超 80 字的语音在日志里只剩半句 ✗，
        #    补档时 46 条里有 11 条因此是残句 ✓）；上限由 max_chars 管（默认 120）✓
        logger.info("[mindscape_voice] 语音已发出 | %d 字 → %s | 正文: %s",
                    len(text), os.path.basename(path), text)
        # ⚠️ 2026-10-25 主人定：语音也要进发言档案 —— 否则 recall 取不回她"说过"的话 ✗
        va_archive_voice(ev, text)
        try:
            r = MessageEventResult()
            r.chain = [Record(file=path)]
            return r
        except Exception as e:
            logger.warning("[mindscape_voice] 构造结果失败: %s", str(e)[:120])
            return "发语音失败 —— 还是用文字说吧"
