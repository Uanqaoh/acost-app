# 每人独立部署的 Supabase 后端

一个项目对应一个人的账本。程序可以分享，项目、凭据和数据不共享；没有中央用户注册、邮件邀请或租户系统。

- `migrations/20261015050412_finance.sql`：账本表、事务接口、权限和私有小票 bucket。
- `migrations/20261015050342_tenancy.sql`：旧空占位，不提供租户功能。
- `migrations/20261017033549_restrict_auto_rls_execute.sql`：只撤销平台自动 RLS 事件触发函数的客户端 `EXECUTE` 权限，不删除或改动该触发器。
- `functions/ledger/index.ts`、`handler.mjs`：账本 HTTPS 接口与图片校验。
- `tests/local_auth_stub.sql`：仅为本机 PostgreSQL 模拟平台角色，**绝不能在云端执行**。

业务表在非公开 qiao schema，开启 RLS 且不给 anon/authenticated 访问权，也没有为客户端创建 RLS 策略；这是默认拒绝的设计。`public.qiao_dispatch` 仅允许服务角色调用，并使用 SECURITY INVOKER。Edge 将独立账本 token 转为 SHA-256 交给数据库核对，按 readonly/ai/admin 执行权限限制。项目后台密钥只由 Edge 环境持有。

每个人的 Supabase 项目分别记录和管理自己的 CLI 迁移历史。首次部署过的项目不要盲目重跑初始迁移、清空迁移历史或手动重复执行旧 SQL；先检查 `supabase migration list` 与该项目当前结构，再按该项目的部署状态推进新迁移。全新项目按 [DISTRIBUTION.md](../DISTRIBUTION.md) 部署。

因此此函数 `verify_jwt=false`：它使用自身的设备凭据认证，不能删掉内部鉴权。公开健康检查只表示函数存活；应调用 meta 验证实际账本连接。

安装、初始化管理员、凭据签发及备份恢复见 [DISTRIBUTION.md](../DISTRIBUTION.md)。旧 `supabase_account.py` 和 `qiao_account` 方案不使用。
