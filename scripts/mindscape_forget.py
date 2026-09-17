# -*- coding: utf-8 -*-
"""mindscape_forget —— 记忆手术：从日记里精确删掉被污染的条目

为什么需要：日记是「外部输入喂出来的」，里面难免混进错的东西 ——
别人一句口嗨被记成了事实、某次误判被反复引用…… 一旦进了长期记忆，
它就每轮都在影响 bot。整份清空代价太大，所以要能**只摘掉那几条**。

为什么删掉就安全了：日记是**追加写入**的，进度由 state 文件里的游标决定，
而游标早就越过这些消息了 —— 删掉的行不会被重新生成，也不会打乱别的条目。

默认只预览（dry-run），加 --apply 才真正删除，且会先留一份带时间戳的备份。

用法:
    python scripts/mindscape_forget.py --diary <日记> --match "<关键词>" [--apply]
    python scripts/mindscape_forget.py --diary <日记> --line 2309 [--apply]
"""
import argparse
import datetime
import os
import shutil
import sys


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diary", required=True, help="日记文件路径")
    ap.add_argument("--match", default="", help="删掉包含这个关键词的条目行")
    ap.add_argument("--line", type=int, default=0, help="按行号删（1 起）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    a = ap.parse_args()

    if not a.match and not a.line:
        print("要么给 --match，要么给 --line，否则会把整个文件选中。")
        return 1
    if not os.path.exists(a.diary):
        print("找不到日记：" + a.diary)
        return 1

    lines = load(a.diary)
    hits = []
    for i, l in enumerate(lines, 1):
        if a.line:
            if i == a.line:
                hits.append(i)
        elif a.match and a.match in l and l.startswith("- "):
            hits.append(i)

    if not hits:
        print("没有匹配到任何条目行。")
        return 0

    print("将删除 %d 行：" % len(hits))
    head = ""
    for i in hits:
        if i >= 2 and lines[i - 2].startswith("## "):
            head = lines[i - 2][3:]
        print("  %5d [%s] %s" % (i, head, lines[i - 1].strip()[:120]))

    if not a.apply:
        print()
        print("（预览模式，什么都没改。确认无误后加 --apply）")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = a.diary + ".bak-" + stamp
    shutil.copy2(a.diary, bak)
    keep = [l for i, l in enumerate(lines, 1) if i not in set(hits)]
    tmp = a.diary + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(keep) + "\n")
    os.replace(tmp, a.diary)
    with open(a.diary + ".forgotten.log", "a", encoding="utf-8") as f:
        f.write("[%s] 删除 %d 行: %s\n"
                % (stamp, len(hits), "; ".join(lines[i - 1].strip()[:80] for i in hits)))
    print()
    print("已删除 %d 行。备份：%s" % (len(hits), os.path.basename(bak)))
    print("留痕：%s" % os.path.basename(a.diary + ".forgotten.log"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
