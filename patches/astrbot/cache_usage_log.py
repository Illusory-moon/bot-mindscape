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
                logger.info(
                    "[mindscape_cache] session=%s provider=%s miss=%d hit=%d out=%d",
                    self.req.session_id or "-",
                    self.provider.provider_config.get("id", "-"),
                    llm_response.usage.input_other,
                    llm_response.usage.input_cached,
                    llm_response.usage.output,
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
