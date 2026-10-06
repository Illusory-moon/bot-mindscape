# -*- coding: utf-8 -*-
"""wake_stage_check —— 校验「线上唤醒补丁」是否与我们的认知一致（2026-10-06 立）。

为什么需要它：线上真正生效的是**容器内** AstrBot 的
  <site-packages>/astrbot/core/pipeline/waking_check/stage.py
而它的补丁历史是**手工内联**的 ✗ —— 曾经出现「仓库改了一套、线上跑另一套」✗，
最危险的不是不一致本身，而是**改完以为生效了** ✗。所以这里把「我们相信线上该有什么」
写成一份清单 ✓，任何人（任何 AI）改完都能一条命令验：

    docker cp qqbot-astrbot:<容器内路径> /tmp/stage.py
    python scripts/wake_stage_check.py /tmp/stage.py

全绿 = 与清单一致 ✓；有 ✗ = 线上不是我们以为的那份，**先别宣布改好了** ✗。
"""
import sys

# (名称, 必须出现的片段, 说明)  —— 片段一律用**行为标志**，不锁具体实现细节 ✓
CHECKS = [
    ("配置读取", "auto_wake_cfg", "每 bot 名字/概率/间隔/群白名单的读取 ✓"),
    ("提到名字", "_mentioned", "「说到名字」才醒（含 @ 与纯文本两种写法）✓"),
    ("@ 名字防误判", "_by_at", "@完整名字才算叫它（防「@爱<名>的某某」误判）✓"),
    ("抽样唤醒", "sample_prob", "低概率冒泡 ✓"),
    ("唤醒原因", "wake_reason", "groupctx 靠它决定「定向性」四种情形 ✓"),
    ("群限制", "group_restrict", "不在白名单的群整条跳过 ✓"),
    ("上下文缓冲", "group_ctx_buffer.jsonl", "未唤醒的消息也落一份给 groupctx ✓"),
    ("缓冲调用点", "record_ctx(event)", "唤醒/未唤醒两条路径都要写缓冲 ✓"),
    ("指令拦截", "_sparxie_is_cmd_query", "#角色面板/*<名>光锥 这类别的 bot 的指令不唤醒 ✓"),
    ("指令配置", "ignore_cmd", "拦截规则来自配置（不硬编码）✓"),
]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = open(sys.argv[1], encoding="utf-8").read()
    bad = 0
    for name, needle, why in CHECKS:
        hit = needle in src
        bad += 0 if hit else 1
        print(("  [OK]  " if hit else "  [MISS]") + " " + name + " —— " + why)
    print("")
    print(("全部命中 ✓ 线上与清单一致" if not bad else
           "有 %d 项没命中 ✗ —— 线上不是我们以为的那份，先别宣布改好了 ✗" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
