# -*- coding: utf-8 -*-
"""Ensure diary entries keep their source when the model omits or invents labels."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import mindscape_diary as diary


class DiaryGroupsTest(unittest.TestCase):
    def test_split_and_write_source_labels(self):
        rows = [dict(ts=1, seq=1, gid="a", gname="一群", time="01-01 00:00", who="甲", txt="甲说话"),
                dict(ts=2, seq=2, gid="b", gname="二群", time="01-01 00:01", who="乙", txt="乙说话")]
        self.assertEqual([len(x) for x in diary.chunk_by_budget(rows, 40, 14000, split_group=True)], [1, 1])
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "diary.md")
            target = {"output": output, "state": output + ".state.json"}
            answers = iter([{"diary": ["没写群名"], "people": {}},
                            {"diary": ["【猜错的群】写错群名"], "people": {}}])
            with patch.object(diary, "fetch", return_value=rows), \
                 patch.object(diary, "call_llm", side_effect=lambda *a: next(answers)), \
                 patch.object(diary, "load_relations", return_value=[]):
                self.assertEqual(diary.run_target({"targets": [target]}), (2, 2))
            with open(output, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("- 【一群】没写群名", text)
            self.assertIn("- 【二群】写错群名", text)
            self.assertNotIn("猜错的群", text)


if __name__ == "__main__":
    unittest.main()
