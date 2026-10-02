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


def strip_media(con, table, column):
    """把记录里内联的 base64 图片就地换成占位符。

    返回 (改了几行, 替换几处, 砍掉几字)。
    只改「改完仍是合法 JSON」的行 —— 会话历史是 JSON 数组，改成坏 JSON 比不改更糟。
    """
    rows = list(con.execute(
        "SELECT rowid, %s FROM %s WHERE %s LIKE '%%data:image%%'" % (column, table, column)
    ))
    n_row = n_hit = n_chars = 0
    for rid, content in rows:
        if not isinstance(content, str) or "data:image" not in content:
            continue
        hits = DATA_URL_RE.findall(content)
        if not hits:
            continue
        new = DATA_URL_RE.sub("[图片]", content)
        try:
            json.loads(new)
        except Exception:
            continue          # 改不动就留给下面「删行」兜底
        con.execute("UPDATE %s SET %s = ? WHERE rowid = ?" % (table, column), (new, rid))
        n_row += 1
        n_hit += len(hits)
        n_chars += sum(len(h) for h in hits)
    return (n_row, n_hit, n_chars)


def clean(db, table, column, max_mb, log_path=None):
    """脱图 + 清理超大行。返回 (脱图行, 替换处, 删图片行, 删超大行, 前MB, 后MB)。"""
    if not db or not os.path.exists(db):
        log("数据库不存在，跳过: %s" % db, log_path)
        return (0, 0, 0.0, 0.0)
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


if __name__ == "__main__":
    try:
        jn_main()
    except Exception as e:
        print("[mindscape_janitor] 失败: %s" % str(e)[:200])
        sys.exit(1)