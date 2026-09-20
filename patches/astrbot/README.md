# AstrBot 唤醒补丁

## 为什么需要补丁

AstrBot 原生的唤醒机制只有两种：

1. **`wake_prefix`** —— 消息以指定前缀开头
2. **`@bot`** —— 消息里带 At 段

而真实群聊需要的策略是：

- **被 @ 时必回**
- **被叫到名字时必回**（不需要 @）
- **没被叫到时，低概率冒泡**（有存在感但不烦人）
- **不同 bot 用不同的名字和概率**
- **限定在哪些群活动**

这些判定发生在框架的 **`waking_check` 阶段** —— 比插件执行更早，
所以**插件层面无法实现**，只能改框架代码。

## 用法

```bash
# 打补丁（自动备份原文件）
python patches/astrbot/install.py

# 还原
python patches/astrbot/install.py --revert
```

补丁会往 `waking_check/stage.py` 插入三个代码块：

| 块 | 位置 | 作用 |
|---|---|---|
| `_waking_config_block.py` | `__init__` 内 | 配置加载（标记 `══ bot-mindscape auto-wake BEGIN/END ══`） |
| `_waking_judge_block.py` | `if not is_wake:` 之前 | 唤醒判定（标记 `── bot-mindscape: 唤醒判定 ──`） |
| `_waking_ctx_block.py` | 模块级 | 群聊上下文缓冲：渲染消息链 + 落盘 |

### 上下文缓冲解决什么

未被唤醒的群消息**不进 LLM 上下文**，于是 bot 回复时「上下文不全」
（群友 A 说「我吃了 KFC」没被唤醒，B 说「带我去吃呗」被唤醒，
bot 只看到后半句，回一句「吃什么？」）。

这个块把**所有**群消息落一份到 `/opt/astrbot/data/group_ctx_buffer.jsonl`，
再由 `group_context_buffers` 插件注入 —— 框架层和插件层各管一段，
因为唤醒阶段比插件执行更早，插件看不到未唤醒的消息。

### ⚠️ 为什么要自己渲染消息链，不用 `event.message_str`

`event.message_str` 在构建时会把 **「@ 本 bot」那一段去掉**。
于是缓冲里那句只剩「发送者: 内容」—— bot 根本看不出这句话是直接对它说的。
`_ms_render_chain()` 自己遍历消息链，把 At 段还原成 `@昵称`，
这样「被 @ 了」这件事才真的进得了上下文。

## 配置

补丁读取 `/opt/astrbot/data/auto_wake_cfg.json`，格式：

```json
{
  "names": ["bot-name"],
  "exclude_names": ["别人的名字"],
  "sample_prob": 0.02,
  "min_interval": 600,
  "per_bot_names": {
    "20000000": ["bot-name", "小名"]
  },
  "per_bot": {
    "20000000": { "enabled": true, "sample_prob": 0.02, "min_interval": 600 }
  },
  "restricted_groups": {
    "20000000": ["20000001"]
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `names` | 全局触发词（兜底） |
| `exclude_names` | 全局排除词（出现则不触发） |
| `sample_prob` | 冒泡概率（0~1），默认 0.02 = 2% |
| `min_interval` | 两次冒泡的最小间隔（秒） |
| `per_bot_names` | 按 bot 账号指定触发词 |
| `per_bot` | 按 bot 账号覆盖概率/间隔/开关 |
| `restricted_groups` | 按 bot 限定活动群 |

## ⚠️ 权威性：容器内的 `.venv` 不在挂载里

实测（2026-09-18）：`qqbot-astrbot` 只挂了 `/opt/astrbot/data`，
**`/opt/astrbot/.venv` 在容器镜像的可写层里**。也就是说：

- 宿主机上那份 `/opt/astrbot/.venv/...` 与容器内的是**两份不同的文件**，
  宿主机那份会**过期**（实测 291 行 vs 容器 374 行）。
- 改容器内的代码：改完 `docker restart` 生效；
  **容器被删除重建（升级镜像）就会丢**，必须重跑 `install.py`。
- 排查问题时，**永远以容器内的文件为准**：

  ```bash
  docker exec qqbot-astrbot wc -l /opt/astrbot/.venv/lib/python3.13/site-packages/astrbot/core/pipeline/waking_check/stage.py
  ```

## 风险与恢复

- 脚本**先备份**再改（`stage.py.bak-mindscape-<时间戳>`）
- 补丁有**标记包裹**，重复执行会跳过
- **框架升级会覆盖补丁**，升级后重跑 `install.py` 即可
- 万一出问题：`install.py --revert` 从最近备份还原

## 其他框架

如果你用的不是 AstrBot，思路是一样的：
找到「消息分发前决定是否处理」的那个阶段，把同样的判定逻辑插进去。
本项目提供的是**策略设计**和**参考实现**。
