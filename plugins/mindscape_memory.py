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


def read_recent(path, max_chars):
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

    lines = tail.splitlines()
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


def read_people(path, max_chars):
    """读人物画像文件，只保留条目行（跳过标题和更新时间）。"""
    if not path or not os.path.exists(path):
        return ""
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                if line.startswith("- "):
                    lines.append(line)
    except Exception:
        return ""
    out = "\n".join(lines)
    # ponytail: 这里按从头截断，而画像文件是按昵称排序的 —— 一旦文件超过
    # max_chars，排序靠后的群友会整批消失（实测：2175 字的画像配 800 字预算，
    # 正好把某位重要的人切掉，bot 于是完全不认得这个人）。当前对策是把
    # people_chars 配足装下整份文件；画像再长大时应改为按「最近出现」挑选条目，
    # 而不是按字母序切。
    return out[:max_chars]


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

    @filter.on_llm_request()
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
                notes = read_recent(nt_path, n_chars)

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

            if len(mem) < min_chars and not dig and not notes and not sty and not sty2:
                return

            old = getattr(request, "system_prompt", "") or ""
            if SECTION_TITLE in old:
                return

            label = bot.get("name") or "你"
            # 光给记忆不够 —— 实测：它只在窗口里翻到一个就下了结论，而同一件事
            # 在日记里记着好几回。它把「我上下文里只有这些」当成了「总共就这些」。
            # 所以这里必须做两件事：
            #   1. 明说这只是最近一部分，不是全部
            #   2. 给出「什么情况下必须先查」的触发条件
            # 光靠工具描述不够：它压根没意识到自己需要查。
            # 触发条件刻意只写**抽象类别**（数量/名单/最值/时间指向），不写具体
            # 例子：具体例子永远列不全，而且会把没枚举到的场景整片漏掉。
            block = (
                "\n\n" + SECTION_TITLE + "\n"
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
            rules = [str(x).strip() for x in (bot.get("rules") or []) if str(x).strip()]
            if rules:
                block += SECTION_RULES + "\n" + "\n".join("- " + r for r in rules) + "\n\n"
            if notes:
                block += SECTION_NOTES + "\n" + notes + "\n\n"
            if sty or sty2:
                block += SECTION_STYLE + "\n"
                if sty:
                    block += SECTION_STYLE_STABLE + "\n" + sty + "\n\n"
                if sty2:
                    block += SECTION_STYLE_RECENT + "\n" + sty2 + "\n\n"
                block += STYLE_GUARD
            if dig:
                block += SECTION_DIGEST + "\n" + dig + "\n\n"
            block += mem

            # 人物画像（可选）：让 bot 认得群里的人
            people_path = bot.get("people")
            if not people_path and path:
                people_path = path.rsplit(".", 1)[0] + ".people.md"
            people_path = _resolve(people_path)
            p_chars = int(bot.get("people_chars") or self.m_cfg.get("people_chars") or DEFAULT_PEOPLE_CHARS)
            people = read_people(people_path, p_chars)
            if people:
                block += (
                    "\n\n## 你认识的人\n"
                    "这些是你记住的群友，聊天时可以自然地认得他们；"
                    "没在名单里的人，就当第一次见。\n\n"
                    + people
                )

            request.system_prompt = old + block
            logger.info("[mindscape_memory] %s 注入 %d 字记忆 / %d 字摘要 / %d 字账本"
                        " / %d 字风格 / %d 字人物",
                        label, len(mem), len(dig), len(notes), len(sty), len(people))
        except Exception as e:
            logger.warning("[mindscape_memory] 注入失败: %s", str(e)[:120])