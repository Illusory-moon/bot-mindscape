# -*- coding: utf-8 -*-
"""mindscape_waking_install —— 给 AstrBot 打「auto-wake 策略」补丁

背景：AstrBot 原生只支持 wake_prefix 和 @bot。
      「提到名字必回 + 低概率冒泡 + 每 bot 独立配置」需要改框架的唤醒阶段。
      本脚本安全地插入代码块：先备份，能识别是否已打过补丁。

用法：
    python patches/astrbot/install.py              # 打补丁
    python patches/astrbot/install.py --revert     # 还原
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
    # R07: 两个代码块都要在，才算完整安装
    return (MARK_BEGIN in text) and ("bot-mindscape: 唤醒判定" in text)


def patch(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if already_patched(text):
        print("[跳过] 已经打过补丁：" + path)
        return True

    cfg_block = read_block("_waking_config_block.py")
    judge_block = read_block("_waking_judge_block.py")

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