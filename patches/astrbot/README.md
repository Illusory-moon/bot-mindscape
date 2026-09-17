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

补丁会往 `waking_check/stage.py` 插入两个代码块，并用标记包裹：

- `# ══ bot-mindscape auto-wake BEGIN/END ══` —— 配置加载
- `# ── bot-mindscape: 唤醒判定 ──` —— 判定逻辑

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

## 风险与恢复

- 脚本**先备份**再改（`stage.py.bak-mindscape-<时间戳>`）
- 补丁有**标记包裹**，重复执行会跳过
- **框架升级会覆盖补丁**，升级后重跑 `install.py` 即可
- 万一出问题：`install.py --revert` 从最近备份还原

## 其他框架

如果你用的不是 AstrBot，思路是一样的：
找到「消息分发前决定是否处理」的那个阶段，把同样的判定逻辑插进去。
本项目提供的是**策略设计**和**参考实现**。
