# -*- coding: utf-8 -*-
"""memory 模块的纯函数自检（打桩框架）"""
import importlib.util, os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__))

m = types.ModuleType('astrbot')
api = types.ModuleType('astrbot.api')
class _Noop:
    def __getattr__(self, k): return lambda *a, **kw: None
api.logger = _Noop()
api.star = types.SimpleNamespace(Star=type('S', (), {}), Context=object)
m.api = api
sys.modules['astrbot'] = m
sys.modules['astrbot.api'] = api

ev = types.ModuleType('astrbot.api.event')
ev.AstrMessageEvent = object
ev.filter = types.SimpleNamespace(on_llm_request=lambda **k: (lambda f: f))
sys.modules['astrbot.api.event'] = ev

pe = types.ModuleType('astrbot.core.provider.entities')
pe.ProviderRequest = object
sys.modules['astrbot.core'] = types.ModuleType('astrbot.core')
sys.modules['astrbot.core.provider'] = types.ModuleType('astrbot.core.provider')
sys.modules['astrbot.core.provider.entities'] = pe

cfg = types.ModuleType('mindscape_config')
cfg.config_path = lambda: os.path.join(HERE, 'x.yaml')
cfg.section = lambda *a, **k: {}
cfg.bot_entries = lambda: []
sys.modules['mindscape_config'] = cfg

spec = importlib.util.spec_from_file_location('mem', os.path.join(HERE, 'mindscape_memory.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 造一个 people 文件
tmp = os.path.join(HERE, '_tmp.people.md')
open(tmp, 'w', encoding='utf-8').write(
    '# 你认识的人（自动维护）\n\n最后更新：2026-01-01 00:00\n\n'
    '- 小甲：喜欢猫，经常熬夜\n'
    '- 小乙：程序员，话少\n')
r = mod.read_people(tmp, 500)
print('read_people 结果:'); print(r)
assert '- 小甲' in r and '- 小乙' in r and '最后更新' not in r
print()
print('read_people 只保留条目行: OK')
os.remove(tmp)

# read_recent 仍工作
d = os.path.join(HERE, '_tmp.md')
open(d, 'w', encoding='utf-8').write('## 2026-01-01 10:00\n- 甲说了一件事\n\n## 2026-01-02 10:00\n- 乙说了另一件事\n')
rr = mod.read_recent(d, 2000)
print('read_recent:', rr.replace(chr(10), ' | ')[:120])
assert '乙说了另一件事' in rr
print('read_recent: OK')
os.remove(d)
print()
print('==> memory 模块自检通过')