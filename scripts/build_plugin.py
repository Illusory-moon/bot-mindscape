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
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(HERE, "plugins")
OUT_DIR = os.path.join(HERE, "dist", "mindscape")


def _version():
    """版本号**单一真源** = 仓库根的 `VERSION` 文件（2026-10-27 · P2-19）。

    事故形态：版本号原先硬编码在构建器里（`version: 0.1.0`），而 `publish_plugin.py`
    只 bump **市场仓**那份 ⇒ 本仓 `dist/metadata.yaml` 永远不动 ⇒ 版本号不可信、会漂 ✗
    ⇒ 现在 build 读 `VERSION` 注入；自检再断言 dist 里的版本与 `VERSION` 一致 ✓
    """
    p = os.path.join(HERE, "VERSION")
    try:
        with open(p, encoding="utf-8") as f:
            v = f.read().strip()
        return v or "0.0.0"
    except Exception:
        return "0.0.0"

# 顺序有意义：core/config 是共享层；Mixin 的先后决定 MRO
ORDER = [
    "mindscape_core", "mindscape_config", "mindscape_gate", "mindscape_groupctx",
    "mindscape_block", "mindscape_guard", "mindscape_silence", "mindscape_vision",
    "mindscape_memory", "mindscape_privacy", "mindscape_recall", "mindscape_notes",
    "mindscape_diary", "mindscape_learn", "mindscape_stickers", "mindscape_sticker_use",
    "mindscape_format", "mindscape_trace", "mindscape_rescue", "mindscape_janitor",
    "mindscape_mention", "mindscape_toolscope", "mindscape_voice", "mindscape_bubble",
]

# 这些模块里的类是 Mixin，需要被主类继承（按此顺序 = MRO）
# ⚠️ 2026-10-27（P2-16）：这份顺序**必须与 ORDER 里 Mixin 模块的出现顺序一致**，
#   否则构建会直接失败（见文件末尾的一致性校验 ✓）。原来的顺序与 ORDER 不一致
#   （groupctx 在 ORDER 第一个、却排在 MIXIN_ORDER 第 11），今天无害但随时会咬人 ✗
#   ⇒ 已按 ORDER 的顺序重排 ✓
MIXIN_ORDER = ["GroupctxMixin", "BlockMixin", "GuardMixin", "SilenceMixin", "VisionMixin",
               "MemoryMixin", "PrivacyMixin", "StickersMixin", "StickerUseMixin",
               "FormatMixin", "TraceMixin", "RescueMixin",
               "MentionMixin", "ToolscopeMixin", "VoiceMixin", "BubbleMixin"]


def _check_global_collision(sources):
    """⚠️ 合并成单文件后，**模块级同名常量会互相覆盖** ✗

    2026-10-31 真事故：新模块写了 `SECTION = "bubble"`，把 voice 模块的
    `SECTION = "voice"` 盖掉了 ⇒ 语音模块从此读的是 bubble 段 ⇒「缺 api_base / voice_id」✗
    合并产物里没有任何报错，只在运行时的日志里露出一句意思不明的话 ✗
    ⇒ 所以**在构建期拦**：同一个模块级名字出现在两个模块里就直接失败 ✓
    """
    names = {}
    pat = re.compile(r"^([A-Z][A-Z0-9_]*) = ", re.M)
    for mod, src in sources.items():
        for m in pat.finditer(src):
            names.setdefault(m.group(1), []).append(mod)
    bad = {k: v for k, v in names.items() if len(v) > 1}
    if bad:
        raise SystemExit(
            "[FAIL] 这些模块级常量在多个模块里重名，合并后会互相覆盖 ✗：\n"
            + "\n".join("  %-16s %s" % (k, ", ".join(v)) for k, v in sorted(bad.items()))
            + "\n     改法：给每个模块的常量加前缀（如 BB_SECTION / MN_TIMEOUT）✓"
        )
    return len(names)


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
    with open(path, encoding="utf-8-sig") as f:   # utf-8-sig 自动吃掉 BOM
        src = f.read()
    tree = ast.parse(src)
    src_lines = src.splitlines()
    imports, classes, others = [], [], []
    top_skipped = set()
    for node in tree.body:
        seg = _seg(src_lines, node)
        if seg is None:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # 丢掉模块之间的互相 import（合并后不存在这些模块）
            mod = getattr(node, "module", None) or ""
            if mod.startswith("mindscape_") or any(
                    a.name.startswith("mindscape_") for a in node.names):
                top_skipped.add(id(node))
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

    # 🔴 函数体 / 类体里的 mindscape_* import 一律硬错（2026-10-27 真事故）：
    #    只剥顶层 ⇒ 嵌套的那行会原样进产物 ⇒ 合并产物里没有这些模块 ⇒ ModuleNotFoundError
    #    ⇒ 被 try 吞掉 ⇒ **功能静默失效**，而自检、开发态全绿（真事：口令注入线上从未生效）
    deep = []
    for node in ast.walk(tree):
        if id(node) in top_skipped:
            continue
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mindscape_"):
            deep.append((node.lineno, "from %s import …" % node.module))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("mindscape_"):
                    deep.append((node.lineno, "import %s" % a.name))
    if deep:
        first = sorted(deep)[0]
        raise SystemExit(
            "[FAIL] %s 第 %d 行有**非顶层**的 mindscape_* import：%s\n"
            "       合并后这些模块不存在 ⇒ 该 import 会 ModuleNotFoundError ⇒ 功能静默失效 ✗\n"
            "       改法：合并产物里模块符号就在同一命名空间 ⇒ 用 globals()/sys.modules 取，别写 import。"
            % (os.path.basename(path), first[0], first[1]))
    return imports, classes, others


HEADER = ('# -*- coding: utf-8 -*-\n'
          '"""bot-mindscape · 单文件整合插件（由 scripts/build_plugin.py 生成，请勿直接编辑）\n\n'
          '源码: plugins/    重新生成: python scripts/build_plugin.py\n"""')


# 只在自建部署里带的模块：会直接改框架数据库，不适合进插件市场
LOCAL_ONLY = ["mindscape_janitor"]


def main(exclude=()):
    all_imports = {}
    mixin_src = {}
    mixin_mod = {}          # Mixin 类名 → 所在模块名（P2-16：校验两套顺序是否一致用）
    plain_classes = []
    other_src = []

    _srcs = {}
    for name in ORDER:
        _p = os.path.join(SRC, name + ".py")
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                _srcs[name] = _f.read()
    n_const = _check_global_collision(_srcs)
    print("  模块级常量：%d 个，无重名 ✓" % n_const)

    for name in ORDER:
        if name in exclude:
            print("  [跳过] " + name + "（市场版不含：会直接改框架数据库）")
            continue
        p = os.path.join(SRC, name + ".py")
        if not os.path.exists(p):
            # 2026-10-27 修（P2-15）：原来只打印「[跳过]」就继续，**退出码仍是 0**
            #   ⇒ 改名/漏文件时产物静默少一层功能，而自检的"字节一致"照样通过 ✗（它比的是
            #     同一份残缺构建）⇒ 现在直接硬错 ✓
            raise SystemExit("[FAIL] ORDER 里的模块文件不存在：%s.py —— 改名了？还是忘了提交？" % name)
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
                mixin_mod[cname] = name        # 2026-10-27（P2-16）：记下它属于哪个模块
            else:
                plain_classes.append(c)
        # 纯函数 / 常量：去掉里面的 Mixin 相关空行即可
        # 2026-10-27（P3）：每段前面插一条**来源注释** ⇒ 6 千行产物里能直接 grep 回源 ✓
        other_src.extend(["# ==== from plugins/%s.py ====" % name]
                         + [s for s in others if s.strip()])
        print("  [读取] %-28s imports=%d classes=%d funcs=%d"
              % (name, len(imports), len(classes), len(others)))

    # 组装
    mixins = [m for m in MIXIN_ORDER if m in mixin_src]
    missing = [m for m in MIXIN_ORDER if m not in mixin_src]
    if missing:
        # 2026-10-27 修（P2-15）：反方向也一样致命 —— 声明了却没找到 ⇒ 那个 Mixin 的功能
        #   整个不进基类（钩子永不注册），而产物语法完全合法 ✗ ⇒ 改成硬错 ✓
        raise SystemExit("[FAIL] MIXIN_ORDER 里的这些 Mixin 没在源码里找到：%s" % ", ".join(missing))
    extra = [c for c in mixin_src if c not in MIXIN_ORDER]
    if extra:
        # 源码有、MIXIN_ORDER 没有 ⇒ 类体进产物但不被继承 ⇒ 钩子静默失效 ✗ ⇒ 硬错 ✓
        raise SystemExit("[FAIL] 这些 Mixin 在源码里但没登记进 MIXIN_ORDER：%s"
                         "（不登记 = 类体进产物但不被继承，钩子永不注册）" % ", ".join(sorted(extra)))
    # 2026-10-27（P2-16）：两套真源的**顺序**也必须一致 ——
    #   ORDER 决定"文件读入顺序"，MIXIN_ORDER 决定"基类 MRO 顺序"；
    #   两者不一致时今天可能无害（没有同名方法），但只要有人加了同名方法，行为就取决于哪套 ✓
    order_seq = [mixin_mod[m] for m in MIXIN_ORDER if m in mixin_mod]
    read_seq = [n for n in ORDER if n in set(order_seq)]
    if order_seq != read_seq:
        raise SystemExit(
            "[FAIL] ORDER 与 MIXIN_ORDER 的**顺序**不一致 ✗\n"
            "       ORDER 里的 Mixin 顺序 : %s\n"
            "       MIXIN_ORDER 的顺序     : %s"
            % (", ".join(read_seq), ", ".join(order_seq)))

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
        "    data_dir = staticmethod(data_dir)",
        "    abs_path = staticmethod(abs_path)",
        "",
        "",
        "cfg = _MindscapeConfigNS()",
    ]

    parts = [HEADER, "", "\n".join(imp_lines), "", ""]
    parts.append("\n\n\n".join(plain_classes) if plain_classes else "")
    parts.append("\n\n\n".join(other_src))
    parts.append("\n".join(ns))          # 命名空间必须放在所有函数定义【之后】
    parts.append("\n\n\n".join(
        "# ==== from plugins/%s.py ====\n%s" % (m, mixin_src[m]) for m in mixins))
    parts.append(main_cls)
    body = "\n".join(parts)

    os.makedirs(OUT_DIR, exist_ok=True)
    # 2026-10-27（CI 抓的）：open(..., "w") 在 Windows 上会把 LF 翻成 CRLF
    #   ⇒ 同一份源码在 Windows / Linux 构建出的产物字节不同 ⇒「产物新鲜度」在 CI 必红 ✗
    #   ⇒ 显式指定 newline，让两个平台产出一模一样 ✓
    with open(os.path.join(OUT_DIR, "main.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    with open(os.path.join(OUT_DIR, "metadata.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("name: mindscape\ndesc: bot-mindscape 整合插件（认知/表达/沉浸/运维）\n"
                "author: bot-mindscape\nversion: %s\n" % _version())

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
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--market", action="store_true",
                    help="生成插件市场版：排除会直接改框架数据库的模块")
    _a = _ap.parse_args()
    sys.exit(main(exclude=tuple(LOCAL_ONLY) if _a.market else ()))
