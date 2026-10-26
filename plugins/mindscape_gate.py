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


def _pg_zone():
    """UTC+8 时区对象 —— **不许在 import 时炸**（2026-10-27 修 · P1-4）。

    事故形态：原来是模块级 `PG_ZONE = ZoneInfo("Asia/Shanghai")` ⇒ 在没有 tzdata 的环境
    （Windows 原生 Python、`python:3.12-slim`、Alpine…）**import 就抛 ZoneInfoNotFoundError**
    ⇒ `mindscape_gate` 起不来 ⇒ 依赖它的 `mindscape_guard` 也起不来 ⇒ **整个插件加载失败** ✗
    （实测：自检因此连带红 7 项）
    ⇒ 现在：优先系统 tz 数据，拿不到就退成**固定 +8 偏移**
      （本系统只用这一个时区、且中国无夏令时 ⇒ 固定偏移与 ZoneInfo 等价 ✓）
    """
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")


PG_ZONE = _pg_zone()


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


_PG_CODE_CACHE = {}          # path -> (mtime, size, code, at)
_PG_CODE_TTL = 30.0          # 秒


def pg_codes_fast():
    """各 bot 的当前口令 —— 带 30 秒缓存（2026-10-27 修 · P2-2）。

    为什么：`pg_redact()` 会被挂在**所有 logger 的所有 handler** 上 ⇒ 每条日志都调一次。
    原来每次都**开文件读整份账本**、再跑正则 ⇒ 日志热路径 = 磁盘 I/O ✗（bot 越多越慢）。
    ⇒ 这里按 (路径, mtime, size) 缓存 30 秒：账本没变就直接复用 ✓
      （口令一天才轮换一次，30 秒的滞后对"打码"来说完全够 ✓）
    """
    import time
    now = time.time()
    codes = []
    for bot in cfg.bot_entries():
        path = pg_notes(bot.get("self_id"))
        if not path or not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except Exception:
            continue
        hit = _PG_CODE_CACHE.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size and (now - hit[3]) < _PG_CODE_TTL:
            if hit[2]:
                codes.append(hit[2])
            continue
        code = ""
        try:
            with open(path, encoding="utf-8") as stream:
                code, _ = pg_current(stream.read())
        except Exception:
            code = ""
        _PG_CODE_CACHE[path] = (st.st_mtime, st.st_size, code, now)
        if code:
            codes.append(code)
    return codes


def pg_redact(text):
    result = str(text)
    for code in pg_codes_fast():
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
