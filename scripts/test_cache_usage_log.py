# -*- coding: utf-8 -*-
"""Check that agent usage logging covers tool responses and missing usage."""
import ast
import importlib.util
import pathlib
import sys
import types


root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cache_usage_log", root / "patches" / "astrbot" / "cache_usage_log.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
patched = patcher.patch_source(source)
assert patcher.patch_source(patched) == patched
assert patched.count("[mindscape_cache] session=%s provider=%s") == 1

tree = ast.parse(patched)
runner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "ToolLoopAgentRunner")
step = next(node for node in runner.body if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "step")
usage_block = next(node for node in ast.walk(step) if isinstance(node, ast.If)
                   and isinstance(node.test, ast.Attribute)
                   and node.test.attr == "usage")
calls = []
logger = types.SimpleNamespace(
    info=lambda fmt, *args: calls.append(("info", fmt % args)),
    warning=lambda fmt, *args: calls.append(("warning", fmt % args)),
)
namespace = {"logger": logger}
isolated = ast.Module(body=[usage_block], type_ignores=[])
ast.fix_missing_locations(isolated)
code = compile(isolated, "<usage block>", "exec")
class Usage(types.SimpleNamespace):
    def __radd__(self, other):
        return self


usage = Usage(input=1200, total=1250, input_other=200,
              input_cached=1000, output=50)
conversation = types.SimpleNamespace(token_usage=0)
self = types.SimpleNamespace(
    stats=types.SimpleNamespace(token_usage=0, current_context_tokens=0),
    req=types.SimpleNamespace(session_id="test-session", conversation=conversation),
    provider=types.SimpleNamespace(provider_config={"id": "test-provider"}),
)
exec(code, namespace | {"self": self, "llm_response": types.SimpleNamespace(usage=usage)})
assert conversation.token_usage == 1250
assert self.stats.current_context_tokens == 1200
assert "miss=200 hit=1000 out=50" in calls[-1][1]
exec(code, namespace | {"self": self, "llm_response": types.SimpleNamespace(usage=None)})
assert calls[-1] == ("warning", "[mindscape_cache] session=test-session usage=missing")
print("cache usage log: OK")
