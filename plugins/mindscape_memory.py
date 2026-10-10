# -*- coding: utf-8 -*-
"""mindscape_memory —— 认知层：滑动窗口式长期记忆注入

问题：bot 的「记忆」如果只靠会话历史，一旦清理就失忆；
      如果每轮把整个记忆文件塞进 prompt，token 又会爆炸。

方案：把长期记忆写成人类可读的 Markdown 文件（由 diary 模块生成），
      每轮请求只注入「最近 max_chars 字」，并在条目边界截断，绝不切半句话。
      这样记忆独立于会话，且体积恒定。
"""
import os

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.entities import ProviderRequest

import mindscape_config as cfg
from mindscape_core import SYS_DECL, sys_tag

DEFAULT_MAX_CHARS = 2500
DEFAULT_MIN_CHARS = 50
DEFAULT_PEOPLE_CHARS = 800
DEFAULT_DIGEST_CHARS = 1200
DEFAULT_NOTES_CHARS = 800
SECTION_TITLE = "## 你的长期记忆"
SECTION_DIGEST = "### 你还记得的最近几天（每天一句）"
SECTION_NOTES = "### 你记下的账（自己用 save_note 维护的，比流水账可靠）"
# 风格层：从「本人语料」学来的说话方式（mindscape_learn 产的）。
# 单独成层，是因为它回答的是「怎么说」，而其它层回答「发生了什么」；
# 而且要能只给某一个 bot 开 —— 没配 style 的 bot 完全不受影响。
SECTION_STYLE = "### 你的说话习惯（长期观察出来的，用来对齐语气）"
SECTION_STYLE_STABLE = "#### 长期稳定的部分"
SECTION_STYLE_RECENT = "#### 最近的变化"
DEFAULT_STYLE_CHARS = 800
DEFAULT_STYLE_RECENT_CHARS = 400
# 风格层最容易写出的「认知 bug」有四类，这里逐条堵住：
#   1) 同一件事在两处出现 → 模型当成两个特征
#   2) 两处说法冲突     → 模型不知道该信哪个
#   3) 把风格当记忆     → 模型把「我习惯这么说」讲成「我记得……」
#   4) 宣告自己的口癖   → 拿着风格档案自我描述，出戏
STYLE_GUARD = (
    "**这两段都是「你说话的方式」，不是记忆、也不是事实。**\n"
    "- 别宣告它们（不要说「我平时喜欢用『沃』」），直接用出来就行。\n"
    "- 两段可能重复 —— 那是同一件事，不是两件，别当成两个特征。\n"
    "- 两段对不上时以「最近的变化」为准（说话习惯本来就在变）。\n"
    "- 别把它们当往事提起 —— 那是记忆的事，不归这里管。\n"
)
SECTION_RULES = "**你自己的规矩**"
HEADER_MARK = "## "
# ⚠️ 注入顺序 = **变化频率**（稳的在前、易变的在后）—— 这是**缓存契约**，不是排版偏好 ✓。
#    所有 part 都挂在**最后一条 user 消息之后**：同一轮里谁变了，**它后面**的全部按全价重算 ✗
#    （2026-10-09 主人实测：同一轮第 2/3 次请求载荷只长 118/134 token ✓ 而未命中 5,743/5,749 ✗
#     —— 差的就是「群缓冲一变 → 排在它后面的记忆块整块作废」✗）。
#    优先级从高到低：记忆块(10) → 群缓冲(-3) → 人物(-6) → 工具过滤(-30) → trace(-100)。
MEM_PRIORITY = 10           # 记忆块：风格(每天) → 摘要(每天) → 账本(偶发) → 记忆(约 10 分钟)
PEOPLE_PRIORITY = -6        # 人物画像：按说话者挑 → **每换一个人就变** → 排在最后一个 part ✓
PEOPLE_TITLE = "## 你认识的人"
# 尺寸靠模块级字典在两个 handler 之间传（日志仍是同一行同一格式 ✓）。
# 不用 event.set_extra：自检的桩 event 没有那个 API ✓（会被 try 吞掉、静默不注入 ✗）。
MEM_SIZES = {}


def _tail_lines(text, budget):
    """块本身超预算时，从尾部按行取到预算内（不切半行）。"""
    if budget <= 0:
        return ""
    out = []
    used = 0
    for ln in reversed(text.splitlines()):
        add = len(ln) + (1 if out else 0)
        if used + add > budget:
            break
        out.insert(0, ln)
        used += add
    return "\n".join(out)


def read_recent(path, max_chars, skip_prefix=""):
    """取最近的记忆，严格不超过 max_chars（从最新条目向前累计）。

    做法：从文件尾部往前扫，按「条目 / 标题」为单位累加，
    一旦加入下一段会超预算就停 —— 保证返回长度是硬上限，且不切半句话。
    """
    if not path or not os.path.exists(path) or max_chars <= 0:
        return ""
    size = os.path.getsize(path)
    # 预算的 4 倍足够容纳多字节字符；再多读一点保证能拿到完整条目
    read_from = max(0, size - max_chars * 6)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        if read_from > 0:
            f.seek(read_from)
            f.readline()                       # 丢掉被截断的半行
        tail = f.read()

    lines = [line for line in tail.splitlines() if not skip_prefix or not line.startswith(skip_prefix)]
    # 从后往前，以「条目块」为单位累加（块 = 连续的非空行，遇到 ## 标题另起一块）
    blocks = []
    cur = []
    for ln in reversed(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith(HEADER_MARK):
            if cur:
                blocks.append(list(reversed(cur)))
                cur = []
            blocks.append([ln])
        else:
            cur.append(ln)
    if cur:
        blocks.append(list(reversed(cur)))

    picked = []
    used = 0
    for blk in blocks:
        text = "\n".join(blk).strip()
        if not text:
            continue
        add = len(text) + (1 if picked else 0)
        if used + add > max_chars:
            if not picked:
                # 最新一块自己就超预算时，「整块丢弃」会让记忆变成全空 —— 实测中
                # 一份 2385 字的成长记录配上 2000 字预算就是这个结果（返回 0 字，
                # bot 表现为「完全不记得任何人」）。退一步：取这一块的尾部（较新
                # 的部分），按行截断，宁可少记也不能全忘。
                keep = _tail_lines(text, max_chars)
                if keep:
                    picked.append(keep)
            break
        picked.insert(0, text)
        used += add
    return "\n".join(picked)


def read_head(path, max_chars):
    """从头读固定字数 —— **文档型**文件用这个。

    read_recent 取的是**尾部**，那是给「追加式」文件（日记、账本）设计的：
    越新的越该进 prompt。但稳定层是每次**覆盖写**的一份完整文档，
    取尾部等于把开头的「口癖」整段切掉、只留后半截 —— 正好切掉最有价值的部分。
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text.strip()
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > 0:
        cut = cut[:nl]
    return cut.strip()


def _clean_style(text):
    """去掉生成器留在文件头的 HTML 注释。

    那是**给人看的**（「每次覆盖写，请勿手改」），进 prompt 只是噪声。
    用纯行过滤而不是 re：这里只需要跳过以 <!-- 开头的行。
    """
    out = [ln for ln in (text or "").splitlines()
           if not ln.strip().startswith("<!--")]
    return "\n".join(out).strip()


def _resolve(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def _entry_name(line):
    """条目行开头的称呼（"- 昵称（备注）→ …" -> "昵称"）。

    不用 re：只切几个分隔符，纯字符串就够 ✓（自检的桩里少一层依赖 ✓）。
    """
    text = line[2:] if line.startswith("- ") else line
    for sep in ("（", "(", "：", ":", "→", "，", ",", " "):
        pos = text.find(sep)
        if pos > 0:
            text = text[:pos]
    name = text.strip()
    return name if len(name) >= 2 else ""


def _digits(line):
    """行里出现的**整段**数字串（用来认 QQ 号）。

    必须整段比对：直接 who_id in line 会让短号（"1"）命中任何含它的数字 ✗
    （AGENTS §4 记过同类：纯数字词只认精确命中 ✓）。
    """
    out, cur = [], []
    for ch in line:
        if ch.isdigit():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def read_people(path):
    """读画像文件 -> [(是否「重要的人」那一节, 条目行)]，保持文件里的先后顺序。

    只收 "- " 开头的条目行 —— 标题与「最后更新」是给人看的 ✓ 不进 prompt ✓。
    """
    if not path or not os.path.exists(path):
        return []
    out = []
    important = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip()
                if s.startswith("## "):
                    important = "重要" in s
                    continue
                if s.startswith("- "):
                    out.append((important, s))
    except Exception:
        return []
    return out


def pick_people(entries, max_chars, who_id="", who_name="", said=""):
    """按说话者挑画像：当前说话者 → 重要的人 → 本条消息里提到的人；其余不注入 ✓。

    2026-10-09 主人批 ✓：画像文件已长到 17 KB / 240 行，旧做法（从头截断 people_chars 字）
    会让排序靠后的人**整批看不见** ✗（那段 ponytail 注释预言的正是这个 ✗），而每轮真正
    用得上的只有「现在跟我说话的是谁」✓（印象插件就是按当前说话者注入的 ✓）。
    长尾改成按需查（recall_memory）✓ —— 体积 2400 字 → 几百字 ✓。
    """
    if max_chars <= 0:
        return ""
    who_id = str(who_id or "").strip()
    who_name = str(who_name or "").strip()
    said = said or ""
    mine, key, talked = [], [], []
    for important, line in entries:
        if (who_id and who_id in _digits(line)) or (who_name and who_name in line):
            mine.append(line)
            continue
        name = _entry_name(line)
        if name and name in said:
            talked.append(line)
            continue
        if important:
            key.append(line)
    picked, used = [], 0
    for line in mine + key + talked:
        if line in picked:
            continue
        add = len(line) + (1 if picked else 0)
        if used + add > max_chars:
            break
        picked.append(line)
        used += add
    return "\n".join(picked)


def read_many(paths, max_chars):
    """按顺序读多个记忆文件，总量硬上限 max_chars（各文件先平分预算）。

    用途：一个 bot 的记忆可能分散在多份文件里 —— 例如日常记的日记，
    外加一份人格/成长档案。两份都要进 prompt，但总量不能失控。
    """
    paths = [p for p in paths if p]
    if not paths or max_chars <= 0:
        return ""
    share = max(200, int(max_chars / len(paths)))
    parts = []
    used = 0
    for p in paths:
        seg = read_recent(p, share).strip()
        if not seg:
            continue
        add = len(seg) + (1 if parts else 0)
        if used + add > max_chars:
            break
        parts.append(seg)
        used += add
    return "\n".join(parts)


class MemoryMixin:
    def setup(self, context):

        self.m_cfg = cfg.section("memory")
        logger.info(
            "[mindscape_memory] loaded | %d bot(s) 配置了记忆",
            len(cfg.bot_entries()),
        )

    def _find_bot(self, self_id):
        for b in cfg.bot_entries():
            if str(b.get("self_id", "")) == str(self_id):
                return b
        return None

    @filter.on_llm_request(priority=MEM_PRIORITY)
    async def inject_memory(self, event: AstrMessageEvent, request: ProviderRequest):
        try:
            bot = self._find_bot(event.get_self_id())
            if not bot:
                return
            path = _resolve(bot.get("diary"))
            max_chars = int(bot.get("memory_chars") or self.m_cfg.get("max_chars") or DEFAULT_MAX_CHARS)
            min_chars = int(self.m_cfg.get("min_chars") or DEFAULT_MIN_CHARS)

            # 支持额外记忆文件（extra_diaries），例如人格文件里的关系与约定
            extras = []
            for x in (bot.get("extra_diaries") or []):
                if isinstance(x, str) and x.strip():
                    extras.append(_resolve(x))
            mem = read_many(extras + [path] if extras else [path], max_chars)

            # 骨架层：前几天各一句（mindscape_digest 产的）。滑动窗口只够覆盖
            # 几小时，没有这一层，bot 每天都「忘了昨天」—— 细节可以让它去
            # recall，但「记不记得昨天发生过什么」必须是常驻的。
            dig = ""
            dig_path = _resolve(bot.get("digest"))
            if dig_path:
                d_chars = int(bot.get("digest_chars")
                              or self.m_cfg.get("digest_chars") or DEFAULT_DIGEST_CHARS)
                dig = read_recent(dig_path, d_chars)

            # 账本：bot 自己**当场写**的东西。日记与摘要都是后台生成的，检索只读，
            # 于是「承诺」没地方落笔 —— 说过的话下一轮就漂了。这本账就是为了让
            # 细节问题有一个稳定的答案。
            notes = ""
            nt_path = _resolve(bot.get("notes"))
            if nt_path:
                n_chars = int(bot.get("notes_chars")
                              or self.m_cfg.get("notes_chars") or DEFAULT_NOTES_CHARS)
                skip = "- 口令：" if cfg.section("privacy_gate").get("enabled") else ""
                notes = read_recent(nt_path, n_chars, skip_prefix=skip)

            sty = ""
            st_path = _resolve(bot.get("style"))
            if st_path:
                s_chars = int(bot.get("style_chars")
                              or self.m_cfg.get("style_chars") or DEFAULT_STYLE_CHARS)
                # 稳定层是文档 → 从头读；近期层是追加流 → 取尾
                sty = _clean_style(read_head(st_path, s_chars))

            # 近期层：最新口癖（mindscape_style 产的 recent）。与稳定层配对，
            # 没有它就退回「只有长期习惯」，没有稳定层就退回「只有最近」——
            # 两者都缺才完全不注入。
            sty2 = ""
            sr_path = _resolve(bot.get("style_recent"))
            if sr_path:
                sr_chars = int(bot.get("style_recent_chars")
                               or self.m_cfg.get("style_recent_chars")
                               or DEFAULT_STYLE_RECENT_CHARS)
                sty2 = _clean_style(read_recent(sr_path, sr_chars))

            # 规矩也是要注入的内容，必须一起参与这个「有没有东西可注入」的判断 ——
            # 一个白板起步的 bot（新接进来的通道）日记/摘要/账本/风格全空，
            # 只看那几样就会**连规矩一起被跳过**，于是它永远不知道该记账，
            # 账本也就永远是空的（鸡生蛋）。
            rules = [str(x).strip() for x in (bot.get("rules") or []) if str(x).strip()]
            # 尺寸交给 inject_people 记（它在最后一个 part ✓ 日志仍是一行同一格式 ✓）
            if len(MEM_SIZES) > 64:          # 兜底：正常每轮都被 pop 掉 ✓
                MEM_SIZES.clear()
            MEM_SIZES[id(request)] = [len(mem), len(dig), len(notes), len(sty)]
            if (
                len(mem) < min_chars
                and not dig
                and not notes
                and not sty
                and not sty2
                and not rules
            ):
                return

            old = getattr(request, "system_prompt", "") or ""
            _parts = getattr(request, "extra_user_content_parts", None)
            if _parts is None:
                _parts = []
                request.extra_user_content_parts = _parts
            # 幂等：旧位置（system_prompt）与新位置（parts）都要查 ✓ 免得重复注入 ✗
            if SECTION_TITLE in old or any(
                    SECTION_TITLE in str(getattr(p, "text", "")) for p in _parts):
                return

            # 光给记忆不够 —— 实测：它只在窗口里翻到一个就下了结论，而同一件事
            # 在日记里记着好几回。它把「我上下文里只有这些」当成了「总共就这些」。
            # 所以这里必须做两件事：
            #   1. 明说这只是最近一部分，不是全部
            #   2. 给出「什么情况下必须先查」的触发条件
            # 光靠工具描述不够：它压根没意识到自己需要查。
            # 触发条件刻意只写**抽象类别**（数量/名单/最值/时间指向），不写具体
            # 例子：具体例子永远列不全，而且会把没枚举到的场景整片漏掉。
            stable = (
                "以下是你自己记下来的往事，是你亲身经历的，可以自然地提起，"
                "但不要照本宣科地念，也不要说「根据我的记忆」这种话。\n\n"
                "**注意：下面只是你最近记下的一部分，不是你的全部记忆。**"
                "更早的事你能用 recall_memory 翻出来 —— 手头没有，不等于没发生过，"
                "别拿眼前这一点就当成了全部。\n\n"
                "**别人说过的，不等于事实。** 下面要是记着「某人说……」，那只是\n"
                "他讲过这句话，不是你自己查证过的结论 —— 可以拿来当谈资，\n"
                "但别替它背书，也别把它当成群里公认的规矩。\n"
                "讲的时候用你自己的方式就行，不用原样复述，也不必每次都点名是谁讲的。\n\n"
                "**遇到下面这几种，先查再答**：\n"
                "- 答案要「翻遍全部」才给得准的：数量、名单、最值、有没有发生过\n"
                "- 问的是具体细节（谁和谁、什么时候、你答应过什么）：先看下面\n"
                "  的「你记下的账」，账上没有的，再用 recall_memory 去翻\n"
                "- 问题指的是更早的时间：以前、上次、第一次、这几天\n"
                "- 你准备说「只有」「就这些」「没有」的时候\n"
            )
            # 规矩：每个 bot 自己的行为约束，写在配置里（不进代码，避免把
            # 某个人设特有的规矩硬编码进通用框架）。
            if rules:
                stable += SECTION_RULES + "\n" + "\n".join("- " + r for r in rules) + "\n\n"
            # 系统注入的**来源标记**（2026-10-10 主人批 ✓）：每次启动随机、猜不到 ✓
            # —— 一次启动内逐字不变 ✓（缓存安全 ✓），只随重启变化 ✓（重启本来就要冷一次 ✓）。
            stable += SYS_DECL
            request.system_prompt = old + "\n\n" + stable
            block = "\n\n" + SECTION_TITLE + "\n"
            # ⚠️ 块内顺序 = **变化频率**（稳的在前 ✓）：风格(每天) → 摘要(每天) →
            #    账本(偶发) → 记忆(约 10 分钟)。越靠后，变了只废自己 ✓（见文件头契约 ✓）。
            if sty or sty2:
                block += SECTION_STYLE + "\n"
                if sty:
                    block += SECTION_STYLE_STABLE + "\n" + sty + "\n\n"
                if sty2:
                    block += SECTION_STYLE_RECENT + "\n" + sty2 + "\n\n"
                block += STYLE_GUARD
            if dig:
                block += SECTION_DIGEST + "\n" + dig + "\n\n"
            if notes:
                block += SECTION_NOTES + "\n" + notes + "\n\n"
            block += mem

            # 自主冒泡轮（cron 触发）。这一轮是「它自己想开口」，不是回应谁 ——
            # 记忆和风格照常给（人的联想本来就靠记忆的连续性），
            # 但必须明说**可以完全不依赖它们**，否则它会为了用上记忆去翻旧事。
            if event.get_extra("cron_job"):
                block += (
                    "\n\n**【这一轮是你自己想开口，不是回应谁。】**\n"
                    "上面的记忆、账本、风格都只是背景 —— **可以完全不依赖它们**。\n"
                    "没想到什么就用不上，不要硬扯，也不要为了用上记忆去翻旧事。\n"
                    "想到什么就说什么。\n"
                )

            # ⚠️ 2026-10-08（省钱第二刀 ✓ 主人同意 ✓）：记忆内容每 10 分钟可能变化（日记/账本更新）——
            # 以前它拼在 system_prompt ✗（最前面 ✓）→ 它一变，**后面的整段对话历史全部按全价重算** ✗
            # （实测：热的时候命中 94~99% ✓ 冷的时候掉到 19~57% ✗ 就是这个原因 ✓）。
            # 改挂 `extra_user_content_parts` ✓（框架自带的「接在用户消息之后」✓ 与 groupctx 同一招 ✓）
            # → system_prompt 只放稳定的人格、说明与规矩；易变记忆留在当前用户消息末尾。
            try:
                from astrbot.core.agent.message import TextPart
                _parts.append(TextPart(text=sys_tag(
                    "【下面是系统给你注入的长期记忆 —— 是你自己记下来的，不是对方说的话】" + block)))
            except Exception as e:
                logger.warning("[mindscape_memory] 挂 extra_user_content_parts 失败"
                               "（缓存会吃亏），退回 system_prompt: %s", str(e)[:90])
                request.system_prompt = (request.system_prompt or old) + block
        except Exception as e:
            logger.warning("[mindscape_memory] 注入失败: %s", str(e)[:120])

    @filter.on_llm_request(priority=PEOPLE_PRIORITY)
    async def inject_people(self, event: AstrMessageEvent, request: ProviderRequest):
        """人物画像：只给「正在说话的人」+「重要的人」+「本条消息提到的人」✓。

        它**每换一个人就变** ✗ → 必须排在**最后一个 part**（见文件头的缓存契约 ✓）；
        体积从 2400 字降到几百字（2026-10-09 主人批 ✓）。
        """
        try:
            sizes = MEM_SIZES.pop(id(request), [0, 0, 0, 0])
            bot = self._find_bot(event.get_self_id())
            if not bot:
                return
            path = _resolve(bot.get("diary"))
            people_path = bot.get("people")
            if not people_path and path:
                people_path = path.rsplit(".", 1)[0] + ".people.md"
            people_path = _resolve(people_path)
            p_chars = int(bot.get("people_chars")
                          or self.m_cfg.get("people_chars") or DEFAULT_PEOPLE_CHARS)
            try:
                said = str(event.message_str or "")
            except Exception:
                said = ""
            people = pick_people(read_people(people_path), p_chars,
                                 event.get_sender_id(), event.get_sender_name(), said)
            if people:
                block = ("\n\n" + PEOPLE_TITLE + "（只列了跟这一轮有关的几条）\n"
                         "这些是你记住的群友，聊天时可以自然地认得他们；"
                         "没列出来的人**不等于**不认识 —— 要确认某个人是谁，"
                         "先用 recall_memory 翻自己的记忆。\n\n" + people)
                old = getattr(request, "system_prompt", "") or ""
                _parts = getattr(request, "extra_user_content_parts", None)
                if _parts is None:
                    _parts = []
                    request.extra_user_content_parts = _parts
                if not (PEOPLE_TITLE in old or any(
                        PEOPLE_TITLE in str(getattr(p, "text", "")) for p in _parts)):
                    try:
                        from astrbot.core.agent.message import TextPart
                        _parts.append(TextPart(text=sys_tag(block)))
                    except Exception as e:
                        logger.warning("[mindscape_memory] 挂 extra_user_content_parts 失败"
                                       "（缓存会吃亏），退回 system_prompt: %s", str(e)[:90])
                        request.system_prompt = (request.system_prompt or old) + block
            logger.info("[mindscape_memory] %s 注入 %d 字记忆 / %d 字摘要 / %d 字账本"
                        " / %d 字风格 / %d 字人物",
                        bot.get("name") or "你", sizes[0], sizes[1], sizes[2], sizes[3],
                        len(people))
        except Exception as e:
            logger.warning("[mindscape_memory] 人物注入失败: %s", str(e)[:120])