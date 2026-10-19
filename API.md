# 个人账本 API

根地址：`https://YOUR_PROJECT_REF.supabase.co/functions/v1/ledger`。每人部署自己的项目；所有业务请求携带 `Authorization: Bearer <本人客户端凭据>`，写入使用 JSON。普通客户端不持有 Supabase service_role 或项目后台密钥。

服务按 readonly、ai、admin 检查权限；除健康检查外必须认证。Mac 只读凭据不可写入；ai 不可管理凭据、导出全部审计或恢复备份。接口不接受浏览器跨站调用；浏览器通过 Mac 的本地只读代理访问。

| 方法 | 路径 | 用途与权限 |
| --- | --- | --- |
| GET | `/api/health` | 服务存活，不返回账单；不能证明凭据有效 |
| GET | `/api/meta` | 账本、账户余额、分类、client.role、revision |
| GET | `/api/entries` | month/q/category/account/type 筛选；limit 默认 100，上限 500；offset 分页 |
| GET | `/api/entries/{id}` | 按 ID 取原单；include_deleted=true 可取软删除记录 |
| GET | `/api/summary` | month 月度汇总；退款冲减支出 |
| POST | `/api/entries` | 新建，ai/admin，必须稳定 idempotency_key |
| POST | `/api/entries/{id}/refunds` | 关联退款，ai/admin，必须稳定 idempotency_key |
| PATCH | `/api/entries/{id}` | 编辑，ai/admin，必须 expected_version |
| DELETE | `/api/entries/{id}` | 软删除，ai/admin，必须 expected_version |
| POST | `/api/entries/{id}/restore` | 恢复，ai/admin，必须 expected_version |
| POST | `/api/receipts` | 上传图片，ai/admin；返回相对 receipt_url |
| GET | `/api/receipts/{filename}` | 私密图片，需有效凭据 |
| GET / POST | `/api/credentials` | 列出／签发设备凭据，admin |
| DELETE | `/api/credentials/{id}` | 撤销，admin；不能撤销最后一个管理员 |
| GET | `/api/audit` | 分页变更记录，admin |
| GET | `/api/export` | 一致性 JSON 快照，admin，不含凭据 |
| POST | `/api/import` | 快照恢复至空库，admin；由 cloud_admin.py 调用 |
| POST | `/api/receipts/import` | 备份小票暂存，admin；原名与哈希校验，不覆盖不同内容 |

`/api/backup` 提示使用 `cloud_admin.py backup`，完整 ZIP 在管理员客户端组装，包括 JSON 与所有小票。仅下载 `/api/export` 不构成完整备份。

## 账单格式

金额为 CAD 正整数分，26.07 为 2607。日期 `YYYY-MM-DD`，时间为 `HH:MM` 或 `null`（未知）；账户、分类使用显示名称。新增示例（虚构，时间未知）：

```json
{"type":"expense","amount_cents":2607,"date":"2026-10-15","time":null,"merchant":"示例商家","category":"餐饮-Grocery","account":"Spending","idempotency_key":"receipt-unique-id"}
```

新增或退款省略 `time` 时保存为 `null`；编辑省略 `time` 表示不改时间，显式传 `"time": null` 表示清空。退款调用原单路径，传入金额、实际到账日期与稳定幂等键。退款继承原单的账户和分类，累计不可超退，不得通过新增收入替代退款。

新增和退款在疑似重复时返回 HTTP 409、`possible_duplicate` 及 `candidates`。匹配条件为相同类型、日期、金额和账户；转账还需相同转入账户，退款还需相同 `original_id`。服务不合并或跳过候选。客户端必须让操作者逐条核对；确认同笔时复用原记录。确认是另一笔时，须取得用户明确确认，再用 CLI 对返回的每个候选重复指定 `--acknowledge-duplicate ID` 后重试；候选清单变更会再次返回 409。

成功返回 `{"entry": {...}}`，包含真实 ID、version、refunded_cents 等字段。编辑、软删除与恢复先 GET，再传当前 expected_version；冲突为 409，缺版本为 428。不要读取新版本后盲目覆盖。退款也更新原单版本。

图片上传为 `{"filename":"receipt.png","data_base64":"..."}`。PNG/JPEG/WebP，最多 8 MiB；由服务器产生不可覆盖的随机文件名。图片上传不会识别文字。

列表返回 entries、total、limit、offset、next_offset、revision。汇总和元信息也返回 revision，组合多个响应前应核对同一版本。错误返回 `{"error":"code","message":"说明"}`；失败不能解释为空账或保存成功。
