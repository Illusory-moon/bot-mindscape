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
                plain = [i for i, m in enumerate(msgs)
                         if isinstance(m, dict) and m.get("role") == "assistant"
                         and not m.get("tool_calls") and _arch_text(m).strip()]
                # 机器文本：不入档、也不留在会话里（它不是她的发言）
                noise = {i for i in plain
                         if any(k in _arch_text(msgs[i]) for k in ARCHIVE_NOISE)}
                plain = [i for i in plain if i not in noise]
                if len(plain) <= keep and not noise:
                    continue
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
    return (n_sess, n_arch)


def jn_main():
    c = cfg.section("janitor")
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
