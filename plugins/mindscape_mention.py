# -*- coding: utf-8 -*-
"""mindscape_mention —— 表达层：真 @（点名）

为什么必须单独做：模型嘴里的「@某某」只是**正文里的字符** —— 被点的人收不到提醒、
也不会变成可点蓝字。框架里唯一会造真 At 的地方是 result_decorate 的 reply_with_mention，
而它只会 @「刚说话的那个人」。所以给 bot 一个工具，让它真想点名时能发出**真的 at 段**。

- 名字 → QQ：先吃群缓冲（每条消息都记了 uid + who），再问 OneBot 要全量成员名单兜底
- 出站：`MessageEventResult().at(name=…, qq=…)` —— 和 send_sticker 同一条路
- 节流：同一会话 15 秒只允许一次，防她连点把人刷屏
"""
import time

from astrbot.api import llm_tool, logger, star
from astrbot.core.message.message_event_result import MessageEventResult

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

MN_THROTTLE = 15            # 同一会话两次点名之间的最小间隔（秒）
MN_BUF_LIMIT = 200          # 从群缓冲最多取多少条来找人
MN_BUF_WINDOW = 1800        # 只认半小时内说过话的人
MN_BUF_TAIL = 512 * 1024


def mn_load_config():
    c = cfg.section("mention") or {}
    return bool(c.get("enabled")), [str(x) for x in (c.get("targets") or [])]


def mn_match_member(who, members):
    """在成员表里找 who → (qq, 显示名)；找不到给 ("", "")。

    顺序：纯数字当号 → 群名片/昵称精确 → 唯一的部分匹配（多个命中就不猜）。
    纯函数，自检直接跑。"""
    who = str(who or "").strip().lstrip("@").strip()
    if not who:
        return "", ""
    members = [m for m in (members or []) if m]
    if who.isdigit():
        for m in members:
            if str(m.get("user_id") or "") == who:
                return who, str(m.get("card") or m.get("nickname") or "")
        if 5 <= len(who) <= 12:      # 成员名单里没有也认：QQ 号本来就是「直接给号」的用法
            return who, ""
    for key in ("card", "nickname"):
        for m in members:
            v = str(m.get(key) or "").strip()
            if v and v == who:
                return str(m.get("user_id") or ""), v
    hits = []
    for m in members:
        for key in ("card", "nickname"):
            v = str(m.get(key) or "").strip()
            if v and (who in v or v in who):
                hits.append((str(m.get("user_id") or ""), v))
                break
    if len(hits) == 1 and hits[0][0]:
        return hits[0]
    return "", ""


class MentionMixin:
    def setup(self, context):
        self.mn_on, self.mn_targets = mn_load_config()
        self.mn_last = {}
        logger.info("[mindscape_mention] loaded | enabled=%s | targets=%s | 节流=%ds",
                    self.mn_on, self.mn_targets or "全部", MN_THROTTLE)
        scope_warn(logger, "mindscape_mention", self.mn_targets, self.mn_on)

    def mn_hit(self, event):
        return self.mn_on and scope_hit(self.mn_targets, event.get_self_id())

    async def mn_members(self, event, gid):
        """群成员表：先吃群缓冲（便宜），再问 OneBot 要全量名单。"""
        out, seen = [], set()

        def _add(uid, nick, card=""):
            uid = str(uid or "").strip()
            if not uid or uid in seen:
                return
            seen.add(uid)
            out.append({"user_id": uid, "nickname": str(nick or ""),
                        "card": str(card or "")})

        rd = globals().get("gc_read_recent")
        gt = globals().get("gc_buffer_path")
        if rd and gt:
            try:
                for r in rd(gt(cfg.section("groupctx")), event.get_platform_name(),
                            str(gid), MN_BUF_LIMIT, MN_BUF_WINDOW, MN_BUF_TAIL):
                    _add(r.get("uid"), r.get("who"))
            except Exception:
                pass
        try:
            bot = getattr(event, "bot", None)
            if bot is not None:
                data = await bot.call_action("get_group_member_list", group_id=int(gid))
                for m in (data or []):
                    if isinstance(m, dict):
                        _add(m.get("user_id"), m.get("nickname"), m.get("card"))
        except Exception as exc:
            logger.warning("[mindscape_mention] 取群成员失败: %s", str(exc)[:100])
        return out

    @llm_tool(name="at_user")
    async def at_user(self, *args, **kwargs):
        """真的 @ 一个人（发出去是**会响的提醒**，不是正文里打「@某某」四个字符）。

        什么时候用：有人明确让你「@ 一下 / 艾特一下 / 点名」某人，或者你自己真想喊谁过来看。
        什么时候别用：只是嘴上提到某人、群里闲聊 —— 那种直接用嘴说；一次只点一个人，别连点。

        Args:
            who(string): 要点的人 —— 群里的名字（昵称 / 群名片），或者直接给 QQ 号。
        """
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        if ev is None:
            return "现在点不了名"
        try:
            if not self.mn_hit(ev):
                return "现在不方便点名"
        except Exception:
            pass
        try:
            gid = ev.get_group_id()
        except Exception:
            gid = None
        if gid is None:
            return "私聊里没有「@」这回事，直接说话就行"
        who = str(kwargs.get("who") or "").strip()
        if not who:
            return "要点谁？给个名字或者号"
        key = str(ev.unified_msg_origin)
        now = time.time()
        if now - float(self.mn_last.get(key) or 0) < MN_THROTTLE:
            return "刚点过一次，等一会儿再点"
        members = await self.mn_members(ev, gid)
        qq, name = mn_match_member(who, members)
        if not qq:
            return "群里没找到「%s」这个人" % who[:20]
        self.mn_last[key] = now
        logger.info("[mindscape_mention] 点名 self=%s 群=%s who=%s -> qq=%s",
                    ev.get_self_id(), gid, who[:16], qq)
        try:
            return MessageEventResult().at(name=name, qq=qq)
        except Exception as exc:
            return "点名失败：" + str(exc)[:60]