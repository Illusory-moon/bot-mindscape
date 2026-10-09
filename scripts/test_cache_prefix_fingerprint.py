# -*- coding: utf-8 -*-
"""Check that outgoing segment hashes change only with segment content."""
import ast
import hashlib
import importlib.util
import pathlib
import sys
import types


root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cache_prefix_fingerprint", root / "patches" / "astrbot" / "cache_prefix_fingerprint.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
patched = patcher.patch_source(source)
assert patcher.patch_source(patched) == patched
tree = ast.parse(patched)
runner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "ToolLoopAgentRunner")
method = next(node for node in runner.body if isinstance(node, ast.AsyncFunctionDef)
              and node.name == "_iter_llm_responses")
site = next(i for i, node in enumerate(method.body)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "parts"
                                                   for t in node.targets))
block = ast.Module(body=method.body[site:site + 3], type_ignores=[])
ast.fix_missing_locations(block)
log = []
logger = types.SimpleNamespace(info=lambda fmt, *args: log.append(fmt % args))
code = compile(block, "<fingerprint>", "exec")


class Message:
    role = "system"

    def __init__(self, content):
        self.content = content

    def model_dump_json(self):
        return self.content


scope = {"hashlib": hashlib, "logger": logger,
         "self": types.SimpleNamespace(req=types.SimpleNamespace(session_id="s1"))}
for value in ("same", "same", "changed"):
    exec(code, scope | {"payload": {"contexts": [Message(value)]}})
assert log[0] == log[1]
assert log[1] != log[2]
assert "same" not in log[0] and "changed" not in log[2]
print("cache prefix fingerprint: OK")
