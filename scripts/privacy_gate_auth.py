# -*- coding: utf-8 -*-
"""Server-side authorization for private observation views."""
import json
import secrets
import time

import yaml

import mindscape_sync as sync
import mindscape_webconfig as managed
from mindscape_gate import pg_matches, pg_valid


SESSIONS = {}
TRIES = {}


def _audit(sftp, config, action, sid, detail=""):
    path = (config.get("privacy_gate") or {}).get("audit_file")
    if not path or not path.startswith("/"):
        raise RuntimeError("隐私审计路径未配置")
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action,
           "self_id": sid, "detail": detail}
    with sftp.open(path, "a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def authorize(sid, code, client):
    sid, code = str(sid or ""), str(code or "").strip().upper()
    now = time.time()
    key = (client, sid)
    count, until = TRIES.get(key, (0, 0))
    if now < until:
        raise ValueError("尝试次数过多，请稍后再试")
    if until and now >= until:
        count = 0
        TRIES.pop(key, None)
    connection = sync._connect()
    try:
        sftp = connection.open_sftp()
        with sftp.open(managed._paths()["config"], "r") as stream:
            config = yaml.safe_load(stream.read()) or {}
        gate = config.get("privacy_gate") or {}
        if not gate.get("enabled"):
            raise RuntimeError("隐私闸门未启用")
        if sid not in {str(x) for x in gate.get("private_self_ids") or []}:
            raise ValueError("该通道没有私聊交付入口")
        bot = next((b for b in (config.get("memory") or {}).get("bots") or []
                    if str(b.get("self_id")) == sid), None)
        path = bot.get("notes") if bot else None
        if not path or not path.startswith("/"):
            raise ValueError("该通道没有可核验的账本")
        with sftp.open(path, "r") as stream:
            notes = stream.read().decode("utf-8")
        length = int(gate.get("length") or 6)
        charset = gate.get("charset") or "alnum"
        valid = pg_valid(code, length, charset) and pg_matches(notes, code, charset=charset)[0]
        if not valid:
            count += 1
            max_tries = int(gate.get("max_tries") or 5)
            lockout = int(gate.get("lockout_minutes") or 30) * 60
            TRIES[key] = (count, now + lockout if count >= max_tries else 0)
            _audit(sftp, config, "denied", sid, "locked" if count >= max_tries else "wrong")
            raise ValueError("口令不对" if count < max_tries else "尝试次数过多，请稍后再试")
        _audit(sftp, config, "authorized", sid)
        TRIES.pop(key, None)
        token = secrets.token_urlsafe(32)
        ttl = int(gate.get("ttl_hours") or 24) * 3600
        SESSIONS[token] = (sid, client, now + ttl)
        return token, ttl
    finally:
        connection.close()


def session(token, client):
    item = SESSIONS.get(token or "")
    if not item or item[1] != client or time.time() >= item[2]:
        return ""
    return item[0]
