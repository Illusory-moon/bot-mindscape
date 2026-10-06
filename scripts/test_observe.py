# -*- coding: utf-8 -*-
"""Focused checks for read-only server snapshots."""
import io
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mindscape_observe as observe


class FakeSftp:
    def __init__(self, files):
        self.files = files

    def stat(self, path):
        if path not in self.files:
            raise OSError(2, "No such file")
        return types.SimpleNamespace(st_mode=stat.S_IFREG, st_size=len(self.files[path]))

    def open(self, path, mode):
        return io.BytesIO(self.files[path])

    def listdir_attr(self, path):
        return [types.SimpleNamespace(filename=os.path.basename(p), st_mode=stat.S_IFREG)
                for p in self.files if os.path.dirname(p) == path]


class FakeClient:
    def __init__(self, files):
        self.sftp = FakeSftp(files)

    def open_sftp(self):
        return self.sftp

    def exec_command(self, command, timeout):
        output = io.BytesIO(b"2026-10-07 event\n")
        output.channel = types.SimpleNamespace(recv_exit_status=lambda: 0)
        return None, output, None

    def close(self):
        pass


class ObserveTest(unittest.TestCase):
    def test_memory_and_logs_are_separate_and_preview_is_scoped(self):
        config = {"memory": {"bots": [{"self_id": "a", "diary": "/data/a.md", "notes": "/data/missing.md"}]},
                  "archive": {"targets": [{"self_id": "a", "file": "/data/said.md"}]},
                  "janitor": {"log": "/logs/janitor.log"}}
        files = {"/config.yaml": yaml.safe_dump(config).encode(),
                 "/data/a.md": b"## today\n- memory\n",
                 "/data/said.md": b"## today\n- said\n",
                 "/logs/janitor.log": b"janitor ok\n",
                 "/logs/other.log": b"other ok\n"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(observe, "ROOT", tmp), \
             patch.object(observe.managed, "_paths", return_value={"config": "/config.yaml"}), \
             patch.object(observe.sync, "available", return_value=True), \
             patch.object(observe.sync, "_connect", return_value=FakeClient(files)), \
             patch.object(observe.sync, "sync_cfg", return_value={"restart_command": "docker restart bot"}):
            self.assertEqual(observe.pull("memory"), {"count": 2, "missing": 1, "errors": 0})
            self.assertEqual(observe.pull("logs"), {"count": 3, "missing": 0, "errors": 0})
            memory = observe.listing("memory")
            logs = observe.listing("logs")
            self.assertIn("memory", observe.preview("memory", memory[0]["id"])["text"])
            self.assertTrue(any(e["path"] == "docker:bot" for e in logs))
            with self.assertRaises(FileNotFoundError):
                observe.preview("logs", memory[0]["id"])
            with self.assertRaises(ValueError):
                observe.preview("memory", "../../config.yaml")


if __name__ == "__main__":
    unittest.main()
