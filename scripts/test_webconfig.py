# -*- coding: utf-8 -*-
"""Focused checks for the web configuration editor."""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

import mindscape_webconfig as webconfig


class FakeSftp:
    def __init__(self, files):
        self.files = files

    def open(self, path, mode):
        if "r" in mode:
            return io.BytesIO(self.files[path])
        files = self.files

        class Writer(io.BytesIO):
            def close(self):
                files[path] = self.getvalue()
                super().close()

        return Writer()

    def posix_rename(self, old, new):
        self.files[new] = self.files.pop(old)


class FakeClient:
    def __init__(self, files):
        self.sftp = FakeSftp(files)

    def open_sftp(self):
        return self.sftp

    def close(self):
        pass


class WebConfigTest(unittest.TestCase):
    def test_bot_isolation_and_remote_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {"config": os.path.join(tmp, "managed.yaml"),
                     "wake": os.path.join(tmp, "managed-wake.json")}
            state = os.path.join(tmp, "managed.sync.json")
            remote_paths = {"config": "/remote/config.yaml", "wake": "/remote/wake.json"}
            config = {"memory": {"bots": [{"self_id": "a", "name": "A", "rules": ["old"]},
                                         {"self_id": "b", "name": "B", "rules": ["keep"]}]},
                      "other": {"untouched": 42}}
            original = "# keep this comment\n" + yaml.safe_dump(config)
            files = {remote_paths["config"]: original.encode(),
                     remote_paths["wake"]: json.dumps({"per_bot": {"a": {"min_interval": 600},
                                                                  "b": {"min_interval": 900}}}).encode()}
            client = FakeClient(files)
            with patch.object(webconfig, "LOCAL", paths), patch.object(webconfig, "STATE", state), \
                 patch.object(webconfig, "_paths", return_value=remote_paths), \
                 patch.object(webconfig.sync, "available", return_value=True), \
                 patch.object(webconfig.sync, "_connect", return_value=client), \
                 patch.object(webconfig.sync, "sync_cfg", return_value={}):
                webconfig.pull()
                self.assertFalse(webconfig.snapshot()["dirty"])
                webconfig.change("config", "memory.bots.rules", "a", "")
                webconfig.change("config", "memory.bots.rules", "a", "new\nsecond")
                webconfig.change("config", "memory.bots.people_chars", "a", 400)
                webconfig.change("wake", "per_bot.min_interval", "a", 300)
                self.assertEqual(webconfig.snapshot()["values"]["b"]["config:memory.bots.rules"], "keep")
                with open(paths["config"], encoding="utf-8") as f:
                    local = f.read()
                self.assertIn("# keep this comment", local)
                self.assertEqual(yaml.safe_load(local)["other"], {"untouched": 42})
                files[remote_paths["config"]] += b"\n# remote edit\n"
                with self.assertRaisesRegex(RuntimeError, "已有新改动"):
                    webconfig.push()
                files[remote_paths["config"]] = original.encode()
                self.assertIn("已同步", webconfig.push())
                saved = yaml.safe_load(files[remote_paths["config"]])
                self.assertEqual(saved["memory"]["bots"][0]["rules"], ["new", "second"])
                self.assertEqual(saved["memory"]["bots"][0]["people_chars"], 400)
                self.assertEqual(saved["memory"]["bots"][1]["rules"], ["keep"])
                self.assertEqual(json.loads(files[remote_paths["wake"]])["per_bot"]["b"]["min_interval"], 900)
                self.assertFalse(webconfig.snapshot()["dirty"])


if __name__ == "__main__":
    unittest.main()
