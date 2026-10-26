# -*- coding: utf-8 -*-
"""Log provider cache usage for every agent response, including tool calls."""
import ast
import pathlib
import sys


OLD = '''            if llm_response.usage:
                # Keep cumulative usage for billing and expose the latest request
                # input separately for context-window occupancy displays.
                self.stats.token_usage += llm_response.usage
                self.stats.current_context_tokens = llm_response.usage.input
                if self.req.conversation:
                    self.req.conversation.token_usage = llm_response.usage.total
'''

NEW = '''            if llm_response.usage:
                # Keep cumulative usage for billing and expose the latest request
                # input separately for context-window occupancy displays.
                self.stats.token_usage += llm_response.usage
                self.stats.current_context_tokens = llm_response.usage.input
                if self.req.conversation:
                    self.req.conversation.token_usage = llm_response.usage.total
                # 2026-10-27 修（P2-18）：框架的 Usage 字段名随版本变过，直接取属性会在
                #   step() 里抛 AttributeError ⇒ **打断整轮响应** ✗
                #   ⇒ 一律 getattr 兜底：缺字段记 -1（账能对、话照常发）✓
                _u = llm_response.usage
                _g = lambda k: getattr(_u, k, -1)   # noqa: E731
                logger.info(
                    "[mindscape_cache] session=%s provider=%s miss=%s hit=%s out=%s",
                    self.req.session_id or "-",
                    (getattr(self.provider, "provider_config", None) or {}).get("id", "-"),
                    _g("input_other"),
                    _g("input_cached"),
                    _g("output"),
                )
            else:
                logger.warning(
                    "[mindscape_cache] session=%s usage=missing",
                    self.req.session_id or "-",
                )
'''


def patch_source(source):
    if NEW in source:
        return source
    if source.count(OLD) != 1:
        raise ValueError("unsupported agent usage block")
    patched = source.replace(OLD, NEW)
    ast.parse(patched)
    return patched


if __name__ == "__main__":
    source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    pathlib.Path(sys.argv[2]).write_text(patch_source(source), encoding="utf-8")
