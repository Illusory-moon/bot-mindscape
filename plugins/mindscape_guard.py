# -*- coding: utf-8 -*-
"""mindscape_guard —— 沉浸层：拦截 LLM 报错，绝不让它发出去

问题：模型全部失败时，框架会把错误文本当成「回复」发给用户，例如：
        LLM 响应错误: All chat models failed: APITimeoutError: Request timed out.
      这一条发出去，role-play 当场出戏，AI 身份暴露。

方案：在回复发送前检查内容，命中错误特征就清空整条回复（什么都不发）。
      对用户来说，bot 只是「这次没说话」，而不是「吐了一句报错」。
"""
import asyncio
import logging
import os
import re

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

import mindscape_config as cfg
from mindscape_core import strip_invisible
from mindscape_gate import pg_audit, pg_config, pg_private, pg_redact, pg_secret_in

# 默认拦截特征（可在配置里覆盖）
DEFAULT_PATTERNS = [
    "LLM 响应错误",
    "All chat models failed",
    "APITimeoutError",
    "APIConnectionError",
    "APIError",
    "RateLimitError",
    "Request timed out",
    "Request timeout",
    "response error",
    "Internal Server Error",
    "Bad Gateway",
    "Service Unavailable",
    "Connection error",
    "Traceback (most recent call last)",
    "openai.",
    "httpx.",
    # 框架**自己**的报错口径（实测漏过一次，见下面的 send 级兜底）：
    #   internal.py 的 except 里直接发 "Error occurred while processing agent request: …"
    "Error occurred while processing agent",
    "Error occurred during AI execution",
    "Failed to download file from",
    "Error Type:",
    "Error Message:",
]

# 工具调用标记漏进正文：整段都不该发出去 ✓（2026-10-11 线上事故后加的护栏）
# 注意那个 chr(60)：源码里不写字面尖括号，免得构建/转义环节把它吃掉 ✓
_LEAK_LT = chr(60)

_LEAK_PAT = r"\s*(?:\|+\s*[A-Za-z]*\s*\|+\s*)?/?\s*(?:tool_calls?|function_calls?|invoke|parameter|calls|function)\b"

LEAK_RE = re.compile(_LEAK_LT + _LEAK_PAT, re.I)


def has_tool_leak(text):
    """正文里有没有「工具调用标记漏出来」✗（整段都不该发出去）。"""
    try:
        return bool(LEAK_RE.search(text or ""))
    except Exception:
        return False


# ── 私有标签 / 思考原文 的总兜底（2026-10-17 立 · 主人点名要的规矩 ✓）────────
# 主人原话：「任何新功能都要保证对**思考和 json 原文**的拦截」（提示词不改 ✓）
#
# **为什么要放在 guard**：各插件（impression / wishonomy / …）**各自**清自己的标签 ✓，
# 但**新插件一旦漏了，没有任何兜底** ✗ ——
# 2026-10-16 那两起真事故就是这么漏的：
#   · impression：她连标签带 JSON **一起截断** ⇒ 插件只吃「标签+完整 {}」⇒ 残片漏进群 ✗
#   · wishonomy：只剩**闭合**标签 + `payload is None` 时**直接早退** ⇒ 标签漏进群 ✗
# ⇒ 所以这里做一道**与插件无关**的兜底：**认得出私有标签形态就扒掉** ✓
#   （宁可多扒一个标签，也不能让 `<impression_update>{"impression":…` 进群 ✗）
#
# ⚠️ 两条纪律：
#   ① **只扒「标签 + 紧跟其后的 JSON」**，绝不「截断到标签为止」✗
#      —— 那会把她真正要说的话一起吃掉（`_11` 里记着那次教训 ✓）
#   ② 只认**明确的私有标签名**（白名单 ✓），不搞「看到尖括号就扒」✗（会误伤正常文本 ✓）

# 已登记的本仓私有标签名（新插件加标签 ⇒ **往这里加一个名字** ✓ 这就是那条约定的落地点）
_MS_TAG_NAMES = ("impression_update", "wish_mood", "wish_grant", "wish_check",
                 "mood", "impression", "mindscape_meta")

_BT = chr(96)          # 反引号

# ⚠️ **每个正则必须写成一个字面量、一行写完** ✗ —— 别换行、别用相邻字面量拼 ✓
#   原因（本仓真坑 ✓ 2026-10-17 被自检当场抓住）：`build_plugin.py` 把相邻字面量折成
#   **一个**常量 ⇒ 产物里那个字符串与源码里的**逐字不同** ⇒ 自检「**转义保真**」报 `转义丢失` ✗
#   ⇒ 判据很简单：**这段正则字符串必须逐字出现在产物里** ✓（拆成两段就必然找不到 ✓）

# ① 完整标签（含中间 JSON 且**闭合**）⇒ 整段扒掉 ✓
PRIV_TAG_FULL = re.compile(r"(?:\[\[|\[|<)(?:impression_update|wish_mood|wish_grant|wish_check|mood|impression|mindscape_meta)(?:\]\]|\]|>)[ \t]*`*[ \t]*\{[^{}]*\}[ \t]*`*[ \t]*(?:\[\[/|\[/|</)(?:impression_update|wish_mood|wish_grant|wish_check|mood|impression|mindscape_meta)(?:\]\]|\]|>)")
# ② 只剩闭合标签 ⇒ 扒掉 ✓（真事故：`…进谁兜了~[[/wish_mood]]`）
PRIV_TAG_CLOSE = re.compile(r"(?:\[\[|\[|<)/(?:impression_update|wish_mood|wish_grant|wish_check|mood|impression|mindscape_meta)(?:\]\]|\]|>)")
# ③ 开标签 + **没闭合的 JSON**（截断）⇒ 从 `{` 起到末尾全扒 ✓
#    （漏出形态必然在末尾 —— 因为那是被截断出来的 ✓）
PRIV_TAG_TRUNC = re.compile(r"(?:\[\[|\[|<)(?:impression_update|wish_mood|wish_grant|wish_check|mood|impression|mindscape_meta)(?:\]\]|\]|>)[ \t]*`*[ \t]*\{[^{}]*$")
# ④ 开标签 + **完整** JSON（没闭合标签）⇒ 一起扒 ✓
PRIV_TAG_OPENJSON = re.compile(r"(?:\[\[|\[|<)(?:impression_update|wish_mood|wish_grant|wish_check|mood|impression|mindscape_meta)(?:\]\]|\]|>)[ \t]*`*[ \t]*\{[^{}]*\}[ \t]*`*")


def put_plain_text(result, text):
    """把 result.chain 里的**可见文本**整体换成 `text` ✓ 返回改动数。

    做法（照本仓惯例 `comp.text = new` ✓）：把 `text` 写进**第一个可写的文本组件**，
    其余文本组件清空 ⇒ **图/语音等非文本组件的顺序不受影响** ✓
    """
    chain = getattr(result, "chain", None)
    if not chain:
        return 0
    n = 0
    done = False
    for comp in chain:
        cur = getattr(comp, "text", None)
        if not isinstance(cur, str):
            continue
        if not done:
            if cur != text:
                comp.text = text
                n += 1
            done = True
        elif cur:
            comp.text = ""
            n += 1
    return n

# ⑤ 思考原文泄漏：模型把「自述式思考」写进正文 ✗
#    真事故（2026-10-16 17:20）：`…一天换三回~，嗯，标签要放对位置，让我把 wish_mood 写完整`
#    ⚠️ 窄口径：只在**提到标签名 / format / 格式**时才判 ✗
#       —— 她平时本来就爱说「让我」「嗯」，一律砍会误伤 ✓
# ⚠️ 窄口径（**宁可不扒，也不误伤** ✗）：只在出现**明显的自述口吻标记**时才判 ✓
#   判据 = 边界符 + 自述语（嗯/哦/欸…，）+ 紧随其后的**短句** + 提到标签/格式/JSON
#   ⚠️ 两条都必须有：① 「嗯，」这种自述语开头 ② 提到标签名或 format 字样
#   ⇒ 她平时说「嗯」「让我」都不带这两条 ⇒ 不会误伤 ✓
#   ⚠️ 不可贪：本鱼第一版写成「…（任意 ≤24 字）…让我…format…」⇒ 把
#      「——花粉你这口径，一天换三回~」也吃掉了 ✗（实测抓到 ⇒ 收紧 ✓）
THINK_LEAK = re.compile(
    r"(?:(?:^|[，,。；;！!？?\n])[ \t]*(?:嗯|哦|欸|哎)[ \t]*，[ \t]*[^\n。！？]{0,30}?(?:标签|格式|format|need format|wish_mood|impression|JSON|json|写完整|写全)|(?:^|[，,。；;！!？?\n])[ \t]*(?:Wait|wait|Need|need|Hmm|hmm)\b[^\n。！？]{0,40}?(?:format|formatting|JSON|json))[^\n。！？]{0,40}$")
# ⚠️ 长度保护：扒完必须还剩点话（≥2 字）—— 否则说明它把整句都吃了 ✗ 那就别扒 ✓
#    （她正常说话里也可能出现「让我…格式」⇒ 宁可留一句，也不能把整句吞掉 ✗）


def strip_private_tags(text):
    """扒掉**私有标签（含紧跟的 JSON）**与**自述式思考残句** ✓ 返回 (新文本, 改了几处)。

    ⚠️ 只删「标签 + 它自己的 JSON」；**绝不截断她真正要说的话** ✗
    """
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    out = text
    for _ in range(6):                      # 多来几轮，防嵌套/残留 ✓
        before = out
        out = PRIV_TAG_FULL.sub("", out)
        out = PRIV_TAG_TRUNC.sub("", out)
        out = PRIV_TAG_OPENJSON.sub("", out)
        out = PRIV_TAG_CLOSE.sub("", out)
        # 思考残句：**只在扒完还剩 ≥2 字时才扒** ✓（否则它把整句吃了 ⇒ 不扒 ✗）
        _hit = THINK_LEAK.search(out)
        if _hit and len(out[:_hit.start()].strip()) >= 2:
            out = out[:_hit.start()]
        if out == before:
            break
    if out != text:
        n = 1
    return out.rstrip(), n


# ── 出站「标记清理」：气泡分隔符 + markdown（2026-10-17 主人点名 ✓）──────────
# 主人原话：「能不能用类似的方式把 **|||** 替换成空字符，她在被模型惯性带着想输出 markdown」
#
# **气泡分隔符 `|||`**：那是本仓"分几条发"的**内部标记** ✗ 绝不能进群 ✓
#   ⚠️ 修法从「换成换行」改成「**替换成空字符**」✓（主人 2026-10-17 指定 ✓）——
#      因为拆气泡那条路会照旧把话分成几条发 ✓ 留着换行反而是多余的空行 ✓
#   ⚠️ 事故形态（2026-10-17 某个群 ✓）：某次出站时 `chain` 里**还没有文本**
#      ⇒ mention 读到 0 字、既没拆也没兜底 ⇒ `|||` 原样进群 ✗（拆开的 2 条晚了 6 秒 ✗）
#
# **markdown**：模型惯性会带 ✗ 而她在群里说话**从不该用格式** ✓
#   实测（全库 6588 条她发出的消息）：`**粗体**` **5 次**、反引号 **0 次**、
#   且那 5 次**全是"想强调"**（`**摄影机后面**` / `**做了**` / `**今**晚`）✓
#   ⇒ **扒掉标记、留下文字**是**零误伤**的 ✓（`**今**晚` → `今晚` ✓ 正是她本来要说的 ✓）
_SEP3 = chr(124) * 3
# ⚠️ 每个正则**一行、一个字面量** ✗ 别换行拼接（产物与源码会逐字不同 ⇒ 转义保真报红 ✓）
MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
MD_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
MD_UNDER = re.compile(r"__([^_\n]+)__")
MD_CODE = re.compile(r"`([^`\n]+)`")
MD_FENCE = re.compile(r"```[a-zA-Z0-9_+\-]*\n?")
MD_HEAD = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.M)
MD_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)
MD_HYPHEN = re.compile(r"^[ \t]*[-*+][ \t]+", re.M)
MD_NUM = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.M)


def strip_md_marks(text):
    """扒掉 markdown **标记**、留下文字 ✓（返回 (新文本, 改了几处)）。

    ⚠️ 只动标记，不动内容 ✓ —— `**今**晚` → `今晚` ✓（她自己要说的就是"今晚"✓）
    """
    if not isinstance(text, str) or not text:
        return text, 0
    n = 0
    out = text
    for pat, rep in ((MD_BOLD, r"\1"), (MD_UNDER, r"\1"), (MD_CODE, r"\1"),
                     (MD_ITAL, r"\1"), (MD_FENCE, ""), (MD_HEAD, ""),
                     (MD_QUOTE, ""), (MD_HYPHEN, ""), (MD_NUM, "")):
        new = pat.sub(rep, out)
        if new != out:
            n += 1
            out = new
    return out, n


def normalize_outbound(text):
    """出站总整理：**私有标签/思考** → **`|||` 去掉** → **markdown 标记扒掉** ✓

    返回 (新文本, 改了几处)。做成一个函数是为了**两条出站管线口径一致** ✓
    """
    if not isinstance(text, str) or not text:
        return text, 0
    hits = 0
    out, _n1 = strip_private_tags(text)
    hits += _n1
    if _SEP3 in out:
        # ⚠️ 口径（2026-10-17 第二次修正 ✓）：`|||` 是**句间分隔** ⇒
        #    替换成**空字符**会让两句话**糊在一起** ✗
        #    真事故：`…别端那些重口味的来凑热闹|||吃饱了赶紧睡…` ⇒ 去空后读成
        #    「…来凑热闹吃饱了赶紧睡…」✗（主人报「两句之间有点精分」的一部分 ✓）
        #    ⇒ 改成**换成「，」** ✓：即使拆气泡那条路没走成，群里读到的仍是**通顺的两句** ✓
        out = out.replace(_SEP3, "，")
        hits += 1
    out, _n2 = strip_md_marks(out)
    hits += _n2
    return out, hits


# 兜底正则：形如 "xxxError: ..." / "xxxException: ..." / "Timeout: ..."
DEFAULT_REGEX = r"(?:[A-Za-z_]*Error|[A-Za-z_]*Exception|Timeout|Failed)\s*[:：]"

# 只看开头这么长，避免正文里恰好出现 "error" 被误杀
SCAN_LEN = 200


def _load_config():
    """从共享配置读取拦截规则（读不到就用默认值）。"""
    path = cfg.config_path()   # 统一走共享配置（数据目录由 mindscape_config 决定）
    if not os.path.exists(path):
        return DEFAULT_PATTERNS, DEFAULT_REGEX
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            conf = yaml.safe_load(f) or {}   # 注意别叫 cfg —— 会和模块级的配置命名空间撞名
        g = (conf.get("guard") or {})
        # 用户自定义模式是「追加」而不是「替换」：
        # 否则配置里只写几条，反而会比内置默认拦得更少（真实踩过）。
        extra = [str(p) for p in (g.get("patterns") or []) if str(p).strip()]
        merged = list(DEFAULT_PATTERNS)
        for p in extra:
            if p not in merged:
                merged.append(p)
        # 只有显式给出 regex 才覆盖内置正则（空字符串视为用默认）
        rx = str(g.get("regex") or "").strip() or DEFAULT_REGEX
        return merged, rx
    except Exception as e:
        logger.warning("[mindscape_guard] 配置读取失败，用默认值: %s", str(e)[:100])
        return DEFAULT_PATTERNS, DEFAULT_REGEX


# 日志脱敏：上游报错里可能夹带 key、带令牌的 URL、Bearer 头
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (re.compile(r"(?i)(api[_-]?key|token|authorization)[=\":\s]+[A-Za-z0-9._\-]{6,}"), r"\1=***"),
    (re.compile(r"https?://[^\s\"']+[?&](key|token|rkey|sign)=[^\s\"'&]+"), "<url-with-secret>"),
]


def redact(text, limit=100):
    """给日志用的摘要：截断 + 抹掉疑似密钥。"""
    t = (text or "").replace(chr(10), " ").replace(chr(13), " ")
    for rx, rep in _SECRET_PATTERNS:
        t = rx.sub(rep, t)
    return pg_redact(t[:limit])


class _PrivacyLogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        clean = pg_redact(message)
        if clean != message:
            record.msg = clean
            record.args = ()
        return True


def pg_install_log_filter():
    loggers = [logging.getLogger(), *(
        value for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger))]
    for current in loggers:
        for handler in current.handlers:
            if not any(isinstance(f, _PrivacyLogFilter) for f in handler.filters):
                handler.addFilter(_PrivacyLogFilter())


# ── send 级兜底：框架有两条「直接 send」的报错出口，根本不过结果管线 ──
# 实测事故（2026-09-30，线上群）：QQ 表情 CDN 404 → agent 阶段抛异常 →
#   internal.py 的 except 里 `await event.send("Error occurred while processing agent
#   request: Failed to download file from … HTTP status code: 404")`
# 这条**不经过 ResultDecorateStage**，所以挂在 on_decorating_result 上的拦截
# 一点机会都没有 —— 群里直接收到一句英文报错。
# 于是给「自己实现了 send 的平台事件类」包一层：命中报错特征就不发，只记日志。
# 只在子类上包（基类 send 本身不发送、且 subclass 会在发完之后再调它）。
MS_SEND_WRAPPED = "_mindscape_send_guard"


def ms_chain_text(message):
    """从 MessageChain 里取纯文本（拿不到就返回空串，不抛）。"""
    if isinstance(message, str):
        return message
    try:
        got = message.get_plain_text()
        if isinstance(got, str):
            return got
    except Exception:
        pass
    parts = []
    for c in (getattr(message, "chain", None) or []):
        t = getattr(c, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "".join(parts)


def ms_install_send_guard(check):
    """给平台事件类的 send 包一层，返回这次包了几个类（幂等）。"""
    try:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent as _Base
    except Exception:
        return 0
    seen, targets = set(), []

    def walk(cls):
        if cls in seen:
            return
        seen.add(cls)
        if cls is not _Base and "send" in cls.__dict__:
            targets.append(cls)
        for sub in cls.__subclasses__():
            walk(sub)

    walk(_Base)
    n = 0
    for cls in targets:
        if getattr(cls, MS_SEND_WRAPPED, False):
            continue
        orig = cls.__dict__["send"]

        async def _ms_send(self, message, _orig=orig, **kw):
            try:
                txt = ms_chain_text(message)
            except Exception:
                txt = ""
            if txt.strip():
                if pg_config().get("enabled") and pg_secret_in(self.get_self_id(), txt):
                    if not pg_private(self):
                        pg_audit("blocked_group", self.get_self_id(), self.get_group_id() or "")
                        return None
                    private_code = True
                else:
                    private_code = False
                try:
                    hit = check(txt)
                except Exception:
                    hit = False
                if hit:
                    logger.warning(
                        "[mindscape_guard] 拦下直接发送的报错（这条不走结果管线）: %s",
                        redact(txt))
                    return None
            sent = await _orig(self, message, **kw)
            if txt.strip() and private_code:
                pg_audit("given", self.get_self_id(), self.get_sender_id() or "")
            return sent

        _ms_send.__name__ = getattr(orig, "__name__", "send")
        setattr(cls, MS_SEND_WRAPPED, True)
        setattr(cls, "send", _ms_send)
        n += 1
    return n

def is_error_text(text, patterns=None, regex=None):
    """判断这段文字是不是框架报错（而不是 bot 的正常回复）。"""
    t = (text or "").strip()
    if not t:
        return False
    head = t[:SCAN_LEN]
    for p in (patterns or DEFAULT_PATTERNS):
        if p and p in head:
            return True
    try:
        if re.search(regex or DEFAULT_REGEX, head):
            return True
    except re.error:
        pass
    return False


MS_SENDTOOL_FLAG = "_ms_sendtool_patched"
MS_LINE_GAP = 0.8          # 连发之间的停顿（秒）—— 真人也是一句一句敲的

# 出站文本整形钩子：由 format 模块注册（`format.targets` 的压平 / 去句号）。
# ⚠️ 存的是「取实例的函数」而不是实例本身 —— 插件可能在补丁之后才 setup，
#    存实例会拿到 None，存函数则每次现取 ⇒ 不会陈旧 ✓
MS_TEXT_FMT = None

# 「句尾去句号」的作用域（= `format.no_period` 原样）—— ⭐ **单一真值** ✓
# 由 format 模块 setup 时登记；那些**模块级钩子**（没有 self ✗，如 mention 的拆分）
# 从这里读 ✓ —— 免得三处各读各的配置、口径漂移 ✗
MS_FORMAT_NP = None


def ms_set_text_formatter(provider):
    """登记「出站文本整形」回调：provider() -> (transform 或 None)。"""
    global MS_TEXT_FMT
    MS_TEXT_FMT = provider


def store_fmt(msgs, context, kwargs):
    """把这一批 plain 逐个过一遍：**先扒私有标签/思考残句** ✓，再走 format 的整形 ✓。

    ⚠️ 顺序：**先扒标签，再压平/去句号** —— 反过来的话，压平会把标签和正文的边界糊掉 ✗
    （2026-10-17 主人定的规矩：任何新功能都要保证对**思考和 JSON 原文**的拦截 ✓）
    带图/语音的走原样 ✓；任一步失败一律**原样放行** ✓（宁可多发一个标签，也不能吞话 ✗）
    """
    out = []
    for m in msgs:
        if not (isinstance(m, dict) and str(m.get("type")) == "plain"
                and isinstance(m.get("text"), str)):
            out.append(m)
            continue
        _orig = m["text"]
        # ① 出站总整理：私有标签/思考 → `|||` 去掉 → markdown 标记扒掉 ✓
        _stripped, _n = normalize_outbound(_orig)
        if _n:
            logger.warning("[mindscape_guard] 出站整理（工具直发路）: %s", _orig[:90])
            m = dict(m)
            m["text"] = _stripped
        # ② format 的整形（压平 / 去句号）
        if MS_TEXT_FMT is not None:
            try:
                tf = MS_TEXT_FMT()
            except Exception:
                tf = None
            if tf is not None:
                try:
                    new = tf(context, m["text"], kwargs)
                    if new and new != m["text"]:
                        m = dict(m)
                        m["text"] = new
                except Exception as exc:
                    logger.warning("[mindscape_guard] 出站整形失败（原样放行）: %s", str(exc)[:80])
        out.append(m)
    return out

def ms_patch_send_tool():
    """把内置工具 `send_message_to_user` 改成「每个 plain 各发一条」。

    实测（2026-10-06）：她和主人想连发短句时都爱用这个内置工具，而它把 components
    拼成一个 MessageChain **一次**发出去 —— 群里只看到一条「火火兔 花花菇 嘻，测完就去睡呀」，
    三段并成一句。规矩层劝不动、工具说明也劝不动，那就直接改它
    （能用代码硬保证的，别指望提示词）。逐条发、之间停 MS_LINE_GAP 秒；
    带非纯文本（图/语音/文件）或指定别的 session 的场景，原样交给原实现。
    """
    try:
        from astrbot.core.tools.message_tools import SendMessageToUserTool
    except Exception as e:
        logger.warning("[mindscape_guard] 拿不到内置发送工具，跳过补丁: %s", type(e).__name__)
        return 0
    if getattr(SendMessageToUserTool, MS_SENDTOOL_FLAG, False):
        return 0
    orig = SendMessageToUserTool.call

    async def _ms_send_to_user(self, context, *args, **kwargs):
        msgs = kwargs.get("messages")
        if msgs is None and args:
            msgs = args[0]
        if pg_config().get("enabled") and isinstance(msgs, (list, tuple)):
            for part in msgs:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    if pg_redact(part["text"]) != part["text"]:
                        return "口令只能在当前私聊里直接回复，不能用跨会话发送工具。"
        if isinstance(msgs, (list, tuple)) and len(msgs) > 1:
            plains = [m for m in msgs if isinstance(m, dict)
                      and str(m.get("type")) == "plain"
                      and str(m.get("text") or "").strip()]
            others = [m for m in msgs
                      if not (isinstance(m, dict) and str(m.get("type")) == "plain")]
            if len(plains) > 1 and not others:
                rest = {k: v for k, v in kwargs.items() if k != "messages"}
                tail = tuple(args[1:]) if args else ()
                n = 0
                for m in plains:
                    try:
                        if tail:
                            await orig(self, context, store_fmt([m], context, kwargs), *tail, **rest)
                        else:
                            await orig(self, context, messages=store_fmt([m], context, kwargs), **rest)
                        n += 1
                    except Exception as exc:
                        logger.warning("[mindscape_guard] 逐条发送第 %d 条失败: %s",
                                       n + 1, str(exc)[:80])
                        break
                    await asyncio.sleep(MS_LINE_GAP)
                logger.info("[mindscape_guard] 内置工具逐条发送 %d 条（原本 %d 段会并成一条）",
                            n, len(plains))
                return "Already sent %d separate messages." % n
        if (isinstance(msgs, (list, tuple)) and len(msgs) == 1
                and isinstance(msgs[0], dict) and str(msgs[0].get("type")) == "plain"):
            msgs = store_fmt(msgs, context, kwargs)
            if args:
                return await orig(self, context, msgs, *args[1:], **kwargs)
            return await orig(self, context, messages=msgs, **kwargs)
        return await orig(self, context, *args, **kwargs)

    SendMessageToUserTool.call = _ms_send_to_user
    setattr(SendMessageToUserTool, MS_SENDTOOL_FLAG, True)
    logger.info("[mindscape_guard] 内置 send_message_to_user 已改成逐条发送")
    return 1


class GuardMixin:
    def setup(self, context):

        self.patterns, self.regex = _load_config()
        pg_install_log_filter()
        self.blocked = 0
        g = cfg.section("guard")
        self.g_send = True if g.get("send_guard") is None else bool(g.get("send_guard"))
        armed = self._ms_arm_send_guard() if self.g_send else 0
        try:
            self.ms_lined = ms_patch_send_tool()
        except Exception as e:
            self.ms_lined = 0
            logger.warning("[mindscape_guard] 逐条发送补丁失败: %s", str(e)[:120])
        logger.info("[mindscape_guard] loaded | %d 条拦截规则 | send 级兜底=%s（本次包了 %d 个类）",
                    len(self.patterns), self.g_send, armed)

    def _ms_hit(self, text):
        """这条文本该不该拦：框架报错 **或** 漏出来的工具调用标记（两层共用同一套判据 ✓）。"""
        return is_error_text(text, self.patterns, self.regex) or has_tool_leak(text)

    def _ms_arm_send_guard(self):
        """装 send 级兜底（幂等）。平台事件类这时可能还没 import，等下面那个钩子再补一次。"""
        try:
            return ms_install_send_guard(self._ms_hit)
        except Exception as e:
            logger.warning("[mindscape_guard] send 级兜底安装失败: %s", str(e)[:120])
            return 0

    @filter.on_astrbot_loaded()
    async def ms_arm_late(self, *args, **kwargs):
        """框架加载完毕：这时候平台事件类都在了，把漏掉的补上。"""
        if not getattr(self, "g_send", False):
            return
        n = self._ms_arm_send_guard()
        if n:
            logger.info("[mindscape_guard] send 级兜底补装 %d 个类", n)

    @filter.on_llm_request(priority=20)
    async def ms_scrub_invisible(self, event: AstrMessageEvent, request):
        """把**本轮正文**里的零宽 / 双向控制符剥掉 ✓（能让显示的样子与实际内容不一致、藏指令 ✗）。

        ⚠️ 只碰「本轮」（`request.prompt` = 当前这条消息）✗ —— **历史一个字都不动** ✓：
        那是缓存的地基，改了它整段前缀就变、命中全废 ✗（主人 2026-10-10 特别叮嘱 ✓）。
        """
        try:
            raw = getattr(request, "prompt", None)
            if isinstance(raw, str) and raw:
                clean = strip_invisible(raw)
                if clean != raw:
                    request.prompt = clean
                    logger.info("[mindscape_guard] 本轮正文剥掉不可见字符: %d -> %d 字 | bot=%s",
                                len(raw), len(clean), event.get_self_id())
        except Exception as e:
            logger.warning("[mindscape_guard] 清洗不可见字符失败: %s", str(e)[:120])

    @filter.on_decorating_result(priority=999)
    async def block_error(self, event: AstrMessageEvent):
        try:
            result = event.get_result()
            if result is None:
                return
            txt = result.get_plain_text() or ""
            # （2026-10-27 已删：一次性诊断 [msdiag] —— 会把出站正文写进日志 ✗）
            if not txt.strip():
                return
            # ⭐ 出站总整理：私有标签/思考 → `|||` 去掉 → markdown 标记扒掉 ✓
            #   2026-10-17 主人定的规矩 + 两条真事故驱动的（详见 normalize_outbound 的注释 ✓）
            #   为什么放这儿：本钩子 priority=**999**（仅次于 silence 的 1000）⇒
            #   **所有插件的最终出站都要从它下面过** ⇒ 是唯一能兜住"新插件忘了清"的位置 ✓
            #   ⚠️ 策略：**扒掉标记、留下她真正要说的话** ✓ 不是整条丢掉 ✗
            _cleaned, _hits = normalize_outbound(txt)
            if _hits:
                logger.warning("[mindscape_guard] 出站整理: %s", redact(txt))
                put_plain_text(result, _cleaned)
                txt = _cleaned
                if not txt.strip():
                    # 整理完啥也不剩（她本来就只想发个标记）⇒ 那就不该发 ✓
                    self.blocked += 1
                    logger.warning("[mindscape_guard] 整理完只剩标记 ⇒ 整条不发")
                    event.clear_result()
                    event.stop_event()
                    return
            leak = has_tool_leak(txt)
            # ⭐ 登记「本轮已经把正文发出去了」—— 给救援模块用 ✓
            #   为什么在这儿：本钩子 **priority=999** ⇒ 真有字要发时一定会走到这一步 ✓
            #   （`receive` 那边靠它判断「她其实说过了」，免得**再补一句**造成"精分" ✗
            #     —— 2026-10-17 真事故：她调了个不存在的工具 ⇒ 那步返回空 ⇒ 救援另生成一句 ✗）
            if txt.strip():
                try:
                    _rs = globals().get("rc_mark_text_sent")
                    if _rs is None:
                        import sys as _gsys
                        for _gn, _gm in list(_gsys.modules.items()):
                            if _gn.endswith("mindscape_rescue"):
                                _rs = getattr(_gm, "rc_mark_text_sent", None)
                                if _rs is not None:
                                    break
                    if _rs is not None:
                        _rs(event.get_self_id(), event.get_group_id())
                except Exception:
                    pass
            if leak or is_error_text(txt, self.patterns, self.regex):
                self.blocked += 1
                # S03: 只记录脱敏摘要，避免上游报错里夹带的密钥进日志
                logger.warning(
                    "[mindscape_guard] 拦下%s（第 %d 条）: %s",
                    "工具标记泄漏" if leak else "报错",
                    self.blocked,
                    redact(txt),
                )
                event.clear_result()
                event.stop_event()
        except Exception as e:
            logger.warning("[mindscape_guard] 检查失败: %s", str(e)[:120])


@filter.on_decorating_result(priority=101)
async def pg_block_group_result(*args, **kwargs):
    event = next((a for a in args if hasattr(a, "get_self_id")), None)
    if event is None or not pg_config().get("enabled"):
        return
    result = event.get_result()
    text = result.get_plain_text() if result else ""
    if text and pg_secret_in(event.get_self_id(), text) and not pg_private(event):
        pg_audit("blocked_group", event.get_self_id(), event.get_group_id() or "")
        event.clear_result()