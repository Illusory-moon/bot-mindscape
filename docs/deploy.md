# 部署指南

## 前置条件

- Python 3.9+
- 一个 OpenAI 兼容的 LLM 接口（用于日记提炼、视觉判断、选图）
- 你的 bot 框架的消息库可读（SQLite）

## 步骤

### 1. 放置代码

把 `plugins/` 下的模块放进你框架的插件目录。以 AstrBot 为例：

```bash
cp plugins/*.py /path/to/astrbot/data/plugins/
```

> 部分模块（如 `janitor`、`diary`）是**独立脚本**，可以不放插件目录，
> 直接配合 cron / systemd timer 运行。

### 2. 配置

用 GUI（推荐）：

```bash
python scripts/config_gui.py
```

或手工：

```bash
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml
```

**配置路径很重要**：插件默认读 `~/.mindscape/config.yaml`，而 GUI/Web 管理台默认写项目内的 `config/config.yaml`。两者不一致时，请用环境变量把它们对齐：

```bash
export MINDSCAPE_CONFIG=/absolute/path/to/config.yaml
```

这个变量需要同时给 bot 进程和定时任务（cron / systemd）设置，否则它们会读到不同的配置。相对数据路径（如 `./data/x.md`）是相对**配置文件所在目录**解析的。

配置文件查找顺序：
1. 环境变量 `MINDSCAPE_CONFIG` 指定的路径
2. `~/.mindscape/config.yaml`

### 3. 设置 API Key

**不要把 key 写进配置文件**，用环境变量：

```bash
export MINDSCAPE_API_KEY="your-key-here"
```

或用文件（`diary.llm.api_key_file`）。

### 4. 定时任务

```bash
# 每 6 小时生成一次日记
0 */6 * * * cd /path/to/bot-mindscape && python plugins/mindscape_diary.py

# 每 15 分钟清理一次会话
*/15 * * * * cd /path/to/bot-mindscape && python plugins/mindscape_janitor.py
```

## 常见问题

### Q: 记忆文件一直是空的？

检查 `diary.source.fields` 是否和你的消息表对得上。
先用 `sqlite3 your.db ".schema messages"` 看一下真实字段名。

### Q: 表情包收不到？

- 确认 `stickers.targets` 里的 `self_id` 和实际 bot 账号一致
- 确认视觉模型支持图片输入
- 采样概率默认 10%，可以临时调大测试

### Q: 报错还是漏出来了？

把具体报错文本加到 `guard.patterns` 里。
默认规则覆盖常见情况，但不同框架的报错格式可能不同。

### Q: bot 突然不回消息了？

先跑一次 `mindscape_janitor.py` 看看会话库是不是膨胀了。
这是「假死」最常见的成因：历史太大 → 上传超时 → 看起来卡住。

## 安全提示

- `.gitignore` 已排除 `config/config.yaml` 和 `secrets/`
- 提交前用 `git diff --cached` 检查有无密钥泄漏


---

## 首次部署自检清单

本项目的自动化自检（`scripts/run_selfcheck.py`）只覆盖纯逻辑，
**框架集成部分必须真正装上跑一轮**。按这个顺序逐项确认：

- [ ] `python scripts/run_selfcheck.py` 全绿
- [ ] 插件文件放好后，**框架启动日志里能看到各模块的 loaded 信息**
- [ ] `recall_memory` / `send_sticker` 出现在工具列表里
- [ ] 发一条**正常消息**，bot 回复正常（说明钩子没破坏主流程）
- [ ] 手动制造一次**模型报错**（例如临时填错 API key），确认报错**没有发出去**
- [ ] 发一张契合人设的图，确认被采集进图库（`sample_prob` 可临时调到 1 测试）
- [ ] 问一句「几天前的事」，确认 `recall_memory` 被调用并能答上来
- [ ] 长记忆文件存在时，确认 `system_prompt` 里出现了记忆段落

> 任何一项不通过，先看该模块的 `logger` 输出；
> 本项目的异常都会带模块名前缀（如 `[mindscape_memory]`），便于定位。

