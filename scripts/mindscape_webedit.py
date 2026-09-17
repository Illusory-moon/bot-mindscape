# -*- coding: utf-8 -*-
"""mindscape_webedit —— 给 web_ui 打补丁：图库编辑 / 移除 / 同步按钮

作为独立模块被 web_ui.py 调用，避免把 web_ui 撑得太大。
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "plugins"))

try:
    import mindscape_config as cfg
except Exception:
    cfg = None

try:
    from mindscape_core import load_index, save_index, IndexLock
except Exception:
    load_index = save_index = None
    IndexLock = None


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else HERE
    return os.path.join(base, path)


def sticker_paths():
    st = cfg.section("stickers") if cfg else {}
    d = _abs(st.get("dir") or "./data/stickers")
    i = _abs(st.get("index") or os.path.join(d, "index.json"))
    return d, i


def edit_item(category, filename, name=None, tags=None, desc=None):
    """修改索引里某一项的元数据。返回 (ok, message)。"""
    if not (load_index and save_index and IndexLock):
        return False, "内部模块不可用"
    d, i = sticker_paths()
    try:
        with IndexLock(i):
            idx = load_index(i)
            if idx is None:
                return False, "索引文件损坏"
            hit = None
            for it in idx:
                if str(it.get("category") or "") == str(category) and str(it.get("file") or "") == str(filename):
                    hit = it
                    break
            if hit is None:
                return False, "找不到这条记录"
            if name is not None:
                hit["name"] = str(name)[:12]
            if tags is not None:
                if isinstance(tags, str):
                    tags = [t.strip()[:10] for t in tags.replace(",", " ").split() if t.strip()]
                hit["tags"] = [str(t)[:10] for t in (tags or [])][:6]
            if desc is not None:
                hit["desc"] = str(desc)[:150]
            save_index(i, idx)
        return True, "已保存"
    except Exception as e:
        return False, str(e)[:120]


def remove_item(category, filename, delete_file=False):
    """从索引移除（默认保留图片文件，可恢复）。"""
    if not (load_index and save_index and IndexLock):
        return False, "内部模块不可用"
    d, i = sticker_paths()
    try:
        with IndexLock(i):
            idx = load_index(i)
            if idx is None:
                return False, "索引文件损坏"
            before = len(idx)
            idx = [it for it in idx
                   if not (str(it.get("category") or "") == str(category)
                           and str(it.get("file") or "") == str(filename))]
            if len(idx) == before:
                return False, "找不到这条记录"
            save_index(i, idx)
            # 记进删除清单（推送时同步给服务器）
            rem_path = i + ".removed"
            rem = []
            try:
                with open(rem_path, encoding="utf-8") as f:
                    rem = json.load(f)
                if not isinstance(rem, list):
                    rem = []
            except Exception:
                rem = []
            rem.append({"category": str(category), "file": str(filename)})
            with open(rem_path, "w", encoding="utf-8") as f:
                json.dump(rem, f, ensure_ascii=False, indent=2)
        if delete_file:
            try:
                os.remove(os.path.join(d, filename))
            except Exception:
                pass
        return True, "已移除" + ("（含文件）" if delete_file else "（文件保留）")
    except Exception as e:
        return False, str(e)[:120]


def sync_available():
    try:
        import mindscape_sync as ms
        return ms.available()
    except Exception:
        return False


def do_sync(action):
    try:
        import mindscape_sync as ms
        if action == "pull":
            n, got = ms.pull(verbose=False)
            return True, "已从服务器拉取 %d 条（新下载 %d 张）" % (n, got)
        if action == "push":
            n, killed, up = ms.push(verbose=False)
            return True, "已推送 %d 条（移除 %d，上传 %d）" % (n, killed, up)
        if action == "status":
            return True, str(ms.status())
        return False, "未知操作"
    except Exception as e:
        return False, str(e)[:160]
