# DEPLOY-WITH-AI · 让 AI 替你装（把这一页整段粘给它）

> 用法：把**下面「提示词」整块**粘给 AI，并告诉它仓库已 clone 在哪。
> 它是写给 AI 的操作说明，**不是给人看的**（人看 → [QUICKSTART.md](QUICKSTART.md)）✓

---

<!-- 从下一行开始整段复制 -->

## 你的任务

把 `bot-mindscape` 装进一个 AstrBot。**先自己判断走哪条路，不要反问用户**：

| 判据 | 结论 |
|---|---|
| 机器上已经有部署在运行的 AstrBot（能找到 `data/plugins/` 目录）| **路径 A：只装插件** |
| 没有 AstrBot，但有 Docker | **路径 B：Docker 跑 AstrBot + 装插件** |
| 用户明确要「唤醒策略 / 自主冒泡」| **路径 C：源码 AstrBot + 打补丁**（在 A/B 之上追加）|

**默认取 A**。只有在确实找不到 AstrBot 且没有 Docker 时才问用户。

## 铁律（违反即返工）

1. **不要改 AstrBot 的网页界面**。本项目的配置由**它自己的管理台**维护
   （`scripts/config_gui.py` 桌面版 / `scripts/web_ui.py` 网页版），或直接编辑配置文件 ✓
2. **全项目只认一份 `config.yaml`**。插件、定时脚本都必须指到同一份（用 `MINDSCAPE_CONFIG` 环境变量）。
   多一份副本 = 两边不同步，会互相覆盖 ✗
3. **API Key 不写进配置文件**，用环境变量 `MINDSCAPE_API_KEY` 或 `diary.llm.api_key_file` ✓
4. **插件必须是构建产物**：`dist/mindscape/main.py`（由 `scripts/build_plugin.py --market` 生成）。
   不要 `cp plugins/*.py` —— AstrBot 一个插件只实例化一个类，多文件会绑错实例 ✗
5. 任何写操作前**先备份**（配置、数据库）；改完**验证**，不要只看命令退出码 ✓

## 步骤（每步都有校验）

### 0. 环境
```bash
python --version                  # 需要 3.11+
pip install -r requirements.txt
```
**期望**：Python ≥ 3.11；依赖安装无报错。

### 1. 配置
```bash
cp config/config.example.yaml config/config.yaml
```
- **最小可用**可以只留 `memory:` 一段（参考 `config/config.minimal.yaml`），其余整段删掉或注释
- 必填：`memory.bots[0].self_id`（bot 的 QQ 号）、`name`、`diary`/`notes`/`people` 三个路径
- ⚠️ **多 bot 共用同一容器时**，`stickers.targets` / `vision.targets` 必须显式写 bot 号：
  **空表 = 作用于全部 bot**（历史事故点）

**校验**：
```bash
python -c "import yaml,sys; yaml.safe_load(open('config/config.yaml',encoding='utf-8')); print('YAML OK')"
```
**期望**：`YAML OK`

### 2. 自检
```bash
python scripts/run_selfcheck.py
```
**期望**：`失败 0 项`。失败时先看失败项名字：`No module named ...` 是缺依赖，不是代码坏。

### 3. 构建插件
```bash
python scripts/build_plugin.py --market
ls -l dist/mindscape/main.py
grep -c mindscape_janitor dist/mindscape/main.py
```
**期望**：产出 `main.py`；**`grep` 计数必须是 `0`**（janitor 不进插件产物）。

### 4. 安装到 AstrBot
```bash
# 路径 A（已有 AstrBot）
mkdir -p <ASTRBOT>/data/plugins/mindscape
cp dist/mindscape/main.py <ASTRBOT>/data/plugins/mindscape/main.py

# 配置放到插件读的位置
mkdir -p <ASTRBOT>/data/plugin_data/astrbot_plugin_mindscape
cp config/config.yaml <ASTRBOT>/data/plugin_data/astrbot_plugin_mindscape/config.yaml
```
**路径 B（Docker）**：把上面两个路径换成宿主机挂载目录（`<挂载>/plugins/...`、`<挂载>/plugin_data/...`），
然后 `docker restart <容器名>`。

**校验**：
```bash
docker logs --tail 200 <容器名> | grep -E '插件已加载|mindscape_.*loaded'
```
**期望**：`[mindscape] 插件已加载（N 个模块）` + 每个模块一行 `loaded`。
（N 随模块数变化，**别背数字**）

### 5. 验收
- 私聊 bot 发一句 → **有回复**
- 群里 @ 一次 → **有回复**
- 没反应 → 读 [docs/troubleshooting.md](docs/troubleshooting.md) 的「不唤醒 / 不回复」两节 ✓

### 6.（可选）路径 C：打补丁
```bash
python patches/astrbot/install.py            # 先备份，已打过会跳过
python patches/astrbot/install.py --revert   # 还原
```
打完之后才可以在配置里开 `groupctx.enabled: true` —— **没打补丁就开它比不开更糟** ✗

## 报告格式（装完这样回报）

```
路径：A / B / C
AstrBot 版本：<若知道>
模块数：<日志里的 N>
自检：通过 N 项 / 失败 0 项
验收：私聊 ✓ 群 @ ✓
改动过的文件：<列表；配置与数据库备份路径>
遗留问题：<有就写，没有写「无」>
```

<!-- 复制到此为止 -->
