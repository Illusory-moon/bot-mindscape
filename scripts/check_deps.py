# -*- coding: utf-8 -*-
"""依赖自检：装到哪个 Python、缺哪个包、各自用在哪。

用法：
    python scripts/check_deps.py          # 人看
    python scripts/check_deps.py --json   # 机器看（给 AI / CI）

为什么要有这个：插件的第三方 import 大多是**函数内延迟 import** ✗
⇒ 缺包时不会在启动报错，而是「某个功能安静地不工作」✗
⇒ 新手最容易踩的是「pip 装到了另一个解释器」，这条命令一次就能看出 ✓
"""
import argparse
import importlib.util
import json
import os
import platform
import sys

# 包名 -> (import 名, 用途, 是否可选)
DEPS = [
    ("PyYAML", "yaml", "配置读写（所有模块与脚本）", False),
    ("httpx", "httpx", "视觉判定 / 选图请求（sticker / vision / rescue）", False),
    ("Pillow", "PIL", "大图与动图预览（vision / stickers）", False),
    ("paramiko", "paramiko", "管理台「同步到服务器 / 观测」（不做远端同步就不需要）", True),
    ("tzdata", "tzdata", "时区库（Windows 原生 Python / slim 镜像常缺，缺了 ZoneInfo 会抛错）", True),
]


def probe(import_name):
    try:
        spec = importlib.util.find_spec(import_name)
    except (ImportError, ValueError, ModuleNotFoundError):
        return None
    if spec is None:
        return None
    # tzdata 是数据包，能找到就算装上了
    return getattr(spec, "origin", None) or "found"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = []
    for pkg, imp, use, optional in DEPS:
        where = probe(imp)
        rows.append({
            "package": pkg,
            "import": imp,
            "ok": where is not None,
            "optional": optional,
            "use": use,
            "origin": where,
        })

    if a.json:
        print(json.dumps({
            "python": sys.executable,
            "version": platform.python_version(),
            "platform": platform.platform(),
            "deps": rows,
            "missing_required": [r["package"] for r in rows if not r["ok"] and not r["optional"]],
            "missing_optional": [r["package"] for r in rows if not r["ok"] and r["optional"]],
        }, ensure_ascii=False, indent=2))
        return 0 if not any(not r["ok"] and not r["optional"] for r in rows) else 1

    print("Python 解释器 : %s" % sys.executable)
    print("版本          : %s" % platform.python_version())
    print("平台          : %s" % platform.platform())
    if sys.version_info < (3, 11):
        print("⚠️  需要 Python 3.11+（当前 %d.%d）" % sys.version_info[:2])
    print()
    print("%-10s %-6s %-8s %s" % ("包", "状态", "类型", "用途"))
    print("-" * 78)
    bad = []
    for r in rows:
        state = "✓ 已装" if r["ok"] else ("✗ 缺" if not r["optional"] else "- 未装(可选)")
        kind = "必需" if not r["optional"] else "可选"
        print("%-10s %-6s %-8s %s" % (r["package"], state, kind, r["use"]))
        if not r["ok"] and not r["optional"]:
            bad.append(r["package"])
    print()
    if bad:
        print("缺少必需依赖：%s" % ", ".join(bad))
        print("→ 就在【上面那个解释器】里装：")
        print("   %s -m pip install -r requirements.txt" % sys.executable)
        print("→ 如果 AstrBot 跑在 Docker 里，宿主机装它看不到 ✗：")
        print("   docker exec -it <容器名> pip install %s" % " ".join(bad))
        return 1
    print("必需依赖齐全 ✓" + ("（可选项缺了只影响对应功能）" if any(not r["ok"] for r in rows) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
