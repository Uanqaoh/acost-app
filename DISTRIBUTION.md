# 通用程序分发

本程序按“每人自己的 Supabase”设计：你和朋友分别拥有、配置和管理各自的 Supabase 项目。朋友收到的是相同的通用程序，不会获得你的数据库、云端地址、密钥、账单或小票。没有中央服务、成员邀请或共用租户。

## Linux 安装

1. 将 `acost-linux.tar.gz` 解压到持久工作目录。
2. 在解压目录运行 `./install-linux.sh`。它只在程序目录创建 `.venv` 并安装 `requirements.txt` 中的 Python 依赖；不会设置云端项目、创建数据库、运行迁移或启动服务。
3. `cloud_admin.py` 与 `ledgerctl.py`、`client_config.py` 都只使用 Python 标准库。安装的 FastAPI/Uvicorn 依赖仅用于本机隔离演示与开发 API。
4. 阅读 `CLIENT_SETUP.md` 和 `AGENT_USAGE.md`，按下面“自己的 Supabase 项目”步骤部署并验收后端，再连接客户端。

## 自己的 Supabase 项目

以下部署步骤需要 Node.js 与 npm，日常客户端需要 Python 3.10+。命令在程序目录运行。在 Supabase 控制台创建一个全新的个人项目；取得自己的 project ref 和 HTTPS 项目 URL。安装包附带固定版本 Supabase CLI 2.121.0 的 npm 锁文件；使用包内本地 CLI，不需要全局安装：

```sh
cd tools
npm ci
cd ..
./tools/node_modules/.bin/supabase login
./tools/node_modules/.bin/supabase link --project-ref YOUR_PROJECT_REF
./tools/node_modules/.bin/supabase db push
./tools/node_modules/.bin/supabase functions deploy ledger --no-verify-jwt
```

不要使用朋友或其他人的项目。`supabase link` 的数据库密码提示只在本机交互输入。

`--no-verify-jwt` 是此函数使用账本自身的客户端凭据校验；不可因此省略函数内部鉴权。迁移会创建私有 `qiao-receipts` bucket。审阅迁移和函数后，只在自己的项目部署。

为本人项目生成 admin 配置和只含该凭据哈希的初始化 SQL。路径须是仅本人可访问的私有目录；bootstrap 会拒绝覆盖已有输出。默认 admin 配置位置由 `cloud_admin.py` 选择，也可用全局 `--config` 明确指定：

```sh
umask 077
mkdir -p "$HOME/.config/acost"
python3 cloud_admin.py --config "$HOME/.config/acost/admin.json" bootstrap \
  --url https://YOUR_PROJECT_REF.supabase.co/functions/v1/ledger \
  --sql-output "$HOME/.config/acost/bootstrap.sql"
```

在自己项目的 Supabase SQL Editor 中打开并执行生成的 `bootstrap.sql`，然后删除这份临时 SQL 文件。它含管理员凭据哈希；相应明文 token 仅保存在本机 `admin.json`。验证 admin 连接：

```sh
python3 cloud_admin.py --config "$HOME/.config/acost/admin.json" clients
```

每台 Mac 创建专属只读凭据，每个 Grok 账号创建独立 `ai` 凭据。输出配置文件是秘密，只发给对应使用者，不放进程序压缩包：

```sh
python3 cloud_admin.py --config "$HOME/.config/acost/admin.json" issue-client \
  --role readonly --name "my-mac" --output "$HOME/.config/acost/mac.json"
python3 cloud_admin.py --config "$HOME/.config/acost/admin.json" issue-client \
  --role ai --name "my-grok" --output "$HOME/.config/acost/grok.json"
```

Mac 双击 `打开Acost.command` 首次会交互配置 HTTPS endpoint 与只读 token；已有配置可用 `python3 viewer.py --config 私有目录/mac.json` 启动。Grok 环境按 `CLIENT_SETUP.md` 使用 `ledgerctl.py configure` 输入自己的 endpoint 和 ai token。若上述数据库迁移、初始化 SQL、函数部署任一尚未由 Supabase 确认成功，账本还不可用；不要把客户端配置完成当成部署完成。已经放在 `~/.config/qiao-ledger/` 的配置会继续被读取，不要搬动现有的 `grok.json`。

## macOS 查看入口

解压 `acost-macos.zip`，双击 `打开演示.command` 会在包内创建 `.venv`（首次运行安装 requirements），启动虚构数据的隔离预览，关闭终端窗口或按 Control-C 停止。它不会连接正式账本。查看自己的线上账本时，先按上面签发的 readonly 凭据配置，双击 `打开Acost.command`，或从终端运行 `python3 viewer.py --config 私有目录/mac.json`；该只读界面会通过 HTTPS 读取本人服务。不要把 admin 或 ai 凭据配置给展示程序。

## 备份、校验与恢复

备份需要本人 admin 配置。包包括一致性账本快照、审计事件、所有小票文件和逐文件 SHA-256 清单；不含客户端凭据。ZIP 本身不加密，按私密数据保管。输出目标必须尚不存在：

```sh
python3 cloud_admin.py --config "$HOME/.config/acost/admin.json" backup \
  --output "$HOME/private-backups/acost-2026-10-15.zip"
python3 cloud_admin.py verify-backup "$HOME/private-backups/acost-2026-10-15.zip"
```

`verify-backup` 在本机检查 ZIP 结构、哈希、小票内容和财务关系，不连接云端。恢复前，在目标个人 Supabase 部署迁移和函数、执行其 bootstrap SQL，并用新项目的 admin 配置确认目标账本为空。恢复只支持空账本，不能覆盖现有账本。详细频率、加密副本和季度隔离恢复演练建议见 [BACKUP_PLAN.md](BACKUP_PLAN.md)。

目标空账本验证通过后执行：

```sh
python3 cloud_admin.py --config "$HOME/.config/acost-target/admin.json" restore \
  "$HOME/private-backups/acost-2026-10-15.zip"
```

恢复仅允许空账本；完成后会比较账单、余额、小票哈希并提示重新签发客户端凭据。备份不含任何旧凭据。保留备份直到恢复结果核对完成。Supabase 平台数据库备份不包含 Storage 对象字节；本程序包会另行下载小票原件并校验哈希，因此两种备份用途不同。

`from-sqlite` 仅适用于旧版 qiao-ledger SQLite 数据库及其 receipts 目录，不适用于 iCost 文件或 iCost `.backup`。iCost 导入格式与步骤见 [ICOST_IMPORT.md](ICOST_IMPORT.md)。

从旧 qiao-ledger SQLite 原型迁移时，先用只读快照工具生成同一种校验备份，再按上述空目标恢复。需同时提供旧数据库与 receipts 目录：

```sh
python3 cloud_admin.py from-sqlite --database /private/path/ledger.sqlite3 \
  --receipts /private/path/receipts \
  --output "$HOME/private-backups/acost-migration.zip"
python3 cloud_admin.py verify-backup "$HOME/private-backups/acost-migration.zip"
```

仅迁移真实、非演示数据。保留旧数据库原件；迁移包包含账单和图片，应限制访问。

## 打包维护

`tools/build_release.py --list` 显示当前发布白名单。正式生成归档时传入独立输出目录；脚本仅从显式文件列表和 `supabase/functions/` 读取。密钥、用户配置、SQLite 数据、依赖目录、临时文件、真实图片、旧中央账户实验和停用草稿均不会进入归档。

若恢复请求超时，不要覆盖或重复导入；先用 `cloud_admin.py --config 目标管理配置 verify-remote 备份.zip` 核对。此工具会比较账单字段、幂等信息、退款、余额、小票与原始审计历史。
