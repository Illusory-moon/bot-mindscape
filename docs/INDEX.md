# 文档目录

> README 只留骨架；**细节都在这里**。按「我想干什么」找 ✓

## 第一次来

| 文档 | 一句话 |
|---|---|
| [部署指南](deploy.md) | **装进 AstrBot 的完整步骤**（构建单文件 → 放插件目录 → 重启 → 看日志），含前置条件与补丁说明 |
| [配置：核心项](config.md) | 记忆 / 摘要 / 风格 这几块最容易配错的字段 |
| [../config/config.example.yaml](../config/config.example.yaml) | **完整配置**，每个字段都有注释 |

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

## 运行环境（摘要）

- **Python 3.11+**（`scripts/check_live.py` 用了 `hashlib.file_digest`）
- 依赖：`requirements.txt`（PyYAML / httpx / Pillow / paramiko 等）
- 一个 OpenAI 兼容的 LLM 接口（日记提炼 / 视觉判断 / 选图）
- bot 框架的消息库可读（SQLite）
