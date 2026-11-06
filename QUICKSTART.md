# QUICKSTART · 从零到「它在群里回你一句」

> 目标：**不要读别的文档**，照着这一页走完。全程约 5–15 分钟（取决于你有没有 AstrBot）。
> 卡住了 → [常见问题对照表](docs/troubleshooting.md) ✓

## 第 0 步 · 先选跑法（30 秒决定，别再纠结）

| 你的情况 | 选这条 | 说明 |
|---|---|---|
| **已经有 AstrBot 在跑** | **A. 装插件**（最常见）| 只往插件目录放一个 `main.py`，重启即可 |
| 没有 AstrBot，也不想装源码 | **B. Docker Desktop** | 用容器跑 AstrBot，插件照样放进它的 `data/plugins/` |
| 需要「唤醒策略 / 自主冒泡」这两个功能 | **C. 源码 AstrBot + 打补丁** | 这两项要求改 AstrBot 本体（见 [INSTALL · B1](INSTALL.md#b1-不打补丁能用哪些功能)）|
| 只是想先把配置和记忆跑通 | **A 或 B**，补丁以后再说 | 不打补丁也能用大部分功能 ✓ |

> ⚠️ **先记住一件事**：本项目的配置**不走 AstrBot 的网页界面** ✗
> 我们有自己的管理台（桌面 `scripts/config_gui.py` / 网页 `scripts/web_ui.py`），
> 它改的是**你本机的 config 副本**，再同步到 bot 那边。AstrBot 的 web UI 只用来装插件、绑人格 ✓

---

## A. 已经有 AstrBot（3 步）

```bash
# 1) 克隆本项目（构建脚本在本地跑，不需要进容器）
git clone https://github.com/Illusory-moon/bot-mindscape.git
cd bot-mindscape
pip install -r requirements.txt

# 2) 复制配置并按提示填写（最小可用配置在 config/config.minimal.yaml）
cp config/config.example.yaml config/config.yaml
python scripts/config_gui.py          # 桌面管理台；或双击 open_dashboard.bat 用网页版

# 3) 构建单文件插件
python scripts/build_plugin.py --market     # 产出 dist/mindscape/main.py
```

把 `dist/mindscape/main.py` 放进 **AstrBot 的插件目录**，然后重启 AstrBot：

```bash
# 目录形如：<astrbot>/data/plugins/mindscape/main.py
```

配置也要放到插件读的那份位置（详见 [部署指南](docs/deploy.md)）：

```
<astrbot>/data/plugin_data/astrbot_plugin_mindscape/config.yaml
```

> 用管理台的「**同步服务器**」按钮可以直接推过去（要先在 `ui.sync` 填好路径/SSH，见 [远程同步](docs/remote-sync.md)）。

**成功标志**（AstrBot 启动日志里应出现）：
```
[mindscape] 插件已加载（16 个模块）
[mindscape_memory] loaded | 1 bot(s) 配置了记忆
```

---

## B. 没有 AstrBot，用 Docker Desktop

1. 装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，启动后确认 `docker ps` 能用
2. 按 AstrBot 官方文档起一个容器（关键是**把 `data/` 挂到宿主机**，否则重启就丢）：
   ```bash
   docker run -d --name astrbot -p 6185:6185 -v /your/data:/AstrBot/data soulter/astrbot
   ```
   > 镜像名/端口以 AstrBot 官方文档为准；这一行只是「要挂 data 目录」的示例 ✓
3. 之后和 **A 的三步** 完全一样，只是路径换成宿主机的挂载目录：
   ```
   /your/data/plugins/mindscape/main.py
   /your/data/plugin_data/astrbot_plugin_mindscape/config.yaml
   ```

**成功标志**：`docker logs -f astrbot` 里出现同样的两行 `loaded` ✓

---

## C. 源码 AstrBot + 打补丁（想要唤醒/冒泡才需要）

先按 A 走完，再：

```bash
python patches/astrbot/install.py          # 打补丁（先备份；已打过会跳过）
python patches/astrbot/install.py --revert # 后悔了：还原
```

补丁只做两件事：**唤醒策略**（@必回 / 提到名字必回 / 低概率冒泡）与**把所有群消息落一份盘**
（给 `groupctx` 用，默认关闭）。**不打补丁 → 这两项用不了，其余照常** ✓

---

## 装完之后 · 三步验收

| # | 做什么 | 期望 |
|---|---|---|
| 1 | 看启动日志 | `插件已加载（16 个模块）` + 每条 `[mindscape_xxx] loaded` |
| 2 | 私聊 bot 一句「你还记得我上次说的事吗」 | **会回话**（回得准不准另说）|
| 3 | 群里 @ 它一次 | 有回应；如果没反应 → [troubleshooting](docs/troubleshooting.md) 的「不唤醒」一节 |

想更稳一点，再跑一次自检（构建后自检最全）：

```bash
python scripts/run_selfcheck.py     # 期望：通过 N 项 / 失败 0 项
```

---

## 接下来读什么

- 想让记忆真的有内容 → [配置：核心项](docs/config.md)（`memory` / `diary` / `digest` / `learn`）
- 想让它的语气像某个人 → 同上 `learn` + `style` 两段
- 想知道每层到底做了什么 → [技术模块详解](docs/features.md)
- 让 AI 替你部署 → [DEPLOY-WITH-AI.md](DEPLOY-WITH-AI.md)
