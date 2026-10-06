# -*- coding: utf-8 -*-
"""bot-mindscape 全套自检

覆盖：
  1. 所有 .py 语法编译
  2. 配置示例 YAML 可解析 + 关键字段齐全
  3. 各模块核心纯函数行为（打桩框架）
  4. 脱敏扫描（无真实信息泄漏）
  5. 仓库结构完整性
  6. 转义保真（产物里带反斜杠的字符串必须与源码逐字一致）
  7. 产物新鲜度（按 --market 重建，与 dist 逐字节比较）

用法：python scripts/run_selfcheck.py
"""
import ast
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
    for key in ["memory", "groupctx", "diary", "stickers", "guard", "silence", "format",
                "trace", "janitor", "waking", "mention"]:
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
    # 任意 filter 属性都能当装饰器用 —— 以后模块加新钩子不会再让自检崩掉
    class _AnyFilter:
        def __getattr__(self, _k):
            return lambda *a, **kw: (lambda f: f)

    ev.filter = _AnyFilter()
    pe = types.ModuleType("astrbot.core.provider.entities")
    pe.ProviderRequest = object
    mc = types.ModuleType("astrbot.core.message.components")
    mc.Image = type("Image", (), {})
    _comp_cache = {}

    def _mc_getattr(name):        # 要什么组件给什么组件（At / Plain / …），同类只造一次
        if name not in _comp_cache:
            _comp_cache[name] = type(name, (), {
                "__init__": lambda s, **kw: s.__dict__.update(kw),
            })
        return _comp_cache[name]

    mc.__getattr__ = _mc_getattr
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

    # janitor：先「脱图」再「删行」—— 脱图保住会话上下文，删行只是兜底。
    #      实测事故（2026-10-02）：群里一张图以 base64 落进历史，单条 48 万字 →
    #      那个会话每轮请求都带着它（≈12 万 token，顶穿上下文窗口）。
    #      老行为是整行删掉（连会话上下文一起没），现在只脱那坨数据。
    try:
        import sqlite3
        import json as _json
        jn = load("mindscape_janitor")
        db = os.path.join(HERE, "_selfcheck.db")
        if os.path.exists(db):
            os.remove(db)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE conversations (content TEXT)")
        con.execute("INSERT INTO conversations VALUES (?)", ("x" * 100,))
        payload = _json.dumps([{"role": "user", "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url":
             {"url": "data:image/png;base64," + "A" * 5000}},
        ]}], ensure_ascii=False)
        con.execute("INSERT INTO conversations VALUES (?)", (payload,))
        con.execute("INSERT INTO conversations VALUES (?)", ("data:image/png;base64,AAAA",))
        # ③ 一行已经**被改坏**的（url 是占位符，行里已经没有 data:image）→ 也要能自愈
        broken = _json.dumps([{"role": "user", "content": [
            {"type": "text", "text": "旧图的残骸"},
            {"type": "image_url", "image_url": {"url": "[图片]", "id": None}},
        ]}], ensure_ascii=False)
        con.execute("INSERT INTO conversations VALUES (?)", (broken,))
        con.commit(); con.close()
        n_srow, n_shit, n_img, n_big, b, a = jn.clean(db, "conversations", "content", 2.0)
        con = sqlite3.connect(db)
        rows = [r[0] for r in con.execute("SELECT content FROM conversations")]
        con.close()
        kept_ctx = any("看这张图" in r for r in rows)
        no_media = not any("data:image" in r for r in rows)
        healed = any("旧图的残骸" in r for r in rows)
        good = (n_srow == 2 and n_shit == 2 and n_img == 1
                and kept_ctx and no_media and healed and len(rows) == 3)
        (ok if good else bad)(
            "janitor.clean 先脱图再删行",
            "脱图 %d 行/%d 处 | 删图片行 %d | 会话保留=%s 无 base64=%s 自愈=%s | 剩 %d 行"
            % (n_srow, n_shit, n_img, kept_ctx, no_media, healed, len(rows)))
        # ⚠️ 脱图后**不能留下非法的 image_url 段**：provider 会直接 400
        #    （Unsupported image_url format），那个会话从此每轮都失败、一句话都说不出来。
        #    实测踩过（2026-10-02 18:5x）。所以断言「换成了文字段」。
        fmt_ok = False
        for r in rows:
            if "看这张图" not in r:
                continue
            for m in _json.loads(r):
                c = m.get("content") if isinstance(m, dict) else None
                if not isinstance(c, list):
                    continue
                for part in c:
                    if (isinstance(part, dict) and part.get("type") == "text"
                            and part.get("text") == "[图片]"):
                        fmt_ok = True
        (ok if fmt_ok else bad)("janitor 脱图后不留非法 image_url 段",
                              "换成文字段=%s" % fmt_ok)
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


def _gitignored():
    """读 .gitignore，返回 (目录名集合, 精确相对路径集合, 裸文件名集合)。

    扫描器必须认 .gitignore：本机有 config/config.yaml（含服务器密码和 bot 号）
    与 data/（拉下来的图库，标签里都是角色名）—— 这些**永远不会被提交**。
    不区分「会被提交的」和「本机私有的」，扫描就会一直报假警，最后没人看它。

    ⚠️ 源码里出现真实名词**依然要拦**（那是真泄漏）—— 这份忽略表只管被 git 忽略的文件。
    """
    dirs, files, names = set(), set(), set()
    try:
        with open(os.path.join(HERE, ".gitignore"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                line = line.lstrip("/")
                if line.endswith("/"):
                    dirs.add(line.rstrip("/"))
                elif "/" in line:
                    files.add(line)
                else:
                    names.add(line)
    except Exception:
        pass
    return dirs, files, names


def check_privacy():
    section("4. 脱敏扫描")
    pats = _private_names()
    SKIP = {"run_selfcheck.py", "private-names.txt", "private-names.example.txt"}
    idirs, ifiles, inames = _gitignored()
    leaked = []
    skipped = 0
    for root, dirs, names in os.walk(HERE):
        rel_root = os.path.relpath(root, HERE).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git") and d not in idirs]
        for n in names:
            if not n.endswith((".py", ".md", ".yaml", ".json", ".txt")):
                continue
            rel = (rel_root + "/" + n) if rel_root else n
            if n in SKIP or n in inames or rel in ifiles:
                skipped += 1
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
        ok("脱敏", "未发现敏感信息（本地词表 %d 条，另有 %d 个文件被 .gitignore 排除）"
           % (len(pats), skipped))


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
        n_mod = 0
        for f in sorted(os.listdir(pdir)):
            if not (f.startswith("mindscape_") and f.endswith(".py")):
                continue
            n_mod += 1
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
                                 " / ".join(miss) or "%d 个模块全部自洽" % n_mod)
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
            s.seen_ok = set()        # 入库桶
            s.seen_no = set()        # 明确拒绝桶
            # 直接绑真实的记账方法 —— 在桩里另写一份，两边早晚会各自漂移
            s._mark_seen = MS2.StickersMixin._mark_seen.__get__(s, _Self)
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
        # 闸门挡下的图要落进【拒绝桶】：重启后不该被重新下载重判
        bucket_ok = (len(s2.seen_no) == 1 and s2.seen_ok == set()
                     and len(s.seen_ok) == 1 and s.seen_no == set())

        src2 = open(os.path.join(PLUGINS, "mindscape_stickers.py"),
                    encoding="utf-8").read()
        bg_ok = ("create_task" in src2 and "_bg.add" in src2
                 and "await self._handle" not in src2)
        shrink_ok = ("def shrink_for_judge" in src2
                     and "shrink_for_judge(path)" in src2)
        (ok if (saved_ok and gate_ok and bucket_ok and bg_ok and shrink_ok) else bad)(
            "R22 采集入库端到端",
            "落盘=%s 闸门=%s 分桶=%s 后台=%s 缩图=%s"
            % (saved_ok, gate_ok, bucket_ok, bg_ok, shrink_ok))
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

    # R24: 去重表（seen.json）的三种状态必须分得清。
    #      accepted = 真正入库过的图；rejected = **明确拒绝**（超尺寸 / 判定不相关）。
    #      以前只有一张平表，自愈时「只保留图库里现存的图」→ 明确拒绝的记录一重启
    #      就被清掉：同一张图被反复下载、反复调用视觉 API，甚至判定翻转后又进库。
    #      现在只清 accepted 里「图已从库中删除」的墓碑，rejected 一律保留。
    try:
        import json as _js
        import hashlib as _hl
        import mindscape_stickers as MS3
        importlib.reload(MS3)

        work = os.path.join(HERE, "_sc_seen")
        if not os.path.isdir(work):
            os.makedirs(work)
        blob = b"\x89PNG\r\n\x1a\n" + b"x" * 64
        real = _hl.md5(blob).hexdigest()
        live_key = "catA:" + real
        dead_key = "catA:" + "f" * 32
        dead2 = "catB:" + "0" * 32
        ifile = os.path.join(work, "index.json")
        sfile = os.path.join(work, "seen.json")
        img = os.path.join(work, real[:10] + ".gif")
        with open(img, "wb") as fp:
            fp.write(blob)
        with open(ifile, "w", encoding="utf-8") as fp:
            _js.dump([{"file": real[:10] + ".gif", "category": "catA"}], fp)

        class _S:
            pass

        def _mk_seen(payload):
            with open(sfile, "w", encoding="utf-8") as fp:
                if isinstance(payload, str):
                    fp.write(payload)
                else:
                    _js.dump(payload, fp)

        def _load():
            s = _S()
            s.seen_path = sfile
            s.index_path = ifile
            s.dir = work
            s.seen_ok, s.seen_no = set(), set()
            saved = []
            s._save_seen = lambda: saved.append(1)
            return MS3.StickersMixin._load_seen(s), s, saved

        # 1) 旧平表：在图库里的算入库，其余**保守归入拒绝**（宁可少收，不能丢拒绝记录）
        _mk_seen([live_key, dead_key, dead2])
        got, s1, saved1 = _load()
        legacy_ok = (got == {live_key, dead_key, dead2}
                     and s1.seen_ok == {live_key}
                     and s1.seen_no == {dead_key, dead2}
                     and bool(saved1))

        # 2) 新格式：accepted 里「图已从库中删除」的墓碑必须清掉
        _mk_seen({"accepted": [live_key, dead_key], "rejected": []})
        got2, s2, _ = _load()
        tomb_ok = (got2 == {live_key} and s2.seen_ok == {live_key})

        # 3) 新格式：rejected **跨重启必须还在** —— 这条就是本次修的病
        _mk_seen({"accepted": [live_key], "rejected": [dead_key]})
        got3, s3, _ = _load()
        keep_ok = (got3 == {live_key, dead_key} and s3.seen_no == {dead_key})

        # 4) 带前缀的文件名不能被误判成墓碑（前缀不是 md5，必须按内容哈希）
        pf = "catA_" + real[:10] + ".gif"
        os.rename(img, os.path.join(work, pf))
        with open(ifile, "w", encoding="utf-8") as fp:
            _js.dump([{"file": pf, "category": "catA"}], fp)
        _mk_seen({"accepted": [live_key], "rejected": []})
        got4, _, _ = _load()
        prefixed_ok = (got4 == {live_key})

        # 5) 坏文件不能炸
        _mk_seen("{not json")
        got5, _, _ = _load()
        bad_ok = (got5 == set())

        # 6) 「判定暂时失败」不许写进去重表：那条分支必须在标记之前就 return
        _src = open(os.path.join(PLUGINS, "mindscape_stickers.py"),
                    encoding="utf-8").read()
        _seg = _src.split("if verdict is None:")[1]
        _seg = _seg[:_seg.index("return") + 6] if "return" in _seg else _seg[:200]
        tmp_ok = ("_mark_seen" not in _seg)

        for n in os.listdir(work):
            os.remove(os.path.join(work, n))
        os.rmdir(work)
        good = legacy_ok and tomb_ok and keep_ok and prefixed_ok and bad_ok and tmp_ok
        (ok if good else bad)(
            "R24 去重表：墓碑清理 / 拒绝保留",
            "旧表迁移=%s 墓碑清理=%s 拒绝保留=%s 带前缀=%s 坏文件=%s 暂失败不记=%s"
            % (legacy_ok, tomb_ok, keep_ok, prefixed_ok, bad_ok, tmp_ok))
    except Exception as e:
        bad("R24 去重表", str(e)[:140])
        bad("R24 去重表自愈", str(e)[:140])

    # R25: 风格学习（mindscape_learn）必须「默认关闭 + 不耦合 + 不碰别人」。
    #      这是个**离线管道**，不挂运行时钩子：只写文件，注入与否由各自 bot 的
    #      memory 配置决定。三条约束任何一条破了都会伤到「不想用它」的 bot。
    try:
        import json as _js2
        import sqlite3 as _sq
        import mindscape_diary as MD
        import mindscape_learn as ML
        importlib.reload(MD)
        importlib.reload(ML)

        # (1) 默认关闭：enabled 不为真，一行都不跑
        _saved_section = ML.cfg.section
        _orig_run = ML.ln_run_target
        called = []
        ML.ln_run_target = lambda d, t: (called.append(1), (0, 0))[1]
        off_ok = True
        for flag in (False, None, 0, ""):
            called[:] = []
            ML.cfg.section = lambda n, _f=flag: (
                {"enabled": _f, "targets": [{"user_id": "1", "output": "p"}]}
                if n == "learn" else {})
            ML.ln_main()
            if called:
                off_ok = False
        called[:] = []
        ML.cfg.section = lambda n: (
            {"enabled": True, "targets": [{"user_id": "1", "output": "p"}]}
            if n == "learn" else {})
        ML.ln_main()
        on_ok = bool(called)
        ML.cfg.section = _saved_section
        ML.ln_run_target = _orig_run

        # (2) only_user：只取目标用户的消息；不传时保持原语义（排除 self_id）
        db = os.path.join(HERE, "_sc_learn.db")
        if os.path.exists(db):
            os.remove(db)
        con = _sq.connect(db)
        con.execute("CREATE TABLE messages (timestamp INT, sequence INT, data TEXT)")
        def _row(ts, seq, uid, text):
            return (ts, seq, _js2.dumps({
                "user_id": uid, "group_id": "9", "group_name": "g",
                "sender": {"nickname": uid},
                "message": [{"type": "text", "data": {"text": text}}]},
                ensure_ascii=False))
        con.executemany("INSERT INTO messages VALUES (?,?,?)", [
            _row(1, 1, "AAA", "我的第一句"),
            _row(2, 2, "BBB", "别人的一句"),
            _row(3, 3, "AAA", "我的第二句"),
        ])
        con.commit()
        con.close()
        src = {"db": db, "where": "1=1"}
        got = MD.fetch(src, {"user_id": "AAA"}, 0, 0, only_user="AAA")
        only_ok = (len(got) == 2 and all(r["uid"] == "AAA" for r in got))
        got2 = MD.fetch(src, {"self_id": "AAA"}, 0, 0)
        default_ok = (len(got2) == 1 and got2[0]["uid"] == "BBB")
        os.remove(db)

        # (3) call_llm 要能换顶层键（日记 diary / 风格 observations）
        import inspect as _insp
        key_ok = "expect_key" in _insp.signature(MD.call_llm).parameters

        # (4) 风格层必须存在、按「账本之后、摘要之前」注入，且没配就完全不进 block
        mem_src = open(os.path.join(PLUGINS, "mindscape_memory.py"),
                       encoding="utf-8").read()
        has_layer = ("SECTION_STYLE" in mem_src and 'bot.get("style")' in mem_src
                     and "DEFAULT_STYLE_CHARS" in mem_src)
        seg = mem_src[mem_src.find("SECTION_NOTES + "):]
        order_ok = (seg.find("SECTION_STYLE + ") > 0
                    and 0 < seg.find("SECTION_STYLE + ") < seg.find("SECTION_DIGEST + "))
        guard_ok = "if sty:" in mem_src and "not sty" in mem_src

        (ok if (off_ok and on_ok and only_ok and default_ok and key_ok
                and has_layer and order_ok and guard_ok) else bad)(
            "R25 风格学习默认关闭且独立",
            "默认关=%s 开了会跑=%s 只取目标=%s 原语义不变=%s 键可换=%s "
            "风格层=%s 顺序=%s 未配不进=%s"
            % (off_ok, on_ok, only_ok, default_ok, key_ok,
               has_layer, order_ok, guard_ok))
    except Exception as e:
        bad("R25 风格学习", str(e)[:140])

    # R26: 风格分层（稳定层 + 近期层）不能写出「认知 bug」。
    #      四类风险：同一件事说两遍 / 两段冲突 / 把风格当记忆 / 宣告自己的口癖。
    #      另外两条硬要求：只收「怎么说」类条目、稳定层必须**覆盖写**。
    try:
        import mindscape_style as SC
        importlib.reload(SC)

        raw_t = (
            "## 2026-09-19 观察\n- 用'沃'代替'我'\n- 说话简短\n"
            "## 2026-09-19 兴趣\n- 在玩魔女狼人杀\n"
            "## 2026-09-19 值得记的\n- 某件私事\n"
            "## 2026-09-20 新词\n- 惹\n"
            "## 2026-09-20 原声示例\n- 想被姐姐揉揉\n"
        )
        secs = SC.sc_parse(raw_t)
        parse_ok = (len(secs) == 5
                    and [(x["date"], x["cat"]) for x in secs][0] == ("2026-09-19", "观察"))

        # 只收「怎么说」：兴趣 / 值得记的 必须被排除
        allines = SC.sc_style_lines(secs)
        cat_ok = (len(allines) == 4
                  and not any(("兴趣" in x) or ("值得记" in x) for x in allines))

        # 日期窗口
        rec = SC.sc_style_lines(secs, cutoff="2026-09-20")
        win_ok = (len(rec) == 2 and all(x.startswith("2026-09-20") for x in rec))

        # 禁词：喂给 LLM 之前就剔除（人名被误读成自称变体的逃生口）
        secs2 = SC.sc_parse("## 2026-09-20 新词\n- 阿明\n- 惹\n"
                            "## 2026-09-20 观察\n- 他自称阿明\n")
        ex = SC.sc_style_lines(secs2, exclude=["阿明"])
        exc_ok = (len(ex) == 1 and "阿明" not in ex[0] and "惹" in ex[0])

        # 产出再剔一遍：只删词、不删行（同行的有用内容要留住）
        san = SC.sc_sanitize("- 自称变体：窝、沃、阿明\n- 阿明\n- 别的", ["阿明"])
        san_ok = ("窝" in san and "沃" in san and "阿明" not in san
                  and "别的" in san and "\n- \n" not in san)
        sysw_ok = "不要把别人的昵称" in SC.DEFAULT_SYSTEM

        # 覆盖写：写两次，第二次必须把第一次顶掉
        p = os.path.join(HERE, "_sc_style.md")
        SC.sc_write(p, "<!-- h -->", "AAAA")
        SC.sc_write(p, "<!-- h -->", "BBBB")
        body = open(p, encoding="utf-8").read()
        ovw_ok = ("AAAA" not in body) and ("BBBB" in body)
        os.remove(p)

        # 默认关闭
        _saved = SC.cfg.section
        _orig = SC.sc_run_target
        called = []
        SC.sc_run_target = lambda td: (called.append(1), (0, 0))[1]
        SC.cfg.section = lambda n: (
            {"enabled": False, "targets": [{"name": "x"}]} if n == "style" else {})
        SC.sc_main()
        off_ok = not called
        SC.cfg.section = lambda n: (
            {"enabled": True, "targets": [{"name": "x"}]} if n == "style" else {})
        SC.sc_main()
        on_ok = bool(called)
        SC.cfg.section = _saved
        SC.sc_run_target = _orig

        # 记忆层：两段都要有、顺序对、四道守卫都在
        m_src = open(os.path.join(PLUGINS, "mindscape_memory.py"),
                     encoding="utf-8").read()
        slot_ok = ('bot.get("style_recent")' in m_src
                   and "DEFAULT_STYLE_RECENT_CHARS" in m_src)
        blk = m_src[m_src.find("if sty or sty2:"):]
        order_ok = (0 <= blk.find("SECTION_STYLE_STABLE")
                    < blk.find("SECTION_STYLE_RECENT")
                    < blk.find("STYLE_GUARD"))
        guard_ok = all(k in m_src for k in (
            "不是记忆、也不是事实", "别宣告它们",
            "那是同一件事，不是两件", "以「最近的变化」为准",
            "别把它们当往事提起"))
        # 生成器写在文件头的给人看的注释，不能进 prompt
        # 自主冒泡轮：记忆照给，但要明说「可以完全不依赖」
        cron_ok = ('event.get_extra("cron_job")' in m_src
                   and "可以完全不依赖它们" in m_src)
        # 稳定层是**文档**（从头读），近期层是**追加流**（取尾）—— 搞反会切掉口癖
        clean_ok = ("def _clean_style" in m_src
                    and "_clean_style(read_head(st_path" in m_src
                    and "_clean_style(read_recent(sr_path" in m_src
                    and "def read_head" in m_src)

        (ok if (parse_ok and cat_ok and win_ok and ovw_ok and off_ok and on_ok
                and slot_ok and order_ok and guard_ok and clean_ok
                and exc_ok and san_ok and sysw_ok and cron_ok) else bad)(
            "R26 风格分层无认知 bug",
            "解析=%s 只收风格=%s 窗口=%s 覆盖写=%s 默认关=%s 开了会跑=%s "
            "双槽=%s 顺序=%s 四守卫=%s 去注释=%s 禁词=%s 产出再剔=%s 提示词=%s 冒泡轮=%s"
            % (parse_ok, cat_ok, win_ok, ovw_ok, off_ok, on_ok,
               slot_ok, order_ok, guard_ok, clean_ok, exc_ok, san_ok, sysw_ok, cron_ok))
    except Exception as e:
        bad("R26 风格分层", str(e)[:140])

    # R27: 沉默权（mindscape_silence）必须「真的不发」，而不是「换个说法不发」；
    #      令牌也绝不能漏进群里。
    try:
        import mindscape_silence as SI
        importlib.reload(SI)

        tok = SI.SI_DEFAULT_TOKEN
        hit = all(SI.si_is_silence(x, tok) for x in (
            "[[silence]]", "silence", "【silence】", " [silence]。 ",
            "**[[silence]]**", "SILENCE"))
        miss = not any(SI.si_is_silence(x, tok) for x in (
            "今天天气不错", "", "（和我无关，安静飘过）",
            "[[silence]] 算了还是说两句吧"))
        norm_ok = hit and miss

        strip_ok = (SI.si_strip("[[silence]] 算了还是说两句吧", tok) == "算了还是说两句吧"
                    and SI.si_strip("先这样 [[Silence]] 再说", tok) == "先这样  再说")

        _saved_sec = SI.cfg.section
        SI.cfg.section = lambda n: {}
        off_ok = SI.si_load_config()[0] is False
        SI.cfg.section = lambda n: {"enabled": True, "token": "[[闭嘴]]"}
        _onc = SI.si_load_config()
        on_ok = (_onc[0] is True and _onc[1] == "[[闭嘴]]" and "[[闭嘴]]" in _onc[3])
        SI.cfg.section = _saved_sec

        class _SiEv:
            def __init__(self, cron):
                self.cron = cron

            def get_self_id(self):
                return "1"

            def get_extra(self, k):
                return {"cron_job": {}} if (k == "cron_job" and self.cron) else None

        class _SiReq:
            system_prompt = ""

        sm = SI.SilenceMixin.__new__(SI.SilenceMixin)
        sm.si_on, sm.si_token, sm.si_targets = True, tok, []
        sm.si_prompt = SI.SI_PROMPT % {"token": tok}
        sm.si_count = 0

        rq1 = _SiReq()
        asyncio.run(sm.si_grant(_SiEv(False), rq1))
        grant_ok = (tok in rq1.system_prompt and "安静飘过" in rq1.system_prompt)
        rq2 = _SiReq()
        asyncio.run(sm.si_grant(_SiEv(True), rq2))
        cron_ok = (tok not in rq2.system_prompt and "别发" in rq2.system_prompt)

        class _SiComp:
            def __init__(self, t):
                self.text = t

        class _SiRes:
            def __init__(self, t):
                self.t = t
                self.chain = [_SiComp(t)]

            def get_plain_text(self):
                return self.t

        class _SiEv2:
            def __init__(self, t):
                self.res = _SiRes(t)
                self.cleared = False
                self.stopped = False

            def get_self_id(self):
                return "1"

            def get_extra(self, k):
                return None

            def get_result(self):
                return self.res

            def clear_result(self):
                self.cleared = True

            def stop_event(self):
                self.stopped = True

        e1 = _SiEv2("[[silence]]")
        asyncio.run(sm.si_block(e1))
        block_ok = bool(e1.cleared and e1.stopped)

        e2 = _SiEv2("[[silence]] 算了还是说两句吧")
        asyncio.run(sm.si_block(e2))
        leak_ok = (not e2.cleared and e2.res.chain[0].text == "算了还是说两句吧")

        e3 = _SiEv2("你今天吃了没")
        asyncio.run(sm.si_block(e3))
        pass_ok = not e3.cleared

        s_src = open(os.path.join(PLUGINS, "mindscape_silence.py"),
                     encoding="utf-8").read()
        prio_ok = ("on_decorating_result(priority=1000)" in s_src
                   and 'event.get_extra("cron_job")' in s_src)

        (ok if (norm_ok and strip_ok and off_ok and on_ok and grant_ok
                and cron_ok and block_ok and leak_ok and pass_ok and prio_ok) else bad)(
            "R27 沉默权真的不说话",
            "归一=%s 剃令牌=%s 默认关=%s 开了=%s 回复轮给=%s 冒泡轮不给=%s "
            "整条清空=%s 不漏令牌=%s 正常放行=%s 优先级=%s"
            % (norm_ok, strip_ok, off_ok, on_ok, grant_ok, cron_ok,
               block_ok, leak_ok, pass_ok, prio_ok))
    except Exception as e:
        bad("R27 沉默权", str(e)[:140])

    # R28: 唤醒的名字匹配 —— 「@小星」必须叫得醒，
    #      但「@爱小星的某某」不能被误判成在叫 bot（这个坑真踩过）。
    try:
        import re as _rex
        ja = open(os.path.join(HERE, "patches", "astrbot",
                               "_waking_judge_block.py"), encoding="utf-8").read()
        jl = ja.splitlines()
        i0 = next(i for i, l in enumerate(jl) if l.strip().startswith("_ms_bare ="))
        i1 = next(i for i, l in enumerate(jl) if l.strip().startswith("_ms_mentioned ="))
        i2 = next(i for i in range(i1, len(jl)) if "_ms_excl))" in jl[i])
        jblock = chr(10).join(jl[i0:i2 + 1])

        class _FakeEv:
            def get_group_id(self):
                return "1"

        def _wake(text, names=("小星", "火花"), excl=("小白",)):
            ns = {"_ms_re": _rex, "_ms_group_ok": True, "_ms_text": text,
                  "_ms_names": list(names), "_ms_excl": list(excl),
                  "_ms_pb": {}, "_ms_groups": [], "event": _FakeEv()}
            exec(jblock, ns)
            return bool(ns["_ms_mentioned"])

        at_ok = _wake("@小星 快来欢迎新人")
        qq_ok = _wake("@小星(123456) 在吗")
        plain_ok = _wake("错错错，小星是笨蛋机器人")
        nick_ok = not _wake("@爱小星的某某 你好")
        quiet_ok = not _wake("今天天气不错")
        (ok if (at_ok and qq_ok and plain_ok and nick_ok and quiet_ok) else bad)(
            "R28 @名字能叫醒且不误唤醒",
            "@名字=%s @名字(qq)=%s 纯文本提到=%s 昵称含名字不误判=%s 无关不唤醒=%s"
            % (at_ok, qq_ok, plain_ok, nick_ok, quiet_ok))
    except Exception as e:
        bad("R28 名字匹配", str(e)[:140])

    # R32: 唤醒补丁「站点表」完整 —— 仓库要能**逐字节复现线上那份 stage.py** ✓（2026-10-06 加 ✓）。
    #      背景：线上是手工内联补丁 ✗，仓库那套块只是参考实现 ✗ → 把差异固化成站点表 ✓，
    #      从此「仓库=线上」可被验证 ✓（build 出来的 md5 必须等于表里记的 live_md5 ✓）。
    try:
        import json as _j3
        _sp = os.path.join(HERE, "patches", "astrbot", "waking_sites.json")
        _doc3 = _j3.load(open(_sp, encoding="utf-8"))
        _sites3 = _doc3.get("sites") or []
        _shape = all(("i1" in s and "i2" in s and isinstance(s.get("new"), list)) for s in _sites3)
        _order = all(_sites3[k]["i2"] <= _sites3[k + 1]["i1"] for k in range(len(_sites3) - 1))
        _ok3 = (bool(_doc3.get("base_md5")) and bool(_doc3.get("live_md5"))
                and len(_sites3) >= 10 and _shape and _order)
        (ok if _ok3 else bad)(
            "R32 唤醒补丁站点表",
            "站点 %d 个，底本 %s / 线上 %s，%s" % (
                len(_sites3), str(_doc3.get("base_md5"))[:8], str(_doc3.get("live_md5"))[:8],
                "区间有序不重叠 ✓" if _order else "区间有问题 ✗"))
    except Exception as e:
        bad("R32 站点表", str(e)[:140])

    # R31: 「#角色面板」这类**别的 bot 的指令**不许叫醒我们、也不许进上下文缓冲（2026-10-06 ✓）。
    #      判据三条：① #/** 开头 ② 名字+查询词收尾 ③ 没有别的文字 ✓
    #      ⚠️ 不许连关键词一起拉黑 ✗ —— 真问游戏知识（「火花，行迹怎么点」）必须照常回 ✓
    try:
        import json as _json2
        import tempfile as _tf2
        csrc = open(os.path.join(HERE, "patches", "astrbot",
                                 "_waking_ctx_block.py"), encoding="utf-8").read()
        cns = {}
        exec(csrc, cns)
        with _tf2.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as _f2:
            _json2.dump({"ignore_cmd": {
                "enabled": True, "prefixes": ["#", "*"],
                "keywords": ["面板", "排行", "圣遗物", "武器", "命座", "遗器",
                             "天赋", "行迹", "光锥"]}}, _f2)
            _tmp2 = _f2.name
        cns["_MS_CMD_CFG_PATH"] = _tmp2
        cns["_MS_CMD_CACHE"]["mtime"] = -1.0
        _f = cns["_ms_is_cmd_query"]
        _cases = [("#火花面板", True), ("*火花光锥", True), ("#面板", True),
                  ("# 火花 圣遗物", True), ("#火花的面板", True),
                  ("火花，行迹怎么点", False), ("#火花面板 帮我看看", False),
                  ("#行迹怎么点", False), ("今天天气不错", False), ("#", False)]
        _got = [(t, bool(_f(t))) for t, _ in _cases]
        _bad2 = [t for (t, g), (_, e) in zip(_got, _cases) if g != e]
        os.unlink(_tmp2)
        (ok if not _bad2 else bad)(
            "R31 别的 bot 的指令不唤醒/不进缓冲",
            ("全部符合 ✓" if not _bad2 else "判错：" + "、".join(_bad2)))
    except Exception as e:
        bad("R31 指令拦截", str(e)[:140])

    # R29: 沉默令牌必须容忍「模型多吐零宽字符」—— 否则会退化成一条空回复：
    #      用户看到「叫它不理」，日志里却什么都没有（真踩过）。
    try:
        import re as _rex2
        import mindscape_silence as SI3
        importlib.reload(SI3)
        _tk = SI3.SI_DEFAULT_TOKEN
        _zw = "\u200b"
        zw_ok = (SI3.si_is_silence(_tk + _zw, _tk)
                 and SI3.si_is_silence(_zw + _tk + _zw, _tk)
                 and SI3.si_is_silence(_tk + "\ufeff", _tk))
        # 重复两次：直接比对认不出来 —— 必须靠「剃完只剩空白」那条兜底
        dup = not SI3.si_is_silence(_tk * 2, _tk)
        fallout = (SI3.si_strip(_tk * 2, _tk) == ""
                   and SI3.si_norm(SI3.si_strip(_tk * 2, _tk)) == "")
        _src = open(os.path.join(PLUGINS, "mindscape_silence.py"),
                    encoding="utf-8").read()
        guard_ok = "令牌带杂字" in _src
        (ok if (zw_ok and dup and fallout and guard_ok) else bad)(
            "R29 沉默令牌容忍零宽杂字",
            "零宽可认=%s 重复认不出=%s 剃完为空=%s 有兜底=%s"
            % (zw_ok, dup, fallout, guard_ok))
    except Exception as e:
        bad("R29 沉默令牌容错", str(e)[:140])

    # R30: 产物必须能【真的加载并实例化】。编译过 ≠ 能跑 ——
    #      guard 里有个局部变量撞了模块级的 cfg，py_compile 毫无问题，
    #      但一实例化就 UnboundLocalError，插件整个加载失败（真实事故）。
    try:
        import importlib.util as _ilu
        _bp = os.path.join(HERE, "dist", "mindscape", "main.py")
        _spec = _ilu.spec_from_file_location("_ms_bundle_check", _bp)
        _mod = _ilu.module_from_spec(_spec)
        sys.modules["_ms_bundle_check"] = _mod
        _spec.loader.exec_module(_mod)
        _inst = _mod.MindscapePlugin(object())   # __init__ 会依次跑每个 Mixin 的 setup
        (ok if _inst is not None else bad)(
            "R30 产物可真实加载并实例化",
            "MindscapePlugin 已实例化（setup 全跑通）")
    except Exception as e:
        bad("R30 产物加载/实例化", "%s: %s" % (type(e).__name__, str(e)[:120]))

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
    # R31: 日记分批必须【一条不漏】。以前是固定 batch 条一组、再在 call_llm 里
    #      把文本砍到 max_input_chars —— 被砍掉的尾巴照样被游标跳过（永久漏记）。
    try:
        import json as _json
        import urllib.request as _ur
        import mindscape_diary as MD

        db = os.path.join(HERE, "_sc_batch.db")
        out = os.path.join(HERE, "_sc_batch.md")
        statef = os.path.join(HERE, "_sc_batch.state.json")
        extra = os.path.join(HERE, "_sc_batch.people.md")

        def _mk(dbp, texts):
            con = sqlite3.connect(dbp)
            con.execute("CREATE TABLE messages (timestamp INT, sequence INT, data TEXT)")
            for i, t in enumerate(texts):
                payload = _json.dumps({"user_id": "9", "group_id": "1",
                                       "message": [{"type": "text", "data": {"text": t}}]})
                con.execute("INSERT INTO messages VALUES (?,?,?)", (100 + i, i + 1, payload))
            con.commit()
            con.close()

        for p in (db, out, statef, extra):
            if os.path.exists(p):
                os.remove(p)
        os.environ["_SC_DIARY_KEY"] = "k"
        d = {"source": {"db": db, "table": "messages",
                        "fields": {"time": "timestamp", "seq": "sequence", "data": "data"}},
             "llm": {"api_base": "http://127.0.0.1:9/v1", "api_key_env": "_SC_DIARY_KEY",
                     "model": "x"},
             "batch": 40, "max_input_chars": 500, "max_tokens": 50, "max_batches": 40,
             "targets": [{"output": out, "state": statef, "persona": "p"}]}

        sent = []
        real_open = _ur.urlopen

        class _R:
            def __init__(self, b):
                self._b = b
            def read(self):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake(req, timeout=None):
            body = _json.loads(req.data.decode("utf-8"))
            sent.append(body["messages"][1]["content"])
            return _R(_json.dumps({"choices": [{"message": {"content": _json.dumps(
                {"diary": ["一条"], "people": {}})}}]}).encode())

        # 注：fetch 会把每条 txt 截到 200 字，所以标记要落在 200 字以内
        marks = ["MK%d" % i + "x" * 196 for i in range(4)]
        _mk(db, marks)
        _ur.urlopen = _fake
        try:
            MD.run_target(d)
        finally:
            _ur.urlopen = real_open
        blob = "\n".join(sent)
        cnt = [blob.count(m) for m in marks]
        st = _json.load(open(statef, encoding="utf-8")) if os.path.exists(statef) else {}
        c1 = all(c == 1 for c in cnt) and st.get("since_seq") == 4

        # 单条超过预算：必须显式失败、且【不推进游标】
        # 预算压到 100 才能真的走到这条路径 —— fetch 把单条 txt 截到 200 字，
        # 单条渲染后约 250 字，500 的预算下它永远装得下。
        os.remove(db)
        _mk(db, ["Z" * 5000])
        for p in (statef, out):
            if os.path.exists(p):
                os.remove(p)
        d["max_input_chars"] = 100
        _ur.urlopen = _fake
        try:
            MD.run_target(d)
        finally:
            _ur.urlopen = real_open
        st2 = _json.load(open(statef, encoding="utf-8")) if os.path.exists(statef) else {}
        c2 = int(st2.get("since_seq") or 0) == 0

        (ok if (c1 and c2) else bad)(
            "R31 日记分批不漏记",
            "每条出现次数=%s 批=%d 游标=%s | 超预算不动游标=%s"
            % (cnt, len(sent), st.get("since_seq"), c2))
        for p in (db, out, statef, extra):
            if os.path.exists(p):
                os.remove(p)
    except Exception as e:
        bad("R31 日记分批", "%s: %s" % (type(e).__name__, str(e)[:140]))

    # R32: 作用域隔离 —— A 的配置不能改变 B 的回复 / 图片 / 记忆。
    #      「空列表 = 全部 bot」是历史语义，最容易意外全开；显式写 all 才是明确的全开。
    try:
        from mindscape_core import scope_hit, scope_list, scope_warn

        class _Ev:
            def __init__(self, sid):
                self.sid = sid
            def get_self_id(self):
                return self.sid

        A, B = "100000001", "100000002"
        only_a = [A]
        iso = (scope_hit(only_a, A) is True and scope_hit(only_a, B) is False)
        al = bool(scope_hit(["all"], A) and scope_hit(["ALL"], B)
                  and scope_hit(["全部"], B))
        empty = bool(scope_hit([], A) and scope_hit(None, B))     # 旧语义：空 = 全部
        norm = (scope_list([" a ", "", None, "b"]) == ["a", "b"])

        class _L:
            def __init__(self):
                self.msgs = []
            def warning(self, *a, **k):
                self.msgs.append(a)
        lg = _L()
        scope_warn(lg, "t", [], True)          # 空 + enabled → 必须警告
        scope_warn(lg, "t", ["all"], True)     # 显式 all → 不该警告
        scope_warn(lg, "t", [], False)         # 没开 → 不该警告
        warn_ok = (len(lg.msgs) == 1)

        # 回复侧：沉默 / 识图
        import mindscape_silence as _SL
        import mindscape_vision as _VS
        _st = type("S", (), {"si_on": True, "si_targets": only_a})()
        hk_say = (not _SL.SilenceMixin._si_hit(_st, _Ev(B))
                  and _SL.SilenceMixin._si_hit(_st, _Ev(A)))
        _vt = type("S", (), {"vs_on": True, "vs_targets": only_a})()
        hk_see = (not _VS.VisionMixin._vs_hit(_vt, _Ev(B))
                  and _VS.VisionMixin._vs_hit(_vt, _Ev(A)))

        # 图片侧：图库分类按 self_id 精确匹配，没配就是「不采」
        import mindscape_stickers as _SK
        _kt = type("S", (), {"s_c": {"targets": [{"self_id": A, "category": "catA"}]}})()
        hk_img = (_SK.StickersMixin._target_category(_kt, A) == "catA"
                  and _SK.StickersMixin._target_category(_kt, B) is None)

        # 记忆侧：日记文件也按 self_id 精确匹配
        import mindscape_recall as _RC
        _old = _RC._bot_entries
        _RC._bot_entries = lambda: [{"self_id": A, "diary": "/tmp/x.md"}]
        try:
            hk_mem = (_RC._diary_for(A) == "/tmp/x.md" and _RC._diary_for(B) == "")
        finally:
            _RC._bot_entries = _old

        good = (iso and al and empty and norm and warn_ok
                and hk_say and hk_see and hk_img and hk_mem)
        (ok if good else bad)(
            "R32 作用域隔离（A 的配置不影响 B）",
            "只给A=%s 显式all=%s 空=全部(旧)=%s 规范化=%s 空表告警=%s | 钩子级: 回复=%s 识图=%s 图片=%s 记忆=%s"
            % (iso, al, empty, norm, warn_ok, hk_say, hk_see, hk_img, hk_mem))
    except Exception as e:
        bad("R32 作用域", "%s: %s" % (type(e).__name__, str(e)[:140]))
    # R02: 路径穿越必须在入库前就被拒绝
    # R33: Markdown 记忆链的一致性 —— 三处「写到一半就退出」的后果。
    try:
        import json as _js2
        import urllib.request as _ur2
        import mindscape_diary as MD2

        work = os.path.join(HERE, "_sc_chain")
        if not os.path.isdir(work):
            os.makedirs(work)
        db = os.path.join(work, "m.db")
        out = os.path.join(work, "d.md")
        statef = os.path.join(work, "d.state.json")
        people = os.path.join(work, "d.people.md")
        for x in (db, out, statef, people, people + ".tmp"):
            if os.path.exists(x):
                os.remove(x)

        def _mk(marks):
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE messages (timestamp INT, sequence INT, data TEXT)")
            for i, t in enumerate(marks):
                payload = _js2.dumps({"user_id": "9", "group_id": "1",
                                      "message": [{"type": "text", "data": {"text": t}}]})
                con.execute("INSERT INTO messages VALUES (?,?,?)", (100 + i, i + 1, payload))
            con.commit()
            con.close()

        os.environ["_SC_CHAIN_KEY"] = "k"
        dd = {"source": {"db": db, "table": "messages",
                         "fields": {"time": "timestamp", "seq": "sequence", "data": "data"}},
              "llm": {"api_base": "http://127.0.0.1:9/v1", "api_key_env": "_SC_CHAIN_KEY",
                      "model": "x"},
              "batch": 40, "max_input_chars": 100000, "max_tokens": 50, "max_batches": 40,
              "targets": [{"output": out, "state": statef, "persona": "p",
                           "people": people}]}
        _real = _ur2.urlopen

        class _RR:
            def __init__(self, b):
                self._b = b
            def read(self):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fk(req, timeout=None):
            return _RR(_js2.dumps({"choices": [{"message": {"content": _js2.dumps(
                {"diary": ["一条"], "people": {"某人": "群友"}})}}]}).encode())

        # (a) 「日记写完、游标还没落盘」：删掉状态再跑一次，不能重复
        _mk(["QA%d" % i + "x" * 60 for i in range(3)])
        _ur2.urlopen = _fk
        try:
            MD2.run_target(dd)
            first = open(out, encoding="utf-8").read()
            os.remove(statef)                       # 模拟：游标没落盘就退出了
            MD2.run_target(dd)
            second = open(out, encoding="utf-8").read()
        finally:
            _ur2.urlopen = _real
        # 留意：3 条消息在同一批里，桩每批只回 1 条日记 —— 所以 - 一条 本来就只该有 1 行。
        # 要证的是「重跑不追加」：内容一模一样、且**只有 1 个批次标记**。
        once_ok = (first == second
                   and first.count("ms-seq:") == 1
                   and first.count("- 一条") == 1)

        # (b) 画像原子替换：不留 .tmp，且内容完整
        people_ok = (os.path.exists(people)
                     and not os.path.exists(people + ".tmp")
                     and "某人" in open(people, encoding="utf-8").read())

        # (c) 摘要：只重算「内容变过」的那天
        import mindscape_digest as DG2
        dg_out = os.path.join(work, "dig.digest.md")
        diary2 = os.path.join(work, "d2.md")
        past = "2020-01-0%d"
        for x in (dg_out, dg_out + ".state.json", diary2):
            if os.path.exists(x):
                os.remove(x)

        def _write_diary(extra):
            with open(diary2, "w", encoding="utf-8") as f:
                for i in (1, 2):
                    f.write("## " + (past % i) + " 10:00\n")
                    for k in range(3):
                        f.write("- 第%d天第%d条\n" % (i, k))
                    if i == 1 and extra:
                        f.write("- 补录的一条\n")

        calls = []

        def _fake_digest(llm, date, lines, mi, mt):
            calls.append(date)
            return "摘要-" + date

        _old_dl = DG2.digest_llm
        DG2.digest_llm = _fake_digest
        try:
            _write_diary(False)
            n1 = DG2.run_digest_target({"diary": diary2, "output": dg_out, "name": "t"}, {})
            n2 = DG2.run_digest_target({"diary": diary2, "output": dg_out, "name": "t"}, {})
            _write_diary(True)                       # 只改第 1 天
            n3 = DG2.run_digest_target({"diary": diary2, "output": dg_out, "name": "t"}, {})
        finally:
            DG2.digest_llm = _old_dl
        calls1 = calls[:n1]
        calls3 = calls[n1 + n2:]
        dig_ok = (n1 == 2 and n2 == 0 and n3 == 1
                  and calls3 == [past % 1])

        for x in (db, out, statef, people, people + ".tmp", dg_out,
                  dg_out + ".state.json", diary2):
            if os.path.exists(x):
                os.remove(x)
        os.rmdir(work)
        good = once_ok and people_ok and dig_ok
        (ok if good else bad)(
            "R33 记忆链一致性（重跑不重复 / 原子写 / 只重算变过的那天）",
            "重跑无重复=%s 画像原子写=%s 摘要:首次%d 无改%d 改一天后%d 重算的=%s | "
            "诊断: 首=%d 次=%d 条首=%d 条次=%d 标记=%s 相同=%s"
            % (once_ok, people_ok, n1, n2, n3, calls3,
               len(first), len(second), first.count("- 一条"), second.count("- 一条"),
               ("ms-seq:" in first), (first == second)))
    except Exception as e:
        bad("R33 记忆链一致性", "%s: %s" % (type(e).__name__, str(e)[:140]))
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
    # R34: 观测层必须真的量得出「多大 / 多久」。
    #      「回复变慢」这类问题没有数据就只能靠猜；而事后拿日志时间戳去拼
    #      「上一行到 Prepare to send」会被工具轮和多段发送打乱 ——
    #      这一层唯一的职责就是把体积和耗时变成一行可信的日志。
    try:
        import importlib as _il
        import mindscape_trace as TR
        _il.reload(TR)
        _log = []
        _real_logger = TR.logger
        TR.logger = types.SimpleNamespace(
            info=lambda msg, *a: _log.append(("INFO", msg % a)),
            warning=lambda msg, *a: _log.append(("WARN", msg % a)))

        class _Ev:
            """两个 event 故意做成**不同对象**（模拟真实框架里的克隆）：
            起始时刻只挂 extras 会静默丢记录，必须靠实例侧的 pending 表对上。"""

            def __init__(self):
                self._x = {}
                self.unified_msg_origin = "self::100000001::group::100000002"

            def set_extra(self, k, v):
                self._x[k] = v

            def get_extra(self, k, d=None):
                return self._x.get(k, d)

            def get_self_id(self):
                return "100000001"

            def get_group_id(self):
                return "100000002"

        try:
            inst = TR.TraceMixin()
            inst.setup(None)
            _log.clear()
            ev, ev2 = _Ev(), _Ev()          # ← 响应拿到的是另一个对象
            req = types.SimpleNamespace(
                system_prompt="提" * 300,
                contexts=[{"role": "user", "content": "字" * 100}],
                prompt="问" * 10, func_tool=None)
            resp = types.SimpleNamespace(
                usage=types.SimpleNamespace(input_other=100,
                                            input_cached=50, output=20))
            asyncio.run(inst.tr_measure_request(ev, req))
            asyncio.run(inst.tr_measure_response(ev2, resp))
            frag = _log[1][1] if len(_log) > 1 else ""
            normal = (len(_log) == 2
                      and _log[0][0] == "INFO" and "出站" in _log[0][1]
                      and "上下文=1条" in _log[0][1]
                      and _log[1][0] == "INFO" and "完成" in frag
                      and "sys=300字" in frag and "会话=0条/0字" in frag
                      and "工具=0" in frag and "耗时=" in frag
                      and "tok=100+50/20" in frag)
            # 阈值调到负数 → 同一条必须升级成 WARNING（线上就靠它 grep 慢轮）
            inst.tr_warn_ms = -1
            _log.clear()
            asyncio.run(inst.tr_measure_request(ev, req))
            asyncio.run(inst.tr_measure_response(ev2, resp))
            loud = (len(_log) == 2 and _log[1][0] == "WARN"
                    and "SLOW" in _log[1][1])
        finally:
            TR.logger = _real_logger
        (ok if (normal and loud) else bad)(
            "R34 观测层量得出「多大 / 多久」",
            "常规=%s 超阈值升级=%s | %s" % (normal, loud, frag[:88]))
    except Exception as e:
        bad("R34 观测层", "%s: %s" % (type(e).__name__, str(e)[:120]))
    # R35: 上下文补齐（群缓冲）——「只读尾部」必须与整读**等价**，定向性四种情形各自成句。
    #      缓冲是追加流（写端 2MB 自截断），整读纯属浪费；但省 IO 不能省语义。
    #      另外：这个模块**默认必须关** —— 它依赖框架补丁，没打补丁就打开会把
    #      「这条不是对你说的」当结论注入，比不注入更糟。
    try:
        import json as _json
        import time as _t
        import importlib as _il3
        import mindscape_groupctx as GC
        _il3.reload(GC)
        buf = os.path.join(HERE, "_sc_buf.jsonl")
        now = _t.time()
        with open(buf, "w", encoding="utf-8") as f:
            for i in range(3000):
                f.write(_json.dumps({
                    "ts": now - (i % 900),
                    "platform": "p",
                    "group": "111" if i % 3 == 0 else "222",
                    "who": "u%d" % i, "text": "t%d" % i,
                }, ensure_ascii=False) + chr(10))
        got = GC.gc_read_recent(buf, "p", "111", 15, 1800, 512 * 1024)
        ref = [r for r in (_json.loads(x) for x in open(buf, encoding="utf-8") if x.strip())
               if str(r.get("platform")) == "p" and str(r.get("group")) == "111"
               and now - float(r.get("ts") or 0) <= 1800][-15:]
        tail_ok = (got == ref) and len(got) == 15
        os.remove(buf)

        class At:
            qq = "100000001"

        class Reply:
            sender_id = "100000001"

        class _GE:
            def __init__(self, msgs=(), extra=None):
                self._m, self._x = list(msgs), dict(extra or {})

            def get_messages(self):
                return self._m

            def get_self_id(self):
                return "100000001"

            def get_extra(self, k, d=None):
                return self._x.get(k, d)

        h_at = GC.gc_head(_GE([At()]))
        h_reply = GC.gc_head(_GE([Reply()]))
        h_mention = GC.gc_head(_GE(extra={"wake_reason": "mention"}))
        h_other = GC.gc_head(_GE())
        head_ok = ("@ 了你本人" in h_at and "引用了你说过的话" in h_reply
                   and "提到了你的名字" in h_mention and "不是对你说的" in h_other)

        # R35b: 引用里的图必须说清是谁发的 —— 实测有人引用了她自己发的表情包，
        #       她回头对着自己的图说「诶，这不是火花花嘛~」（框架把引用里的图
        #       渲染进了本条正文，模型当成了新收到的图）。
        class Image:
            pass

        # 类名必须正好是 "Reply"（gc_quote 按类名认组件），所以用 type() 造
        ReplyOther = type("Reply", (), {"sender_id": "200000002", "chain": [Image()]})
        ReplyMine = type("Reply", (), {"sender_id": "100000001", "chain": [Image()]})

        q_mine = GC.gc_quote(_GE([ReplyMine()]))
        q_other = GC.gc_quote(_GE([ReplyOther()]))
        q_none = GC.gc_quote(_GE())
        quote_ok = (q_mine == (True, True, True) and q_other == (True, False, True)
                    and q_none == (False, False, False)
                    and "你自己发的" in GC.gc_quote_note(True)
                    and "别人发的" in GC.gc_quote_note(False)
                    and "不用对着它认图" in GC.gc_quote_note(True))

        # R35c: 只加系统提示压不住「有人给我发了张图」的直觉（2026-10-03 22:50 实测）
        #       → 请求正文里那行 quoted 标记必须被就地改写
        class _TP:
            def __init__(self, text):
                self.text = text

        class _Req:
            def __init__(self, parts):
                self.extra_user_content_parts = parts

        _p = [_TP("x"), _TP("[Image Attachment in quoted message: path /tmp/a.jpg]")]
        _n = GC.gc_quote_rewrite(_Req(_p), True)
        _p2 = [_TP("[Image Attachment in quoted message: path /tmp/b.jpg]")]
        class _Frozen:            # pydantic 冻结模型：属性不可写，只能换对象
            def __init__(self, text):
                object.__setattr__(self, "text", text)

            def __setattr__(self, k, v):
                raise AttributeError("frozen")

        _p3 = [_Frozen("[Image Attachment in quoted message: path /tmp/c.jpg]")]
        _n3 = GC.gc_quote_rewrite(_Req(_p3), True)
        rw_ok = (_n == 1 and "Image Attachment" not in _p[1].text
                 and "你自己" in _p[1].text
                 and GC.gc_quote_rewrite(_Req(_p2), False) == 0
                 and "Image Attachment" in _p2[0].text
                 and _n3 == 1 and "你自己" in _p3[0].text)
        inst = GC.GroupctxMixin()
        inst.setup(None)
        default_off = inst.gc_on is False
        good = tail_ok and head_ok and quote_ok and rw_ok and default_off
        (ok if good else bad)(
            "R35 群上下文补齐（尾读等价 / 定向性四态 / 引用图归属 / 正文改写 / 默认关）",
            "尾读等价=%s(%d条) 定向性=%s 引用图=%s 正文改写=%s 默认关=%s"
            % (tail_ok, len(got), head_ok, quote_ok, rw_ok, default_off))
    except Exception as e:
        bad("R35 群上下文补齐", "%s: %s" % (type(e).__name__, str(e)[:140]))

    # R38: 历史里的图也看得见 —— 主人 2026-10-04：「不管引用与否，真人都看得见图，
    #      能在我们这边优化的就在这边优化，不要指望用户端改」。
    #      缓冲补丁记 imgs（本地路径），唤醒时把「还活着」的图挂到 request.image_urls
    try:
        import importlib as _il8
        import tempfile as _tf8
        import time as _tm8
        import mindscape_groupctx as GC8
        _il8.reload(GC8)
        _fd, _live = _tf8.mkstemp(suffix=".jpg")
        os.close(_fd)
        _fd2, _old = _tf8.mkstemp(suffix=".jpg")
        os.close(_fd2)
        _now8 = _tm8.time()
        _recs8 = [
            {"who": "old", "uid": "9", "ts": _now8 - 9999, "imgs": [_old]},   # 窗口外 → 不要
            {"who": "A", "uid": "1", "ts": _now8 - 30, "text": "[图片]",
             "imgs": ["/nope/x.jpg", _live, _live]},                   # 不存在 + 重复
            {"who": "B", "uid": "2", "ts": _now8 - 10, "text": "嗯"},     # 没图
        ]
        import asyncio as _a8
        _loop8 = _a8.new_event_loop()

        def _run8(coro):
            return _loop8.run_until_complete(coro)

        _got8 = GC8.gc_history_images(_recs8, 300, 9, now=_now8)
        _Img8 = type("Image", (), {})
        _Plain8 = type("Plain", (), {})

        class _E8:
            def __init__(self, m):
                self._m = m

            def get_messages(self):
                return self._m

        _refs8 = [p for p, _w, _t in _got8]
        _res8 = (_run8(GC8.gc_resolve_ref(_live)),
                 _run8(GC8.gc_resolve_ref("/nope/x.jpg")),
                 _run8(GC8.gc_resolve_ref("file://" + _live)),
                 _run8(GC8.gc_resolve_ref("ftp://nope")))
        img_ok = (_refs8 == ["/nope/x.jpg", _live]           # 新的记录在前 + 去重 + 窗口外不要
                  and GC8.gc_history_images(_recs8, 300, 0, now=_now8) == []
                  and GC8.gc_history_images(_recs8, 300, 1, now=_now8)[0][0] == "/nope/x.jpg"
                  and _res8 == (_live, "", _live, "")          # 本地直接用 / 不存在的丢掉 / file:// 也认
                  and [p for p, _w, _t in GC8.gc_history_images(_recs8, 300, 9, now=_now8, sender="1")] == _refs8
                  and GC8.gc_history_images(_recs8, 300, 9, now=_now8, sender="7") == []   # 同人过滤
                  and GC8.gc_has_image(_E8([_Img8()]))
                  and not GC8.gc_has_image(_E8([_Plain8()])))
        with _tf8.TemporaryDirectory() as _dir8:
            _buf8 = os.path.join(_dir8, "context.jsonl")
            _chat8 = [
                {"ts": _now8 - 25, "who": "A", "uid": "1", "text": "[图片]", "imgs": [_live]},
                {"ts": _now8 - 20, "who": "B", "uid": "2", "text": "插了一句"},
                {"ts": _now8 - 1, "who": "A", "uid": "1", "text": "火花，聊聊别的"},
            ]
            with open(_buf8, "w", encoding="utf-8") as _fp8:
                for _item8 in _chat8:
                    _fp8.write(_json.dumps({**_item8, "platform": "p", "group": "g"},
                                           ensure_ascii=False) + chr(10))

            class _Event8:
                def get_self_id(self): return "bot"
                def get_sender_id(self): return "1"
                def get_platform_name(self): return "p"
                def get_group_id(self): return "g"
                def get_messages(self): return []
                def get_extra(self, key): return None

            _inst8 = GC8.GroupctxMixin()
            _inst8.gc_on, _inst8.gc_targets, _inst8.gc_path = True, ["bot"], _buf8
            _inst8.gc_count, _inst8.gc_window, _inst8.gc_tail = 15, 1800, 512 * 1024
            _inst8.gc_mark = False
            _inst8.gc_img_on, _inst8.gc_img_max = True, 1
            _inst8.gc_img_window, _inst8.gc_img_same = 120, True
            _req8 = types.SimpleNamespace(system_prompt="", image_urls=[])
            _run8(_inst8.gc_inject(_Event8(), _req8))
            prompt_ok = (_req8.image_urls == [_live]
                         and "[%s] A: [图片]" % _tm8.strftime("%H:%M:%S", _tm8.localtime(_now8 - 25))
                         in _req8.system_prompt
                         and "本条消息时间：" in _req8.system_prompt
                         and "距本条约" in _req8.system_prompt
                         and "没有明确指向" in _req8.system_prompt
                         and "与本条话题无关" in _req8.system_prompt)
        os.remove(_live)
        os.remove(_old)
        (ok if img_ok and prompt_ok else bad)(
            "R38 历史图挂载与回复关联（窗口 / 同人 / 秒级时间 / 不相关不谈图）",
            "候选=%r 解析=%r 注入=%s" % (_refs8, _res8, prompt_ok))
    except Exception as e:
        bad("R38 历史里的图带得动", "%s: %s" % (type(e).__name__, str(e)[:140]))

    # R39: 真 @ 点名 —— 名字/号 → QQ 的匹配（纯函数；真 At 段由 MessageEventResult.at 出）
    try:
        import importlib as _il9
        import mindscape_mention as MN
        _il9.reload(MN)
        _ms = [
            {"user_id": "111", "nickname": "芳芳", "card": ""},
            {"user_id": "222", "nickname": "starry", "card": "Starry★"},
            {"user_id": "333", "nickname": "小菲比", "card": "小菲比3号"},
        ]
        mn_ok = (MN.mn_match_member("1207436794", _ms) == ("1207436794", "")   # 直接给号
                 and MN.mn_match_member("111", _ms) == ("111", "芳芳")          # 号 + 有名单
                 and MN.mn_match_member("@芳芳", _ms) == ("111", "芳芳")        # 带 @ 前缀也认
                 and MN.mn_match_member("Starry★", _ms) == ("222", "Starry★")  # 群名片精确
                 and MN.mn_match_member("小菲比3号", _ms) == ("333", "小菲比3号")
                 and MN.mn_match_member("菲比", _ms) == ("333", "小菲比3号")    # 唯一部分匹配（群名片优先）
                 and MN.mn_match_member("", _ms) == ("", "")
                 and MN.mn_match_member("查无此人", _ms) == ("", ""))
        _ms2 = _ms + [{"user_id": "444", "nickname": "菲比酱", "card": ""}]
        mn_ok = mn_ok and MN.mn_match_member("菲比", _ms2) == ("", "")          # 歧义不猜
        (ok if mn_ok else bad)("R39 真 @ 点名（号 / 群名片 / 昵称 / 唯一部分匹配 / 歧义不猜）", "")
    except Exception as e:
        bad("R39 真 @ 点名", "%s: %s" % (type(e).__name__, str(e)[:140]))

    # R40: @ 要能挂在她这条回复的最前面（有正文就 @+正文；没正文就单发 @）
    #      工具自己发消息那种做法会让模型认为「事办完了」，群里只剩一个干巴巴的 @（实测三次）
    try:
        import importlib as _il10
        import mindscape_mention as MN2
        _il10.reload(MN2)
        _p = {}
        _now = 1000.0
        _p["s1"] = ("1207436794", "qwerty", _now)
        _a = MN2.mn_take_pending(_p, "s1", _now + 5)
        _b = MN2.mn_take_pending(_p, "s1", _now + 6)          # 取过就没了
        _p["s2"] = ("1", "x", _now)
        _c = MN2.mn_take_pending(_p, "s2", _now + MN2.MN_PENDING_TTL + 1)   # 过期丢掉
        _d = MN2.mn_take_pending(_p, "nope", _now)
        at_ok = (_a == ("1207436794", "qwerty") and _b is None and _c is None and _d is None)
        (ok if at_ok else bad)("R40 @ 排队（挂到回复前 / 取过即清 / 过期丢掉）",
                              "取=%r 再取=%r 过期=%r" % (_a, _b, _c))
    except Exception as e:
        bad("R40 @ 排队", "%s: %s" % (type(e).__name__, str(e)[:140]))
    # R36: 句尾去句号 —— 末尾留空、句中改逗号，且**绝不碰 ASCII 的「.」**
    #      （4.6 / 0+0 是版本号，剃了就出事故）；子选项空 = 关，不能沿用
    #      「空 = 全部 bot」那条旧语义，否则谁忘写一行全场的句号都没了。
    try:
        import importlib as _il4
        import mindscape_format as FM
        _il4.reload(FM)
        cases = [
            ("今天天气不错。", "今天天气不错"),
            ("A。B。", "A，B"),
            ("等等。。", "等等"),
            ("4.6 版本的银狼 0+0。", "4.6 版本的银狼 0+0"),
            ("没有句号的句子~", "没有句号的句子~"),
        ]
        wrong = [(a, FM.drop_period(a), b) for a, b in cases if FM.drop_period(a) != b]
        inst_off = FM.FormatMixin()
        inst_off.setup(None)
        off_by_default = (inst_off.f_np == [])
        _real_section = FM.cfg.section
        FM.cfg.section = lambda n, d=None: ({"no_period": ["100000001"]}
                                           if n == "format" else {})
        try:
            inst_on = FM.FormatMixin()
            inst_on.setup(None)
        finally:
            FM.cfg.section = _real_section
        scoped = (inst_on.f_np == ["100000001"])
        good = (not wrong) and off_by_default and scoped
        (ok if good else bad)(
            "R36 句尾去句号（留空 / 句中改逗号 / 不碰 ASCII 点 / 空=关）",
            "%d/%d 条用例 默认关=%s 按 bot 生效=%s | %s"
            % (len(cases) - len(wrong), len(cases), off_by_default, scoped,
               str(wrong)[:80]))
    except Exception as e:
        bad("R36 句尾去句号", "%s: %s" % (type(e).__name__, str(e)[:140]))
    # R37: 框架有两条报错出口**不走结果管线**（agent 异常 / LLM 构建失败，都是直接
    #      `event.send()`）—— 挂在 on_decorating_result 上的拦截一点机会都没有。
    #      实测漏过一次真实报错（群里收到一句英文 Error occurred while processing agent
    #      request: Failed to download file …）。这里给平台事件类的 send 包一层兜底，
    #      同时确认**不能误伤正常回复**、且重复装不会叠包。
    try:
        import importlib as _il5
        import types as _ty
        import mindscape_guard as GD
        _il5.reload(GD)
        sent = []
        plat = _ty.ModuleType("astrbot.core.platform.astr_message_event")

        class _Base:
            async def send(self, message):
                sent.append("base")

        class _Sub(_Base):
            async def send(self, message):
                sent.append("sub")
                return await super().send(message)

        plat.AstrMessageEvent = _Base
        _pkg = _ty.ModuleType("astrbot.core.platform")
        _old_plat = sys.modules.get("astrbot.core.platform.astr_message_event")
        _old_pkg = sys.modules.get("astrbot.core.platform")
        sys.modules["astrbot.core.platform"] = _pkg
        sys.modules["astrbot.core.platform.astr_message_event"] = plat

        class _Chain:
            def __init__(self, t):
                self._t = t
                self.chain = []

            def get_plain_text(self, *a, **k):
                return self._t

        try:
            n1 = GD.ms_install_send_guard(GD.is_error_text)
            n2 = GD.ms_install_send_guard(GD.is_error_text)
            ev = _Sub()
            leaked = ("Error occurred while processing agent request: "
                      "Failed to download file from https URL host='cdn.example.com' "
                      "file='raw300.gif' len=91. HTTP status code: 404")
            asyncio.run(ev.send(_Chain(leaked)))
            blocked = (sent == [])
            asyncio.run(ev.send(_Chain("嘻，直播间不供句号~")))
            passed = (sent == ["sub", "base"])
            good = (n1 >= 1 and n2 == 0 and blocked and passed)
        finally:
            if _old_plat is None:
                sys.modules.pop("astrbot.core.platform.astr_message_event", None)
            else:
                sys.modules["astrbot.core.platform.astr_message_event"] = _old_plat
            if _old_pkg is None:
                sys.modules.pop("astrbot.core.platform", None)
            else:
                sys.modules["astrbot.core.platform"] = _old_pkg
        (ok if good else bad)(
            "R37 send 级报错兜底（不走管线的那条路）",
            "包了%d类 重复装=%d 拦住=%s 正常放行=%s" % (n1, n2, blocked, passed))
    except Exception as e:
        bad("R37 send 级兜底", "%s: %s" % (type(e).__name__, str(e)[:140]))


# ── 6. 转义保真 ──
def check_escapes():
    """产物里「带反斜杠的字符串」必须与源码逐字一致。

    为什么要有这一条：源码里的正则一旦在编辑中丢了反斜杠，
    **AST 照样通过、构建照样成功**，但运行时语义已经变了 ——
    实测踩过：日期正则被写成没有反斜杠的版本，整段过滤失效，
    直到拿纯函数的真实返回值去查才发现。

    这里不猜语义，只做一件事：源码里每个含反斜杠的字符串常量，
    都必须在产物里原样出现。用 chr(92) 代替反斜杠，免得检查器自己踩这个坑。
    """
    section("6. 转义保真（防「AST 过、语义坏」）")
    out = os.path.join(HERE, "dist", "mindscape", "main.py")
    if not os.path.exists(out):
        bad("转义保真", "产物不存在，先跑 scripts/build_plugin.py")
        return
    # 只有【真正进产物】的模块才该在这里查：
    #   ORDER 里的模块会被合并；LOCAL_ONLY 的会被排除；
    #   其余（digest / style / forget 等）是独立脚本，压根不进产物。
    # 两个清单都从 build_plugin.py 现读，免得写死后失同步。
    order, skip = set(), set()
    try:
        bp = open(os.path.join(HERE, "scripts", "build_plugin.py"), encoding="utf-8").read()
        m = re.search(r"ORDER\s*=\s*\[(.*?)\]", bp, re.S)
        if m:
            order = set(re.findall(chr(34) + r"([a-z_]+)" + chr(34), m.group(1)))
        m = re.search(r"LOCAL_ONLY\s*=\s*\[([^\]]*)\]", bp)
        if m:
            skip = {x.strip().strip(chr(34)).strip(chr(39)) for x in m.group(1).split(",") if x.strip()}
    except Exception:
        pass
    if not order:
        bad("转义保真", "读不到 build_plugin.py 的 ORDER，检查会失真")
        return
    bundle = open(out, encoding="utf-8", errors="replace").read()
    total, miss, broke = 0, [], []
    for n in sorted(os.listdir(PLUGINS)):
        if not n.endswith(".py") or n[:-3] not in order or n[:-3] in skip:
            continue
        p = os.path.join(PLUGINS, n)
        try:
            tree = ast.parse(open(p, encoding="utf-8-sig").read())
        except Exception as e:
            broke.append("%s: %s" % (n, str(e)[:60]))
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            v = node.value
            if chr(92) not in v or not (2 <= len(v) <= 400):
                continue
            total += 1
            if v not in bundle:
                miss.append("%s: %r" % (n, v[:44]))
    # 「一个都没扫到」本身就是故障信号：检查没工作，比检查通过更像坏消息
    if total == 0:
        bad("转义保真", "一个带反斜杠的字符串都没扫到 —— 检查本身没工作；解析失败 %d 个 %s"
            % (len(broke), broke[:3]))
        return
    if broke:
        bad("转义保真 解析失败", str(broke[:3]))
    if miss:
        for x in miss[:8]:
            bad("转义丢失", x)
    else:
        ok("转义保真", "源码 %d 个带反斜杠的字符串，产物里逐字都在" % total)


# ── 7. 产物新鲜度 ──
def check_build_fresh():
    """dist 里的产物必须能由当前源码重建出来（逐字节一致）。

    为什么要有：产物是**提交进仓库的**，而真正跑的是产物。以前只证明「语法能过」，
    没有证明「产物 == 源码」—— `docs/deploy.md` 与实现悄悄分叉就是这么来的。
    这里按 `--market` 的口径在临时目录重建，再与现有产物比字节。
    """
    section("7. 产物新鲜度（源码能否重建出当前 dist）")
    out = os.path.join(HERE, "dist", "mindscape", "main.py")
    if not os.path.exists(out):
        bad("产物新鲜度", "dist/mindscape/main.py 不存在，先跑 scripts/build_plugin.py")
        return
    try:
        import contextlib as _cl
        import io as _io
        import tempfile
        import build_plugin as BP
        cur = open(out, "rb").read()
        tmp = tempfile.mkdtemp(prefix="_sc_build_")
        old = BP.OUT_DIR
        BP.OUT_DIR = tmp
        buf = _io.StringIO()
        try:
            with _cl.redirect_stdout(buf):
                BP.main(exclude=tuple(BP.LOCAL_ONLY))
        finally:
            BP.OUT_DIR = old
        fresh = open(os.path.join(tmp, "main.py"), "rb").read()   # OUT_DIR 已经是 …/mindscape
        if fresh == cur:
            ok("产物新鲜度", "重建结果与 dist 逐字节一致（%d 字节）" % len(cur))
        else:
            bad("产物新鲜度", "重建结果与 dist 不一致 —— 改了源码没重新构建？"
                              "（dist %d / 重建 %d 字节）" % (len(cur), len(fresh)))
    except Exception as e:
        bad("产物新鲜度", "%s: %s" % (type(e).__name__, str(e)[:120]))


def main():
    print("bot-mindscape 全套自检")
    print("仓库: " + HERE)
    check_syntax()
    check_config()
    check_functions()
    check_privacy()
    check_structure()
    check_escapes()
    check_build_fresh()
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
