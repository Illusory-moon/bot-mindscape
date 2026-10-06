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
TARGET_CANDIDATES = [
    "/opt/astrbot/.venv/lib/python3.13/site-packages/astrbot/core/pipeline/waking_check/stage.py",
    os.path.expanduser("~/astrbot/.venv/lib/python3.13/site-packages/astrbot/core/pipeline/waking_check/stage.py"),
]
MARK_BEGIN = "# ══ bot-mindscape auto-wake BEGIN ══"
MARK_END = "# ══ bot-mindscape auto-wake END ══"


def find_target(explicit=None):
    if explicit:
        return explicit
    for p in TARGET_CANDIDATES:
        if os.path.exists(p):
            return p
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
    """
    if "_ms_image_paths" in text or CTX_MARK not in text:
        return text
    i = text.find(CTX_MARK)
    j = text.find("@register_stage", i)
    if j == -1:
        return text
    new = text[:i] + read_block("_waking_ctx_block.py") + "\n\n\n" + text[j:]
    bak = path + ".bak-ctximg-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
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
    stop_anchor = "        if not is_wake:"
    if stop_anchor in text and text.count("_ms_record_ctx(event)") < 2:
        text = text.replace(
            stop_anchor + "\n            event.stop_event()",
            stop_anchor + "\n            _ms_record_ctx(event)\n            event.stop_event()",
            1,
        )

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
    d = os.path.dirname(path)
    baks = sorted([x for x in os.listdir(d) if x.startswith(os.path.basename(path) + ".bak-mindscape-")])
    if not baks:
        print("[失败] 找不到备份文件")
        return False
    latest = os.path.join(d, baks[-1])
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