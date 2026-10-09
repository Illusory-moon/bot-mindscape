# -*- coding: utf-8 -*-
"""Exercise both context and stored-tool limits against real AstrBot source."""
import ast
import importlib.util
import pathlib
import sys
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "tool_context_limit", ROOT / "patches" / "astrbot" / "tool_context_limit.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
patched = patcher.patch_source(source)
assert patcher.patch_source(patched) == patched
tree = ast.parse(patched)
runner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "ToolLoopAgentRunner")
method = next(node for node in runner.body if isinstance(node, ast.FunctionDef)
              and node.name == "_sanitize_contexts_for_provider")


class Message:
    def __init__(self, role, content, tool_call_id=None):
        self.role, self.content, self.tool_call_id = role, content, tool_call_id

    def model_copy(self, update):
        return Message(self.role, update["content"], self.tool_call_id)


namespace = {
    "T": types.SimpleNamespace(Any=object),
    "Message": Message,
    "sanitize_contexts_by_modalities": lambda items, _: (items, None),
    "log_context_sanitize_stats": lambda _: None,
}
isolated = ast.Module(body=[ast.ClassDef(name="Runner", bases=[], keywords=[],
                                         body=[method], decorator_list=[])], type_ignores=[])
ast.fix_missing_locations(isolated)
exec(compile(isolated, "<patched runner>", "exec"), namespace)
runner_instance = namespace["Runner"]()
runner_instance.provider = types.SimpleNamespace(provider_config={})
runner_instance.req = types.SimpleNamespace(model="deepseek-v4-flash")
large = Message("tool", "x" * 4000, "call-1")
short = Message("tool", "done", "call-2")
other = Message("user", "y" * 4000)
result = runner_instance._sanitize_contexts_for_provider([large, short, other])
assert len(result[0].content) < 2100 and result[0].tool_call_id == "call-1"
assert large.content == "x" * 4000
assert result[1] is short and result[2] is other
as_dict = {"role": "tool", "content": "z" * 4000, "tool_call_id": "call-3"}
dict_result = runner_instance._sanitize_contexts_for_provider([as_dict])[0]
assert len(dict_result["content"]) < 2100 and as_dict["content"] == "z" * 4000
assert dict_result["tool_call_id"] == "call-3"
runner_instance.provider.provider_config["modalities"] = ["text"]
assert len(runner_instance._sanitize_contexts_for_provider([large])[0].content) < 2100
old_user = {"role": "user", "content": [
    {"type": "text", "text": "original question"},
    {"type": "text", "text": "【本条消息的定向性】stale injection"},
]}
old_assistant = {"role": "assistant", "content": [
    {"type": "think", "think": "private reasoning"},
    {"type": "text", "text": "answer"},
]}
recent_user = {"role": "user", "content": [
    {"type": "text", "text": "recent question"},
    {"type": "text", "text": "【本条消息的定向性】stale injection"},
]}
current_user = {"role": "user", "content": "current question"}
history = [old_user, old_assistant, large, recent_user, current_user]
trimmed = runner_instance._sanitize_contexts_for_provider(history)
assert trimmed[0]["content"] == [{"type": "text", "text": "original question"}]
assert trimmed[1]["content"][0]["think"] == ""
assert old_assistant["content"][0]["think"] == "private reasoning"
assert trimmed[2].tool_call_id == "call-1" and len(trimmed[2].content) < 2100
assert trimmed[3]["content"] == [{"type": "text", "text": "recent question"}]
assert recent_user["content"][1]["text"].endswith("stale injection")
assert trimmed[4] is current_user
runner_instance.req.model = "other-model"
assert runner_instance._sanitize_contexts_for_provider(history)[1] is old_assistant

handle = next(node for node in runner.body if isinstance(node, ast.AsyncFunctionDef)
              and node.name == "_handle_function_tools")
append = next(node for node in handle.body if isinstance(node, ast.FunctionDef)
              and node.name == "_append_tool_call_result")
tool_blocks = []
tool_namespace = {
    "tool_call_result_blocks": tool_blocks,
    "ToolCallMessageSegment": lambda **kwargs: types.SimpleNamespace(**kwargs),
    "self": types.SimpleNamespace(_merge_follow_up_notice=lambda content: content),
}
isolated_tool = ast.Module(body=[append], type_ignores=[])
ast.fix_missing_locations(isolated_tool)
exec(compile(isolated_tool, "<patched tool result>", "exec"), tool_namespace)
tool_namespace["_append_tool_call_result"]("call-4", "a" * 4000)
tool_namespace["_append_tool_call_result"]("call-5", "short")
assert tool_blocks[0].tool_call_id == "call-4"
assert tool_blocks[0].content.startswith("a" * 1900)
assert len(tool_blocks[0].content) < 2000
assert tool_blocks[1].content == "short"
print("tool result and provider context limits: OK")
