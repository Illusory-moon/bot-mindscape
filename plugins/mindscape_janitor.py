# -*- coding: utf-8 -*-
"""mindscape_janitor —— 运维层：会话历史防膨胀

问题：不少 bot 框架会把聊天里的图片以 base64 形式存进会话历史。
      攒到几十 MB 后，每轮请求都要把这坨东西发给模型 ——
      上传超时 → bot 看起来「卡死」，但日志里一句报错都没有。

方案：定期清理「含 base64 图片」和「体积过大」的会话记录。
      记忆的职责交给 diary/memory（独立文件），不会因此失忆。

用法（独立脚本，不需要装在 bot 框架里）：
    python mindscape_janitor.py
配合 cron / systemd timer 每 15 分钟跑一次即可。
"""
import datetime
import os
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


def clean(db, table, column, max_mb, log_path=None):
    """清理含图片的会话 + 超大行。返回 (删图片数, 删超大数, 清理前MB, 清理后MB)。"""
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

    if n_img or n_big:
        try:
            cur.execute("VACUUM")
            con.commit()
        except Exception as e:
            log("VACUUM 失败: %s" % str(e)[:80], log_path)
    con.close()
    return (n_img, n_big, before / 1048576.0, after / 1048576.0)


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

    n_img, n_big, before, after = clean(db, table, column, max_mb, log_path)
    if n_img or n_big:
        log("清理: 图片会话 %d | 超大行 %d | %.2f MB -> %.2f MB"
            % (n_img, n_big, before, after), log_path)
    else:
        log("无需清理 | 当前 %.2f MB" % after, log_path)


if __name__ == "__main__":
    try:
        jn_main()
    except Exception as e:
        print("[mindscape_janitor] 失败: %s" % str(e)[:200])
        sys.exit(1)