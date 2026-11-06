# 文档目录

> README 只留骨架；**细节都在这里**。按「我想干什么」找 ✓

## 第一次来

| 文档 | 一句话 |
|---|---|
| [QUICKSTART](../QUICKSTART.md) | **5 分钟跑到「它在群里回你一句」**（三条路径 + 三步验收）|
| [INSTALL](../INSTALL.md) | 机器友好的分步清单（每步一条命令 + 期望输出）|
| [DEPLOY-WITH-AI](../DEPLOY-WITH-AI.md) | **把这一页粘给 AI**，让它替你装（写死默认路径，别反问）|
| [部署指南](deploy.md) | 完整的部署说明：构建单文件 → 放插件目录 → 配置 → API Key → 补丁 |
| [配置：核心项](config.md) | 记忆 / 摘要 / 风格 这几块最容易配错的字段 |
| [../config/config.example.yaml](../config/config.example.yaml) | **完整配置**，每个字段都有注释 |
| [../config/config.minimal.yaml](../config/config.minimal.yaml) | **最小可用配置**，先跑起来用 |

## 卡住了

| 文档 | 一句话 |
|---|---|
| [常见问题对照表](troubleshooting.md) | 模块不加载 / 不回复 / 配置读不到 / 报错漏进群 / 管理台 / 依赖装不上 |
| `python scripts/check_deps.py` | **依赖自检**：打印当前解释器路径、每个包装没装、缺了怎么装（`--json` 给 AI）|

## 想弄懂它

| 文档 | 一句话 |
|---|---|
| [技术模块详解](features.md) | 六层分别治什么 + 消息在框架里的流向图 + 每个模块的职责 |
| [架构说明](architecture.md) | 代码怎么分层、模块怎么拼成单文件、钩子挂在哪 |
| [设计哲学](why.md) | 为什么这么设计、放弃了什么 |
| [状态与范围](scope.md) | 这个项目**做什么 / 不做什么** |

## 运维与验证

| 文档 | 一句话 |
|---|---|
| [远程同步](remote-sync.md) | 本机开管理台、服务器跑 bot 时，怎么拉图库/记忆/日志（可选） |
| [去重表 seen.json](seen-table.md) | 采集去重表长什么样、谁维护它（**只拉不推**） |
| [冒烟测试报告](smoke-test-report.md) | 一次真机验证记录，可以照着复现 |
| [审查提示词](review-prompt.md) | 让 AI 审这个项目时用的固定提示词 |

## 其它

| 文档 | 一句话 |
|---|---|
| [彩蛋](egg.md) | 「沉默权」授权的来龙去脉 |
| [致谢与参考](credits.md) | 借鉴过哪些项目（含协议提示）、运行环境 |

---

## 兼容与运行环境

| 项 | 要求 | 备注 |
|---|---|---|
| **Python** | **3.11+** | `scripts/check_live.py` 用了 `hashlib.file_digest`，3.11 才有 |
| **AstrBot** | **4.x** | 插件以 4.x 的插件目录与 DB（`data_v4.db`）为准；更早版本未验证 |
| **依赖** | `PyYAML` / `httpx` / `Pillow` / `paramiko` | 见 `requirements.txt`；**装到 AstrBot 用的那个解释器里** ✗ |
| **LLM 接口** | OpenAI 兼容 | 日记提炼 / 视觉判断 / 选图需要；只跑记忆+表达+沉浸不需要模型 |
| **消息库** | 可读的 SQLite | 生成日记用 `diary.source.db`；会话清理用 `janitor.db`（4.x 是 `data_v4.db`）|
| **管理台** | 不需要 AstrBot 网页界面 ✓ | 桌面 `scripts/config_gui.py` / 网页 `scripts/web_ui.py`（默认只听 127.0.0.1）|
| **补丁** | 可选 | 只有「唤醒策略 / 自主冒泡 / groupctx」需要改 AstrBot 本体 |
