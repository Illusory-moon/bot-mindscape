# -*- coding: utf-8 -*-
"""conflict_guard —— 耦合冲突检测（2026-10-16 立）

**为什么要它**（主人原话 2026-10-16）：
  「顺便给全仓做一个耦合冲突检测吧，不然以后这种 bug 反正我自己是找不到根因」

那天真踩的坑：`flatten_result`（decorating, priority=**900**）**先把 `|||` 抹掉**，
而 `mindscape_mention`（同一条链、优先级更低）**靠 `|||` 拆气泡** ⇒
它拿不到分隔符 → 回退到 `MN_LAST_TEXT`（原始 llm 正文，**没经过整形**）
⇒ **整形被绕过** ⇒ 群里发出带句尾句号的整句 ✗
⇒ 而且 `event.send()` 直发**不产生 `Prepare to send`** ⇒ **日志里搜不到** ⇒ 极难查 ✗

⇒ 本模块把那类**跨模块的隐式耦合**变成自检项 ✓
  —— 它们**单模块看都没问题**，只有把**整条钩子链**放一起才成立 ✓

四类检测（都来自本仓真实踩过的坑 ✓）：

  **CF1** 模块级钩子里用了 `self` —— 必 `NameError`，被外层 `try` 吞掉 ⇒ **静默失效** ✗
  **CF2** decorating 链的**顺序冲突** —— 高优先级钩子改掉了低优先级钩子依赖的形态 ✗
  **CF3** **绕过式回退** —— 从旧副本读文本 ⇒ 前面钩子的整形被跳过 ✗
  **CF4** **提示词与工具口径不一致** —— prompt 允许的值被工具拒掉 ⇒ 模型重发一遍 ✗

⚠️ 已知局限（写清楚，免得当成万能 ✗）：
  · 只做**文本层静态检查**，看不懂语义 ⇒ 会有漏报 ✓
  · CF2 的判据是「有改写者 + 有依赖者」⇒ **不能判断改写者是不是真的必须在那之前跑** ✗
    （所以它给的是**信号**，不是判决 ✓ 真出事还得靠 [msdiag] 那种运行时探针 ✓）

用法：
    import conflict_guard
    conflict_guard.check(HERE, ok, bad, section)
"""
import ast
import os
import re

LINE_SEP = "|||"

# 本仓 decorating 链的**已知优先级**（数值取自各模块源码 ✓ **大者先跑** ✓）
# 定序方式：日志实证（flatten_result 900 → sticker_use 850 → mention ✓）
KNOWN_DECORATING = [
    ("mindscape_silence", 1000, "看清空整条回复"),
    ("mindscape_guard", 999, "看清空整条回复（block_error）"),
    ("mindscape_format", 900, "改 chain 文本（压平 / 去句号）"),
    ("mindscape_sticker_use", 850, "往 chain 里追加图"),
    ("mindscape_mention", 300, "读 chain 文本 / 按 ||| 拆分"),
    ("mindscape_guard", 101, "口令拦截（pg_block_group_result，模块级钩子）"),
]

# 表达「我处理过句尾标点」的记号（CF2 用它区分「有人处理」vs「没人处理」✓）
PUNCT_CARE = ("drop_period", "MS_FORMAT_NP", "no_period", "PERIODS")


def _sources(repo_root):
    """读各插件模块源码。返回 {模块名: (源码, 路径)}"""
    for sub in ("plugins", os.path.join("bot-mindscape", "plugins"),
                os.path.join(repo_root, "plugins")):
        pdir = os.path.join(repo_root, sub) if not os.path.isabs(sub) else sub
        if os.path.isdir(pdir):
            out = {}
            for fn in sorted(os.listdir(pdir)):
                if fn.startswith("mindscape_") and fn.endswith(".py"):
                    p = os.path.join(pdir, fn)
                    with open(p, encoding="utf-8-sig") as f:
                        out[fn[10:-3]] = (f.read(), p)
            return out
    return {}


# ── CF1 ────────────────────────────────────────────────────────────────
# 模块级函数里出现 `self.xxx` ⇒ 那一定是**自由变量** ⇒ NameError ✗
# （真事故：本鱼在 `mn_attach_hook(*args, **kwargs)` 里写了 `self.f_np` ✓）
def check_cf1(srcs):
    """只查**被当钩子挂的模块级函数**里的自由 `self.`（AST 版 ✓ 不误伤注释/类方法 ✓）

    判据：
      ① 函数是**顶格 def**（模块级）
      ② 它的**最左参数不是 self** —— 是的话说明它本来就是「被当方法调」的（如 guard 的 send 包装）✓ 放过
      ③ 函数体（含嵌套函数）里出现对 `self` 的**裸名 Load** ⇒ 自由变量 ⇒ 运行时必炸 ✗
      ④ **例外**：若某个**内层函数显式声明了 `self` 参数**，那裸名 `self` 可能是**传进来的**
         （guard 的 `_sent(self, chain)` 就是这种 ✓）⇒ **放过**，免得误报 ✓
    """
    found = []
    for mod, (src, _p) in srcs.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:                       # 只看模块顶层 ✓
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            first = (a.posonlyargs + a.args)
            if first and first[0].arg == "self":
                continue                             # 显式 self 参数 ⇒ 合法 ✓
            # ④ 内层函数有显式 self 参数 ⇒ 本函数里裸名 self 视为「传进来的」✓
            inner_has_self = False
            for sub in ast.walk(node):
                if sub is node or not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                sa = (sub.args.posonlyargs + sub.args.args)
                if sa and sa[0].arg == "self":
                    inner_has_self = True
                    break
            if inner_has_self:
                continue
            hit = None
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "self" \
                        and isinstance(sub.ctx, ast.Load):
                    hit = getattr(sub, "lineno", node.lineno)
                    break
            if hit:
                found.append((mod, hit, node.name))
    return found


# ── CF2 ────────────────────────────────────────────────────────────────
def check_cf2(srcs):
    """① 有钩子在装饰链上**改文本**吗  ② 有低优先级钩子**依赖 |||** 吗
       ③ 依赖方**自己处理句尾标点**了吗（处理了 = 已知耦合、有兜底 ✓）"""
    writers, readers, punct_ok = [], [], []
    for mod, (src, _p) in srcs.items():
        for i, ln in enumerate(src.split("\n"), 1):
            if re.search(r"\bcomp\.text\s*=", ln):
                writers.append((mod, i))
            if re.search(r"LINE_SEP\s+not\s+in|LINE_SEP\s+in\s", ln):
                readers.append((mod, i))
        if any(k in src for k in PUNCT_CARE):
            punct_ok.append(mod)
    return {"writers": writers, "readers": readers, "punct_ok": sorted(set(punct_ok))}


# ── CF3 ────────────────────────────────────────────────────────────────
FALLBACK_RE = re.compile(r"(?:MN_LAST_TEXT|_LAST_TEXT|LAST_TEXT)\s*(?:\.\s*get|\[)")


def check_cf3(srcs):
    hits = []
    for mod, (src, _p) in srcs.items():
        for i, ln in enumerate(src.split("\n"), 1):
            if FALLBACK_RE.search(ln) and not ln.strip().startswith("#"):
                hits.append((mod, i, ln.strip()[:84]))
    return hits


# ── CF4 ────────────────────────────────────────────────────────────────
ZERO_RE = re.compile(r'"delta"\s*:\s*0')


def check_cf4(extra_srcs):
    """提示词允许 0 / 工具却把 0 当无效 —— 只看**同一条通路**里的矛盾 ✓"""
    hits = []
    for mod, src in extra_srcs.items():
        note_zero = bool(ZERO_RE.search(src))
        falsy = [ln.strip()[:78] for ln in src.split("\n")
                 if re.search(r"if\s+not\s+amount\s*:", ln)
                 and "return" not in ln]
        rejects = [ln.strip()[:78] for ln in src.split("\n")
                   if re.search(r"if\s+not\s+amount\s*:", ln)]
        if note_zero and rejects:
            # ⚠️ 只看「not amount」后面紧跟的那句是不是**报错话术**（要报数 = 拒绝）
            bad_style = any(("给我一个数" in src[max(0, src.find(r) - 0):src.find(r) + 120])
                            for r in rejects)
            if bad_style:
                hits.append({"mod": mod, "where": rejects,
                             "what": "提示词允许 delta=0，但工具把 0 当「没给数」拒掉（还回了报错话术）"})
    return hits


# ───────────────────────────────────────────────────────────────────────
def check(repo_root, ok, bad, section, extra_paths=None):
    """自检入口。repo_root = 工作区根（或仓库根，两者都认 ✓）"""
    srcs = _sources(repo_root)
    section("CF 耦合冲突检测（跨模块的隐式耦合 —— 单模块看都正常 ✗）")
    if not srcs:
        bad("CF 耦合冲突检测", "没找到 plugins/mindscape_*.py —— 检查 repo_root ✓")
        return

    # CF1
    try:
        f1 = check_cf1(srcs)
        if f1:
            bad("CF1 模块级函数里的自由 self（必 NameError ⇒ 被 try 吞 ⇒ 静默失效 ✗）",
                " / ".join("%s:%d %s" % x for x in f1[:3]))
        else:
            ok("CF1 模块级函数不用 self",
               "%d 个模块 ✓（真事故：mn_attach_hook 里写 self.f_np ⇒ 静默失效 ✗）" % len(srcs))
    except Exception as e:
        bad("CF1 模块级函数不用 self", "%s: %s" % (type(e).__name__, str(e)[:110]))

    # CF2
    try:
        r = check_cf2(srcs)
        if r["writers"] and r["readers"]:
            who_r = sorted({m for m, _ in r["readers"]})
            covered = [m for m in who_r if m in r["punct_ok"]]
            detail = ("改写者=%s ｜ %s 依赖者=%s ｜ 依赖方自处理标点=%s"
                      % ([m for m, _ in r["writers"]], LINE_SEP, who_r, covered or "无"))
            if covered:
                ok("CF2 decorating 链顺序耦合（已登记 · 依赖方有兜底）", detail)
            else:
                bad("CF2 decorating 链顺序冲突（依赖方没有兜底 ⇒ 会绕回旧副本 ✗）", detail)
        else:
            ok("CF2 decorating 链顺序耦合",
               "没有「改写者 + %s 依赖者」组合 ✓" % LINE_SEP)
    except Exception as e:
        bad("CF2 decorating 链顺序耦合", "%s: %s" % (type(e).__name__, str(e)[:110]))

    # CF3
    try:
        f3 = check_cf3(srcs)
        if f3:
            ok("CF3 绕过式回退（已登记 ⇒ 改动这些地方时要自查 ✗）",
               " / ".join("%s:%d" % (m, i) for m, i, _ in f3[:4])
               + " —— 读的是**旧副本**，前面钩子的整形会被跳过 ✓")
        else:
            ok("CF3 绕过式回退", "没有从旧副本回退读文本的地方 ✓")
    except Exception as e:
        bad("CF3 绕过式回退", "%s: %s" % (type(e).__name__, str(e)[:110]))

    # CF4（wishonomy 的源码不在本仓 ⇒ 由调用方给路径 ✓）
    try:
        extra = {}
        for p in (extra_paths or []):
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8-sig") as f:
                    extra[os.path.basename(os.path.dirname(p))] = f.read()
        f4 = check_cf4(extra)
        if f4:
            h = f4[0]
            bad("CF4 提示词与工具口径一致", "%s：%s ｜ %s" % (h["mod"], h["what"], h["where"][:60]))
        else:
            ok("CF4 提示词与工具口径一致",
               ("查了 %d 个外部插件 ✓" % len(extra)) if extra else "没给外部插件路径 ⇒ 跳过 ✓")
    except Exception as e:
        bad("CF4 提示词与工具口径一致", "%s: %s" % (type(e).__name__, str(e)[:110]))
