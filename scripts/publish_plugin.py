# -*- coding: utf-8 -*-
"""把 dist/mindscape 的构建产物发布到 AstrBot 插件市场仓库。

为什么单独一个仓库：插件市场要求【仓库根目录就是插件】，
而主仓库是「框架源码 + 构建产物」混着的，直接丢进去不符合规范。

用法:
    python scripts/publish_plugin.py              构建 + 同步 main.py / 版本号
    python scripts/publish_plugin.py --push       同步完再 commit + push
    python scripts/publish_plugin.py --dir D:\\x   指定插件仓库位置
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), "astrbot_plugin_mindscape")


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _version():
    """从主仓库 README 顶部的 badge 猜不到，就读 dist 的 metadata；再不行用 0.1.0。"""
    p = os.path.join(HERE, "dist", "mindscape", "metadata.yaml")
    try:
        m = re.search(r"^version:\s*(\S+)", open(p, encoding="utf-8").read(), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.1.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR, help="插件仓库目录")
    ap.add_argument("--push", action="store_true", help="同步后提交并推送")
    ap.add_argument("--bump", default="", help="同时把版本号改成这个值，例如 0.2.0")
    a = ap.parse_args()

    dst = os.path.abspath(a.dir)
    if not os.path.isdir(os.path.join(dst, ".git")):
        print("[失败] 插件仓库不存在: " + dst)
        print("       先 git clone https://github.com/Illusory-moon/astrbot_plugin_mindscape")
        return 1

    # 1) 构建（构建脚本自己会做语法检查）
    code, out = _run([sys.executable, os.path.join(HERE, "scripts", "build_plugin.py")], HERE)
    if code != 0:
        print("[失败] 构建失败:\n" + out)
        return 1

    # 2) 同步 main.py
    src = os.path.join(HERE, "dist", "mindscape", "main.py")
    tgt = os.path.join(dst, "main.py")
    old = hashlib.md5(open(tgt, "rb").read()).hexdigest() if os.path.exists(tgt) else ""
    shutil.copy2(src, tgt)
    new = hashlib.md5(open(tgt, "rb").read()).hexdigest()
    print(("  [同步] main.py " + ("已更新 " if old != new else "无变化 ")) + new[:8])

    # 3) 版本号
    mf = os.path.join(dst, "metadata.yaml")
    txt = open(mf, encoding="utf-8").read()
    ver = a.bump or _version()
    txt2 = re.sub(r"^version:.*$", "version: " + ver, txt, count=1, flags=re.M)
    if txt2 != txt:
        open(mf, "w", encoding="utf-8").write(txt2)
        print("  [同步] 版本号 -> " + ver)

    # 4) 可选：提交推送
    if not a.push:
        print("完成（未推送）。加 --push 直接提交推送。")
        return 0
    for cmd in (["git", "add", "-A"],
                ["git", "commit", "-m", "chore: 同步 main.py (" + ver + ")"],
                ["git", "push"]):
        code, out = _run(cmd, dst)
        if code != 0 and "nothing to commit" not in out:
            print("[失败] " + " ".join(cmd) + "\n" + out)
            return 1
        print("  [git] " + " ".join(cmd[1:3]) + (" -> " + out.splitlines()[-1] if out else ""))
    print("已推送到插件仓库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
