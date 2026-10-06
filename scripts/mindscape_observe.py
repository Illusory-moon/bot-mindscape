# -*- coding: utf-8 -*-
"""Read-only server snapshots for the local web UI."""
import errno
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import tempfile

import yaml

import mindscape_sync as sync
import mindscape_webconfig as managed


ROOT = os.path.join(managed.ROOT, "data", "observe")
MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 30 * 1024 * 1024
MAX_FILES = 100
PREVIEW = 200 * 1024


def _dir(kind):
    if kind not in ("memory", "impression", "logs"):
        raise ValueError("未知资料类型")
    return os.path.join(ROOT, kind)


def _atomic(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".observe-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _paths(config):
    base = posixpath.dirname(managed._paths()["config"])
    found = {}

    def add(path, label):
        if not isinstance(path, str) or not path or "\0" in path:
            return
        path = posixpath.normpath(path if posixpath.isabs(path) else posixpath.join(base, path))
        if posixpath.isabs(path):
            found.setdefault(path, label)

    for bot in (config.get("memory") or {}).get("bots") or []:
        if not isinstance(bot, dict):
            continue
        owner = str(bot.get("name") or bot.get("self_id") or "bot")
        for field in ("diary", "people", "digest", "notes"):
            add(bot.get(field), owner + " / " + field)
        for path in bot.get("extra_diaries") or []:
            add(path, owner + " / extra")
    for section, field in (("diary", "output"), ("digest", "output"),
                           ("archive", "file")):
        for target in (config.get(section) or {}).get("targets") or []:
            if isinstance(target, dict):
                add(target.get(field), section)
    return found


def _log_paths(sftp, config):
    found = {}
    configured = sync.sync_cfg().get("remote_logs") or []
    if not isinstance(configured, list):
        raise ValueError("ui.sync.remote_logs 必须是列表")
    for path in configured:
        if isinstance(path, str) and posixpath.isabs(path) and "\0" not in path:
            found[posixpath.normpath(path)] = "日志"
    janitor = (config.get("janitor") or {}).get("log")
    if isinstance(janitor, str) and posixpath.isabs(janitor):
        parent = posixpath.dirname(janitor)
        for item in sftp.listdir_attr(parent):
            if item.filename.endswith(".log") and stat.S_ISREG(item.st_mode):
                found[posixpath.join(parent, item.filename)] = "日志"
    return found


def _containers():
    settings = sync.sync_cfg()
    names = settings.get("log_containers") or []
    if not isinstance(names, list):
        raise ValueError("ui.sync.log_containers 必须是列表")
    names = list(names)
    command = shlex.split(str(settings.get("restart_command") or ""))
    if len(command) == 3 and command[:2] == ["docker", "restart"]:
        names.append(command[2])
    for name in dict.fromkeys(names):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("容器名不合法")
        yield name


def _capture(sftp, path):
    info = sftp.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError("不是普通文件或超过 5 MB")
    with sftp.open(path, "rb") as f:
        raw = f.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("超过 5 MB")
    return raw, info


def _save_entry(kind, key, label, raw, mtime=None):
    ident = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    _atomic(os.path.join(_dir(kind), ident + ".txt"), raw)
    return {"id": ident, "path": key, "label": label, "size": len(raw), "mtime": mtime}


def pull(kind):
    _dir(kind)
    if not sync.available():
        raise RuntimeError("请先配置 ui.sync")
    client = sync._connect()
    try:
        sftp = client.open_sftp()
        config_raw, _ = _capture(sftp, managed._paths()["config"])
        config = yaml.safe_load(config_raw)
        if not isinstance(config, dict):
            raise ValueError("服务器配置格式不正确")
        if kind == "memory":
            paths = _paths(config)
        elif kind == "impression":
            path = sync.sync_cfg().get("remote_impression") or "/opt/astrbot/data/plugin_data/astrbot_plugin_impression/states.json"
            if not isinstance(path, str) or not posixpath.isabs(path) or "\0" in path:
                raise ValueError("ui.sync.remote_impression 必须是绝对路径")
            paths = {posixpath.normpath(path): "人物印象"}
        else:
            paths = _log_paths(sftp, config)
        entries, total = [], 0
        for path, label in list(paths.items())[:MAX_FILES]:
            try:
                raw, info = _capture(sftp, path)
                if total + len(raw) > MAX_TOTAL:
                    raise ValueError("本次总量超过 30 MB")
                entries.append(_save_entry(kind, path, label, raw, info.st_mtime))
                total += len(raw)
            except (OSError, ValueError) as e:
                if isinstance(e, OSError) and e.errno == errno.ENOENT:
                    entries.append({"path": path, "label": label, "error": "尚未生成", "missing": True})
                else:
                    entries.append({"path": path, "label": label, "error": str(e)[:100]})
        if kind == "logs":
            for name in _containers():
                if len(entries) >= MAX_FILES:
                    break
                key = "docker:" + name
                command = "docker logs --tail 2000 --timestamps " + name + " 2>&1"
                _, stdout, _ = client.exec_command(command, timeout=30)
                raw = stdout.read(MAX_FILE + 1)
                if stdout.channel.recv_exit_status() != 0:
                    entries.append({"path": key, "label": "容器", "error": raw.decode("utf-8", "replace")[:100]})
                elif len(raw) > MAX_FILE or total + len(raw) > MAX_TOTAL:
                    entries.append({"path": key, "label": "容器", "error": "日志超过大小限制"})
                else:
                    entries.append(_save_entry(kind, key, "容器", raw))
                    total += len(raw)
        _atomic(os.path.join(_dir(kind), "index.json"), json.dumps(entries, ensure_ascii=False).encode("utf-8"))
        return {"count": sum("id" in e for e in entries),
                "missing": sum(e.get("missing", False) for e in entries),
                "errors": sum("error" in e and not e.get("missing") for e in entries)}
    finally:
        client.close()


def listing(kind):
    path = os.path.join(_dir(kind), "index.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def preview(kind, ident):
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{20}", ident):
        raise ValueError("文件编号不合法")
    if ident not in {e.get("id") for e in listing(kind)}:
        raise FileNotFoundError("本地没有这份文件")
    with open(os.path.join(_dir(kind), ident + ".txt"), "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - PREVIEW))
        raw = f.read()
    return {"text": raw.decode("utf-8", "replace"), "truncated": size > PREVIEW}
