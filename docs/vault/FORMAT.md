# Acost 加密账本格式 v1

2026-11-18。这份说明定义加密版账本在服务器上存什么、客户端怎么加密和解密。
Python 参考实现是 `acost_vault/crypto.py`，测试向量是同目录的 `vectors.json`（`make_vectors.py` 生成）。
以后 App（iPhone 用 CryptoKit）要用同一组向量对账：每一项都算出同样的字节，才算格式实现对了。

服务器只看得到乱码和几样元数据：账本编号、每条记录的编号和版本号、序号、写入时间、密文长度（已补齐）、幂等键的盲指纹。

## 1. 钥匙

| 名字 | 是什么 | 放在哪 |
| --- | --- | --- |
| master key（MK） | 32 字节随机数 | 用户的密码管理器（恢复码）；Muse 上 `~/.config/acost/master.key`，权限 600 |
| 数据钥匙（DEK） | 32 字节随机数，真正加密每条记录 | 服务器 `acost_vault.ledgers.wrapped_key`，用 MK 包起来 |
| 指纹钥匙 | 从 DEK 推出来，见第 3 节 | 不存，用到时现算 |
| 记账钥匙 `ql_…` | 只证明能读写这本账，解不了密 | Muse 安全卡片 |

MK 只包住 DEK。换恢复码时只要重新包一次 DEK，不用重新加密整本账。

## 2. 通用密文格式

所有密文（包好的 DEK、记录、快照）都是同一种字节排列：

```
偏移  长度  内容
0     1     格式版本，固定 0x01
1     4     key_version，无符号 32 位大端整数，从 1 开始
5     12    nonce，每次加密新取的随机数
17    n+16  AES-256-GCM 的密文加 16 字节标签
```

算法是 AES-256-GCM。AAD（附加认证数据）不存在密文里，由读的一方按下面的规则自己拼出来；拼出来的和写入时不一样，解密就失败。
这样服务器没法把一条记录挪到别的编号、别的版本或别的账本下面。

编号的文本形式：UUID 一律用小写带连字符的 36 字符形式（`6f1c2b8e-4a0d-4e5b-9c3a-1f2e3d4c5b6a`）。整数用十进制、不补零。AAD 用 UTF-8 编码。

| 用途 | 钥匙 | AAD |
| --- | --- | --- |
| 包 DEK | MK | `acost-wrap-v1\|<ledger_id>\|<key_version>` |
| 一条记录 | DEK | `acost-record-v1\|<ledger_id>\|<record_id>\|<version>\|<key_version>` |
| 快照 | DEK | `acost-snapshot-v1\|<ledger_id>\|<upto_seq>\|<key_version>` |

包 DEK 时明文就是 DEK 本身（32 字节，不补齐），所以包好的 DEK 一共 1+4+12+32+16 = 65 字节。

## 3. 幂等键的盲指纹

```
指纹钥匙 = HKDF-SHA256(IKM = DEK, salt = 32 个 0x00, info = "acost idem v1", L = 32)
idem_tag  = HMAC-SHA256(指纹钥匙, UTF-8(idempotency_key))      // 32 字节
```

服务器对 `(ledger_id, idem_tag)` 做唯一约束：同一个幂等键不能记两次，但服务器不知道键是什么。只有一笔账的第 1 版带 `idem_tag`。

## 4. 记录

### 明文和补齐

明文是一个 JSON 对象，序列化成「规范形式」：键按 Unicode 码位排序，没有空格（`,` 和 `:` 两边都不加），UTF-8，非 ASCII 字符不转义，不允许 NaN。
（和 Python `json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 一样。）

加密前补齐到 256 字节的整数倍（ISO/IEC 7816-4）：先接一个 `0x80`，再补 `0x00` 直到长度是 256 的倍数。至少补 1 个字节，所以 255 字节的明文会变成 256，256 字节的明文会变成 512。
解密后从尾部去掉 `0x00`，最后一个非零字节必须是 `0x80`，去掉它。JSON 里不会出现 `0x00`，所以不会弄错。

### 文档结构

```json
{"schema": 1, "kind": "entry", "id": "<record_id>", "version": 3, "data": { ... }}
```

`id` 和 `version` 必须和 AAD 里的一致，读的一方要核对。`kind` 有四种：

| kind | 一本账里有几条 | data |
| --- | --- | --- |
| `ledger` | 1 | `book`（账本名）、`currency`（固定 `CAD`）、`timezone`（`America/Toronto`）、`demo`（布尔）、`opening_balances_initialized`（布尔） |
| `account` | 每个账户一条 | `name`、`opening_balance_cents`、`sort_order` |
| `category` | 每个分类一条 | `name`、`color`（`#RRGGBB`）、`sort_order`、`deleted`（布尔；清理没用的分类时置 `true`） |
| `entry` | 每笔账一条 | 和旧账本 `qiao.entries` 一行的字段相同，见下 |

`entry` 的 data 字段：`id`（32 位小写十六进制，就是 record_id 去掉连字符）、`type`、`amount_cents`、`date`、`time`（可为 null）、`merchant`、`note`、`category`、`account`、`to_account`、`original_id`、`receipt_url`（加密版固定为 null）、`items`（可为 null）、`idempotency_key`、`request_hash`、`deleted_at`、`created_at`、`updated_at`、`version`、`created_by`、`updated_by`。
注意两个版本号不一样：文档的 `version` 是这条加密记录的第几版（从 1 开始，服务器核对不能跳号）；`data.version` 是这笔账在记账接口里的版本号（`expected_version` 比的就是它），从旧账本搬过来的账保留原来的值，所以可能比记录版本大。删除一笔账是追加一个新版本，`deleted_at` 填上删除时间（软删除，可以恢复）；没有真正抹掉记录的操作。

每次修改都追加一个新版本，旧版本留在服务器上，这就是修改历史。同一个 `id` 的版本号从 1 开始、每次加 1，不能跳号。

## 5. 同步规则

服务器给每条记录分配账本内的序号 `seq`（从 1 开始，连续）。账本的 `head` 是最大的序号。

- 取变化：给出「我已有到第 N 条」，服务器按序号返回之后的记录。客户端要核对：序号连续、没有跳号；每条记录的版本号正好是这个编号的上一版加 1。
- 写入：带上客户端看到的 `head`。服务器发现 `head` 变了就拒绝（409），客户端先取变化、按新账本重跑规则，再写。一次写入的几条记录（比如退款和被退的那笔）在一个事务里一起成功或一起失败。
- 防回滚：客户端记住见过的最大 `head`。服务器返回的 `head` 比它小，就报错，不继续用。
- 能防：改内容、伪造、对调、把旧版本冒充新版本、藏起中间的记录。防不了：服务器删数据或停服务（所以要定期导出加密备份）；一台全新的设备第一次同步时，服务器藏起最新的几条。

## 6. 快照

快照是某个序号为止整本账的当前状态，加密成一块，新设备先下快照再补后面的变化。明文：

```json
{"schema": 1, "kind": "snapshot", "upto_seq": 812, "records": [{"seq": 17, "doc": { ...记录文档... }}, ...]}
```

`records` 里每个编号只留最新的一版，按 `seq` 排序。读的一方照样核对第 5 节的规则：之后取到的变化必须从 `upto_seq + 1` 接上。

## 7. 恢复码

```
恢复码 = "ACOST1-" + 每 4 个字符一组、用 "-" 隔开的 Base32(MK ‖ 校验)
校验   = SHA-256("acost recovery v1" ‖ 0x00 ‖ MK) 的前 3 字节
```

Base32 用 RFC 4648 字母表（A–Z、2–7），35 字节正好 56 个字符，没有补位。读的时候不分大小写，忽略空格和 `-`，把 `0` 当 `O`、`1` 当 `I`。校验不对就拒绝，提示可能抄错了。

## 8. 以后换算法或换钥匙

- 格式版本字节变成 `0x02` 就是新格式；读的一方不认识就报错，不猜。
- 换 DEK：`key_version` 加 1，新记录用新 DEK；旧记录保留旧的 `key_version`，客户端需要同时拿到新旧两把 DEK（v1 只有一把，换 DEK 的工具以后再写）。
