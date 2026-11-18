#!/usr/bin/env python3
"""Standard-library CLI for Acost's explicitly configured shared API.

Examples:
  python3 ledgerctl.py summary --month 2026-10
  python3 ledgerctl.py add --amount 26.07 --date 2026-10-14 \
      --time 10:53 --merchant 'Nations Fresh Foods' --key RECEIPT-STABLE-KEY
  python3 ledgerctl.py add --json < entry.json

Run configure once with an HTTPS URL, or supply LEDGER_URL and LEDGER_TOKEN.
Missing configuration never falls back to localhost. Secrets are never printed.
"""

import argparse
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import getpass
import io
import zipfile
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from client_config import ClientError, Connection, default_config, load_connection, save_connection

_ACTIVE_CONNECTION = None
_CONFIG_PATH = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 can still run the CLI with explicit dates.
    ZoneInfo = None


MAX_RECEIPT_BYTES = 8 * 1024 * 1024
ENTRY_TYPES = ("expense", "income", "transfer")


class LedgerError(Exception):
    """A user-facing error that does not contain authentication credentials."""

    def __init__(self, message, *, status=None, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


def amount_cents(value):
    """Convert a positive decimal string to cents without float rounding."""
    value = str(value).strip()
    if not re.fullmatch(r"\+?(?:\d+(?:\.\d*)?|\.\d+)", value):
        raise LedgerError("金额必须是正十进制数，例如 26.07。")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise LedgerError("金额无效。") from None
    if amount.as_tuple().exponent < -2:
        raise LedgerError("金额最多允许两位小数。")
    if amount <= 0:
        raise LedgerError("金额必须大于 0。")
    # The backend stores SQLite INTEGER cents; keep values within its range.
    cents = amount * 100
    if cents > 9223372036854775807:
        raise LedgerError("金额超出支持范围。")
    return int(cents)


def balance_cents(value):
    """A stated account balance in cents; it may be zero or negative (money owed)."""
    value = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", value):
        raise LedgerError("余额必须是数字，最多两位小数，例如 1234.56；欠款写负数。")
    cents = int(Decimal(value) * 100)
    if abs(cents) > 999999999999:
        raise LedgerError("余额超出支持范围。")
    return cents


def check_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise LedgerError("日期必须为 YYYY-MM-DD。")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise LedgerError("日期无效。") from None
    return value


def check_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise LedgerError("时间必须为 HH:MM。")
    try:
        dt.time.fromisoformat(value)
    except ValueError:
        raise LedgerError("时间无效。") from None
    return value


def check_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise LedgerError("月份必须为 YYYY-MM。")
    check_date(value + "-01")
    return value


def toronto_now():
    """Use Toronto time; never silently substitute the machine's timezone."""
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo("America/Toronto"))
        except (KeyError, ValueError):
            pass
    # Minimal Linux installs sometimes omit the IANA timezone database. UTC is
    # an explicit, documented fallback rather than guessing EST/EDT offsets.
    print("提示：系统缺少 America/Toronto 时区数据，默认日期和时间使用 UTC；"
          "可显式提供 --date 和 --time，或安装系统 tzdata。", file=sys.stderr)
    return dt.datetime.now(dt.timezone.utc)


def read_json():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise LedgerError("标准输入不是有效 JSON：" + str(exc)) from None
    if not isinstance(payload, dict):
        raise LedgerError("--json 必须接收一条 JSON 对象。")
    return payload


def validate_payload(payload, *, creating=False, refund=False):
    """Validate supplied values, leaving absent JSON fields absent."""
    if creating or refund:
        missing = [field for field in ("amount_cents", "date") if field not in payload]
        if missing:
            raise LedgerError("JSON 缺少必填字段：" + ", ".join(missing))
    if "amount_cents" in payload:
        value = payload["amount_cents"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LedgerError("amount_cents 必须为大于 0 的整数分。")
        if value > 9223372036854775807:
            raise LedgerError("amount_cents 超出支持范围。")
    if "date" in payload:
        check_date(payload["date"])
    if "time" in payload and payload["time"] is not None:
        check_time(payload["time"])
    if "currency" in payload and payload["currency"] != "CAD":
        raise LedgerError("当前账本只支持 CAD。")
    if "type" in payload and payload["type"] not in ENTRY_TYPES:
        raise LedgerError("type 必须为 expense、income 或 transfer。")
    if "idempotency_key" in payload:
        if not isinstance(payload["idempotency_key"], str) or not payload["idempotency_key"].strip():
            raise LedgerError("idempotency_key 必须为非空字符串。")
    if "items" in payload and payload["items"] is not None:
        items = payload["items"]
        def bad(item):
            return (not isinstance(item, dict) or set(item) != {"name", "amount_cents"}
                    or not isinstance(item["name"], str) or not 0 < len(item["name"].strip()) <= 200
                    or isinstance(item["amount_cents"], bool) or not isinstance(item["amount_cents"], int)
                    or abs(item["amount_cents"]) > 999999999999)
        if not isinstance(items, list) or len(items) > 200 or any(bad(item) for item in items):
            raise LedgerError("items 必须是最多 200 项的数组；每项只含 name（商品名）和 amount_cents"
                              "（整数分，折扣可为负数）。")
    if "duplicate_acknowledgements" in payload:
        acknowledged = payload["duplicate_acknowledgements"]
        if (not isinstance(acknowledged, list)
                or any(not isinstance(value, str) or not value.strip() for value in acknowledged)
                or len(set(acknowledged)) != len(acknowledged)):
            raise LedgerError("duplicate_acknowledgements 必须是不重复的非空账单 ID 字符串数组。")
    return payload


def command_payload(args):
    fields = ("amount", "date", "time", "merchant", "note", "category", "account",
              "to_account", "receipt_url", "type")
    if args.command in ("add", "refund") and args.unknown_time:
        raise LedgerError("新增和退款不能把时间留空。小票上有时间就用 --time；没有就省略，程序会写入多伦多当前时间。")
    if args.json:
        conflicts = [name for name in fields if getattr(args, name, None) is not None]
        if args.unknown_time:
            conflicts.append("unknown_time")
        if conflicts:
            raise LedgerError("--json 不可同时使用字段选项：" + ", ".join(conflicts))
        payload = read_json()
    else:
        payload = {}
        if args.command in ("add", "refund"):
            if args.amount is None:
                raise LedgerError("请提供 --amount，或使用 --json 输入 amount_cents。")
            if args.date is None or args.time is None:
                now = toronto_now()
                payload["date"] = args.date or now.strftime("%Y-%m-%d")
                payload["time"] = args.time or now.strftime("%H:%M")
        for field in fields:
            value = getattr(args, field, None)
            if value is not None:
                payload["amount_cents" if field == "amount" else field] = (
                    amount_cents(value) if field == "amount" else value
                )
        if args.unknown_time:
            payload["time"] = None
        if args.command == "add":
            payload.setdefault("type", "expense")
            payload.setdefault("account", "Spending")
            if "category" not in payload and payload["type"] != "transfer":
                raise LedgerError("请用 --category 指定分类（meta 返回的已有分类）。")
    if args.currency is not None:
        payload["currency"] = args.currency
    if args.key is not None:
        if "idempotency_key" in payload and payload["idempotency_key"] != args.key:
            raise LedgerError("--key 与 JSON 中的 idempotency_key 不一致。")
        payload["idempotency_key"] = args.key
    acknowledged = getattr(args, "acknowledge_duplicate", None)
    if acknowledged is not None:
        if ("duplicate_acknowledgements" in payload
                and payload["duplicate_acknowledgements"] != acknowledged):
            raise LedgerError("--acknowledge-duplicate 与 JSON 中的 duplicate_acknowledgements 不一致。")
        payload["duplicate_acknowledgements"] = acknowledged
    if args.command in ("add", "refund") and payload.get("time") is None:
        now = toronto_now()
        payload["time"] = now.strftime("%H:%M")
        if not payload.get("date"):
            payload["date"] = now.strftime("%Y-%m-%d")
    if args.command not in ("add", "refund") and "duplicate_acknowledgements" in payload:
        raise LedgerError("duplicate_acknowledgements 仅用于新增或退款。")
    if args.command == "edit" and not payload:
        raise LedgerError("请至少提供一个要修改的字段。")
    validate_payload(payload, creating=args.command == "add", refund=args.command == "refund")
    # Currency is fixed by this backend rather than a mutable entry attribute.
    payload.pop("currency", None)
    return payload


def connection():
    global _ACTIVE_CONNECTION
    if _ACTIVE_CONNECTION is None:
        path = _CONFIG_PATH or os.environ.get("LEDGER_CONFIG") or default_config("grok")
        try:
            _ACTIVE_CONNECTION = load_connection(path)
        except ClientError as error:
            raise LedgerError(str(error)) from None
    return _ACTIVE_CONNECTION


def api_request(method, path, *, payload=None, query=None):
    try:
        client = connection()
        if client.backend == "vault":
            # The encrypted ledger answers the same routes, worked out here (acost_vault, needs cryptography).
            from acost_vault.backend import VaultBackend
            client = VaultBackend(client)
        raw, _ = client.request(method, "/api/" + path.lstrip("/"),
                                payload=payload, query=query, max_bytes=256 * 1024 * 1024)
        return raw
    except ClientError as error:
        raise LedgerError(str(error), status=error.status, details=error.details) from None


def positive_version(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerError("expected_version 必须是读取原单返回的正整数版本号。")
    return value


def require_saved_entry(result):
    if not isinstance(result, dict) or not isinstance(result.get("entry"), dict) or not result["entry"].get("id"):
        raise LedgerError("服务未返回实际保存的账单 ID，不能确认已保存；请用同一幂等键核对或重试。")
    return result


def category_names():
    meta = json_response(api_request("GET", "meta"))
    return {item.get("name") for item in meta.get("categories") or [] if isinstance(item, dict)}


def guard_category(payload, allow_new):
    """The service creates any unseen category name, so stop new names here."""
    category = payload.get("category")
    if not isinstance(category, str) or not category.strip() or allow_new:
        return
    if category not in category_names():
        raise LedgerError("分类「" + category + "」不在账本里。先问用户：改用 meta 返回的已有分类，"
                          "或在用户同意新建后加 --allow-new-category 重试。")


def guard_items_total(payload, entry_id=None):
    """A receipt's line items must add up to the entry amount."""
    items, amount = payload.get("items"), payload.get("amount_cents")
    if entry_id is not None and (("items" in payload) != ("amount_cents" in payload)):
        if "items" in payload and not items:
            return
        saved = json_response(api_request("GET", entry_path(entry_id))).get("entry") or {}
        if "items" not in payload:
            items = saved.get("items")
        else:
            amount = saved.get("amount_cents")
    if not items or not isinstance(amount, int):
        return
    total = sum(item.get("amount_cents", 0) for item in items if isinstance(item, dict))
    if total != amount:
        raise LedgerError("items 合计 %d 分，不等于这一笔的金额 %d 分。核对小票（税单独作为一项），"
                          "金额和 items 要一起改对；看不清就不附 items。" % (total, amount))


def configure_client(args):
    if not args.url:
        raise LedgerError("配置共享模式需要 --url https://你的服务地址。")
    if args.token_stdin:
        token = sys.stdin.readline().rstrip("\r\n")
    elif sys.stdin.isatty():
        token = getpass.getpass("粘贴账本钥匙（ql_ 开头，不会显示）：").strip()
    else:
        raise LedgerError("请在交互终端输入凭据，或使用 --token-stdin 安全传入；不要放在命令参数中。")
    if args.backend == "vault":
        return configure_vault(args, token)
    try:
        client = Connection(args.url, token)
        raw, _ = client.request("GET", "/api/meta")
        meta = json_response(raw)
        role = meta.get("client", {}).get("role")
        if role not in {"ai", "admin", "readonly"}:
            raise LedgerError("服务没有返回共享客户端角色，配置未保存。")
        path = _CONFIG_PATH or os.environ.get("LEDGER_CONFIG") or default_config("grok")
        storage = save_connection(path, client.url, token)
        return {"ok": True, "mode": "shared", "role": role, "credential_storage": storage}
    except ClientError as error:
        raise LedgerError(str(error)) from None


def configure_vault(args, token):
    """Save an encrypted-ledger key (for example the Mac's backup key). The master key is not involved."""
    if not args.ledger_id:
        raise LedgerError("加密版需要 --ledger-id（SQL Editor 里 create_ledger 返回的编号）。")
    from acost_vault.sync import Transport, VaultError
    try:
        client = Connection(args.url, token, backend="vault", ledger_id=args.ledger_id)
        status, state = Transport(client.url, token).call("GET", "/api/state")
    except (ClientError, VaultError) as error:
        raise LedgerError(str(error)) from None
    if status != 200:
        raise LedgerError("HTTP %d：%s；配置未保存。" % (status, state.get("message", "无法读取账本")))
    if state.get("ledger_id") != args.ledger_id.lower():
        raise LedgerError("这把钥匙属于另一本账，和 --ledger-id 不一致；配置未保存。")
    path = _CONFIG_PATH or os.environ.get("LEDGER_CONFIG") or default_config("grok")
    storage = save_connection(path, client.url, token, backend="vault", ledger_id=state["ledger_id"])
    return {"ok": True, "mode": "shared", "backend": "vault", "role": state["credential"]["role"],
            "ledger": state["name"], "credential_storage": storage}


def json_response(raw):
    if not raw:
        return {"ok": True}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise LedgerError("服务返回了无效 JSON。") from None


def save_output(path, raw, overwrite):
    destination = Path(path).expanduser()
    try:
        with destination.open("wb" if overwrite else "xb") as output:
            output.write(raw)
    except FileExistsError:
        raise LedgerError("输出文件已存在；如需覆盖，请显式使用 --overwrite。") from None
    except OSError as exc:
        raise LedgerError("无法保存文件：" + str(exc)) from None
    return {"ok": True, "output": str(destination.resolve()), "bytes": len(raw)}


def redact_token(value, token):
    """Avoid displaying a token even if a server unexpectedly reflects it."""
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [redact_token(item, token) for item in value]
    if isinstance(value, dict):
        return {redact_token(key, token): redact_token(item, token)
                for key, item in value.items()}
    return value


def entry_path(entry_id):
    if not str(entry_id).strip():
        raise LedgerError("账单 ID 不能为空。")
    return "entries/" + urllib.parse.quote(str(entry_id), safe="")


def send_report(args):
    """`vault report --file problem.json`: send Hao a problem report (acost_vault/report.py; no cryptography needed).
    With this machine's ledger key when there is one; with --invite when the install never got that far."""
    from acost_vault import report
    from acost_vault.sync import VaultError
    if not args.file:
        raise LedgerError("report 需要 --file 报告（JSON：step、summary、error、checks）。")
    try:
        problem = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LedgerError("读不了报告文件：%s" % error) from None
    try:
        if args.invite:
            return report.send(problem, invite=args.invite, url=args.url or report.SERVICE_URL,
                               use_env_proxy=os.environ.get("LEDGER_USE_ENV_PROXY") == "1")
        client = connection()
        if client.backend != "vault":
            raise LedgerError("问题报告只发到加密版账本的服务（backend: vault）。")
        return report.send(problem, token=client.token, url=client.url, use_env_proxy=client.use_env_proxy)
    except VaultError as error:
        raise LedgerError(str(error), status=error.status) from None


def execute(args):
    command = args.command
    if command == "configure":
        return configure_client(args)
    if command == "vault" and args.action == "report":
        return send_report(args)
    if command == "vault":
        try:
            from acost_vault import admin
            from acost_vault.sync import VaultError
        except ImportError as error:
            raise LedgerError("加密版需要 cryptography 库；请用 ~/acost-venv/bin/python 运行（%s）。" % error) from None
        try:
            if args.action == "join":
                if not args.invite:
                    raise LedgerError("join 需要 --invite 邀请码。")
                return admin.join(args.invite, url=args.url or admin.SERVICE_URL,
                                  directory=Path(_CONFIG_PATH).expanduser().parent if _CONFIG_PATH else None)
            return admin.run(connection(), args.action, args, sys.stdin)
        except VaultError as error:
            raise LedgerError(str(error), status=error.status) from None
        except ClientError as error:
            raise LedgerError(str(error), status=error.status, details=error.details) from None
    if command == "get":
        return json_response(api_request("GET", entry_path(args.id), query={"include_deleted": "true"} if args.include_deleted else None))
    if command == "add-category":
        payload = {"name": args.name}
        if args.color:
            payload["color"] = args.color
        return json_response(api_request("POST", "categories", payload=payload))
    if command == "set-balance":
        return json_response(api_request("POST", "accounts/balance", payload={
            "account": args.account, "balance_cents": balance_cents(args.amount)}))
    if command in ("meta", "health"):
        return json_response(api_request("GET", command))
    if command in ("list", "summary"):
        query = {}
        for field in ("month", "q", "account", "category", "type", "limit", "offset"):
            value = getattr(args, field, None)
            if value is not None:
                query[field] = check_month(value) if field == "month" else value
        if command == "list":
            if args.limit < 1 or args.limit > 500 or args.offset < 0:
                raise LedgerError("limit 必须为 1–500，offset 不能小于 0。")
        return json_response(api_request("GET", "entries" if command == "list" else "summary", query=query))
    if command in ("add", "refund", "edit"):
        payload = command_payload(args)
        if command == "edit":
            supplied = payload.get("expected_version")
            if args.expected_version is not None:
                if supplied is not None and supplied != args.expected_version:
                    raise LedgerError("--expected-version 与 JSON 版本不一致。")
                supplied = args.expected_version
            payload["expected_version"] = positive_version(supplied)
        elif not payload.get("idempotency_key"):
            raise LedgerError("新增或退款必须提供稳定 --key / idempotency_key，重试沿用同一键。")
        if command != "refund":
            guard_items_total(payload, args.id if command == "edit" else None)
            guard_category(payload, args.allow_new_category)
            if args.allow_new_category:
                payload["allow_new_category"] = True
        path = "entries" if command == "add" else entry_path(args.id)
        if command == "refund":
            path += "/refunds"
        return require_saved_entry(json_response(api_request("PATCH" if command == "edit" else "POST", path, payload=payload)))
    if command in ("delete", "restore"):
        path = entry_path(args.id) + ("/restore" if command == "restore" else "")
        return json_response(api_request("DELETE" if command == "delete" else "POST", path,
                                         payload={"expected_version": positive_version(args.expected_version)}))
    if command == "upload":
        source = Path(args.file).expanduser()
        try:
            if not source.is_file():
                raise LedgerError("小票路径必须是文件。")
            if source.stat().st_size > MAX_RECEIPT_BYTES:
                raise LedgerError("小票文件不能超过 8 MB。")
            with source.open("rb") as stream:
                data = stream.read(MAX_RECEIPT_BYTES + 1)
        except OSError as exc:
            raise LedgerError("无法读取小票：" + str(exc)) from None
        if len(data) > MAX_RECEIPT_BYTES:
            raise LedgerError("小票文件不能超过 8 MB。")
        if not data:
            raise LedgerError("小票文件为空。")
        return json_response(api_request("POST", "receipts", payload={
            "filename": source.name,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }))
    if command in ("export", "backup"):
        # Check before requesting, and use exclusive creation again when saving.
        destination = Path(args.output).expanduser()
        if destination.exists() and not args.overwrite:
            raise LedgerError("输出文件已存在；如需覆盖，请显式使用 --overwrite。")
        raw = api_request("GET", command)
        if command == "export":
            json_response(raw)
        elif connection().mode == "shared":
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = archive.namelist()
                    if "manifest.json" not in names or not any(name.endswith(".sqlite3") for name in names):
                        raise ValueError()
                    json.loads(archive.read("manifest.json"))
            except (zipfile.BadZipFile, ValueError, KeyError):
                raise LedgerError("服务返回的备份不是有效的完整备份包。") from None
        elif not raw.startswith(b"SQLite format 3\x00"):
            raise LedgerError("本地原型返回的备份不是有效 SQLite 文件。")
        return save_output(destination, raw, args.overwrite)
    raise LedgerError("未知命令。")


def add_payload_options(parser, *, refund=False, editing=False):
    parser.add_argument("--json", action="store_true", help="从标准输入读取单条原始 JSON 对象")
    parser.add_argument("--amount", help="正金额，最多两位小数；例如 26.07")
    parser.add_argument("--currency", choices=("CAD",), help="目前只支持 CAD")
    parser.add_argument("--date", help="YYYY-MM-DD；添加/退款默认多伦多当天")
    time_options = parser.add_mutually_exclusive_group()
    time_options.add_argument("--time", help="实际时间 HH:MM；小票上的时间优先，添加/退款省略时使用多伦多当前时间")
    time_options.add_argument("--unknown-time", action="store_true", help="仅编辑时可用，会清空时间；新增和退款不要使用")
    parser.add_argument("--note", help="备注")
    parser.add_argument("--key", help="稳定去重键；重试同笔录入必须使用相同键")
    if not editing:
        parser.add_argument("--acknowledge-duplicate", action="append", metavar="ID",
                            help="核对并确认是另一笔交易后，逐个填写疑似重复账单 ID；可重复使用")
    if not refund:
        parser.add_argument("--type", choices=ENTRY_TYPES, help="添加默认为 expense")
        parser.add_argument("--merchant", help="商家")
        parser.add_argument("--category", help="分类；必须是 meta 里已有的")
        parser.add_argument("--allow-new-category", action="store_true",
                            help="仅在用户明确同意新建这个分类后使用；否则不在账本里的分类会被拒绝")
        parser.add_argument("--account", help="添加默认为 Spending")
        parser.add_argument("--to-account", help="转账的收款账户")
        parser.add_argument("--receipt-url", help="上传接口返回的小票 URL")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Acost API 命令行：查询、录入、退款、编辑、撤销与备份。",
        epilog="默认共享模式：configure 保存 HTTPS 地址与凭据，或 LEDGER_URL/LEDGER_TOKEN。没有本地默认地址。"
               "--json 模式不会补猜金额或日期。",
    )
    parser.add_argument("--config", type=Path, help="私有配置路径；默认用户配置目录 grok.json")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure", help="验证并保存共享 HTTPS 地址与专属凭据")
    config.add_argument("--url", required=True)
    config.add_argument("--token-stdin", action="store_true", help="从标准输入读取一行凭据，避免命令历史")
    config.add_argument("--backend", choices=("ledger", "vault"), default="ledger", help="vault：加密版账本")
    config.add_argument("--ledger-id", help="加密版：这把钥匙所属账本的编号")
    get = commands.add_parser("get", help="按真实 ID 读取原单及当前版本，支持跨月查询")
    get.add_argument("id")
    get.add_argument("--include-deleted", action="store_true", help="恢复前读取已删除原单")
    commands.add_parser("health", help="检查服务状态")
    commands.add_parser("meta", help="读取账户、分类和配置")
    category = commands.add_parser("add-category", help="新建一个分类；只在用户同意后用，平时用 tools/categories.py enable")
    category.add_argument("name", help="分类名，如 Bills-Internet")
    category.add_argument("--color", help="#RRGGBB；默认灰色")
    balance = commands.add_parser("set-balance", help="登记一个账户现在的真实余额；账本据此算出期初余额，以后的余额才是真的")
    balance.add_argument("account", help="账户名，如 Spending")
    balance.add_argument("--amount", required=True, help="现在的真实余额，如 1234.56；欠款写负数，如 --amount=-80")
    listing = commands.add_parser("list", help="查询账单")
    listing.add_argument("--month", help="YYYY-MM")
    listing.add_argument("--q", "--query", dest="q", help="搜索文字")
    listing.add_argument("--account")
    listing.add_argument("--category")
    listing.add_argument("--type", choices=ENTRY_TYPES + ("refund",))
    listing.add_argument("--limit", type=int, default=100, help="每页 1–500 条，默认 100")
    listing.add_argument("--offset", type=int, default=0, help="跳过条数；按 next_offset 继续分页")
    summary = commands.add_parser("summary", help="按月统计；省略月份由服务选择")
    summary.add_argument("--month", help="YYYY-MM")
    addition = commands.add_parser("add", help="添加收入、支出或转账")
    add_payload_options(addition)
    refund = commands.add_parser("refund", help="为指定账单添加退款")
    refund.add_argument("id", help="原始账单 ID")
    add_payload_options(refund, refund=True)
    edit = commands.add_parser("edit", help="修改指定账单")
    edit.add_argument("id", help="账单 ID")
    edit.add_argument("--expected-version", type=int, help="先 get 原单读取 version；JSON 可提供 expected_version")
    add_payload_options(edit, editing=True)
    for command, help_text in (("delete", "软删除指定账单"), ("restore", "恢复软删除账单")):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("id", help="账单 ID")
        sub.add_argument("--expected-version", type=int, required=True, help="先 get 原单读取 version")
    vault = commands.add_parser("vault", help="加密版账本：开通、恢复码、自检、搬账、导出")
    vault.add_argument("action", choices=("join", "init", "recovery-code", "restore", "check", "import", "export", "report"),
                       help="join 用邀请码加入；init 开通；recovery-code 显示恢复码（只给用户看）；restore 从标准输入读恢复码；"
                            "check 自检；import 搬入旧备份；export 导出成旧备份格式；report 给 Hao 发问题报告（用 tools/report.py）")
    vault.add_argument("--invite", help="join：Hao 发的一次性邀请码；report：还没有钥匙时用它代替")
    vault.add_argument("--url", help="join、report：加密账本地址；默认 Acost 的服务")
    vault.add_argument("--empty", action="store_true", help="init：不建起步账户和分类（马上要 import 旧账时用）")
    vault.add_argument("--book", help="init：账本名")
    vault.add_argument("--file", help="import：cloud_admin.py backup 的 ZIP 或快照 JSON；report：报告 JSON")
    vault.add_argument("--compare-with", type=Path, help="import：旧账本配置，逐月和旧账本的汇总比")
    vault.add_argument("--output", help="export：输出 ZIP")
    upload = commands.add_parser("upload", help="上传最多 8 MB 的小票文件")
    upload.add_argument("file", help="本地小票路径")
    for command, help_text in (("export", "导出完整 JSON"), ("backup", "保存共享完整备份 ZIP（管理员权限）")):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--output", required=True, help="输出文件路径")
        sub.add_argument("--overwrite", action="store_true", help="明确允许覆盖现有输出文件")
    return parser


def main(argv=None):
    global _ACTIVE_CONNECTION, _CONFIG_PATH
    args = build_parser().parse_args(argv)
    _ACTIVE_CONNECTION = None
    _CONFIG_PATH = args.config
    try:
        result = execute(args)
        print(json.dumps(redact_token(result, _ACTIVE_CONNECTION.token if _ACTIVE_CONNECTION else os.environ.get("LEDGER_TOKEN")),
                         ensure_ascii=False, allow_nan=False))
        return 0
    except (LedgerError, ValueError, TypeError) as exc:
        # JSON stderr allows callers to parse failures without scraping prose.
        message = str(exc)
        token = _ACTIVE_CONNECTION.token if _ACTIVE_CONNECTION else os.environ.get("LEDGER_TOKEN")
        if token:
            message = message.replace(token, "[redacted]")
        failure = {"ok": False, "error": message}
        if isinstance(exc, LedgerError) and exc.details:
            failure.update(exc.details, message=message, status=exc.status)
        print(json.dumps(redact_token(failure, token), ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "已取消。"}, ensure_ascii=False), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
