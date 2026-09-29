# 部署指南

> **AstrBot 只有这一条安装入口**（README 的快速开始也指向这里）。
> 别的框架见文末「其他框架」。

## 前置条件

- Python 3.9+
- 一个 OpenAI 兼容的 LLM 接口（日记提炼 / 视觉判断 / 选图）
- bot 框架的消息库可读（SQLite）

## AstrBot 安装（四步）

### 1. 构建成【单文件单类】

```bash
python scripts/build_plugin.py --market      # 产出 dist/mindscape/main.py
```

⚠️ **不要 `cp plugins/*.py`**。AstrBot 把每个插件当独立包加载，而它的
`_get_classes` **只认一个插件类**（找到第一个就 break）—— 多文件各一个类时，
只有一个会被实例化，**其余钩子会绑到错的实例上**。
所以本项目由构建脚本把各模块合并成一个 `MindscapePlugin`。

### 2. 安装

```bash
mkdir -p /path/to/astrbot/data/plugins/mindscape
cp dist/mindscape/main.py /path/to/astrbot/data/plugins/mindscape/main.py
# 然后重启 AstrBot
```

启动日志应看到：

```
[mindscape] 插件已加载（N 个模块）
[mindscape_xxx] loaded | ...
```

N = Mixin 个数（随模块增减），每个模块还会各打一行 `loaded`。

### 3. 配置

插件在 AstrBot 里读 **插件数据目录**：

```
<astrbot 数据目录>/plugin_data/astrbot_plugin_mindscape/config.yaml
```
（即 `StarTools.get_data_dir("astrbot_plugin_mindscape")/config.yaml`）

查找顺序：

1. 环境变量 `MINDSCAPE_CONFIG` 指定的路径
2. `<astrbot 数据目录>/plugin_data/astrbot_plugin_mindscape/config.yaml`
3. 框架外运行脚本时：`<项目>/config/config.yaml`

```bash
export MINDSCAPE_CONFIG=/absolute/path/to/config.yaml
```

**相对路径（如 `./data/x.md`）是相对「配置文件所在目录」解析的** ——
换了配置位置，数据文件也跟着换位置，注意别让两处指到不同的数据。

> ⚠️ **全项目只认一份配置。** 独立脚本（见定时任务）也必须指到同一份：
> 多一份副本就多一次「两边不同步、互相覆盖」的机会 —— 真踩过。

### 4. 设置 API Key

**不要把 key 写进配置文件**，用环境变量：

```bash
export MINDSCAPE_API_KEY="your-key-here"
```

或用文件（`diary.llm.api_key_file`）。

## 可选的框架集成：唤醒策略 + 上下文补齐

只想要「记忆 + 表达 + 沉浸」，到第 4 步就够了。要让 bot **知道什么时候该开口**，
还得给框架打一个补丁 —— 因为唤醒判定发生在**消息分发阶段**，插件来不及插手：

```bash
python patches/astrbot/install.py             # 打补丁（先备份；已打过会跳过）
python patches/astrbot/install.py --revert    # 从备份还原
```

补丁做两件事：

1. **唤醒策略** —— @必回 / 提到名字必回 / 低概率冒泡，且每个 bot 独立配置
2. **落盘群消息** —— 把**所有**群消息写一份到 `<astrbot 数据目录>/group_ctx_buffer.jsonl`

第 2 条的读者是插件的 `groupctx` 模块（**默认关闭**，要先打补丁再打开）：

```yaml
groupctx:
  enabled: true
  targets: ["all"]        # 或列出 bot 号
```

> ⚠️ **没打补丁就打开 `groupctx`，比不开更糟**：它拿不到补丁记的「唤醒原因」，
> 会把「这条不是对你说的」当成结论注入。所以默认是关的。

## 定时任务（可选）

这些是**独立脚本**，不进插件目录也能跑：

```bash
# 每 10 分钟提炼一次日记（顺带补每日摘要）
*/10 * * * * MINDSCAPE_CONFIG=/path/to/config.yaml \
  python /path/to/bot-mindscape/plugins/mindscape_diary.py

# 每 15 分钟清理会话历史（防「假死」）
*/15 * * * * MINDSCAPE_CONFIG=/path/to/config.yaml \
  python /path/to/bot-mindscape/plugins/mindscape_janitor.py
```

可直接运行的脚本：`mindscape_diary` / `mindscape_digest` / `mindscape_learn` /
`mindscape_style` / `mindscape_janitor`。

> `mindscape_janitor` 是**唯一不进插件产物**的模块（它直接改框架数据库），
> 只作为独立脚本使用。
> **定时任务必须设 `MINDSCAPE_CONFIG`**，否则它和插件会各读一份配置 ——
> 表现就是「bot 当场记得的事，摘要里没有」这种诡异的不同步。

## 其他框架

本项目只依赖三样东西：

- 一个能改写 `system_prompt` 的钩子（记忆 / 风格 / 静默）
- 一个「LLM 请求前、结果发出前」的钩子（拦截 / 格式压平）
- 消息库可读（SQLite）

`plugins/` 下的模块是**库**，`MindscapePlugin` 只是把钩子接起来的胶水。
换框架时重写这一层胶水即可，其余模块不用改。

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

- `.gitignore` 已排除 `config/config.yaml`、`config/config.local.yaml`、`data/`
- 提交前跑 `python scripts/run_selfcheck.py`（含**脱敏扫描**，会认 `.gitignore`）
- 真实 QQ 号 / 群号 / 昵称不要出现在本仓库；人格与语料放另一个仓库

---

## 首次部署自检清单

自动化自检（`scripts/run_selfcheck.py`）覆盖面已经不小 —— 其中一项会在临时目录
**按 `--market` 重新构建**，与 `dist/mindscape/main.py` 逐字节比较，不一致直接失败
（防止「改了源码忘了构建」就发布）。

但**框架集成部分仍必须真正装上跑一轮**，按这个顺序逐项确认：

- [ ] `python scripts/run_selfcheck.py` 全绿
- [ ] 插件文件放好后，**启动日志里能看到合并插件与各模块的 `loaded`**
- [ ] `recall_memory` / `send_sticker` 出现在工具列表里
- [ ] 发一条**正常消息**，bot 回复正常（说明钩子没破坏主流程）
- [ ] 手动制造一次**模型报错**（例如临时填错 API key），确认报错**没有发出去**
- [ ] 发一张契合人设的图，确认被采集进图库（`sample_prob` 可临时调到 1 测试）
- [ ] 问一句「几天前的事」，确认 `recall_memory` 被调用并能答上来
- [ ] 长记忆文件存在时，确认 `system_prompt` 里出现了记忆段落

> 任何一项不通过，先看该模块的 `logger` 输出；
> 本项目的异常都会带模块名前缀（如 `[mindscape_memory]`），便于定位。
