# -*- coding: utf-8 -*-
"""mindscape_style —— 认知层：把「风格原文」压成 稳定层 + 近期层

背景：mindscape_learn 产出的是**追加式原文**，一天三千多字，越滚越长。
      而记忆每轮只能注入固定字数 —— 于是 prompt 里看到的永远是「当天的尾巴」，
      15 天里反复出现的稳定特质反而挤不进去。

所以风格也分两层，和记忆的「摘要 / 原文」一个思路：

  - **稳定层**：对【全周期】条目均匀抽样后压一遍，**每次覆盖写**。
    覆盖写是关键 —— 追加式会越滚越旧，覆盖式永远是最新共识。
  - **近期层**：最近 N 天的风格条目原样留下，捕捉「口癖在变」。

⚠️ 只收「怎么说」（观察 / 新词 / 原声示例），**不收 兴趣 / 值得记的**。
   那些是事实，混进风格层会让 bot 把它们当成自己的记忆说出来。
"""
import datetime
import json
import os
import re
import urllib.request

import mindscape_config as cfg
from mindscape_digest import dg_abs, sample_lines

# 只有这三类是「怎么说」；兴趣 / 值得记的 属于事实，刻意排除
STYLE_CATS = ("观察", "新词", "原声示例")

STABLE_HEADER = "<!-- 由 mindscape_style 生成；每次覆盖写，请勿手改 -->"
RECENT_HEADER = "<!-- 由 mindscape_style 生成；最近若干天的风格条目 -->"

DEFAULT_SYSTEM = (
    "你在整理一个人的说话风格档案。下面是长期观察到的条目"
    "（已按时间均匀抽样，覆盖全部记录）。"
    "请把它压成一份**稳定的说话习惯**清单："
    "只写「怎么说」—— 口癖、语气词、句式、断句、称呼习惯、表情符号用法；"
    "**不要写兴趣、在做的事、生活事实**，那些不属于风格；"
    "同一个特征被反复提到说明它稳，优先写；"
    "原声示例挑最典型的，最多 5 条。"
    "**不要把别人的昵称/人名当成自称变体** —— 自称只用代词或本人昵称；"
    "看到「自称或他人」这类含糊说法，一律不要收进自称。"
    "输出 Markdown，用「### 口癖」「### 说话方式」「### 典型原声」三节，"
    "不要前言、不要总结、不要写「根据观察」这类话。"
)


def sc_parse(text):
    """把风格原文拆成 [{'date','cat','items'}]。"""
    secs = []
    cur = None
    for ln in (text or "").splitlines():
        if ln.startswith("## "):
            t = ln[3:].strip()
            d, _, cat = t.partition(" ")
            cur = {"date": d.strip(), "cat": cat.strip(), "items": []}
            secs.append(cur)
        elif ln.startswith("- ") and cur is not None:
            v = ln[2:].strip()
            if v:
                cur["items"].append(v)
    return secs


def sc_style_lines(secs, cutoff=None, exclude=None):
    """取「怎么说」类条目，可选按日期下限过滤、按禁词过滤。

    exclude：不许出现在风格层里的词（人名等）。
    在**喂给 LLM 之前**就剔除 —— 它看不见，就不会写出来。
    """
    exclude = [w for w in (exclude or []) if w]
    out = []
    for s in secs:
        if s["cat"] not in STYLE_CATS:
            continue
        if cutoff and (not s["date"] or s["date"] < cutoff):
            continue
        for it in s["items"]:
            if any(w in it for w in exclude):
                continue
            out.append(s["date"] + " " + s["cat"] + ": " + it)
    return out


def sc_sanitize(text, exclude):
    """把禁词从**产出**里再剔一遍 —— LLM 不一定听话，确定性过滤才可靠。

    只删词、不删行：一行里往往还有别的有用信息
    （「自称变体：窝、沃、小灰灰」里前两个是对的）。删完清掉悬空的分隔符。
    """
    exclude = [w for w in (exclude or []) if w]
    if not exclude:
        return text
    out = []
    for ln in (text or "").splitlines():
        if any(w in ln for w in exclude):
            for w in exclude:
                ln = ln.replace(w, "")
            ln = re.sub(r"\s*[、,，]\s*(?=[、,，])", "", ln)   # 连续分隔符合并
            ln = re.sub(r"[、,，]\s*$", "", ln).rstrip()
            if ln.strip() in ("", "-", "###", "####"):
                continue
        out.append(ln)
    return "\n".join(out)


def sc_write(path, header, body):
    """**覆盖写**（不是追加）—— 这是本模块和 learn 最大的区别。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write((body or "").strip() + "\n")
    os.replace(tmp, path)


def sc_llm(llm, lines, max_input_chars, max_tokens, timeout=120):
    """把全周期条目压成稳定层文本；不可用返回 None。"""
    api_base = (llm.get("api_base") or "").rstrip("/")
    if not api_base:
        raise RuntimeError("style.llm.api_base 未配置")
    key = os.environ.get(llm.get("api_key_env") or "", "")
    if not key and llm.get("api_key_file"):
        with open(dg_abs(llm["api_key_file"]), encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError("未找到 API key（检查 api_key_env / api_key_file）")
    picked = sample_lines(lines, max_input_chars)
    user = "长期观察到的条目：\n" + "\n".join(picked)
    body = json.dumps({
        "model": llm.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": llm.get("persona") or DEFAULT_SYSTEM},
            {"role": "user", "content": user[:max_input_chars]},
        ],
        "temperature": float(llm.get("temperature", 0.3)),
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


def sc_run_target(d):
    """处理一个风格文件。返回 (稳定层字数, 近期层条数)。"""
    llm = d.get("llm") or {}
    src = dg_abs(d.get("source"))
    if not src or not os.path.exists(src):
        print("[mindscape_style] 风格原文不存在，跳过: %s" % src)
        return 0, 0
    stable_out = dg_abs(d.get("stable") or (src.rsplit(".", 1)[0] + ".stable.md"))
    recent_out = dg_abs(d.get("recent") or (src.rsplit(".", 1)[0] + ".recent.md"))
    days = int(d.get("recent_days") or 2)
    max_in = int(d.get("max_input_chars") or 24000)
    max_tok = int(d.get("max_tokens") or 900)

    exclude = [str(x) for x in (d.get("exclude") or []) if str(x).strip()]
    text = open(src, encoding="utf-8").read()
    secs = sc_parse(text)
    all_lines = sc_style_lines(secs, exclude=exclude)
    if not all_lines:
        print("[mindscape_style] 没找到风格类条目（观察/新词/原声示例），跳过")
        return 0, 0

    # 近期层：确定性，不过 LLM
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    recent = sc_style_lines(secs, cutoff=cutoff, exclude=exclude)
    if recent:
        body = sc_sanitize("\n".join("- " + x for x in recent), exclude)
        sc_write(recent_out, RECENT_HEADER, body)
    print("[mindscape_style] 近期层 %d 条（%s 起，禁词 %d 个）"
          % (len(recent), cutoff, len(exclude)))

    # 稳定层：全周期均匀抽样后压一遍，覆盖写
    stable = ""
    try:
        stable = sc_llm(llm, all_lines, max_in, max_tok)
    except Exception as e:
        # 压缩失败就保留上一版稳定层 —— 宁可旧，不可空
        print("[mindscape_style] 压缩失败，保留上一版稳定层: %s" % str(e)[:120])
        stable = ""
    if stable:
        stable = sc_sanitize(stable, exclude)
        # 自我封顶：**在行边界上**截断，别让注入端去硬切。
        cap = int(d.get("max_stable_chars") or 0)
        if cap and len(stable) > cap:
            cut = stable[:cap]
            nl = cut.rfind(chr(10))
            stable = (cut[:nl] if nl > 0 else cut).rstrip()
            print("[mindscape_style] 稳定层超 %d 字，已在行边界截断" % cap)
        sc_write(stable_out, STABLE_HEADER, stable)
        print("[mindscape_style] 稳定层 %d 字（源 %d 条全周期）"
              % (len(stable), len(all_lines)))
        return len(stable), len(recent)
    return 0, len(recent)


def sc_main():
    d = cfg.section("style")
    if not d:
        print("[mindscape_style] 未找到 style 配置，跳过")
        return
    if not d.get("enabled"):
        print("[mindscape_style] style.enabled 不为真，跳过（默认关闭）")
        return
    for target in (d.get("targets") or []):
        td = dict(d)
        td.update(target)
        sc_run_target(td)


if __name__ == "__main__":
    sc_main()
