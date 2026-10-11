# 2026-10-12 · 补档：两处「只活在容器里」的框架补丁（cron / persona）

**背景**：2026-10-12 做框架级审计时发现，容器里跑着两处**仓库没有任何记录**的框架改动。
它们只存在于容器可写层（**删容器重建就丢**），而且都影响实际行为：

| 文件（容器内 `<site-packages>/astrbot/...`） | 规模 | 影响的行为 |
|---|---|---|
| `core/cron/manager.py` | 517 行 → **588 行**（9 站） | 定时任务的节奏、**cron 轮有没有上下文** |
| `core/persona_mgr.py` | 433 行 → **455 行**（3 站） | **哪个 QQ 号用哪套人格** |

**本目录不提交框架文件**（那是 AstrBot 的代码）。只留「底本 md5 + 站点表」，
用同目录那套 `wake_stage_apply.py` 的机制就能**逐字节重建**线上那份 ——
与唤醒补丁完全同一套做法 ✓。

---

## 一、`core/cron/manager.py` 改了四处

1. **按任务浮动（jitter）**：`payload.jitter`（秒）> 0 → 给这个定时任务加随机延迟。
   原话：「定时任务不再卡在整点。做成**按任务**开关：提醒类任务要准时，冒泡类任务要飘。」
2. **冒泡闸门 `_gate_pass(job)`**：`payload.gate = {"daily_count": N, "window": [起, 止]}`（分钟，0~1440）
   - 把窗口**均分成 N 段**，每段随机取一分钟当"开口时刻"（时间随机、不整点、段间天然拉开）
   - 随机种子 = `crc32(job_id + 日期)` ⇒ **重启不会重排**，同一天的时间是稳定的
   - `_run_job` 里：**闸门没过就直接 `return`** —— 不跑，**也不写 DB**
3. **cron 轮的 `self_id` 修正**：框架默认把 `CronMessageEvent` 的 `self_id` 设成 `"astrbot"`，
   而插件（记忆等）是靠 `self_id` 找 bot 配置的 ⇒ **整条注入链全是空**。
   现由 `payload.self_id` 指定本轮账号，并补 `cron_event.message_obj.group_id`。
4. **cron 轮补 `OnLLMRequestEvent` 派发**：cron 轮原本绕过了 pipeline 的钩子派发点，
   于是**记忆 / 风格层 / 群缓冲全都不注入**。现在补 `call_event_hook(cron_event, EventType.OnLLMRequestEvent, req)`，
   让 cron 轮和普通轮走同一套注入（各自按需在插件侧决定要不要跳过）。

> **3 与 4 是修 bug**（cron 轮本来是个"没人喂上下文"的残废轮）；
> **1 与 2 是功能**（让定时冒泡不像闹钟）。任何一条丢掉，行为都会明显变化。

## 二、`core/persona_mgr.py` 改了一处

`shuimeng patch`：**火花账号（`platform_id = sparxie`）用 `sparxie` 人格**。
- 模块级：`SPARXIE_PERSONA_ID = 'sparxie'`、`SPARXIE_PLATFORM_IDS = ('sparxie',)`
- `get_persona_v3`：umo 以 `sparxie:` 开头 → 返回 sparxie 人格
- `resolve_selected_persona`：同样判断，且**优先级最高**（放在 `force_applied_persona_id` 之后、
  其它分支之前）

> 丢了它，两个号会退回同一个默认人格 —— `cmd_data` 里的 `default_personality: "shuimeng"`
> **只够管一个号**。

---

## 三、重打与核验（容器被重建 / 框架升级之后）

```bash
CT=qqbot-astrbot
SP=/opt/astrbot/.venv/lib/python3.13/site-packages/astrbot

# 0) 底本还在不在（容器里的 .bak；升级后可能没了 —— 那就从对应版本的 AstrBot 包取）
docker exec $CT ls -1 $SP/core/cron/manager.py.bak-mindscape-jitter-* \
                      $SP/core/persona_mgr.py.bak-sparxie

# 1) 取底本与线上（⚠️ 必须 docker cp；sftp 拿到的是宿主机那份，不是同一个文件）
docker cp $CT:$SP/core/cron/manager.py.bak-mindscape-jitter-20260920210707 /tmp/base_manager.py
docker cp $CT:$SP/core/cron/manager.py                                  /tmp/live_manager.py
docker cp $CT:$SP/core/persona_mgr.py.bak-sparxie                       /tmp/base_persona.py
docker cp $CT:$SP/core/persona_mgr.py                                   /tmp/live_persona.py

# 2) 核验（应打印 [一致]）—— 底本 md5 必须与站点表记录相符，否则站点表已过期
python scripts/wake_stage_apply.py check /tmp/base_manager.py patches/astrbot/cron_sites.json /tmp/live_manager.py
python scripts/wake_stage_apply.py check /tmp/base_persona.py patches/astrbot/persona_sites.json /tmp/live_persona.py

# 3) 重建 + 送回 + 编译检查 + 重启
python scripts/wake_stage_apply.py build /tmp/base_manager.py patches/astrbot/cron_sites.json /tmp/patched_manager.py
docker exec $CT cp -a $SP/core/cron/manager.py $SP/core/cron/manager.py.bak-<用途>-$(date +%Y%m%d%H%M%S)
docker cp /tmp/patched_manager.py $CT:$SP/core/cron/manager.py
docker exec $CT /opt/astrbot/.venv/bin/python -m py_compile $SP/core/cron/manager.py
docker restart qqbot-astrbot
```

**站点表记录的指纹**（核验时对得上才算没漂）：

| 站点表 | base_md5 | live_md5 | 行数 | 站数 |
|---|---|---|---|---|
| `cron_sites.json` | `fbadffe0…` | `75ca9483…` | 517 → 588 | 9（8 insert + 1 replace）|
| `persona_sites.json` | `fb4e49bc…` | `3ad675fe…` | 433 → 455 | 3（全 insert）|

> ⚠️ **框架升级会换掉底本** ⇒ 站点表的行号会失效。那时按新版底本 `regen` 重新生成，
> 或照 `check` 打出来的差异手工合并；核验永远用上面那条 `check` 命令 ✓。
> 自检 **R73** 守着这两张表的自洽性（`live_lines == base_lines − 被替换行 + 新增行`），
> 不依赖框架文件也能发现表格被改坏。

---

## 四、为什么值得补这个档

这三样东西（cron 闸门与节奏、cron 轮的上下文修复、火花号人格路由）此前**只活在容器里**：
换镜像、删容器、迁移到别的机器 —— 全部消失，而且**没有任何地方记录它们存在过**。
本目录现在用「站点表 + 核验命令」把它们变成了**可重建、可验证**的东西 ✓。
