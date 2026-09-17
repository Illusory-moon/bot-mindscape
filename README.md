# bot-mindscape · 灵魂景观

> **为 AI bot 注入持久认知与无缝沉浸的通用增强框架。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 快速开始

```bash
git clone https://github.com/Illusory-moon/bot-mindscape.git
cd bot-mindscape
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml，填入你的 bot 信息
python scripts/run_selfcheck.py    # 自检环境
python scripts/config_gui.py       # 生成配置
```

> **两种管理界面随你挑**：
> - 桌面版：`python scripts/config_gui.py`（tkinter，零依赖）
> - 网页版：`python scripts/web_ui.py`（标准库 http.server，访问 http://127.0.0.1:8777）
>
> 各层怎么对接 bot 框架，见 [部署指南](docs/deploy.md)。

---

## 为什么需要它

现在的群聊 bot 普遍有三个毛病：

| 症状 | 表现 |
|---|---|
| **金鱼记忆** | 清一次历史就失忆，问三天前的事答不上来 |
| **文字机器** | 只会干巴巴打字，从不发表情包，像个客服 |
| **机械出戏** | 服务器一抖，群里就冒出 `API Error: Request timed out` |

`bot-mindscape` 把这三件事拆成 **认知 / 表达 / 沉浸** 三层，一次性解决。

---

## 技术模块

### 一、认知层 Cognition —— 治「金鱼记忆」

记忆不是「一段越堆越长的文本」，而是**四层**，按由近及远注入：

| 层 | 谁写的 | 解决什么 |
|---|---|---|
| **规矩** | 你写在配置里（`bots[].rules`） | 这个 bot 的行为约束，随记忆一起进 prompt |
| **账本** | **bot 当场写**（工具 `save_note`） | 「细节」从此有稳定答案，不再每次现编 |
| **摘要** | 后台每天生成 | 「昨天」压成骨架，几乎不吃窗口预算 |
| **原文** | 后台从聊天流提炼 | 逐条事件，提供细节与口吻 |

| 模块 | 职责 |
|---|---|
| `mindscape_memory` | **分层注入** —— 按 规矩 → 账本 → 摘要 → 原文 拼装，每层独立字数预算，永不膨胀 |
| `mindscape_diary` | **结构化长期记忆** —— LLM 从聊天流提炼事件，写成人类可读的 Markdown；顺带产出人物画像 |
| `mindscape_digest` | **每日摘要** —— 把「已过完的一天」压成一句话，为每个日期只调一次 LLM |
| `mindscape_notes` | **可写的账本** —— 给 bot 一个 `save_note` 工具，它当场就能落笔 |
| `mindscape_recall` | **混合检索 + 边界自知** —— 精确 + 模糊匹配，多词检索，且明说「这只是最近一部分」 |

**设计要点**：记忆存在人类可读的 Markdown 里，不锁在数据库。

为什么是这四层、每一层堵的是哪种失效 —— 见 [设计哲学](docs/why.md)。

### 二、表达层 Expression —— 治「文字机器」

| 模块 | 职责 |
|---|---|
| `mindscape_stickers` | **多模态采集** —— 视觉模型判断图片是否契合人设，自动入库并打标签 |
| `mindscape_sticker_use` | **智能调度** —— 按语境选图 + 概率强制发送 + **斗图队形**（群里在刷图时跟进） |
| `import_stickers.py` | **多源导入** —— 从目录 / 其他插件索引批量入库 |

**设计要点**：图库支持**分类隔离**，不同 bot 用不同素材，互不串味。

### 三、沉浸层 Immersion —— 治「机械出戏」

| 模块 | 职责 |
|---|---|
| `mindscape_guard` | **错误拦截** —— 经该钩子的常见错误文本（API Error / Timeout / Traceback）会被吞掉，不会发出去 |
| `mindscape_format` | **输出规范化** —— 压平多行、去除 AI 腔 |
| `mindscape_rescue` | **空回复救援** —— 推理模型只吐 reasoning、正文为空时，补一次轻量调用兜住 |
| `mindscape_silence`（规划中） | **静默规则** —— 该不说话的时候，真的不说话 |

**设计要点**：这是同类项目几乎没人做的一层。

### 四、唤醒层 Waking —— 治「该说话时不说，不该说时乱说」

| 模块 | 职责 |
|---|---|
| `mindscape_waking` | **唤醒策略** —— @必回 / 提到名字必回 / 低概率冒泡 / 每 bot 独立 / 群白名单 |

**设计要点**：唤醒判定发生在框架的**消息分发阶段**（比插件更早），
因此本项目以**补丁 + 自动安装脚本**的形式提供（见 `patches/`）。

**为什么这层重要**：大多数 bot 要么「不叫不动」，要么「见谁都搭话」。
真正的群聊体感是：**叫它必应，不叫它时偶尔刷个存在感**。

---

### 五、运维层 Ops —— 治「假死」

| 模块 | 职责 |
|---|---|
| `mindscape_janitor` | **会话膨胀清理** —— 防止历史堆到几十 MB 导致请求超时 |
| `scripts/web_ui.py` | **本地管理台** —— 网页查看图库 / 编辑配置 |

---

## 特点与不同点

同类开源项目大多**专攻单维度**（要么只做记忆，要么只做表情包）。

`bot-mindscape` 的不同在于：

1. **三维整合** —— 认知、表达、沉浸是一个整体，不是三个散装脚本
2. **部署极简** —— 配置文件驱动，不需要改源码
3. **报错拦截** —— 目前几乎没有项目做过这一层；
   即使服务器炸了，正在 role-play 的 bot **也不会吐出一句冷冰冰的 API Error**
4. **记忆会分层，而且 bot 能自己记** —— 大多数「长期记忆」只是把历史切片塞进 prompt；
   这里是 规矩 / 账本 / 摘要 / 原文 四层，且**细节由 bot 当场记账**，而不是事后编

---

## 效果预览

### 表情包库（本地预览页）

![表情包库](assets/example-gallery.jpg)

采集模块会自动判断每张图是否契合人设，入库时生成名称、标签和适用场景描述；
不同 bot 的素材按 `category` 隔离，互不串味。

> 预览页由 `scripts/web_ui.py` 提供（零依赖，只监听本机）。

---

## 配置示例

完整版见 [`config/config.example.yaml`](config/config.example.yaml)，这里只挑记忆相关的核心项：

```yaml
memory:
  max_chars: 2500          # 每轮注入的总字数预算
  digest_chars: 1200       #   其中「摘要层」
  notes_chars: 800         #   其中「账本层」
  bots:
    - self_id: "20000000"
      name: "bot-name"
      diary: "./data/bot-name.md"            # 原文层：逐条事件
      digest: "./data/bot-name.digest.md"    # 摘要层：每天一句骨架
      notes: "./data/bot-name.notes.md"      # 账本层：工具 save_note 维护
      people: "./data/bot-name.people.md"    # 人物画像
      people_chars: 800
      rules:                                 # 规矩层：这个 bot 自己的行为约束
        - "被点名时必须回复"
      extra_diaries: []                      # 还能把别处的文件并进原文层

digest:                    # 每日摘要生成：把「昨天」压成骨架
  enabled: true
  targets:
    - name: "bot-name"
      diary: "./data/bot-name.md"
      output: "./data/bot-name.digest.md"
  min_entries: 3           # 少于这么多条的一天不生成
  keep_days: 30
  max_per_run: 3           # 单次最多补几天（首次回填分几次跑完）

stickers:
  sample_prob: 0.10                    # 图片采样概率
  targets:
    - self_id: "20000000"
      category: "bot-name"             # 图库分类隔离，不同 bot 互不串味
  judge:
    persona: "一名温柔的学生少女"
    max_side: 1200                     # 任一边超过这个像素数就不入库

guard:
  patterns:
    - "API Error"
    - "Request timed out"
    - "Traceback (most recent call last)"
```

---

## 文档

- [架构说明](docs/architecture.md)
- [设计哲学](docs/why.md)
- [部署指南](docs/deploy.md)

---

### 远程同步（可选）

在 `ui.sync` 里填好服务器信息后，本地管理台会多出「拉取 / 推送」按钮，也可以在命令行直接用：

```bash
python scripts/mindscape_sync.py status  # 看两边差异
python scripts/mindscape_sync.py pull    # 服务器 -> 本地
python scripts/mindscape_sync.py push    # 本地 -> 服务器
```

**推送前会先拉取远端再合并**，所以不会覆盖远端自动采集的新素材；
删除默认只从索引移除，图片文件留底可恢复。
---

## 许可

MIT License —— 随便用，随便改。

---

## 致谢

本项目的实践场景来自一群真实的群友 —— 感谢他们愿意让一个 AI 在群里「长大」。

### 设计与思路参考

以下开源项目在各自维度上做得很深，本项目的部分设计受其启发：

| 项目 | 协议 | 借鉴点 |
|---|---|---|
| [Komachi-qq-aibot](https://github.com/MagicIndex135731/Komachi-qq-aibot) | MIT | 「证据约束」的检索思路、人物画像维度 |
| [smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) | **GPL-3.0** | *仅借鉴功能构想*（多源图库、斗图队形），**未使用其任何代码** |
| [Yuki-QQbot](https://github.com/YuanYeYouTao/Yuki-QQbot) | MIT | 分层架构的工程组织方式 |

> ⚠️ 本项目为 **MIT** 协议。上表中的 GPL-3.0 项目**仅作为思路来源**，未复制任何代码 ——
> 著作权的保护对象是「表达」而非「思想」。若你打算把本项目与 GPL 项目合并分发，请自行确认合规性。

### 运行环境

- 各 bot 框架（AstrBot / NoneBot2 等）及其社区
- 所有在群里被记住的群友