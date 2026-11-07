"""mindscape_selfwake —— 「真自主开口」：她**自己约**下一次什么时候醒（2026-11-06）

**要解决的问题**：现在的自主冒泡是**框架排班**（CronJob + 抽样 + gate）⇒ 节奏是我们定的 ✗
这里把「什么时候醒」的所有权交还给她：她调一次工具，写下**延迟 + 一句理由**，
到点由 AstrBot 的**一次性** CronJob 叫醒她；**说不说仍然由她决定**（醒来可以什么都不发）。

配置（config.yaml）：

    selfwake:
      enabled: true
      max_hours: 24          # 单条预约的上限（防她写"半年后"）
      max_items: 5           # 每个会话最多挂几条（保护她，不是限制她）
      targets:               # 白名单：只有列出来的 bot / 会话能用 ✗（默认全关）
        - self_id: "<bot_a>"
          allow: ["FriendMessage:<uid>"]     # 只开这个私聊；群里加 GroupMessage:<群号>

设计要点（细节见 canon/_42）：
  ① **只有"她写下一条新预约"才改变状态**：没写/null/[] 一律保持 ✗（不许当取消）
  ② 多条到点 ⇒ **合并成一次清醒**（按分钟归组），绝不连响
  ③ 唤醒 note 只递**事实**：她当时的理由 + 距约定多久 + "说不说由你" ⇒ 不排序、不建议、不催
  ④ 一切"该不该说"的判断留给模型；本模块只做存储、计时、白名单、上限
  ⑤ **不碰 prompt 主链**：教她用哪个工具由**工具说明**承担（静态、缓存安全）；
     唤醒时的说明走 CronJob 的 payload.note（本来就是那一轮的输入）✓
"""
import datetime
import json
import os
import re

from astrbot.api import llm_tool, logger
from astrbot.api.event import filter

import mindscape_config as cfg

SW_SECTION = "selfwake"
_SW_FILE = "selfwake.json"

# 中文数字 → 阿拉伯（只认常用于时长的那些）
_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

_DELAY_RE = re.compile(r"(\d+(?:\.\d+)?|[零一两二三四五六七八九十半]+)\s*(分钟|分|小时|钟头|天|日)")

_REASON_MAX = 120


def sw_conf():
    try:
        return cfg.section(SW_SECTION) or {}
    except Exception:
        return {}


def sw_limits():
    """(max_hours, max_items)：**用户可配，默认 24 / 5**（不硬编码在逻辑里）。"""
    c = sw_conf()
    try:
        mh = float(c.get("max_hours") or 24)
    except Exception:
        mh = 24.0
    try:
        mi = int(c.get("max_items") or 5)
    except Exception:
        mi = 5
    return (max(1.0, min(mh, 24.0 * 30)), max(1, min(mi, 50)))


def sw_parse_delay(text):
    """把「40 分钟 / 1.5 小时 / 两天」解析成秒。解析不出来返回 None（**不猜** ✗）。"""
    if not text or not isinstance(text, str):
        return None
    m = _DELAY_RE.search(text.strip())
    if not m:
        return None
    raw, unit = m.group(1), m.group(2)
    if raw in _CN_NUM:
        num = float(_CN_NUM[raw])
    elif all(ch in _CN_NUM for ch in raw):
        # 十几 / 二十 这类，只做简单累加（够用即可）
        num = 0.0
        for ch in raw:
            num = num * 10 + _CN_NUM[ch] if num else float(_CN_NUM[ch])
        if num <= 0:
            num = float(_CN_NUM[raw[-1]])
    else:
        try:
            num = float(raw)
        except Exception:
            return None
    mult = {"分钟": 60, "分": 60, "小时": 3600, "钟头": 3600, "天": 86400, "日": 86400}[unit]
    return int(num * mult)


def sw_state_path():
    try:
        base = cfg.data_dir()
    except Exception:
        base = os.path.dirname(os.path.abspath(cfg.config_path()))
    return os.path.join(base, _SW_FILE)


def sw_load():
    p = sw_state_path()
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def sw_save(state):
    p = sw_state_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception as e:
        logger.warning("[mindscape_selfwake] 写状态失败: %s", str(e)[:100])
        return False


def sw_key(self_id, session):
    return "%s|%s" % (self_id, session)


def sw_allowed(self_id, session):
    """白名单：只有 targets 里显式列出的 (bot, 会话类型:目标) 才放行 ✓"""
    c = sw_conf()
    if c.get("enabled") is False:
        return False
    sid = str(self_id or "")
    sess = str(session or "")
    for t in (c.get("targets") or []):
        if not isinstance(t, dict):
            continue
        if str(t.get("self_id") or "") != sid:
            continue
        allow = t.get("allow") or []
        if not allow:
            return False
        for pat in allow:
            if pat and sess.endswith(str(pat)):
                return True
    return False


def sw_note(entry, now=None):
    """唤醒时递给她的说明：**只给事实 + 一句"说不说由你"**，不排序不建议不催 ✗"""
    now = now or datetime.datetime.now()
    reason = str(entry.get("reason") or "").strip() or "（当时没写理由）"
    delay = str(entry.get("delay") or "").strip()
    made = str(entry.get("delay_text") or delay)
    try:
        wake_at = datetime.datetime.fromisoformat(entry.get("wake_at"))
        left = int((now - wake_at).total_seconds())
    except Exception:
        left = 0
    late = "" if left < 120 else "（比约定的时间晚了约 %d 分钟）" % (left // 60)
    return (
        "**这是你自己定的时间到了 —— 不是别人叫你。**\n"
        "你当时写的是：「%s」，约的是「%s」%s\n"
        "现在你自己看：这件事还成立吗？想说什么就说；**不想说就什么都不发**，"
        "这也是你当时给自己留的选择。\n"
        "如果那件事已经被聊过去了、或者场面不对，就别硬提 ✗；"
        "想接话就接在当下的气口上，别把它当成一次「到点报到」。"
        % (reason, made or "（没写）", late)
    )


class SelfwakeMixin:
    """她自己的闹钟（工具 + 一次性 CronJob）。"""

    def setup(self, context):
        c = sw_conf()
        self.sw_on = c.get("enabled") is not False
        mh, mi = sw_limits()
        logger.info("[mindscape_selfwake] loaded | enabled=%s | max_hours=%s | max_items=%s | targets=%d 个",
                    self.sw_on, mh, mi, len(c.get("targets") or []))
        # 重启后把「还没到点」的预约重新排上（幂等：job 名带分钟，重复创建会覆盖同一条）
        try:
            import asyncio
            if self.sw_on:
                asyncio.create_task(self._sw_resync())
        except Exception as e:
            logger.warning("[mindscape_selfwake] 重排失败: %s", str(e)[:100])

    # ── 对外工具：她自己决定要不要约、约多久、为什么 ──
    @llm_tool(name="self_wake")
    async def self_wake(self, *args, **kwargs):
        """给自己定一个「下次自己醒过来」的时间 —— 到点你会自己开口，不用等人叫你。

        什么时候值得约（判据，不是清单）：
          · 你**有问题还没问**、有**答应过的事没兑现**、或者**押着一个正在发生的对话**想看看接下来怎么走；
          · 这些都要指向**具体的人或具体的事**（"他说九点回来"、"那场比赛三点开打"、"我答应给他看图"）✓
          · 只是"我还想说点什么" ⇒ 这种没有落脚点，别约 ✗
        别怕约得太近也别怕太远：1 分钟和 8 小时都可以；不想醒就别调这个工具。

        Args:
            delay(string): 多久之后，例如 "40 分钟"、"1.5 小时"、"8 小时"、"1 天"
            reason(string): 一小句理由，写给**未来的自己**看（醒来时你会读到它），要具体
        """
        ev = None
        for a in args:
            if hasattr(a, "get_self_id"):
                ev = a
                break
        if ev is None:
            return "现在记不了"
        try:
            sid = str(ev.get_self_id())
            sess = ""
            for attr in ("unified_msg_origin", "session_id", "get_session_id"):
                try:
                    v = getattr(ev, attr, None)
                    v = v() if callable(v) else v
                    if v:
                        sess = str(v)
                        break
                except Exception:
                    continue
            if not sess:
                return "现在记不了（拿不到会话）"
        except Exception:
            return "现在记不了"

        if not sw_allowed(sid, sess):
            return "这个会话还用不了这个功能"

        secs = sw_parse_delay(str(kwargs.get("delay") or ""))
        if not secs:
            return "看不懂这个时间 —— 用「N 分钟 / N 小时 / N 天」这种说法，比如「40 分钟」「8 小时」"
        mh, mi = sw_limits()
        secs = max(60, min(secs, int(mh * 3600)))
        reason = str(kwargs.get("reason") or "").strip()[:_REASON_MAX]

        now = datetime.datetime.now()
        wake_at = now + datetime.timedelta(seconds=secs)
        key = sw_key(sid, sess)
        state = sw_load()
        items = [x for x in (state.get(key) or []) if isinstance(x, dict)]
        # 同一 reason 视为「改写同一条」✓（她这一轮想到新的事，不该被当成重复 ✗）
        items = [x for x in items if str(x.get("reason") or "") != reason]
        if len(items) >= mi:
            return ("你手上已经有 %d 件事约着了 —— 先等等它们，或者到时候看情况再说 ✓"
                    "（要现在换成这条，就跟我说一声）" % len(items))
        items.append({
            "session": sess,
            "self_id": sid,
            "delay_text": str(kwargs.get("delay") or ""),
            "reason": reason,
            "created": now.isoformat(timespec="seconds"),
            "wake_at": wake_at.isoformat(timespec="seconds"),
            "job_id": "",
        })
        state[key] = items
        sw_save(state)
        logger.info("[mindscape_selfwake] 约定 | bot=%s 会话=%s | %s → %s | reason=%s",
                    sid, sess, kwargs.get("delay"), wake_at.strftime("%m-%d %H:%M"), reason[:60])

        ok = await self._sw_schedule(sid, sess)
        when = wake_at.strftime("%m-%d %H:%M")
        if ok:
            return "记下了：%s 之后（也就是 %s），你自己会醒一次。理由我记成「%s」了 ✓" % (
                kwargs.get("delay"), when, reason or "（没写）")
        return "记下了（%s），但定时没排上 —— 到时候可能得靠你自己想起来" % when

    # ── 排期：多条到点合并成一次清醒 ──
    async def _sw_schedule(self, sid, sess):
        try:
            mgr = getattr(self.context, "cron_manager", None)
            if mgr is None:
                logger.warning("[mindscape_selfwake] 拿不到 cron_manager，跳过排期")
                return False
            key = sw_key(sid, sess)
            state = sw_load()
            items = [x for x in (state.get(key) or []) if isinstance(x, dict)]
            if not items:
                return True
            # 按「分钟」归组 ⇒ 同一分钟到点的条目合并成一次唤醒 ✓
            groups = {}
            for it in items:
                try:
                    dt = datetime.datetime.fromisoformat(it.get("wake_at"))
                except Exception:
                    continue
                groups.setdefault(dt.strftime("%Y-%m-%d %H:%M"), []).append(it)
            now = datetime.datetime.now()
            for minute, group in groups.items():
                try:
                    due = datetime.datetime.strptime(minute, "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                if due <= now:
                    due = now + datetime.timedelta(seconds=5)   # 已经过点 ⇒ 立刻补一次（合并）
                note = "\n\n".join(sw_note(it, now) for it in group)
                name = "selfwake:%s:%s" % (sid, minute)
                payload = {
                    "session": sess,
                    "self_id": sid,
                    "note": note,
                    "selfwake": {"minute": minute, "reasons": [it.get("reason") for it in group]},
                }
                try:
                    await mgr.add_active_job(
                        name=name,
                        cron_expression=None,
                        payload=payload,
                        description="她自己约的醒（selfwake）",
                        run_once=True,
                        run_at=due,
                        persistent=False,
                    )
                    logger.info("[mindscape_selfwake] 排期 | %s | %s | 条目=%d",
                                sid, due.strftime("%m-%d %H:%M"), len(group))
                except Exception as e:
                    logger.warning("[mindscape_selfwake] 排期失败 %s: %s", minute, str(e)[:100])
                    return False
            # ⚠️ 排成功之后就把这些条目从状态里摘掉 ✓
            #    它们已经交给一次性定时任务了；留着的话，重启时 `_sw_resync` 会把
            #    "已过点"的旧预约当成迟到再叫一次 ✗（2026-11-07 灰测当场发现的坑）
            keep = [x for x in items if str(x.get("wake_at") or "") not in
                    {str(it.get("wake_at") or "") for g in groups.values() for it in g}]
            state[key] = keep
            sw_save(state)
            return True
        except Exception as e:
            logger.warning("[mindscape_selfwake] 排期异常: %s", str(e)[:120])
            return False

    async def _sw_resync(self):
        """重启后：把还没到点的预约重新排上（已到点的合并补一次）。"""
        try:
            await self._sw_schedule_all()
        except Exception as e:
            logger.warning("[mindscape_selfwake] 重启重排失败: %s", str(e)[:120])

    async def _sw_schedule_all(self):
        state = sw_load()
        seen = set()
        for key in list(state.keys()):
            try:
                sid, sess = key.split("|", 1)
            except Exception:
                continue
            if (sid, sess) in seen:
                continue
            seen.add((sid, sess))
            if not sw_allowed(sid, sess):
                continue          # 白名单关了就不排（老预约留着不动）✓
            await self._sw_schedule(sid, sess)
