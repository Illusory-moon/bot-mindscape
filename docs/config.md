# 配置：核心项逐个说

> **完整版**永远是 [`config/config.example.yaml`](../config/config.example.yaml)（每个字段都有注释）✓
> 这里挑的是**记忆 / 摘要 / 风格**这几块最容易配错的项。


```yaml
memory:
  max_chars: 2500          # 每轮注入的总字数预算
  digest_chars: 1200       #   其中「摘要层」
  notes_chars: 800         #   其中「账本层」
  bots:
    - self_id: "20000000"
      name: "bot-name"
      diary: "./data/bot-name.md"            # 原文层：逐条事件
      digest: "./data/bot-name.digest.md"    # 摘要层：每天一句骨架
      notes: "./data/bot-name.notes.md"      # 账本层：工具 save_note 维护
      people: "./data/bot-name.people.md"    # 人物画像
      people_chars: 800
      rules:                                 # 规矩层：这个 bot 自己的行为约束
        - "被点名时必须回复"
      extra_diaries: []                      # 还能把别处的文件并进原文层
      # 风格层：留空 = 不注入，这个 bot 完全不受影响（想给哪个 bot 开就写哪个）
      # style: "./data/bot-name.style-stable.md"        # 稳定层（见下面的 style 段）
      # style_recent: "./data/bot-name.style-recent.md" # 近期层
      # style_chars: 800
      # style_recent_chars: 400

digest:                    # 每日摘要生成：把「昨天」压成骨架
  enabled: true
  targets:
    - name: "bot-name"
      diary: "./data/bot-name.md"
      output: "./data/bot-name.digest.md"
  min_entries: 3           # 少于这么多条的一天不生成
  keep_days: 30
  max_per_run: 3           # 单次最多补几天（首次回填分几次跑完）

learn:                     # 风格学习：只读某人的语料，学他「怎么说」（**默认关闭**）
  enabled: false           # ← 要显式打开；不开就一行都不跑
  source: { db: "./data/messages.db", table: "messages" }
  targets:
    - user_id: "20000000"                 # 学**谁**（通常是你自己）
      output: "./data/bot-name.style.md"  # 追加式原文；只学习、不回复
      # persona: 决定「学什么」。不写则用内置兜底 —— 唯一需要针对具体人定制的地方

style:                     # 风格分层：把上面的原文压成「稳定层 + 近期层」
  enabled: false
  targets:
    - source: "./data/bot-name.style.md"
      stable: "./data/bot-name.style-stable.md"   # 每次覆盖写 → memory.bots[].style
      recent: "./data/bot-name.style-recent.md"   # 最近几天的原始条目 → style_recent
      recent_days: 2
      # exclude: ["某人的昵称"]   # 不许出现在风格层里的词；喂给 LLM 前就剔除，产出后再剔一遍

stickers:
  sample_prob: 0.10                    # 图片采样概率
  targets:
    - self_id: "20000000"
      category: "bot-name"             # 图库分类隔离，不同 bot 互不串味
  judge:
    persona: "一名温柔的学生少女"
    max_side: 1200                     # 任一边超过这个像素数就不入库

guard:
  patterns:
    - "API Error"
    - "Request timed out"
    - "Traceback (most recent call last)"

silence:                   # 沉默的权利：不想说话时**真的什么都不发**（默认关闭）
  enabled: false           # ← 要显式打开
  token: "[[silence]]"     # 模型只输出它就代表「这轮不想说话」
  targets: []              # 留空 = 所有 bot
```
