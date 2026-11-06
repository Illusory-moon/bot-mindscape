# 远程同步（可选）

> 只在「本机开管理台、服务器跑 bot」这种两处部署时用得上；单机跑不需要 ✓

在 `ui.sync` 里填好服务器信息后，本地管理台可拉取图库、记忆与日志。去重表（`seen.json`）也随 `pull` 拉下来（**只拉不推**，它由插件在服务器上维护，见 [docs/seen-table.md](docs/seen-table.md)）。记忆来自服务器配置的记忆文件路径；日志来自 `janitor.log` 同目录和重启命令对应容器，其他来源可用 `remote_logs` / `log_containers` 追加。快照保存在本机已忽略的 `data/observe/`，记忆和日志分开查看。图库也可以在命令行直接同步：

管理台配置页分为「规矩 / 记忆 / 认知 / 表达 / 运行」五个标签。表单修改自动保存到本机的
`config/managed.yaml` 与 `config/managed-wake.json`，点击「同步服务器」才上传。
首次打开会从服务器读取完整配置；远端在此期间有改动则拒绝覆盖。上传前两份远端文件分别备份，
也可在 `ui.sync.restart_command` 指定同步成功后的重启命令。
`remote_config` 与 `remote_wake` 可覆盖默认的 AstrBot 路径。图库同步仍使用下面的独立命令。

```bash
python scripts/mindscape_sync.py status  # 看两边差异
python scripts/mindscape_sync.py pull    # 服务器 -> 本地
python scripts/mindscape_sync.py push    # 本地 -> 服务器
```

**推送前会先拉取远端再合并**，所以不会覆盖远端自动采集的新素材；
删除默认只从索引移除，图片文件留底可恢复。
