# -*- coding: utf-8 -*-
"""Limit tool results in stored history and in provider context copies."""
import ast
import pathlib
import sys


OLD = '''    def _sanitize_contexts_for_provider(
        self,
        contexts: list[Message] | list[dict[str, T.Any]],
    ) -> list[Message] | list[dict[str, T.Any]]:
        modalities = self.provider.provider_config.get("modalities", None)
        if (
            not modalities
        ):  # Unconfigured (None or empty list) defaults to support all modalities
            return contexts
        sanitized_contexts, stats = sanitize_contexts_by_modalities(
            contexts,
            self.provider.provider_config.get("modalities", None),
        )
        log_context_sanitize_stats(stats)
        return sanitized_contexts

'''

NEW = '''    def _sanitize_contexts_for_provider(
        self,
        contexts: list[Message] | list[dict[str, T.Any]],
    ) -> list[Message] | list[dict[str, T.Any]]:
        modalities = self.provider.provider_config.get("modalities", None)
        if not modalities:
            sanitized_contexts = contexts
        else:
            sanitized_contexts, stats = sanitize_contexts_by_modalities(
                contexts, modalities,
            )
            log_context_sanitize_stats(stats)

        # Keep the stored conversation intact; only shorten the provider payload.
        markers = (
            "【本条消息的定向性】", "【本群最近的真实聊天记录",
            "【上面历史里带的那几张图", "【下面是系统给你注入的长期记忆",
            "## 你的长期记忆", "**【这一轮是你自己想开口",
        )
        user_indexes = [i for i, msg in enumerate(sanitized_contexts)
                        if (msg.get("role") if isinstance(msg, dict) else msg.role) == "user"]
        keep_from = user_indexes[-1] if user_indexes else 0
        deepseek = "deepseek" in str(
            self.req.model or self.provider.provider_config.get("model") or ""
        ).lower()
        limited = []
        for i, msg in enumerate(sanitized_contexts):
            role = msg.get("role") if isinstance(msg, dict) else msg.role
            content = msg.get("content") if isinstance(msg, dict) else msg.content
            new_content = content
            if role == "tool" and isinstance(content, str) and len(content) > 2000:
                new_content = content[:2000] + "\\n[tool result truncated]"
            elif i < keep_from and role == "user":
                if isinstance(content, str):
                    cut = min((pos for marker in markers
                               if (pos := content.find(marker)) >= 0), default=len(content))
                    new_content = content[:cut].rstrip() or content
                elif isinstance(content, list):
                    new_content = [part for part in content if not (
                        (part.get("type") if isinstance(part, dict) else part.type) == "text"
                        and any(str(part.get("text") if isinstance(part, dict) else part.text)
                                .lstrip().startswith(marker) for marker in markers)
                    )] or content
            elif i < keep_from and role == "assistant" and deepseek and isinstance(content, list):
                new_content = [
                    ({**part, "think": ""} if isinstance(part, dict)
                     else part.model_copy(update={"think": ""}))
                    if (part.get("type") if isinstance(part, dict) else part.type) == "think"
                    else part for part in content
                ]
            limited.append(
                ({**msg, "content": new_content} if isinstance(msg, dict)
                 else msg.model_copy(update={"content": new_content}))
                if new_content != content else msg
            )
        return limited

'''

OLD_TOOL = '''        def _append_tool_call_result(tool_call_id: str, content: str) -> None:
            tool_call_result_blocks.append(
                ToolCallMessageSegment(
                    role="tool",
                    tool_call_id=tool_call_id,
                    content=self._merge_follow_up_notice(content),
                ),
            )
'''

NEW_TOOL = '''        def _append_tool_call_result(tool_call_id: str, content: str) -> None:
            if len(content) > 1900:
                content = content[:1900] + "\\n[tool result truncated]"
            tool_call_result_blocks.append(
                ToolCallMessageSegment(
                    role="tool",
                    tool_call_id=tool_call_id,
                    content=self._merge_follow_up_notice(content),
                ),
            )
'''


def patch_source(source):
    previous = NEW.replace(
        "keep_from = user_indexes[-1] if user_indexes else 0",
        "keep_from = user_indexes[-2] if len(user_indexes) >= 2 else 0",
    )
    if previous in source:
        source = source.replace(previous, NEW)
    if NEW not in source:
        if source.count(OLD) != 1:
            raise ValueError("unsupported provider context method")
        source = source.replace(OLD, NEW)
    if NEW_TOOL not in source:
        if source.count(OLD_TOOL) != 1:
            raise ValueError("unsupported tool result method")
        source = source.replace(OLD_TOOL, NEW_TOOL)
    patched = source
    ast.parse(patched)
    return patched


if __name__ == "__main__":
    source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    pathlib.Path(sys.argv[2]).write_text(patch_source(source), encoding="utf-8")
