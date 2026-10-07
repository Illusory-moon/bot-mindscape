# -*- coding: utf-8 -*-
"""Rotate each configured bot's privacy code without printing it."""
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins"))
import mindscape_config as cfg
from mindscape_gate import pg_audit, pg_current, pg_notes, pg_rotate


def main():
    gate = cfg.section("privacy_gate")
    if not gate.get("enabled"):
        raise RuntimeError("privacy_gate 未启用")
    targets = {str(x) for x in gate.get("private_self_ids") or []}
    changed = 0
    seen = set()
    for bot in cfg.bot_entries():
        sid = str(bot.get("self_id") or "")
        if sid not in targets:
            continue
        path = pg_notes(sid)
        if not path:
            raise RuntimeError("bot 缺少账本路径: " + sid)
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else "# 账本\n"
        kwargs = {"length": int(gate.get("length") or 6),
                  "charset": gate.get("charset") or "alnum"}
        new, update = pg_rotate(old, **kwargs)
        while pg_current(new)[0] in seen:
            new, update = pg_rotate(new, force=True, **kwargs)
        seen.add(pg_current(new)[0])
        if update:
            if os.path.exists(path):
                stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                shutil.copy2(path, path + ".bak-gate-" + stamp)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".gate-tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(new)
            os.replace(tmp, path)
            pg_audit("rotated", sid)
            changed += 1
    print("privacy_gate: rotated=%d bots=%d" % (changed, len(targets)))


if __name__ == "__main__":
    main()
