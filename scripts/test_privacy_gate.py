# -*- coding: utf-8 -*-
import datetime
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
sys.path.insert(0, os.path.dirname(__file__))
import mindscape_gate as gate
import privacy_gate_auth as privacy


class FakeSftp:
    def __init__(self, files):
        self.files = files

    def open(self, path, mode):
        if mode == "a":
            return AppendFile(self.files, path)
        return io.BytesIO(self.files[path])


class AppendFile:
    def __init__(self, files, path):
        self.files, self.path = files, path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def write(self, text):
        self.files[self.path] = self.files.get(self.path, b"") + text.encode()


class GateTest(unittest.TestCase):
    def test_rotation_expiry_and_mix(self):
        now = datetime.datetime(2026, 10, 8, 4, tzinfo=gate.PG_ZONE)
        text, changed = gate.pg_rotate("# 账本\n- 别的：保留\n", now)
        self.assertTrue(changed)
        code, expiry = gate.pg_current(text, now)
        self.assertTrue(gate.pg_valid(code))
        self.assertEqual(expiry, now + datetime.timedelta(hours=24))
        self.assertEqual(gate.pg_rotate(text, now)[1], False)
        self.assertFalse(gate.pg_matches(text, code, expiry)[0])
        custom, _ = gate.pg_rotate(text, now, length=8, charset="AB234", force=True)
        custom_code, _ = gate.pg_current(custom, now)
        self.assertTrue(gate.pg_valid(custom_code, 8, "AB234"))
        with self.assertRaises(ValueError):
            gate.pg_generate(1)
        with self.assertRaises(ValueError):
            gate.pg_generate(6, "ABC")

    def test_outbound_detection_ignores_case(self):
        now = datetime.datetime.now(gate.PG_ZONE)
        note, _ = gate.pg_rotate("# 账本\n", now)
        code, _ = gate.pg_current(note)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.md")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(note)
            with patch.object(gate, "pg_notes", return_value=path), \
                 patch.object(gate.cfg, "bot_entries", return_value=[{"self_id": "a"}]):
                self.assertTrue(gate.pg_secret_in("a", "先说 " + code.lower()))
                self.assertNotIn(code.lower(), gate.pg_redact("先说 " + code.lower()))

    def test_authorization_is_per_bot_and_locked(self):
        now = datetime.datetime.now(gate.PG_ZONE)
        note, _ = gate.pg_rotate("# 账本\n", now)
        code, _ = gate.pg_current(note)
        config = {"privacy_gate": {"enabled": True, "audit_file": "/audit", "max_tries": 2,
                                    "private_self_ids": ["a", "b"]},
                  "memory": {"bots": [{"self_id": "a", "notes": "/a"},
                                      {"self_id": "b", "notes": "/b"}]}}
        files = {"/config": json.dumps(config).encode(), "/a": note.encode(),
                 "/b": gate.pg_rotate("# 账本\n", now, force=True)[0].encode()}
        connection = types.SimpleNamespace(open_sftp=lambda: FakeSftp(files), close=lambda: None)
        with patch.object(privacy.sync, "_connect", return_value=connection), \
             patch.object(privacy.managed, "_paths", return_value={"config": "/config"}), \
             patch.dict(privacy.TRIES, {}, clear=True), patch.dict(privacy.SESSIONS, {}, clear=True):
            token, ttl = privacy.authorize("a", code, "127.0.0.1")
            self.assertEqual(ttl, 86400)
            self.assertEqual(privacy.session(token, "127.0.0.1"), "a")
            self.assertEqual(privacy.session(token, "127.0.0.2"), "")
            with self.assertRaises(ValueError):
                privacy.authorize("b", code, "127.0.0.1")
            with self.assertRaises(ValueError):
                privacy.authorize("b", code, "127.0.0.1")
            with self.assertRaises(ValueError):
                privacy.authorize("b", code, "127.0.0.1")
            self.assertNotIn(code.encode(), files["/audit"])


if __name__ == "__main__":
    unittest.main()
