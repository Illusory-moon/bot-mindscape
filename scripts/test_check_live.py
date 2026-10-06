# -*- coding: utf-8 -*-
import os
import io
import sys
import types
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_live
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

    def test_channel_report_distinguishes_binding_from_expected_persona(self):
        config = {"memory": {"bots": [{"self_id": "web", "name": "网页", "diary": "/web.md"},
                                      {"self_id": "qq", "diary": "/qq.md"}]},
                  "format": {"targets": ["web", "qq"], "no_period": ["web"]},
                  "janitor": {"db": "/db.sqlite"}}
        client = types.SimpleNamespace(
            open_sftp=lambda: types.SimpleNamespace(open=lambda *a: io.BytesIO(yaml.safe_dump(config).encode())),
            close=lambda: None)
        settings = {"text_channel": "web", "expected_personas": {"web": "right"}}
        with patch.object(check_live.sync, "available", return_value=True), \
             patch.object(check_live.sync, "_connect", return_value=client), \
             patch.object(check_live.sync, "sync_cfg", return_value=settings), \
             patch.object(check_live.managed, "_paths", return_value={"config": "/config.yaml"}), \
             patch.object(check_live, "persona_rows", return_value=[("web:session", "wrong")]):
            report = check_live.channels_report()
            self.assertEqual(report["channels"][0]["persona"], "与预期不符")
            self.assertEqual(report["channels"][1]["persona"], "未核对")
            self.assertIsNone(report["channels"][1]["bindings"])
            self.assertTrue(any("人格绑定" in x for x in report["issues"]))
        with patch.object(check_live.sync, "available", return_value=True), \
             patch.object(check_live.sync, "_connect", return_value=client), \
             patch.object(check_live.sync, "sync_cfg", return_value=settings), \
             patch.object(check_live.managed, "_paths", return_value={"config": "/config.yaml"}), \
             patch.object(check_live, "persona_rows", return_value=[("web:session", "right")]):
            report = check_live.channels_report()
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["channels"][0]["persona"], "符合预期")
            settings["text_channel"] = ""
            report = check_live.channels_report()
            self.assertTrue(any("text_channel 未配置" in x for x in report["issues"]))


if __name__ == "__main__":
    unittest.main()
