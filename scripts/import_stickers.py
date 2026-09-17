# -*- coding: utf-8 -*-
"""mindscape_import_stickers —— 多源图库导入（独立工具，不依赖 bot 框架）

支持：
  1. 本地目录 —— 批量入库，可选调视觉模型打标签
  2. 已有索引 —— 合并别的图库索引

用法：
    python scripts/import_stickers.py ./my_pics --category bot-name
    python scripts/import_stickers.py ./my_pics --category bot-name --no-ai --tags 开心,可爱
    python scripts/import_stickers.py other_index.json --format index --img-dir ./other_pics --category bot-name

安全说明：
  - 只接受扁平文件名，拒绝 ../、绝对路径、盘符
  - 源图与目标都在图库目录内（解析符号链接后校验）
  - 索引用「原子替换 + 文件锁」更新，避免与在线采集互相覆盖
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "plugins"))

from mindscape_core import (IMG_EXT, DEFAULT_PROMPT, load_index, safe_name,
                            save_index, verdict_ok)

try:
    import mindscape_config as cfg
except Exception:
    cfg = None

LOCK_SUFFIX = ".lock"


class IndexLock:
    """最简跨进程锁（O_EXCL 创建锁文件）。

    只用于串行化「读索引 → 合并 → 写索引」，不是高性能锁。
    拿不到锁就明确失败，绝不带着旧快照往下写。
    """

    def __init__(self, path, timeout=5.0):
        self.lock = path + LOCK_SUFFIX
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        import time
        t0 = time.time()
        while True:
            try:
                self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() - t0 > self.timeout:
                    raise RuntimeError(
                        "索引正被另一个进程使用（%s）。请等采集结束再导入。" % self.lock)
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass
        try:
            os.remove(self.lock)
        except Exception:
            pass
        return False


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    if cfg:
        return os.path.join(os.path.dirname(cfg.config_path()), path)
    return os.path.join(HERE, path)


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _judge(path, jcfg):
    """调视觉模型打标签。返回 verdict dict、False（明确不相关）或 None（失败）。"""
    import base64
    import re
    try:
        import httpx
    except ImportError:
        print("    [错误] 缺少 httpx：pip install httpx（或加 --no-ai）")
        return None
    api_base = (jcfg.get("api_base") or "").rstrip("/")
    key = os.environ.get(jcfg.get("api_key_env") or "", "")
    if not api_base or not key:
        print("    [错误] 未配置 judge.api_base 或环境变量 %s" % (jcfg.get("api_key_env") or "?"))
        return None
    prompt = (jcfg.get("prompt") or DEFAULT_PROMPT).replace(
        "{persona}", jcfg.get("persona") or "二次元角色")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    low = path.lower()
    mime = "image/gif" if low.endswith(".gif") else ("image/png" if low.endswith(".png") else "image/jpeg")
    try:
        r = httpx.post(
            api_base + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={"model": jcfg.get("model") or "gpt-4o-mini",
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": prompt},
                      {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}]}],
                  "max_tokens": int(jcfg.get("max_tokens") or 2000)},
            timeout=120)
        msg = r.json()["choices"][0]["message"]
        txt = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    except Exception as e:
        print("    [警告] 请求失败: " + str(e)[:80])
        return None
    from mindscape_core import parse_verdict
    v = parse_verdict(txt)
    if v is None:
        return None
    if not v.get("related"):
        return False
    return v if verdict_ok(v) else None


def _entry(fname, category, name, tags, desc):
    return {
        "file": fname,
        "category": category,
        "name": str(name or "未命名")[:12],
        "tags": [str(x)[:10] for x in (tags or [])][:6],
        "desc": str(desc or "")[:150],
    }


def _commit(index_path, out_dir, new_entries):
    """加锁 → 重读最新索引 → 合并 → 原子写。"""
    with IndexLock(index_path):
        idx = load_index(index_path)
        if idx is None:
            print("  [错误] 索引文件损坏，已中止（不会覆盖）")
            return 0
        have = {(x.get("category"), x.get("file")) for x in idx}
        added = 0
        for it in new_entries:
            key = (it.get("category"), it.get("file"))
            if key in have:
                continue
            idx.append(it)
            have.add(key)
            added += 1
        save_index(index_path, idx)
        return added


def from_dir(src, out_dir, index_path, category, use_ai, jcfg, tags, prefix):
    files = []
    for root, _, names in os.walk(src):
        for n in names:
            if os.path.splitext(n)[1].lower() in IMG_EXT:
                files.append(os.path.join(root, n))
    print("发现 %d 张图片" % len(files))
    if use_ai and not (jcfg.get("api_base") and os.environ.get(jcfg.get("api_key_env") or "")):
        print("[错误] AI 模式需要 judge.api_base 与环境变量 %s；"
              "如不需要打标签请加 --no-ai" % (jcfg.get("api_key_env") or "?"))
        return 1

    pending = []
    os.makedirs(out_dir, exist_ok=True)
    for i, f in enumerate(files, 1):
        base = os.path.basename(f)
        h = _md5(f)
        ext = os.path.splitext(f)[1].lower()
        fname = safe_name((prefix or "") + h[:10] + ext)
        if not fname:
            print("  [跳过] 非法文件名: " + base)
            continue
        dst = os.path.join(out_dir, fname)
        if not os.path.exists(dst):
            try:
                shutil.copy2(f, dst)
            except Exception as e:
                print("  [跳过] %s 复制失败: %s" % (base, str(e)[:60]))
                continue
        else:
            try:
                if _md5(dst) != h:
                    print("  [跳过] %s 与库内同名文件内容不同" % base)
                    continue
            except Exception:
                continue

        if use_ai:
            v = _judge(dst, jcfg)
            if v is None:
                print("  [%d/%d] %s -> 判定失败（不收录）" % (i, len(files), base))
                continue
            if v is False:
                print("  [%d/%d] %s -> 不相关（跳过）" % (i, len(files), base))
                continue
            meta = v
        else:
            meta = {"name": os.path.splitext(base)[0][:12], "tags": tags, "desc": "手工导入"}
        pending.append(_entry(fname, category, meta.get("name"),
                              meta.get("tags"), meta.get("desc")))
        print("  [%d/%d] %s -> %s" % (i, len(files), base, meta.get("name")))

    added = _commit(index_path, out_dir, pending)
    print("完成：新增 %d 条（候选 %d）" % (added, len(pending)))
    return 0


def from_index(src_json, out_dir, index_path, category, src_img_dir):
    src = load_index(src_json)
    if src is None:
        print("[错误] 源索引无法解析")
        return 1
    print("源索引 %d 条" % len(src))
    if src_img_dir is None:
        src_img_dir = os.path.dirname(os.path.abspath(src_json))
    pending = []
    missing = 0
    os.makedirs(out_dir, exist_ok=True)
    for it in src:
        raw = it.get("file") or it.get("filename") or ""
        fname = safe_name(raw)
        if not fname:
            print("  [拒绝] 非法文件名: %r" % str(raw)[:60])
            continue
        sp = os.path.join(src_img_dir, fname)
        dst = os.path.join(out_dir, fname)
        # 源图必须真实存在，否则不入库（R13）
        if not os.path.exists(dst):
            if not os.path.exists(sp):
                missing += 1
                print("  [缺图] %s 源文件不存在，跳过" % fname)
                continue
            try:
                shutil.copy2(sp, dst)
            except Exception as e:
                print("  [失败] %s: %s" % (fname, str(e)[:60]))
                continue
        pending.append(_entry(fname, category, it.get("name"),
                              it.get("tags"), it.get("desc")))
    added = _commit(index_path, out_dir, pending)
    print("完成：新增 %d 条 | 缺图跳过 %d" % (added, missing))
    return 0


def main():
    ap = argparse.ArgumentParser(description="bot-mindscape 图库导入")
    ap.add_argument("src", help="目录 或 索引 json")
    ap.add_argument("--format", choices=["dir", "index"], default="dir")
    ap.add_argument("--category", default="general", help="入库分类")
    ap.add_argument("--no-ai", action="store_true", help="不调模型，直接入库")
    ap.add_argument("--tags", default="", help="手动标签（逗号分隔），配合 --no-ai")
    ap.add_argument("--prefix", default="", help="文件名前缀")
    ap.add_argument("--img-dir", default=None, help="index 格式时的图片目录")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--index", default=None)
    args = ap.parse_args()

    s = (cfg.section("stickers") if cfg else {}) or {}
    out_dir = _abs(args.out_dir or s.get("dir") or "./data/stickers")
    index_path = _abs(args.index or s.get("index") or os.path.join(out_dir, "index.json"))
    jcfg = s.get("judge") or {}
    tags = [x.strip() for x in args.tags.replace("，", ",").split(",") if x.strip()]

    print("图库目录: " + out_dir)
    print("索引文件: " + index_path)
    print("分类: " + args.category)
    print()
    if args.format == "dir":
        if not os.path.isdir(args.src):
            print("不是目录：" + args.src)
            return 1
        return from_dir(args.src, out_dir, index_path, args.category,
                        not args.no_ai, jcfg, tags, args.prefix)
    return from_index(args.src, out_dir, index_path, args.category, args.img_dir)


if __name__ == "__main__":
    sys.exit(main())