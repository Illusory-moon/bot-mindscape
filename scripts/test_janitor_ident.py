# -*- coding: utf-8 -*-
"""janitor 的 SQL 标识符守卫：表名 / 字段名必须过白名单，且**两处口径一致**。

背景（2026-10-12 按 docs/review-prompt.md 复查时发现）：
配置里的 `table` / `column` / `id_column` 是要**拼进 SQL** 的（值早就参数化了 ✓，
标识符只能拼 ✗）。作者其实写好了 `_safe_ident()` 白名单守卫，但**只在 `clean()` 里用了**，
`archive_talk()` 那条路拿的是没净化的原值 ✗ —— 同一份配置两处口径不一致，
等于白防（配置一旦被写坏 / 从别处同步进来，archive 那半边就是注入面）。

本文件守住：① `_safe_ident` 的判定 ② 两条路都净化（喂恶意表名也不出事、也不动错表）。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (HERE, os.path.join(REPO, "plugins"), REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = importlib.util.spec_from_file_location(
    "mindscape_janitor", os.path.join(REPO, "plugins", "mindscape_janitor.py"))
jn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jn)

EVIL = 'conversations"; DROP TABLE conversations; --'


def _make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE conversations (user_id TEXT, content TEXT)")
    body = json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200}}])
    con.execute("INSERT INTO conversations VALUES (?, ?)", ("u1", body))
    con.execute("INSERT INTO conversations VALUES (?, ?)", ("u1", "normal text"))
    con.commit()
    con.close()


def _tables(path):
    con = sqlite3.connect(path)
    got = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    return got


class JanitorIdentTest(unittest.TestCase):
    def test_safe_ident_rules(self):
        self.assertEqual(jn._safe_ident("conversations", "fb"), "conversations")
        self.assertEqual(jn._safe_ident("a_b1", "fb"), "a_b1")
        for bad in (EVIL, "1abc", "", None, "a-b", "a b", "表名", "a;b"):
            self.assertEqual(jn._safe_ident(bad, "fb"), "fb", "该拒绝: %r" % (bad,))

    def test_clean_falls_back_on_evil_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            _make_db(db)
            jn.clean(db, EVIL, "content", 2.0, None)
            self.assertIn("conversations", _tables(db))          # 表还在（没被 DROP）✓
            con = sqlite3.connect(db)
            rows = con.execute("SELECT content FROM conversations").fetchall()
            con.close()
            self.assertIn("normal text", [r[0] for r in rows])   # 动的是回退表 ✓

    def test_archive_talk_falls_back_on_evil_idents(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            _make_db(db)
            out = os.path.join(d, "archive.md")
            # 三个标识符全喂恶意值：不能抛异常、不能把表搞没 ✓
            n_sess, n_arch = jn.archive_talk(db, EVIL, EVIL, EVIL,
                                             [{"prefix": "", "file": out, "keep_last": 1}], None)
            self.assertIsInstance(n_sess, int)
            self.assertIsInstance(n_arch, int)
            self.assertIn("conversations", _tables(db))

    def test_archive_talk_source_guards(self):
        # 口径一致性（源码级）：archive_talk 自己也净化，main() 两处也净化 ✓
        src = open(os.path.join(REPO, "plugins", "mindscape_janitor.py"), encoding="utf-8").read()
        head = src.split("def archive_talk")[1][:600]
        self.assertIn('_safe_ident(table, "conversations")', head)
        self.assertIn('_safe_ident(column, "content")', head)
        self.assertIn('_safe_ident(id_column, "user_id")', head)
        self.assertIn('_safe_ident(c.get("table")', src)
        self.assertIn('_safe_ident(c.get("column")', src)
        self.assertIn('_safe_ident(ac.get("id_column")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
