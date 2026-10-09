# 2026-10-09 · 压小 tool 结果体量（主人批：1+2 一起做）

**背景**：实测 tool 消息占会话 **24.9%** ✓ 而其中 `web_search_tavily` **一家占 68%** ✗
（每次搜索 6~10k 字 ✓ 默认一次拿 7 条 ✓）→ 每次请求都要重发一遍 ✗

**改了两处（都是框架文件 ✓ 在容器内 ✓）**：

| 文件（容器内路径） | 原值 | 新值 |
|---|---|---|
| `.../astrbot/core/tools/web_search_tools.py` | `kwargs.get("max_results", 7)` | `…, 5)` ✓ |
| 同文件（schema 说明 ✓） | `Default is 7. Range is 5-20.` | `Default is 5. … Prefer the smallest number that answers the question.` ✓ |
| `.../astrbot/core/agent/runners/tool_loop_agent_runner.py` | `TOOL_RESULT_MAX_ESTIMATED_TOKENS = 27_500` | `= 3_000` ✓ |
| 同文件 | `TOOL_RESULT_PREVIEW_MAX_ESTIMATED_TOKENS = 7000` | `= 800` ✓ |

**阈值的实际效果**：这套落盘机制要求同时启用框架的文件读取工具。当前部署没有该工具，所以上述阈值不会压短结果。不要把这两项常量当作已经生效的节流措施。

**2026-10-09 补充**：`tool_context_limit.py` 在 `ToolLoopAgentRunner._sanitize_contexts_for_provider` 里把每条超长 `tool` 结果限到 2,000 字，剥离旧用户消息里的注入块，并清空 DeepSeek 旧轮次的思考文本。本轮和最近一轮用户消息保留。它只改发给 provider 的副本，不改 `run_context.messages` 和会话数据库；`tool_call_id` 原样保留。补丁只适配已验证的框架版本，版本不符会拒绝应用。构造与测试命令：

**15:36 再补**：确认一个群在约 6 分钟内连续触发 13 次工具回合、22 条工具结果；配置 `max_agent_step` 原为 30，现改 3（第 3 步后框架仍会发一次不带工具的收尾请求）。
同一补丁现在还在 `_append_tool_call_result` 入会话前把每条结果限到 1900 字+截断标记，保留 `tool_call_id`；现有异常会话 rowid=97 的 20 条旧结果也做了同样裁剪。
框架、配置、数据库备份均带 `bak-cost-guard-20261009153553` 后缀。新写入结果不再依赖仅出站截断；服务商账单仍是费用验证依据。

```bash
python patches/astrbot/tool_context_limit.py <容器取回的原文件> <待部署文件>
python scripts/test_tool_context_limit.py <容器取回的原文件>
```

当前容器内备份为 `tool_loop_agent_runner.py.bak-context-history-20261009151638`（上一版仅压 `tool`）；原始框架版本在 `.bak-tool-context-20261009145919`。升级 AstrBot 后须重新取回线上文件、重新验证并应用补丁。上述两次部署改了请求内容，会使原缓存前缀重新建立；线上节省额必须看真实账单，不能只用 trace 的末次响应 token 推算完整工具循环费用。

**恢复办法** ✓：容器内 `*.bak-tsize-*` / `.bak-maxres-*` 覆盖回去 → `docker restart qqbot-astrbot` ✓
**注意** ✗：这是**框架补丁** ✓ 升级 AstrBot 会丢 ✓ → 升级后照上表重打 ✓（见 canon/_11 的"补丁易丢"那条 ✓）
