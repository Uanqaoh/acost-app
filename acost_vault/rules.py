"""The bookkeeping rules, moved off the server: a port of public.qiao_dispatch and the ledger Edge handler.

Input: the current ledger state and one API request (method, path, query, body), exactly as ledgerctl
sends it. Output: (status, body) as the old server would answer, plus the documents to append when the
request changed something. No network and no keys here. The answers must match the recorded checklist in
tests/vault_cases, which was taken from the old server; when the two disagree, the recording wins.

Order matters: each check runs in the same order as in SQL, so a request with several problems gets the
same first error. Comments name the SQL function each part mirrors.
"""
import copy
import datetime as dt
import hashlib
import json
import re
import uuid

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ENTRY_FIELDS = ("id", "type", "amount_cents", "date", "time", "merchant", "note", "category", "account",
                "to_account", "original_id", "receipt_url", "items", "idempotency_key", "request_hash",
                "deleted_at", "created_at", "updated_at", "version", "created_by", "updated_by")
EDITABLE = {"type", "amount_cents", "date", "time", "merchant", "note", "category", "account", "to_account",
            "receipt_url", "items", "allow_new_category"}
DEFAULT_COLOR = "#aa9d89"
MAX_CENTS = 999999999999
MAX_BIGINT = 9223372036854775807
TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
HEX32 = re.compile(r"[a-f0-9]{32}")

# The Edge handler's route table and admin paths (supabase/functions/ledger/handler.mjs), in its order.
ROUTES = [
    (re.compile(r"/api/(health|meta|summary|audit|export|backup)"), ("GET", "HEAD")),
    (re.compile(r"/api/entries"), ("GET", "HEAD", "POST")),
    (re.compile(r"/api/entries/[a-f0-9]{32}"), ("GET", "HEAD", "PATCH", "DELETE")),
    (re.compile(r"/api/entries/[a-f0-9]{32}/(restore|refunds)"), ("POST",)),
    (re.compile(r"/api/receipts"), ("POST",)),
    (re.compile(r"/api/receipts/import"), ("POST",)),
    (re.compile(r"/api/receipts/[a-f0-9]{32}\.(png|jpg|webp)"), ("GET", "HEAD")),
    (re.compile(r"/api/categories"), ("POST",)),
    (re.compile(r"/api/accounts/balance"), ("POST",)),
    (re.compile(r"/api/categories/unused"), ("GET", "HEAD", "DELETE")),
    (re.compile(r"/api/credentials"), ("GET", "HEAD", "POST")),
    (re.compile(r"/api/credentials/[a-f0-9]{32}"), ("DELETE",)),
    (re.compile(r"/api/import"), ("POST",)),
]
ADMIN_ONLY = re.compile(r"/api/(credentials(?:/|$)|categories/unused$|audit$|export$|backup$|import$|receipts/import$)")


class Fail(Exception):
    def __init__(self, message, status=400, code="invalid_request", extra=None):
        super().__init__(message)
        self.status = status
        self.body = {"error": code, "message": message, **(extra or {})}


def fail(message, status=400, code="invalid_request", extra=None):
    raise Fail(message, status, code, extra)


def stamp(now=None):
    """qiao.stamp(): UTC, whole seconds."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def js_number_roundtrip(value):
    """The Edge handler parses JSON in JavaScript and re-serializes it: 2.0 arrives in SQL as 2."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return int(value)
    if isinstance(value, list):
        return [js_number_roundtrip(item) for item in value]
    if isinstance(value, dict):
        return {key: js_number_roundtrip(item) for key, item in value.items()}
    return value


def canonical(value):
    """qiao.canonical(): keys in code-point order, no spaces, UTF-8."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def text_of(value):
    """jsonb ->> : a string as itself, anything else as its JSON text, JSON null as SQL NULL."""
    if value is None:
        return None
    return value if isinstance(value, str) else canonical(value)


def strfield(value, field, max_length, optional=False, present=True):
    """qiao.str(). `present=False` stands for SQL NULL (the key was missing)."""
    if optional and (not present or value is None):
        return ""
    if not present or not isinstance(value, str):
        fail(field + " 必须是字符串")
    result = value.strip(" ")
    if len(result) > max_length or (result == "" and not optional):
        fail(field + " 长度不正确")
    return result


def integer(value, field, minimum, maximum, present=True):
    """qiao.integer()."""
    if not present or not is_int(value):
        fail(field + " 必须是整数")
    if value < minimum or value > maximum:
        fail(field + " 超出允许范围")
    return value


def valid_date(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) or value.startswith("0000"):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def line_items(value, present):
    """qiao.items()."""
    if not present or value is None:
        return None
    if not isinstance(value, list):
        fail("items 必须是商品明细数组")
    if len(value) > 200:
        fail("items 最多 200 项")
    result = []
    for position, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) - {"name", "amount_cents"}:
            fail("items 第 %d 项只能包含 name 和 amount_cents" % position)
        result.append({"name": strfield(item.get("name"), "items.name", 200, present="name" in item),
                       "amount_cents": integer(item.get("amount_cents"), "items.amount_cents", -MAX_CENTS, MAX_CENTS,
                                               present="amount_cents" in item)})
    return result or None


class Ledger:
    """One ledger's current state: what the old tables held, plus each object's record bookkeeping."""

    def __init__(self, ledger_id=None):
        self.ledger_id = ledger_id
        self.settings = {"book": "默认账本", "currency": "CAD", "timezone": "America/Toronto",
                         "demo": False, "opening_balances_initialized": False}
        self.accounts = {}      # name -> {name, opening_balance_cents, sort_order}
        self.categories = {}    # name -> {name, color, sort_order, deleted}
        self.entries = {}       # id -> row (ENTRY_FIELDS)
        self.records = {}       # (kind, key) -> {"record_id", "version", "seq"}
        self.head = 0

    # --- applying decrypted documents (sync) -------------------------------------------------
    def apply(self, doc, seq):
        kind, data = doc.get("kind"), doc.get("data")
        if not isinstance(data, dict):
            raise ValueError("记录内容不完整")
        if kind == "ledger":
            key = "ledger"
            self.settings = {k: data[k] for k in ("book", "currency", "timezone", "demo", "opening_balances_initialized")}
        elif kind == "account":
            key = data["name"]
            self.accounts[key] = {k: data[k] for k in ("name", "opening_balance_cents", "sort_order")}
        elif kind == "category":
            key = data["name"]
            self.categories[key] = {k: data[k] for k in ("name", "color", "sort_order", "deleted")}
        elif kind == "entry":
            key = data["id"]
            if uuid.UUID(hex=key) != uuid.UUID(doc["id"]):
                raise ValueError("账单编号和记录编号不一致")
            self.entries[key] = {k: data.get(k) for k in ENTRY_FIELDS}
        else:
            raise ValueError("不认识的记录种类")
        known = self.records.get((kind, key))
        if known and known["record_id"] != doc["id"]:
            raise ValueError("同一个对象出现了两个记录编号")
        self.records[(kind, key)] = {"record_id": doc["id"], "version": doc["version"], "seq": seq}
        self.head = max(self.head, seq)

    def documents(self):
        """Every object's current document, for a snapshot or an import."""
        for (kind, key), meta in sorted(self.records.items(), key=lambda item: item[1]["seq"]):
            yield meta["seq"], self.document(kind, key, meta["record_id"], meta["version"])

    def document(self, kind, key, record_id, version):
        if kind == "ledger":
            data = dict(self.settings)
        elif kind == "account":
            data = dict(self.accounts[key])
        elif kind == "category":
            data = dict(self.categories[key])
        else:
            data = dict(self.entries[key])
        return {"schema": 1, "kind": kind, "id": record_id, "version": version, "data": data}

    # --- queries -------------------------------------------------------------------------
    def category_exists(self, name):
        category = self.categories.get(name)
        return category is not None and not category["deleted"]

    def live_categories(self):
        return sorted((c for c in self.categories.values() if not c["deleted"]), key=lambda c: (c["sort_order"], c["name"]))

    def next_sort_order(self, table):
        return max((item["sort_order"] for item in table.values()), default=0) + 1

    def refunded(self, entry_id):
        return sum(e["amount_cents"] for e in self.entries.values() if e["original_id"] == entry_id and e["deleted_at"] is None)

    def entry_json(self, entry_id):
        row = self.entries[entry_id]
        result = {k: row[k] for k in ENTRY_FIELDS if k not in ("idempotency_key", "request_hash")}
        result["refunded_cents"] = self.refunded(entry_id) if row["type"] == "expense" else 0
        return result

    def balances(self):
        totals = {}
        for e in self.entries.values():
            if e["deleted_at"] is not None:
                continue
            sign = 1 if e["type"] in ("income", "refund") else -1
            totals[e["account"]] = totals.get(e["account"], 0) + sign * e["amount_cents"]
            if e["type"] == "transfer":
                totals[e["to_account"]] = totals.get(e["to_account"], 0) + e["amount_cents"]
        return [{"id": a["name"], "name": a["name"], "opening_balance_cents": a["opening_balance_cents"],
                 "balance_cents": a["opening_balance_cents"] + totals.get(a["name"], 0)}
                for a in sorted(self.accounts.values(), key=lambda a: (a["sort_order"], a["name"]))]


class Engine:
    """Answers one request the way the old server did. Writes go to a copy of the ledger; only a success
    returns the changed objects as documents, so a refused request changes nothing (SQL's rollback)."""

    def __init__(self, ledger, credential, *, now=None, new_id=None):
        self.base = ledger
        self.credential = credential          # {"id", "name", "role"} of the caller, from the server
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.new_id = new_id or (lambda: uuid.uuid4().hex)

    # --- entry point -----------------------------------------------------------------------
    def handle(self, method, path, query=None, body=None):
        """Return (status, body, documents)."""
        self.db = copy.deepcopy(self.base)
        self.dirty = []
        try:
            status, result = self._handle(method.upper(), path, query or {}, body)
        except Fail as refused:
            return refused.status, refused.body, []
        return status, result, self._documents()

    def touch(self, kind, key):
        if (kind, key) not in self.dirty:
            self.dirty.append((kind, key))

    def _documents(self):
        docs = []
        for kind, key in self.dirty:
            meta = self.db.records.get((kind, key))
            record_id = meta["record_id"] if meta else str(uuid.UUID(hex=key) if kind == "entry" else uuid.uuid4())
            version = meta["version"] + 1 if meta else 1
            docs.append(self.db.document(kind, key, record_id, version))
        return docs

    def stamp(self):
        return stamp(self.now())

    def today_and_now(self):
        zone = self.db.settings.get("timezone") or "America/Toronto"
        local = self.now().astimezone(ZoneInfo(zone))
        return local.strftime("%Y-%m-%d")

    # --- the Edge handler, then the SQL dispatcher ----------------------------------------------
    def _handle(self, method, path, query, body):
        role = self.credential["role"]
        if path == "/api/health" and method in ("GET", "HEAD"):
            return 200, {"ok": True}
        if ADMIN_ONLY.match(path) and role != "admin":
            fail("此操作需要管理凭证", 403, "forbidden")
        if method not in ("GET", "HEAD") and role == "readonly":
            fail("此凭证仅可查看账本", 403, "readonly")
        route = next((methods for pattern, methods in ROUTES if pattern.fullmatch(path)), None)
        if route is None:
            fail("接口不存在", 404, "not_found")
        if method not in route:
            fail("此接口不支持该请求方法", 405, "method_not_allowed")
        if path == "/api/meta":
            return 200, self.meta()
        if path == "/api/backup":
            fail("请运行 cloud_admin.py backup 下载包含账本与全部小票的完整备份", 501, "use_cloud_backup")
        if method in ("GET", "HEAD"):
            payload = {}
        else:
            payload = {} if body is None else js_number_roundtrip(body)
            if not isinstance(payload, dict):
                fail("请求内容必须是 JSON 对象")
        self.apply_version(method, path, payload)
        query = {key: value if isinstance(value, str) else str(value) for key, value in query.items()}
        if method == "HEAD":
            method = "GET"
        return self.dispatch(method, path, query, payload)

    def apply_version(self, method, path, payload):
        """handler.mjs applyVersion (no If-Match header from ledgerctl)."""
        if not re.fullmatch(r"/api/entries/[a-f0-9]{32}(/(restore|refunds))?", path) or method in ("GET", "HEAD"):
            return
        if "expected_version" not in payload and not path.endswith("/refunds"):
            fail("请先读取账单，并提供 expected_version", 428, "version_required")
        if "expected_version" in payload:
            value = payload["expected_version"]
            if not is_int(value) or abs(value) > 2 ** 53 - 1 or value < 1:
                fail("expected_version 必须为正整数")

    def dispatch(self, method, path, query, payload):
        db = self.db
        if path in ("/api/export",) and method == "GET":
            return 200, self.export()
        if path in ("/api/audit", "/api/credentials", "/api/import", "/api/receipts/import") or path.startswith("/api/credentials/"):
            fail("加密版账本不提供这个接口：钥匙在服务器上发（acost_vault.issue_key），搬账用 tools/vault_migrate.py，"
                 "修改历史留在搬账前的备份里。", 501, "vault_unsupported")
        if path == "/api/entries" and method == "GET":
            return 200, self.list_entries(query)
        if path == "/api/summary" and method == "GET":
            month = query.get("month") or None
            if month is not None and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
                fail("month 必须使用 YYYY-MM 格式")
            return 200, self.summary(month)
        if path == "/api/categories/unused":
            used = {e["category"] for e in db.entries.values()}
            names = [c["name"] for c in db.live_categories() if c["name"] not in used]
            if method == "DELETE" and names:
                for name in names:
                    db.categories[name]["deleted"] = True
                    self.touch("category", name)
            return 200, {"categories": names, "deleted": method == "DELETE"}
        if path == "/api/categories" and method == "POST":
            return self.add_category(payload)
        if path == "/api/accounts/balance" and method == "POST":
            return self.set_balance(payload)
        if path == "/api/receipts" and method == "POST":
            # Photos are not kept (Hao, 2026-11-16). record.py carries on without a receipt link.
            return 200, {"receipt_url": None, "stored": False,
                         "message": "加密版账本不保存小票照片；这一笔照常记账，不带小票链接。"}
        if re.fullmatch(r"/api/receipts/[a-f0-9]{32}\.(png|jpg|webp)", path):
            fail("小票不存在", 404, "not_found")
        if path == "/api/entries" or path.endswith("/refunds"):
            return self.create(path, payload)
        return self.entry_route(method, path, query, payload)

    # --- meta, list, summary, export -------------------------------------------------------------
    def meta(self):
        db = self.db
        return {"book": db.settings["book"], "currency": "CAD", "revision": db.head,
                "opening_balances_initialized": bool(db.settings["opening_balances_initialized"]),
                "demo": bool(db.settings["demo"]), "accounts": db.balances(),
                "categories": [{"id": c["name"], "name": c["name"], "color": c["color"]} for c in db.live_categories()],
                "client": dict(self.credential)}

    def list_entries(self, query):
        db = self.db
        limit, offset = query.get("limit", "100"), query.get("offset", "0")
        if not re.fullmatch(r"[0-9]{1,10}", limit) or not re.fullmatch(r"[0-9]{1,10}", offset):
            fail("limit 和 offset 必须为整数")
        if not 1 <= int(limit) <= 500:
            fail("limit 必须在 1 到 500 之间")
        limit, offset = int(limit), int(offset)
        month = query.get("month") or None
        if month is not None and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
            fail("month 必须使用 YYYY-MM 格式")
        kind = query.get("type") or None
        if kind is not None and kind not in ("expense", "income", "transfer", "refund"):
            fail("未知账单类型")
        search = strfield(query.get("q"), "q", 300, optional=True, present="q" in query).lower()
        category, account = query.get("category") or None, query.get("account") or None
        rows = [e for e in db.entries.values() if e["deleted_at"] is None
                and (month is None or e["date"][:7] == month)
                and (category is None or e["category"] == category)
                and (account is None or e["account"] == account)
                and (kind is None or e["type"] == kind)
                and (search == "" or search in e["merchant"].lower() or search in e["note"].lower()
                     or search in e["category"].lower())]
        rows.sort(key=lambda e: (e["date"], e["time"] is not None, e["time"] or "", e["created_at"], e["id"]), reverse=True)
        page = rows[offset:offset + limit]
        total = len(rows)
        return {"entries": [db.entry_json(e["id"]) for e in page], "total": total, "limit": limit, "offset": offset,
                "revision": db.head, "next_offset": offset + len(page) if offset + len(page) < total else None}

    def summary(self, month):
        db = self.db
        selected = [e for e in db.entries.values() if e["deleted_at"] is None and (month is None or e["date"][:7] == month)]
        gross = sum(e["amount_cents"] for e in selected if e["type"] == "expense")
        income = sum(e["amount_cents"] for e in selected if e["type"] == "income")
        refund = sum(e["amount_cents"] for e in selected if e["type"] == "refund")
        cats, days = {}, {}
        for e in selected:
            if e["type"] in ("expense", "refund"):
                category = db.categories.get(e["category"])
                color = category["color"] if category and not category["deleted"] else DEFAULT_COLOR
                row = cats.setdefault((e["category"], color), {"name": e["category"], "color": color, "amount_cents": 0, "count": 0})
                row["amount_cents"] += e["amount_cents"] if e["type"] == "expense" else -e["amount_cents"]
                row["count"] += e["type"] == "expense"
            if e["type"] != "transfer":
                day = days.setdefault(e["date"], {"date": e["date"], "expense_cents": 0, "income_cents": 0, "refund_cents": 0})
                day["expense_cents"] += {"expense": e["amount_cents"], "refund": -e["amount_cents"]}.get(e["type"], 0)
                day["income_cents"] += e["amount_cents"] if e["type"] == "income" else 0
                day["refund_cents"] += e["amount_cents"] if e["type"] == "refund" else 0
        return {"expense_cents": gross - refund, "gross_expense_cents": gross, "income_cents": income,
                "refund_cents": refund, "net_cents": income - gross + refund,
                "balance_cents": sum(a["balance_cents"] for a in db.balances()),
                "category_totals": sorted(cats.values(), key=lambda c: (-c["amount_cents"], c["name"])),
                "daily": [days[d] for d in sorted(days)], "count": len(selected), "revision": db.head}

    def export(self):
        """The old backup snapshot format (schema 3), so the reverse tool can import it into the old ledger."""
        db = self.db
        settings = {"book": db.settings["book"], "currency": "CAD", "schema_version": "3",
                    "demo": "true" if db.settings["demo"] else "false", "timezone": db.settings["timezone"],
                    "opening_balances_initialized": "true" if db.settings["opening_balances_initialized"] else "false"}
        entries = sorted(db.entries.values(), key=lambda e: (e["date"], e["time"] is None, e["time"] or "", e["id"]))
        return {"format": "qiao-ledger-snapshot", "version": 1, "schema_version": 3, "exported_at": self.stamp(),
                "settings": [{"key": k, "value": settings[k]} for k in sorted(settings)],
                "accounts": [dict(a) for a in sorted(db.accounts.values(), key=lambda a: (a["sort_order"], a["name"]))],
                "categories": [{k: c[k] for k in ("name", "color", "sort_order")} for c in db.live_categories()],
                "entries": [dict(e) for e in entries], "receipts": [], "audit_events": []}

    # --- categories and balances ------------------------------------------------------------
    def add_category(self, payload):
        db = self.db
        if set(payload) - {"name", "color"}:
            fail("新增分类只接受 name 和 color")
        name = strfield(payload.get("name"), "category", 100, present="name" in payload)
        color = strfield(payload.get("color"), "color", 7, optional=True, present="color" in payload) or DEFAULT_COLOR
        if not re.fullmatch(r"#[A-Fa-f0-9]{6}", color):
            fail("color 必须是 #RRGGBB")
        if db.category_exists(name):
            c = db.categories[name]
            return 200, {"category": {"name": c["name"], "color": c["color"], "sort_order": c["sort_order"]}, "created": False}
        c = self.new_category(name, color)
        return 201, {"category": {"name": c["name"], "color": c["color"], "sort_order": c["sort_order"]}, "created": True}

    def new_category(self, name, color):
        db = self.db
        db.categories[name] = {"name": name, "color": color, "sort_order": db.next_sort_order(db.categories), "deleted": False}
        self.touch("category", name)
        return db.categories[name]

    def set_balance(self, payload):
        db = self.db
        if set(payload) - {"account", "balance_cents"}:
            fail("设置余额只接受 account 和 balance_cents")
        name = strfield(payload.get("account"), "account", 100, present="account" in payload)
        total = integer(payload.get("balance_cents"), "balance_cents", -MAX_CENTS, MAX_CENTS, present="balance_cents" in payload)
        before = next((a for a in db.balances() if a["name"] == name), None)
        if before is None:
            fail("账户不存在：" + name, 404, "not_found")
        db.accounts[name]["opening_balance_cents"] += total - before["balance_cents"]
        self.touch("account", name)
        if not db.settings["opening_balances_initialized"]:
            db.settings["opening_balances_initialized"] = True
            self.touch("ledger", "ledger")
        return 200, {"account": next(a for a in db.balances() if a["name"] == name)}

    # --- qiao.normalize ------------------------------------------------------------------------
    def normalize(self, value, original=None):
        db = self.db
        if not isinstance(value, dict):
            fail("请求内容必须是 JSON 对象")
        raw_type = value.get("type")
        kind = "expense" if raw_type is None else text_of(raw_type)
        if original is not None:
            kind = "refund"
        elif kind not in ("expense", "income", "transfer"):
            fail("type 必须是 expense、income 或 transfer；退款请使用 refunds 接口")
        if "currency" in value and value["currency"] != "CAD":
            fail("本账本仅支持 CAD，请先把外币转换成加元")
        if "date" in value:
            day = strfield(value["date"], "date", 10)
        else:
            day = self.today_and_now()
        if not valid_date(day):
            fail("date 必须使用有效的 YYYY-MM-DD 日期")
        hhmm = strfield(value["time"], "time", 5) if "time" in value and value["time"] is not None else None
        if hhmm is not None and not TIME.fullmatch(hhmm):
            fail("time 必须使用 HH:MM 格式")
        cents = integer(value.get("amount_cents"), "amount_cents", 1, MAX_CENTS, present="amount_cents" in value)
        account = strfield(value["account"] if "account" in value else "Spending", "account", 100)
        if account not in db.accounts:
            fail("账户不存在：" + account)
        category = strfield(value["category"] if "category" in value else "其他", "category", 100, optional=True)
        merchant = strfield(value.get("merchant"), "merchant", 300, optional=True, present="merchant" in value)
        note = strfield(value.get("note"), "note", 10000, optional=True, present="note" in value)
        destination = None
        if kind == "transfer":
            destination = strfield(value.get("to_account"), "to_account", 100, present="to_account" in value)
            if destination not in db.accounts:
                fail("转入账户不存在")
            if account == destination:
                fail("转出与转入账户不能相同")
            category = ""
        if original is not None:
            account, category, merchant = original["account"], original["category"], original["merchant"]
        if "receipt_url" in value and value["receipt_url"] not in (None, ""):
            strfield(value["receipt_url"], "receipt_url", 150)
            fail("receipt_url 必须引用已上传的小票")   # the encrypted ledger keeps no receipt files
        items = line_items(value.get("items"), "items" in value)
        if category != "" and not db.category_exists(category):
            if value.get("allow_new_category") is not True:
                fail("分类不存在：" + category + "。请使用已有分类；用户同意新建后，带 allow_new_category 重试", 400, "unknown_category")
            self.new_category(category, DEFAULT_COLOR)
        return {"type": kind, "amount_cents": cents, "date": day, "time": hhmm, "merchant": merchant, "note": note,
                "category": category, "account": account, "to_account": destination,
                "original_id": original["id"] if original else None, "receipt_url": None, "items": items}

    def check_version(self, entry, payload, required=True):
        """qiao.check_version()."""
        expected = payload.get("expected_version")
        if expected is None:
            if required:
                fail("请先读取账单，并提供 expected_version", 428, "version_required")
            return
        integer(expected, "expected_version", 1, MAX_BIGINT)
        if entry["version"] != expected:
            fail("账单已被其他客户端修改，请重新读取后核对", 409, "version_conflict")

    def check_duplicates(self, value, acknowledgements, present):
        """qiao.check_duplicates()."""
        if not present:
            acknowledgements = []
        if not isinstance(acknowledgements, list):
            fail("duplicate_acknowledgements 必须是已核对账单 ID 的数组")
        if (len(acknowledgements) > 100 or any(not isinstance(x, str) or not HEX32.fullmatch(x) for x in acknowledgements)
                or len(set(canonical(x) for x in acknowledgements)) != len(acknowledgements)):
            fail("确认清单包含重复、无效或过多账单 ID")
        acknowledged = sorted(acknowledgements)
        matches = sorted((e for e in self.db.entries.values() if e["deleted_at"] is None and e["type"] == value["type"]
                          and e["date"] == value["date"] and e["amount_cents"] == value["amount_cents"]
                          and e["account"] == value["account"] and e["to_account"] == value["to_account"]
                          and e["original_id"] == value["original_id"]), key=lambda e: e["id"])
        preview = matches[:100]
        ids = [e["id"] for e in preview]
        if len(matches) <= 100 and acknowledged == ids:
            return
        fail("发现疑似重复账目，请先核对；同一笔沿用原记录，确认是不同消费后再提交完整确认清单。未保存新账目。", 409, "possible_duplicate", {
            "candidates": [{"id": e["id"], "version": e["version"], "type": e["type"], "date": e["date"], "time": e["time"],
                            "amount_cents": e["amount_cents"], "currency": "CAD", "account": e["account"],
                            "merchant": e["merchant"], "to_account": e["to_account"], "original_id": e["original_id"]}
                           for e in preview],
            "candidate_count": len(matches), "candidates_truncated": len(matches) > 100, "duplicate_acknowledgements": ids})

    # --- POST /api/entries and /refunds ------------------------------------------------------------
    def create(self, path, payload):
        db = self.db
        payload = dict(payload)
        expected = {"expected_version": payload.pop("expected_version")} if "expected_version" in payload else {}
        if path == "/api/entries":
            action, entry_id = "create", None
        else:
            entry_id = path.split("/")[3]
            action = "refund:" + entry_id
        key = strfield(payload.get("idempotency_key"), "idempotency_key", 200, present="idempotency_key" in payload)
        entry_part = {k: v for k, v in payload.items() if k not in ("idempotency_key", "duplicate_acknowledgements", "allow_new_category")}
        digest = hashlib.sha256(canonical({"action": action, "entry": entry_part}).encode("utf-8")).hexdigest()
        existing = next((e for e in db.entries.values() if e["idempotency_key"] == key), None)
        if existing is not None:
            if existing["request_hash"] != digest:
                fail("该 idempotency_key 已用于另一项操作", 409, "idempotency_conflict")
            self.dirty = []
            return 201, {"entry": db.entry_json(existing["id"])}
        original = None
        if entry_id is not None:
            original = db.entries.get(entry_id)
            if original is None or original["deleted_at"] is not None:
                fail("账单不存在", 404, "not_found")
            self.check_version(original, expected, required=False)
            if original["type"] != "expense":
                fail("退款必须关联一笔支出", 409, "refund_conflict")
        normalized = self.normalize(payload, original)
        if original is not None and db.refunded(original["id"]) + normalized["amount_cents"] > original["amount_cents"]:
            fail("累计退款不能超过原支出金额", 409, "refund_conflict")
        self.check_duplicates(normalized, payload.get("duplicate_acknowledgements"), "duplicate_acknowledgements" in payload)
        new_id, now = self.new_id(), self.stamp()
        actor = self.credential["id"]
        row = dict(normalized, id=new_id, idempotency_key=key, request_hash=digest, deleted_at=None, created_at=now,
                   updated_at=now, version=1, created_by=actor, updated_by=actor)
        db.entries[new_id] = {k: row.get(k) for k in ENTRY_FIELDS}
        self.touch("entry", new_id)
        if original is not None:
            self.touch_original(original["id"])
        return 201, {"entry": db.entry_json(new_id)}

    def touch_original(self, entry_id):
        row = self.db.entries[entry_id]
        row.update(version=row["version"] + 1, updated_at=self.stamp(), updated_by=self.credential["id"])
        self.touch("entry", entry_id)

    # --- /api/entries/{id} and /restore ------------------------------------------------------------
    def entry_route(self, method, path, query, payload):
        db = self.db
        parts = path.split("/")
        entry_id, action = parts[3], (parts[4] if len(parts) > 4 else None)
        if (action == "restore" and method != "POST") or (action is None and method not in ("GET", "PATCH", "DELETE")):
            fail("接口不存在", 404, "not_found")
        include = query.get("include_deleted", "false")
        if method == "GET" and include not in ("true", "false"):
            fail("include_deleted 必须是 true 或 false")
        row = db.entries.get(entry_id)
        if row is None or (row["deleted_at"] is not None and (method == "PATCH" or (method == "GET" and include != "true"))):
            fail("账单不存在", 404, "not_found")
        if method == "GET":
            return 200, {"entry": db.entry_json(entry_id)}
        self.check_version(row, payload)
        payload = {k: v for k, v in payload.items() if k != "expected_version"}
        if method == "PATCH":
            if set(payload) - EDITABLE:
                fail("包含不支持修改的字段")
            if row["type"] == "refund":
                fail("退款请先撤销，再重新登记", 409, "refund_conflict")
            merged = {**{k: row[k] for k in ENTRY_FIELDS}, **payload}
            normalized = self.normalize(merged)
            refunded = db.refunded(entry_id)
            if refunded > 0 and (normalized["type"] != "expense" or normalized["amount_cents"] < refunded
                                 or normalized["account"] != row["account"] or normalized["category"] != row["category"]):
                fail("已有退款：不能改变类型、账户、分类或把金额改为小于已退金额", 409, "refund_conflict")
            for field in ("type", "amount_cents", "date", "time", "merchant", "note", "category", "account", "to_account",
                          "receipt_url", "items"):
                row[field] = normalized[field]
            row.update(updated_at=self.stamp(), updated_by=self.credential["id"], version=row["version"] + 1)
            self.touch("entry", entry_id)
            return 200, {"entry": db.entry_json(entry_id)}
        if payload:
            fail("删除或恢复请求只接受 expected_version")
        deleting = method == "DELETE"
        if (deleting and row["deleted_at"] is None) or (action == "restore" and row["deleted_at"] is not None):
            if deleting and row["type"] == "expense" and db.refunded(entry_id) > 0:
                fail("请先撤销相关退款，再删除原支出", 409, "refund_conflict")
            original = None
            if row["type"] == "refund":
                original = db.entries[row["original_id"]]
                if action == "restore":
                    if original["deleted_at"] is not None:
                        fail("原支出不存在", 404, "not_found")
                    if (original["type"] != "expense" or db.refunded(original["id"]) + row["amount_cents"] > original["amount_cents"]
                            or row["account"] != original["account"] or row["category"] != original["category"]):
                        fail("原支出已改变或恢复后会超额，无法恢复退款", 409, "refund_conflict")
            now = self.stamp()
            row.update(deleted_at=now if deleting else None, updated_at=now, updated_by=self.credential["id"],
                       version=row["version"] + 1)
            self.touch("entry", entry_id)
            if original is not None:
                self.touch_original(original["id"])
        result = {"entry": db.entry_json(entry_id)}
        if deleting:
            result["deleted"] = True
        return 200, result


def ledger_from_snapshot(snapshot, ledger_id=None):
    """A Ledger holding an old backup snapshot's contents (for the move and for the checklist seeds).
    Receipt links are dropped (photos are not moved); everything else is kept exactly, including each
    entry's version, idempotency key and request hash, so retries after the move still match."""
    if not isinstance(snapshot, dict) or snapshot.get("format") != "qiao-ledger-snapshot" or snapshot.get("version") != 1 \
            or snapshot.get("schema_version") not in (2, 3):
        raise ValueError("不是认识的旧账本备份")
    for field in ("settings", "accounts", "categories", "entries"):
        if not isinstance(snapshot.get(field), list):
            raise ValueError("备份缺少：" + field)
    ledger = Ledger(ledger_id)
    settings = {item["key"]: item["value"] for item in snapshot["settings"]}
    if settings.get("currency") != "CAD":
        raise ValueError("备份不是 CAD 账本")
    ledger.settings = {"book": settings.get("book") or "默认账本", "currency": "CAD",
                       "timezone": settings.get("timezone") or "America/Toronto",
                       "demo": settings.get("demo") == "true",
                       "opening_balances_initialized": settings.get("opening_balances_initialized") == "true"}
    for item in snapshot["accounts"]:
        ledger.accounts[item["name"]] = {"name": item["name"], "opening_balance_cents": item["opening_balance_cents"],
                                         "sort_order": item["sort_order"]}
    for item in snapshot["categories"]:
        ledger.categories[item["name"]] = {"name": item["name"], "color": item["color"], "sort_order": item["sort_order"],
                                           "deleted": False}
    for item in snapshot["entries"]:
        row = {k: item.get(k) for k in ENTRY_FIELDS}
        row["receipt_url"] = None
        if row["type"] == "transfer":
            row["category"] = ""
        ledger.entries[row["id"]] = row
    return ledger


def import_documents(ledger):
    """Version-1 documents for every object of a freshly built Ledger, in a stable order."""
    docs = [("ledger", "ledger")]
    docs += [("account", a["name"]) for a in sorted(ledger.accounts.values(), key=lambda a: a["sort_order"])]
    docs += [("category", c["name"]) for c in sorted(ledger.categories.values(), key=lambda c: c["sort_order"])]
    # Originals before their refunds, then by date, so a partial read is never inconsistent.
    docs += [("entry", e["id"]) for e in sorted(ledger.entries.values(),
                                                  key=lambda e: (e["type"] == "refund", e["date"], e["id"]))]
    result = []
    for kind, key in docs:
        record_id = str(uuid.UUID(hex=key)) if kind == "entry" else str(uuid.uuid4())
        result.append(ledger.document(kind, key, record_id, 1))
    return result
