# 去重表（seen.json）口径统一

> 2026-10-12 修。涉及 `scripts/web_ui.py`、`scripts/import_stickers.py`、
> `scripts/mindscape_sync.py`、`plugins/mindscape_stickers.py`（→ 需重建 `dist`）。
> 自检规矩：**R69 / R70 / R71**；定点测试：`scripts/test_seen_table.py`。

## 一、症状（怎么发现的）

在主人的库上做图库体检时发现三条互相矛盾的现象：

1. 管理台「去重表」页**恒为空白**，但服务器上的 `seen.json` 有 69 条记录；
2. 「清理墓碑」按钮若被点到，会**把整张表写成 `[]`** —— 等于清空去重表，
   让 bot 把见过的图全部重判一遍（白烧识图 API）；
3. 图库里有 **6 组内容逐字节相同**的图，其中一组（`28e6b2a9aa.gif` 与
   `sparxie_28e6b2a9aa.gif`）**两条条目同时在库**、名字还不一样 ——
   正是 `mindscape_stickers` 注释里担心的「去重记录一丢，那张图重发就会以原名
   再入一份（造出重复）」。

## 二、根因：同一个「已处理」概念，三处口径不一致

| 位置 | 它以为的键 | 后果 |
|---|---|---|
| 插件 `mindscape_stickers._load_seen` | `分类:内容md5`，存成 `{"accepted":[…],"rejected":[…]}` | 正确（2026-09 升级后的口径） |
| 管理台 `web_ui.load_seen/seen_rows` | 「平表」+ **文件名前 10 位** | 遇到 dict 返回空表 ⇒ 页面空白；前缀匹配会把 `sparxie_<md5前10位>.gif` 误判成墓碑 |
| 导入 `import_stickers.py` | 只写索引，**完全不碰 seen** | bot 后来收到同一张图会认为「没见过」→ 重新判定 → 用自己的命名再存一份 ⇒ 重复条目 |

补一句设计事实（容易误会）：插件的**判活依据是内容 md5**，不是「索引里有没有」。
导入脚本会把文件重命名成 `<前缀><md5 前 10 位>.ext`，前缀一加，文件名前缀就不再是
md5 —— 所以管理台那套前缀匹配**必然**判错（插件注释里已经记过这条教训）。

另一条容易误会的事实：**墓碑会在插件下次启动时被自愈清掉**
（`_load_seen` 里 `seen_ok = {k for k in acc if k in live}` + 日志「清掉 N 条墓碑」）。
所以「那张图再发也收不进来」只成立到下一次重启为止；**长期真的拦着不收的是
`rejected`**（明确拒绝：超尺寸 / 判定不相关，重启也保留）。

## 三、改了什么

### `scripts/web_ui.py`（管理台）

- 新增 `load_seen_lists()`：两种格式都认（dict 现行格式 / 旧平表），旧接口
  `load_seen()` 保留为「两类键的并集」。
- 重写 `save_seen(accepted, rejected=None)`：**写回 dict 格式**（旧签名兼容），
  写前留 `.bak`、原子替换。旧版写平表会让插件按「legacy 迁移」把非索引项全部塞进
  `rejected`，语义被悄悄改变。
- 新增 `live_seen_keys()`：按插件同一口径算 `分类:内容md5`（逐文件读内容），
  按索引 mtime + 大小缓存；**读不出来返回 `None`**，调用方一律不标墓碑
  —— 宁可这次不标，也不能拿不可信的判活结果去删表。
- 重写 `seen_rows()`：三态标注「入库 / 墓碑 / 拒收」，墓碑排最前。
- 新增 `prune_seen_plan()`：清理方案只删 `accepted` 里对不上图库的，
  **`rejected` 原样保留**（旧版按「不在索引」判定，会把 `rejected` 一起删掉）；
  图库读不出来时不删。
- 两处接口与页面文案同步：`/api/seen/remove` 区分入库/拒收记录；
  页面提示改掉「墓碑会让那张图再发也收不进来」这句错话，并显示入库/拒收条数。

### `scripts/import_stickers.py`（导入）

- 新增 `_seen_path()` / `_register_seen()`，在 `_commit()` **索引锁内**、
  写完索引之后，把本次新增的图登记为 `accepted` 键 `分类:内容md5`。
  登记失败只告警（索引已经写入，不退化成事故）。

### `plugins/mindscape_stickers.py`（插件，需重建 dist）

- `_load_seen()` 的去重集合由 `seen_ok | seen_no` 改为 **`seen_ok | seen_no | live`**：
  「图库里现有的」也算处理过。去重键带分类，所以**跨分类**收同一张图不受影响
  （那是设计允许的：同一张图可以同时属于 sparxie 与 shuimeng）。
  这样导入脚本 / 手工放进图库的图不会再被当成「没见过」而重复入库。

### `scripts/mindscape_sync.py`（同步）

- `pull()` 顺带拉一份 `seen.json`（默认 `<remote_dir>/seen.json`，可用
  `ui.sync.remote_seen` 覆盖），解析不过就跳过、不动本机那份。
  不拉的话，管理台的去重表页在本地**永远是空的**。
- **只拉不推**：`seen.json` 由插件在服务器上维护，本机的移除/清理只作用于这份快照，
  下一次 pull 会被服务器版本覆盖。双向同步（含 `.removed` 副作用文件、与在线采集
  的合并语义）留作后续设计，不在本次改动里。

## 四、怎么验证的

```
python scripts/test_seen_table.py     # 7 项：格式兼容 / 内容 md5 判活 / 三态 / 写回格式 / 清理方案
python scripts/run_selfcheck.py       # 187 项通过、0 失败（含新增 R69/R70/R71 与产物新鲜度）
python scripts/build_plugin.py --market   # 改了 plugins/ 必须按市场口径重建 dist
```

`test_seen_table.py` 里那两条是这次的核心回归守卫：

- `test_live_keys_use_content_md5`：断言 `live` 里**不得**出现
  `sparxie:sparxie_xxxxxxxx`（前缀匹配的典型错值）；
- `test_prune_only_drops_tombstones` / `test_prune_refuses_when_library_unreadable`：
  清理只删真墓碑、图库读不出来时不删。

## 五、部署注意

1. 插件改动**必须**重新 `build_plugin.py --market` 并更新 `dist/mindscape/main.py`，
   否则自检的「产物新鲜度」会红（`--market` 会排除 `mindscape_janitor`，
   它是本地部署脚本、不进市场版产物）。
2. 管理台与导入脚本是纯脚本改动，无需重新部署；
   `seen.json` 的新格式**不需要**手工迁移（管理台与插件都兼容旧平表）。
3. 想让「图库现有的也算处理过」生效，需要**重启 bot**（`_load_seen` 在启动时执行）。
