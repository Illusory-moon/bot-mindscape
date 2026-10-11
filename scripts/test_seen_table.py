# -*- coding: utf-8 -*-
"""去重表（seen.json）口径的定点检查。

背景（2026-10-12 实修）：插件早就把 seen.json 从「平表」升级成
{"accepted": [...], "rejected": [...]}，键 = `分类:内容md5`，而管理台仍按
「平表 + 文件名前 10 位」判活 —— 后果有三：
  1. 去重表页**恒为空白**（load_seen 遇到 dict 直接返回空表）；
  2. 判活把 `sparxie_<md5前10位>.gif` 这种（导入脚本产的）名字误判成墓碑；
  3. 「清理墓碑」拿到空表后会把整张表写成 `[]`（等于清空），而且连 rejected 一起删。
本文件守住修好之后的口径：认 dict 格式、按内容 md5 判活、只清 accepted 里的墓碑。
"""
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLUGINS = os.path.join(REPO, "plugins")
for _p in (HERE, PLUGINS, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _md5(data):
    return hashlib.md5(data).hexdigest()


class SeenTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.stickers = os.path.join(root, "stickers")
        os.makedirs(self.stickers)

        # 两张图：一张按 md5 前 10 位命名（bot 自动采集），一张带 sparxie_ 前缀（导入脚本）
        self.body_a = b"sticker-A-body"
        self.body_b = b"sticker-B-body"
        self.hash_a = _md5(self.body_a)
        self.hash_b = _md5(self.body_b)
        self.file_a = self.hash_a[:10] + ".gif"
        self.file_b = "sparxie_" + self.hash_b[:10] + ".gif"
        for name, body in ((self.file_a, self.body_a), (self.file_b, self.body_b)):
            with open(os.path.join(self.stickers, name), "wb") as f:
                f.write(body)

        self.index = os.path.join(self.stickers, "stickers_index.json")
        with open(self.index, "w", encoding="utf-8") as f:
            json.dump([{"file": self.file_a, "category": "sparxie", "name": "A"},
                       {"file": self.file_b, "category": "sparxie", "name": "B"}], f)

        self.seen = os.path.join(self.stickers, "seen.json")
        self.cfg = os.path.join(root, "config.yaml")
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("stickers:\n"
                    "  dir: %s\n  index: %s\n  seen: %s\n"
                    % (self.stickers.replace("\\", "/"), self.index.replace("\\", "/"),
                       self.seen.replace("\\", "/")))

        os.environ["MINDSCAPE_CONFIG"] = self.cfg
        import mindscape_config
        importlib.reload(mindscape_config)
        import web_ui
        self.web = importlib.reload(web_ui)

    def tearDown(self):
        os.environ.pop("MINDSCAPE_CONFIG", None)
        self.tmp.cleanup()

    def _write_seen(self, data):
        with open(self.seen, "w", encoding="utf-8") as f:
            json.dump(data, f)

    # ── 1. 两种格式都要认 ──────────────────────────────────────────────
    def test_reads_dict_format(self):
        self._write_seen({"accepted": ["sparxie:aaa"], "rejected": ["shuimeng:bbb"]})
        self.assertEqual(self.web.load_seen_lists(),
                         (["sparxie:aaa"], ["shuimeng:bbb"]))
        self.assertEqual(sorted(self.web.load_seen()), ["shuimeng:bbb", "sparxie:aaa"])

    def test_reads_legacy_flat_list(self):
        self._write_seen(["sparxie:aaa", "general:ccc"])
        self.assertEqual(self.web.load_seen_lists(),
                         (["sparxie:aaa", "general:ccc"], []))

    # ── 2. 判活必须按内容 md5，不能按文件名前缀 ────────────────────────
    def test_live_keys_use_content_md5(self):
        live = self.web.live_seen_keys()
        self.assertEqual(live, {"sparxie:" + self.hash_a, "sparxie:" + self.hash_b})
        # 带前缀的那张：前缀不是 md5，按前缀凑就会得到 "sparxie:sparxie_xx" —— 必须不得出现
        self.assertNotIn("sparxie:" + self.file_b[:10], live)

    # ── 3. 墓碑 / 入库 / 拒收 三态 ────────────────────────────────────
    def test_states(self):
        dead = "sparxie:" + "0" * 32
        self._write_seen({"accepted": ["sparxie:" + self.hash_a, dead],
                          "rejected": ["shuimeng:" + "1" * 32]})
        rows = {r["key"]: r for r in self.web.seen_rows()}
        self.assertEqual(rows["sparxie:" + self.hash_a]["state"], "live")
        self.assertEqual(rows[dead]["state"], "tomb")
        self.assertFalse(rows[dead]["live"])
        # 拒收记录重启也保留：不能算墓碑，也不能被「清理墓碑」删掉
        rej = rows["shuimeng:" + "1" * 32]
        self.assertEqual(rej["state"], "rejected")
        self.assertTrue(rej["live"])
        # 墓碑排在最前面，方便一眼看到
        self.assertEqual(self.web.seen_rows()[0]["state"], "tomb")

    # ── 4. 写回必须保持 dict 格式（否则插件会当旧平表迁移） ────────────
    def test_save_keeps_dict_format(self):
        self._write_seen({"accepted": ["sparxie:old"], "rejected": ["shuimeng:keep"]})
        self.web.save_seen(["sparxie:new"], ["shuimeng:keep"])
        with open(self.seen, encoding="utf-8") as f:
            d = json.load(f)
        self.assertIsInstance(d, dict)
        self.assertEqual(d["accepted"], ["sparxie:new"])
        self.assertEqual(d["rejected"], ["shuimeng:keep"])

    # ── 5. 清理墓碑：只删真墓碑，拒收原样保留，绝不写空 ────────────────
    def test_prune_only_drops_tombstones(self):
        dead = "sparxie:" + "0" * 32
        self._write_seen({"accepted": ["sparxie:" + self.hash_a, dead],
                          "rejected": ["shuimeng:" + "1" * 32]})
        accepted, rejected, removed = self.web.prune_seen_plan()
        self.assertEqual(accepted, ["sparxie:" + self.hash_a])
        self.assertEqual(rejected, ["shuimeng:" + "1" * 32])
        self.assertEqual(removed, 1)

    def test_prune_refuses_when_library_unreadable(self):
        # 索引指到一个不存在的文件：判活不可信 -> 方案为 None（这次不删）
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write("stickers:\n  dir: %s\n  index: %s\n  seen: %s\n"
                    % (self.stickers.replace("\\", "/"),
                       os.path.join(self.stickers, "missing.json").replace("\\", "/"),
                       self.seen.replace("\\", "/")))
        import mindscape_config
        importlib.reload(mindscape_config)
        self.web = importlib.reload(self.web)
        self._write_seen({"accepted": ["sparxie:whatever"], "rejected": []})
        self.assertIsNone(self.web.prune_seen_plan())


if __name__ == "__main__":
    unittest.main(verbosity=2)
