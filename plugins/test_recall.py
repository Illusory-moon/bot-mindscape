# -*- coding: utf-8 -*-
"""混合检索效果对比测试"""
import importlib.util, os, sys, tempfile, types, unittest
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

class RecallTest(unittest.TestCase):
    def test_full_search_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [os.path.join(tmp, 'a.md'), os.path.join(tmp, 'b.md')]
            for path in paths:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('## 2026-10-01\n' + ''.join('- 示例记录 %d\n' % i for i in range(12)))
            hits, total = mod.search_diary(paths, '示例记录', full=True)
            self.assertEqual((len(hits), total), (24, 24))
            hits, total = mod.search_diary(paths, '示例记录')
            self.assertEqual((len(hits), total), (15, 24))


if __name__ == '__main__':
    diary = sys.argv[1] if len(sys.argv) > 1 else ''
    print('日记文件:', diary, '|', os.path.getsize(diary) if os.path.exists(diary) else 'MISSING')
    print()
    for kw in ['爬楼', '薯片', '猫儿猫儿', '下雨', '完全不存在的词汇xyz']:
        hits, total = mod.search_diary(diary, kw, limit=4)
        print('=== 搜「%s」-> %d/%d 条 ===' % (kw, len(hits), total))
        for h in hits:
            print('   ' + h[:100])
        print()
