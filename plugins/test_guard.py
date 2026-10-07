# -*- coding: utf-8 -*-
"""mindscape_guard 自检（不依赖 bot 框架，直接测核心函数）"""
import importlib.util
import asyncio
import io
import logging
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
    class _Filter:
        def __getattr__(self, name):
            return lambda **kwargs: (lambda func: func)

    ev.filter = _Filter()
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
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    dedicated = logging.getLogger('mindscape-test-privacy')
    dedicated.addHandler(handler)
    dedicated.propagate = False
    dedicated.setLevel(logging.INFO)
    mod.pg_redact = lambda value: value.replace('A2B3C4', '[hidden]')
    mod.pg_install_log_filter()
    dedicated.info('code=%s', 'A2B3C4')
    hidden = 'A2B3C4' not in stream.getvalue() and '[hidden]' in stream.getvalue()
    print(('OK  ' if hidden else 'FAIL') + ' | dedicated logger redacts code')
    ok += int(hidden)
    dedicated.removeHandler(handler)
    plain = mod.ms_chain_text('A2B3C4') == 'A2B3C4'
    print(('OK  ' if plain else 'FAIL') + ' | direct send extracts plain text')
    ok += int(plain)
    base = types.ModuleType('astrbot.core.platform.astr_message_event')
    class Event:
        def __init__(self, group):
            self.group, self.sent = group, []
        def get_self_id(self): return 'bot'
        def get_group_id(self): return self.group
        def get_sender_id(self): return 'developer'
    class PlatformEvent(Event):
        async def send(self, message):
            self.sent.append(message)
    base.AstrMessageEvent = Event
    sys.modules['astrbot.core'] = types.ModuleType('astrbot.core')
    sys.modules['astrbot.core.platform'] = types.ModuleType('astrbot.core.platform')
    sys.modules['astrbot.core.platform.astr_message_event'] = base
    audit = []
    mod.pg_config = lambda: {'enabled': True}
    mod.pg_secret_in = lambda sid, value: 'A2B3C4' in value
    mod.pg_private = lambda event: not event.get_group_id()
    mod.pg_audit = lambda *args: audit.append(args)
    mod.ms_install_send_guard(lambda value: False)
    group, private = PlatformEvent('group'), PlatformEvent('')
    asyncio.run(group.send('A2B3C4'))
    asyncio.run(private.send('A2B3C4'))
    blocked = not group.sent and private.sent == ['A2B3C4'] and [a[0] for a in audit] == ['blocked_group', 'given']
    print(('OK  ' if blocked else 'FAIL') + ' | group blocks and private audits after send')
    ok += int(blocked)
    print()
    print('==> %d/%d passed' % (ok, len(CASES) + 3))
    return 0 if ok == len(CASES) + 3 else 1


if __name__ == '__main__':
    sys.exit(main())
