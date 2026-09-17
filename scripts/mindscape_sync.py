# -*- coding: utf-8 -*-
"""mindscape_sync —— 本地素材库与远程服务器的双向同步

设计目标（易维护优先）：
  - 本地改完一键推送，不用登服务器
  - 推送前先拉取，避免覆盖服务器自动采集的新图
  - 删除默认只从索引移除（图片文件留底，可恢复）

配置（放在本机，不进仓库）：
    ui:
      sync:
        enabled: true
        host: "1.2.3.4"
        port: 22
        user: "root"
        password: "..."
        key_file: "~/.ssh/id_rsa"     # 有则优先于 password
        remote_dir: "/opt/bot/data/stickers"

命令行：
    python scripts/mindscape_sync.py pull     # 服务器 -> 本地
    python scripts/mindscape_sync.py push     # 本地 -> 服务器（先自动合并）
    python scripts/mindscape_sync.py status
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "plugins"))

try:
    import mindscape_config as cfg
except Exception:
    cfg = None


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else HERE
    return os.path.join(base, path)


def sync_cfg():
    if not cfg:
        return {}
    s = (cfg.section("ui") or {}).get("sync") or {}
    return s if isinstance(s, dict) else {}


def available():
    s = sync_cfg()
    return bool(s.get("enabled")) and bool(s.get("host"))


def _connect(s=None):
    import paramiko
    s = s or sync_cfg()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=s.get("host"), port=int(s.get("port") or 22),
              username=s.get("user") or "root",
              timeout=float(s.get("timeout") or 20),
              banner_timeout=float(s.get("timeout") or 20),
              auth_timeout=float(s.get("timeout") or 20))
    kf = s.get("key_file")
    if kf:
        kw["key_filename"] = os.path.expanduser(str(kf))
    else:
        kw["password"] = s.get("password") or ""
    c.connect(**kw)
    return c


def _paths(s):
    rd = (s.get("remote_dir") or ".").rstrip("/")
    # 远端索引文件名可配（不同框架/插件习惯不同）
    idx_name = str(s.get("remote_index") or "index.json").lstrip("/")
    return rd, rd + "/" + idx_name


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _local_paths(st=None, local_index=None, local_dir=None):
    st = st if st is not None else (cfg.section("stickers") if cfg else {})
    ld = local_dir or _abs(st.get("dir") or "./data/stickers")
    li = local_index or _abs(st.get("index") or os.path.join(ld, "index.json"))
    return ld, li


def pull(local_index=None, local_dir=None, verbose=True):
    """服务器 -> 本地（只下缺的图片）。"""
    s = sync_cfg()
    if not available():
        raise RuntimeError("未配置 ui.sync 或未启用")
    local_dir, local_index = _local_paths(None, local_index, local_dir)
    rdir, rindex = _paths(s)
    os.makedirs(local_dir, exist_ok=True)
    c = _connect(s)
    try:
        sftp = c.open_sftp()
        with sftp.file(rindex, "r") as f:
            remote = json.loads(f.read().decode("utf-8"))
        if not isinstance(remote, list):
            remote = []
        got = 0
        for it in remote:
            fn = str(it.get("file") or "")
            if not fn:
                continue
            lp = os.path.join(local_dir, fn)
            if os.path.exists(lp) and os.path.getsize(lp) > 0:
                continue
            try:
                with sftp.file(rdir + "/" + fn, "rb") as f:
                    data = f.read()
                if data:
                    with open(lp, "wb") as f:
                        f.write(data)
                    got += 1
            except Exception as e:
                if verbose:
                    print("  [skip] %s: %s" % (fn, str(e)[:60]))
        sftp.close()
    finally:
        c.close()
    _save(local_index, remote)
    if verbose:
        print("拉取完成: %d 条索引，新下载 %d 张" % (len(remote), got))
    return len(remote), got


def push(local_index=None, local_dir=None, verbose=True):
    """本地 -> 服务器（先拉取合并，再上传）。"""
    s = sync_cfg()
    if not available():
        raise RuntimeError("未配置 ui.sync 或未启用")
    local_dir, local_index = _local_paths(None, local_index, local_dir)
    rdir, rindex = _paths(s)
    removed = _load(local_index + ".removed")
    c = _connect(s)
    try:
        sftp = c.open_sftp()
        remote = []
        try:
            with sftp.file(rindex, "r") as f:
                remote = json.loads(f.read().decode("utf-8"))
            if not isinstance(remote, list):
                remote = []
        except Exception:
            remote = []
        local = _load(local_index)

        def key(it):
            return (str(it.get("category") or ""), str(it.get("file") or ""))

        merged = {}
        for it in remote:
            if it.get("file"):
                merged[key(it)] = it
        for it in local:
            if it.get("file"):
                merged[key(it)] = it        # 本地优先
        killed = 0
        for r in removed:
            k = (str(r.get("category") or ""), str(r.get("file") or ""))
            if k in merged:
                merged.pop(k)
                killed += 1
        out_items = list(merged.values())

        up = 0
        for fn in {str(x.get("file")) for x in out_items if x.get("file")}:
            lp = os.path.join(local_dir, fn)
            if not os.path.exists(lp):
                continue
            rp_ = rdir + "/" + fn
            try:
                sftp.stat(rp_)
            except Exception:
                sftp.put(lp, rp_)
                up += 1

        tmp = rindex + ".tmp"
        with sftp.file(tmp, "w") as f:
            f.write(json.dumps(out_items, ensure_ascii=False, indent=2).encode("utf-8"))
        try:
            sftp.remove(rindex)
        except Exception:
            pass
        sftp.rename(tmp, rindex)
        sftp.close()
    finally:
        c.close()
    _save(local_index, out_items)
    try:
        if os.path.exists(local_index + ".removed"):
            os.remove(local_index + ".removed")
    except Exception:
        pass
    if verbose:
        print("推送完成: 共 %d 条（移除 %d，上传新图 %d）" % (len(out_items), killed, up))
    return len(out_items), killed, up


def status(local_index=None):
    s = sync_cfg()
    if not available():
        raise RuntimeError("未配置 ui.sync 或未启用")
    _, local_index = _local_paths(None, local_index, None)
    local = _load(local_index)
    rdir, rindex = _paths(s)
    c = _connect(s)
    try:
        sftp = c.open_sftp()
        try:
            with sftp.file(rindex, "r") as f:
                remote = json.loads(f.read().decode("utf-8"))
        except Exception:
            remote = []
        sftp.close()
    finally:
        c.close()

    def key(it):
        return (str(it.get("category") or ""), str(it.get("file") or ""))

    lk = {key(x) for x in local if x.get("file")}
    rk = {key(x) for x in remote if x.get("file")}
    return {"local": len(lk), "remote": len(rk),
            "only_local": len(lk - rk), "only_remote": len(rk - lk)}


def main():
    ap = argparse.ArgumentParser(description="bot-mindscape 素材库同步")
    ap.add_argument("action", choices=["pull", "push", "status"])
    a = ap.parse_args()
    try:
        if a.action == "pull":
            pull()
        elif a.action == "push":
            push()
        else:
            print(status())
    except Exception as e:
        print("失败:", str(e)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
