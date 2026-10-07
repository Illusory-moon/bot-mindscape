# -*- coding: utf-8 -*-
"""mindscape_rescue —— 回复兜底（空回复救援 + 冒泡轮历史去任务化）

背景：推理模型（如 deepseek-flash）有时把全部输出放在 reasoning_content 里，
      content 为空；若这一次又没有 tool_calls，框架就会得到一条完全空的回复，
      然后静默 skip —— 用户看到的是「bot 没反应」。

本模块在「回复成形之前」检测这种完全空回复，用一次极轻量的调用补一句话，
避免出现「叫了不理」的观感。代价：仅在空回复时多一次请求。

设计上有意为之（未经深思不要「修」）：
    救援补话时是带着这个 bot 本轮真实的人设 / 记忆 / 风格去问的，
    所以它**允许**回答沉默令牌 —— 「这一轮到底该不该说话」这个判断，
    最终仍然由 bot 自己拿主意，救援只负责「别让它卡成一条空回复」。
    实测过：模型空正文 → 救援读完快照 → 回 [[silence]] → 沉默插件照常吞掉，
    并且在日志里留下「真静默（第 N 次）」。链路是通的，不要改成强制补话。

⚠️ 为什么挂 on_llm_response 而不是 on_decorating_result：
   后者所在的 result_decorate/stage.py 开头就是
       if result is None or not result.chain: return
   而空回复恰恰没有 chain —— 装饰钩子根本不会被调用，挂在那里等于永远不触发。
   on_llm_response 由 agent 的 on_agent_done 派发（tool_loop_agent_runner 的
   _complete_with_assistant_response，即「无工具调用的终止步」），此时
   final_llm_resp 就是同一个对象，改写 completion_text 会被
   internal.py 的 `if final_llm_resp.completion_text` 直接采用。

配置（可选）：
    rescue:
      enabled: true
      api_base: "https://api.deepseek.com/v1"
      api_key_env: "MINDSCAPE_API_KEY"
      model: "your-model"
      persona: "一句话描述你的人设"
      max_tokens: 120
      timeout: 20
"""
import os
import re
from collections import deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg


# 「出站类工具」—— 用了它们就等于这一轮已经把话发出去了 ✓ 救援不该再抢一句 ✗。
# 2026-10-07 加：实测她 say_lines 说完后，救援把「正文被抑制」误判为空回复 ✗。
RC_OUTBOUND_TOOLS = {"send_message_to_user", "say_lines", "at_user"}
RC_SENT = {}          # key = "self_id|group_id" → 时间戳 ✓ 跨钩子用模块级（event 存储不可靠 ✓）
RC_SENT_TTL = 180


def rc_sent_recent(event, now=None):
    """本轮（近 RC_SENT_TTL 秒）有没有出站类工具发过话。"""
    import time as _t
    key = "%s|%s" % (event.get_self_id(), event.get_group_id())
    ts = RC_SENT.get(key) or 0
    return (float(now if now is not None else _t.time()) - float(ts)) < RC_SENT_TTL


class RescueMixin:
    def setup(self, context):
        self.r_cfg = cfg.section("rescue") or {}
        env = self.r_cfg.get("api_key_env") or ""
        self.r_ready = bool((self.r_cfg.get("api_base") or "").strip()
                            and env and os.environ.get(env))
        # 一定要把「就绪没就绪」喊出来：以前拿不到 key 就静默 return，
        # 结果「空回复救援」一次都没生效过，日志里却一个字都没有。
        logger.info("[mindscape_rescue] loaded | %s | %s",
                    "启用" if self.r_cfg.get("enabled", True) else "关闭",
                    "就绪" if self.r_ready
                    else ("未就绪（缺 api_base 或环境变量 %s），空回复将无法兜住" % (env or "(未配置)")))

    @filter.on_llm_response()
    async def rescue_empty(self, event: AstrMessageEvent, response):
        if not self.r_cfg.get("enabled", True):
            return
        try:
            if response is None:
                return
            # 已经有文字 / 已经带了结果链（比如只发了图）/ 还要调工具，都不算空回复
            if (getattr(response, "completion_text", "") or "").strip():
                return
            # 走到这里 = 这一轮文字真的空了。先记一笔，方便日后定位；
            # 以前这里什么都不留，出问题时只能看到一句「The message is empty」。
            logger.info("[mindscape_rescue] 空文字回复 | chain=%s tools=%s ready=%s",
                        bool(getattr(response, "result_chain", None)),
                        bool(getattr(response, "tools_call_name", None)),
                        getattr(self, "r_ready", False))
            if not getattr(self, "r_ready", False):
                return
            # 有结果链不等于「有东西可发」：实测出现过「文字被清空、链里只剩
            # 空壳组件」的情况 —— 那时 rescue 必须出手，否则就是一次静默的「叫它不理」。
            _chain = getattr(getattr(response, "result_chain", None), "chain", None) or []
            _media = ("Image", "Record", "Video", "File", "Node", "Nodes")
            if any(type(_c).__name__ in _media for _c in _chain):
                return
            if getattr(response, "tools_call_name", None):
                return
            # ⚠️ 2026-10-07 实测踩坑：她这轮用 say_lines 连发 2 条 ✓，正文被 mention 模块
            # **主动抑制**掉了 ✓ —— 但这里把「被抑制」当成了「模型没吐字」✗，于是补了一条
            # **完全不属于她人格**的话发进群 ✗（`tools_call_name` 在这一步还是空的 ✗ 判据形同虚设 ✓）。
            # 规矩：**本轮只要已经用「出站类工具」说过话，就不许补** ✓（跨钩子状态存模块级 ✓）。
            if rc_sent_recent(event):
                logger.info("[mindscape_rescue] 本轮已用工具说过话 → 不补 ✓")
                return
            text = await self._ask_once(event)
            if text:
                response.completion_text = text
                logger.info("[mindscape_rescue] 空回复已补（%s）: %s",
                            "带人设快照" if str(event.get_extra("_ms_ctx_prompt") or "").strip()
                            else "通用兜底", text[:40])
        except Exception as e:
            logger.warning("[mindscape_rescue] 救援失败: %s", str(e)[:120])

    @filter.on_llm_request(priority=-10)
    async def rc_snapshot(self, event: AstrMessageEvent, request):
        """抓一份「这一轮真实用到的」人设 + 记忆 + 风格快照。

        priority=-10 让它最后跑 —— 等 memory / silence 都往 system_prompt 里塞完了再取，
        拿到的就是模型真正看到的那一段。救援补话时带上它，补出来才像这个 bot。
        以前救援用的是配置里那句通用人设 —— 对味道重的人设来说补出来就是白开水。
        """
        try:
            sp = getattr(request, "system_prompt", "") or ""
            if sp:
                event.set_extra("_ms_ctx_prompt", sp[-1400:])
            rows = []
            for m in (getattr(request, "contexts", None) or [])[-5:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                c = m.get("content")
                if isinstance(c, list):
                    c = " ".join((x.get("text") or "") for x in c if isinstance(x, dict))
                c = str(c or "").strip()
                if role in ("user", "assistant") and c:
                    rows.append(("对方" if role == "user" else "我") + "：" + c[:120])
            if rows:
                event.set_extra("_ms_ctx_recent", "\n".join(rows[-4:]))
        except Exception:
            pass

    @filter.on_using_llm_tool()
    async def rc_capture_sent(self, event: AstrMessageEvent, tool, tool_args):
        """记下这一轮真正发出去的话 —— 冒泡轮要用它替换任务黑话；顺便记「这轮已经出站过」✓。"""
        try:
            _name = getattr(tool, "name", "")
            if _name in RC_OUTBOUND_TOOLS:
                import time as _t
                RC_SENT["%s|%s" % (event.get_self_id(), event.get_group_id())] = _t.time()
            if _name != "send_message_to_user":
                return
            if not isinstance(tool_args, dict):
                return
            parts = []
            for m in (tool_args.get("messages") or []):
                if isinstance(m, dict) and m.get("type") == "plain" and m.get("text"):
                    parts.append(str(m["text"]))
            if parts:
                event.set_extra("_ms_sent_text", " ".join(parts)[:300])
        except Exception:
            pass

    @filter.on_llm_response()
    async def rc_clean_cron_meta(self, event: AstrMessageEvent, response):
        """冒泡轮：把「任务黑话」的总结换成它真正说过的那句话。

        AstrBot 的 cron 提示词要求模型「总结并输出你的动作和结果」，于是对话历史里
        会存下这种句子：

            [CronJob] bubble-xxx: 冒泡完成。动作：以<某人>身份在群里发了一句「…」，
            没提任务/定时，没提问，没刷屏。

        这些词（任务 / 定时 / 系统 / 身份）每轮都会被当作上下文喂回去，是出戏源头。
        但它同时也是「我冒过泡、说了什么」的唯一留痕 —— 所以不是删掉，而是**改写成
        第一人称**：人记住的是自己说过的话，不是「我完成了一个任务」。
        """
        try:
            if not event.get_extra("cron_job"):
                return
            if response is None:
                return
            txt = (getattr(response, "completion_text", "") or "").strip()
            if not txt:
                return          # 本来就没话，没什么可清的
            # 判据【不能】等 "[CronJob]" 前缀 —— 那个前缀是 AstrBot 在 runner 跑完之后
            # 自己拼上去的，模型自己写的那段根本没有它（所以这条逻辑空转了四天）。
            # 冒泡轮里模型只能靠工具说话，收尾那段必然是「任务总结」，直接换掉即可。
            sent = str(event.get_extra("_ms_sent_text") or "").strip()
            if sent:
                response.completion_text = sent
                logger.info("[mindscape_rescue] 冒泡轮历史去任务化（原文 %d 字）-> %s", len(txt), sent[:40])
            else:
                response.completion_text = ""
                logger.info("[mindscape_rescue] 冒泡轮没发话，历史不留痕")
        except Exception as e:
            logger.warning("[mindscape_rescue] 冒泡轮清理失败: %s", str(e)[:120])

    async def _ask_once(self, event):
        import httpx
        api_base = (self.r_cfg.get("api_base") or "").rstrip("/")
        key = os.environ.get(self.r_cfg.get("api_key_env") or "", "")
        if not api_base or not key:
            return ""
        # 优先用「这一轮真实的人设/记忆/风格」快照；配置里的 persona 只当兜底
        persona = (str(event.get_extra("_ms_ctx_prompt") or "").strip()
                   or self.r_cfg.get("persona") or "一个自然的聊天伙伴")
        recent = str(event.get_extra("_ms_ctx_recent") or "").strip()
        last = ""
        try:
            data = getattr(event, "message_obj", None)
            last = str(getattr(data, "message_str", "") or "")[:200]
        except Exception:
            last = ""
        related = self._identity_memory(event, last)
        prompt = (
            "下面是你的人设、记忆和说话风格（照着来，不要照抄原文）：\n"
            + persona
            + (("\n\n最近几轮对话：\n" + recent) if recent else "")
            + "\n\n刚才对方说了：\n" + (last or "（一条消息）")
            + (("\n\n关于当前发言者，你记下的往事：\n" + related) if related else "")
            + "\n\n请用你自己的口吻补一句自然的回应（不超过30字）。"
              "不要解释、不要客套、不要提及你是 AI，也不要提你刚才没说话。"
              "记忆片段不完整；不要因为眼前没有记录就断言不认识对方或没有档案。"
        )
        try:
            async with httpx.AsyncClient(timeout=float(self.r_cfg.get("timeout") or 20)) as cli:
                resp = await cli.post(
                    api_base + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={"model": self.r_cfg.get("model") or "gpt-4o-mini",
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": int(self.r_cfg.get("max_tokens") or 120)},
                )
            if resp.status_code != 200:
                return ""
            msg = resp.json()["choices"][0]["message"]
            txt = (msg.get("content") or "").strip()
            if not txt:
                txt = (msg.get("reasoning_content") or "").strip()
            txt = txt.strip().strip('"').strip("“”").strip()
            if txt and len(txt) > 60:
                txt = txt[:60]
            return txt
        except Exception as e:
            logger.warning("[mindscape_rescue] 补话失败: %s", str(e)[:100])
            return ""

    @staticmethod
    def _identity_memory(event, last):
        if not re.search(r"我是谁|你认识我|还记得我|认得我吗|我叫什么", last):
            return ""
        try:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            name = str(getattr(sender, "nickname", "") or getattr(sender, "name", "") or "").strip()
            if len(name) < 2:
                return ""
            sid = str(event.get_self_id())
            bot = next((b for b in cfg.bot_entries() if str(b.get("self_id")) == sid), {})
            paths = list(bot.get("extra_diaries") or []) + [bot.get("diary")]
            hits = deque(maxlen=4)
            # ponytail: rare rescue scans files once; reuse recall's index if these files grow large.
            for path in paths:
                if not isinstance(path, str) or not path:
                    continue
                if not os.path.isabs(path):
                    path = os.path.join(os.path.dirname(cfg.config_path()), path)
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("- ") and name in line:
                            hits.append(line.strip())
            return "\n".join(hits)[:700]
        except Exception as e:
            logger.warning("[mindscape_rescue] 身份记忆检索失败: %s", type(e).__name__)
            return ""
