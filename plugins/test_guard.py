# -*- coding: utf-8 -*-
"""mindscape_guard 自检（不依赖 bot 框架，直接测核心函数）"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _stub_framework():
    """打桩 astrbot，让模块能 import 进来（仅用于自检）。"""
    m = types.ModuleType('astrbot')
    api = types.ModuleType('astrbot.api')
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None)

    class _Star:
        def __init__(self, *a, **k):
            pass

    api.star = types.SimpleNamespace(Star=_Star, Context=object)
    ev = types.ModuleType('astrbot.api.event')
    ev.AstrMessageEvent = object
    ev.filter = types.SimpleNamespace(on_decorating_result=lambda **k: (lambda f: f))
    m.api = api
    sys.modules['astrbot'] = m
    sys.modules['astrbot.api'] = api
    sys.modules['astrbot.api.event'] = ev


CASES = [
    ('LLM 响应错误: All chat models failed: APITimeoutError: Request timed out.', True),
    ('APIConnectionError: Connection error.', True),
    ('RateLimitError: rate limit exceeded', True),
    ('Traceback (most recent call last): File ...', True),
    ('openai.APITimeoutError: Request timed out.', True),
    ('Internal Server Error', True),
    ('今天天气不错，出去走走吧', False),
    ('你说的 error 是什么意思呀？', False),
    ('这个 500 块的套餐挺划算', False),
    ('', False),
]


def main():
    _stub_framework()
    path = os.path.join(HERE, 'mindscape_guard.py')
    spec = importlib.util.spec_from_file_location('mindscape_guard', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ok = 0
    for text, expect in CASES:
        got = mod.is_error_text(text)
        mark = 'OK  ' if got == expect else 'FAIL'
        if got == expect:
            ok += 1
        print('%s | expect %-5s got %-5s | %s' % (mark, expect, got, text[:44]))
    print()
    print('==> %d/%d passed' % (ok, len(CASES)))
    return 0 if ok == len(CASES) else 1


if __name__ == '__main__':
    sys.exit(main())
