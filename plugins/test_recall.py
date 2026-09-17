# -*- coding: utf-8 -*-
"""混合检索效果对比测试"""
import importlib.util, os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__))

# 打桩 astrbot + mindscape_config
m = types.ModuleType('astrbot')
api = types.ModuleType('astrbot.api')
class _Noop:
    def __getattr__(self, k): return lambda *a, **kw: None
api.logger = _Noop()
def _llm_tool(**kw):
    return lambda f: f
api.llm_tool = _llm_tool
api.star = types.SimpleNamespace(Star=type('S', (), {}), Context=object)
m.api = api
sys.modules['astrbot'] = m
sys.modules['astrbot.api'] = api

cfg = types.ModuleType('mindscape_config')
cfg.bot_entries = lambda: []
cfg.config_path = lambda: os.path.join(HERE, 'x.yaml')
cfg.section = lambda *a, **k: {}
sys.modules['mindscape_config'] = cfg

spec = importlib.util.spec_from_file_location('recall', os.path.join(HERE, 'mindscape_recall.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

DIARY = sys.argv[1] if len(sys.argv) > 1 else ''
print('日记文件:', DIARY, '|', os.path.getsize(DIARY), 'bytes' if os.path.exists(DIARY) else 'MISSING')
print()
for kw in ['爬楼', '薯片', '猫儿猫儿', '下雨', '完全不存在的词汇xyz']:
    hits = mod.search_diary(DIARY, kw, limit=4)
    print('=== 搜「%s」-> %d 条 ===' % (kw, len(hits)))
    for h in hits:
        print('   ' + h[:100])
    print()