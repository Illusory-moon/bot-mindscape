# -*- coding: utf-8 -*-
"""mindscape_janitor —— 运维层：会话历史防膨胀

问题：不少 bot 框架会把聊天里的图片以 base64 形式存进会话历史。
      攒到几十 MB 后，每轮请求都要把这坨东西发给模型 ——
      上传超时 → bot 看起来「卡死」，但日志里一句报错都没有。

方案：**先脱图，再删行** ——
  1. 含内联图片（`data:image/…;base64,…`）的记录：把那坨数据**就地换成「[图片]」**，
     会话上下文原样保留（只丢那张历史图的像素，模型本来也用不上）。
  2. 改不动的行（不是 JSON）或仍然超大的整行（> max_mb）：删掉兜底。
记忆的职责交给 diary/memory（独立文件），不会因此失忆。

为什么不是「一律删会话」：删一行 = 那个会话的**全部上下文**一起没了。
实测事故（2026-10-02）：群里一张图落进历史，**单条 48 万字**，
那个会话每轮请求都带着它（≈12 万 token，直接顶穿上下文窗口）——
而正确做法只是把那 48 万字换成 4 个字。

用法（独立脚本，不需要装在 bot 框架里）：
    python mindscape_janitor.py
配合 cron / systemd timer 每 5~15 分钟跑一次即可。
"""
import datetime
import json
import os
import re
import sqlite3
import sys

import mindscape_config as cfg

DEFAULT_MAX_MB = 2.0
DEFAULT_LOG = "./data/janitor.log"


def jn_abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def log(msg, path=None):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if path:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


IDENT_RE = None


def _safe_ident(name, fallback):
    """只允许字母数字下划线，避免把奇怪的表名/字段名拼进 SQL。"""
    import re as _re
    n = str(name or "")
    return n if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) else fallback


# 内联图片的 data-URL（后面那一串 base64 就是要脱掉的东西）
DATA_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
PLACEHOLDER = "[图片]"          # 内联图被换成它（image_url 段则整段换成文字段）


def _strip_parts(msgs):
    """把消息里内联的 base64 图片换成占位符，**返回替换处数**。

    ⚠️ 关键：`image_url` 段不能只把 url 换掉 —— 那样 provider 会直接 400
    （`Unsupported image_url format`），整个会话从此每轮都失败、她一句话都说不出来。
    实测踩过（2026-10-02：线上群会话被旧版正则改坏，她静默了两轮）。
    所以**整段换成文字段**，同时顺手修掉历史上已经被改坏的段。
    """
    n = 0
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            new = DATA_URL_RE.sub(PLACEHOLDER, c)
            if new != c:
                m["content"] = new
                n += 1
            continue
        if not isinstance(c, list):
            continue
        out = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "image_url":
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else None
                bad = (isinstance(url, str)
                       and not (url.startswith("http") or url.startswith("data:image")))
                if isinstance(url, str) and (url.startswith("data:image") or bad):
                    out.append({"type": "text", "text": PLACEHOLDER})
                    n += 1
                    continue
            if isinstance(part, dict):
                for k, v in list(part.items()):
                    if isinstance(v, str) and "data:image" in v:
                        part[k] = DATA_URL_RE.sub(PLACEHOLDER, v)
                        n += 1
            out.append(part)
        m["content"] = out
    return n


def strip_media(con, table, column):
    """把记录里内联的 base64 图片换成占位符（保住会话上下文）。

    返回 (改了几行, 替换几处, 砍掉几字)。
    改完必须仍是合法 JSON —— 坏 JSON 比不改更糟，所以解析失败就跳过、留给删行兜底。

    顺手**自愈**历史上被改坏的段（那种行里已经没有 `data:image` 了，只挑了占位符），
    所以筛选条件除了内联图，还包括「占位符当 url」的行。
    """
    mark = "%" + PLACEHOLDER + "%"
    rows = list(con.execute(
        "SELECT rowid, %s FROM %s WHERE %s LIKE '%%data:image%%' OR %s LIKE ?" % (column, table, column, column),
        (mark,),
    ))
    n_row = n_hit = n_chars = 0
    for rid, content in rows:
        if not isinstance(content, str):
            continue
        # 注意：不能因为「没有 data:image」就跳过 —— 被改坏过的行正是这种（只剩占位符）
        if "data:image" not in content and '"' + PLACEHOLDER + '"' not in content:
            continue
        try:
            msgs = json.loads(content)
        except Exception:
            continue          # 解析不了就别碰，留给下面「删行」兜底
        hits = DATA_URL_RE.findall(content)      # 先量一下砍掉多少（按原始文本算）
        n = _strip_parts(msgs)
        if not n:
            continue
        new = json.dumps(msgs, ensure_ascii=False)
        con.execute("UPDATE %s SET %s = ? WHERE rowid = ?" % (table, column), (new, rid))
        n_row += 1
        n_hit += n
        n_chars += sum(len(h) for h in hits)
    return (n_row, n_hit, n_chars)


def clean(db, table, column, max_mb, log_path=None):
    """脱图 + 清理超大行。返回 (脱图行, 替换处, 删图片行, 删超大行, 前MB, 后MB)。"""
    if not db or not os.path.exists(db):
        log("数据库不存在，跳过: %s" % db, log_path)
        return (0, 0, 0, 0, 0.0, 0.0)
    table = _safe_ident(table, "conversations")
    column = _safe_ident(column, "content")
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    cur = con.cursor()
    before = list(cur.execute(
        "SELECT COALESCE(SUM(length(CAST(%s AS BLOB))),0) FROM %s" % (column, table)
    ))[0][0]

    # ① 先脱图：会话留着，只把那坨 base64 换成占位符
    n_srow, n_shit, n_schar = strip_media(cur, table, column)
    # ② 剩下的（脱不动的）与仍然超大的整行：删掉兜底
    cur.execute(
        "DELETE FROM %s WHERE %s LIKE '%%data:image%%'" % (table, column)
    )
    n_img = cur.rowcount
    cur.execute(
        "DELETE FROM %s WHERE length(CAST(%s AS BLOB)) > ?" % (table, column),
        (int(max_mb * 1048576),),
    )
    n_big = cur.rowcount
    con.commit()

    after = list(cur.execute(
        "SELECT COALESCE(SUM(length(CAST(%s AS BLOB))),0) FROM %s" % (column, table)
    ))[0][0]

    # 只有真删了行才 VACUUM；纯脱图只是把页面腾出来复用，不值得每次都锁一次库
    if n_img or n_big:
        try:
            cur.execute("VACUUM")
            con.commit()
        except Exception as e:
            log("VACUUM 失败: %s" % str(e)[:80], log_path)
    con.close()
    return (n_srow, n_shit, n_img, n_big, before / 1048576.0, after / 1048576.0)


# ── 发言档案（2026-10-06 主人裁定）────────────────────────────────────────
# 为什么：她自己在这个会话里的历史回复，是**最强的风格锚** —— 她会照着自己抄，
# 逐日滚雪球（实测：旧群 19 条回复里 18 条同一个开头；同期新建的群 13 条里 0 条）。
# 但那些话又是她的记忆，不能删 ✗。
# 做法：把她的**纯回复**原样搬进「发言档案」文件，会话里只留最近 keep_last 条
# （防连续对话断片）→ 会话里再也没有一墙自己的旧口气；她需要时用 recall_memory
# 从档案里把原话取回来 ✓。
# **通用** ✓：prefix / file / keep_last / labels 全在 config 里，代码里没有人名与号。
# 机器文本（不是她说的话）：冒泡轮的任务描述有时会被原样存成一条 assistant 消息
# （实测档案里混进过 `[CronJob] bubble-xxx: … triggered at …`）。这类**不入档、直接移出会话** ✓。
ARCHIVE_NOISE = ("[CronJob]", "triggered at", "[auto_wake", "bubble-")


def _arch_text(msg):
    """取一条消息的可见文本（忽略 think 段）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text") for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


# 2026-10-08（主人批 ✓）：**只在这个会话静下来之后才搬** ✗ 见下 ✓。
IDLE_BEFORE_TRIM = 300      # 秒：会话静默超过这么久才搬 ✓（5 分钟）


def session_idle_seconds(cur, table, rid, now=None):
    """这个会话静默了多少秒 ✓（取 conversations.updated_at ✓ —— 它就是「上次有人说话」的时间 ✓）。

    取不到就返回 None ✓（调用方按「不静默」保守跳过 ✗ —— 宁可不搬，也不要废缓存 ✓）。
    """
    try:
        row = cur.execute("SELECT updated_at FROM %s WHERE rowid=?" % table, (rid,)).fetchone()
        if not row or not row[0]:
            return None
        s = str(row[0]).strip()[:19]
        # ⚠️ 2026-10-08 本鱼第一版踩的坑 ✗：**库里的时间戳是 UTC** ✓（实测 updated_at=14:37 vs 本地 22:37 ✓），
        #    而本鱼拿**本地时间**去比 ✗ → 算出"静默了 8 小时" ✓ → **门槛形同虚设** ✗（22:34:51 搬完 51 秒又搬 ✗）。
        #    → 两边都用 UTC ✓。
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.timezone.utc)
        return (now or datetime.datetime.now(datetime.timezone.utc)) - dt
    except Exception:
        return None


# ⚠️ 2026-10-08 主人裁定（选 1 ✓ 治本）：把**我们每轮注入的块**从**存下来的旧消息**里剥掉 ✓。
#
# 为什么必须剥 ✓：这些块**每轮都不一样** ✗（定向性 / 群缓冲 / 记忆块 ✓）→ 一旦被存进会话 ✓
# 那个群的会话就堆到 **37 万字** ✗（10 条 user 平均 **7,079 字** ✓）→ **每轮都要重发一遍** ✗
# （单轮 ≈ 40k token ✓ = 实测 1.5M/小时 ✓）；而且 janitor 一裁剪 ✓ 从中间抽消息 ✓
# 后面所有缓存全废 ✗ → 命中率卡在 60% ✓。
# 语义上也对 ✓：**那些块只对「当时那一轮」有意义** ✓ 事后就是死重 ✓（用户原话一个字不动 ✓）。
INJECT_MARKERS = (
    "【本条消息的定向性】",                    # mindscape_groupctx
    "【本群最近的真实聊天记录",                # mindscape_groupctx（群聊缓冲）
    "【上面历史里带的那几张图",                # mindscape_groupctx（图片附件说明）
    "【下面是系统给你注入的长期记忆",          # mindscape_memory
    "## 你的长期记忆",                        # mindscape_memory（SECTION_TITLE）
    "**【这一轮是你自己想开口",                # mindscape_memory（cron 轮）
    # ⚠️ 2026-10-10 补齐 ✓：漏一个记号 = 那块永远留在会话里（每轮重发 + 废缓存 ✗）→ 自检 R62 守着 ✓
    "## 你认识的人",                          # mindscape_memory（人物画像）
    "## 这一轮的消息里带了图",                # mindscape_vision（有图提醒）
    "（系统提示：正文里出现了像图片/附件的字样",  # mindscape_vision（假图标注）
    "# 沉默的权利",                           # mindscape_silence（回复轮）
    "这一轮是自主冒泡",                       # mindscape_silence（冒泡轮）
)


def strip_injections(msgs, keep_last=1):
    """剥掉**旧消息**里的注入块 ✓，返回 (新消息列表, 剥掉的字数) ✓。

    最后 keep_last 条 **user** 消息原样留着 ✓（那一轮可能还在上下文窗口里 ✓）。
    注入块永远是**整段 / 整 part 贴在末尾** ✓ → 按记号**截断**就是安全的 ✓。
    """
    if not isinstance(msgs, list):
        return msgs, 0
    uidx = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"]
    keep_from = uidx[-keep_last] if len(uidx) >= keep_last else -1
    saved = 0
    for i, m in enumerate(msgs):
        if i >= keep_from or not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            new = c
            for k in INJECT_MARKERS:
                j = new.find(k)
                if j >= 0:
                    new = new[:j].rstrip()
            if len(new) < len(c):
                saved += len(c) - len(new)
                m["content"] = new
        elif isinstance(c, list):
            kept = []
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = str(p.get("text") or "")
                    if any(t.lstrip().startswith(k) for k in INJECT_MARKERS):
                        saved += len(t)
                        continue
                kept.append(p)
            if len(kept) != len(c):
                m["content"] = kept
    return msgs, saved


def trim_users(msgs, keep):
    """只留最后 keep 条 **user** 消息（别人说的旧话）✓ 返回 (新列表, 丢掉条数) ✓。

    ⚠️ **只丢 user** ✗ —— 带 tool_calls 的 assistant 绝不单独丢 ✓
    （丢了会让它的 tool 结果变孤儿、下一次请求直接 400 ✗，AGENTS 里记过这个坑 ✓）。
    她自己的旧回复由 archive_talk 按 keep_last 搬进档案 ✓ 这里管的是**别人的话** ✓。
    为什么敢丢 ✓：群里的原话**日记早就抓过了** ✓（日记直接读网关库 ✓）
    → 会话里留着只是冗余 ✓ 而且每次请求都要重发一遍 ✗（这就是 A 要省的钱 ✓）。
    """
    if not isinstance(msgs, list) or keep <= 0:
        return msgs, 0
    idx = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"]
    if len(idx) <= keep:
        return msgs, 0
    drop = set(idx[:-keep])
    return [m for i, m in enumerate(msgs) if i not in drop], len(drop)


def archive_talk(db, table, column, id_column, targets, log_path=None):
    """把指定会话里「自己的纯回复」搬进档案文件，会话里只留最近 keep_last 条。

    返回 (处理会话数, 归档条数)。**先写档案、后改库** —— 宁可重复，不可丢失 ✓。
    带 tool_calls 的 assistant 消息**不碰**（删了会让 tool 结果变孤儿、API 报错 ✗）。
    """
    targets = targets or []
    if not db or not os.path.exists(db) or not targets:
        return (0, 0)
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    cur = con.cursor()
    n_sess = n_arch = 0
    n_saved = 0          # 剥掉注入块的字数 ✓（2026-10-08 治本那一步 ✓）
    n_dropped = 0        # 丢掉的旧 user 消息条数 ✓（2026-10-09 A ✓）
    try:
        for t in targets:
            if not isinstance(t, dict):
                continue
            prefix = str(t.get("prefix") or "")
            path = jn_abs(t.get("file"))
            keep = max(0, int(t.get("keep_last", 1)))
            labels = t.get("labels") or {}
            if not prefix or not path:
                continue
            rows = list(cur.execute(
                "SELECT rowid, %s, %s FROM %s WHERE %s LIKE ?"
                % (id_column, column, table, id_column), (prefix + "%",)))
            for rid, uid, content in rows:
                if not isinstance(content, str):
                    continue
                try:
                    msgs = json.loads(content)
                except Exception:
                    continue
                if not isinstance(msgs, list):
                    continue
                # ★★ 静默快照**必须先取** ✗（2026-10-09 真事故 ✓ 主人发现 ✓）——
                #    下面的 strip / trim 会**写库** ✓ 一写就把 updated_at 刷成「现在」✗
                #    于是稍后的静默判定永远看到「刚刚说过话」✗ → **归档被永久跳过** ✗
                #    （实测：私聊会话堆到 **287,925 字** ✗ 一次都没归档过 ✓）
                _idle = session_idle_seconds(cur, table, rid)
                if _idle is None or _idle.total_seconds() < IDLE_BEFORE_TRIM:
                    continue
                # 只在静默会话里改写历史，避免活跃期间反复打断缓存前缀。
                msgs, _saved = strip_injections(msgs)
                # ★ A（2026-10-09 主人批 ✓）：别人的旧话也只留最近 N 条 ✓
                #    顺序 ✓：先把两件事**都算完** ✓ 再**一次写入** ✓（只碰一次 updated_at ✓）
                msgs, _dropped = trim_users(msgs, int(t.get("keep_user_last", 40)))
                if _saved or _dropped:
                    cur.execute("UPDATE %s SET %s=? WHERE rowid=?" % (table, column),
                                (json.dumps(msgs, ensure_ascii=False), rid))
                    con.commit()
                    n_saved += _saved
                    n_dropped += _dropped
                plain = [i for i, m in enumerate(msgs)
                         if isinstance(m, dict) and m.get("role") == "assistant"
                         and not m.get("tool_calls") and _arch_text(m).strip()]
                # 机器文本：不入档、也不留在会话里（它不是她的发言）
                noise = {i for i in plain
                         if any(k in _arch_text(msgs[i]) for k in ARCHIVE_NOISE)}
                plain = [i for i in plain if i not in noise]
                if len(plain) <= keep and not noise:
                    continue
                # ⚠️ 2026-10-08 主人批 ✓：**她正聊着的时候别搬** ✗ ——
                #    搬一次 = 改写这个会话的内容 ✗ → 它的 prompt 前缀全变 ✗ → **下一轮必然冷启动** ✗
                #    （实测：21:14:28 冷轮次(2.2%) ↔ 21:14:30 janitor 归档 ✓ 时间戳对到秒 ✓；
                #      一小时搬 8 次 ≈ 20~40 万未命中 token ✗ 是系统里最大的一处缓存泄漏 ✓）。
                #    等她**静默 ≥ IDLE_BEFORE_TRIM** 再搬 ✓ → 那次冷启动落在一个**本来就已经冷**的会话上 ✓ = 几乎免费 ✓；
                #    而「把她的旧回复搬走、杀掉风格锚」这件事照旧成立 ✓（下次开口前早搬完了 ✓）。
                drop = (plain[:-keep] if keep else plain) + list(noise)
                drop = sorted(set(drop))
                gid = str(uid).split(":")[-1]
                label = str(labels.get(gid) or gid)
                now = datetime.datetime.now().strftime("%m-%d %H:%M")
                lines = []
                for i in drop:
                    if i in noise:
                        continue
                    txt = _arch_text(msgs[i]).strip().replace("\n", " ")
                    lines.append("- [%s][%s] %s" % (now, label, txt))
                try:
                    parent = os.path.dirname(path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    if lines:
                        with open(path, "a", encoding="utf-8") as fh:
                            fh.write("\n".join(lines) + "\n")
                except Exception as e:
                    log("发言档案写入失败，本轮跳过: %s" % str(e)[:80], log_path)
                    continue
                drop_set = set(drop)
                keep_msgs = [m for i, m in enumerate(msgs) if i not in drop_set]
                cur.execute("UPDATE %s SET %s = ? WHERE rowid = ?" % (table, column),
                            (json.dumps(keep_msgs, ensure_ascii=False), rid))
                n_sess += 1
                n_arch += len(drop)
        con.commit()
    finally:
        con.close()
    if n_dropped:
        log("裁剪别人的旧话: %d 条 user 消息 ✓（只留最近 keep_user_last 条 ✓ 原话日记里都有 ✓）"
            % n_dropped, log_path)
    if n_saved:
        log("剥离每轮注入块: %d 字 ✓（定向性 / 群缓冲 / 记忆块只对当时那一轮有用 ✓ 存着只会让每轮重发 + 废缓存 ✗）"
            % n_saved, log_path)
    return (n_sess, n_arch)


def jn_single_instance(lock_path=None):
    """拿一把单实例锁 —— 拿不到就返回 None（本轮跳过）✓。

    ⚠️ 2026-10-08 实测踩坑：线上那份 bundle 曾经**带着** janitor 上传 ✗（用了完整构建 ✓
    该用 `--market` 的 ✓），而宿主 timer 也在跑 ✓ → 两个进程同时读到同一批未归档的回复 ✗
    → 各自「先写档案」✗ → **档案里出现成块的重复**（实测两个 bot 分别脏了 4 条和 12 条 ✗ 已清 ✓）。

    本函数**不做**文本去重 ✗：archive_talk 的设计是「先写档案、后改库 —— 宁可重复，不可丢失」✓
    （这条是**故意**的 ✓ 别改成先删后写 ✗），而且她**确实可能**说两遍一样的话 ✓。
    所以这里只保证一件事：**同一时刻只有一个 janitor 在跑** ✓。
    """
    import fcntl
    path = jn_abs(lock_path or "/tmp/mindscape_janitor.lock")
    try:
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    except Exception:
        return None
    return fh          # 必须由调用方持有引用 ✓ 函数返回/进程结束才释放 ✓


def jn_main():
    c = cfg.section("janitor")
    _lock = jn_single_instance()
    if _lock is None:
        print("[mindscape_janitor] 已有一个实例在跑（或拿不到锁），本轮跳过 ✓")
        return
    if not c:
        print("[mindscape_janitor] 未找到 janitor 配置，跳过")
        return
    db = jn_abs(c.get("db"))
    table = c.get("table") or "conversations"
    column = c.get("column") or "content"
    max_mb = float(c.get("max_mb") or DEFAULT_MAX_MB)
    log_path = jn_abs(c.get("log") or DEFAULT_LOG)

    n_srow, n_shit, n_img, n_big, before, after = clean(db, table, column, max_mb, log_path)
    if n_srow or n_img or n_big:
        log("清理: 脱图 %d 行/%d 处 | 删图片行 %d | 删超大行 %d | %.2f MB -> %.2f MB"
            % (n_srow, n_shit, n_img, n_big, before, after), log_path)
    else:
        log("无需清理 | 当前 %.2f MB" % after, log_path)

    # 发言档案：把她的历史回复从会话搬进档案（风格锚 vs 记忆，两全）
    ac = cfg.section("archive") or {}
    n_sess, n_arch = archive_talk(db, table, column,
                                  str(ac.get("id_column") or "user_id"),
                                  ac.get("targets") or [], log_path)
    if n_arch:
        log("发言归档: %d 个会话 / %d 条 -> 档案（keep_last 已在会话里保留）"
            % (n_sess, n_arch), log_path)


if __name__ == "__main__":
    try:
        jn_main()
    except Exception as e:
        print("[mindscape_janitor] 失败: %s" % str(e)[:200])
        sys.exit(1)