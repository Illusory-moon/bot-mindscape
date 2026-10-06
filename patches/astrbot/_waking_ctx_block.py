# ── bot-mindscape: 群聊上下文缓冲（install.py 插在模块级）──
# 注：这两个函数的命名由本补丁决定；线上容器若已有同名实现，install.py 会跳过。
# 未唤醒的群消息不进 LLM 上下文，bot 回复时「上下文不全」。
# 这里把【所有】群消息落一份到磁盘，由插件里的 mindscape_groupctx 模块读出来注入。


def _ms_render_chain(event) -> str:
    """按消息链渲染文本，**保留「@ 自己」**。

    为什么不能直接用 event.message_str：AstrBot 在构建它时会把「@ 本 bot」
    那一段去掉。结果写进缓冲的历史记录只剩「发送者: 内容」——
    bot 根本看不出那句话是直接对它说的，只当是群友闲聊。
    """
    parts = []
    for c in (event.get_messages() or []):
        n = type(c).__name__
        if n == "At":
            nm = getattr(c, "name", "") or ""
            parts.append("@" + (str(nm) or str(getattr(c, "qq", ""))))
        elif n == "AtAll":
            parts.append("@全体成员")
        elif n == "Plain":
            parts.append(str(getattr(c, "text", "") or ""))
        elif n == "Image":
            parts.append("[图片]")
        elif n == "Face":
            parts.append("[" + (str(getattr(c, "name", "") or "") or "表情") + "]")
        elif n == "Reply":
            parts.append("[引用]")
    s = "".join(parts).strip()
    return s or str(event.message_str or "")


def _ms_image_refs(event) -> list:
    """本条消息带的图 → 「引用」列表：本地路径优先，其次可下载的 URL。

    为什么是「引用」不是「路径」：唤醒判定在**预处理之前**（STAGES_ORDER 里
    WakingCheckStage 排在 PreProcessStage 前面），这时候图还没落成文件，只有 URL。
    所以先记下来，等插件真正要用了（on_llm_request，早已过预处理）再去下 / 直接用。
    """
    import os as _o
    out = []
    for c in (event.get_messages() or []):
        if type(c).__name__ != "Image":
            continue
        ref = ""
        for attr in ("path", "file", "url"):
            v = getattr(c, attr, "") or ""
            if not isinstance(v, str) or not v:
                continue
            if v.startswith("/") and _o.path.exists(v):
                ref = v
                break
            if not ref and v.startswith(("http://", "https://", "file://")):
                ref = v
        if ref and ref not in out:
            out.append(ref)
    return out


# ── 「别的 bot 的指令」识别（2026-10-06 加）─────────────────────────
# 场景：群里还有别的机器人（典型是**云崽系**：Yunzai-Bot + miao-plugin 那一挂 ✓），
# 它们的命令长这样：「#角色面板」「*<名字>光锥」「#<名字>圣遗物」——**恰好含 bot 的名字** ✗，
# 于是被「提到名字」规则叫醒 ✗，还容易被她记成「又来问面板/配置」✗ 写进印象层。
#
# 判据（主人 2026-10-06 定）：① # 或 * 开头 ② 「名字 + 面板/圣遗物/…」这种查询格式
# ③ **一般没有别的文字** ✗。三条同时满足才算指令 ✓。
#
# ⚠️ 不许把关键词直接拉黑 ✗ —— 有人真的问她游戏知识（「<名字>，行迹怎么点」）必须照常回 ✓；
#    所以只拦「#/** 开头 + 纯查询」这一种形态 ✓，规则写在配置里（ignore_cmd ✓ 本文件无硬编码语义 ✓）。
_MS_CMD_CFG_PATH = "/opt/astrbot/data/auto_wake_cfg.json"
_MS_CMD_CACHE = {"mtime": -1.0, "cfg": {}}
# 命令的「角色名」部分：允许空（#面板 ✓）或一个短名字（#<bot名>面板 ✓ / #<bot名>的面板 ✓），
# 不许含空格与标点 —— 一旦有别的字，说明这是句人话，不是命令 ✓
# 存**字符串**不是编译对象 ✓ —— 补丁插进别人的文件，不能假设顶层 import 过 re ✓
_MS_CMD_NAME = r"^[^\s，。！？、,.!?#*：:；;~～\-_/|]{0,12}$"


def _ms_cmd_cfg() -> dict:
    """读配置里的 ignore_cmd 段（按 mtime 缓存，每条消息只 stat 一次 ✓）。"""
    try:
        import json as _j, os as _o
        mt = _o.path.getmtime(_MS_CMD_CFG_PATH)
        if mt != _MS_CMD_CACHE["mtime"]:
            with open(_MS_CMD_CFG_PATH, encoding="utf-8") as _f:
                _doc = _j.load(_f) or {}
            _MS_CMD_CACHE["cfg"] = _doc.get("ignore_cmd") or {}
            _MS_CMD_CACHE["mtime"] = mt
    except Exception:
        pass
    return _MS_CMD_CACHE["cfg"]


def _ms_is_cmd_query(text) -> bool:
    """这条消息是不是「别的 bot 的查询指令」（是 → 不唤醒、不进缓冲 ✓）。"""
    cfg = _ms_cmd_cfg()
    if not cfg or not cfg.get("enabled", True):
        return False
    t = str(text or "").strip()
    if len(t) < 2 or t[0] not in (cfg.get("prefixes") or ["#", "*"]):
        return False
    body = t[1:].strip()
    if not body:
        return False
    import re as _r
    for k in (cfg.get("keywords") or []):
        if k and body.endswith(k):
            head = body[: -len(k)].strip()
            return bool(_r.match(_MS_CMD_NAME, head))
    return False


def _ms_record_ctx(event) -> None:
    """把群消息追加到缓冲文件（带文件锁 + 体积自截断）。"""
    try:
        # 别的 bot 的查询指令不进缓冲 ✓ —— 否则她回复时会在上下文里看见「#<名字>面板」✗，
        # 照样会以为有人在冲她问面板 ✓（拦截要在「到她手上之前」✓）。
        if _ms_is_cmd_query(_ms_render_chain(event)):
            return
        import json as _j, os as _o, time as _t, fcntl as _f
        _p = "/opt/astrbot/data/group_ctx_buffer.jsonl"
        _rec = {
            "ts": _t.time(),
            "platform": str(event.get_platform_name()),
            "self_id": str(event.get_self_id()),
            "group": str(event.get_group_id()),
            "uid": str(event.get_sender_id()),
            "who": str(event.get_sender_name() or event.get_sender_id())[:24],
            "text": _ms_render_chain(event)[:300],
        }
        _imgs = _ms_image_refs(event)
        if _imgs:
            _rec["imgs"] = _imgs
        if not _rec["text"].strip():
            return
        with open(_p, "a", encoding="utf-8") as _fp:
            _f.flock(_fp.fileno(), _f.LOCK_EX)
            _fp.write(_j.dumps(_rec, ensure_ascii=False) + chr(10))
            _f.flock(_fp.fileno(), _f.LOCK_UN)
        try:
            if _o.path.getsize(_p) > 2_000_000:
                _ls = open(_p, encoding="utf-8").readlines()[-2000:]
                open(_p, "w", encoding="utf-8").writelines(_ls)
        except Exception:
            pass
    except Exception:
        pass