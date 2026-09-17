# -*- coding: utf-8 -*-
"""mindscape_core —— 不依赖任何 bot 框架的纯逻辑

放在这里的函数可以被打包脚本、独立命令行工具直接复用，
不需要安装 AstrBot 等框架。
"""
import json
import os
import re

DEFAULT_PROMPT = (
    "看这张图，判断它适不适合当一个「{persona}」用的表情包。\n\n"
    "判断标准：\n"
    "1. 图里是动漫/二次元风格的人物表情（开心、无语、得意、委屈、鄙夷等）→ related = true\n"
    "2. 如果是风景照、聊天截图、真人照片、纯文字图 → related = false\n\n"
    "⚠️ 不要纠结这个角色出自哪个作品，只看视觉特征和使用场景。\n"
    "⚠️ 下面方括号里是格式示例，必须根据实际图片填写真实内容，绝对不要照抄。\n\n"
    "只返回一行 JSON，不要解释、不要代码块：\n"
    '{"related": true, "name": "<起个中文短名>", "tags": ["<3-5个中文标签>"], "desc": "<一句话说明什么场景用>"}'
)

PLACEHOLDERS = {
    "四字以内中文名", "四字中文名", "中文名", "name",
    "3到5个中文标签", "3-5个中文标签", "标签", "tags",
    "一句话说明适合什么场景", "一句话描述", "一句话说明", "desc",
}

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def abs_path(path, base_dir):
    """把相对路径解析为绝对路径（相对于配置目录）。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir or ".", path)


def verdict_ok(v):
    """校验视觉判定结果是否有效（防模型照抄 prompt 示例）。"""
    if not isinstance(v, dict):
        return False
    name = str(v.get("name") or "").strip()
    desc = str(v.get("desc") or "").strip()
    tags = [str(x).strip() for x in (v.get("tags") or [])]
    if not name or name in PLACEHOLDERS:
        return False
    if name.startswith("<") or name.startswith("（"):
        return False
    if not desc or desc in PLACEHOLDERS:
        return False
    if not tags or any(x in PLACEHOLDERS for x in tags):
        return False
    return True


def parse_verdict(txt):
    """从模型输出里解析出 JSON 判定（含代码块和解释文字的兜底）。"""
    if not txt:
        return None
    bt = chr(96) * 3
    t = txt.replace(bt + "json", "").replace(bt, "").strip()

    def _as_dict(obj):
        # 模型偶尔返回 JSON 字符串/数组而不是对象，直接 .get() 会崩
        return obj if isinstance(obj, dict) else None

    try:
        got = _as_dict(json.loads(t))
        if got is not None:
            return got
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\"related\"[^{}]*\}", t)
    if m:
        try:
            got = _as_dict(json.loads(m.group(0)))
            if got is not None:
                return got
        except Exception:
            pass
    return None


def match_score(item, query):
    """按名字/标签/描述给候选图打分。"""
    hay = " ".join([
        str(item.get("name", "")),
        " ".join(str(x) for x in (item.get("tags") or [])),
        str(item.get("desc", "")),
    ]).lower()
    q = (query or "").lower()
    if not q:
        return 0.5
    if q in hay:
        return 100.0
    return float(sum(1 for ch in q if ch in hay))


def safe_name(name, allow_subdir=False):
    """校验索引里的文件名，拒绝路径穿越 / 盘符 / 绝对路径。

    返回规范化后的安全文件名；不合法返回 ""。
    """
    n = str(name or "").strip()
    if not n:
        return ""
    if "\x00" in n:
        return ""
    # 盘符 / UNC / 绝对路径
    if re.match(r"^[A-Za-z]:", n) or n.startswith("\\\\") or n.startswith("/"):
        return ""
    # 分隔符（本项目图库是扁平结构）
    if not allow_subdir and ("/" in n or "\\" in n):
        return ""
    if ".." in n.split("/") or ".." in n.split("\\"):
        return ""
    base = os.path.basename(n)
    if base != n.replace("\\", "/").split("/")[-1] and not allow_subdir:
        return ""
    return base


def is_inside(child, parent):
    """判断 child 是否位于 parent 目录内（解析符号链接后）。"""
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return os.path.commonpath([c, p]) == p
    except Exception:
        return False


class IndexLock:
    """最简跨进程锁：串行化「读索引 → 合并 → 写索引」。

    采集插件与导入脚本会同时碰索引，没有锁的话后写的会覆盖先写的。
    拿不到锁就抛错，绝不带旧快照往下写。
    """

    def __init__(self, path, timeout=5.0):
        self.lock = path + ".lock"
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
                    raise RuntimeError("索引被另一个进程占用: " + self.lock)
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


def load_index(path):
    """读图库索引。文件损坏时返回 None（区别于合法的空列表）。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else None
    except Exception:
        return None


def save_index(path, idx):
    """原子写索引（先写临时文件再替换），避免读者读到半份 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)