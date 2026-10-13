# 部署侧私有记录（`.private/`）—— 公开仓只留约定，内容在私有仓

> 2026-10-13 立。**为什么有这一层**：本目录下有一部分东西是
> **「线上容器里逐字节是什么」的记录**（站点表 / 补档文档），它们
> ① **必须留档** —— 删了容器就再也重建不出来；
> ② 又**天然带私域标识** —— 本仓库是面向市场的通用插件，按项目铁律
>    **连私域名字都不许出现** ✗。
> 两条都得满足 ⇒ 内容搬进**私有仓**，公开仓只留**这份约定** ✓

## 一、哪些东西属于「私有」

| 名字 | 是什么 |
|---|---|
| `cron_sites.json` | `core/cron/manager.py` 补丁的**站点表**（底本 → 线上，按行号拼接逐字节重建） |
| `persona_sites.json` | `core/persona_mgr.py` 补丁的站点表 |
| `waking_sites.json` | `waking_check/stage.py` 补丁的站点表（唤醒补丁） |
| `2026-10-12-cron-persona-patches.md` | 上面几份的**补档文档**（改了什么 / 为什么 / 怎么重建与核验） |

判据很简单：**「表里每个字符串都要原样塞回线上代码」的文件 = 私有** ✗。
它们是**部署记录**，不是插件功能 —— 本仓库的插件不读它们也能跑 ✓。

## 二、放在哪儿、怎么放

```
<本仓>/patches/astrbot/.private/          ← 已 gitignore，内容来自私有仓
├── cron_sites.json
├── persona_sites.json
├── waking_sites.json
└── 2026-10-12-cron-persona-patches.md
```

私有仓为 `<owner>/bot-mindscape-private`（示例路径，实际见项目内部台账），
把它的 `patches/astrbot/` 内容整体放到上面的 `.private/` 里即可 ✓

## 三、自检的两条口径（**别搞混**）

| 口径 | 怎么跑 | 私有内容缺失时 |
|---|---|---|
| **公开**（默认） | `python scripts/run_selfcheck.py` | 打印「跳过（私有站点表不在，公开口径）」✓ **不并进全绿** |
| **私有**（本机 / 部署机） | `MINDSCAPE_PRIVATE=1 python scripts/run_selfcheck.py` | **直接红** ✗ |

> 「跳过」与「通过」是**两种不同的结论** —— 这一条是踩过坑才立的：
> 从前扫描器读不到输入就**静默算通过**，结果红灯不亮、假绿上线 ✗
> （见项目内部台账「只打印不分支的校验等于没有校验」）。
> ⇒ 私有内容**在的时候必须真验**，不在的时候**必须显式说不在** ✓

## 四、核验与重建

工具在公开仓的 `scripts/` 里（它们只吃命令行参数，不关心表放在哪）：

```bash
python scripts/wake_stage_apply.py check /tmp/base_persona.py patches/astrbot/.private/persona_sites.json /tmp/live_persona.py
python scripts/wake_stage_apply.py build /tmp/base_persona.py patches/astrbot/.private/persona_sites.json /tmp/patched_persona.py
```

- 完整步骤（`docker cp` 取底本 → 重建 → 编译检查 → 重启）见
  `.private/2026-10-12-cron-persona-patches.md` ✓
- ⚠️ 站点表是**逐字节记录**：要改就与线上那份**同时改**，改完立刻 `check` 验一次 ✓
- ⚠️ 取线上文件**一律 `docker cp`** —— `sftp` / 宿主机路径拿到的是**另一份文件** ✗
