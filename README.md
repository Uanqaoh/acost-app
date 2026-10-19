# Acost

把小票交给 Grok / Agent 识别和记账，在 Mac 直接打开只读页面查看。同一个人的所有客户端连接其本人唯一的 Supabase 账本。朋友收到同一程序包，部署并配置自己的 Supabase，数据、凭据和费用各自独立。

## 开始使用

- 新部署、向朋友分发：先读 [DISTRIBUTION.md](DISTRIBUTION.md)。
- Mac 与 Grok 连接：[CLIENT_SETUP.md](CLIENT_SETUP.md)。
- Agent 记账规则：[AGENT_USAGE.md](AGENT_USAGE.md)。
- 接口与权限：[API.md](API.md)。
- Supabase 源码：[supabase/README.md](supabase/README.md)。
- iCost 历史资料准备与迁移边界：[ICOST_IMPORT.md](ICOST_IMPORT.md)。

日常查看双击 `打开Acost.command`；首次需要配置自己的 HTTPS 地址和 readonly 凭据。配置存放在个人目录，不在程序包内。`打开演示.command` 使用虚构数据在本机预览，不连接正式账本。

Python 3.10+。Mac 展示、CLI 和备份工具只用 Python 标准库；本机演示依赖 `requirements.txt` 中固定版本的 FastAPI/Uvicorn。云端运行 Supabase Postgres 和 Edge Function，不需要租用额外 Python 服务器。

本版代码已完成本地 Postgres、接口、客户端及备份恢复验证。每位使用者都需按安装说明部署并管理自己的 Supabase 项目；程序包不含任何人的项目配置或凭据。iCost 历史导入器尚未实现，资料准备与迁移边界见 [ICOST_IMPORT.md](ICOST_IMPORT.md)。

## 当前功能

- 月份、关键词、账户、分类与类型查询；账单详情与私密小票。
- 支出、收入、转账、部分／全额退款、版本校验修改、软删除与恢复。
- 整数分计算；退款冲减支出、不计收入，转账不计收支。
- 每设备可撤销凭据；Mac 只读，AI 可记账，管理员负责凭据与备份。
- 带校验的数据库及小票备份；只向空账本恢复；旧 SQLite 转换工具。

第一版只支持 CAD。预算、报销、真实资产期初余额尚未配置时明确显示未设置，不显示编造结果。程序不自带 OCR 或大模型，识别由用户选择的 Agent 完成；没有 iCost 自动同步。

## 数据保管

`cloud_admin.py backup` 生成完整私有备份，`verify-backup` 检查本地文件，`restore` 只恢复空目标。`verify-remote` 可在恢复响应中断后重新核对目标，避免重复迁移。备份不含客户端凭据，新项目需重新签发。

Supabase Free 项目在低活动期可能暂停；平台数据库备份也不能代替小票文件备份。请将完整备份保存在项目之外，并实际验证恢复。[免费项目暂停说明](https://supabase.com/docs/guides/platform/free-project-pausing)、[备份说明](https://supabase.com/docs/guides/platform/backups)。

`server.py`、`start.sh`、`GROK_DEPLOY.md` 是旧本机原型；`supabase_account.py` 是停用的中央账户实验。它们不属于本版云端安装流程，不应公开运行旧服务，也不应让朋友加入你的项目。
