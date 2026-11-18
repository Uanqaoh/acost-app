# Acost

用 Muse 记账：把小票发给自己的 Muse，它记账，并生成一个只读的看板。只记加元。

**怎么装：读 [FRIEND_SETUP.md](FRIEND_SETUP.md)。** 你需要 Hao 发的一个邀请码。

## 你的账是加密的

- 每一笔账都在你自己的 Muse 上用 AES-256-GCM 加密，再发到服务器。服务器（Hao 的 Supabase 项目）只存乱码，Hao 也看不到你的金额、商家、分类。
- 解密的钥匙（master key）只在你的 Muse 上；它的备份是你自己的恢复码，存在你的密码管理器里。丢了恢复码，又丢了 Muse，账就打不开了。
- 服务器能看到的只有：你有一本账、大概有几条记录、什么时候写的。
- 小票照片不保存。

加密格式和测试数据：[docs/vault/FORMAT.md](docs/vault/FORMAT.md)、[docs/vault/vectors.json](docs/vault/vectors.json)。

## 这里有什么

这是给 Muse 用的程序文件，每个文件的 SHA-256 写在 [MANIFEST.json](MANIFEST.json) 里，`tools/fetch.py` 下载时逐个核对。

- `ledgerctl.py`、`client_config.py`、`acost_vault/`：读写加密账本；记账规则也在这里运行。
- `tools/record.py` 等：记小票、转账、退款、固定扣费、预算、全年汇总、每日例行。
- `design/template.html`：看板页面。
- `QUICK_CARD.md`、`AGENT_USAGE.md`：给 Muse 的记账规则。

只需要 Python 3.10+；加密部分用 `cryptography`，安装脚本会装在 `~/acost-venv` 里。
