# -*- coding: utf-8 -*-
"""mindscape_digest —— 认知层：把日记压成「每日摘要」（分层记忆的骨架层）

问题：注入用的是滑动窗口，只取最近 N 字。可一个活跃群一天就能写出一万多字
      日记，于是 2500 字的预算只覆盖得到四五个小时 —— bot 每天醒来都
      「忘了昨天」。而 recall 是**检索**不是记忆：得先想起来去查，
      查出来的还只是零碎片段。

方案：为每个「已经过完的日期」生成一条摘要（一天一小段），单独存一份文件。
      注入时两层一起进 prompt：

        · 摘要   —— 骨架：她记得前几天发生过什么（本模块产出）
        · 原文   —— 细节：最近几小时的逐条记录（滑动窗口）
        · recall —— 深挖：问到具体人事时再去全文检索

只处理**已经过完的日期**（今天的日记还没写完），已经生成过的日期直接跳过，
所以这个脚本跑多勤都无所谓，一天只会真正调用一次 LLM。

用法: MINDSCAPE_CONFIG=... python mindscape_digest.py
"""
import datetime
import json
import os
import re
import urllib.request

import mindscape_config as cfg

HEADER_RE = re.compile(r"^##\s*(\d{4}-\d{2}-\d{2})")

DEFAULT_SYSTEM = (
    "你在整理自己的长期记忆。下面是你某一天记下的流水账条目。"
    "请用 1 到 3 句话概括这一天，写成第一人称、像回忆一样自然。"
    "只写这天真正发生过的事，不要评价、不要编造、不要提「今天」这种相对时间，"
    "直接说事。输出纯文本，不要 JSON、不要标题、不要条目标号。"
)


def dg_abs(path):
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    base = os.path.dirname(cfg.config_path()) if cfg else "."
    return os.path.join(base, path)


def parse_days(text):
    """把日记按日期切成 {日期: [条目行]}。

    只收 \"- \" 开头的行 —— 标题、空行、说明文字都不算内容。
    同一天出现多个标题（追历史时会这样）会自动并到一起。
    """
    days = {}
    cur = None
    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            cur = m.group(1)
            days.setdefault(cur, [])
            continue
        if cur and line.startswith("- "):
            days[cur].append(line)
    return days


def load_digests(path):
    """读已有的摘要文件 -> {日期: 摘要}。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return out
    cur = None
    buf = []

    def flush():
        if cur and buf:
            out[cur] = " ".join(x.strip() for x in buf if x.strip())

    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            flush()
            cur = m.group(1)
            buf = []
            continue
        s = line.strip()
        if cur and s and not s.startswith("#"):
            buf.append(s)
    flush()
    return out


def save_digests(path, name, digests):
    """原子写回（先写 .tmp 再 replace），免得中途断了留下半个文件。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# " + (name or "bot") + " · 每日摘要\n\n")
        for d in sorted(digests):
            f.write("## " + d + "\n" + str(digests[d]).strip() + "\n\n")
    os.replace(tmp, path)


def sample_lines(lines, max_chars):
    """条目太多时**均匀抽样**。

    直接切片 user[:max_chars] 只保留开头，会整段丢掉一天的后半截 ——
    而「昨晚发生了什么」往往正是要被记住的那部分。
    """
    total = sum(len(x) + 1 for x in lines)
    if total <= max_chars:
        return lines
    keep = max(1, int(len(lines) * max_chars / total))
    step = len(lines) / float(keep)
    return [lines[int(i * step)] for i in range(keep)]


def digest_llm(llm, date, entries, max_input_chars, max_tokens, timeout=90):
    """让 LLM 概括一天。返回摘要文本；不可用时返回 None（调用方会中断重试）。"""
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("digest.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(dg_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")
    picked = sample_lines(entries, max_input_chars)
    user = "日期：" + date + "\n我那天记下的：" + "\n".join(picked)
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": llm.get("persona") or DEFAULT_SYSTEM},
            {"role": "user", "content": user[:max_input_chars]},
        ],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    txt = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return txt.strip() or None


def run_digest_target(d, common):
    """处理一个日记文件的「补摘要」。返回新增条数。"""
    diary = dg_abs(d.get("diary"))
    if not diary or not os.path.exists(diary):
        print("[mindscape_digest] 日记不存在，跳过: %s" % diary)
        return 0
    out = dg_abs(d.get("output") or (diary.rsplit(".", 1)[0] + ".digest.md"))
    min_entries = int(d.get("min_entries") or common.get("min_entries") or 3)
    max_chars = int(d.get("max_chars") or common.get("max_chars") or 260)
    max_input = int(common.get("max_input_chars") or 24000)
    max_tokens = int(common.get("max_tokens") or 400)
    keep_days = int(common.get("keep_days") or 30)
    per_run = int(common.get("max_per_run") or 3)

    try:
        with open(diary, encoding="utf-8", errors="replace") as f:
            days = parse_days(f.read())
    except Exception as e:
        print("[mindscape_digest] 读日记失败: %s" % str(e)[:120])
        return 0

    done = load_digests(out)
    today = datetime.date.today().strftime("%Y-%m-%d")
    todo = [x for x in sorted(days)
            if x < today and x not in done and len(days[x]) >= min_entries]

    made = 0
    for date in todo[:per_run]:
        try:
            txt = digest_llm(common.get("llm") or {}, date, days[date], max_input, max_tokens)
        except Exception as e:
            # 失败就停：已生成的那几天已经落盘，剩下的下次再补
            print("[mindscape_digest] LLM 失败，中断: %s" % str(e)[:120])
            break
        if not txt:
            print("[mindscape_digest] 响应为空，中断")
            break
        done[date] = txt[:max_chars]
        save_digests(out, d.get("name"), done)   # 每成功一条就落盘
        made += 1

    # 只留最近 keep_days 天；摘要文件是给人看的，不该无限长
    if len(done) > keep_days:
        for old in sorted(done)[:-keep_days]:
            del done[old]
    if made or not os.path.exists(out):
        save_digests(out, d.get("name"), done)
    print("[mindscape_digest] %s: 待补 %d 天，本次新增 %d 条，累计 %d 天"
          % (d.get("name") or "?", len(todo), made, len(done)))
    return made


def dg_main():
    d = cfg.section("digest")
    if not d:
        print("[mindscape_digest] 未找到 digest 配置，跳过")
        return
    if d.get("enabled") is False:
        print("[mindscape_digest] 已关闭，跳过")
        return
    total = 0
    for t in (d.get("targets") or []):
        total += run_digest_target(t, d)
    print("[mindscape_digest] 完成，共新增 %d 条每日摘要" % total)


if __name__ == "__main__":
    dg_main()
