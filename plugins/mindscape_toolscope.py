# -*- coding: utf-8 -*-
"""mindscape_toolscope —— 工具箱按 bot 隔离（2026-10-07）。

问题：AstrBot 的 `@llm_tool` 是**全局注册**的 ✗ —— 属于某个 bot 的工具，
另一个 bot 也能看到、也能调 ✗（`soul_memory` 那个插件的 docstring 里早就记着这个坑 ✓）。

实测代价（2026-10-07 22:51）：有个 bot 看到一个**不属于它**的工具 → 调了 ✗ → 被拒 ✗ →
它把那条报错**读成了「对方那句话不属于我」**✗ → 在群里自言自语冒出一句 ✗。

做法：在 `on_llm_request` 的**最后一棒**（priority 很低 ✓）按 `self_id` 把不该给她的
工具从 `request.func_tool` 里摘掉 ✓。规则进配置 ✓：代码里没有 bot 号、也没有具体工具名 ——
`tool_scope.allow` 是一张「工具名 → 允许的 self_id 列表」表 ✓（不在表里的工具对所有 bot 可见 ✓）。
"""
import re

from astrbot.api import logger
from astrbot.api.event import filter

import mindscape_config as cfg


def ts_ids(raw):
    """把配置里的 self_id 列表/字符串归一成集合。"""
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return {x for x in re.split(r"[\s,，;；]+", str(raw or "").strip()) if x}


def ts_removed_for(allow, self_id):
    """返回「这个 bot 不该看到的工具名」列表（纯函数 ✓ 好测 ✓）。"""
    sid = str(self_id or "")
    out = []
    for name, allowed in (allow or {}).items():
        ids = ts_ids(allowed)
        if ids and sid not in ids:
            out.append(str(name))
    return out


class ToolscopeMixin:
    def setup(self, context):
        try:
            conf = cfg.section("tool_scope")
            logger.info("[mindscape_toolscope] loaded | %s | 隔离规则 %d 条",
                        "启用" if conf.get("enabled", True) else "关闭",
                        len(conf.get("allow") or {}))
        except Exception:
            pass

    @filter.on_llm_request(priority=-30)
    async def ts_scope_tools(self, event, request):
        """摘掉不属于当前 bot 的工具（见模块 docstring）。"""
        try:
            conf = cfg.section("tool_scope")
            if not conf.get("enabled", True):
                return
            allow = conf.get("allow") or {}
            if not isinstance(allow, dict) or not allow:
                return
            ts = getattr(request, "func_tool", None)
            if ts is None:
                return
            names = ts_removed_for(allow, event.get_self_id())
            if not names:
                return
            have = {getattr(t, "name", "") for t in (getattr(ts, "tools", None) or [])}
            gone = []
            for n in names:
                if n not in have:
                    continue
                try:
                    ts.remove_tool(n)
                    gone.append(n)
                except Exception:
                    pass
            if gone:
                logger.info("[mindscape_toolscope] self=%s 摘掉不属于它的工具: %s",
                            event.get_self_id(), gone)
        except Exception as e:
            logger.warning("[mindscape_toolscope] 过滤失败: %s", str(e)[:120])
