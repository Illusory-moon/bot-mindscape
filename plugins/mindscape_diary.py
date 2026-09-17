# -*- coding: utf-8 -*-
"""mindscape_diary —— 认知层：把聊天流提炼成长期记忆

思路：定期从消息库里增量读取群聊记录，让 LLM 提炼「发生了什么有趣的事」，
      追加到人类可读的 Markdown 日记里。

关键设计：
  - **只记事件，不学风格** —— prompt 里明确禁止总结任何人的说话方式
  - **增量处理** —— 用 state 文件记录进度，跑多少次都不会重复
  - **表结构可配** —— 不绑定任何特定 bot 框架
"""
import datetime
import json
import os
import sqlite3
import urllib.request

import mindscape_config as cfg

DEFAULT_SEG_SYMBOLS = {
    "text": "{text}", "at": "@{qq}", "image": "[图]", "face": "[表情]",
    "reply": "[回复]", "video": "[视频]", "record": "[语音]",
    "json": "[卡片]", "file": "[文件]",
}

DEFAULT_PERSONA = (
    "你在翻阅自己所在群聊的记录，想在自己的成长日记里记下值得记的人和事。"
    "注意：只记「别人说了什么、发生了什么有趣的事、谁和谁怎么了」，"
    "绝对不要去分析、模仿或总结任何人的说话风格。"
    "用第一人称、短句、轻松的语气写，每条一两句话。"
    '只输出一个 JSON 对象，格式：'
    '{"diary":["条目1","条目2"], "people":{"昵称":"一句话描述"}}'
    "diary：每条一句话，最多6条，没有值得记的就输出空数组。"
    "people：本次记录里出现的、值得记住的人，值为一句话描述（身份/特征/和你的关系/近况）。"
    "这用于以后认人，只记事实，不要写评价。没有新人物就输出空对象。"
)


def _abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(cfg.config_path()), path)


def seg_to_text(segs, symbols=None):
    """把消息段数组转成纯文本。"""
    sym = symbols or DEFAULT_SEG_SYMBOLS
    parts = []
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        t = s.get("type")
        dd = s.get("data") or {}
        tpl = sym.get(t)
        if tpl:
            try:
                parts.append(tpl.format(**dd))
            except Exception:
                parts.append(str(tpl))
        elif t:
            parts.append("[" + str(t) + "]")
    return "".join(parts)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {"since_ts": d.get("since_ts", 0), "since_seq": d.get("since_seq", 0)}
    except Exception:
        return {"since_ts": 0, "since_seq": 0}


def save_state(path, st):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def fetch(src, target, since_ts, since_seq):
    """从 SQLite 增量读取消息（表名/字段名来自配置）。"""
    db = _abs(src.get("db"))
    if not db or not os.path.exists(db):
        return []
    table = src.get("table") or "messages"
    fields = src.get("fields") or {}
    f_time = fields.get("time") or "timestamp"
    f_seq = fields.get("seq") or "sequence"
    f_data = fields.get("data") or "data"
    where = src.get("where") or ""
    # 同时覆盖「更晚的时间」与「同一秒但序号更大」两种情况，
    # 否则在同一秒内保存进度后，该秒后续消息会永久漏读。
    sql = ("SELECT %s, %s, %s FROM %s WHERE (%s > ? OR (%s = ? AND %s > ?))"
           % (f_time, f_seq, f_data, table, f_time, f_time, f_seq))
    if where:
        sql += " AND (" + where + ")"
    sql += " ORDER BY %s, %s" % (f_time, f_seq)

    self_id = str(target.get("self_id") or "")
    groups = [str(g) for g in (target.get("groups") or [])]

    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    cur = con.cursor()
    rows = []
    try:
        for ts, seq, data in cur.execute(sql, (since_ts, since_ts, since_seq)):
            try:
                d = json.loads(data)
            except Exception:
                continue
            uid = str(d.get("user_id", ""))
            if uid and uid == self_id:
                continue
            gid = str(d.get("group_id", ""))
            if groups and gid not in groups:
                continue
            txt = seg_to_text(d.get("message"), src.get("symbols"))
            if not txt.strip():
                continue
            sender = (d.get("sender") or {}).get("card") or (d.get("sender") or {}).get("nickname") or uid
            rows.append({
                "ts": ts, "seq": seq,
                "gname": str(d.get("group_name", ""))[:20],
                "who": str(sender)[:16], "uid": uid,
                "time": datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "txt": txt[:200],
            })
    finally:
        con.close()
    return rows


def call_llm(llm, persona, msgs, max_input_chars, max_tokens):
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("diary.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")

    user = "群聊记录：\n" + "\n".join(
        "[" + m["time"] + "][" + m["gname"] + "] " + m["who"] + ": " + m["txt"] for m in msgs
    )
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": persona or DEFAULT_PERSONA},
            {"role": "user", "content": user[:max_input_chars]},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    try:
        parsed = json.loads(out["choices"][0]["message"]["content"])
    except Exception:
        # 解析失败 ≠ 没有值得记的事 —— 返回 None 让调用方中断并重试，
        # 否则这批消息会被标记为「已处理」，永久丢失。
        return None
    if not isinstance(parsed, dict) or "diary" not in parsed:
        return None
    entries = parsed.get("diary")
    if not isinstance(entries, list):
        return None
    return parsed


def _update_people(path, people, now):
    """把人物画像合并进 people.md（同名覆盖，保留最近时间）。"""
    existing = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("- ") and "：" in line:
                    k, v = line[2:].split("：", 1)
                    existing[k.strip()] = v.strip()
    except Exception:
        pass
    for k, v in people.items():
        key = str(k).strip()
        if key and str(v).strip():
            existing[key] = str(v).strip()[:80]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 你认识的人（自动维护）\n\n")
        f.write("最后更新：" + now + "\n\n")
        for k in sorted(existing):
            f.write("- " + k + "：" + existing[k] + "\n")


def run_target(d):
    """处理一个 bot 的日记，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 40)
    max_in = int(d.get("max_input_chars") or 14000)
    max_tok = int(d.get("max_tokens") or 900)

    total_read = total_added = 0
    for target in (d.get("targets") or []):
        out_file = _abs(target.get("output"))
        state_file = _abs(target.get("state") or (out_file + ".state.json"))
        st = load_state(state_file)
        rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0))
        if not rows:
            continue
        total_read += len(rows)
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            try:
                res = call_llm(llm, target.get("persona"), chunk, max_in, max_tok)
            except Exception as e:
                # 失败就停：只推进会成功的那部分，剩下的下次重试
                print("[mindscape_diary] LLM 失败，本批中断，剩余留待下次: %s" % str(e)[:120])
                break
            if res is None:
                # call_llm 返回 None 表示响应不可用（非合法空日记）
                print("[mindscape_diary] 响应不可用，本批中断")
                break
            entries = res.get("diary") or []
            os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            if entries:
                with open(out_file, "a", encoding="utf-8") as fp:
                    fp.write("## " + now + "\n")
                    for e in entries:
                        fp.write("- " + str(e) + "\n")
                    fp.write("\n")
                total_added += len(entries)
            # 人物画像：单独落盘（后出现的描述覆盖旧的）
            people = res.get("people") or {}
            if isinstance(people, dict) and people:
                people_file = target.get("people") or (out_file.rsplit(".", 1)[0] + ".people.md")
                people_file = _abs(people_file)
                _update_people(people_file, people, now)
            st["since_ts"] = chunk[-1]["ts"]
            st["since_seq"] = chunk[-1]["seq"]
            save_state(state_file, st)
    return total_read, total_added


def main():
    d = cfg.section("diary")
    if not d:
        print("[mindscape_diary] 未找到 diary 配置，跳过")
        return
    read, added = run_target(d)
    print("[mindscape_diary] 读取 %d 条消息，新增 %d 条日记" % (read, added))


if __name__ == "__main__":
    main()