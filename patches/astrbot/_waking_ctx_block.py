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


def _ms_record_ctx(event) -> None:
    """把群消息追加到缓冲文件（带文件锁 + 体积自截断）。"""
    try:
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