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

**为什么动阈值** ✓：原来的 2.75 万 token 阈值对我们等于没开 ✗（我们的结果才 3~5k token ✓）
降到 3,000 之后 ✓：**大结果自动落盘成文件** ✓ 上下文里只留 ~800 token 的预览 ✓
→ 她**要看全文可以用读取工具** ✓ → **不会答错** ✓ 只在她真需要时才多一次工具调用 ✓

**恢复办法** ✓：容器内 `*.bak-tsize-*` / `.bak-maxres-*` 覆盖回去 → `docker restart qqbot-astrbot` ✓
**注意** ✗：这是**框架补丁** ✓ 升级 AstrBot 会丢 ✓ → 升级后照上表重打 ✓（见 canon/_11 的"补丁易丢"那条 ✓）
