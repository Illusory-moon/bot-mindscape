# -*- coding: utf-8 -*-
"""mindscape_groupctx —— 上下文补齐：把「没唤醒时群友说的话」也带进这一轮

问题：唤醒判定发生在**框架的消息分发阶段**，比插件执行更早 ——
没被唤醒的群消息根本不进 LLM 上下文。于是群友 A 说「我吃了 KFC」（没叫 bot）、
B 说「带我去吃呗」（叫了 bot）时，bot 只看到后半句，回一句「吃什么？」。
上下文不全，回复必然错位。

分工：框架侧补丁（`patches/astrbot`）负责把【所有】群消息落一份到磁盘；
本模块在这一轮真的调 LLM 之前，把该群最近的对话 + 本条消息的「定向性」注入上下文。

⚠️ 2026-10-08：这些内容**每轮都在变** ✗ —— 所以挂 `extra_user_content_parts` ✓（用户消息之后 ✓）
而不是拼进 `system_prompt` ✗ —— 拼在前面会把**后面的整段历史**的缓存全废掉 ✗（命中价差 50 倍 ✓）。
两半各管一段，是因为插件在唤醒阶段之后才拿得到消息。

定向性：群里 bot 最常见的两种错 ——
  1) 把群友之间的对话当成对自己说的（尤其低概率冒泡唤醒时）；
  2) 被 @ 了却看不出 —— 框架构建 message_str 时会把「@ 自己」剥掉，
     历史记录里那条只剩「发送者: 内容」。
所以直接把「这句话到底是不是对你说的」写进 prompt。

⚠️ **默认关闭**：它依赖 `patches/astrbot`（写缓冲 + 记 wake_reason）。
   没打补丁就打开，定向性会退化成「这句不是对你说的」—— **比不注入更糟**。
"""
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg
from mindscape_core import scope_hit, scope_warn

# 与 patches/astrbot 里补丁的落盘路径一致（补丁硬编码了这个绝对路径）
DEFAULT_BUFFER = "/opt/astrbot/data/group_ctx_buffer.jsonl"
DEFAULT_COUNT = 15          # 注入最近多少条
DEFAULT_WINDOW = 30 * 60    # 只取 30 分钟内的（太旧的不算上下文）
DEFAULT_TAIL = 512 * 1024   # 只读文件尾部这么多字节（够 800 行，即使每行接近上限长度）
GC_KEEP_LINES = 800         # 再从中取最后这么多行（与整读的旧实现等价）
GC_PRIORITY = 1             # 先于记忆注入：这条消息是「上下文」，记忆是「背景」
DEFAULT_IMG_MAX = 1         # 历史里的图最多挂几张（1 张就够：多一张 = 多一次视觉推理 = 慢十几秒）
DEFAULT_IMG_WINDOW = 120    # 只挂最近 2 分钟发过的图（「发完马上问」的窗口，再久基本无关）
DEFAULT_IMG_SAME = True     # 只挂「跟当前说话的人同一个发送者」的图（附图也慢，别乱挂）


def gc_buffer_path(conf):
    """缓冲文件路径。相对路径按【配置文件所在目录】解析（全项目一致的规矩）。"""
    raw = str(conf.get("buffer") or "").strip()
    if not raw:
        return DEFAULT_BUFFER
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(os.path.dirname(cfg.config_path()), raw))


def gc_tail_lines(path, tail_bytes, keep):
    """只读文件尾部的若干行。

    缓冲是追加流（写端 2MB 自截断），整读一遍纯属浪费 —— 实测 1.4MB / 7389 行，
    而 30 分钟窗口 + 只取 15 条根本用不到那么多。
    """
    with open(path, "rb") as fp:
        fp.seek(0, os.SEEK_END)
        size = fp.tell()
        start = max(0, size - tail_bytes)
        fp.seek(start)
        data = fp.read()
    lines = data.decode("utf-8", "ignore").split(chr(10))
    if start > 0:
        lines = lines[1:]        # 首行大概率被截断，丢掉
    return [ln for ln in lines if ln.strip()][-keep:]


def gc_read_recent(path, platform, group, limit, window_sec, tail_bytes):
    """从缓冲文件读该群最近的对话（窗口过滤 + 只留最后 limit 条）。"""
    if not path or not os.path.exists(path):
        return []
    try:
        lines = gc_tail_lines(path, tail_bytes, GC_KEEP_LINES)
    except Exception:
        return []
    now = time.time()
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if str(rec.get("platform")) != platform:
            continue
        if str(rec.get("group")) != str(group):
            continue
        if now - float(rec.get("ts") or 0) > window_sec:
            continue
        out.append(rec)
    return out[-limit:]


def gc_other_bot(text, names):
    """这条消息是不是在叫**别的 bot**（2026-10-08 加 ✓ 主人裁定：别人叫别的 bot 时别插话 ✗）。

    实况：群里除了她还有别的 bot ✓ 各有唤醒词 ✓ —— 有人叫「鲸鲸」（另一个 bot 的名字）时，
    那句话里也带着**她自己的名字** ✗ → 她照样被唤醒、照样插话 ✗。
    名字表进配置 ✓（代码里不留名字 ✓）；**没有配置就永远不触发** ✓（fail-open ✓ 不会误伤 ✓）。
    """
    t = text or ""
    for n in (names or []):
        s = str(n).strip()
        if s and s in t:
            return True
    return False


def gc_head(event):
    """本条消息的定向性 —— 四种情形各自一句。"""
    msgs = event.get_messages() or []
    me = str(event.get_self_id())
    at_self = any(type(c).__name__ == "At"
                  and str(getattr(c, "qq", "")) == me for c in msgs)
    reply_self = any(type(c).__name__ == "Reply"
                     and str(getattr(c, "sender_id", "")) == me for c in msgs)
    reason = event.get_extra("wake_reason")
    if at_self:
        return "本条消息【@ 了你本人】—— 它就是对你说的。"
    if reply_self:
        return "本条消息【引用了你说过的话】—— 它是接着你的话说的。"
    # ⚠️ 2026-10-08 加：**别人在叫别的 bot** ✗ —— 主人报的实况：有人叫「鲸鲸」（另一个 bot 的名字 ✓），
    #    那句话里也带着她的名字 ✗ → 她照样被唤醒、照样插话 ✗。群里不止一个 bot ✓ 各有各的唤醒词 ✓，
    #    所以先按**别的 bot 的名字表**（配置 groupctx.other_bot_names ✓ 代码里不留名字 ✓）判一次 ✓。
    try:
        _others = cfg.section("groupctx").get("other_bot_names") or []
    except Exception:
        _others = []
    if gc_other_bot(getattr(event, "message_str", ""), _others):
        return ("本条消息【提到了群里**别的 bot** 的名字】✗ —— 群里除了你还有别的 bot ✓ 它们各有各的名字 ✓。"
                "这句是在**叫它们** ✓ **不是在跟你说话** ✗。**别接话** ✓，"
                "除非同一句里明确 @ 了你、或者直接问的是你 ✓。")
    if reason == "mention":
        # ⚠️ 2026-10-08 主人裁定：这一条**不是「可以应」**✗ —— 得让她分清两种情形 ✓。
        #    实测（10-07/10-08 共 73 组）：群友在聊**游戏机制 / 剧情**（词条 / 伤害占比 / 配队 /
        #    剧情名词 ✓）时提到她的名字，她每次都读成「在说我这个人」✗ 于是插话 ✗。
        #    而她的个性是**设定允许的**（知识库《战斗设定》：「她会按主 C 流讲，这是人设不是无知」✓）
        #    → 所以**不能让她闭嘴** ✗（那是把人格掐了 ✗）—— 要给她的是**边界** ✓：
        #    「你可以觉得你该是主 c ✓ 但**不能打扰别人的规划** ✗」（主人原话 ✓）。
        return ("本条消息【没有 @ 你，但提到了你的名字】—— 先分清是哪一种：\n"
                "① 他们在**叫你**（名字在句首、后面跟着「，」、或带着问句 /「你」）→ 正常回 ✓；\n"
                "② 他们在**聊你**（你的游戏数据、配队、词条、伤害、剧情）→ 那是在讨论你这个角色 ✓ "
                "**不是在跟你说话** ✗ —— 这时候你可以有性格 ✓ 但要分清场合：\n"
                "   · **调侃可以** ✓（自夸、接梗、耍宝，那是你 ✓）；**嘴欠要看时机** ✗。\n"
                "   · 别人在**认真谈配置 / 规划 / 谁站哪个位置**时 ✗ —— **别去否定别人的方案** ✗。"
                "你可以觉得你该是主 c ✓ **但不能打扰别人的规划** ✓ —— 别用「我不该站后排」「让她自己来求我」"
                "这种话去顶掉别人的配队 ✗。\n"
                "   · 要么不接 ✓；要接就接一句**懂行**的话（你是这个角色 ✓ 游戏里的事你比谁都清楚 ✓）。")
    return ("本条消息【既没有 @ 你，也没有提到你的名字】—— 它多半是群友之间的对话，"
            "**不是对你说的**。要么不说；要说就只接一句【跟他们在聊的那件事有关】的话"
            "（附和、吐槽、递个梗都行），**别把话头拐回自己身上**、别借机汇报自己、也别替别人回答。"
            "不要把它当成在问你。")


def gc_quote(event):
    """本条消息引用了什么 —— 返回 (有没有引用, 引用的是不是我自己, 引用里有没有图)。

    为什么必须单独说一句：aiocqhttp 会 get_msg 把**被引用那条的完整组件链**塞进
    Reply.chain，框架还会把引用里的图渲染成 [Image Attachment in quoted message: …]
    混进**本条消息的正文**。模型只看正文，**看不出这张图是谁发的** ——
    实测：有人引用了 bot 自己发的表情包，bot 回头对着自己的图说「诶，这不是我嘛~」。
    但反过来也要说清：**引用别人的图，正是「让她看图」的正规入口之一**
    （先发图、再引用 + @ 她）—— 那种图就该看、该回应，不能一律当成「旧图，别理」。
    """
    msgs = event.get_messages() or []
    me = str(event.get_self_id())
    quoted = mine = has_img = False
    for c in msgs:
        if type(c).__name__ != "Reply":
            continue
        quoted = True
        mine = str(getattr(c, "sender_id", "") or "") == me
        for x in (getattr(c, "chain", None) or []):
            if type(x).__name__ == "Image":
                has_img = True
    if not has_img:                      # 兜底：链里拿不到，就看框架渲染进正文的那句标记
        try:
            if "Image Attachment in quoted message" in str(event.message_str or ""):
                has_img = True
        except Exception:
            pass
    return (quoted, mine, has_img)


def gc_quote_note(mine):
    """引用里那张图是谁发的 —— 只给方向，不写台词。"""
    if mine:
        return ("⚠️ 本条消息**引用的是你自己之前那条（带图或表情包）**：那张图是**你自己发的**，"
                "你本来就知道它长什么样 —— 不用对着它认图、点评、问「这是什么」，"
                "也**别把它说成是对方拿出来的、搬出来的**；除非有人明确让你聊这张图。")
    return ("⚠️ 本条消息**引用的是别人发的图**：这张图是**别人发出来给你看的**，"
            "该看就看、该接话就接话 —— 别当成你自己发过的东西。")


def gc_quote_rewrite(request, mine):
    """把自己发的引用图，在**请求正文里**就地改写成明确的归属。

    为什么不能只加系统提示：系统提示里那句笔记压不住「有人给我发了张图」的直觉 ——
    用户消息正文里明晃晃挂着 [Image Attachment in quoted message: path …] 和那张真图。
    实测 2026-10-03 22:50：她刚自己发的图 + 一句话，群友隔 21 秒引用回来 + @ 她，
    她下一句就把那张图说成是「对方搬出来顶包」的东西了 —— 归属判定没错（日志打出
    「引用=自己发的图」），是**那句话**把图说成了对方的素材。所以在正文里写清楚。
    """
    if not mine:
        return 0
    parts = getattr(request, "extra_user_content_parts", None)
    if not parts:
        return 0
    new_text = ("[引用里的图：这是**你自己**之前发出去的那张（连图一起被引回来了）—— "
                "对方只是接着你的话说，不是对方发给你的新图/新素材]")
    n = 0
    for i in range(len(parts)):
        p = parts[i]
        t = getattr(p, "text", None)
        if not (isinstance(t, str) and "Image Attachment in quoted message" in t):
            continue
        try:
            parts[i] = type(p)(text=new_text)   # 先换对象：pydantic 冻结模型也能改
        except Exception:
            try:
                p.text = new_text               # 普通可写模型
            except Exception:
                continue
        n += 1
    return n


def gc_has_image(event):
    """本条消息**自己**带图吗（引用里的图不算 —— 那条走 gc_quote）。"""
    try:
        return any(type(c).__name__ == "Image" for c in (event.get_messages() or []))
    except Exception:
        return False


def gc_history_images(recs, window_sec, cap, now=None, sender=None):
    """群缓冲里「还活着」的那几张图 —— [(路径, 谁发的, 时间)]，新的在前。

    主人 2026-10-04：「不管引用与否，真人都看得见图，能在我们这边优化的就在这边优化，
    不要指望用户端改」→ 历史消息里带的图，我们自己也挂上（缓冲里补丁记的 imgs）。
    边界：只认缓冲里记过 imgs 的记录、只认文件还在的、只取最近 window_sec、最多 cap 张。
    """
    if not recs or cap <= 0:
        return []
    now = time.time() if now is None else now
    out, seen = [], set()
    for r in reversed(list(recs)):
        try:
            ts = float(r.get("ts") or 0)
        except Exception:
            continue
        if window_sec and now - ts > window_sec:
            continue
        if sender is not None and str(r.get("uid") or "") != str(sender):
            continue
        for p in (r.get("imgs") or []):
            p = str(p)
            if p in seen:
                continue
            seen.add(p)
            out.append((p, str(r.get("who") or "?"), ts))
            if len(out) >= cap:
                return out
    return out


GC_IMG_CACHE = {}
GC_IMG_CACHE_MAX = 32


async def gc_resolve_ref(ref):
    """把一条「图片引用」变成能喂给模型的本地路径。

    缓冲里记的可能是本地路径（图已经落过盘），也可能是 URL —— 唤醒判定在预处理之前，
    那时候只有 URL。是 URL 就地补一次下载（AstrBot 自己的下载器，带它的证书/代理处理）。
    """
    ref = str(ref or "")
    if not ref:
        return ""
    if ref.startswith("file://"):
        ref = ref[7:]
    try:
        if os.path.exists(ref):
            return ref
    except Exception:
        return ""
    if not ref.startswith(("http://", "https://")):
        return ""
    hit = GC_IMG_CACHE.get(ref)
    if hit:
        return hit if os.path.exists(hit) else ""
    try:
        from astrbot.core.utils.io import download_image_by_url
    except Exception:
        return ""
    p = ""
    try:
        p = await download_image_by_url(ref)
    except Exception as exc:
        logger.warning("[mindscape_groupctx] 历史图下载失败 %s: %s", ref[:60], str(exc)[:80])
        return ""
    if p and os.path.exists(p):
        if len(GC_IMG_CACHE) >= GC_IMG_CACHE_MAX:
            GC_IMG_CACHE.clear()
        GC_IMG_CACHE[ref] = p
        return p
    return ""


class GroupctxMixin:
    def setup(self, context):
        c = cfg.section("groupctx")
        self.gc_on = bool(c.get("enabled"))
        self.gc_path = gc_buffer_path(c)
        self.gc_count = int(c.get("count") or DEFAULT_COUNT)
        self.gc_window = int(c.get("window_sec") or DEFAULT_WINDOW)
        self.gc_tail = int(c.get("tail_bytes") or DEFAULT_TAIL)
        self.gc_mark = True if c.get("directness") is None else bool(c.get("directness"))
        self.gc_targets = [str(x) for x in (c.get("targets") or [])]
        self.gc_img_on = True if c.get("images") is None else bool(c.get("images"))
        self.gc_img_max = int(c.get("image_max") or DEFAULT_IMG_MAX)
        self.gc_img_window = int(c.get("image_window_sec") or DEFAULT_IMG_WINDOW)
        self.gc_img_same = (DEFAULT_IMG_SAME if c.get("image_same_sender") is None
                            else bool(c.get("image_same_sender")))
        logger.info(
            "[mindscape_groupctx] loaded | enabled=%s | buffer=%s | 最近 %d 条/%ds | 定向性=%s | 历史图=%s(max %d/%ds 同人=%s)",
            self.gc_on, self.gc_path, self.gc_count, self.gc_window, self.gc_mark,
            self.gc_img_on, self.gc_img_max, self.gc_img_window, self.gc_img_same)
        scope_warn(logger, "mindscape_groupctx", self.gc_targets, self.gc_on)

    @filter.on_llm_request(priority=GC_PRIORITY)
    async def gc_inject(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            if not self.gc_on or not scope_hit(self.gc_targets, event.get_self_id()):
                return
            gid = event.get_group_id()
            if gid is None:
                return
            # 自主冒泡轮不要群缓冲 —— 那会让它退化成「接别人的话」，
            # 而这一轮的意义是自己找话题。
            if event.get_extra("cron_job"):
                return
            now = time.time()
            recs = gc_read_recent(self.gc_path, event.get_platform_name(),
                                  str(gid), self.gc_count, self.gc_window,
                                  self.gc_tail)
            q_quoted, q_mine, q_img = gc_quote(event)
            hist_refs = []
            if self.gc_img_on and not gc_has_image(event) and not (q_quoted and q_img):
                hist_refs = gc_history_images(
                    recs, self.gc_img_window, self.gc_img_max * 3,
                    sender=str(event.get_sender_id()) if self.gc_img_same else None)
            hist_imgs = []
            ref2path = {}
            for _ref, _w, _t in hist_refs:
                if len(hist_imgs) >= self.gc_img_max:
                    break
                _p = await gc_resolve_ref(_ref)
                if _p:
                    hist_imgs.append((_p, _w, _t))
                    ref2path[str(_ref)] = _p
            img_no = {}
            for _k, (_p, _w, _t) in enumerate(hist_imgs, 1):
                img_no[_p] = _k
            lines = []
            if self.gc_mark:
                lines += ["", "【本条消息的定向性】", gc_head(event)]
                if q_quoted and q_img:
                    lines.append(gc_quote_note(q_mine))
                    if q_mine and gc_quote_rewrite(request, True):
                        logger.info("[mindscape_groupctx] 引用正文改写=自己发的图")
            if recs:
                lines.append("")
                lines.append("【本群最近的真实聊天记录（用于理解上下文，不要逐条回应，也不要复述）】")
                for r in recs:
                    tag = " ".join("［附件%d］" % img_no[ref2path[str(x)]]
                                   for x in (r.get("imgs") or []) if str(x) in ref2path)
                    lines.append("[" + time.strftime("%H:%M:%S", time.localtime(float(r.get("ts") or now)))
                                 + "] " + str(r.get("who", "?"))[:16] + ": "
                                 + str(r.get("text", ""))[:200]
                                 + (("  " + tag) if tag else ""))
            if hist_imgs:
                lines.append("")
                lines.append("【上面历史里带的那几张图，按顺序就是附件 1…%d（标了［附件N］的那条就是它）】"
                             % len(hist_imgs))
                lines.append("本条消息时间：%s。历史图虽然可见，并不等于本条消息在请你评价它。"
                             % time.strftime("%H:%M:%S", time.localtime(now)))
                for _k, (_p, _w, _t) in enumerate(hist_imgs, 1):
                    lines.append("附件%d = %s 在 %s 发的图（距本条约 %d 秒）"
                                 % (_k, _w, time.strftime("%H:%M:%S", time.localtime(_t)),
                                    max(0, int(now - _t))))
                lines.append("先回应本条消息。只有当本条没有明确指向那张历史图，且图只是与本条话题无关的"
                             "情绪或状态表达时，才不要在回复中谈图；否则可自然结合图来回答。"
                             "时间间隔只作判断线索，不能单独决定是否谈图。")
            if not lines:
                return
            # ⚠️ 2026-10-08（省钱要省在根上 ✓）：这一整段**每轮都在变**（定向性 + 本群最近聊天 ✗）——
            # 以前拼进 system_prompt ✗ → 它一变，**后面的整段对话历史全部按全价重算** ✗
            # （实测缓存命中率只有 34% ✓ 而未命中 ¥2/M vs 命中 ¥0.04/M = 差 **50 倍** ✗✗）。
            # 改挂到 `extra_user_content_parts` ✓ —— 这是框架自带的「接在用户消息之后」的位置 ✓
            # （框架自己的 astrbot/group_chat_context.py 就是这么用的 ✓）→ 稳定前缀不再被污染 ✓。
            try:
                # 用到处再导入 ✓（模块级导入在测试环境的假 astrbot 里会炸 ✗）
                from astrbot.core.agent.message import TextPart
                parts = getattr(request, "extra_user_content_parts", None)
                if parts is None:
                    parts = []
                    request.extra_user_content_parts = parts
                parts.append(TextPart(text=chr(10).join(lines)))
            except Exception as e:
                # 兜底：宁可费钱，不可丢上下文 ✓ —— 但要打警告 ✗（不然缓存没救回来都不知道 ✓）
                logger.warning("[mindscape_groupctx] 挂 extra_user_content_parts 失败（缓存会吃亏），退回 system_prompt: %s", str(e)[:90])
                request.system_prompt = ((request.system_prompt or "") + chr(10)
                                         + chr(10).join(lines))
            if hist_imgs:
                try:
                    urls = getattr(request, "image_urls", None)
                    if urls is None:
                        urls = []
                        request.image_urls = urls
                    for _p, _w, _t in hist_imgs:
                        if _p not in urls:
                            urls.append(_p)
                except Exception as exc:
                    logger.warning("[mindscape_groupctx] 历史图挂载失败: %s", str(exc)[:120])
            logger.info("[mindscape_groupctx] 注入 self=%s 群=%s 历史=%d 条 引用=%s 历史图=%d",
                        event.get_self_id(), gid, len(recs),
                        ("自己发的图" if (q_quoted and q_img and q_mine) else
                         "别人的图" if (q_quoted and q_img) else "无"),
                        len(hist_imgs))
        except Exception as exc:
            logger.warning("[mindscape_groupctx] 注入失败: %s", str(exc)[:120])
