# 常见问题对照表

> 先按「症状」查，再按「原因」验证，最后照「怎么办」做 ✓
> 装的时候没这么细的问题 → 直接看 [QUICKSTART](QUICKSTART.md) / [INSTALL](INSTALL.md)

## 模块不加载

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 日志里**一条 `[mindscape_...] loaded` 都没有** | 插件没被 AstrBot 发现 | 确认路径是 `<astrbot>/data/plugins/mindscape/main.py`，**目录名随便、文件名必须是 `main.py`** ✓ 然后重启 |
| 有 `插件已加载（N 个模块）`，但某个模块的 `loaded` 没有 | 那个模块的配置有错（YAML 段落写坏最常见）| 用 `python -c "import yaml;yaml.safe_load(open('config.yaml',encoding='utf-8'))"` 验一遍 |
| 模块数比文档里少 | 你装的是**旧构建产物** | 重新 `python scripts/build_plugin.py --market` 再复制 |
| 报 `ModuleNotFoundError` | 缺依赖 | `pip install -r requirements.txt`（**装到 AstrBot 用的那个 Python 里** ✗ 常见错：装到了另一个解释器）|

## 不回复 / 不唤醒

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 私聊回、群里不回 | 群里没唤醒它 | 群里 **@ 一次**试试；再不行看下节的「唤醒」 |
| @ 了也不回 | ① 唤醒补丁没打 ② 名单/群白名单拦了 | ① `python patches/astrbot/install.py` ② 看 AstrBot 日志有没有 `whitelist_check ... stopped` |
| 什么都不说，但日志有「真静默」 | **这是特性**：它选了沉默 ✓ | 配置里关掉 `silence`，或检查沉默令牌触发频率 |
| 群里冒出一句「（没有回复）」之类 | 沉默令牌被当正文发了 | 该版本 bug，升级到最新构建 |
| 回得很慢 | 每轮注入体积大 | 看 `[mindscape_trace]` 那行体积数字，压 `memory.max_chars` / `digest_chars` |

## 配置读不到

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 改了 `config/config.yaml` 但没生效 | 插件读的是**插件数据目录**那份 ✗ | 插件读 `<astrbot>/data/plugin_data/astrbot_plugin_mindscape/config.yaml`；或设 `MINDSCAPE_CONFIG` 指到你那份 |
| 数据文件路径找不到 | 相对路径是**相对配置文件所在目录** | 要么写绝对路径，要么把数据跟着配置一起放 ✓ |
| 定时脚本和插件「记忆不同步」 | 两边读到**不同的 config.yaml** | 定时任务也设 `MINDSCAPE_CONFIG`，全项目只认一份 ✗ |

## 报错漏进群

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 群里出现 `API Error` / traceback | 报错拦截没生效 | 确认 `guard.enabled: true`；确认出站没走**绕过结果管线**的自定义发送 |
| 群里出现内部标记（`<xxx>`、`|||`、思考原文）| 老版本 | 升级构建产物（新版在出站统一整理）|

## 工作台 / 管理台

| 症状 | 原因 | 怎么办 |
|---|---|---|
| **看到 AstrBot 的网页界面就发懵** | ⚠️ **本项目不用它配** ✗ | 用我们自己的：桌面 `python scripts/config_gui.py`；网页 `python scripts/web_ui.py`（或双击 `open_dashboard.bat`，端口 8777–8787 自动挑）|
| 管理台打开是空白/端口占用 | 端口被占 | 关掉占用的进程，或 `python scripts/web_ui.py --port 8790` |
| 「同步服务器」按钮点了没反应 | `ui.sync` 没配 | 见 [远程同步](remote-sync.md)（需要 SSH 与远端路径）|
| 管理台改了但 bot 没变 | 只改了**本机副本**，没同步 | 点「同步服务器」，或手动把 `config/managed.yaml` 推到远端并重启 |

## 装不上依赖

> **插件启动时会自己报**：缺哪个依赖、会导致什么功能失效、该怎么装 —— 日志里搜 `依赖自检` ✓
> 看到 `[mindscape] 依赖自检：缺 1 项（paramiko）` 这种就说明只是某个可选功能不可用，其余照常 ✓

> **先跑这一条**，它会同时告诉你「装到哪个解释器」和「缺哪个包」：
> ```bash
> python scripts/check_deps.py
> ```
> 第一行 `Python 解释器 : <路径>` 就是**你应该 pip 的那个** ✗ 装到别的解释器等于没装 ✓

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `pip install` 报编译错误 | 用了系统 Python / 缺编译环境 | 建虚拟环境：`python -m venv .venv` 再装 ✓ |
| 装完还是 `ModuleNotFoundError` | 装到了另一个解释器 ✗ | `python -c "import sys;print(sys.executable)"` 对着看 |
| `PyYAML` 装上了但 AstrBot 报缺 | AstrBot 在容器里，宿主机装的它看不到 ✗ | 装到**容器内**：`docker exec -it <容器> pip install pyyaml httpx pillow` |

## 还是不行

按这个顺序收集信息再问（省一半来回）：

```bash
python --version && python -c "import sys;print(sys.executable)"
python scripts/run_selfcheck.py | tail -5
docker logs --tail 100 <容器名> | grep -iE 'mindscape|error' | tail -30
```
