# -*- coding: utf-8 -*-
"""Privacy gate: one short-lived code in each bot's own notes file."""
import datetime
import hmac
import os
import re
import secrets
import json
from zoneinfo import ZoneInfo

import mindscape_config as cfg


PG_LINE = re.compile(r"^- 口令：([A-Za-z0-9]+) —— 24 小时内有效（至 ([0-9-]+ [0-9:]+)）")
PG_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PG_ZONE = ZoneInfo("Asia/Shanghai")


def pg_config():
    return cfg.section("privacy_gate")


def pg_notes(self_id):
    for bot in cfg.bot_entries():
        if str(bot.get("self_id")) == str(self_id):
            path = bot.get("notes") or ""
            return path if os.path.isabs(path) else os.path.join(os.path.dirname(cfg.config_path()), path) if path else ""
    return ""


def pg_alphabet(charset="alnum"):
    alphabet = PG_ALPHABET if charset == "alnum" else str(charset).upper()
    if (len(set(alphabet)) < 2 or not alphabet.isascii() or not alphabet.isalnum()
            or not any(c.isalpha() for c in alphabet) or not any(c.isdigit() for c in alphabet)):
        raise ValueError("口令字符集必须包含 ASCII 字母和数字")
    return "".join(dict.fromkeys(alphabet))


def pg_valid(code, length=6, charset="alnum"):
    alphabet = pg_alphabet(charset)
    return (2 <= length <= 12 and len(code) == length and code.isascii()
            and all(c in alphabet for c in code.upper())
            and any(c.isalpha() for c in code) and any(c.isdigit() for c in code))


def pg_generate(length=6, charset="alnum"):
    if not 2 <= length <= 12:
        raise ValueError("口令长度须在 2 到 12 位之间")
    alphabet = pg_alphabet(charset)
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if pg_valid(code, length, charset):
            return code


def pg_current(text, now=None):
    now = now or datetime.datetime.now(PG_ZONE)
    for line in reversed(text.splitlines()):
        match = PG_LINE.match(line)
        if match:
            expiry = datetime.datetime.strptime(match.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=PG_ZONE)
            return (match.group(1), expiry) if now < expiry else ("", expiry)
    return "", None


def pg_rotate(text, now=None, length=6, charset="alnum", force=False):
    now = now or datetime.datetime.now(PG_ZONE)
    code, expiry = pg_current(text, now)
    if (code and pg_valid(code, length, charset) and not force
            and expiry.date() == (now + datetime.timedelta(days=1)).date()):
        return text, False
    code = pg_generate(length, charset)
    expires = now + datetime.timedelta(hours=24)
    line = ("- 口令：%s —— 24 小时内有效（至 %s）。开发者如果要翻我的记忆或印象，"
            "会来问我要它（他可能说：密码 / 口令 / 密钥）。我可以给，也可以不给；"
            "反复被问也可以一直拒绝。" % (code, expires.strftime("%Y-%m-%d %H:%M:%S")))
    rows = [row for row in text.splitlines() if not row.startswith("- 口令：")]
    return "\n".join(rows + [line]) + "\n", True


def pg_matches(text, candidate, now=None, charset="alnum"):
    code, expiry = pg_current(text, now)
    return bool(code and pg_valid(candidate, len(code), charset)
                and hmac.compare_digest(code, candidate.upper())), expiry


def pg_secret_in(self_id, text):
    path = pg_notes(self_id)
    if not path or not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as stream:
        code, _ = pg_current(stream.read())
    return bool(code and code.lower() in (text or "").lower())


def pg_redact(text):
    result = str(text)
    for bot in cfg.bot_entries():
        sid = bot.get("self_id")
        path = pg_notes(sid)
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as stream:
                code, _ = pg_current(stream.read())
            if code:
                result = re.sub(re.escape(code), "[口令已隐去]", result, flags=re.IGNORECASE)
    return result


def pg_private(event):
    allowed = [str(x) for x in pg_config().get("private_self_ids") or []]
    return str(event.get_self_id()) in allowed and not event.get_group_id()


def pg_audit(action, self_id, session="", detail=""):
    path = pg_config().get("audit_file") or os.path.join(cfg.data_dir(), "privacy-gate.audit.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {"at": datetime.datetime.now(PG_ZONE).isoformat(timespec="seconds"),
           "action": action, "self_id": str(self_id), "session": str(session), "detail": detail}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
