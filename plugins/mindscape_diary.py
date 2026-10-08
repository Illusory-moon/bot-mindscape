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

import re

# 群名清洗（2026-10-07 加）：实测某群的 group_name 在库里带**控制字符**（\x11\x10 ✗ ——
# 群名里的表情被网关存坏了 ✓），原样写进日记 → 1670 条【群名】带坏字节 ✗，还会进她的 prompt ✗。
# 规矩：只留可打印字符 ✓；去掉网关的表情占位 `<…>` ✓；清完为空就退回群号 ✓。
_GFX_JUNK = re.compile(r"<[^<>]{0,24}>")


def dy_gname(raw, gid=""):
    """把**群名**洗成可安全进 prompt 的短标签（详见上面注释）。

    ⚠️ 2026-10-07 踩坑：这里原本在「群名与群号都为空」时回退成「群聊」✗ ——
    可**私聊记录**正是这种情况 ✓ → 于是私聊日记的标签被写成了「【群聊】」✗，
    还把下游 `m["gname"] or DM_LABEL` 那句「私聊」兜底**整个抢走**了 ✗。
    修法：**这里只负责群名** ✓ —— 取不到就返回空串 ✓，让调用方自己决定怎么称呼 ✓。
    """
    s = "".join(ch for ch in str(raw or "") if ch.isprintable())
    s = _GFX_JUNK.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:20] or str(gid or "").strip()

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
    # 记忆按群分房（2026-10-06 主人裁定）：模型在生成时**看得到**每条记录来自哪个群，
    # 只是以前没要求它写下来 → 存进日记后来源就丢了，注入时自然分不清是哪群的事。
    "每条日记前面用【群名】标出这件事发生在哪个群（群名在记录的方括号里）。"
    "方括号里写「私聊」的，就是一对一私聊、不是群 —— 照写「【私聊】」就好，别自己编群名。"
    "同一个群的条目排在一起，绝不把两个群的事混进同一句。"
    '只输出一个 JSON 对象，格式：'
    '{"diary":["条目1","条目2"], "people":{"昵称":"一句话描述"}}'
    "diary：每条一句话，最多6条，没有值得记的就输出空数组。"
    "people：本次记录里出现的、值得记住的人，值为一句话描述（身份/特征/和你的关系/近况）。"
    "这用于以后认人，只记事实，不要写评价。没有新人物就输出空对象。"
)


def dy_abs(path):
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


def fetch(src, target, since_ts, since_seq, only_user=None):
    # target 可以带自己的 source（db/table/where/fields）—— 顶层那份是默认 ✓。
    # 用途：同一个源库里，群消息和私聊是两个 event_name（group_message / private_message），
    # 想各写一份日记，就得能按 target 换 where ✓（2026-10-06 加）。
    src = ((target or {}).get("source") or src) or {}
    """从 SQLite 增量读取消息（表名/字段名来自配置）。

    only_user：只取这个 user_id 的消息（mindscape_learn 用它学某人的风格）。
    不传时保持原语义 —— 排除 self_id（日记只记别人）。
    """
    db = dy_abs(src.get("db"))
    if not db or not os.path.exists(db):
        return []
    # 只收指定发送者的消息（2026-10-07 加 ✓）：
    # 网关库**什么都存** ✗ —— 连「被平台白名单拦掉、bot 根本没收到」的私聊也在里面 ✗；
    # 不按发送者过滤，她就会「记得」自己从没看过的话 ✗（实测混进过陌生人的私聊 ✓）。
    _senders = {str(x).strip() for x in ((target or {}).get("senders") or []) if str(x).strip()}
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
            if only_user is not None:
                if uid != str(only_user):
                    continue
            elif uid and uid == self_id:
                continue
            if _senders and uid not in _senders:
                continue          # 只收允许的发送者 ✓（见上面注释 ✓）
            gid = str(d.get("group_id", ""))
            if groups and gid not in groups:
                continue
            txt = seg_to_text(d.get("message"), src.get("symbols"))
            if not txt.strip():
                continue
            sender = (d.get("sender") or {}).get("card") or (d.get("sender") or {}).get("nickname") or uid
            rows.append({
                "ts": ts, "seq": seq,
                "gid": gid,
                "gname": dy_gname(d.get("group_name"), gid),
                "who": str(sender)[:16], "uid": uid,
                "time": datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "txt": txt[:200],
            })
    finally:
        con.close()
    return rows


# 私聊记录没有群名 → 方括号里留空，模型会**自己编一个群名**
# （实测：它把一对一私聊写成了某个群的记录 ✗）。给个明确回退标签 ✓。
DM_LABEL = "私聊"


def dy_render(msgs):
    """把一批消息渲染成发给模型的那段文本。

    单独抽出来，是因为**分批**和**发送**必须用同一套算法 ——
    两边不一致就会出现「按 5000 字分好批、发出去却是 6000 字被砍」。
    """
    return "群聊记录：\n" + "\n".join(
        "[" + m["time"] + "][" + (m.get("gname") or DM_LABEL) + "] " + m["who"] + ": " + m["txt"]
        for m in msgs)


def chunk_by_budget(rows, batch, max_input_chars, split_group=False):
    """先按条数切、再按【真实渲染长度】细分，保证每条消息都进得了某一次请求。

    为什么要这么麻烦：以前是固定 batch 条一组，再在 call_llm 里把文本砍到
    max_input_chars —— 砍掉的那截尾巴没人知道，而游标照样推到 chunk[-1]，
    于是那几条消息**永久漏记**（把预算调小或把批次调大就能触发）。

    单条自己就超预算时**抛 ValueError**：明确失败、停在原游标，绝不静默截断。
    调用方接住它、打印、**不推进游标**。
    """
    out = []
    for i in range(0, len(rows), batch):
        cur = []
        for r in rows[i:i + batch]:
            if split_group and cur and (r.get("gid"), r.get("gname")) != (cur[0].get("gid"), cur[0].get("gname")):
                out.append(cur)
                cur = []
            trial = cur + [r]
            if cur and len(dy_render(trial)) > max_input_chars:
                out.append(cur)
                cur = [r]
            else:
                cur = trial
            if len(dy_render(cur)) > max_input_chars:
                raise ValueError(
                    "单条消息渲染后 %d 字 > max_input_chars=%d，装不进任何一批"
                    % (len(dy_render(cur)), max_input_chars))
        if cur:
            out.append(cur)
    return out


def call_llm(llm, persona, msgs, max_input_chars, max_tokens, relations=None,
             expect_key="diary"):
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("diary.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(dy_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")

    user = dy_render(msgs)
    if len(user) > max_input_chars:
        # 绝不再静默截断：截断 + 推进游标 = 被砍掉的那几条永久漏记。
        # 调用方应当先用 chunk_by_budget 分好批。
        raise RuntimeError(
            "本批渲染后 %d 字，超过 max_input_chars=%d —— 调用方必须先分批"
            % (len(user), max_input_chars))
    system = persona or DEFAULT_PERSONA
    if relations:
        # 没有这段，摘要会把「喜欢的人」写成「某个群友」—— 日记正文和人物画像
        # 都会跟着错，而这些关系本来是作者写死的。
        system += ("\n\n你已经知道的关系（描述必须与这里一致，绝不能写成陌生群友）：\n"
                   + "\n".join(relations))
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # 提炼类任务温度别太高；某些口径（如风格学习）需要更保守
        "temperature": float(llm.get("temperature", 0.7)),
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
    # 2026-10-08 加：把**跑批的 token 也记上** ✓ —— 本鱼那个 trace 只看得到**容器内**的调用 ✗，
    # 而宿主机这批（日记 / 摘要 / 风格）是用 /opt/mindscape/.api_key **直连**的 ✓ 一直不在账上 ✗
    # （主人问「那一小时 75 万未命中从哪来」✓ 这里是最大的盲区 ✓）。
    try:
        _u = out.get("usage") or {}
        print("[mindscape_diary] LLM 用量: 未命中 %s ｜ 命中缓存 %s ｜ 输出 %s | model=%s"
              % (_u.get("prompt_cache_miss_tokens", _u.get("prompt_tokens", "?")),
                 _u.get("prompt_cache_hit_tokens", "?"),
                 _u.get("completion_tokens", "?"),
                 out.get("model") or "?"))
    except Exception:
        pass
    try:
        parsed = json.loads(out["choices"][0]["message"]["content"])
    except Exception:
        # 解析失败 ≠ 没有值得记的事 —— 返回 None 让调用方中断并重试，
        # 否则这批消息会被标记为「已处理」，永久丢失。
        return None
    # expect_key：不同口径要的顶层键不一样（日记是 diary，风格学习是 observations）
    if not isinstance(parsed, dict) or expect_key not in parsed:
        return None
    entries = parsed.get(expect_key)
    if not isinstance(entries, list):
        return None
    return parsed


REL_TITLE = "## 对我来说重要的人（来自人格档案，自动整理不会覆盖）"
AUTO_TITLE = "## 群里遇到的人（自动整理）"


def load_relations(spec):
    """从人格档案里取「关系与称呼」这类段落，作为不会被自动覆盖的权威条目。

    为什么需要：人物画像是一句话自动摘要，它不知道谁是「喜欢的人」，
    只会把对方写成「群友」。一旦好友被降级成陌生人，bot 就会认错人 ——
    而这类关系是**作者写死的**，不该由摘要模型来猜。

    spec 可以是字符串（文件路径，取全文的 - 行），也可以是
    {file: ..., section: "关系与称呼"} —— 只取该 ## 段落里的 - 行。
    """
    if not spec:
        return []
    if isinstance(spec, str):
        spec = {"file": spec}
    path = dy_abs(spec.get("file"))
    if not path or not os.path.exists(path):
        return []
    section = str(spec.get("section") or "").strip()
    out = []
    inside = not section          # 没指定段落就取全文的 - 行
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip()
                if s.startswith("## "):
                    inside = (section in s) if section else True
                    continue
                if inside and s.startswith("- "):
                    out.append(s)
    except Exception:
        return []
    return out


def _relation_heads(relations):
    """取每条关系「→」之前的名字部分，用来判断摘要是否在讲同一个人。"""
    heads = []
    for r in relations or []:
        body = r[2:] if r.startswith("- ") else r
        heads.append(body.split("→", 1)[0].strip())
    return heads


def _update_people(path, people, now, relations=None):
    """把人物画像合并进 people.md（同名覆盖，保留最近时间）。

    relations 是来自人格档案的权威条目，单独放在文件开头；
    自动摘要如果提到了权威条目里的人（如「小爱」出现在
    「Alice（小爱）」中），**不允许**把它写成普通群友。
    """
    relations = [r for r in (relations or []) if str(r).strip()]
    heads = _relation_heads(relations)
    existing = {}
    try:
        with open(path, encoding="utf-8") as f:
            in_auto = True                    # 旧格式没有分段标题，按自动段处理
            for line in f:
                s = line.strip()
                if s.startswith("## "):
                    in_auto = s.startswith(AUTO_TITLE)
                    continue
                if s.startswith("#"):         # 一级标题不是分段
                    continue
                if in_auto and s.startswith("- ") and "：" in s:
                    k, v = s[2:].split("：", 1)
                    existing[k.strip()] = v.strip()
    except Exception:
        pass
    # 历史上被摘要降级过的条目（比如「某人：群友」）也要清掉 ——
    # 只挡新的不够，旧的那条会一直躺在文件里继续误导 bot。
    for k in [k for k in existing if any(k in h for h in heads)]:
        del existing[k]
    for k, v in people.items():
        key = str(k).strip()
        if not key or not str(v).strip():
            continue
        if any(key in h for h in heads):
            continue                          # 权威关系里的人，不让摘要顶掉
        existing[key] = str(v).strip()[:80]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 先写临时文件、再原子替换 —— 中途退出只会留下一个 .tmp，
    # 绝不会把画像**写坏成半份**（以前直接覆盖写，进程一死就只剩半截）。
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# 你认识的人\n\n")
            f.write("最后更新：" + now + "\n\n")
            if relations:
                f.write(REL_TITLE + "\n")
                for r in relations:
                    f.write(r + "\n")
                f.write("\n")
            f.write(AUTO_TITLE + "\n")
            for k in sorted(existing):
                f.write("- " + k + "：" + existing[k] + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # 同一目录内：原子
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def dy_has_batch(path, rng, tail_bytes=200000):
    """产物里是否已经写过这一批（看【文件尾巴】就够了）。

    重跑要补的总是最后那批 —— 崩溃发生在「写完日记、游标还没落盘」之间，
    所以只需在尾部找标记，不必扫整个文件。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return ("ms-seq:" + rng).encode("utf-8") in f.read()
    except Exception:
        return False


def run_target(d):
    """处理一个 bot 的日记，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 40)
    max_in = int(d.get("max_input_chars") or 14000)
    max_tok = int(d.get("max_tokens") or 900)
    # ponytail: fetch 不带 LIMIT，一次运行会把游标之后的全部积压跑完 ——
    # 首次指向一个几千条消息的群时会变成几百上千次 LLM 调用（烧钱且占满
    # 这台 2 核小机器）。用 max_batches 给单次运行封顶，剩下的留给下一次
    # cron 慢慢追。追历史变慢时才需要调大它。
    max_batches = int(d.get("max_batches") or 40)

    total_read = total_added = 0
    for target in (d.get("targets") or []):
        out_file = dy_abs(target.get("output"))
        state_file = dy_abs(target.get("state") or (out_file + ".state.json"))
        st = load_state(state_file)
        relations = load_relations(target.get("relations"))
        people_file = dy_abs(target.get("people") or (out_file.rsplit(".", 1)[0] + ".people.md"))
        rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0))
        if not rows:
            # 即使没新消息，也要保证权威关系已经落在画像里
            if relations:
                _update_people(people_file, {}, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), relations)
            continue
        total_read += len(rows)
        all_people = {}
        try:
            batches = chunk_by_budget(rows, batch, max_in, split_group=True)
        except ValueError as e:
            # 分不出合法的批：停在原游标，等主人调大 max_input_chars
            print("[mindscape_diary] 分批失败，本轮不动游标: %s" % str(e)[:140])
            continue
        done = 0
        for bi, chunk in enumerate(batches):
            if done >= max_batches:
                print("[mindscape_diary] 已达单次上限 %d 批，剩余 %d 批留待下次"
                      % (max_batches, len(batches) - bi))
                break
            try:
                res = call_llm(llm, target.get("persona"), chunk, max_in, max_tok, relations)
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
            # 标题用这批消息自己的时间，而不是「运行时刻」—— 追历史时一次运行会
            # 写出几十批，用运行时刻就会出现几十个一模一样的 ## 标题。
            stamp = datetime.datetime.fromtimestamp(chunk[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
            # 这一批的来源游标范围，写成【独立一行】的 HTML 注释：
            #   - 渲染时看不见；检索只认 "## " 和 "- "，所以不会污染记忆文本
            #   - 万一「日记写完、游标还没落盘」就退出，重跑时靠它认出这批已写过，
            #     只推进游标、不再追加一遍（以前会整整重复一批）
            rng = "%s-%s" % (chunk[0]["seq"], chunk[-1]["seq"])
            if entries and not dy_has_batch(out_file, rng):
                with open(out_file, "a", encoding="utf-8") as fp:
                    fp.write("<!-- ms-seq:" + rng + " -->\n")
                    fp.write("## " + stamp + "\n")
                    label = chunk[0].get("gname") or DM_LABEL
                    for e in entries:
                        entry = str(e).strip()
                        if entry.startswith("【") and "】" in entry:
                            entry = entry.split("】", 1)[1].lstrip()
                        fp.write("- 【" + label + "】" + entry + "\n")
                    fp.write("\n")
                total_added += len(entries)
            elif entries:
                print("[mindscape_diary] 这批已写过（ms-seq:%s），只推进游标" % rng)
                total_added += 0
            # 人物画像先攒着，一轮结束时合并落盘一次即可
            people = res.get("people") or {}
            if isinstance(people, dict) and people:
                all_people.update(people)
            st["since_ts"] = chunk[-1]["ts"]
            st["since_seq"] = chunk[-1]["seq"]
            save_state(state_file, st)
            done += 1
        # 人物画像：权威关系 + 本轮摘要，合并后写一次
        if all_people or relations:
            _update_people(people_file, all_people,
                           datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), relations)
    return total_read, total_added


def dy_main():
    d = cfg.section("diary")
    if not d:
        print("[mindscape_diary] 未找到 diary 配置，跳过")
        return
    read, added = run_target(d)
    print("[mindscape_diary] 读取 %d 条消息，新增 %d 条日记" % (read, added))


if __name__ == "__main__":
    dy_main()
