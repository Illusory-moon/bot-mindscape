# -*- coding: utf-8 -*-
"""mindscape_waking_install —— 给 AstrBot 打「auto-wake 策略」补丁

背景：AstrBot 原生只支持 wake_prefix 和 @bot。
      「提到名字必回 + 低概率冒泡 + 每 bot 独立配置」需要改框架的唤醒阶段。
      本脚本安全地插入代码块：先备份，能识别是否已打过补丁。

用法：
    python patches/astrbot/install.py              # 打补丁
    python patches/astrbot/install.py --revert     # 还原

⚠️⚠️ 2026-10-06：**请在容器内跑**，或改完 `docker cp` 送进容器 ✓。
    容器只挂载了 `data/` 等目录，**`.venv` 不在挂载里** ✗ —— 宿主机上的那份 stage.py
    与容器内的是**两个不同文件**（2026-10-06 实测：宿主机 12432 字节 / 没打过补丁 ✗，
    容器内 21572 字节 / 手写内联版 ✓）。在本脚本里直接跑，很可能改的是一份**没人加载**的文件 ✗。
    改完务必自检：`python scripts/wake_stage_check.py <从容器取回的 stage.py>` ✓（见 README ✓）。
"""
import argparse
import datetime
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 2026-10-27 修（P2-14）：原来把 `python3.13` 与 `~/astrbot` **写死**在候选路径里 ⇒
#   别人的 venv 是 3.11/3.12、或者装在别处，就一个都命中不到 ✗
#   ⇒ 改成**扫 site-packages**：先看常见安装根，再递归找 `*/astrbot/core/pipeline/waking_check/stage.py`
_SITE_ROOTS = [
    "/opt/astrbot",
    os.path.expanduser("~/astrbot"),
    os.path.expanduser("~/.astrbot"),
    "/app",
]
TARGET_CANDIDATES = [
    "/opt/astrbot/.venv/lib/python3.13/site-packages/astrbot/core/pipeline/waking_check/stage.py",
    os.path.expanduser("~/astrbot/.venv/lib/python3.13/site-packages/astrbot/core/pipeline/waking_check/stage.py"),
]
MARK_BEGIN = "# ══ bot-mindscape auto-wake BEGIN ══"
MARK_END = "# ══ bot-mindscape auto-wake END ══"

_TAIL = os.path.join("astrbot", "core", "pipeline", "waking_check", "stage.py")


def _scan_targets():
    """在常见安装根下找 stage.py（深度有限，别把整盘扫一遍 ✓）。"""
    found = []
    for root in _SITE_ROOTS:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            depth = cur[len(root):].count(os.sep)
            if depth > 6:
                dirs[:] = []
                continue
            if "stage.py" in files and cur.replace("\\", "/").endswith("waking_check"):
                found.append(os.path.join(cur, "stage.py"))
    return found


def find_target(explicit=None):
    if explicit:
        return explicit
    for p in TARGET_CANDIDATES:
        if os.path.exists(p):
            return p
    # 2026-10-27（P2-14）：写死的候选都不中 ⇒ 再扫一遍常见安装根 ✓
    hits = _scan_targets()
    if hits:
        return hits[0]
    return None


def read_block(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def already_patched(text):
    # R07: 三个代码块都要在，才算完整安装
    return (MARK_BEGIN in text) and ("bot-mindscape: 唤醒判定" in text) and ("_ms_render_chain" in text)


CTX_MARK = "# ── bot-mindscape: 群聊上下文缓冲（install.py 插在模块级）──"


def upgrade_ctx_block(path, text):
    """老版（不带图片路径）的缓冲块 → 新版。

    为什么需要这个：already_patched() 只认「打过没打过」，老容器升级补丁时会被
    一句「已打过」骗过去，缓冲里永远不会有 imgs。这里按块首注释定位、整块替换。

    ⚠️ 2026-10-27 修（P1-8）：
      ① 哨兵原来写的是 `_ms_image_paths`，而函数实际叫 `_ms_image_refs`
         ⇒ 条件**恒真** ⇒ 每次重跑都「[更新]」+ 写盘 + 多一份 .bak-ctximg-* ✗
      ② 原来**先写盘、后没验**：替换后不 compile 就落盘，出问题也回不去 ✗
         ⇒ 现在先在内存里拼好 + compile() 通过，才备份、才写一次 ✓
    """
    if "_ms_image_refs" in text or CTX_MARK not in text:
        return text
    i = text.find(CTX_MARK)
    j = text.find("@register_stage", i)
    if j == -1:
        print("[跳过] 找到旧缓冲块，但找不到块尾锚点（@register_stage）—— 未改动")
        return text
    new = text[:i] + read_block("_waking_ctx_block.py") + "\n\n\n" + text[j:]
    try:
        compile(new, path, "exec")          # ① 先验：语法不过就一个字节都不写 ✗
    except SyntaxError as exc:
        print("[失败] 升级后的文件语法不过（第 %s 行：%s）—— 未改动任何文件"
              % (exc.lineno, exc.msg))
        return text
    bak = path + ".bak-ctximg-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)                        # ② 只写这一次
    print("[更新] 群聊上下文缓冲块 → 带图片路径版（备份 " + bak + "）")
    return new


def patch(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = upgrade_ctx_block(path, text)
    if already_patched(text):
        print("[跳过] 已经打过补丁：" + path)
        return True

    cfg_block = read_block("_waking_config_block.py")
    judge_block = read_block("_waking_judge_block.py")
    ctx_block = read_block("_waking_ctx_block.py")

    # 0) 模块级：群聊上下文缓冲（渲染消息链 + 落盘）
    #    必须先插，因为下面两处调用点要用到它。
    if "_ms_render_chain" not in text:
        m0 = re.search(r"^@register_stage", text, re.M)
        if not m0:
            print("[失败] 找不到模块级插入点：@register_stage")
            return False
        text = text[:m0.start()] + ctx_block + "\n\n\n" + text[m0.start():]

    # 1) 在 __init__ 里、group_auto_wake 那行后面插入配置块
    #    2026-10-27 修（P1-8）：半套状态（缓冲块在、配置块只插了半截）重跑会插出两组
    #    `self.auto_wake_cfg` —— 语法仍合法、但行为错 ✗ ⇒ 已有 MARK_BEGIN 就直接跳过 ✓
    if MARK_BEGIN in text:
        print("[跳过] 配置块已存在（MARK_BEGIN 命中），不重复插入")
    else:
        m = re.search(r"^(\s*)self\.group_auto_wake\s*=.*$", text, re.M)
        if not m:
            print("[失败] 找不到插入点：self.group_auto_wake")
            print("       框架版本可能不同，请手工参考 patches/astrbot/README.md")
            return False
        indent = m.group(1)
        insert_at = m.end()
        indented = MARK_BEGIN + "\n" + "\n".join(
            (indent + ln) if ln.strip() else ln for ln in cfg_block.splitlines()
        ) + "\n" + MARK_END + "\n"
        text = text[:insert_at] + "\n" + indented + text[insert_at:]

    # 2) 在唤醒判定处插入 judge 块（插在 group_auto_wake 使用处之前）
    # 判定块必须插在「唤醒失败则停止事件」之前
    m2 = re.search(r"^(\s*)if\s+not\s+is_wake\s*:.*$", text, re.M)
    if not m2:
        print("[失败] 找不到判定锚点（if not is_wake:）；文件未做任何修改")
        print("       框架版本可能不同，请参考 patches/astrbot/README.md 手工插入")
        return False
    ind = m2.group(1)
    j_indented = "\n".join(
        (ind + ln) if ln.strip() else ln for ln in judge_block.splitlines()
    )
    text = text[:m2.start()] + j_indented + "\n" + text[m2.start():]

    # 3) 落盘调用点：每条走到这里的群消息都记一份（含未唤醒的）
    anchor = 'event.set_extra("activated_handlers", activated_handlers)'
    if anchor in text and "_ms_record_ctx(event)" not in text:
        text = text.replace(
            anchor,
            '_ms_record_ctx(event)  # 未唤醒的消息也要进上下文缓冲' + "\n        " + anchor,
            1,
        )
    # 2026-10-27 修（P2-17）：原来 `stop_anchor` 把 `if not is_wake:` 连同**8 个空格**写死，
    #   而且替换不到时**什么都不做就继续** ✗ ⇒ 框架缩进一变，就少插一个 `_ms_record_ctx`：
    #   未唤醒的消息进不了群缓冲，groupctx 的「定向性」变差，而且**没有任何报错** ✓
    #   ⇒ 现在分组捕获缩进、按捕获到的缩进插入；**替换失败直接返回 False** ✓
    m_stop = re.search(r"^(\s*)if\s+not\s+is_wake\s*:\s*$", text, re.M)
    if m_stop is None:
        print("[失败] 找不到落盘锚点（if not is_wake:）—— 未改动任何文件")
        return False
    if text.count("_ms_record_ctx(event)") < 2:
        ind2 = m_stop.group(1)
        head = m_stop.group(0)
        tail_pat = re.compile(re.escape(head) + r"\n(\s*)event\.stop_event\(\)")
        m_tail = tail_pat.search(text)
        if m_tail is None:
            print("[失败] 找到 `if not is_wake:` 但后面没有 event.stop_event() —— 未改动任何文件")
            return False
        _nl = chr(10)
        text = (text[:m_tail.start()] + head + _nl + ind2 + "    _ms_record_ctx(event)"
                + _nl + m_tail.group(0)[len(head) + 1:] + text[m_tail.end():])

    # R07: 先编译检查生成结果，通过后才备份并落盘
    try:
        compile(text, path, "exec")
    except SyntaxError as e:
        print("[失败] 补丁后语法错误（第 %s 行: %s），已放弃，原文件未改动"
              % (e.lineno, e.msg))
        return False

    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path + ".bak-mindscape-" + ts)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("[完成] 已打补丁：" + path)
    print("       备份：" + path + ".bak-mindscape-" + ts)
    return True


def revert(path):
    """从备份还原。

    ⚠️ 2026-10-27 修（P1-8）：原来只 glob `*.bak-mindscape-*`，**不认** `.bak-ctximg-*`
      ⇒ 若只发生过「缓冲块升级」，revert 会说"找不到备份"；而主备份又是在升级**之后**
      才拷的 ⇒ 那种情况下"还原"会回到半补丁态 ✗
      ⇒ 现在两类备份都认，按**修改时间**取最新的一份 ✓
    """
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    cands = []
    for x in os.listdir(d):
        if not x.startswith(base + ".bak-"):
            continue
        if ".bak-mindscape-" in x or ".bak-ctximg-" in x:
            cands.append(os.path.join(d, x))
    if not cands:
        print("[失败] 找不到备份文件（.bak-mindscape-* / .bak-ctximg-* 都没有）")
        return False
    latest = max(cands, key=lambda p: os.path.getmtime(p))
    shutil.copy2(latest, path)
    print("[完成] 已从备份还原：" + latest)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="stage.py 路径（默认自动探测）")
    ap.add_argument("--revert", action="store_true", help="从备份还原")
    args = ap.parse_args()

    path = find_target(args.target)
    if not path:
        print("[失败] 找不到 AstrBot 的 waking_check/stage.py")
        print("       请用 --target 指定路径")
        return 1
    print("目标文件：" + path)
    ok = revert(path) if args.revert else patch(path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())