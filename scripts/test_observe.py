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
from mindscape_gate import pg_rotate, pg_current


class FakeSftp:
    def __init__(self, files):
        self.files = files

    def stat(self, path):
        if path not in self.files:
            raise OSError(2, "No such file")
        return types.SimpleNamespace(st_mode=stat.S_IFREG, st_size=len(self.files[path]),
                                     st_mtime=1700000000)

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
                 "/plugins/impression/states.json": b'{"bots":{"a":{"person":{"impression":"A"}},"b":{"person":{"impression":"B"}}}}',
                 "/logs/janitor.log": b"janitor ok\n",
                 "/logs/other.log": b"other ok\n"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(observe, "ROOT", tmp), \
             patch.object(observe.managed, "_paths", return_value={"config": "/config.yaml"}), \
             patch.object(observe.sync, "available", return_value=True), \
             patch.object(observe.sync, "_connect", return_value=FakeClient(files)), \
             patch.object(observe.sync, "sync_cfg", return_value={"restart_command": "docker restart bot",
                                                                   "remote_impression": "/plugins/impression/states.json"}):
            self.assertEqual(observe.pull("memory", "a"), {"count": 2, "missing": 0, "errors": 0})
            self.assertEqual(observe.pull("notes", "a"), {"count": 0, "missing": 1, "errors": 0})
            self.assertEqual(observe.pull("impression", "a"), {"count": 1, "missing": 0, "errors": 0})
            self.assertEqual(observe.pull("logs"), {"count": 3, "missing": 0, "errors": 0})
            memory = observe.listing("memory", "a")
            impression = observe.listing("impression", "a")
            logs = observe.listing("logs")
            self.assertEqual(memory[0]["mtime"], 1700000000)
            self.assertEqual(memory[0]["size"], len(files["/data/a.md"]))
            self.assertTrue(any(e.get("missing") and "mtime" not in e for e in observe.listing("notes", "a")))
            self.assertIn("memory", observe.preview("memory", memory[0]["id"], "a")["text"])
            shown = observe.preview("impression", impression[0]["id"], "a")["text"]
            self.assertIn('"a"', shown)
            self.assertNotIn('"b"', shown)
            self.assertTrue(any(e["path"] == "docker:bot" for e in logs))
            with self.assertRaises(FileNotFoundError):
                observe.preview("logs", memory[0]["id"])
            with self.assertRaises(FileNotFoundError):
                observe.preview("memory", impression[0]["id"], "a")
            with self.assertRaises(FileNotFoundError):
                observe.preview("memory", memory[0]["id"], "b")
            with self.assertRaises(ValueError):
                observe.preview("memory", "../../config.yaml", "a")

    def test_logs_hide_current_code(self):
        note, _ = pg_rotate("# 账本\n")
        code, _ = pg_current(note)
        config = {"privacy_gate": {"enabled": True, "private_self_ids": ["a"]},
                  "memory": {"bots": [{"self_id": "a", "notes": "/notes.md"}]}}
        files = {"/config.yaml": yaml.safe_dump(config).encode(), "/notes.md": note.encode(),
                 "/logs/app.log": ("sent " + code + "\n").encode()}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(observe, "ROOT", tmp), \
             patch.object(observe.managed, "_paths", return_value={"config": "/config.yaml"}), \
             patch.object(observe.sync, "available", return_value=True), \
             patch.object(observe.sync, "_connect", return_value=FakeClient(files)), \
             patch.object(observe.sync, "sync_cfg", return_value={"remote_logs": ["/logs/app.log"]}):
            observe.pull("logs")
            ident = observe.listing("logs")[0]["id"]
            self.assertNotIn(code, observe.preview("logs", ident)["text"])


if __name__ == "__main__":
    unittest.main()
