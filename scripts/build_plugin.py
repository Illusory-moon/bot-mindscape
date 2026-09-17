# -*- coding: utf-8 -*-
"""mindscape_build —— 把模块化源码合并成【单文件、单插件类】的 AstrBot 插件

为什么要合并（实测结论）：
  1. AstrBot 把每个插件当独立包加载，插件之间不能互相 import
  2. AstrBot 的 _get_classes 只认【一个】插件类
     （名字以 plugin 结尾或叫 Main，找到第一个就 break）
     → 多文件各自一个类时，只有一个会被实例化，其余钩子会绑错实例

做法：
  各模块保留模块化源码；本脚本用 ast 拆出「导入 / 类 / 其他」三部分，
  把各类（XxxMixin）合成一个 MindscapePlugin。

用法: python scripts/build_plugin.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "plugins")
OUT_DIR = os.path.join(HERE, "dist", "mindscape")

# 顺序有意义：core/config 是共享层；Mixin 的先后决定 MRO
ORDER = [
    "mindscape_core", "mindscape_config",
    "mindscape_guard", "mindscape_memory", "mindscape_recall",
    "mindscape_diary", "mindscape_stickers", "mindscape_sticker_use",
    "mindscape_format", "mindscape_janitor",
]

# 这些模块里的类是 Mixin，需要被主类继承（按此顺序）
MIXIN_ORDER = ["GuardMixin", "MemoryMixin", "StickersMixin",
               "StickerUseMixin", "FormatMixin"]


def _seg(src_lines, node):
    """取源码片段 —— 关键：要包含装饰器行。

    ast 的 node.lineno 指向 def/class 那一行，装饰器在 decorator_list 里，
    直接 get_source_segment 会把 @llm_tool / @filter.xxx 丢掉。
    """
    start = node.lineno
    decs = getattr(node, "decorator_list", None) or []
    if decs:
        start = min(start, min(d.lineno for d in decs))
    end = getattr(node, "end_lineno", node.lineno)
    return chr(10).join(src_lines[start - 1:end])


def split_module(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    src_lines = src.splitlines()
    imports, classes, others = [], [], []
    for node in tree.body:
        seg = _seg(src_lines, node)
        if seg is None:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # 丢掉模块之间的互相 import（合并后不存在这些模块）
            mod = getattr(node, "module", None) or ""
            if mod.startswith("mindscape_") or any(
                    a.name.startswith("mindscape_") for a in node.names):
                continue
            imports.append(seg)
        elif isinstance(node, ast.ClassDef):
            classes.append(seg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            others.append(seg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            others.append(seg)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue      # 模块 docstring
        else:
            others.append(seg)
    return imports, classes, others


HEADER = ('# -*- coding: utf-8 -*-\n'
          '"""bot-mindscape · 单文件整合插件（由 scripts/build_plugin.py 生成，请勿直接编辑）\n\n'
          '源码: plugins/    重新生成: python scripts/build_plugin.py\n"""')


def main():
    all_imports = {}
    mixin_src = {}
    plain_classes = []
    other_src = []

    for name in ORDER:
        p = os.path.join(SRC, name + ".py")
        if not os.path.exists(p):
            print("  [跳过] " + name)
            continue
        imports, classes, others = split_module(p)
        for s in imports:
            all_imports[s.strip()] = s
        for c in classes:
            # 取类名
            try:
                cname = ast.parse(c).body[0].name
            except Exception:
                cname = "?"
            if cname.endswith("Mixin"):
                mixin_src[cname] = c
            else:
                plain_classes.append(c)
        # 纯函数 / 常量：去掉里面的 Mixin 相关空行即可
        other_src.extend([s for s in others if s.strip()])
        print("  [读取] %-28s imports=%d classes=%d funcs=%d"
              % (name, len(imports), len(classes), len(others)))

    # 组装
    mixins = [m for m in MIXIN_ORDER if m in mixin_src]
    missing = [m for m in MIXIN_ORDER if m not in mixin_src]
    if missing:
        print("  [警告] 缺少 Mixin: " + ", ".join(missing))

    setups = "\n".join("        %s.setup(self, context)" % m for m in mixins)
    main_head = [
        "# " + "=" * 66,
        "# 插件入口：把所有 Mixin 的钩子收进同一个类",
        "# " + "=" * 66,
        "class MindscapePlugin(%s, star.Star):" % ", ".join(mixins),
        "    def __init__(self, context):",
        "        self.context = context",
        "        self.name = \"mindscape\"",
        "        self.author = \"bot-mindscape\"",
    ]
    if setups:
        main_head.extend(setups.split(chr(10)))
    else:
        main_head.append("        pass")
    main_head.append("        logger.info(\"[mindscape] 插件已加载（" + str(len(mixins)) + " 个模块）\")")
    main_cls = chr(10).join(main_head)
    imp_lines = sorted(set(all_imports.values()),
                       key=lambda x: (0 if x.strip().startswith("import ") else 1, x))
    # 跨模块引用的「命名空间对象」：源码里 import mindscape_config as cfg，
    # 合并后这些模块不存在了，所以造一个同名的命名空间让 cfg.xxx() 照常工作。
    ns = [
        "# " + "=" * 66,
        "# 命名空间：让 cfg.xxx() / core.xxx() 这类调用在合并后依然可用",
        "# " + "=" * 66,
        "class _MindscapeConfigNS:",
        "    \"\"\"config 模块的函数集合（合并后替代 import mindscape_config as cfg）。\"\"\"",
        "    section = staticmethod(section)",
        "    load = staticmethod(load)",
        "    bot_entries = staticmethod(bot_entries)",
        "    config_path = staticmethod(config_path)",
        "    abs_path = staticmethod(abs_path)",
        "",
        "",
        "cfg = _MindscapeConfigNS()",
    ]

    parts = [HEADER, "", "\n".join(imp_lines), "", ""]
    parts.append("\n\n\n".join(plain_classes) if plain_classes else "")
    parts.append("\n\n\n".join(other_src))
    parts.append("\n".join(ns))          # 命名空间必须放在所有函数定义【之后】
    parts.append("\n\n\n".join(mixin_src[m] for m in mixins))
    parts.append(main_cls)
    body = "\n".join(parts)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "main.py"), "w", encoding="utf-8") as f:
        f.write(body)
    with open(os.path.join(OUT_DIR, "metadata.yaml"), "w", encoding="utf-8") as f:
        f.write("name: mindscape\ndesc: bot-mindscape 整合插件（认知/表达/沉浸/运维）\n"
                "author: bot-mindscape\nversion: 0.1.0\n")

    # 语法自检
    try:
        compile(body, "main.py", "exec")
        print()
        print("语法检查: OK")
    except SyntaxError as e:
        print()
        print("语法检查: FAIL  line %s: %s" % (e.lineno, e.msg))
        return 1
    print("输出: %s (%d 行)" % (os.path.join(OUT_DIR, "main.py"), len(body.splitlines())))
    return 0


if __name__ == "__main__":
    sys.exit(main())