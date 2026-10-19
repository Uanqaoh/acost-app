"""Single-user SQLite ledger. Money is stored as integer cents throughout."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import contextmanager, closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_CATEGORIES = [
    ("餐饮-Grocery", "#b49459"), ("餐饮-Eating out", "#ce9c81"),
    ("购物", "#9886ab"), ("住房", "#859982"),
    ("交通", "#7c9fb2"), ("其他", "#aa9d89"),
]
VALID_TYPES = {"expense", "income", "transfer"}
MAX_AMOUNT = 999_999_999_999
MAX_PAGE_SIZE = 500
ROLES = {"readonly", "ai", "admin"}


class LedgerError(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status, self.code = status, code


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def string(value, field, limit, optional=False):
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise LedgerError(f"{field} 必须是字符串")
    value = value.strip()
    if len(value) > limit or (not value and not optional):
        raise LedgerError(f"{field} 长度不正确")
    return value


def amount(value):
    if type(value) is not int or not 0 < value <= MAX_AMOUNT:
        raise LedgerError("amount_cents 必须是大于零的整数分")
    return value


def date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise LedgerError("date 必须使用 YYYY-MM-DD 格式")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise LedgerError("日期不存在")
    return value


def time(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise LedgerError("time 必须使用 HH:MM 格式")
    return value


def month(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", value):
        raise LedgerError("month 必须使用 YYYY-MM 格式")
    return value


def pagination(filters):
    values = []
    for field, default in (("limit", 100), ("offset", 0)):
        raw = filters.get(field, default)
        if type(raw) not in (str, int) or not re.fullmatch(r"\d{1,10}", str(raw)):
            raise LedgerError(f"{field} 必须为整数")
        values.append(int(raw))
    limit, offset = values
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise LedgerError(f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间")
    return limit, offset


class Ledger:
    def __init__(self, data_dir, demo=False):
        self.timezone_name = os.environ.get("LEDGER_TIMEZONE", "America/Toronto")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "ledger.sqlite3"
        self.receipts_dir = self.data_dir / "receipts"
        self.receipts_dir.mkdir(exist_ok=True)
        self._initialize(demo)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _initialize(self, demo):
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT PRIMARY KEY, opening_balance_cents INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT PRIMARY KEY, color TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    date TEXT NOT NULL, time TEXT NOT NULL, merchant TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL REFERENCES accounts(name),
                    to_account TEXT, original_id TEXT REFERENCES entries(id),
                    receipt_url TEXT, idempotency_key TEXT UNIQUE, request_hash TEXT,
                    deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS entries_date ON entries(date, time);
                CREATE INDEX IF NOT EXISTS entries_original ON entries(original_id);
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, entity_id TEXT NOT NULL,
                    before_json TEXT, after_json TEXT);
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    token_hash TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
                CREATE TABLE IF NOT EXISTS receipts (
                    filename TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    created_at TEXT NOT NULL, created_by TEXT NOT NULL);
            """)
            # Additive migration preserves IDs, financial amounts, and idempotency keys.
            columns = {row["name"] for row in con.execute("PRAGMA table_info(entries)")}
            for name, definition in {
                "version": "INTEGER NOT NULL DEFAULT 1",
                "created_by": "TEXT NOT NULL DEFAULT 'legacy'",
                "updated_by": "TEXT NOT NULL DEFAULT 'legacy'",
            }.items():
                if name not in columns:
                    con.execute(f"ALTER TABLE entries ADD COLUMN {name} {definition}")
            initialized = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            if initialized:
                con.execute("UPDATE settings SET value='2' WHERE key='schema_version'")
                con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('opening_balances_initialized','false')")
                return
            con.executemany("INSERT INTO settings(key,value) VALUES (?,?)", [
                ("schema_version", "2"), ("book", "默认账本"),
                ("currency", "CAD"), ("demo", "true" if demo else "false"),
                ("opening_balances_initialized", "true" if demo else "false")])
            con.executemany("INSERT INTO accounts(name,opening_balance_cents) VALUES (?,?)", [
                ("Spending", 300000 if demo else 0), ("Savings", 0)])
            con.executemany("INSERT INTO categories(name,color) VALUES (?,?)", DEFAULT_CATEGORIES)
            if demo:
                values = {
                    "type": "expense", "amount_cents": 2607,
                    "date": "2026-10-14", "time": "10:53",
                    "merchant": "Nations Fresh Foods", "category": "餐饮-Grocery", "account": "Spending",
                    "note": "演示数据，未导入 iCost。喜茶抹茶茉莉奶茶 3 件 CAD 5.00；统一番茄鸡蛋方便面 CAD 7.99；牛眼肉片 CAD 11.91；葡萄柚味 Perrier 气泡水 CAD 0.99；塑料袋 CAD 0.05。税前 CAD 25.94，HST CAD 0.13，总额 CAD 26.07。Spending 初始余额 CAD 3,000.00 为模拟值。",
                }
                self._insert(con, self._normalize(con, values), None, None, "demo")

    def _account(self, con, value):
        value = string(value, "account", 100)
        if not con.execute("SELECT 1 FROM accounts WHERE name=?", (value,)).fetchone():
            raise LedgerError(f"账户不存在：{value}")
        return value

    def _normalize(self, con, values, refund_original=None):
        if not isinstance(values, dict):
            raise LedgerError("请求内容必须是 JSON 对象")
        kind = values.get("type", "expense")
        if refund_original:
            kind = "refund"
        elif not isinstance(kind, str) or kind not in VALID_TYPES:
            raise LedgerError("type 必须是 expense、income 或 transfer；退款请使用 refunds 接口")
        if "currency" in values and values["currency"] != "CAD":
            raise LedgerError("本账本仅支持 CAD，请先把外币转换成加元")
        current = None
        if "date" not in values or "time" not in values:
            try:
                current = dt.datetime.now(ZoneInfo(self.timezone_name))
            except (ZoneInfoNotFoundError, ValueError):
                raise LedgerError("账本时区不可用，请显式提供 date（YYYY-MM-DD）和 time（HH:MM），或配置有效的 LEDGER_TIMEZONE 与系统时区数据")
        result = {
            "type": kind, "amount_cents": amount(values.get("amount_cents")),
            "date": date(values["date"] if "date" in values else current.date().isoformat()),
            "time": time(values["time"] if "time" in values else current.strftime("%H:%M")),
            "merchant": string(values.get("merchant", ""), "merchant", 300, True),
            "note": string(values.get("note", ""), "note", 10000, True),
            "category": string(values.get("category", "其他"), "category", 100, True),
            "account": self._account(con, values.get("account", "Spending")),
            "to_account": None, "original_id": None, "receipt_url": None,
        }
        if kind == "transfer":
            result["to_account"] = self._account(con, values.get("to_account"))
            if result["account"] == result["to_account"]:
                raise LedgerError("转出与转入账户不能相同")
            result["category"] = ""
        if refund_original:
            result.update(account=refund_original["account"], category=refund_original["category"],
                          original_id=refund_original["id"], merchant=refund_original["merchant"])
        receipt = values.get("receipt_url")
        if receipt:
            receipt = string(receipt, "receipt_url", 150)
            match = re.fullmatch(r"/api/receipts/([a-f0-9]{32}\.(?:png|jpg|webp))", receipt)
            if not match or not (self.receipts_dir / match[1]).is_file():
                raise LedgerError("receipt_url 必须引用已上传的小票")
            result["receipt_url"] = receipt
        if result["category"]:
            con.execute("INSERT OR IGNORE INTO categories(name,color) VALUES (?,?)", (result["category"], "#aa9d89"))
        return result

    def _key(self, values):
        if not isinstance(values, dict):
            raise LedgerError("请求内容必须是 JSON 对象")
        key = values.get("idempotency_key")
        return string(key, "idempotency_key", 200) if key is not None else None

    def _hash(self, action, values):
        return hashlib.sha256(json.dumps({"action": action, "entry": values}, ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _existing(self, con, key, request_hash):
        if not key:
            return None
        row = con.execute("SELECT * FROM entries WHERE idempotency_key=?", (key,)).fetchone()
        if row and row["request_hash"] != request_hash:
            raise LedgerError("该 idempotency_key 已用于另一项操作", 409, "idempotency_conflict")
        return self._serialize(con, row) if row else None

    @staticmethod
    def _revision(con):
        return con.execute("SELECT COALESCE(MAX(sequence),0) FROM audit_events").fetchone()[0]

    def _audit(self, con, actor, action, entity_id, before=None, after=None):
        actor = string(actor, "actor", 200)
        con.execute("INSERT INTO audit_events(at,actor,action,entity_id,before_json,after_json) VALUES (?,?,?,?,?,?)",
                    (now(), actor, action, entity_id,
                     json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
                     json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None))

    def _check_version(self, row, expected_version):
        if expected_version is None:
            return  # The legacy, loopback-only prototype retains its original contract.
        if type(expected_version) is not int or expected_version < 1:
            raise LedgerError("expected_version 必须为正整数")
        if row["version"] != expected_version:
            raise LedgerError("账单已被其他客户端修改，请重新读取后核对", 409, "version_conflict")

    def _touch_original(self, con, original_id, actor, before):
        con.execute("UPDATE entries SET version=version+1,updated_at=?,updated_by=? WHERE id=?",
                    (now(), actor, original_id))
        after = self._serialize(con, self._row(con, original_id, True))
        self._audit(con, actor, "refund_balance_changed", original_id, before, after)

    def _insert(self, con, values, key, request_hash, actor="local"):
        stamp = now()
        record = dict(values, id=uuid.uuid4().hex, idempotency_key=key,
                      request_hash=request_hash, created_at=stamp, updated_at=stamp,
                      version=1, created_by=actor, updated_by=actor)
        columns = list(record)
        con.execute(f"INSERT INTO entries ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [record[key] for key in columns])
        result = self._serialize(con, con.execute("SELECT * FROM entries WHERE id=?", (record["id"],)).fetchone())
        self._audit(con, actor, "refund" if values["type"] == "refund" else "create", result["id"], after=result)
        return result

    def _row(self, con, entry_id, include_deleted=False):
        row = con.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            raise LedgerError("账单不存在", 404, "not_found")
        return row

    def _refunded(self, con, entry_id):
        return con.execute("SELECT COALESCE(SUM(amount_cents),0) FROM entries WHERE original_id=? AND deleted_at IS NULL",
                           (entry_id,)).fetchone()[0]

    def _serialize(self, con, row):
        result = {key: row[key] for key in [
            "id", "type", "amount_cents", "date", "time", "merchant", "note", "category", "account",
            "to_account", "original_id", "receipt_url", "deleted_at", "created_at", "updated_at",
            "version", "created_by", "updated_by"]}
        result["refunded_cents"] = self._refunded(con, row["id"]) if row["type"] == "expense" else 0
        return result

    def create(self, values, actor="local"):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            key = self._key(values)
            digest = self._hash("create", {field: value for field, value in values.items() if field != "idempotency_key"})
            existing = self._existing(con, key, digest)
            if existing:
                return existing
            normalized = self._normalize(con, values)
            return self._insert(con, normalized, key, digest, actor)

    def refund(self, entry_id, values, actor="local", expected_version=None):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            key = self._key(values)
            digest = self._hash("refund:" + entry_id, {field: value for field, value in values.items() if field != "idempotency_key"})
            existing = self._existing(con, key, digest)
            if existing:
                return existing
            original = self._row(con, entry_id)
            self._check_version(original, expected_version)
            if original["type"] != "expense":
                raise LedgerError("退款必须关联一笔支出", 409, "refund_conflict")
            normalized = self._normalize(con, values, original)
            if self._refunded(con, entry_id) + normalized["amount_cents"] > original["amount_cents"]:
                raise LedgerError("累计退款不能超过原支出金额", 409, "refund_conflict")
            before = self._serialize(con, original)
            result = self._insert(con, normalized, key, digest, actor)
            self._touch_original(con, entry_id, actor, before)
            return result

    def edit(self, entry_id, values, actor="local", expected_version=None):
        if not isinstance(values, dict):
            raise LedgerError("请求内容必须是 JSON 对象")
        allowed = {"type", "amount_cents", "date", "time", "merchant", "note", "category", "account", "to_account", "receipt_url"}
        if set(values) - allowed:
            raise LedgerError("包含不支持修改的字段")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._row(con, entry_id)
            self._check_version(row, expected_version)
            before = self._serialize(con, row)
            if row["type"] == "refund":
                raise LedgerError("退款请先撤销，再重新登记", 409, "refund_conflict")
            normalized = self._normalize(con, dict(row) | values)
            refunded = self._refunded(con, entry_id)
            if refunded and (normalized["type"] != "expense" or normalized["amount_cents"] < refunded
                             or normalized["account"] != row["account"] or normalized["category"] != row["category"]):
                raise LedgerError("已有退款：不能改变类型、账户、分类或把金额改为小于已退金额", 409, "refund_conflict")
            normalized["updated_at"] = now()
            normalized["updated_by"] = actor
            normalized["version"] = row["version"] + 1
            con.execute(f"UPDATE entries SET {','.join(key+'=?' for key in normalized)} WHERE id=?",
                        [normalized[key] for key in normalized] + [entry_id])
            result = self._serialize(con, self._row(con, entry_id))
            self._audit(con, actor, "edit", entry_id, before, result)
            return result

    def delete(self, entry_id, actor="local", expected_version=None):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._row(con, entry_id, True)
            self._check_version(row, expected_version)
            if row["deleted_at"]:
                return self._serialize(con, row)
            before = self._serialize(con, row)
            if row["type"] == "expense" and self._refunded(con, entry_id):
                raise LedgerError("请先撤销相关退款，再删除原支出", 409, "refund_conflict")
            stamp = now()
            original_before = self._serialize(con, self._row(con, row["original_id"], True)) if row["type"] == "refund" else None
            con.execute("UPDATE entries SET deleted_at=?,updated_at=?,updated_by=?,version=version+1 WHERE id=?", (stamp, stamp, actor, entry_id))
            result = self._serialize(con, self._row(con, entry_id, True))
            self._audit(con, actor, "delete", entry_id, before, result)
            if original_before:
                self._touch_original(con, row["original_id"], actor, original_before)
            return result

    def restore(self, entry_id, actor="local", expected_version=None):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._row(con, entry_id, True)
            self._check_version(row, expected_version)
            if not row["deleted_at"]:
                return self._serialize(con, row)
            before = self._serialize(con, row)
            original_before = None
            if row["deleted_at"] and row["type"] == "refund":
                original = self._row(con, row["original_id"])
                original_before = self._serialize(con, original)
                if original["type"] != "expense" or self._refunded(con, original["id"]) + row["amount_cents"] > original["amount_cents"]:
                    raise LedgerError("恢复后会超过原支出金额，无法恢复", 409, "refund_conflict")
                if row["account"] != original["account"] or row["category"] != original["category"]:
                    raise LedgerError("原支出的账户或分类已改变，无法恢复退款", 409, "refund_conflict")
            con.execute("UPDATE entries SET deleted_at=NULL,updated_at=?,updated_by=?,version=version+1 WHERE id=?", (now(), actor, entry_id))
            result = self._serialize(con, self._row(con, entry_id))
            self._audit(con, actor, "restore", entry_id, before, result)
            if original_before:
                self._touch_original(con, row["original_id"], actor, original_before)
            return result

    def entry(self, entry_id, include_deleted=False):
        with self.connect() as con:
            con.execute("BEGIN")
            return self._serialize(con, self._row(con, entry_id, include_deleted))

    def entries(self, filters):
        limit, offset = pagination(filters)
        selected_month = month(filters.get("month"))
        conditions, params = ["deleted_at IS NULL"], []
        if selected_month:
            conditions.append("substr(date,1,7)=?")
            params.append(selected_month)
        for field in ("category", "account", "type"):
            value = filters.get(field)
            if value:
                if field == "type" and value not in VALID_TYPES | {"refund"}:
                    raise LedgerError("未知账单类型")
                conditions.append(field + "=?")
                params.append(value)
        query = filters.get("q", "")
        if query:
            query = string(query, "q", 300)
            conditions.append("(instr(lower(merchant),lower(?))>0 OR instr(lower(note),lower(?))>0 OR instr(lower(category),lower(?))>0)")
            params.extend([query] * 3)
        with self.connect() as con:
            con.execute("BEGIN")
            where = " AND ".join(conditions)
            total = con.execute("SELECT COUNT(*) FROM entries WHERE " + where, params).fetchone()[0]
            rows = con.execute("SELECT * FROM entries WHERE " + where + " ORDER BY date DESC,time DESC,created_at DESC,id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            return {"entries": [self._serialize(con, row) for row in rows], "total": total,
                    "limit": limit, "offset": offset, "revision": self._revision(con),
                    "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def _balances(self, con):
        balances = {row["name"]: row["opening_balance_cents"] for row in con.execute("SELECT * FROM accounts")}
        for row in con.execute("SELECT type,amount_cents,account,to_account FROM entries WHERE deleted_at IS NULL"):
            delta = row["amount_cents"] if row["type"] in {"income", "refund"} else -row["amount_cents"]
            balances[row["account"]] += delta
            if row["type"] == "transfer":
                balances[row["to_account"]] += row["amount_cents"]
        return balances

    def _meta(self, con):
        settings = dict(con.execute("SELECT key,value FROM settings"))
        balances = self._balances(con)
        return {"book": settings["book"], "currency": settings["currency"], "revision": self._revision(con),
                "opening_balances_initialized": settings.get("opening_balances_initialized") == "true",
                "demo": settings["demo"] == "true", "accounts": [
                    {"id": row["name"], "name": row["name"], "balance_cents": balances[row["name"]],
                     "opening_balance_cents": row["opening_balance_cents"]}
                    for row in con.execute("SELECT * FROM accounts ORDER BY rowid")],
                "categories": [{"id": row["name"], "name": row["name"], "color": row["color"]}
                               for row in con.execute("SELECT * FROM categories ORDER BY rowid")]}

    def meta(self):
        with self.connect() as con:
            con.execute("BEGIN")
            return self._meta(con)

    def summary(self, selected_month):
        selected_month = month(selected_month)
        with self.connect() as con:
            con.execute("BEGIN")
            rows = con.execute("SELECT * FROM entries WHERE deleted_at IS NULL" +
                               (" AND substr(date,1,7)=?" if selected_month else ""),
                               [selected_month] if selected_month else []).fetchall()
            colors = dict(con.execute("SELECT name,color FROM categories"))
            result = {"expense_cents": 0, "gross_expense_cents": 0, "income_cents": 0,
                      "refund_cents": 0, "net_cents": 0, "balance_cents": sum(self._balances(con).values()),
                      "category_totals": [], "daily": [], "count": len(rows), "revision": self._revision(con)}
            categories, daily = {}, {}
            for row in rows:
                kind, value = row["type"], row["amount_cents"]
                if kind == "transfer":
                    continue
                day = daily.setdefault(row["date"], {"date": row["date"], "expense_cents": 0,
                                                     "income_cents": 0, "refund_cents": 0})
                if kind == "income":
                    result["income_cents"] += value
                    day["income_cents"] += value
                    continue
                category = categories.setdefault(row["category"], {"name": row["category"], "amount_cents": 0,
                                                                     "color": colors.get(row["category"], "#aa9d89"), "count": 0})
                if kind == "expense":
                    result["gross_expense_cents"] += value
                    day["expense_cents"] += value
                    category["amount_cents"] += value
                    category["count"] += 1
                else:
                    result["refund_cents"] += value
                    day["refund_cents"] += value
                    day["expense_cents"] -= value
                    category["amount_cents"] -= value
            result["expense_cents"] = result["gross_expense_cents"] - result["refund_cents"]
            result["net_cents"] = result["income_cents"] - result["expense_cents"]
            result["category_totals"] = sorted(categories.values(), key=lambda row: row["amount_cents"], reverse=True)
            result["daily"] = sorted(daily.values(), key=lambda row: row["date"])
            return result

    def export(self):
        with self.connect() as con:
            con.execute("BEGIN")
            return {"schema_version": 2, "exported_at": now(), "meta": self._meta(con),
                    "entries": [self._serialize(con, row) | {"idempotency_key": row["idempotency_key"]}
                                for row in con.execute("SELECT * FROM entries ORDER BY date,time,id")]}

    def backup(self, destination):
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    @staticmethod
    def _credential_public(row):
        return {key: row[key] for key in ("id", "name", "role", "created_at", "revoked_at")}

    def create_credential(self, name, role, actor="local-admin"):
        name = string(name, "name", 100)
        if role not in ROLES:
            raise LedgerError("role 必须是 readonly、ai 或 admin")
        credential_id = uuid.uuid4().hex
        token = "ql_" + credential_id + "." + secrets.token_urlsafe(32)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO credentials(id,name,role,token_hash,created_at) VALUES (?,?,?,?,?)",
                        (credential_id, name, role, hashlib.sha256(token.encode()).hexdigest(), now()))
            record = self._credential_public(con.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone())
            self._audit(con, actor, "credential_create", credential_id, after=record)
        return {"credential": record, "token": token}

    def authenticate(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"ql_([a-f0-9]{32})\.[A-Za-z0-9_-]{43}", token):
            raise LedgerError("访问凭证无效或已撤销", 401, "unauthorized")
        with self.connect() as con:
            row = con.execute("SELECT * FROM credentials WHERE id=?", (token[3:35],)).fetchone()
            supplied_hash = hashlib.sha256(token.encode()).hexdigest()
            expected_hash = row["token_hash"] if row else "0" * 64
            matches = hmac.compare_digest(supplied_hash, expected_hash)
            if not matches or row is None or row["revoked_at"]:
                raise LedgerError("访问凭证无效或已撤销", 401, "unauthorized")
            return {key: row[key] for key in ("id", "name", "role")}

    def credentials(self):
        with self.connect() as con:
            return [self._credential_public(row) for row in con.execute("SELECT * FROM credentials ORDER BY created_at,id")]

    def revoke_credential(self, credential_id, actor="local-admin"):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone()
            if row is None:
                raise LedgerError("凭证不存在", 404, "not_found")
            before = self._credential_public(row)
            if row["revoked_at"]:
                return before
            if row["role"] == "admin" and con.execute("SELECT COUNT(*) FROM credentials WHERE role='admin' AND revoked_at IS NULL").fetchone()[0] <= 1:
                raise LedgerError("不能撤销最后一个管理凭证；请先创建替代凭证", 409, "last_admin")
            con.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (now(), credential_id))
            after = self._credential_public(con.execute("SELECT * FROM credentials WHERE id=?", (credential_id,)).fetchone())
            self._audit(con, actor, "credential_revoke", credential_id, before, after)
            return after

    def audit(self, filters):
        limit, offset = pagination(filters)
        entity_id = filters.get("entity_id")
        where, params = (" WHERE entity_id=?", [entity_id]) if entity_id else ("", [])
        with self.connect() as con:
            con.execute("BEGIN")
            total = con.execute("SELECT COUNT(*) FROM audit_events" + where, params).fetchone()[0]
            events = []
            for row in con.execute("SELECT * FROM audit_events" + where + " ORDER BY sequence DESC LIMIT ? OFFSET ?", params + [limit, offset]):
                event = {key: row[key] for key in ("sequence", "at", "actor", "action", "entity_id")}
                event.update(before=json.loads(row["before_json"]) if row["before_json"] else None,
                             after=json.loads(row["after_json"]) if row["after_json"] else None)
                events.append(event)
            return {"events": events, "total": total, "limit": limit, "offset": offset, "revision": self._revision(con),
                    "next_offset": offset + len(events) if offset + len(events) < total else None}

    def save_receipt(self, content, extension, actor="local"):
        """Receipts are immutable. Publish only complete files before referencing them."""
        if extension not in {"png", "jpg", "webp"}:
            raise LedgerError("不支持的图片格式")
        filename = uuid.uuid4().hex + "." + extension
        target = self.receipts_dir / filename
        temporary = self.receipts_dir / ("." + filename + ".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(target)
            with self.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                record = {"filename": filename, "sha256": hashlib.sha256(content).hexdigest(),
                          "size": len(content), "created_at": now(), "created_by": actor}
                con.execute("INSERT INTO receipts(filename,sha256,size,created_at,created_by) VALUES (?,?,?,?,?)", tuple(record.values()))
                self._audit(con, actor, "receipt_upload", filename, after=record)
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return "/api/receipts/" + filename

    def backup_bundle(self, destination):
        """Consistent database snapshot and immutable receipt files with restore checks."""
        destination = Path(destination)
        with tempfile.TemporaryDirectory(prefix="qiao-snapshot-") as temporary:
            snapshot = Path(temporary) / "ledger.sqlite3"
            self.backup(snapshot)
            with closing(sqlite3.connect(snapshot)) as con:
                con.row_factory = sqlite3.Row
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchall():
                    raise LedgerError("数据库快照验证失败", 500, "backup_invalid")
                rows = list(con.execute("SELECT id,type,original_id,receipt_url,deleted_at FROM entries ORDER BY id"))
                required = {row["receipt_url"].rsplit("/", 1)[-1] for row in rows if row["receipt_url"]}
                required.update(row[0] for row in con.execute("SELECT filename FROM receipts"))
                balances = self._balances(con)
                category_totals = {row[0]: row[1] for row in con.execute("SELECT category,SUM(CASE WHEN type='expense' THEN amount_cents ELSE -amount_cents END) FROM entries WHERE deleted_at IS NULL AND type IN ('expense','refund') GROUP BY category ORDER BY category")}
                facts = {"entry_ids": [row["id"] for row in rows], "entry_count": len(rows),
                         "active_entry_count": sum(row["deleted_at"] is None for row in rows),
                         "refund_links": {row["id"]: row["original_id"] for row in rows if row["type"] == "refund"},
                         "account_balances_cents": balances, "category_totals_cents": category_totals,
                         "audit_count": con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]}
            receipts = {path.name: path for path in self.receipts_dir.iterdir()
                        if re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|webp)", path.name) and path.is_file() and not path.is_symlink()}
            if required - receipts.keys():
                raise LedgerError("小票文件缺失，不能生成完整备份", 409, "backup_missing_receipt")
            files = {}
            with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
                for name, path in [("ledger.sqlite3", snapshot)] + [("receipts/" + name, path) for name, path in sorted(receipts.items())]:
                    content = path.read_bytes()
                    files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                    archive.writestr(name, content)
                manifest = {"format": "qiao-ledger-backup", "version": 1, "schema_version": 2,
                            "created_at": now(), "files": files, "validation": facts,
                            "receipt_count": len(receipts)}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
            return manifest
