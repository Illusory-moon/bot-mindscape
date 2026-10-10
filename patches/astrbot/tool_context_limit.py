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
        # ⚠️ 2026-10-10 补齐 ✓：凡是**注入块的开头记号**都必须在这里 ✓ ——
        #   漏一个，那块就永远留在历史里（每轮重发 + 废缓存 ✗）。自检 R62 守着 ✓。
        markers = (
            "【本条消息的定向性】", "【本群最近的真实聊天记录",
            "【上面历史里带的那几张图", "【下面是系统给你注入的长期记忆",
            "## 你的长期记忆", "**【这一轮是你自己想开口",
            "## 你认识的人",                    # mindscape_memory（人物画像）
            "## 这一轮的消息里带了图",          # mindscape_vision（有图提醒）
            "（系统提示：正文里出现了像图片/附件的字样",   # mindscape_vision（假图标注）
            "# 沉默的权利",                     # mindscape_silence（回复轮）
            "这一轮是自主冒泡",                 # mindscape_silence（冒泡轮）
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


# 记号元组（保持与 NEW 里那份**逐字一致** ✓ —— 下面 import 时会断言，防止两处漂移 ✗）
def _markers_span(text):
    """在 text 里定位「注释 + markers 元组」那一整段 ✓ -> (起点, 终点)；找不到返回 None。

    唯一真源是 NEW 里那一份 ✓：升级只认它，别处不再抄一遍（免得两处漂移 ✗）。
    """
    head = "        markers = ("
    tail = chr(10) + "        )"
    i = text.find(head)
    if i < 0:
        return None
    j = text.find(tail, i)
    if j < 0:
        return None
    start = i
    while start > 0:
        prev_end = text.rfind(chr(10), 0, start - 1)
        prev_start = prev_end + 1
        if text[prev_start:start - 1].strip().startswith("#"):
            start = prev_start
        else:
            break
    return start, j + len(tail)


assert _markers_span(NEW) is not None, "NEW 里找不到 markers 语句 ✗"


def _upgrade_markers(source):
    """把**更早版本**的 markers 语句升级成 NEW 里那一份 ✓（线上是先上 6 记号那一版的 ✓）。

    这样 patch_source 对「已打过旧补丁的线上文件」也幂等 ✓ —— 仓库能逐字节复现线上 ✓。
    """
    want_span = _markers_span(NEW)
    got = _markers_span(source)
    if not want_span or not got:
        return source
    return source[:got[0]] + NEW[want_span[0]:want_span[1]] + source[got[1]:]


def patch_source(source):
    source = _upgrade_markers(source)
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