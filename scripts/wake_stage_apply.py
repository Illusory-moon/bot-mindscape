# -*- coding: utf-8 -*-
"""wake_stage_apply —— 用「站点表」从底本重建线上那份唤醒补丁（2026-10-06 立）。

为什么用站点表而不是 patch 文件：
  · 线上那份 stage.py 的补丁是**手工内联**的 ✗，分散在 12 处；
  · Windows 上没有 `patch` 命令 ✗；
  · 于是把「底本原文 → 线上新文」（各带 3 行上下文，保证唯一）机械抽成 waking_sites.json ✓，
    用**纯字符串精确替换**重建 —— 逐字节可复现 ✓，不依赖任何外部工具 ✓。

用法：
    python scripts/wake_stage_apply.py build <底本> <站点表> <输出>
    python scripts/wake_stage_apply.py check <底本> <站点表> <线上副本>

底本来源（两处内容相同 ✓ 双份保险）：
  · 容器内 stage.py.bak-wakefix（md5 6018bd99a757cebcb8d3cbe378990cd7）
  · 宿主机 /opt/astrbot/.venv/.../waking_check/stage.py（同 md5）
"""
import hashlib
import json
import os
import sys


def md5_bytes(b):
    return hashlib.md5(b).hexdigest()


def load_text(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def build(base, sites_path, out):
    doc = json.load(open(sites_path, encoding="utf-8"))
    text = load_text(base)
    got = md5_bytes(open(base, "rb").read())
    if got != doc.get("base_md5"):
        print("[警告] 底本 md5 与站点表记录不符（%s != %s）—— 仍继续 ✓" % (got, doc.get("base_md5")))
    sites = doc.get("sites") or []
    # 按**底本行号切片拼接** ✓ —— 不做文本搜索、与顺序无关 ✓。
    # （2026-10-06 实测：用「原文匹配」会被相邻站的上下文互相踩 ✗，正序倒序都不行 ✗）
    lines = text.split("\n")
    res, pos = [], 0
    for i, s in enumerate(sites, 1):
        i1, i2 = int(s.get("i1", 0)), int(s.get("i2", 0))
        if i1 < pos or i2 < i1:
            print("[失败] 第 %d 站行号越界/重叠（%d..%d，当前游标 %d）" % (i, i1, i2, pos))
            return 1
        res.extend(lines[pos:i1])
        res.extend(s.get("new") or [])
        pos = i2
    res.extend(lines[pos:])
    text = "\n".join(res)
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    out_md5 = md5_bytes(open(out, "rb").read())
    want = doc.get("live_md5")
    print("[重建] %s（%d 字节）" % (out, os.path.getsize(out)))
    print("       md5 %s%s" % (out_md5, "  == 站点表记录的线上 md5 ✓✓" if out_md5 == want else "  != 记录的 %s ✗" % want))
    return 0 if out_md5 == want else 1
    # ↓ 下面是旧实现（文本匹配版 ✗）保留作注释，别再用
    # for i, s in enumerate(sites, 1):
    #     old, new = s.get("old") or "", s.get("new") or ""
    #     cnt = text.count(old)
    #     if cnt != 1:
    #         print("[失败] 第 %d 站匹配 %d 次（应为 1）" % (i, cnt))
    #         return 1
    #     text = text.replace(old, new, 1)
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    out_md5 = md5_bytes(open(out, "rb").read())
    want = doc.get("live_md5")
    print("[重建] %s（%d 字节）" % (out, os.path.getsize(out)))
    print("       md5 %s%s" % (out_md5, "  == 站点表记录的线上 md5 ✓✓" if out_md5 == want else "  != 记录的 %s ✗" % want))
    return 0 if out_md5 == want else 1


def check(base, sites_path, live):
    tmp = live + ".rebuilt"
    rc = build(base, sites_path, tmp)
    same = open(tmp, "rb").read() == open(live, "rb").read()
    os.remove(tmp)
    print("[一致] 线上 = 底本 + 站点表 ✓✓" if same else "[漂移] 线上与重建结果不一致 ✗")
    return 0 if (rc == 0 and same) else 1


def regen(base, live, sites_path):
    """改动线上之后，重新生成站点表 ✓（改完请顺手 build 一次确认 md5 ✓）。"""
    import difflib

    def rd(p):
        with open(p, encoding="utf-8", newline="") as f:
            return f.read().split("\n")

    a, b = rd(base), rd(live)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    sites = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        sites.append({"i1": i1, "i2": i2, "new": b[j1:j2], "kind": tag})
    doc = {
        "note": "AstrBot waking_check/stage.py 的唤醒补丁站点表（底本 → 线上）。按底本行号拼接重建 ✓ 与顺序无关 ✓",
        "base_md5": md5_bytes(open(base, "rb").read()),
        "live_md5": md5_bytes(open(live, "rb").read()),
        "base_lines": len(a),
        "live_lines": len(b),
        "sites": sites,
    }
    with open(sites_path, "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("[已生成] %s：站点 %d 个（底本 %s… → 线上 %s…）"
          % (sites_path, len(sites), doc["base_md5"][:8], doc["live_md5"][:8]))
    return 0


def main():
    if len(sys.argv) == 5 and sys.argv[1] in ("build", "check"):
        mode, base, sites_path, other = sys.argv[1:5]
        return build(base, sites_path, other) if mode == "build" else check(base, sites_path, other)
    if len(sys.argv) == 5 and sys.argv[1] == "regen":
        return regen(sys.argv[2], sys.argv[3], sys.argv[4])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
