# 个人账本备份方案

本文件是建议方案，不代表已经设置或运行自动备份。备份凭据只保存在账本所有者的 Mac；管理员配置绝不交给 Grok。当前 `cloud_admin.py backup` 由 Mac 上的管理员手动运行，且需要 Mac 开机、联网和 Supabase 可用；Mac 关机时不会生成备份。

## 备份包含什么

程序生成的 ZIP 包含一致性账本快照、审计事件、账单引用的小票文件以及逐文件 SHA-256 清单，并检查文件清单和财务关系。包不含客户端凭据。ZIP 自身不加密；不要通过聊天、邮件或未加密文件共享发送。Supabase 平台数据库备份仅覆盖数据库，不含 Storage API 保存的小票字节，因此不能替代本程序的完整包。Supabase 建议 Free 项目定期导出数据并保存异地副本；Free 项目连续一周不活跃后可能暂停。参见 [Supabase 数据库备份说明](https://supabase.com/docs/guides/platform/backups) 和 [Supabase 定价与 Free 项目说明](https://supabase.com/pricing)。

## 建议频率与保留

数据较少时，每周生成一次完整备份；新增账单频繁或希望减少最多一周的数据损失时，每日生成一次。建议保留最近 30 个每日包、12 个每周包和 12 个每月包，并至少保留一份最近验证通过的包。保留期是操作建议，不是当前已配置的自动清理规则。每次清理前确认更近期的备份和独立副本均已校验通过。

## Mac 上生成并验证

先选择一个仅本人可访问的目录。建议本机受 FileVault 保护的目录保留一份，另在独立的加密外接 APFS 卷保存第二份；如果加密卷未连接，第二份未完成，不能将该次运行记作完整备份。此流程不会自动上传备份到 Supabase 或其他云端。

在程序目录运行以下命令。`cloud_admin.py backup` 要求输出文件尚不存在，因此日期文件名用于避免覆盖；如果同一天重复运行，请选新的序号文件名。

```sh
umask 077
BACKUP_DIR="$HOME/Library/Application Support/Acost/backups"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d)"
python3 cloud_admin.py backup --output "$BACKUP_DIR/acost-$STAMP.zip"
python3 cloud_admin.py verify-backup "$BACKUP_DIR/acost-$STAMP.zip"
```

检查两条命令都以成功状态结束，并保存 `verify-backup` 返回的账单、小票和 SHA-256 校验结果。然后把 ZIP 复制到已解锁的独立加密卷；在副本上再运行一次 `verify-backup`。只有两份校验都通过才算完成。若下载或校验失败，保留错误信息、检查网络和磁盘空间，并在下一次成功前保留上一个通过验证的备份。

该命令使用所有者本机的默认管理员配置；如配置放在自定义私有位置，可在 `backup` 前添加 `--config 私有配置路径`。不要将管理员配置复制到外接备份卷或客户端包。

## 自动运行的边界

当前没有创建 LaunchAgent、日历任务、提醒或自动清理任务。若之后决定设置每日或每周任务，应先确认 Mac 在计划时间醒着并能联网；任务必须将命令退出状态、校验结果和失败原因写入可查看日志，并在失败时明确提醒所有者。Mac 关机时任务不会运行；开机后是否补跑取决于届时配置的调度器，不能假设已经补齐。

## 季度恢复演练

每季度选择一份已通过本地校验的包，在单独的隔离 Supabase 项目中完成演练：部署匹配版本的迁移和 Edge Function，生成该测试项目自己的 bootstrap/admin 配置，确认目标账本为空，再运行 `cloud_admin.py restore`。随后用 `verify-remote` 比较恢复后的账单、审计、小票和校验值，并验证小票可读取。不要在正式账本上演练；恢复只允许导入空账本，不会覆盖已有数据。测试项目含有完整账本副本，须限制访问；演练完成且确定无保留需要后，才按项目管理流程清理测试项目和临时管理配置，并记录日期、备份文件校验结果、恢复结果及任何差异。

恢复命令及项目初始化步骤见 [DISTRIBUTION.md](DISTRIBUTION.md)。恢复需要管理员配置；Grok 的 `ai` 凭据无权执行完整备份或恢复。
