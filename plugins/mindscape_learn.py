# -*- coding: utf-8 -*-
"""mindscape_learn —— 认知层：从某个人的语料里学「他怎么说话」

和 diary 的分工：

  - diary 记「发生了什么」；
  - 本模块学「他怎么说」—— 典型场景：**用自己的人格搭 bot，让 bot 对齐自己的语气**。

只学习、不回复：这是一条**离线管道**，不挂任何运行时钩子。
产物是人类可读的 Markdown，注入与否、注入多少由记忆层单独决定
（`memory.bots[].style` / `style_chars`），所以它不会污染别的 bot。

三条硬约束：

  - **默认关闭** —— `learn.enabled` 不为真就一行都不跑
  - **不耦合** —— 不依赖任何具体人设，「研究员 prompt」由配置提供
  - **不碰别人** —— 本模块只写文件；谁读它，完全由各自 bot 的 memory 配置决定
"""
import datetime
import os

import mindscape_config as cfg
from mindscape_diary import call_llm, dy_abs, fetch, load_state, save_state

# 默认的「研究员 prompt」。**只是兜底**：真正决定学什么的应该是使用者写的
# targets[].persona —— 通用框架不该硬编码某个人是谁。
LN_DEFAULT_PERSONA = (
    "你是「说话风格研究员」，正在观察一个人真实发过的群消息。"
    "请提炼**增量**风格信息。只输出一个 JSON 对象，不要任何其他文字，格式："
    '{"observations":["关于他说话方式的观察：句式/断句/语气/习惯，每条一句话，'
    '只写本批体现的新特征"],'
    '"words":["新口头禅/高频词/语气词/梗"],'
    '"interests":["体现的爱好/状态/在做的事"],'
    '"memorable":["关于他生活/关系/约定、值得以后记住的事实"],'
    '"examples":["最体现他风格的 1-3 句原话，尽量短，用于模仿"]}'
    "要求：只写有把握且本批体现的；不写已知常识；没有新的就写空数组 []。"
)

# 产物分两处：风格（怎么说）和事实（值得记的）。
# 分开是有意的 —— 风格要常驻注入以对齐语气，事实该走检索。
LN_SECTIONS = [
    ("observations", "观察"),
    ("words", "新词"),
    ("interests", "兴趣"),
    ("examples", "原声示例"),
]


def ln_append(path, title, lines):
    """追加一节到 Markdown。返回写入条数。"""
    lines = [str(x).strip() for x in (lines or []) if x and str(x).strip()]
    if not lines:
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("## " + title + "\n")
        for x in lines:
            f.write("- " + x + "\n")
        f.write("\n")
    return len(lines)


def ln_run_target(d, target):
    """学一个对象，返回 (读取条数, 新增条数)。"""
    src = d.get("source") or {}
    llm = d.get("llm") or {}
    batch = int(d.get("batch") or 20)
    max_in = int(d.get("max_input_chars") or 12000)
    max_tok = int(d.get("max_tokens") or 1200)
    # ponytail: fetch 不带 LIMIT，一次运行会把游标之后的积压全跑完 ——
    # 首次指向一个几千条消息的库时会变成几百次 LLM 调用。单次封顶，
    # 剩下的留给下一轮定时任务慢慢追。
    max_batches = int(d.get("max_batches") or 40)
    persona = target.get("persona") or LN_DEFAULT_PERSONA

    user_id = str(target.get("user_id") or "")
    out_file = dy_abs(target.get("output"))
    if not user_id or not out_file:
        print("[mindscape_learn] target 缺 user_id 或 output，跳过")
        return 0, 0
    notes_file = dy_abs(target.get("notes")
                        or (out_file.rsplit(".", 1)[0] + ".notes.md"))
    state_file = dy_abs(target.get("state") or (out_file + ".state.json"))

    st = load_state(state_file)
    rows = fetch(src, target, st.get("since_ts", 0), st.get("since_seq", 0),
                 only_user=user_id)
    if not rows:
        return 0, 0

    total_added = 0
    for i in range(0, len(rows), batch):
        if (i // batch) >= max_batches:
            print("[mindscape_learn] 已达单次上限 %d 批，剩余 %d 条留待下次"
                  % (max_batches, len(rows) - i))
            break
        chunk = rows[i:i + batch]
        try:
            res = call_llm(llm, persona, chunk, max_in, max_tok,
                           expect_key="observations")
        except Exception as e:
            # 失败就停：只推进会成功的那部分，剩下的下次重试
            print("[mindscape_learn] LLM 失败，本批中断，剩余留待下次: %s"
                  % str(e)[:120])
            break
        if res is None:
            print("[mindscape_learn] 响应不可用，本批中断")
            break
        # 标题用这批消息自己的时间，而不是「运行时刻」 —— 追历史时一次运行会
        # 写出几十批，用运行时刻就会出现几十个一模一样的 ## 标题。
        stamp = datetime.datetime.fromtimestamp(chunk[-1]["ts"]).strftime("%Y-%m-%d %H:%M")
        for key, head in LN_SECTIONS:
            total_added += ln_append(out_file, stamp + " " + head, res.get(key))
        total_added += ln_append(notes_file, stamp + " 值得记的", res.get("memorable"))
        st["since_ts"] = chunk[-1]["ts"]
        st["since_seq"] = chunk[-1]["seq"]
        save_state(state_file, st)
    return len(rows), total_added


def ln_main():
    d = cfg.section("learn")
    if not d:
        print("[mindscape_learn] 未找到 learn 配置，跳过")
        return
    if not d.get("enabled"):
        # 默认关闭：不显式打开就一行都不跑
        print("[mindscape_learn] learn.enabled 不为真，跳过（默认关闭）")
        return
    targets = d.get("targets") or []
    if not targets:
        print("[mindscape_learn] learn.targets 为空，跳过")
        return
    tr = ta = 0
    for target in targets:
        r, a = ln_run_target(d, target)
        tr += r
        ta += a
    print("[mindscape_learn] 读取 %d 条消息，新增 %d 条风格观察" % (tr, ta))


if __name__ == "__main__":
    ln_main()