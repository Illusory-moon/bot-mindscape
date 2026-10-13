# -*- coding: utf-8 -*-
"""R80 · 未决台账守卫（防「账本过期」）。

**为什么有它**（2026-10-13 真踩）：
权威文档里有**五处**长期写着「三条服务器侧小项待主人定」✗，而它们 **10-12 17:18 就做完了** ✓ ——
**结论没写回权威文档** ⇒ 把下一个 agent（本鱼）实打实误导了一轮 ✗。
这与「词表缺失静默通过 = 假绿」是**同一类病**：只打印、不分支、不核对 ✗。

**口径**（三条断言，全部真 if 分支）：
  ① 文档里出现的每个 `⟦待办:ID⟧` ⇒ 必须在台账里有条目（孤儿编号 = 红）
  ② 台账里 status=done 的条目 ⇒ 文档里**不许再声称它「未做」**（本病的正面判据）
  ③ 台账里 status=open 的条目 ⇒ 文档里**至少要出现一次**（偷偷做完了没销账 = 红）

⚠️ 它**只防口径漂移**，防不了「判断错」—— 别指望它包打天下 ✓
⚠️ 台账 `canon/pending.json` 在**工作区**里、不在本仓里 ⇒ 公开仓的
   自检不依赖它；找不到台账时此项**显式记「跳过」**（不许静默算通过 ✗）
"""
import glob
import json
import os
import re

MARK = "⟦待办:"
MARK_RE = re.compile("⟦待办:([A-Za-z0-9_\\-]+)⟧")
TAIL_WIN = 260                      # 编号必须挂在这一行、或它后面几行内

# 要守的文档：**用文件名 glob 指定**（工作区级的 canon/*.md 与根目录两份）
# ⚠️ 别在这里写死某个带私域词的文件名 ✗ —— 本仓是公开仓，自检会当场抓住 ✓
DOC_GLOBS = ("*.md", "canon/*.md", "server-inventory/*.md")
DOC_SKIP = {"pending.md"}           # 台账正文（如将来有 md 版）
LEDGER = os.path.join("canon", "pending.json")
OPEN_CLAIM = re.compile(r"待主人定|待主人裁|待主人拍板|等主人点|未做|未跑|没跑|尚未上线|未上线")

# 自称「还没做」的措辞 —— 台账里 status=done 的条目**不许**再带着它出现
DONE_WORDS = ("待主人定", "待主人裁", "待主人拍板", "等主人点", "未做", "未跑",
              "没跑", "尚未上线", "未上线", "均未动手", "未动手")


def workdir(repo_root):
    """工作区 = 本仓的**上一层**（本仓是工作区里的一个子目录）✓"""
    return os.path.dirname(os.path.abspath(repo_root))


def doc_files(root):
    """工作区里要守的文档清单（glob 得到，随文件增减自动跟上 ✓）"""
    out = []
    for pat in DOC_GLOBS:
        for fp in sorted(glob.glob(os.path.join(root, pat.replace("/", os.sep)))):
            rel = os.path.relpath(fp, root).replace("\\", "/")
            if os.path.basename(rel) in DOC_SKIP:
                continue
            out.append(rel)
    return out


def load_ledger(root):
    """读台账。返回 (items, err)：不存在 ⇒ (None, 说明)；格式坏 ⇒ (None, 错)"""
    fp = os.path.join(root, LEDGER)
    if not os.path.exists(fp):
        return None, "缺 %s（工作区级台账）" % LEDGER.replace("\\", "/")
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return None, "台账读不出来/不是合法 JSON：%s" % str(e)[:100]
    items = d.get("items")
    if not isinstance(items, list) or not items:
        return None, "台账里没有 items 列表"
    out = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            return None, "台账有条目缺 id"
        out[str(it["id"])] = it
    return out, None


def scan_docs(root):
    """扫文档，返回 {id: [(rel, lineno, 行文本)]}（编号挂在本行或其后 TAIL_WIN 字符内的行上）"""
    found = {}
    for rel in doc_files(root):
        fp = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(fp):
            continue
        lines = open(fp, encoding="utf-8", errors="replace").read().splitlines()
        for i, line in enumerate(lines):
            for m in MARK_RE.finditer(line):
                found.setdefault(m.group(1), []).append((rel, i + 1, line.strip()))
            if MARK in line:
                # 编号可能挂在下几行（条目换行写）—— 往后找一段
                buf = ""
                for j in range(i + 1, min(i + 4, len(lines))):
                    buf += lines[j] + "\n"
                    if len(buf) > TAIL_WIN:
                        break
                for m in MARK_RE.finditer(buf):
                    found.setdefault(m.group(1), []).append((rel, i + 1, line.strip()))
    return found


def check(repo_root, ok, bad, section):
    """供 run_selfcheck 调用：三条断言 + 两向都真的分支 ✓"""
    root = workdir(repo_root)
    section("R80 未决台账（防账本过期）")
    led, err = load_ledger(root)
    if led is None:
        # 台账不在 ⇒ 显式「跳过」（不许静默算通过 ✗）—— 公开仓裸克隆就该走这一支
        ok("R80 未决台账", "跳过（%s；本工作区/本机才有这份台账）" % err)
        return
    found = scan_docs(root)

    # ① 孤儿编号：文档里有、台账里没有
    orphans = sorted(set(found) - set(led))
    (ok if not orphans else bad)(
        "R80① 编号都在台账里",
        "文档里出现但台账没有的编号：%s" % ",".join(orphans) if orphans else
        "%d 个编号全部有主 ✓" % len(found))

    # ② 已 done 的条目，文档里不许再声称「未做」
    lying = []
    for tid, item in led.items():
        if str(item.get("status", "")).lower() != "done":
            continue
        for rel, ln, text in found.get(tid, []):
            if any(w in text for w in DONE_WORDS):
                lying.append("%s:%d（%s 已 done，却仍写着「%s」）"
                             % (rel, ln, tid, next(w for w in DONE_WORDS if w in text)))
    (ok if not lying else bad)(
        "R80② 已完成的没被写成「未做」",
        "；".join(lying) if lying else
        "%d 条 done 条目在文档里都已是完成口径 ✓" % sum(
            1 for it in led.values() if str(it.get("status", "")).lower() == "done"))

    # ③ open 的条目必须在文档里露面（偷偷做完没销账 = 红）
    ghosts = sorted(t for t, it in led.items()
                    if str(it.get("status", "")).lower() == "open" and t not in found)
    (ok if not ghosts else bad)(
        "R80③ open 条目不失踪",
        "台账标 open、文档里一次都没出现的编号：%s" % ",".join(ghosts) if ghosts else
        "%d 条 open 条目都在文档里露过面 ✓" % sum(
            1 for it in led.values() if str(it.get("status", "")).lower() == "open"))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    P, F = [], []
    import tempfile

    class _Shim:
        """命令行自跑（不依赖 run_selfcheck）：只为人工核对用 ✓"""
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check(_repo, lambda n, d="": (P.append(n), print("  [OK]   %s %s" % (n, d))),
          lambda n, d="": (F.append(n), print("  [FAIL] %s %s" % (n, d))),
          lambda t: print("\n== %s ==" % t))
    print("\n通过 %d 项 / 失败 %d 项" % (len(P), len(F)))
    sys.exit(1 if F else 0)
