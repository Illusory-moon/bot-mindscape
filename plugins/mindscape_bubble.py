"""mindscape_bubble —— 「自主冒泡」的提示词统一在框架里（2026-10-31）

**要解决的问题**：原来每个「冒泡」是一次 AstrBot 的 CronJob，**提示词各自写死在
DB 的 `cron_jobs.payload.note` 里** ⇒ 两个 bot 五个任务、五份文案、五种毛病
（其中一个 bot 的四个任务里枚举了「做题做烦了」，它就照着这句反复说 ✗）。

**现在的做法**：**一个模板放在 config**，插件启动时把它**同步进所有冒泡任务的 note** ⇒
改文案只改一处、两个 bot 一起变 ✓；任务的**时间 / 群 / 每日次数**仍然归 CronJob 管，
本模块**不创建、不改动**这些字段 ✗（只动 `note`，其余原样保留）。

配置（config.yaml）：

    bubble:
      enabled: true
      db: /opt/astrbot/data/data_v4.db       # AstrBot 主库（cron_jobs 表在这）
      targets: ["<bot_a>", "<bot_b>"]        # 只同步这些 self_id 名下的任务（真实号写进 config）
      note: |
        （冒泡时用的那段话）

⚠️ 写 note 的两条纪律（血泪 ✓）：
  ① **别给"示例答法"的词表**（「困了/做题做烦了/无聊」这类枚举会被照抄成八股 ✗）
  ② 给**方向**就行（"随口说一句当下的状态或念头"）＋ 一句「别每次都说同一类」
"""
import json
import os
import sqlite3

from astrbot.api import logger
from astrbot.api.event import filter

import mindscape_config as cfg

BB_SECTION = "bubble"

# 兜底模板（config 没写 note 时用它，免得同步成空字符串）
DEFAULT_NOTE = (
    "现在没有人给你发消息，也没有人在等你回话 —— 是你自己想冒个泡。"
    "可以不说，也可以只说半句，两个都对。"
    "想说什么都行：手上正干着的事、刚冒出来的一个念头、突然想起的东西，或者一句「今天有点想说话」。"
    "不用有由头，不用有意义。"
    "说的时候：短、随口，像平时在群里打字那样，别写小作文；不要接别人的话、不要回答谁、不要问问题等人接；"
    "不要解释你为什么出现，别用「冒泡」「定时」「任务」「系统」这类词；"
    "**同一类事别反复说**（尤其别每次都拿同一件小事当由头）—— 换个说法、换个念头。"
    "想发图就用 send_sticker，想说话就用 send_message_to_user。"
)


def bb_conf():
    try:
        return cfg.section(BB_SECTION) or {}
    except Exception:
        return {}


def bb_sync(db=None, note=None, targets=None):
    """把模板同步进冒泡任务的 note。返回 (扫到几个, 改了几个)。

    ⚠️ 只改 `payload.note`；`session` / `gate` / `self_id` 等**原样保留** ✗
    """
    c = bb_conf()
    if db is None:
        db = str(c.get("db") or "")
    if note is None:
        note = str(c.get("note") or "").strip() or DEFAULT_NOTE
    if targets is None:
        targets = {str(x) for x in (c.get("targets") or [])}
    else:
        targets = {str(x) for x in targets}
    if not db or not os.path.exists(db) or not targets:
        return 0, 0

    seen, changed = 0, 0
    try:
        con = sqlite3.connect(db, timeout=10)
    except Exception as e:
        logger.warning("[mindscape_bubble] 连不上库 %s: %s", db, str(e)[:80])
        return 0, 0
    try:
        try:
            rows = con.execute("select job_id, payload from cron_jobs").fetchall()
        except Exception as e:
            logger.warning("[mindscape_bubble] 读 cron_jobs 失败（表不存在？）: %s", str(e)[:80])
            return 0, 0
        for job_id, payload in rows:
            try:
                d = json.loads(payload or "{}")
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if str(d.get("self_id") or "") not in targets:
                continue
            seen += 1
            if str(d.get("note") or "") == note:
                continue
            d["note"] = note
            try:
                con.execute("update cron_jobs set payload=? where job_id=?",
                            (json.dumps(d, ensure_ascii=False), job_id))
                changed += 1
            except Exception as e:
                logger.warning("[mindscape_bubble] 写 %s 失败: %s", str(job_id)[:20], str(e)[:80])
        if changed:
            con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass
    return seen, changed


class BubbleMixin:
    """启动时把「冒泡提示词」同步成 config 里那一份（幂等）。"""

    def setup(self, context):
        c = bb_conf()
        self.bb_on = c.get("enabled") is not False
        logger.info("[mindscape_bubble] loaded | enabled=%s | targets=%d 个",
                    self.bb_on, len(c.get("targets") or []))

    @filter.on_astrbot_loaded()
    async def bb_sync_on_load(self, *args, **kwargs):
        try:
            c = bb_conf()
            if c.get("enabled") is False:
                logger.info("[mindscape_bubble] 已在配置里关掉，跳过同步")
                return
            seen, changed = bb_sync()
            logger.info("[mindscape_bubble] 冒泡提示词同步：扫到 %d 个任务，改了 %d 个", seen, changed)
        except Exception as e:
            logger.warning("[mindscape_bubble] 同步异常: %s", str(e)[:120])
