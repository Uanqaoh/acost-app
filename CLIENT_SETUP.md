# Mac 展示与 Grok 连接


按 DISTRIBUTION.md 在本人 Supabase 项目完成部署、初始化和凭据签发后配置客户端。本文“共享”仅指同一人的 Mac 与 Grok 共用本人账本；朋友部署自己的项目。运行客户端不会创建或复制正式数据库。

## Mac 日常入口

需要 Python 3.10 或更新版本。双击 `打开Acost.command`：首次会询问共享服务 HTTPS 地址和 **Mac 专属 readonly 凭据**，验证后自动打开浏览器。以后双击同一文件即可查看。终端窗口保留时展示程序持续运行，按 Control-C 关闭。更换地址或凭据可双击 `配置共享账本.command`。

默认浏览器地址是 `http://127.0.0.1:8767`，只在这台 Mac 可用；本地程序通过 HTTPS 读取云端正式账本。默认端口已被占用时先关闭已运行的展示窗口，或由技术人员指定其他端口。首次运行可能需要在 Finder 允许打开本地脚本。

凭据优先存放 macOS 钥匙串，使用 Security.framework 写入，不经过命令参数。钥匙串不可用时退回仅当前用户可读的 `600` 配置文件，并在配置结果中明确提示。新安装的默认配置位置：

- macOS：`~/Library/Application Support/Acost/viewer.json`。
- Linux：`${XDG_CONFIG_HOME:-~/.config}/acost/viewer.json`。

如果同名配置已经在 `~/Library/Application Support/Qiao Ledger/` 或 `${XDG_CONFIG_HOME:-~/.config}/qiao-ledger/`，客户端继续使用那个文件，不会搬动或改写它。

不要发送或打包这个文件。浏览器不会收到共享凭据，也没有写入接口。只读代理校验精确 Host、同源来源和请求方法，阻止跨站访问及 DNS 重绑定；上游同时检查 readonly 角色。网络失败由页面保留上次成功数据并标记过期，不能作为空账本。页面可见时约 30 秒刷新一次，也可手动刷新。

## Grok 持久配置

将程序代码和 `AGENT_USAGE.md` 提供给新 Grok 账号；不用复制账本数据。管理员为每个账号发放可单独撤销的 `ai` 凭据。在交互终端执行：

```sh
python3 ledgerctl.py configure --url https://实际共享服务地址
python3 ledgerctl.py meta
```

配置命令的交互输入不会显示凭据。自动部署可用 `--token-stdin` 从私有输入提供一行凭据，避免写入命令历史。Grok 默认配置为用户配置目录下的 `grok.json`；`--config /私有路径/grok.json` 可显式选择位置，参数需放在子命令之前。

新增／退款若未提供 `--time`，时间保持未知；绝不默认填中午或当前时间。编辑省略时间不改原值；要明确清空时用 `--unknown-time`。新增和退款如遇 `possible_duplicate`，先按候选 ID 逐条执行 `get` 核对。确认同一交易就复用旧账单；确认是另一笔且用户已明确同意后，才用重复的 `--acknowledge-duplicate ID` 参数提交所有候选 ID。不能自行合并、跳过或删除记录；候选变化时重新核对。

临时覆盖可设置 `LEDGER_URL` 与 `LEDGER_TOKEN`，只使用该次环境中的凭据。缺少地址或凭据会报错；共享模式只允许 HTTPS，不自动跟随重定向，也不回退到 localhost。自建服务 URL 使用 HTTPS 根地址；Supabase 使用 `https://项目标识.supabase.co/functions/v1/ledger`。仅允许这个精确函数路径，不能附加 `/api`、尾斜线、账号、密码或查询参数。传输使用系统 TLS 证书验证，直接连接服务，不读取系统代理环境变量。

## 隔离本地预览

正式使用不需要这些选项。开发时共享服务必须绑定回环地址、使用独立演示目录与独立凭据，并使 `/api/meta` 返回 `demo: true` 和 `client.role: readonly`。在开发进程环境中设置临时 `LEDGER_URL=http://127.0.0.1:端口` 与 `LEDGER_TOKEN=隔离只读凭据`，再执行：

```sh
python3 viewer.py --allow-loopback --port 8767
```

这些环境变量只由显式 `--allow-loopback` 读取，正式 Mac 展示始终读取自己的配置文件。此模式拒绝 `localhost`、其他 IP、未标记演示的账本以及 AI/管理凭据。Grok CLI 的隔离测试可以设置 `LEDGER_MODE=local`，但仍需显式地址；不适用于正式 Grok 工作流。

## 连接验收

在隔离共享账本中完成：Mac 能读到账单；Mac 写请求返回拒绝；AI 的新增、按 ID 查询、分页和退款正常；未知时间仍为未记录；编辑带旧版本被服务拒绝；网络失败显示错误而非空账；同一幂等键重试不产生第二条；相似账单返回候选，未经明确确认不会写入。验证后再进行真实部署和迁移，不能因为旧记录曾为空而覆盖现有数据。


## 给朋友分发：各自配置自己的 Supabase

朋友获得相同程序代码和安装说明，在自己的 Supabase 项目初始化数据库、图片存储及接口，再为自己的 Grok 与 Mac 分别配置凭据。安装、初始化和备份步骤见 DISTRIBUTION.md。

不要给朋友用户自己的项目配置、令牌、数据库、小票或真实参考图片。安装包不得硬编码用户的云端地址。更换同一个人的 Grok 账号，才是在其本人项目内签发新客户端凭据。

此前的“Supabase 私人账本与邀请”操作段已移到 [停用草稿](SUPERSEDED_MULTIUSER_DRAFT.md)。`supabase_account.py` 暂不作为安装入口，所依赖的 RPC 未实现。
