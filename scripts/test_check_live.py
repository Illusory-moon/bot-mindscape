# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_live import check_config


class LiveCheckTest(unittest.TestCase):
    def test_text_channel_and_memory_ownership(self):
        config = {"memory": {"bots": [{"self_id": "a", "diary": "/a.md"},
                                      {"self_id": "web", "diary": "/web.md"}]},
                  "format": {"targets": ["a", "web"], "no_period": ["web"]},
                  "groupctx": {"targets": ["a"]},
                  "vision": {"targets": ["a"]},
                  "stickers": {"targets": [{"self_id": "a"}]}}
        self.assertEqual(check_config(config, "web"), [])
        config["memory"]["bots"][1]["diary"] = "/a.md"
        config["groupctx"]["targets"].append("web")
        issues = check_config(config, "web")
        self.assertTrue(any("共用记忆文件" in issue for issue in issues))
        self.assertTrue(any("groupctx" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
