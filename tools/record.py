#!/usr/bin/env python3
"""Record one receipt with one command. The caller only describes the receipt.

  record.py receipt.json --ledger '~/acost/bin/ledger-ai'      (or "-" to read stdin)

receipt.json:
  {
    "merchant": "Costco", "date": "2026-11-10", "time": "15:20",   time only if printed on the receipt
    "account": "Spending", "total_cents": 5234,                    what was actually paid
    "photo": "/path/to/receipt.jpg", "transaction_no": "0042",     both optional
    "tax_cents": 345,                                              all tax lines added up; 0 if none
    "rounding_cents": 0,                                           cash rounding, may be negative
    "items": [
      {"name": "鸡蛋", "amount_cents": 899, "category": "Food-Grocery"},
      {"name": "洗衣液", "amount_cents": 1990, "category": "Shopping-Daily", "taxed": true},
      {"name": "优惠券", "amount_cents": -200, "category": "Shopping-Daily", "taxed": true}
    ]
  }
  Items unreadable, or a spoken amount: leave out "items" and give one "category".
  Costco warehouse receipts: add "code" (the item number printed before the name) to each line, the same
  code on its instant-savings line, and "qty" when one line is several of the same item. The codes go to
  costco_watch.py's list for price adjustments, never to the ledger.
  Income: add "type": "income" with one "category" and no items.

Money moved between the user's own accounts, and money that came back:
  {"type": "transfer", "date": "2026-11-10", "account": "Spending", "to_account": "Savings", "total_cents": 50000}
  {"type": "refund", "date": "2026-11-12", "merchant": "Costco", "total_cents": 1990}
  A refund's date is the day the money came back. The script finds the purchase by merchant
  ("original_date" narrows it down); when several fit it lists them, and "original_id" picks one.

The script uploads the photo, makes the key, splits by category, shares the tax,
records every part and checks what came back. It prints one JSON object:
  status   saved | already_saved | ask | partial | refused
  next     the one thing to do next, in words
What it submitted is kept in ~/acost/journal (or ACOST_JOURNAL_DIR), so running the
same receipt again finishes or repeats the same entries and never adds new ones.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recurring import Ledger, LedgerError, check_date, toronto_now  # noqa: E402
from ledgerctl import check_time  # noqa: E402
import costco_watch  # noqa: E402

FIELDS = {"type", "merchant", "date", "time", "account", "total_cents", "photo", "transaction_no",
          "tax_cents", "rounding_cents", "items", "category", "note", "again",
          "to_account", "original_id", "original_date"}
ONLY = {"transfer": {"type", "date", "time", "account", "to_account", "total_cents", "note", "again"},
        "refund": {"type", "date", "time", "merchant", "total_cents", "original_id", "original_date", "note", "again"}}
NEEDS = {"transfer": ("account", "to_account"), "refund": ()}
TAX_NAME, ROUNDING_NAME = "HST", "现金取整"


class Refused(Exception):
    """Nothing more can be done without the user; the message is what to ask or fix."""


def integer(receipt, field, default=None, positive=False):
    value = receipt.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0):
        raise Refused("%s 必须是整数分%s。" % (field, "，且大于 0" if positive else ""))
    return value


def money(cents):
    return "%sC$%.2f" % ("-" if cents < 0 else "", abs(cents) / 100)


def check(receipt):
    if not isinstance(receipt, dict) or not set(receipt) <= FIELDS:
        raise Refused("小票描述里只能有这些字段：" + ", ".join(sorted(FIELDS)))
    kind = receipt.get("type", "expense")
    if kind not in ("expense", "income", "transfer", "refund"):
        raise Refused("type 只能是 expense、income、transfer 或 refund。")
    if kind in ONLY and not set(receipt) <= ONLY[kind]:
        raise Refused("%s只写这些字段：%s" % ("转账" if kind == "transfer" else "退款", ", ".join(sorted(ONLY[kind]))))
    for field in NEEDS.get(kind, ("merchant", "account")):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            raise Refused("缺少 " + field + "。" + ("问用户用哪个账户。" if field.endswith("account") else ""))
    try:
        check_date(receipt.get("date"))
        if receipt.get("time") is not None:
            check_time(receipt["time"])
        if receipt.get("original_date") is not None:
            check_date(receipt["original_date"])
    except LedgerError as error:
        raise Refused(str(error)) from None
    if not isinstance(receipt.get("again", 2), int) or isinstance(receipt.get("again"), bool) or receipt.get("again", 2) < 2:
        raise Refused("again 是第几次买：2、3……只在用户确认是另一笔时写。")
    total, tax, rounding = (integer(receipt, "total_cents", positive=True), integer(receipt, "tax_cents", 0),
                            integer(receipt, "rounding_cents", 0))
    if kind == "transfer":
        if receipt["account"] == receipt["to_account"]:
            raise Refused("转出和转入不能是同一个账户。")
        return
    if kind == "refund":
        for field in ("original_id", "merchant"):
            if field in receipt and (not isinstance(receipt[field], str) or not receipt[field].strip()):
                raise Refused(field + " 不能为空。")
        if not receipt.get("original_id") and not receipt.get("merchant"):
            raise Refused("退款要写 merchant（原来在哪家买的），或写 original_id。")
        return
    if tax < 0:
        raise Refused("tax_cents 不能是负数。")
    items = receipt.get("items")
    if items is None:
        if not isinstance(receipt.get("category"), str) or not receipt["category"].strip():
            raise Refused("没有 items 时要写一个 category。")
        if tax or rounding:
            raise Refused("没有 items 时不要写 tax_cents 或 rounding_cents，只写 total_cents。")
        return
    if receipt.get("type") == "income" or "category" in receipt:
        raise Refused("收入不写 items；有 items 时分类写在每一项里，不写外面的 category。")
    if not isinstance(items, list) or not items or len(items) > 190:
        raise Refused("items 必须是 1–190 项的数组。")
    for item in items:
        if (not isinstance(item, dict) or not set(item) <= {"name", "amount_cents", "category", "taxed", "code", "qty"}
                or not isinstance(item.get("name"), str) or not item["name"].strip()
                or isinstance(item.get("amount_cents"), bool) or not isinstance(item.get("amount_cents"), int)
                or not isinstance(item.get("category"), str) or not item["category"].strip()
                or not isinstance(item.get("taxed", False), bool)):
            raise Refused("items 每项要有 name、amount_cents（整数分，折扣为负）、category，可加 taxed。"
                          "整单折扣和小费这一版不支持：问用户怎么记。")
        if "code" in item and (not isinstance(item["code"], str) or not costco_watch.CODE.match(item["code"])):
            raise Refused("code 是小票上商品名前面那串 4–8 位数字，写成字符串，如 \"1062199\"；看不清就不写 code。")
        if "qty" in item and (isinstance(item["qty"], bool) or not isinstance(item["qty"], int) or item["qty"] < 1
                              or item["amount_cents"] <= 0 or "code" not in item):
            raise Refused("qty 只写在有 code 的商品行上，是这一行买了几件（整数，至少 1）；折扣行不写 qty。")
    listed = sum(item["amount_cents"] for item in items) + tax + rounding
    if listed != total:
        raise Refused("商品 + 税 + 取整 = %s，小票总额是 %s，差 %s。重新核对小票；看不清就不写 items，只写一个 category。"
                      % (money(listed), money(total), money(total - listed)))


def share(amount, weights):
    """Split amount by weights with the largest-remainder rule, so the parts add up exactly."""
    whole = sum(weights)
    parts = [amount * weight // whole for weight in weights]
    order = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % whole), -weights[i], i))
    for i in order[:amount - sum(parts)]:
        parts[i] += 1
    return parts


def split(receipt):
    """One {category, amount_cents, items} per category; the amounts add up to the total."""
    items = receipt.get("items")
    if items is None:
        return [{"category": receipt["category"], "amount_cents": receipt["total_cents"], "items": None}]
    groups = {}
    for item in items:
        groups.setdefault(item["category"], []).append(item)
    names = list(groups)
    subtotal = [sum(item["amount_cents"] for item in groups[name]) for name in names]
    taxed = [sum(item["amount_cents"] for item in groups[name] if item.get("taxed")) for name in names]
    tax = receipt.get("tax_cents", 0)
    weights = taxed if any(weight > 0 for weight in taxed) else subtotal
    weights = [max(weight, 0) for weight in weights]
    if tax and not any(weights):
        raise Refused("有税但没有可以分摊的商品金额；核对小票。")
    taxes = share(tax, weights) if tax else [0] * len(names)
    parts = []
    for name, base, extra in zip(names, subtotal, taxes):
        lines = [{"name": item["name"].strip(), "amount_cents": item["amount_cents"]} for item in groups[name]]
        if extra:
            lines.append({"name": TAX_NAME, "amount_cents": extra})
        parts.append({"category": name, "amount_cents": base + extra, "items": lines})
    rounding = receipt.get("rounding_cents", 0)
    if rounding:
        largest = max(parts, key=lambda part: part["amount_cents"])
        largest["amount_cents"] += rounding
        largest["items"].append({"name": ROUNDING_NAME, "amount_cents": rounding})
    for part in parts:
        if part["amount_cents"] <= 0:
            raise Refused("分类「%s」的小计不是正数（%s）。折扣可能记错了分类；核对后重试。"
                          % (part["category"], money(part["amount_cents"])))
    return parts


def receipt_key(receipt, photo_hash):
    """Stable for the same purchase however it is photographed or re-read."""
    base = [receipt["date"], str(receipt["total_cents"]), receipt["account"]]
    if receipt.get("transaction_no"):
        base.append("tx" + "".join(str(receipt["transaction_no"]).split()))
    elif receipt.get("time"):
        base.append(receipt["time"])
    elif photo_hash:
        base.append("img" + photo_hash[:32])
    else:
        base.append("m" + "".join(receipt["merchant"].lower().split()))
    if receipt.get("again"):
        base.append("again%s" % receipt["again"])
    return "rcpt:" + ":".join(base)


class Journal:
    """What was submitted for one receipt, so a re-run repeats it exactly."""

    def __init__(self, key):
        self.key = key
        folder = Path(os.environ.get("ACOST_JOURNAL_DIR") or "~/acost/journal").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)


def likely_duplicate(ledger, receipt):
    """Entries the server's same-amount check misses once a receipt is split."""
    merchant = "".join(receipt["merchant"].lower().split())
    same = [entry for entry in ledger.entries(receipt["date"][:7])
            if entry.get("date") == receipt["date"] and entry.get("account") == receipt["account"]
            and entry.get("type") == receipt.get("type", "expense")
            and "".join((entry.get("merchant") or "").lower().split()) == merchant]
    if same and (sum(entry["amount_cents"] for entry in same) == receipt["total_cents"]
                 or any(entry["amount_cents"] == receipt["total_cents"] for entry in same)):
        return same
    return []


def describe(entries):
    return "；".join("%s %s %s（ID %s）" % (e.get("date"), e.get("merchant") or "", money(e.get("amount_cents") or 0), e.get("id"))
                    for e in entries)


def squash(text):
    return "".join((text or "").lower().split())


def again_key(receipt):
    return ":again%s" % receipt["again"] if receipt.get("again") else ""


def same_or_new(receipt, entries, recorded_at):
    """Nothing but date, amount, account and name identifies this one, and it is already in the ledger."""
    return {"ok": False, "status": "ask", "entries": entries,
            "next": "账本里已经有一样的一笔（%s 记的，ID %s）。描述里没有时间、交易号或照片，分不清是同一笔还是又发生了一次。"
                    "问用户：是同一笔就不用再记；确实是另一笔，在描述里加 \"again\": %d 再运行。"
                    % (recorded_at or "之前", "、".join(str(entry["id"]) for entry in entries), (receipt.get("again") or 1) + 1)}


def submit(ledger, journal, args, receipt, not_duplicate):
    """Send a one-entry journal (a transfer or a refund). A second run returns the same entry."""
    payload, saved = journal.data["payloads"][0], journal.data["saved"]
    was_complete = bool(saved)
    if was_complete and not receipt.get("time"):
        return same_or_new(receipt, [{"amount_cents": payload["amount_cents"], "id": saved["entry"]}], journal.data.get("time"))
    ok, result = ledger.run(*args, payload=payload)
    if not ok and result.get("error") == "possible_duplicate":
        ids = [candidate.get("id") for candidate in result.get("candidates") or []]
        if ids and (receipt.get("again") or set(ids) <= set(not_duplicate)):   # the user said it is another one
            extra = [part for found in ids for part in ("--acknowledge-duplicate", found)]
            ok, result = ledger.run(*args, *extra, payload=payload)
        else:
            raise Refused("账本里已经有一样的一笔：%s。是同一笔就不用再记；用户确认是另一笔，在描述里加 \"again\": %d 再运行。"
                          % (describe(result.get("candidates") or []), (receipt.get("again") or 1) + 1))
    if not ok:
        raise Refused("账本没有接受：%s。能改的改好再运行；不明白就如实告诉用户。" % (result.get("message") or result.get("error")))
    entry = result.get("entry") or {}
    if entry.get("deleted_at"):
        raise Refused("这一笔之前记过，后来被删除了（ID %s）。问用户要不要恢复。" % entry.get("id"))
    if entry.get("amount_cents") != payload["amount_cents"] or entry.get("date") != payload["date"]:
        raise Refused("账本返回的内容和提交的不一致（ID %s）。告诉用户。" % entry.get("id"))
    saved["entry"] = entry["id"]
    journal.save()
    return {"ok": True, "status": "already_saved" if was_complete else "saved", "type": payload.get("type", "refund"),
            "date": payload["date"], "entries": [{"amount_cents": payload["amount_cents"], "id": entry["id"]}],
            "total_cents": payload["amount_cents"], "entry": entry}


def start(journal, receipt, payload, **extra):
    """Keep what is about to be submitted, so a re-run sends exactly the same thing."""
    payload["time"] = receipt.get("time") or toronto_now().strftime("%H:%M")
    if isinstance(receipt.get("note"), str) and receipt["note"].strip():
        payload["note"] = receipt["note"].strip()
    journal.data = dict(extra, key=journal.key, time=payload["time"], payloads=[payload], saved={})
    journal.save()


def transfer(ledger, receipt, not_duplicate=()):
    """Money moved between two of the user's own accounts: one entry, neither spending nor income."""
    key = "xfer:%s:%s:%s:%s" % (receipt["date"], receipt["total_cents"], receipt["account"], receipt["to_account"])
    key += (":" + receipt["time"] if receipt.get("time") else "") + again_key(receipt)
    journal = Journal(key)
    if not journal.data.get("saved"):
        accounts = {item.get("name") for item in ledger.need("meta").get("accounts") or []}
        for name in (receipt["account"], receipt["to_account"]):
            if name not in accounts:
                raise Refused("账户「%s」不在账本里（账本里有：%s）。问用户是哪个账户。" % (name, "、".join(sorted(accounts))))
        start(journal, receipt, {"type": "transfer", "amount_cents": receipt["total_cents"], "date": receipt["date"],
                                 "account": receipt["account"], "to_account": receipt["to_account"], "idempotency_key": key})
    result = submit(ledger, journal, ["add", "--json"], receipt, not_duplicate)
    if result["ok"]:
        result.pop("entry")
        result["next"] = ("这笔转账之前已经记过，没有重复记。" if result["status"] == "already_saved" else "已记好。") + (
            "告诉用户：%s → %s %s，转账不算支出也不算收入。然后刷新看板。"
            % (receipt["account"], receipt["to_account"], money(receipt["total_cents"])))
    return result


def refundable(entry):
    return (entry.get("amount_cents") or 0) - (entry.get("refunded_cents") or 0)


def describe_purchases(entries):
    return "；".join("%s %s %s，分类 %s，还能退 %s（ID %s）" % (
        e.get("date"), e.get("merchant") or "", money(e.get("amount_cents") or 0), e.get("category") or "",
        money(refundable(e)), e.get("id")) for e in entries)


def find_purchase(ledger, receipt):
    """The one expense this refund belongs to; otherwise say what to ask the user."""
    if receipt.get("original_id"):
        ok, result = ledger.run("get", receipt["original_id"].strip())
        if not ok:
            raise Refused("账本里没有这笔原支出（ID %s）。核对 ID。" % receipt["original_id"])
        found = [result.get("entry") or {}]
    else:
        found, offset, name = [], 0, squash(receipt["merchant"])
        while offset is not None:
            page = ledger.need("list", "--q", receipt["merchant"].strip(), "--type", "expense",
                               "--limit", "500", "--offset", str(offset))
            found.extend(page.get("entries") or [])
            offset = page.get("next_offset")
        found = [entry for entry in found if name in squash(entry.get("merchant"))
                 and entry.get("date", "") <= receipt["date"]
                 and receipt.get("original_date") in (None, entry.get("date"))]
    found = [entry for entry in found if entry.get("type") == "expense" and not entry.get("deleted_at")]
    fits = [entry for entry in found if refundable(entry) >= receipt["total_cents"]]
    if len(fits) == 1:
        return fits[0]
    if fits:
        raise Refused("有 %d 笔支出都可能是原来那一笔：%s。问用户是哪一笔，在描述里加 \"original_id\" 再运行。"
                      % (len(fits), describe_purchases(fits[:8])))
    if found:
        raise Refused("找到的支出都不够退 %s：%s。一张小票拆成了几笔的话，每一笔各退各的；否则和用户核对金额。"
                      % (money(receipt["total_cents"]), describe_purchases(found[:8])))
    raise Refused("账本里找不到这笔退款对应的支出。问用户原来是哪天、在哪家买的；原来那笔没记过的话，先问用户怎么处理。")


def refund(ledger, receipt, not_duplicate=()):
    """Money that came back for an earlier purchase: linked to it, and it lowers that month's spending."""
    target = "id" + receipt["original_id"].strip() if receipt.get("original_id") else "m" + squash(receipt["merchant"])
    key = "refund:%s:%s:%s" % (receipt["date"], receipt["total_cents"], target)
    key += "".join(":" + receipt[field] for field in ("original_date", "time") if receipt.get(field)) + again_key(receipt)
    journal = Journal(key)
    if not journal.data.get("saved"):
        purchase = find_purchase(ledger, receipt)
        start(journal, receipt, {"amount_cents": receipt["total_cents"], "date": receipt["date"],
                                 "idempotency_key": "refund:%s:%s:%s%s" % (purchase["id"], receipt["date"],
                                                                         receipt["total_cents"], again_key(receipt))},
              purchase={field: purchase.get(field) for field in ("id", "date", "merchant", "category", "amount_cents")})
    purchase = journal.data["purchase"]
    result = submit(ledger, journal, ["refund", purchase["id"], "--json"], receipt, not_duplicate)
    if result["ok"]:
        result.pop("entry")
        result.update(type="refund", original=purchase)
        result["next"] = ("这笔退款之前已经记过，没有重复记。" if result["status"] == "already_saved" else "已记好。") + (
            "告诉用户：退款 %s，冲减的是 %s %s 那笔 %s（分类 %s）。然后刷新看板。"
            % (money(receipt["total_cents"]), purchase["date"], purchase.get("merchant") or "",
               money(purchase["amount_cents"]), purchase.get("category") or ""))
    return result


def record(ledger, receipt, not_duplicate=()):
    check(receipt)
    if receipt.get("type") in ("transfer", "refund"):
        return (transfer if receipt["type"] == "transfer" else refund)(ledger, receipt, not_duplicate)
    photo_hash = None
    if receipt.get("photo"):
        photo = Path(receipt["photo"]).expanduser()
        if not photo.is_file() or photo.stat().st_size > 8 * 1024 * 1024:
            raise Refused("小票照片不存在或超过 8 MB：" + str(photo))
        photo_hash = hashlib.sha256(photo.read_bytes()).hexdigest()
    key = receipt_key(receipt, photo_hash)
    journal = Journal(key)
    done = journal.data.get("saved") or {}
    if not done:  # nothing is in the ledger yet, so the newest description wins
        parts = split(receipt)
        meta = ledger.need("meta")
        known = {item.get("name") for item in meta.get("categories") or []}
        missing = sorted({part["category"] for part in parts} - known)
        if missing:
            raise Refused("分类不在账本里：%s。先问用户：改用已有分类，还是新建。" % "、".join(missing))
        if receipt["account"] not in {item.get("name") for item in meta.get("accounts") or []}:
            raise Refused("账户「%s」不在账本里。问用户用哪个账户。" % receipt["account"])
        if not receipt.get("again"):
            found = likely_duplicate(ledger, receipt)
            if found:
                raise Refused("账本里可能已经有这张小票：%s。是同一笔就不用再记；用户确认是另一笔的话，"
                              "在描述里加 \"again\": 2 再运行。" % describe(found))
        time = receipt.get("time") or journal.data.get("time") or toronto_now().strftime("%H:%M")
        url = journal.data.get("receipt_url") if journal.data.get("photo_hash") == photo_hash else None
        if photo_hash and not url:
            url = ledger.need("upload", str(Path(receipt["photo"]).expanduser())).get("receipt_url")
        payloads = []
        for index, part in enumerate(parts):
            payload = {"type": receipt.get("type", "expense"), "amount_cents": part["amount_cents"],
                       "date": receipt["date"], "time": time, "merchant": receipt["merchant"].strip(),
                       "category": part["category"], "account": receipt["account"],
                       "idempotency_key": key + ":" + part["category"]}
            note = [receipt["note"].strip()] if isinstance(receipt.get("note"), str) and receipt["note"].strip() else []
            if len(parts) > 1:
                note.append("同一张小票拆分 %d/%d" % (index + 1, len(parts)))
            if note:
                payload["note"] = " · ".join(note)
            if part["items"]:
                payload["items"] = part["items"]
            if url:
                payload["receipt_url"] = url
            payloads.append(payload)
        journal.data = {"key": key, "time": time, "photo_hash": photo_hash, "receipt_url": url,
                        "payloads": payloads, "saved": {}}
        journal.save()
    payloads, saved = journal.data["payloads"], journal.data["saved"]
    was_complete = len(saved) == len(payloads)
    if was_complete and not (receipt.get("transaction_no") or receipt.get("time") or photo_hash):
        return same_or_new(receipt, [{"category": payload["category"], "amount_cents": payload["amount_cents"],
                                      "id": saved[payload["category"]]} for payload in payloads], journal.data.get("time"))
    for payload in payloads:
        category = payload["category"]
        args = ["add", "--json"]
        ok, result = ledger.run(*args, payload=payload)
        if not ok and result.get("error") == "possible_duplicate":
            ids = [candidate.get("id") for candidate in result.get("candidates") or []]
            allowed = set(saved.values()) | set(not_duplicate)
            if receipt.get("again"):   # the user already said this is another purchase at the same place
                allowed |= {candidate.get("id") for candidate in result.get("candidates") or []
                            if squash(candidate.get("merchant")) == squash(receipt["merchant"])}
            if ids and set(ids) <= allowed:  # only this receipt's own parts, or ones the user cleared
                for found in ids:
                    args += ["--acknowledge-duplicate", found]
                ok, result = ledger.run(*args, payload=payload)
            else:
                raise Refused("疑似重复，还没记完：%s。逐个核对；是同一笔就不要再记，"
                              "用户确认是另一笔后，加 --not-duplicate ID 原样再运行。"
                              % describe(result.get("candidates") or []), saved)
        if not ok:
            raise Refused("账本没有接受「%s」这一笔：%s。原样再运行一次；还不行就告诉用户。"
                          % (category, result.get("error")), saved)
        entry = result.get("entry") or {}
        if entry.get("deleted_at"):
            raise Refused("这张小票之前记过，后来被删除了（ID %s）。问用户要不要恢复。" % entry.get("id"), saved)
        if entry.get("amount_cents") != payload["amount_cents"] or entry.get("date") != payload["date"]:
            raise Refused("账本返回的内容和提交的不一致（ID %s）。告诉用户。" % entry.get("id"), saved)
        saved[category] = entry["id"]
        journal.save()
    entries = [{"category": payload["category"], "amount_cents": payload["amount_cents"],
                "id": saved[payload["category"]]} for payload in payloads]
    total = sum(entry["amount_cents"] for entry in entries)
    result = {"ok": True, "status": "already_saved" if was_complete else "saved", "merchant": payloads[0]["merchant"],
              "date": payloads[0]["date"], "entries": entries, "total_cents": total,
              "next": ("这张小票之前已经记过，没有重复记。" if was_complete else "已记好。")
                      + "告诉用户共 %d 笔，合计 %s，然后刷新看板。" % (len(entries), money(total))}
    try:   # the ledger is done; the price watch list is extra and never undoes it
        watching = costco_watch.remember(receipt, key)
        if watching:
            result["costco_watch"] = watching
    except (OSError, LedgerError) as error:
        result["costco_watch_error"] = "商品编号没存进退差价对照表：%s。账已经记好，不用重记。" % error
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="一条命令记一笔：小票、收入、转账、退款。上传、拆分、算税、提交、核对都在里面。")
    parser.add_argument("receipt", help="小票描述的 JSON 文件；- 表示从标准输入读")
    parser.add_argument("--ledger", help="账本命令，如 ~/acost/bin/ledger-ai")
    parser.add_argument("--not-duplicate", action="append", default=[], metavar="ID",
                        help="用户确认不是同一笔的疑似重复账单 ID；可重复")
    args = parser.parse_args(argv)
    saved = {}
    try:
        try:
            text = sys.stdin.read() if args.receipt == "-" else Path(args.receipt).expanduser().read_text(encoding="utf-8")
            receipt = json.loads(text)
        except (OSError, ValueError) as error:
            raise Refused("读不了小票描述：" + str(error)) from None
        result = record(Ledger(args.ledger), receipt, args.not_duplicate)
    except Refused as refusal:
        saved = refusal.args[1] if len(refusal.args) > 1 else {}
        result = {"ok": False, "status": "partial" if saved else "refused", "saved_ids": saved,
                  "next": ("记了一半，已记的不会重复。" if saved else "没记。") + refusal.args[0]}
    except LedgerError as error:
        result = {"ok": False, "status": "refused", "next": "没记。连不上账本或账本出错：%s。如实告诉用户，稍后原样再运行。" % error}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
