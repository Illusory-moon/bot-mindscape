# -*- coding: utf-8 -*-
"""Local editable copy of the server configuration used by web_ui."""
import datetime
import hashlib
import json
import os
import shutil
import tempfile

import yaml
from yaml.nodes import MappingNode, SequenceNode

import mindscape_sync as sync


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = {
    "config": os.path.join(ROOT, "config", "managed.yaml"),
    "wake": os.path.join(ROOT, "config", "managed-wake.json"),
}
STATE = os.path.join(ROOT, "config", "managed.sync.json")
REMOTE_DEFAULT = {
    "config": "/opt/astrbot/data/plugin_data/astrbot_plugin_mindscape/config.yaml",
    "wake": "/opt/astrbot/data/auto_wake_cfg.json",
}

# tab, label, source, path, type, scope, optional hint
FIELDS = [
    ("规矩", "行为规矩（每行一条）", "config", "memory.bots.rules", "lines", "bot", "写给所选 bot，留空即无额外规矩"),
    ("规矩", "不使用句号", "config", "format.no_period", "target", "bot", "只改变所选 bot 的出站文字"),
    ("记忆", "每轮记忆字数", "config", "memory.bots.memory_chars", "int", "bot", "越大越详细，也越耗 token"),
    ("记忆", "人物画像字数", "config", "memory.bots.people_chars", "int", "bot", "仅当画像文件已配置时生效"),
    ("记忆", "每日摘要字数", "config", "memory.digest_chars", "int", "global", "所有 bot 共用的上限"),
    ("记忆", "账本字数", "config", "memory.notes_chars", "int", "global", "所有 bot 共用的上限"),
    ("记忆", "生成每日摘要", "config", "digest.enabled", "bool", "global", "后台任务还须正常运行"),
    ("认知", "补充群聊上下文", "config", "groupctx.enabled", "bool", "global", "只在已配置的目标 bot 生效"),
    ("认知", "群聊记录条数", "config", "groupctx.count", "int", "global", "每次回复读取最近多少条"),
    ("认知", "群聊回看时间（秒）", "config", "groupctx.window_sec", "int", "global", "过期消息不会进入上下文"),
    ("认知", "查看历史图片", "config", "groupctx.images", "bool", "global", "图片会增加回复耗时"),
    ("认知", "图片回看时间（秒）", "config", "groupctx.image_window_sec", "int", "global", "超过此时间的图片不再附带"),
    ("认知", "最多查看图片数", "config", "groupctx.image_max", "int", "global", "建议保持为 1"),
    ("认知", "只看同一人发的图", "config", "groupctx.image_same_sender", "bool", "global", "避免误认别人的图片"),
    ("表达", "收图抽样概率", "config", "stickers.sample_prob", "prob", "global", "0 到 100%，提高会增加识图费用"),
    ("表达", "主动配图概率", "config", "stickers.send.force_prob", "prob", "global", "0 到 100%"),
    ("表达", "选图候选数", "config", "stickers.send.candidates", "int", "global", "每轮给 bot 看的备选图片数"),
    ("表达", "斗图时跟进", "config", "stickers.send.formation.enabled", "bool", "global", "群里有人连续发图时使用"),
    ("表达", "斗图判定时间（秒）", "config", "stickers.send.formation.window", "int", "global", ""),
    ("运行", "冒泡概率", "wake", "per_bot.sample_prob", "prob", "bot", "没有被点名时随机回应的概率"),
    ("运行", "冒泡时间间隔（秒）", "wake", "per_bot.min_interval", "int", "bot", "两次随机回应至少相隔多久"),
    ("运行", "额外唤醒开关", "wake", "per_bot.enabled", "bool", "bot", "关闭后名字和随机冒泡都不会唤醒；平台原生 @ 不受影响"),
    ("运行", "点名词（每行一个）", "wake", "per_bot_names", "lines", "bot", "说到这些词时会唤醒所选 bot"),
    ("运行", "慢回复警戒（毫秒）", "config", "trace.warn_ms", "int", "global", "超过后在日志中标记"),
]
CATALOG = [dict(tab=t, label=l, source=s, path=p, type=k, scope=sc, hint=h)
           for t, l, s, p, k, sc, h in FIELDS]
BY_PATH = {(f["source"], f["path"]): f for f in CATALOG}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _paths():
    s = sync.sync_cfg()
    return {k: s.get("remote_" + k) or v for k, v in REMOTE_DEFAULT.items()}


def _server_id():
    return str(sync.sync_cfg().get("host")) + "|" + "|".join(_paths().values())


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _atomic(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        shutil.copy2(path, path + ".bak-" + stamp)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".managed-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parse(source, raw):
    data = yaml.safe_load(raw) if source == "config" else json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是对象")
    return data


def _remote_read(sftp, path):
    with sftp.open(path, "rb") as f:
        return f.read()


def pull():
    if not sync.available():
        raise RuntimeError("请先在本地 config.yaml 配好 ui.sync")
    client = sync._connect()
    try:
        sftp = client.open_sftp()
        raw = {k: _remote_read(sftp, p) for k, p in _paths().items()}
        for k, v in raw.items():
            _parse(k, v)
        for k, v in raw.items():
            _atomic(LOCAL[k], v)
        _atomic(STATE, json.dumps({**{k: _digest(v) for k, v in raw.items()},
                                   "server": _server_id()}).encode())
    finally:
        client.close()


def _load():
    if not all(os.path.exists(p) for p in (*LOCAL.values(), STATE)):
        pull()
    if json.loads(_read(STATE)).get("server") != _server_id():
        raise RuntimeError("服务器连接已更换，请重新读取服务器配置")
    return {k: _parse(k, _read(p)) for k, p in LOCAL.items()}


def _bot_ids(data):
    return {str(b.get("self_id")) for b in data["config"].get("memory", {}).get("bots", [])
            if isinstance(b, dict) and b.get("self_id")}


def _at(data, field, bot, create=False):
    parts = field["path"].split(".")
    source = field["source"]
    node = data[source]
    if source == "config" and field["scope"] == "bot" and parts[:2] == ["memory", "bots"]:
        entries = node.get("memory", {}).get("bots", [])
        node = next(b for b in entries if str(b.get("self_id")) == bot)
        parts = parts[2:]
    elif source == "wake" and parts[0] == "per_bot":
        node = node.setdefault("per_bot", {}).setdefault(bot, {}) if create else node.get("per_bot", {}).get(bot, {})
        parts = parts[1:]
    elif source == "wake" and parts[0] == "per_bot_names":
        node = node.setdefault("per_bot_names", {}) if create else node.get("per_bot_names", {})
        parts = [bot]
    for part in parts[:-1]:
        node = node.setdefault(part, {}) if create else node.get(part, {})
        if not isinstance(node, dict):
            raise ValueError("配置结构与表单不匹配")
    return node, parts[-1]


def snapshot():
    data = _load()
    bots = [dict(id=str(b["self_id"]), name=str(b.get("name") or b["self_id"]))
            for b in data["config"].get("memory", {}).get("bots", []) if isinstance(b, dict) and b.get("self_id")]
    values = {}
    for bot in bots:
        values[bot["id"]] = {}
    for f in CATALOG:
        ids = [b["id"] for b in bots] if f["scope"] == "bot" else ["global"]
        for bot in ids:
            node, key = _at(data, f, bot)
            value = node.get(key) if isinstance(node, dict) else None
            if f["type"] == "target":
                value = bot in (value or []) or "all" in (value or []) or "*" in (value or [])
            elif value is None and f["source"] == "wake":
                wake = data["wake"]
                if f["path"] == "per_bot.sample_prob":
                    value = wake.get("sample_prob", 0.02)
                elif f["path"] == "per_bot.min_interval":
                    value = wake.get("min_interval", 600)
                elif f["path"] == "per_bot.enabled":
                    value = True
                elif f["path"] == "per_bot_names":
                    value = wake.get("names", [])
            elif value is None and f["path"] == "memory.bots.memory_chars":
                value = data["config"]["memory"].get("max_chars", 2500)
            elif value is None and f["path"] == "memory.bots.people_chars":
                value = data["config"]["memory"].get("people_chars", 800)
            elif f["type"] == "lines":
                value = "\n".join(str(x) for x in (value or []))
            elif f["type"] == "prob" and value is not None:
                value = round(float(value) * 100, 3)
            values.setdefault(bot, {})[f["source"] + ":" + f["path"]] = value
    base = json.loads(_read(STATE))
    if base.get("server") != _server_id():
        raise RuntimeError("服务器连接已更换，请先重新读取服务器配置")
    dirty = any(_digest(_read(LOCAL[k])) != base.get(k) for k in LOCAL)
    allowed = {str(x) for x in data["config"].get("privacy_gate", {}).get("private_self_ids") or []}
    return dict(bots=bots, gate_bots=[b for b in bots if b["id"] in allowed],
                fields=CATALOG, values=values, dirty=dirty)


def _value(field, value):
    kind = field["type"]
    if kind in ("bool", "target"):
        if not isinstance(value, bool):
            raise ValueError("需要开或关")
        return value
    if kind == "lines":
        if not isinstance(value, str) or len(value) > 10000:
            raise ValueError("文字过长")
        return [line.strip() for line in value.splitlines() if line.strip()]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000000:
            raise ValueError("请输入 0 到 1000000 的整数")
        return value
    if kind == "prob":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError("请输入 0 到 100 的百分数")
        return round(value / 100, 6)
    raise ValueError("未知类型")


def _yaml_field(raw, field, bot, value):
    """Replace one YAML value using PyYAML source marks, retaining other comments."""
    def member(mapping, key):
        if not isinstance(mapping, MappingNode):
            return None, None
        for name, child in mapping.value:
            if name.value == key:
                return name, child
        return None, None

    root = yaml.compose(raw)
    parts = field["path"].split(".")
    parent = root
    if parts[:2] == ["memory", "bots"]:
        _, memory = member(root, "memory")
        _, bots = member(memory, "bots")
        if not isinstance(bots, SequenceNode):
            return None
        parent = None
        for entry in bots.value:
            _, bot_node = member(entry, "self_id")
            if bot_node is not None and bot_node.value == bot:
                parent = entry
                break
        parts = parts[2:]
    for part in parts[:-1]:
        _, parent = member(parent, part)
    if not isinstance(parent, MappingNode):
        return None
    name, old = member(parent, parts[-1])
    encoded = json.dumps(value, ensure_ascii=False)
    if old is not None:
        start = old.start_mark.index
        if isinstance(value, list) and old.start_mark.line > name.start_mark.line:
            if value:
                prefix = " " * old.start_mark.column
                encoded = ("\n" + prefix).join("- " + json.dumps(x, ensure_ascii=False)
                                                for x in value)
            else:
                start = name.start_mark.index
                encoded = name.value + ": []"
            if old.end_mark.index < len(raw) and raw[old.end_mark.index] != "\n":
                encoded += "\n" + " " * old.end_mark.column
        return raw[:start] + encoded + raw[old.end_mark.index:]
    if not parent.value:
        return None
    indent = parent.value[0][0].start_mark.column
    line_start = raw.rfind("\n", 0, parent.end_mark.index) + 1
    return raw[:line_start] + " " * indent + parts[-1] + ": " + encoded + "\n" + raw[line_start:]


def change(source, path, bot, value):
    field = BY_PATH.get((source, path))
    if not field:
        raise ValueError("不支持编辑这个配置项")
    data = _load()
    if field["scope"] == "bot" and bot not in _bot_ids(data):
        raise ValueError("请选择已配置的 bot")
    value = _value(field, value)
    node, key = _at(data, field, bot, create=True)
    if field["type"] != "target" and node.get(key) == value:
        base = json.loads(_read(STATE))
        return any(_digest(_read(LOCAL[k])) != base.get(k) for k in LOCAL)
    if field["type"] == "target":
        targets = list(node.get(key) or [])
        if "all" in targets or "*" in targets:
            raise ValueError("当前对全部 bot 生效，请在高级配置中调整范围")
        targets = [x for x in targets if str(x) != bot]
        if value:
            targets.append(bot)
        node[key] = targets
    else:
        node[key] = value
    if source == "config":
        original = _read(LOCAL[source]).decode("utf-8")
        updated = _yaml_field(original, field, bot, node[key])
        if updated is None or _parse("config", updated) != data[source]:
            raise ValueError("这项配置的 YAML 结构无法安全更新，请在高级配置中修改")
        raw = updated.encode("utf-8")
    else:
        raw = json.dumps(data[source], ensure_ascii=False, indent=2).encode("utf-8")
    _atomic(LOCAL[source], raw)
    return snapshot()["dirty"]


def push():
    if not sync.available():
        raise RuntimeError("服务器同步尚未配置")
    data = {k: _read(p) for k, p in LOCAL.items()}
    for k, raw in data.items():
        _parse(k, raw)
    base = json.loads(_read(STATE))
    client = sync._connect()
    try:
        sftp = client.open_sftp()
        paths = _paths()
        remote = {k: _remote_read(sftp, p) for k, p in paths.items()}
        for k, raw in remote.items():
            if _digest(raw) != base.get(k):
                raise RuntimeError("服务器配置已有新改动，请先重新读取，避免覆盖")
        changed = [k for k in LOCAL if _digest(data[k]) != base.get(k)]
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        for k in changed:
            path = paths[k]
            with sftp.open(path + ".bak-" + stamp, "wb") as f:
                f.write(remote[k])
            tmp = path + ".mindscape-tmp"
            with sftp.open(tmp, "wb") as f:
                f.write(data[k])
            sftp.posix_rename(tmp, path)
        if changed:
            _atomic(STATE, json.dumps({**{k: _digest(data[k]) for k in LOCAL},
                                       "server": _server_id()}).encode())
        command = sync.sync_cfg().get("restart_command")
        if changed and command:
            _, stdout, stderr = client.exec_command(command, timeout=90)
            status = stdout.channel.recv_exit_status()
            if status:
                raise RuntimeError("文件已同步，但重启失败：" + stderr.read().decode("utf-8", "replace")[:120])
        return "配置已同步" + ("并重启服务" if changed and command else "；生效可能需要重启") if changed else "服务器已是最新版"
    finally:
        client.close()
