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
def check_privacy():
    section("4. 脱敏扫描")
    pats = ["20000000", "20000001", "20000002", "20000003",
            "bot-name", "bot", "example-bot", "***", "主播腔", "毒舌",
            "某企划"]
    SELF = "run_selfcheck.py"     # 本脚本含词表，跳过自身
    leaked = []
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for n in names:
            if not n.endswith((".py", ".md", ".yaml", ".json", ".txt")):
                continue
            if n == SELF:
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
        ok("脱敏", "未发现敏感信息")


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
            "plugins/mindscape_janitor.py",
            "scripts/config_gui.py", "scripts/web_ui.py",
            "scripts/import_stickers.py",
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
    #      「完全不记得任何人」（这正是某个 bot忘了重要的人的机制性原因）。
    try:
        import mindscape_memory as MM3
        importlib.reload(MM3)
        f = os.path.join(HERE, "_sc_bigblock.md")
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("## 我的成长记录" + chr(10))
            for i in range(60):
                fp.write("- 第 %d 条：重要的人是姐姐" % i + chr(10))
        r = MM3.read_recent(f, 300)
        good = bool(r) and ("重要的人" in r) and len(r) <= 300
        (ok if good else bad)("R11 超大块不丢记忆",
                              "%d 字，含关键词=%s" % (len(r), "重要的人" in r))
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
    #      实测：重要的人被自动摘要写成「群友，常发图」，而人格档案里她明明是
    #      「Alice（小爱）→ 喜欢的人」。关系是作者写死的，不该交给摘要模型猜。
    try:
        import mindscape_diary as MD
        importlib.reload(MD)
        f = os.path.join(HERE, "_sc_people.md")
        # 先放一个「旧格式」文件，确认迁移不会把已有条目整批冲掉
        with open(f, "w", encoding="utf-8") as fp:
            # 旧格式：没有分段标题，且已经有一条被降级的「重要的人：群友」
            fp.write("# 你认识的人（自动维护）" + chr(10) * 2
                     + "最后更新：2026-01-01 00:00" + chr(10) * 2
                     + "- 老条目：迁移前就存在" + chr(10)
                     + "- 重要的人：群友，常发图" + chr(10))
        rel = ["- Alice（小爱）→ 喜欢的人，认真对待"]
        MD._update_people(f, {"重要的人": "群友，常发图", "新群友": "刚进群"},
                          "2026-01-02 00:00", rel)
        got = open(f, encoding="utf-8").read()
        kept_old = "老条目" in got
        pinned = "喜欢的人" in got
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
                     + "- Alice（小爱）→ 喜欢的人" + chr(10)
                     + "这段普通文字不该被取" + chr(10)
                     + "## 别的段落" + chr(10)
                     + "- 这段也不该被取" + chr(10))
        rows = MD2.load_relations({"file": f, "section": "关系与称呼"})
        right = (len(rows) == 1 and "重要的人" in rows[0])
        (ok if right else bad)("R14 relations 只取指定段落", "%d 行: %s" % (len(rows), rows))
        os.remove(f)
    except Exception as e:
        bad("R14 relations 段落提取", str(e)[:140])

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