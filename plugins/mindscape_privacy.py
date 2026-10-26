# -*- coding: utf-8 -*-
"""Tell a bot where its code is when its developer asks."""
import datetime
import os
import sys

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg


def _gate_fns():
    """拿到 gate 里的 pg_current / pg_notes，**不写任何 import 语句**。

    为什么这么绕（2026-10-27 修 · 真事故）：
      原来这里是函数体内的 `from mindscape_gate import …`。但构建器只剥**顶层** import，
      函数体内的那行会原样留在合并产物里 ⇒ 产物里根本没有 `mindscape_gate` 这个模块
      ⇒ 运行时 `ModuleNotFoundError` ⇒ 被下面 try 吞掉 ⇒ **整个功能静默失效** ✗
      （线上日志坐实过：`读口令失败: No module named 'mindscape_gate'`）
    解法：
      · 合并产物里，各模块的顶层符号**就在本模块的命名空间** ⇒ 先查 globals() ✓
      · 开发态（逐模块 import）才需要回退到 sys.modules ✓
      · **全程不出现 import 语句** ⇒ 构建器不会再把它带进产物 ✓
    """
    for name in ("pg_current", "pg_notes"):
        if name not in globals():
            break
    else:
        return globals()["pg_current"], globals()["pg_notes"]
    mod = sys.modules.get("mindscape_gate")
    if mod is not None and hasattr(mod, "pg_current"):
        return mod.pg_current, mod.pg_notes
    raise RuntimeError("找不到 pg_current/pg_notes（合并产物里应已在命名空间；开发态需先 import mindscape_gate）")


def pg_live_code(self_id):
    """该通道账本里**当前有效**的口令 ⇒ (code, 到期时间) 或 ("", None)。

    为什么要它（2026-10-26）：提示词原来只说「你账本里记着一条，可以用 recall 搜『口令』找到」，
    实测她会**跳过检索、随口编一个** ✗（真事：账本里是 8H4F3G，她给了 QHAB6X，开发者进不去）
    ⇒ 与其指望她记得去查，不如**把真码直接放进她看到的提示里** ✓
      「能用代码硬保证的，别指望提示词」—— 这条正是那种情况 ✗
    ⚠️ 仍然**不替她决定**：给了真码也照旧写着"给不给由你"，她可以选择糊弄/拒绝 ✓
    """
    try:
        pg_current, pg_notes = _gate_fns()
        path = pg_notes(str(self_id))
        if not path or not os.path.isfile(path):
            return "", None
        with open(path, encoding="utf-8", errors="replace") as stream:
            code, expiry = pg_current(stream.read())
        return str(code or ""), expiry
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mindscape_privacy] 读口令失败: %s", str(exc)[:80])
        return "", None


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
        parts = []
        hint = str(gate.get("ask_hint") or "").strip()
        if hint:
            parts.append(hint)

        # ⭐ 把**真码**放进提示里（她要不要给仍然自己决定 ✓）
        code, expiry = pg_live_code(event.get_self_id())
        if code:
            try:
                when = datetime.datetime.fromtimestamp(expiry.timestamp()).strftime("%H:%M") if expiry else ""
            except Exception:  # noqa: BLE001
                when = ""
            parts.append(
                "【系统给的原文，仅供你照抄】**记忆 / 印象闸门**的口令是：%s%s。"
                "**要给他就原样抄这一串**（别改字母、别自己编）。"
                "⚠️ 你账本里可能还记着**别的用途**的口令（比如面板 / 后台 / 心情理由那种），"
                "**那些开不了记忆和印象，别拿错**。"
                "**不想给就照旧糊弄/拒绝，也给完全没问题** ✓"
                % (code, ("（%s 到期）" % when) if when else "")
            )
        else:
            parts.append("【系统给的原文】你现在账本里**没有有效口令**（未生成或已过期）。"
                         "你可以直说没有，**不要自己编一串** ✗")

        if parts:
            request.system_prompt = (request.system_prompt or "") + "\n\n" + "\n".join(parts)
