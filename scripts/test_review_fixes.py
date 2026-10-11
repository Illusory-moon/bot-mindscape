import datetime
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import mindscape_sync as sync
import mindscape_webedit as webedit
from mindscape_janitor import archive_talk, clean


class ReviewFixesTest(unittest.TestCase):
    def test_archive_keeps_entire_reply_and_missing_db_has_full_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "history.db")
            archive = os.path.join(tmp, "said.md")
            reply = "长回复" * 200
            with closing(sqlite3.connect(db)) as con:
                with con:
                    # ⚠️ 会话必须**已经静默**才会被归档 ✓ —— `archive_talk` 有一道闸：
                    #       _idle = session_idle_seconds(cur, table, rid)
                    #       if _idle is None or _idle.total_seconds() < IDLE_BEFORE_TRIM: continue
                    #    （正聊着搬会废掉 prompt 缓存前缀 ✗；快照取晚了会永远跳过 ✗ —— 两处事故
                    #      在 janitor 的注释里都记着 ✓）
                    #    所以表里**必须有 `updated_at` 且是过去的时间**：
                    #    没有这一列时 `session_idle_seconds` 返回 None ⇒ 一律跳过 ⇒ 本测试会空跑 ✗
                    #    （2026-10-12 修：原稿就是没有这一列，于是它**在 main 上一直红着** ✓
                    #      而线上功能其实是好的 —— janitor 日志里有 617 次「发言归档」✓）
                    con.execute("CREATE TABLE conversations "
                                "(user_id TEXT, content TEXT, updated_at TEXT)")
                    idle = (datetime.datetime.now(datetime.timezone.utc)
                            - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                    con.execute("INSERT INTO conversations VALUES (?, ?, ?)",
                                ("bot:group", json.dumps([{"role": "assistant", "content": reply}]),
                                 idle))
                    con.execute("INSERT INTO conversations VALUES (?, ?, ?)",
                                ("bot:noise", json.dumps([{"role": "assistant", "content":
                                                           "[CronJob] bubble-task triggered at 12:00"}]),
                                 idle))
            self.assertEqual(archive_talk(db, "conversations", "content", "user_id",
                                          [{"prefix": "bot:", "file": archive,
                                            "keep_last": 0}]), (2, 2))
            with open(archive, encoding="utf-8") as source:
                archived = source.read()
            self.assertIn(reply, archived)
            self.assertNotIn("[CronJob]", archived)
            self.assertEqual(len(clean(os.path.join(tmp, "missing.db"),
                                       "conversations", "content", 2)), 6)

    def test_invalid_index_cannot_leave_gallery(self):
        for filename in ("../outside", "C:\\outside", "/outside"):
            with self.assertRaises(ValueError):
                sync._items([{"file": filename}])
            self.assertEqual(webedit.remove_item("x", filename, delete_file=True)[0], False)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.json")
            with open(path, "w", encoding="utf-8") as out:
                out.write("{bad")
            with self.assertRaises(json.JSONDecodeError):
                sync._load(path)


if __name__ == "__main__":
    unittest.main()
