# 配置作用域：这段配置到底管哪个 bot

> 同容器跑多个 bot 时，「哪段配置管哪个 bot」是事故高发区 —— 尤其**空值**。
> 下面这张表是**从代码实现里盘出来的**（不是设计意图）。

## 三条语义

| 写法 | 含义 |
|---|---|
| 列出 bot 号 | 只对这些 bot 生效（**推荐**） |
| 显式 `["all"]` | 所有 bot 生效。也认 `*` / `全部` / `所有`，大小写不敏感（**意思明确**） |
| **留空 / 不写** | ⚠️ `silence` / `vision` / `format` / `groupctx` 四个模块按「**全部 bot**」处理（历史语义，加载时会警告）；**其余模块按「谁都不生效」处理** |

## 逐模块

| 配置段 | 作用域键 | 空值语义 | 影响面 |
|---|---|---|---|
| `memory.bots[]` 各字段 | 按 `self_id` 精确匹配 | 没配 → 该 bot **没有记忆** | 回复内容 |
| `memory.bots[].knowledge` | 同上 | 没配 → `lookup_knowledge` 无资料 | 回复内容 |
| `memory.bots[].rules` | 同上 | 没配 → 无额外规矩 | 回复内容 |
| `silence.targets` | `self_id` 列表 | ⚠️ **空 = 全部 bot** | 清空整条回复 |
| `vision.targets` | 同上 | ⚠️ **空 = 全部 bot** | 注入识图提醒 |
| `format.targets` | 同上 | ⚠️ **空 = 全部 bot** | 压平回复 |
| `format.no_period` | `self_id` 列表 | ✅ **空 = 关**（子选项，**不**用旧语义） | 去掉出站文本里的「。」 |
| `groupctx.targets` | 同上 | ⚠️ **空 = 全部 bot** | 注入群上下文 + 定向性（还要 `enabled: true`） |
| `stickers.targets[].category` | 按 `self_id` 查表 | 没配 → **谁都不采集** | 图片入库 |
| `sticker_use`（复用 `stickers.targets`） | 同上 | 没配 → **谁都不发图** | 发送内容 |
| `blocklist.targets[].users` | 按 `self_id` 查表 | 没配 → 不拦 | 拦截一切 |
| `guard.patterns` | **全局**（没有 targets） | — | 拦截错误文本 |
| `rescue` | **全局** | — | 空回复补一次 |
| `diary` / `learn` / `style` / `digest` 的 `targets` | 独立脚本各自遍历 | 空 → 不跑 | 后台文件 |
| `janitor` | **全局**（独立脚本） | — | 清会话库 |

## ⚠️ 两个已知陷阱

### 1. 「空 = 全部」的那三个模块

`silence` / `vision` / `format` 的 `targets` **留空会作用于全部 bot**。
两个 bot 同容器时，给 A 开的配置会**悄悄作用到 B** 上。

现在：

- 想表达「确实要全开」，**显式写 `["all"]`**
- **留空会打一条 WARNING**：`enabled=True 但 targets 为空：按旧语义这会作用于全部 bot…`

> 是否把「空 = 全部」改成「空 = 不生效」还在观察期：那是**破坏性变更**，
> 先用警告把存量配置逼出来，再决定。

### 2. 唤醒名字的回落（`auto_wake_cfg.json`）

`per_bot_names` 缺某个 bot 时会**回落到全局 `names`**，而全局是**两个 bot 名字的并集**
→ 表现就是「有人叫 B 的名字，A 也醒」。加 bot 时**务必两边都写**。

## 自检怎么保证

`scripts/run_selfcheck.py` 的 **R32** 用两个假 `self_id` 做**钩子级**检查：

- 只给 A 配 → B 的**沉默 / 识图 / 图库分类 / 日记文件**都不受影响
- 显式 `["all"]` → 两个都生效
- 空表 + `enabled` → **必须告警**（且 `all` 与未启用时不该告警）
- `None` / 空串 / 纯空白会被规范掉 —— 不会变成字面量 `"None"` 混进作用域
