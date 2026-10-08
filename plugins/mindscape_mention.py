# -*- coding: utf-8 -*-
"""mindscape_mention —— 表达层：真 @（点名）

为什么必须单独做：模型嘴里的「@某某」只是**正文里的字符** —— 被点的人收不到提醒、
也不会变成可点蓝字。框架里唯一会造真 At 的地方是 result_decorate 的 reply_with_mention，
而它只会 @「刚说话的那个人」。所以给 bot 一个工具，让它真想点名时能发出**真的 at 段**。

- 名字 → QQ：先吃群缓冲（每条消息都记了 uid + who），再问 OneBot 要全量成员名单兜底
- 出站：`MessageEventResult().at(name=…, qq=…)` —— 和 send_sticker 同一条路
- 节流：同一会话 15 秒只允许一次，防她连点把人刷屏
"""
import asyncio
import re
import time

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.components import At
from astrbot.core.message.message_event_result import MessageEventResult

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

MN_THROTTLE = 15            # 同一会话两次点名之间的最小间隔（秒）
MN_BUF_LIMIT = 200          # 从群缓冲最多取多少条来找人
MN_BUF_WINDOW = 1800        # 只认半小时内说过话的人
MN_BUF_TAIL = 512 * 1024
MN_PENDING_TTL = 120       # 排队的 @ 最多等这么久（跨轮了就丢掉，别挂到下一句去）
MN_HOOK_PRIORITY = 100
MN_LINES_MAX = 3          # 连发工具一次最多几条（真人也顶多连发两三句）
MN_LINE_GAP = 0.8         # 连发之间的停顿（秒）—— 真人也是一句一句敲的     # on_decorating_result：跑在 guard(999) 之后


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


def mn_line_chain(text):
    """把一段纯文本包成 MessageChain。

    `event.send()` 的签名是 `send(message: MessageChain)` —— 传字符串会炸
    （实测 2026-10-06：她调 say_lines 三次，全都是 `'str' object has no attribute 'chain'`）。
    路径随版本变，兜两层。"""
    for mod_path in ('astrbot.api.message_components', 'astrbot.core.message.components'):
        try:
            mod = __import__(mod_path, fromlist=['Plain', 'MessageChain'])
            plain = getattr(mod, 'Plain')
            chain = getattr(mod, 'MessageChain', None)
            if chain is None:
                from astrbot.core.message.message_event_result import MessageChain as chain
            return chain([plain(text)])
        except Exception:
            continue
    return None


def mn_clean_chain(chain, qq=None, name=None):
    """把正文里**手打的 @** 清掉（@ 已经由 At 组件负责 ✓）。

    两类都清：① `@昵称(123456)`（号会外泄 ✗）② 紧接着真 @ 的那份纯文本 `@昵称`
    （否则名字出现两遍 ✗）。返回改动次数。
    """
    n = 0
    for comp in chain or []:
        txt = getattr(comp, "text", None)
        if not isinstance(txt, str) or "@" not in txt:
            continue
        new = MN_AT_LITERAL.sub("", txt)
        if name:
            new = re.sub(r"^\s*@" + re.escape(str(name)) + r"\s*", "", new)
        if new != txt:
            comp.text = new
            n += 1
    return n


def mn_take_pending(pending, key, now, ttl=MN_PENDING_TTL):
    """取出一条排队的点名（过期的丢掉）→ (qq, name) 或 None。纯函数，自检直接跑。"""
    item = (pending or {}).pop(key, None)
    if not item:
        return None
    try:
        qq, name, ts = item
    except Exception:
        return None
    if ttl and now - float(ts) > ttl:
        return None
    return (str(qq), str(name or ""))



# @ 排队：**模块级**字典（不挂插件实例）—— 模块级钩子拿得到，跨钩子/跨实例都稳
MN_PENDING = {}
# 「这一轮她已经连发过了」：同样是模块级（钩子与工具拿到的 event 不一定同源 ✓）。
# 用途：say_lines 成功的这一轮**抑制正文** —— 不然她会同一轮答两遍（连发一次 + 正文一次），
# 读起来割裂 ✗（实测 2026-10-06 19:33：3 条讲配装 + 1 条另起话头「货币战争？all in芳芳…」）。
MN_SAID = {}            # key = "self_id|群" → (发了什么, 时间, event_id, message_id) ✓
MN_SAID_TTL = 300       # 状态在表里最多留多久（清理用 ✓）
# ⚠️ 2026-10-08：原先这里还有个 MN_SAID_WINDOW=40 的**时间窗口** ✗ —— 实测它会误伤新一轮 ✗
# （同一群 40 秒内开新轮 → 正文被当重复抑制 ✗）→ **已删除** ✓ 改按 message_id 判「同一轮」✓。
# 她手打的 @：实测 2026-10-06 22:04 —— 她一边调 at_user、一边在正文里又写了一遍
# 「@昵称(2166832487)」（她照抄的是**收到消息里**的渲染格式 ✓），于是群里看到：
#   [At] + @昵称(QQ号) + 正文  ✗ 号直接外泄。
# 规矩：**@ 由 At 组件负责**，正文里手打的一律清掉 ✓（号绝不外泄 ✓）。
MN_AT_LITERAL = re.compile(r"@[^\s@()（）]{1,24}[（(]\d{5,12}[)）]")


@filter.on_decorating_result(priority=MN_HOOK_PRIORITY)
async def mn_attach_hook(*args, **kwargs):
    """把排队的 @ 插到这条回复的**最前面**；这轮没正文就单独发一个 @。

    为什么用模块级钩子而不是类方法：类方法的 decorating 钩子在本部署里**没被调用**
    （同插件另外四个类方法钩子都跑得好好的，排查过 stop_event / 流式输出 / 注册行都在），
    模块级注册是 AstrBot 最标准的那条路，先换过来把功能做通。
    """
    try:
        event = None
        for a in args:
            if hasattr(a, "get_self_id"):
                event = a
                break
        if event is None:
            return
        key = "%s|%s" % (event.get_self_id(), event.get_group_id())
        item = mn_take_pending(MN_PENDING, key, time.time()) if MN_PENDING else None
        # ⚠️ 2026-10-07 实测踩坑：原先这里用 `.pop()` ✗ —— **取一次就没了** ✓，
        #    而她 say_lines 之后还会继续调工具 ✓，工具循环会**再生成一次正文** ✗ →
        #    第二次发送时 MN_SAID 已空 ✗ → 那句正文就漏进群了 ✗（群里看到「嗯，那本不属于我~…」✗）。
        #    改成 `.get()` ✓ 并把**同一轮的 event id** 也记下 ✓；
        #    只对「同一轮 ✓」或「40 秒内 ✓」生效，免得误伤同一群里**下一轮**的正文 ✗。
        #    已知上限：同一群 40 秒内开新轮，其正文可能被误抑制 ✓（升级路：换成真正的 turn id ✓）。
        # ⚠️ 2026-10-08 **二次踩坑** ✗：**绝不能用时间窗口** ✗ ——
        #    20:00:42 她刚用 say_lines 说完 ✓ 20:00:57 群里来了**新问题**（「朋克洛德是哪里」✓）
        #    只隔 **15 秒** ✓ → 上一轮的残留状态把**新一轮的正文**当成"重复"抑制掉了 ✗✗
        #    → 她那条回答**一个字都没发出去** ✗ 而会话历史里记着"已发出" ✗ → 她还以为讲过了 ✗
        #    （主人 20:02 报的「她说解释完了但我没看到」就是这个 ✓）。
        #    正确口径 ✓：**按这一轮的那条消息判** ✓ —— 同一个 event 或同一条 message_id 才算"同一轮" ✓；
        #    **新消息进来 = 新一轮 → 绝不抑制** ✓。
        said = MN_SAID.get(key) if MN_SAID else None
        if said:
            _same_turn = (len(said) > 2 and said[2] == id(event))
            _same_msg = (len(said) > 3 and said[3]
                         and str(said[3]) == str(getattr(
                             getattr(event, "message_obj", None), "message_id", "") or ""))
            if not (_same_turn or _same_msg):
                logger.info("[mindscape_mention] 上一条连发状态属于【别的消息】→ 不抑制正文 ✓")
                said = None
                MN_SAID.pop(key, None)
        enabled, targets = mn_load_config()
        if enabled and scope_hit(targets, event.get_self_id()):
            result = event.get_result()
            chain = getattr(result, "chain", None) if result is not None else None
            if chain:
                removed = mn_clean_chain(chain, (item or (None, None))[0],
                                         (item or (None, None))[1])
                if removed:
                    logger.info("[mindscape_mention] 清掉正文里手打的 @ %d 处", removed)
        if item is None and said is None:
            return
        # ① 她这一轮用 say_lines 说过了 → 不再另发正文（要补就该写进 lines 里 ✓）。
        #    有 @ 排队时不抑制 —— @ 是挂在正文前面的，抑制会把它一起吞掉 ✗。
        if said is not None and item is None:
            event.clear_result()
            logger.info("[mindscape_mention] 本轮已连发 %d 条 → 抑制正文 self=%s",
                        said[0], event.get_self_id())
            return
        if not item:
            logger.info("[mindscape_mention] 键对不上，丢弃排队（%s）", key)
            return
        qq, name = item
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if chain:
            chain.insert(0, At(qq=qq, name=name))
            mn_clean_chain(chain, qq, name)      # 手打的那份 @ 一并清掉（名字别出现两遍 ✓）
            logger.info("[mindscape_mention] @ 挂在回复前 self=%s qq=%s（带正文）",
                        event.get_self_id(), qq)
        else:
            event.set_result(MessageEventResult().at(name=name, qq=qq))
            logger.info("[mindscape_mention] @ 单独发 self=%s qq=%s（这轮没正文）",
                        event.get_self_id(), qq)
    except Exception as exc:
        logger.warning("[mindscape_mention] 挂 @ 失败: %s", str(exc)[:100])


class MentionMixin:
    def setup(self, context):
        self.mn_on, self.mn_targets = mn_load_config()
        self.mn_last = {}
        logger.info("[mindscape_mention] loaded | enabled=%s | targets=%s | 节流=%ds",
                    self.mn_on, self.mn_targets or "全部", MN_THROTTLE)
        scope_warn(logger, "mindscape_mention", self.mn_targets, self.mn_on)
        try:      # 诊断：到底注册了哪些 decorating 钩子、什么顺序
            import astrbot.core.star.star_handler as _sh
            _hs = [(h.handler_name, getattr(h, "extras_configs", {}).get("priority"))
                   for h in _sh.star_handlers_registry.get_handlers_by_event_type(
                       _sh.EventType.OnDecoratingResultEvent, only_activated=False)]
            logger.info("[mindscape_mention] decorating 钩子清单(%d): %s", len(_hs), _hs)
        except Exception as _exc:
            logger.warning("[mindscape_mention] 钩子清单读取失败: %s", str(_exc)[:120])

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

        **@ 会自动加在你这条回复的最前面** —— 所以紧接着把想说的话写出来就行（「@某某 你说的那句话…」）；
        不想说别的也可以，那就只发一个 @。

        什么时候用：有人明确让你「@ 一下 / 艾特一下 / 点名」某人，或者你自己真想喊谁过来看。
        ⚠️ 点了名之后，**别再在正文里手打「@某人」**（更不要写「@某人(QQ号)」）——
        @ 会自动挂在你这条回复的最前面 ✓，正文里直接写你要说的话就行。
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
        # 键用稳定字段：跨钩子拿到的 event 不保证同源（这条坑我们踩过），
        # 用 unified_msg_origin 会在响应侧对不上。self_id + 群号 就稳。
        key = "%s|%s" % (ev.get_self_id(), gid)
        now = time.time()
        if now - float(self.mn_last.get(key) or 0) < MN_THROTTLE:
            return "刚点过一次，等一会儿再点"
        members = await self.mn_members(ev, gid)
        qq, name = mn_match_member(who, members)
        if not qq:
            return "群里没找到「%s」这个人" % who[:20]
        self.mn_last[key] = now
        MN_PENDING[key] = (qq, name, now)
        logger.info("[mindscape_mention] 点名排队 self=%s 群=%s who=%s -> qq=%s key=%s obj=%s id=%s",
                    ev.get_self_id(), gid, who[:16], qq, key, type(self).__name__, id(self))
        return "点名排上了：它会加在你**这条回复的最前面** —— 接着把想说的话写完就行；不想多说，那就只发这个 @。"
    @llm_tool(name="say_lines")
    async def say_lines(self, *args, **kwargs):
        """一口气连发几条短消息 —— 每条单独成一个气泡（像真人想到一句打一句）。

        一条一句、最多 3 条，按顺序发出去。想先丢一句、再补一句的时候就用它。
        别用 send_message_to_user 连发：它会把好几条并成一条消息（实测过）。

        Args:
            lines(array): 要连发的短消息列表，一条一句。
        """
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        if ev is None:
            return "现在发不了"
        try:
            if not self.mn_hit(ev):
                return "现在发不了"
        except Exception:
            pass
        raw = kwargs.get("lines")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return "给我一个字符串列表，一条一句"
        parse = globals().get("parse_response")
        fl = globals().get("flatten")
        dp = globals().get("drop_period")
        f_c = (cfg.section("format") or {})
        join_with = f_c.get("join_with") or "，"
        drop = bool(f_c.get("drop_last_if_short", False))
        short_len = int(f_c.get("short_len") or 8)
        no_period = bool(getattr(self, "f_np", None)) and scope_hit(self.f_np, ev.get_self_id())
        items = []
        for x in raw:
            t = str(x or "").strip()
            if not t:
                continue
            if parse:
                try:
                    t = (parse(t)[0] or t).strip()
                except Exception:
                    pass
            if fl and scope_hit(getattr(self, "targets", []) or [], ev.get_self_id()):
                try:
                    t = fl(t, join_with, drop, short_len)
                except Exception:
                    pass
            if no_period and dp:
                try:
                    t = dp(t)
                except Exception:
                    pass
            t = t.strip()
            if t:
                items.append(t[:300])
            if len(items) >= MN_LINES_MAX:
                break
        if not items:
            return "没有可发的内容"
        sent = 0
        for t in items:
            chain = mn_line_chain(t)
            if chain is None:
                logger.warning("[mindscape_mention] 连发失败: 拿不到 MessageChain")
                break
            try:
                await ev.send(chain)          # 一条一次 → 单独气泡；也走 guard 的 send 级兜底
                sent += 1
            except Exception as exc:
                logger.warning("[mindscape_mention] 连发失败: %s", str(exc)[:100])
                break
            await asyncio.sleep(MN_LINE_GAP)
        logger.info("[mindscape_mention] 连发 self=%s 条数=%d/%d",
                    ev.get_self_id(), sent, len(items))
        # 失败时给**明确**的回话：以前写「发好了：0 条」自相矛盾 ——
        # 她（或任何模型）会读成「没东西可发」，而不是「工具坏了」，于是内容整条丢掉（13:39 真丢过一次）。
        if sent == 0:
            return "没发出去（这个功能现在有毛病）—— 把想说的话直接写在正文里就行，别绕路。"
        if sent < len(items):
            return "只发出去 %d 条（剩下的没发成）—— 剩下的话直接写在正文里。" % sent
        # 全部成功 → 记一笔，让 decorating 钩子把这一轮的正文抑制掉（同一轮别答两遍 ✓）。
        try:
            gid = ev.get_group_id()
        except Exception:
            gid = None
        if gid is not None:
            MN_SAID["%s|%s" % (ev.get_self_id(), gid)] = (
        sent, time.time(), id(ev),
        str(getattr(getattr(ev, "message_obj", None), "message_id", "") or ""))
        return ("发好了：%d 条（每条一个气泡）。这一轮要说的话就算说完了 —— "
                "还想补就写进 lines 里，不用再另发正文。" % sent)
