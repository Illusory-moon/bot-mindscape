# INSTALL · 机器友好版安装清单

> 给**人和 AI 都能照着执行**的分步清单：每步一条命令 + 期望输出 + 失败时看哪。
> 想读人话版 → [QUICKSTART.md](QUICKSTART.md)；想知道为什么 → [docs/deploy.md](docs/deploy.md) ✓

## 前置

| 项 | 要求 | 怎么查 |
|---|---|---|
| Python | **3.11+** | `python --version` |
| 依赖 | `PyYAML` / `httpx` / `Pillow` / `paramiko` | `pip install -r requirements.txt` |
| LLM 接口 | OpenAI 兼容（日记提炼 / 视觉判断 / 选图）| `export MINDSCAPE_API_KEY=...` |
| 消息库 | 可读的 SQLite（AstrBot 自带 `data_v4.db` 即可）| 见 [B2](#b2-消息来源库怎么指) |
| AstrBot | 有就能装插件；没有可用 Docker | 找 `data/plugins/` 目录 |

## 步骤

| # | 命令 | 期望 | 失败看 |
|---|---|---|---|
| 1 | `pip install -r requirements.txt` | 无报错 | [troubleshooting · 依赖](docs/troubleshooting.md#装不上依赖) |
| 2 | `cp config/config.example.yaml config/config.yaml` | 文件存在 | — |
| 3 | 编辑 `config/config.yaml`：填 `memory.bots[0].self_id` 与三处数据路径 | YAML 能解析：`python -c "import yaml;yaml.safe_load(open('config/config.yaml',encoding='utf-8'))"` → `None`（无输出即成功）| [troubleshooting · 配置](docs/troubleshooting.md#配置读不到) |
| 4 | `python scripts/run_selfcheck.py` | `失败 0 项` | 缺包 → 回第 1 步 |
| 5 | `python scripts/build_plugin.py --market` | 生成 `dist/mindscape/main.py` | 语法错误会在此报出 |
| 6 | `grep -c mindscape_janitor dist/mindscape/main.py` | **`0`** | 不为 0 ⇒ 构建参数用错了 |
| 7 | 复制到 AstrBot：`data/plugins/mindscape/main.py` | 文件存在 | — |
| 8 | 复制配置：`data/plugin_data/astrbot_plugin_mindscape/config.yaml` | 与第 3 步同一份内容 | [B1](#b1-不打补丁能用哪些功能) |
| 9 | 重启 AstrBot | 日志出现 `插件已加载（N 个模块）` | [troubleshooting · 不加载](docs/troubleshooting.md#模块不加载) |
| 10 | 私聊一句 + 群里 @ 一次 | 都有回复 | [troubleshooting · 不回复](docs/troubleshooting.md#不回复--不唤醒) |
| 11 | （可选）`python patches/astrbot/install.py` | 提示已备份/已存在 | 只在要「唤醒 / 冒泡」时做 |

## 常被问到的三件事

### B1 不打补丁能用哪些功能

| 功能 | 不打补丁 | 需要补丁 |
|---|---|---|
| 认知层（记忆 / 摘要 / 账本 / 风格 / 资料库）| ✅ | — |
| 表达层（格式 / 表情包 / 语音）| ✅ | — |
| 沉浸层（报错拦截 / 沉默 / 空回复救援 / 隐私）| ✅ | — |
| 观测层（每轮体积与耗时）| ✅ | — |
| **唤醒层**：@必回 / 提到名字必回 / 低概率冒泡 | ❌ | ✅ |
| **唤醒层**：`groupctx` 群上下文补齐 | ❌ | ✅（且默认关闭，打了才建议开）|
| **自主冒泡**（没人叫也开口）| ❌ | ✅ |

⇒ **不打补丁 = 少「它自己开口」这一块，其余照常** ✓

### B2 消息来源库怎么指

「长期记忆」要**从消息库里提炼原文**，所以要告诉它库在哪。目前有两个不同的库，别混：

| 用途 | 配置键 | 典型值 |
|---|---|---|
| **生成日记 / 学语气**（认知层）| `diary.source`（`learn.source` 同结构）| `db: "./data/messages.db"` · `table: "messages"` · `fields.time: "timestamp"` |
| **会话膨胀清理**（运维层）| `janitor.db` | AstrBot 4.x 是 `<数据目录>/data_v4.db`，填绝对路径最稳 |

**校验**（用 Python，不依赖 sqlite3 命令行）：
```bash
python -c "import sqlite3;c=sqlite3.connect('你的库路径');print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")][:10])"
```
**期望**：打印出表名列表（比如 `['messages', ...]` 或 `['conversations', ...]`）。

> 没有 `messages.db` 怎么办：`diary.source` 可以先不填（日记就不会自动生成，其余功能照常 ✓）；
> 但**检索**要有东西可搜 —— 先让 bot 在群里说几天话、开 `archive` 归档，或者手工写几条进 `diary` 文件 ✓

### B3 三步验收

1. 启动日志：`插件已加载（16 个模块）` + 每个 `[mindscape_xxx] loaded`
2. 私聊一句 → 有回复
3. 群里 @ → 有回复

三步都过 = 装好了 ✓ 有一步不过就去 [troubleshooting](docs/troubleshooting.md) ✓
