# -*- coding: utf-8 -*-
"""bot-mindscape 全套自检

覆盖：
  1. 所有 .py 语法编译
  2. 配置示例 YAML 可解析 + 关键字段齐全
  3. 各模块核心纯函数行为（打桩框架）
  4. 脱敏扫描（无真实信息泄漏）
  5. 仓库结构完整性

用法：python scripts/run_selfcheck.py
"""
import importlib.util
import os
import re
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(HERE, "plugins")
PASS, FAIL = [], []


def ok(name, detail=""):
    PASS.append(name)
    print("  [OK]   %s %s" % (name, detail))


def bad(name, detail=""):
    FAIL.append(name)
    print("  [FAIL] %s %s" % (name, detail))


def section(title):
    print()
    print("== " + title + " " + "=" * max(0, 50 - len(title)))


# ── 1. 语法 ──
def check_syntax():
    section("1. 语法编译")
    pyfiles = []
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            if n.endswith(".py"):
                pyfiles.append(os.path.join(root, n))
    for f in pyfiles:
        rel = os.path.relpath(f, HERE)
        r = subprocess.run([sys.executable, "-m", "py_compile", f],
                           capture_output=True, text=True)
        if r.returncode == 0:
            ok("编译", rel)
        else:
            bad("编译", rel + " | " + (r.stderr or "")[:120])


# ── 2. 配置 ──
def check_config():
    section("2. 配置示例")
    try:
        import yaml
    except ImportError:
        bad("PyYAML", "未安装，跳过配置检查")
        return
    p = os.path.join(HERE, "config", "config.example.yaml")
    if not os.path.exists(p):
        bad("配置文件", "不存在")
        return
    try:
        d = yaml.safe_load(open(p, encoding="utf-8"))
    except Exception as e:
        bad("YAML 解析", str(e)[:150])
        return
    ok("YAML 解析", "顶层键: " + ", ".join(d.keys()))
    for key in ["memory", "diary", "stickers", "guard", "format", "janitor", "waking"]:
        if key in d:
            ok("配置段", key)
        else:
            bad("配置段缺失", key)


# ── 3. 核心函数 ──
def stub():
    m = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Noop:
        def __getattr__(self, k):
            return lambda *a, **kw: None

    api.logger = _Noop()
    api.llm_tool = lambda **k: (lambda f: f)
    api.star = types.SimpleNamespace(Star=type("S", (), {}), Context=object)
    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = object
    ev.filter = types.SimpleNamespace(
        on_llm_request=lambda **k: (lambda f: f),
        on_decorating_result=lambda **k: (lambda f: f),
        event_message_type=lambda *a, **k: (lambda f: f),
    )
    pe = types.ModuleType("astrbot.core.provider.entities")
    pe.ProviderRequest = object
    mc = types.ModuleType("astrbot.core.message.components")
    mc.Image = type("Image", (), {})
    mer = types.ModuleType("astrbot.core.message.message_event_result")
    mer.MessageEventResult = type("M", (), {"__init__": lambda s: None})
    emt = types.ModuleType("astrbot.core.star.filter.event_message_type")
    emt.EventMessageType = types.SimpleNamespace(
        ALL=1, GROUP_MESSAGE=2, PRIVATE_MESSAGE=4)
    for n, mod in [("astrbot", m), ("astrbot.api", api), ("astrbot.api.event", ev),
                   ("astrbot.core", types.ModuleType("astrbot.core")),
                   ("astrbot.core.provider", types.ModuleType("astrbot.core.provider")),
                   ("astrbot.core.provider.entities", pe),
                   ("astrbot.core.message", types.ModuleType("astrbot.core.message")),
                   ("astrbot.core.message.components", mc),
                   ("astrbot.core.message.message_event_result", mer),
                   ("astrbot.core.star", types.ModuleType("astrbot.core.star")),
                   ("astrbot.core.star.filter", types.ModuleType("astrbot.core.star.filter")),
                   ("astrbot.core.star.filter.event_message_type", emt)]:
        sys.modules[n] = mod
    m.api = api


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PLUGINS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_functions():
    section("3. 核心函数行为")
    stub()
    sys.path.insert(0, PLUGINS)
    cfg = types.ModuleType("mindscape_config")
    cfg.config_path = lambda: os.path.join(HERE, "config", "nonexistent.yaml")
    cfg.section = lambda *a, **k: {}
    cfg.bot_entries = lambda: []
    cfg.load = lambda *a, **k: {}
    sys.modules["mindscape_config"] = cfg

    # guard
    try:
        g = load("mindscape_guard")
        cases = [("LLM 响应错误: APITimeoutError", True), ("APITimeoutError: timed out", True),
                 ("今天天气不错", False), ("你说的 error 是啥", False)]
        good = all(g.is_error_text(t) == e for t, e in cases)
        ok("guard.is_error_text", "4/4") if good else bad("guard.is_error_text")
    except Exception as e:
        bad("guard 加载", str(e)[:100])

    # memory
    try:
        m = load("mindscape_memory")
        tmp = os.path.join(HERE, "_selfcheck_mem.md")
        open(tmp, "w", encoding="utf-8").write("## T1\n- A事件\n\n## T2\n- B事件\n")
        r = m.read_recent(tmp, 2000)
        (ok if "B事件" in r else bad)("memory.read_recent")
        pt = os.path.join(HERE, "_selfcheck_people.md")
        open(pt, "w", encoding="utf-8").write("# t\n\n最后更新：x\n\n- 甲：描述\n")
        pr = m.read_people(pt, 500)
        (ok if ("- 甲" in pr and "最后更新" not in pr) else bad)("memory.read_people")
        os.remove(tmp); os.remove(pt)
    except Exception as e:
        bad("memory 加载", str(e)[:100])

    # recall
    try:
        rc = load("mindscape_recall")
        ex = "爬楼", "爬到对面6楼", "完全无关xyz"
        s1 = rc.score_line(ex[1], ex[0])
        s2 = rc.score_line(ex[1], ex[2])
        (ok if (s1 > 0 and s2 == 0) else bad)("recall.score_line",
            "匹配=%.1f 无关=%.1f" % (s1, s2))
    except Exception as e:
        bad("recall 加载", str(e)[:100])

    # janitor
    try:
        import sqlite3
        jn = load("mindscape_janitor")
        db = os.path.join(HERE, "_selfcheck.db")
        if os.path.exists(db):
            os.remove(db)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE conversations (content TEXT)")
        con.execute("INSERT INTO conversations VALUES (?)", ("x" * 100,))
        con.execute("INSERT INTO conversations VALUES (?)", ("data:image/png;base64,AAAA",))
        con.commit(); con.close()
        n_img, n_big, b, a = jn.clean(db, "conversations", "content", 2.0)
        (ok if n_img == 1 else bad)("janitor.clean", "删图片会话=%d" % n_img)
        os.remove(db)
    except Exception as e:
        bad("janitor 加载", str(e)[:100])

    # format
    try:
        fm = load("mindscape_format")
        r = fm.flatten("第一段\n\n第二段")
        (ok if ("\n" not in r and "第一段" in r and "第二段" in r) else bad)("format.flatten", repr(r)[:40])
    except Exception as e:
        bad("format 加载", str(e)[:100])

    # stickers/use 只验证可加载
    for nm in ["mindscape_stickers", "mindscape_sticker_use", "mindscape_diary"]:
        try:
            mod = load(nm)
            ok("加载", nm)
        except Exception as e:
            bad("加载 " + nm, str(e)[:100])


# ── 4. 脱敏 ──
def _private_names():
    """只在本地有效的敏感词（自己的昵称、真名、群号、bot 号、群名……）。

    刻意**不写进源码**：这份清单本身也会被公开，明文列出来等于把要藏的东西
    印在封面上 —— 而且扫描器还得为了「不扫自己」开一个例外，越描越黑。
    改成读 scripts/private-names.txt（一行一个，# 开头是注释，已被 .gitignore
    忽略）；仓库里只留 .example 模板。文件不存在时这一项自动跳过。
    """
    words = []
    try:
        with open(os.path.join(HERE, "scripts", "private-names.txt"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line)
    except Exception:
        pass
    return words


def check_privacy():
    section("4. 脱敏扫描")
    pats = _private_names()
    SKIP = {"run_selfcheck.py", "private-names.txt", "private-names.example.txt"}
    leaked = []
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for n in names:
            if not n.endswith((".py", ".md", ".yaml", ".json", ".txt")):
                continue
            if n in SKIP:
                continue
            fp = os.path.join(root, n)
            try:
                txt = open(fp, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for pt in pats:
                if pt in txt:
                    # 致谢章节里出现项目名是合理的（但那些不是上面的词）
                    leaked.append(os.path.relpath(fp, HERE) + " -> " + pt)
            if re.search(r"sk-[A-Za-z0-9]{20,}", txt):
                leaked.append(os.path.relpath(fp, HERE) + " -> APIKEY")
    if leaked:
        for x in leaked:
            bad("泄漏", x)
    else:
        ok("脱敏", "未发现敏感信息（本地词表 %d 条）" % len(pats))


# ── 5. 结构 ──
def check_structure():
    section("5. 仓库结构")
    need = ["README.md", "LICENSE", ".gitignore", "requirements.txt",
            "config/config.example.yaml",
            "docs/architecture.md", "docs/why.md", "docs/deploy.md",
            "plugins/mindscape_config.py", "plugins/mindscape_guard.py",
            "plugins/mindscape_memory.py", "plugins/mindscape_recall.py",
            "plugins/mindscape_diary.py", "plugins/mindscape_stickers.py",
            "plugins/mindscape_sticker_use.py", "plugins/mindscape_format.py",
            "plugins/mindscape_janitor.py", "plugins/mindscape_digest.py",
            "plugins/mindscape_notes.py",
            "scripts/config_gui.py", "scripts/web_ui.py",
            "scripts/import_stickers.py", "scripts/mindscape_forget.py",
            "patches/astrbot/install.py", "patches/astrbot/README.md"]
    for rel in need:
        p = os.path.join(HERE, rel.replace("/", os.sep))
        if os.path.exists(p):
            ok("存在", rel)
        else:
            bad("缺失", rel)



def check_regressions():
    section("6. 回归断言（针对历史审查发现的缺陷）")
    stub()
    sys.path.insert(0, PLUGINS)
    import asyncio
    import importlib
    import sqlite3

    cfgmod = types.ModuleType("mindscape_config")
    cfgmod.config_path = lambda: os.path.join(HERE, "_sc_cfg.yaml")
    cfgmod.section = lambda *a, **k: {}
    cfgmod.bot_entries = lambda: []
    cfgmod.load = lambda *a, **k: {}
    sys.modules["mindscape_config"] = cfgmod

    # R03: 记忆注入链路的关键点（纯函数级）
    # 说明：模块化源码里各类已改名为 *Mixin，插件类由 build_plugin.py 合成，
    # 所以这里验证「配置解析 + read_recent + 类型校验」这三段，而不是实例化插件。
    try:
        import mindscape_memory as MM
        importlib.reload(MM)
        diary = os.path.join(HERE, "_sc_diary.md")
        with open(diary, "w", encoding="utf-8") as f:
            f.write("## 2026-01-01" + chr(10) + "- 测试条目" + chr(10) * 2)
            for i in range(40):
                f.write("- 测试条目 %d" % i + chr(10))
        mem = MM.read_recent(diary, 2500)
        has_cfg_attr = hasattr(MM, "DEFAULT_MAX_CHARS") and hasattr(MM, "DEFAULT_PEOPLE_CHARS")
        good = len(mem) > 50 and has_cfg_attr
        (ok if good else bad)("R03 记忆链路（read_recent + 常量）",
                              "%d 字" % len(mem))
        os.remove(diary)
    except Exception as e:
        bad("R03 记忆链路", str(e)[:140])

    # R10: 分类隔离用的属性名必须各自独立（Mixin 合并后不能互相覆盖）
    try:
        src = open(os.path.join(PLUGINS, "mindscape_stickers.py"), encoding="utf-8").read()
        src2 = open(os.path.join(PLUGINS, "mindscape_sticker_use.py"), encoding="utf-8").read()
        src3 = open(os.path.join(PLUGINS, "mindscape_format.py"), encoding="utf-8").read()
        # 三个模块的配置属性名前缀必须不同
        a = "self.s_c" in src
        b = "self.u_c" in src2
        c = "self.f_c" in src3
        (ok if (a and b and c) else bad)("B06 Mixin 属性隔离",
            "stickers=%s use=%s format=%s" % (a, b, c))
    except Exception as e:
        bad("B06 Mixin 属性隔离", str(e)[:140])

    # R09: read_recent 必须是硬上限
    try:
        import mindscape_memory as MM2
        importlib.reload(MM2)
        f = os.path.join(HERE, "_sc_long.md")
        with open(f, "w", encoding="utf-8") as fp:
            for i in range(10):
                fp.write("## T%d" % i + chr(10) + "- " + "内容" * 30 + chr(10) * 2)
        r = MM2.read_recent(f, 100)
        (ok if len(r) <= 100 else bad)("R09 滑动窗口是硬上限", "实际 %d 字（上限 100）" % len(r))
        os.remove(f)
    except Exception as e:
        bad("R09 滑动窗口", str(e)[:140])

    # R11: 单个块超过预算时不能返回空。实测：一份 2385 字的「成长记录」整段
    #      就是一个块，配上 2000 字预算会整块丢弃 → 返回 0 字，bot 表现为
    #      「完全不记得任何人」（这就是 bot 忘了某个人的机制性原因）。
    try:
        import mindscape_memory as MM3
        importlib.reload(MM3)
        f = os.path.join(HERE, "_sc_bigblock.md")
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("## 我的成长记录" + chr(10))
            for i in range(60):
                fp.write("- 第 %d 条：关键词在此" % i + chr(10))
        r = MM3.read_recent(f, 300)
        good = bool(r) and ("关键词" in r) and len(r) <= 300
        (ok if good else bad)("R11 超大块不丢记忆",
                              "%d 字，含关键词=%s" % (len(r), "关键词" in r))
        os.remove(f)
    except Exception as e:
        bad("R11 超大块不丢记忆", str(e)[:140])

    # R12: 空回复救援必须挂在 on_llm_response 上。挂在 on_decorating_result 会
    #      永远不触发 —— result_decorate/stage.py 开头就是
    #      `if result is None or not result.chain: return`，而空回复恰恰没有 chain。
    try:
        src = open(os.path.join(PLUGINS, "mindscape_rescue.py"), encoding="utf-8").read()
        code = src.split('"""', 2)[-1]          # 去掉模块 docstring，只查真正的代码
        has_right = "on_llm_response" in code
        has_wrong = "on_decorating_result" in code
        (ok if (has_right and not has_wrong) else bad)(
            "R12 救援挂在正确的钩子上",
            "on_llm_response=%s on_decorating_result=%s" % (has_right, has_wrong))
    except Exception as e:
        bad("R12 救援挂点", str(e)[:140])

    # R13: 人物画像必须让「关系与称呼」这类权威条目压过自动摘要。
    #      实测：某位重要的人被自动摘要写成「群友，常发图」，而人格档案里她明明是
    #      「Alice（小爱）→ 重要的人」。关系是作者写死的，不该交给摘要模型猜。
    try:
        import mindscape_diary as MD
        importlib.reload(MD)
        f = os.path.join(HERE, "_sc_people.md")
        # 先放一个「旧格式」文件，确认迁移不会把已有条目整批冲掉
        with open(f, "w", encoding="utf-8") as fp:
            # 旧格式：没有分段标题，且已经有一条被降级的「小爱：群友」
            fp.write("# 你认识的人（自动维护）" + chr(10) * 2
                     + "最后更新：2026-01-01 00:00" + chr(10) * 2
                     + "- 老条目：迁移前就存在" + chr(10)
                     + "- 小爱：群友，常发图" + chr(10))
        rel = ["- Alice（小爱）→ 重要的人，认真对待"]
        MD._update_people(f, {"小爱": "群友，常发图", "新群友": "刚进群"},
                          "2026-01-02 00:00", rel)
        got = open(f, encoding="utf-8").read()
        kept_old = "老条目" in got
        pinned = "重要的人" in got
        not_downgraded = ("群友，常发图" not in got) and ("群友，发图" not in got)
        added_new = "新群友" in got
        (ok if (kept_old and pinned and not_downgraded and added_new) else bad)(
            "R13 权威关系不被摘要覆盖",
            "旧条目=%s 关系=%s 未降级=%s 新条目=%s"
            % (kept_old, pinned, not_downgraded, added_new))
        os.remove(f)
    except Exception as e:
        bad("R13 权威关系", str(e)[:140])

    # R14: load_relations 只能取指定段落的 - 行，不能把整份人格档案倒进来
    try:
        import mindscape_diary as MD2
        importlib.reload(MD2)
        f = os.path.join(HERE, "_sc_soul.md")
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("# 人格" + chr(10)
                     + "## 关系与称呼" + chr(10)
                     + "- Alice（小爱）→ 重要的人" + chr(10)
                     + "这段普通文字不该被取" + chr(10)
                     + "## 别的段落" + chr(10)
                     + "- 这段也不该被取" + chr(10))
        rows = MD2.load_relations({"file": f, "section": "关系与称呼"})
        right = (len(rows) == 1 and "Alice" in rows[0])
        (ok if right else bad)("R14 relations 只取指定段落", "%d 行: %s" % (len(rows), rows))
        os.remove(f)
    except Exception as e:
        bad("R14 relations 段落提取", str(e)[:140])

    # R15: 每日摘要层必须闭环 —— 日记按天切分 -> 写摘要 -> 读回。
    #      同一天出现多个 ## 标题（追历史时会这样）要合并，不能当成两天。
    try:
        import mindscape_digest as DG
        importlib.reload(DG)
        f = os.path.join(HERE, "_sc_diary2.md")
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("## 2026-01-01 10:00" + chr(10) + "- 甲" + chr(10) + "- 乙" + chr(10)
                     + "## 2026-01-01 11:00" + chr(10) + "- 丙" + chr(10)
                     + "## 2026-01-02 09:00" + chr(10) + "- 丁" + chr(10))
        days = DG.parse_days(open(f, encoding="utf-8").read())
        merged = len(days.get("2026-01-01", [])) == 3
        p = os.path.join(HERE, "_sc_digest.md")
        DG.save_digests(p, "bot-name",
                        {"2026-01-01": "那天发生了甲和乙。", "2026-01-02": "第二天是丁。"})
        back = DG.load_digests(p)
        rt = (back.get("2026-01-01") == "那天发生了甲和乙。" and len(back) == 2)
        sampled = DG.sample_lines(["- %d" % i for i in range(200)], 100)
        samp = 0 < len(sampled) < 200
        (ok if (merged and rt and samp) else bad)(
            "R15 每日摘要闭环",
            "同日合并=%s 读写一致=%s 均匀抽样=%s" % (merged, rt, samp))
        for x in (f, p):
            if os.path.exists(x):
                os.remove(x)
    except Exception as e:
        bad("R15 每日摘要闭环", str(e)[:140])

    # R16: 模块级函数不能跨模块重名。构建脚本是按顺序把各模块源码拼起来，
    #      重名的只有最后一个生效 —— 曾经 _abs 有 4 份、实现各不相同，
    #      于是三个模块的路径解析被悄悄换成了另一个模块的（生产环境全用绝对
    #      路径才没爆，一用相对路径就会踩）。
    try:
        import ast as _ast
        seen, dup = {}, []
        pdir = os.path.join(HERE, "plugins")
        for f in sorted(os.listdir(pdir)):
            if not (f.startswith("mindscape_") and f.endswith(".py")):
                continue
            src = open(os.path.join(pdir, f), encoding="utf-8-sig").read()
            for n in _ast.parse(src).body:
                if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    if n.name in seen:
                        dup.append("%s(%s+%s)" % (n.name, seen[n.name][:-3], f[:-3]))
                    else:
                        seen[n.name] = f
        (ok if not dup else bad)("R16 模块级函数不重名",
                                 " / ".join(dup) or "%d 个唯一名" % len(seen))
    except Exception as e:
        bad("R16 模块级函数不重名", str(e)[:140])

    # R17: 每个模块用到的全局名必须能在本模块里找到（定义或 import）。
    #      曾经 mindscape_stickers.py 用了 _abs 却没定义 —— 在合并后的单文件里
    #      它一直蹭别的模块的同名函数，直到把重名清掉才暴露成 NameError
    #      （整个插件加载失败）。这种「跨模块搭便车」编译期查不出来，只能看符号表。
    try:
        import symtable
        import builtins as _bi
        pdir = os.path.join(HERE, "plugins")
        miss = []
        for f in sorted(os.listdir(pdir)):
            if not (f.startswith("mindscape_") and f.endswith(".py")):
                continue
            src = open(os.path.join(pdir, f), encoding="utf-8-sig").read()
            top = symtable.symtable(src, f, "exec")
            defined = {s.get_name() for s in top.get_symbols()}
            used = set()

            def _walk(t):
                for s in t.get_symbols():
                    if s.is_global() and not s.is_assigned():
                        used.add(s.get_name())
                for ch in t.get_children():
                    _walk(ch)

            _walk(top)
            unknown = sorted(n for n in used
                             if n not in defined
                             and not hasattr(_bi, n)
                             and not n.startswith("__"))
            if unknown:
                miss.append("%s:%s" % (f[10:-3], ",".join(unknown)))
        (ok if not miss else bad)("R17 模块不蹭别人的全局名",
                                 " / ".join(miss) or "12 个模块全部自洽")
    except Exception as e:
        bad("R17 模块全局名自洽", str(e)[:140])

    # R18: 检索必须「知道自己只看了多少」。
    #      实测：bot 只在最近窗口里翻到一个就下了结论，日记里其实记着好几回 ——
    #      它把「上下文里有的」当成了「全部」。
    #      修法两半：(1) 注入的记忆块写明「这只是最近一部分」+ 何时必须先查；
    #      (2) 检索返回真实命中总数，并给 full 模式供数数用。
    try:
        import mindscape_recall as RC
        importlib.reload(RC)
        f = os.path.join(HERE, "_sc_recall.md")
        with open(f, "w", encoding="utf-8") as fp:
            for i in range(40):
                fp.write("## 2026-01-%02d" % (i % 28 + 1) + chr(10)
                         + "- 第 %d 条示例记录" % i + chr(10))
        hits, total = RC.search_diary(f, "示例记录")
        capped = (len(hits) == RC.DEFAULT_LIMIT and total == 40)
        allhits, total2 = RC.search_diary(f, "示例记录", full=True)
        full_ok = (len(allhits) == 40 and total2 == 40)
        # 提问用的词常常不是记日记用的词：允许给一组近义词，命中任意一个都算
        syn = RC.split_terms("示例记录 近义词甲,近义词乙")
        multi, mtotal = RC.search_diary(f, "完全对不上的词 示例记录")
        multi_ok = (syn == ["示例记录", "近义词甲", "近义词乙"] and mtotal == 40)
        # 结果文案不能谎报「全部」—— full 模式被 FULL_CAP 截断时也必须说清。
        # 曾经这里写「全部 87 条」而只列了 80 条，等于自己又犯了「把看到的
        # 当成全部」这个毛病。
        t_full_trunc = RC.format_hits("x", ["- a"] * 80, 87, True)
        t_full_all = RC.format_hits("x", ["- a"] * 40, 40, True)
        t_part = RC.format_hits("x", ["- a"] * 15, 41, False)
        honest = ("全部" not in t_full_trunc.split(chr(10))[0]
                  and "还有 7 条没列出来" in t_full_trunc
                  and "已全部列出" in t_full_all
                  # 命中条数不等于个数：同一对会在多天被反复记到，得会归并
                  and "命中条数不等于个数" in t_full_all
                  and "命中条数不等于个数" in t_full_trunc
                  and "不是全部" in t_part and "full=true" in t_part)
        mem_src = open(os.path.join(PLUGINS, "mindscape_memory.py"),
                       encoding="utf-8").read()
        told = ("不是你的全部记忆" in mem_src and "先查再答" in mem_src)
        (ok if (capped and full_ok and told and multi_ok and honest) else bad)(
            "R18 检索知道自己的边界",
            "总数=%d 默认返回=%d full返回=%d 多词=%s 文案不谎报=%s 注入有提醒=%s"
            % (total, len(hits), len(allhits), multi_ok, honest, told))
        os.remove(f)
    except Exception as e:
        bad("R18 检索知道自己的边界", str(e)[:140])

    # R19: 「别人的话 ≠ 事实」必须写进提示词，而且不能逼它改说话方式。
    #      实测：日记里记着「某人说……」，bot 开口就变成「本本上写的是……」，
    #      把别人的一句口嗨升格成了自己笔记本里的权威事实，然后拿去当规矩执法。
    #      同时要写明「不用原样复述、用你自己的方式讲」，否则它会为了标注来源
    #      变成复读机，反而把说话风格改掉了。
    try:
        mem2 = open(os.path.join(PLUGINS, "mindscape_memory.py"), encoding="utf-8").read()
        rec2 = open(os.path.join(PLUGINS, "mindscape_recall.py"), encoding="utf-8").read()
        mem_ok = ("别人说过的，不等于事实" in mem2 and "不用原样复述" in mem2)
        rec_ok = ("他讲过这句话" in rec2 and "不用原样复述" in rec2)
        (ok if (mem_ok and rec_ok) else bad)(
            "R19 转述不等于事实", "注入块=%s 检索结果=%s" % (mem_ok, rec_ok))
    except Exception as e:
        bad("R19 转述不等于事实", str(e)[:140])

    # R20: 账本必须「可写 + 读得回来 + 同名覆盖」，而且要真的进注入。
    #      病根：日记/摘要都是后台生成的、检索只读 —— bot 能承诺却没地方落笔，
    #      于是同一件细节问几次能答出几个样（实测同一个问题六轮六个答案）。
    try:
        import mindscape_notes as NT
        importlib.reload(NT)
        t = NT.upsert_note("", "甲", "乙来挂的")
        t = NT.upsert_note(t, "丙", "丁自己认的")
        t = NT.upsert_note(t, "甲", "乙来挂的（后来补挂）")   # 同名要就地覆盖
        rows = NT.parse_notes(t)
        upsert_ok = (len(rows) == 2 and rows[0][0] == "丙"
                     and rows[1][1] == "乙来挂的（后来补挂）")
        f = os.path.join(HERE, "_sc_notes.md")
        NT.write_notes(f, t)
        with open(f, encoding="utf-8") as fp:
            rt = (NT.parse_notes(fp.read()) == rows)
        mem_src = open(os.path.join(PLUGINS, "mindscape_memory.py"),
                       encoding="utf-8").read()
        injected = ("SECTION_NOTES" in mem_src and "SECTION_RULES" in mem_src
                    and 'bot.get("notes")' in mem_src and 'bot.get("rules")' in mem_src)
        (ok if (upsert_ok and rt and injected) else bad)(
            "R20 账本可写可读且已注入",
            "同名覆盖=%s 读写一致=%s 注入=%s" % (upsert_ok, rt, injected))
        os.remove(f)
    except Exception as e:
        bad("R20 账本", str(e)[:140])

    # R21: 采集入库必须有「分辨率闸门」。
    #      实测：视觉模型把 1920×1200 的原神剧情截图判成了「二次元、可爱」，
    #      直接进了图库。**体积不是判据**（大 GIF 往往正是最合适的那张），
    #      **分辨率才是**；而且读文件头是零成本，能挡在视觉判定之前、省一次 API。
    try:
        import struct as _st
        import mindscape_stickers as MS
        importlib.reload(MS)
        src = open(os.path.join(PLUGINS, "mindscape_stickers.py"),
                   encoding="utf-8").read()
        has_gate = ("def img_size" in src) and ("max_side" in src)
        cases = [
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
             + _st.pack(">II", 1920, 1200) + b"\x00" * 32, (1920, 1200), "png"),
            (b"GIF89a" + _st.pack("<HH", 500, 400) + b"\x00" * 32, (500, 400), "gif"),
            (b"\xff\xd8\xff\xc0" + _st.pack(">H", 17) + b"\x08"
             + _st.pack(">HH", 1080, 1920) + b"\x00" * 32, (1920, 1080), "jpg"),
        ]
        f = os.path.join(HERE, "_sc_size.bin")
        got = {}
        for data, want, tag in cases:
            with open(f, "wb") as fp:
                fp.write(data)
            got[tag] = (MS.img_size(f) == want)
        os.remove(f)
        (ok if (has_gate and all(got.values())) else bad)(
            "R21 采集有分辨率闸门",
            "闸门=%s %s" % (has_gate, " ".join("%s=%s" % (k, v) for k, v in got.items())))
    except Exception as e:
        bad("R21 分辨率闸门", str(e)[:140])

    # R22: 采集入库要能真的落盘 —— 端到端跑一遍，不是看源码里有没有关键字。
    #      病根：闸门那段把 h 从 md5 复用成了图片高度，于是后面 h[:10] 炸成
    #      'int' object is not subscriptable；异常被 collect 吞成一条 WARN，
    #      日志里看着像「偶尔失败」，其实是**每一张通过闸门的图都存不进去**。
    #      变量再被顶掉一次，这条就会红。
    #      顺带看住两件事：采集必须丢后台（await 会堵死消息流水线），
    #      判定前必须缩图（原图 base64 上行把 2 秒拖成 25 秒）。
    try:
        import asyncio as _aio
        import struct as _st2
        import mindscape_stickers as MS2
        importlib.reload(MS2)

        work = os.path.join(HERE, "_sc_stickers")
        if os.path.isdir(work):
            for n in os.listdir(work):
                os.remove(os.path.join(work, n))
        os.makedirs(work, exist_ok=True)
        img = os.path.join(work, "t.png")
        with open(img, "wb") as fp:
            fp.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                     + _st2.pack(">II", 500, 500) + b"\x00" * 32)

        class _Comp:
            async def convert_to_file_path(self):
                return img

        class _Self:
            pass

        def _mk(max_side):
            s = _Self()
            s.s_c = {"judge": {"max_side": max_side}}
            s.seen = set()
            s.dir = work
            s.index_path = os.path.join(work, "index.json")
            s._save_seen = lambda: None
            s.added = []
            s._add_index = lambda fname, cat, v: s.added.append(fname)

            async def _j(p):
                return {"related": True, "name": "测试图",
                        "desc": "一张测试图", "tags": ["测试"]}
            s._judge = _j
            return s

        s = _mk(1200)
        _aio.run(MS2.StickersMixin._handle(s, _Comp(), "catA"))
        saved = sorted(n for n in os.listdir(work) if n != "t.png")
        saved_ok = (len(saved) == 1 and len(saved[0]) == 14
                    and saved[0].endswith(".png") and s.added == saved)

        s2 = _mk(100)          # 闸门要挡得住
        _aio.run(MS2.StickersMixin._handle(s2, _Comp(), "catA"))
        gate_ok = (s2.added == []
                   and sorted(n for n in os.listdir(work) if n != "t.png") == saved)

        src2 = open(os.path.join(PLUGINS, "mindscape_stickers.py"),
                    encoding="utf-8").read()
        bg_ok = ("create_task" in src2 and "_bg.add" in src2
                 and "await self._handle" not in src2)
        shrink_ok = ("def shrink_for_judge" in src2
                     and "shrink_for_judge(path)" in src2)
        (ok if (saved_ok and gate_ok and bg_ok and shrink_ok) else bad)(
            "R22 采集入库端到端",
            "落盘=%s 闸门=%s 后台=%s 缩图=%s" % (saved_ok, gate_ok, bg_ok, shrink_ok))
        for n in os.listdir(work):
            os.remove(os.path.join(work, n))
        os.rmdir(work)
    except Exception as e:
        bad("R22 采集入库", str(e)[:140])

    # R23: save_sticker 必须能看见「引用消息」里的图。
    #      病根：只扫 event.message_obj.message 的顶层，而 aiocqhttp 适配器是把被
    #      引用消息的完整链塞进 Reply.chain（它会 call_action("get_msg")）。
    #      于是「引用一张图说加进表情库」永远回「没看到图片」，
    #      而模型那条路看得见同一张图 —— 表现为左右脑互搏。
    try:
        import ast as _ast
        src3 = open(os.path.join(PLUGINS, "mindscape_sticker_use.py"),
                    encoding="utf-8").read()
        tree3 = _ast.parse(src3)
        fn3 = [n for n in tree3.body
               if isinstance(n, _ast.FunctionDef) and n.name == "pick_image"]
        ns3 = {}
        if fn3:
            exec(compile(_ast.Module(body=[fn3[0]], type_ignores=[]), "<x>", "exec"), ns3)
        pick = ns3.get("pick_image")

        class _Img:
            pass

        class _Rpl:
            def __init__(self, chain):
                self.chain = chain

        plain = object()
        top_ok = quote_ok = none_ok = loop_ok = False
        if pick:
            top_ok = isinstance(pick([plain, _Img()], _Img, _Rpl), _Img)
            inner = _Img()
            quote_ok = pick([_Rpl([plain, inner]), plain], _Img, _Rpl) is inner
            none_ok = pick([_Rpl([plain]), plain], _Img, _Rpl) is None
            loop = _Rpl([])
            loop.chain = [loop]          # 自己引用自己，不能死循环
            loop_ok = pick([loop], _Img, _Rpl) is None
        wired = ("pick_image(comps, Image, Reply)" in src3
                 and "import Image, Reply" in src3)
        (ok if (top_ok and quote_ok and none_ok and loop_ok and wired) else bad)(
            "R23 save_sticker 认得引用里的图",
            "顶层=%s 引用=%s 空=%s 防环=%s 接线=%s"
            % (top_ok, quote_ok, none_ok, loop_ok, wired))
    except Exception as e:
        bad("R23 引用图片", str(e)[:140])

    # R24: 去重表（seen.json）要能自愈。
    #      病根：从 WebUI 删掉图库条目时，seen 里的去重记录不会跟着删，于是留下
    #      一条**墓碑** —— collect 第一件事就是 `if key in self.seen: return`，
    #      所以那张图再发一次也收不进来，用户看到的是「删了以后就再也收不回来」。
    #      文件名就是 md5 的前 10 位，所以只比对前缀，不用重新哈希整个图库。
    try:
        import json as _js
        import mindscape_stickers as MS3
        importlib.reload(MS3)

        work = os.path.join(HERE, "_sc_seen")
        if not os.path.isdir(work):
            os.makedirs(work)
        # 图库里真放一张图（内容 md5 决定它的去重键）；另有 2 条对不上的
        import hashlib as _hl
        blob = b"\x89PNG\r\n\x1a\n" + b"x" * 64
        real = _hl.md5(blob).hexdigest()
        live_key = "catA:" + real
        dead_key = "catA:" + "f" * 32
        dead2 = "catB:" + "0" * 32
        ifile = os.path.join(work, "index.json")
        sfile = os.path.join(work, "seen.json")
        with open(os.path.join(work, real[:10] + ".gif"), "wb") as fp:
            fp.write(blob)
        with open(ifile, "w", encoding="utf-8") as fp:
            _js.dump([{"file": real[:10] + ".gif", "category": "catA"}], fp)
        with open(sfile, "w", encoding="utf-8") as fp:
            _js.dump([live_key, dead_key, dead2], fp)

        class _S:
            pass

        s = _S()
        s.seen_path = sfile
        s.index_path = ifile
        s.dir = work
        s._save_seen = lambda: None
        kept = MS3.StickersMixin._load_seen(s)

        # 带前缀的文件名不能被误判成墓碑（catA_<md5>.gif 的前缀不是 md5）
        pf = "catA_" + real[:10] + ".gif"
        os.rename(os.path.join(work, real[:10] + ".gif"), os.path.join(work, pf))
        with open(ifile, "w", encoding="utf-8") as fp:
            _js.dump([{"file": pf, "category": "catA"}], fp)
        with open(sfile, "w", encoding="utf-8") as fp:
            _js.dump([live_key], fp)
        s3 = _S()
        s3.seen_path = sfile
        s3.index_path = ifile
        s3.dir = work
        s3._save_seen = lambda: None
        prefixed_ok = (MS3.StickersMixin._load_seen(s3) == {live_key})
        with open(sfile, "w", encoding="utf-8") as fp:
            _js.dump([live_key, dead_key], fp)

        # 坏文件不能炸：seen.json 写成乱七八糟的，应返回空集
        with open(sfile, "w", encoding="utf-8") as fp:
            fp.write("{not a list")
        s2 = _S()
        s2.seen_path = sfile
        s2.index_path = ifile
        s2.dir = work
        s2._save_seen = lambda: None
        bad_ok = (MS3.StickersMixin._load_seen(s2) == set())

        prune_ok = (kept == {live_key})
        for n in os.listdir(work):
            os.remove(os.path.join(work, n))
        os.rmdir(work)
        (ok if (prune_ok and bad_ok and prefixed_ok) else bad)(
            "R24 去重表自愈",
            "只留现存=%s 坏文件不炸=%s 带前缀不误杀=%s"
            % (prune_ok, bad_ok, prefixed_ok))
    except Exception as e:
        bad("R24 去重表自愈", str(e)[:140])

    # R05: 同一秒内更大序号的消息不能被漏读
    try:
        import mindscape_diary as MD
        db = os.path.join(HERE, "_sc_msgs.db")
        if os.path.exists(db):
            os.remove(db)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (timestamp INT, sequence INT, data TEXT)")
        for ts, seq in [(100, 1), (100, 2), (101, 3)]:
            payload = ('{"user_id":"9","group_id":"1","message":'
                       '[{"type":"text","data":{"text":"m%d"}}]}' % seq)
            con.execute("INSERT INTO messages VALUES (?,?,?)", (ts, seq, payload))
        con.commit()
        con.close()
        src = {"db": db, "table": "messages",
               "fields": {"time": "timestamp", "seq": "sequence", "data": "data"}}
        rows = MD.fetch(src, {"self_id": "0", "groups": []}, 100, 1)
        seqs = [r["seq"] for r in rows]
        (ok if seqs == [2, 3] else bad)("R05 同秒游标", "返回 %s（应为 [2, 3]）" % seqs)
        os.remove(db)
    except Exception as e:
        bad("R05 同秒游标", str(e)[:140])

    # R02: 路径穿越必须在入库前就被拒绝
    try:
        from mindscape_core import safe_name, is_inside
        c1 = safe_name("../x.png")
        c2 = safe_name("ok.png")
        c3 = safe_name("/etc/passwd")
        c4 = safe_name("C:\\win.png")
        good = (c1 == "" and c2 == "ok.png" and c3 == "" and c4 == "")
        (ok if good else bad)("R02 路径穿越拦截",
                              "../x=%r ok=%r abs=%r 盘符=%r" % (c1, c2, c3, c4))
    except Exception as e:
        bad("R02 路径校验", str(e)[:140])


def main():
    print("bot-mindscape 全套自检")
    print("仓库: " + HERE)
    check_syntax()
    check_config()
    check_functions()
    check_privacy()
    check_structure()
    check_regressions()
    print()
    print("=" * 56)
    print("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print()
        print("失败清单:")
        for f in FAIL:
            print("  - " + f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())