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
                    con.execute("CREATE TABLE conversations (user_id TEXT, content TEXT)")
                    con.execute("INSERT INTO conversations VALUES (?, ?)",
                                ("bot:group", json.dumps([{"role": "assistant", "content": reply}])))
                    con.execute("INSERT INTO conversations VALUES (?, ?)",
                                ("bot:noise", json.dumps([{"role": "assistant", "content":
                                                           "[CronJob] bubble-task triggered at 12:00"}])))
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
