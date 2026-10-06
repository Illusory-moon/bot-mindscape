# -*- coding: utf-8 -*-
"""Check live channel boundaries and deployed plugin bytes without changing the server."""
import argparse
import hashlib
import json
import os
import shlex
import sys

import yaml

import mindscape_sync as sync
import mindscape_webconfig as managed


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = "/opt/astrbot/data/plugins/mindscape/main.py"


def _ids(targets):
    return {str(t.get("self_id")) if isinstance(t, dict) else str(t)
            for t in (targets or [])}


def check_config(data, text_channel=""):
    issues = []
    bots = (data.get("memory") or {}).get("bots") or []
    ids = [str(b.get("self_id") or "") for b in bots if isinstance(b, dict)]
    known = set(ids)
    if not ids or "" in ids or len(known) != len(ids):
        issues.append("memory.bots 的 self_id 缺失或重复")
    fmt = data.get("format") or {}
    formatted = _ids(fmt.get("targets"))
    unpunctuated = _ids(fmt.get("no_period"))
    if known - formatted:
        issues.append("format.targets 未覆盖所有记忆通道")
    if unpunctuated - formatted:
        issues.append("format.no_period 含未启用格式化的通道")
    for section in ("diary", "archive", "stickers"):
        for target in (data.get(section) or {}).get("targets") or []:
            if isinstance(target, dict) and str(target.get("self_id") or "") not in known:
                issues.append(section + ".targets 引用了未知通道")
    for section in ("groupctx", "vision"):
        extra = _ids((data.get(section) or {}).get("targets")) - known
        if extra:
            issues.append(section + ".targets 引用了未知通道")
    owners = {}
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        sid = str(bot.get("self_id") or "")
        for path in (bot.get("diary"), bot.get("people"), bot.get("digest"), bot.get("notes"),
                     *(bot.get("extra_diaries") or [])):
            if path and path in owners and owners[path] != sid:
                issues.append("多个通道共用记忆文件: " + str(path))
            elif path:
                owners[path] = sid
    for target in (data.get("archive") or {}).get("targets") or []:
        if isinstance(target, dict):
            path, sid = target.get("file"), str(target.get("self_id") or "")
            if path and path in owners and owners[path] != sid:
                issues.append("发言档案归属与记忆通道冲突: " + str(path))
            elif path:
                owners[path] = sid
    if text_channel:
        if text_channel not in known:
            issues.append("文字通道未配置 memory.bots")
        if text_channel not in formatted or text_channel not in unpunctuated:
            issues.append("文字通道未完整启用出站格式")
        for section in ("groupctx", "vision", "stickers"):
            if text_channel in _ids((data.get(section) or {}).get("targets")):
                issues.append("文字通道误入 " + section + ".targets")
    return issues


def remote_hash(sftp, path):
    digest = hashlib.sha256()
    with sftp.open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def persona_rows(client, db):
    code = ("import json,sqlite3\n"
            "db=sqlite3.connect('file:' + " + repr(db) + " + '?mode=ro',uri=True)\n"
            "rows=db.execute(\"select scope_id,value from preferences where key='session_service_config'\").fetchall()\n"
            "print(json.dumps([(scope,(json.loads(value).get('val') or {}).get('persona_id')) for scope,value in rows]))")
    _, stdout, stderr = client.exec_command("python3 -c " + shlex.quote(code), timeout=20)
    raw = stdout.read()
    if stdout.channel.recv_exit_status():
        raise RuntimeError("读取会话人格绑定失败: " + stderr.read().decode("utf-8", "replace")[:100])
    return json.loads(raw)


def check_persona(client, db, prefix, expected=""):
    rows = [(scope, persona) for scope, persona in persona_rows(client, db)
            if scope.startswith(prefix)]
    if not rows:
        return ["未找到该通道的显式会话人格绑定"]
    if any(not persona or (expected and persona != expected) for _, persona in rows):
        return ["该通道存在缺失或不符合预期的人格绑定"]
    return []


def channels_report(text_channel=""):
    if not sync.available():
        raise RuntimeError("请先配置 ui.sync")
    text_channel = text_channel or str(sync.sync_cfg().get("text_channel") or "")
    client = sync._connect()
    try:
        sftp = client.open_sftp()
        with sftp.open(managed._paths()["config"], "rb") as f:
            raw = f.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("服务器配置超过 5 MB")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError("服务器配置格式不正确")
        expected = sync.sync_cfg().get("expected_personas") or {}
        if not isinstance(expected, dict):
            raise ValueError("ui.sync.expected_personas 必须是对象")
        db = (data.get("janitor") or {}).get("db")
        bindings = persona_rows(client, db) if db else []
        issues = check_config(data, text_channel)
        if text_channel and not db:
            issues.append("未配置会话数据库，无法核对人格绑定")
        rows = []
        fmt = data.get("format") or {}
        sections = {k: (data.get(k) or {}).get("targets") or []
                    for k in ("diary", "digest", "archive", "stickers", "vision", "groupctx")}
        digest_outputs = {t.get("output") for t in sections["digest"] if isinstance(t, dict)}
        for bot in (data.get("memory") or {}).get("bots") or []:
            if not isinstance(bot, dict) or not bot.get("self_id"):
                continue
            sid = str(bot["self_id"])
            found = [persona for scope, persona in bindings if scope.startswith(sid + ":")]
            want = str(expected.get(sid) or "")
            if sid != text_channel:
                status = "未核对"
            elif not found or any(not p for p in found):
                status = "未绑定"
            elif want and any(p != want for p in found):
                status = "与预期不符"
            elif want:
                status = "符合预期"
            else:
                status = "已绑定（未设预期）"
            if sid == text_channel:
                if status in ("未绑定", "与预期不符"):
                    issues.append("文字通道人格绑定" + status)
                elif not want:
                    issues.append("文字通道未配置预期人格，无法核对归属")
            rows.append({"id": sid, "name": str(bot.get("name") or sid),
                         "targets": {"format": sid in _ids(fmt.get("targets")),
                                     "no_period": sid in _ids(fmt.get("no_period")),
                                     "diary": sid in _ids(sections["diary"]),
                                     "digest": bool(bot.get("digest") and bot["digest"] in digest_outputs),
                                     "archive": sid in _ids(sections["archive"]),
                                     "stickers": sid in _ids(sections["stickers"]),
                                     "vision": sid in _ids(sections["vision"]),
                                     "groupctx": sid in _ids(sections["groupctx"])},
                         "persona": status, "bindings": len(found) if sid == text_channel else None,
                         "actual_personas": sorted({str(p) for p in found if p}),
                         "expected_persona": want})
        return {"channels": rows, "issues": issues, "text_channel": text_channel}
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text-channel", default="", help="需隔离图片和群缓冲的文字通道 self_id")
    ap.add_argument("--expected-persona", default="", help="文字通道预期的人格 ID")
    ap.add_argument("--remote-plugin", default=PLUGIN)
    ap.add_argument("--skip-plugin", action="store_true")
    args = ap.parse_args()
    if not sync.available():
        raise RuntimeError("请先配置 ui.sync")
    client = sync._connect()
    try:
        sftp = client.open_sftp()
        with sftp.open(managed._paths()["config"], "rb") as f:
            raw = f.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("服务器配置超过 5 MB")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise ValueError("服务器配置格式不正确")
        issues = check_config(data, args.text_channel)
        if args.text_channel:
            db = (data.get("janitor") or {}).get("db")
            if db:
                issues.extend(check_persona(client, db, args.text_channel + ":", args.expected_persona))
            else:
                issues.append("未配置会话数据库，无法核对人格绑定")
        if not args.skip_plugin:
            local = os.path.join(ROOT, "dist", "mindscape", "main.py")
            with open(local, "rb") as f:
                expected_hash = hashlib.file_digest(f, "sha256").hexdigest()
            actual_hash = remote_hash(sftp, args.remote_plugin)
            if expected_hash != actual_hash:
                issues.append("线上插件与本地 dist 摘要不一致")
            print("插件 SHA-256: 本地 %s / 线上 %s" % (expected_hash[:12], actual_hash[:12]))
        for issue in issues:
            print("[FAIL]", issue)
        if not issues:
            print("[OK] 通道配置、人格绑定与已检查的部署文件一致")
        return 1 if issues else 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
