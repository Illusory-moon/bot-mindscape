# 真实环境验证报告

**日期**：2026-09-17
**环境**：真实 AstrBot 4.27.5 + QQ 群聊 + 2GB 内存服务器
**方式**：把整合插件装进生产，用真实群聊验证，验证后保留（B 方案：取代旧插件）

## 一、验证结论

| 能力 | 验证方式 | 结果 |
|---|---|---|
| **插件加载** | 观察启动日志 | ✅ 5 个模块全部 setup 成功 |
| **工具注册** | 观察工具列表 | ✅ recall_memory / save_sticker / send_sticker |
| **记忆注入** | 询问「几天前的事」 | ✅ **她翻到三天前的日记并正确回答** |
| **按需检索** | 日志中的工具调用 | ✅ `recall_memory(keyword='三天前')` |
| **表情包发送** | 日志中的工具调用 | ✅ `send_sticker(want='得意')` 成功出图 |
| **稳定性** | 部署后持续观察 | ✅ 零错误（除正常的工具无返回提示） |

## 二、真机测试发现的缺陷（本地自检抓不到）

这些 bug 的共同特点：**只有真正装进框架、跑真实消息才会暴露**。

### B01 · 配置的 guard.patterns 是「替换」而不是「追加」

**现象**：配置里只写 3 条模式，拦截能力反而比默认更弱，`LLM 响应错误` 漏拦。

**为什么自检没发现**：本地自检用打桩环境，配置路径指向不存在的文件，
**这段代码从未被执行过**（一直走默认分支）。

### B02 · 模块级工具拿不到 event

**现象**：`recall_memory` 拿不到 self_id，查不到日记。

**根因**：框架用 `functools.partial(handler, 插件实例)` 绑定，
模块级函数的第一个位置参数收到的是**插件实例**，不是 event。
必须从 `args` 里用 `hasattr(a, 'get_self_id')` 找出来。

### B03 · 合并器丢装饰器

**现象**：`@llm_tool` / `@filter.xxx` 在合并产物里消失。

**根因**：AST 的 `node.lineno` 指向 `def` 行，**装饰器在 `decorator_list` 里**，
`ast.get_source_segment` 不会带出来。

### B04 · 跨模块 import 悬空

**现象**：`NameError: name 'cfg' is not defined`。

**根因**：源码里 `import mindscape_config as cfg`，合并后该模块不存在。
**解法**：生成一个同名命名空间对象。

### B05 · 解析结果未验类型

**现象**：`'str' object has no attribute 'get'`（采集任务反复报错）。

**根因**：模型偶尔返回 JSON **字符串**而非对象，`.get()` 直接崩。

### B06 · Mixin 属性名冲突（最隐蔽）

**现象**：图库分类读出来是 `format` 段的配置 —— 看起来像配置写错，实际是代码问题。

**根因**：三个 Mixin 都用 `self.c` 存自己的配置段，
`setup` 依次执行，**后执行的覆盖先执行的**。

**解法**：每个 Mixin 用独立前缀（`s_c` / `u_c` / `f_c` / `m_cfg` / `g_c`）。

## 三、架构上的两个硬约束（实测确认）

1. **插件之间不能互相 import** —— 框架把每个插件当独立包（`plugins.<name>.main`）加载
2. **框架只认一个插件类** —— `_get_classes` 找名字以 `plugin` 结尾或叫 `Main` 的类，找到第一个就停

**因此**：模块化源码必须通过 `scripts/build_plugin.py` 合并成**单文件单类**才能部署。

## 四、数据安全

迁移**不搬运、不转换、不覆盖**任何数据，只让新插件指向原路径：

| 数据 | 位置 | 迁移后 |
|---|---|---|
| 长期记忆 | `<你的数据目录>/long_term_memory.md` | 原位未动 |
| 图库 | `/opt/astrbot/data/stickers/` | 原位未动 |
| 人格 | 数据库 personas + SOUL.md | 未动 |

## 五、可复现的验证步骤

```bash
# 1) 构建单文件插件
python scripts/build_plugin.py

# 2) 部署（目标目录必须含 main.py + metadata.yaml）
cp dist/mindscape/* <框架>/data/plugins/mindscape/

# 3) 配置（指向你的真实数据）
cp config/config.example.yaml ~/.mindscape/config.yaml

# 4) 重启框架，观察日志应出现：
#    [mindscape] 插件已加载（5 个模块）

# 5) 群里问一句「几天前的事」，看是否触发 recall_memory
```
