"""`ledgerctl.py vault ...`: open a ledger, show or use the recovery code, check it, move an old backup in,
and export back to the old format. Runs wherever ledgerctl runs (Muse's wrappers, or the Mac).

Only `recovery-code` ever prints the recovery code, and nothing prints the master key or a ledger key.
Amounts are not printed either: checks report counts and whether everything matched.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile

from . import crypto, provision
from .backend import default_master_key_path
from .rules import Engine, ENTRY_FIELDS, ledger_from_snapshot
from .sync import Transport, Vault, VaultError, read_master_key

ROOT = Path(__file__).resolve().parents[1]


def master_path(connection):
    return Path(connection.master_key_path or default_master_key_path()).expanduser()


def transport(connection):
    return Transport(connection.url, connection.token, use_env_proxy=connection.use_env_proxy)


def write_private(path, text):
    """Create a 0600 file; never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def starter_categories(path=None):
    catalog = json.loads(Path(path or ROOT / "categories.json").read_text(encoding="utf-8"))
    return [(item["name"], catalog["groups"][item["name"].split("-", 1)[0]]["color"])
            for item in catalog["categories"] if item.get("starter")]


def init(connection, *, empty=False, book="默认账本"):
    """First data key for a new ledger, a new master key file, and (unless empty) the starter accounts and categories."""
    path = master_path(connection)
    if path.exists() or path.is_symlink():
        raise VaultError("master key 文件已经存在（%s）；这台机器已经开通过。换机器用 vault restore。" % path, 409)
    link = transport(connection)
    master = crypto.new_key()
    write_private(path, master.hex() + "\n")
    try:
        provision.set_first_key(link, connection.ledger_id, master)
    except BaseException:
        path.unlink()
        raise
    created = 0
    if not empty:
        with Vault(link, connection.ledger_id, master) as vault:
            created = provision.write_ledger(vault, provision.new_ledger(starter_categories(), book=book))
    return {"ok": True, "ledger_id": crypto.canonical_uuid(connection.ledger_id), "master_key_file": str(path),
            "records": created,
            "next": "运行 vault recovery-code，把恢复码给用户看一次，请他存进密码管理器。没有恢复码，丢了这台机器账就打不开。"}


def recovery_code(connection):
    code = crypto.recovery_code(read_master_key(master_path(connection)))
    return {"recovery_code": code,
            "next": "只给用户看这一次：请他存进密码管理器，不要截图、不要存在别处。不要把它写进任何文件或记忆。"}


def restore(connection, code):
    """The recovery code back into a master key file, after checking it opens this ledger's data key."""
    master = crypto.parse_recovery_code(code)
    status, state = transport(connection).call("GET", "/api/state")
    if status != 200:
        raise VaultError("无法读取账本状态：HTTP %d" % status, status)
    if not state.get("wrapped_key"):
        raise VaultError("这本账还没有开通，没有可以恢复的钥匙。", 409)
    try:
        crypto.unwrap_key(master, crypto.unb64(state["wrapped_key"]), connection.ledger_id)
    except crypto.VaultCryptoError:
        raise VaultError("恢复码打不开这本账：可能抄错了，或者是另一本账的恢复码。", 400) from None
    path = master_path(connection)
    if path.exists():
        if read_master_key(path) == master:
            return {"ok": True, "master_key_file": str(path), "note": "这台机器已经有同一把钥匙。"}
        raise VaultError("这台机器上已经有另一把 master key（%s），没有覆盖。" % path, 409)
    write_private(path, master.hex() + "\n")
    return {"ok": True, "master_key_file": str(path), "next": "运行 vault check 自检。"}


def check(connection, cache_dir=None):
    """Pull everything, decrypt and verify it, and say how long it took. No amounts."""
    started = time.time()
    link = transport(connection)
    status, state = link.call("GET", "/api/state")      # the connection and the key, before anything local
    if status != 200:
        raise VaultError("HTTP %d：%s" % (status, state.get("message", "无法读取账本状态")), status)
    if state.get("ledger_id") != crypto.canonical_uuid(connection.ledger_id):
        raise VaultError("这把钥匙属于另一本账，和 ACOST_LEDGER_ID 不一致。", 409)
    if not state.get("wrapped_key"):
        raise VaultError("这本账还没有开通（连接和钥匙都正常）；下一步运行 vault init。", 409)
    with Vault(link, connection.ledger_id, read_master_key(master_path(connection)), directory=cache_dir) as vault:
        vault.ensure(force=True)
        ledger = vault.ledger()
        role = vault.credential["role"]
        cache = str(vault.path)
    entries = [e for e in ledger.entries.values()]
    return {"ok": True, "ledger_id": crypto.canonical_uuid(connection.ledger_id), "role": role, "head": ledger.head,
            "entries": len(entries), "active_entries": sum(e["deleted_at"] is None for e in entries),
            "months": len({e["date"][:7] for e in entries}), "accounts": len(ledger.accounts),
            "categories": len(ledger.live_categories()), "server_calls": link.calls,
            "seconds": round(time.time() - started, 2), "cache": cache,
            "python_ok": True, "cryptography": __import__("cryptography").__version__}


# --- moving the old ledger in, and back -------------------------------------------------------------

def read_old_backup(path):
    """A cloud_admin.py backup ZIP (checked against its own manifest) or a bare snapshot JSON."""
    from cloud_admin import read_bundle, validate_snapshot
    path = Path(path).expanduser()
    if zipfile.is_zipfile(path):
        return read_bundle(path)[0]
    return validate_snapshot(json.loads(path.read_text(encoding="utf-8")))


def month_totals(entries):
    """Monthly totals worked out straight from backup rows, independently of the rules engine."""
    months = {}
    for e in entries:
        if e.get("deleted_at"):
            continue
        month = months.setdefault(e["date"][:7], {"expense": 0, "income": 0, "refund": 0, "transfer": 0, "count": 0})
        month[e["type"]] += e["amount_cents"]
        month["count"] += 1
    return months


def summary_of(engine_ledger, month):
    status, body, _ = Engine(engine_ledger, {"id": "check", "name": "check", "role": "readonly"}).handle(
        "GET", "/api/summary", {"month": month})
    if status != 200:
        raise VaultError("加密账本算不出 %s 的汇总。" % month, 500)
    body.pop("revision", None)
    return body


def comparable(row):
    row = {k: row.get(k) for k in ENTRY_FIELDS}
    row["receipt_url"] = None              # photos are not moved
    if row["type"] == "transfer":
        row["category"] = ""
    return row


def import_backup(connection, backup_path, *, compare_with=None):
    """Move an old backup into this (empty) encrypted ledger, read it back from the server with a fresh
    cache, and check entry by entry and month by month. compare_with: an old-ledger Connection whose
    live /api/summary must match too (the move day: the old ledger is frozen)."""
    snapshot = read_old_backup(backup_path)
    master = read_master_key(master_path(connection))
    link = transport(connection)
    with Vault(link, connection.ledger_id, master) as vault:
        written = provision.write_ledger(vault, ledger_from_snapshot(snapshot, vault.ledger_id))
    with tempfile.TemporaryDirectory() as temp, Vault(transport(connection), connection.ledger_id, master,
                                                       directory=temp) as fresh:
        fresh.ensure()                               # everything back from the server, decrypted again
        moved = fresh.ledger()
    problems = []
    expected = {e["id"]: comparable(e) for e in snapshot["entries"]}
    found = {k: comparable(v) for k, v in moved.entries.items()}
    if expected != found:
        problems.append("账单逐笔核对不一致：%d 笔不同" % sum(expected.get(k) != found.get(k) for k in set(expected) | set(found)))
    old_accounts = {a["name"]: a["opening_balance_cents"] for a in snapshot["accounts"]}
    if old_accounts != {a["name"]: a["opening_balance_cents"] for a in moved.accounts.values()}:
        problems.append("账户或期初余额不一致")
    if {c["name"]: c["color"] for c in snapshot["categories"]} != {c["name"]: c["color"] for c in moved.live_categories()}:
        problems.append("分类不一致")
    months = sorted(month_totals(snapshot["entries"]))
    straight = month_totals(snapshot["entries"])
    for month in months:
        body = summary_of(moved, month)
        mine = {"expense": body["gross_expense_cents"], "income": body["income_cents"], "refund": body["refund_cents"],
                "count": body["count"]}
        theirs = {k: straight[month][k] for k in ("expense", "income", "refund", "count")}
        if mine != theirs:
            problems.append(month + " 的合计和备份不一致")
        if compare_with is not None:
            old = json.loads(compare_with.request("GET", "/api/summary", query={"month": month})[0])
            old.pop("revision", None)
            if old != body:
                problems.append(month + " 的汇总和旧账本不一致")
    result = {"ok": not problems, "records_written": written, "entries": len(found), "months_checked": len(months),
              "first_month": months[0] if months else None, "last_month": months[-1] if months else None,
              "compared_with_old_ledger": compare_with is not None, "receipt_links_dropped":
                  sum(bool(e.get("receipt_url")) for e in snapshot["entries"]), "problems": problems}
    if problems:
        raise VaultError("搬账核对没通过：" + "；".join(problems) + "。不要切换，保留旧账本。", 500)
    return result


def export_bundle(connection, output):
    """The encrypted ledger back in the old backup format (cloud_admin.py restore can load it into an empty old ledger)."""
    from cloud_admin import write_bundle
    master = read_master_key(master_path(connection))
    with Vault(transport(connection), connection.ledger_id, master) as vault:
        vault.ensure(force=True)
        ledger = vault.ledger()
    engine = Engine(ledger, {"id": "export", "name": "export", "role": "admin"})
    engine.db = ledger
    snapshot = engine.export()

    def no_receipts(name):
        raise VaultError("加密账本没有小票照片。", 500)
    validation = write_bundle(output, snapshot, no_receipts)
    return {"ok": True, "output": str(Path(output).expanduser()), "entries": validation["entry_count"],
            "next": "用 cloud_admin.py --config <旧账本 admin 配置> restore 这个文件，只能恢复进空的旧账本。"}


SERVICE_URL = "https://fptsrkpraxmliefkcquk.supabase.co/functions/v1/vault"


def join(invite, *, url=SERVICE_URL, directory=None, environ=None):
    """Redeem a one-time invite: the server makes this person's empty ledger and two keys, which go straight
    into two private config files (bookkeeping, dashboard). Neither key is printed."""
    from client_config import config_directories, save_connection, validate_url
    environ = os.environ if environ is None else environ
    folder = Path(directory or config_directories()[0]).expanduser()
    paths = {"ai": folder / "ai.json", "readonly": folder / "readonly.json"}
    if any(p.exists() for p in paths.values()):
        raise VaultError("这台机器已经加入过（%s 已存在）；不用再用邀请码。" % folder, 409)
    url = validate_url(url, "shared", "vault")
    link = Transport(url, None, use_env_proxy=environ.get("LEDGER_USE_ENV_PROXY") == "1")
    status, answer = link.call("POST", "/api/redeem", {"invite": invite})
    if status != 201:
        raise VaultError("HTTP %d：%s" % (status, answer.get("message", "邀请码没能用上")), status)
    for role, key in (("ai", answer["ai_key"]), ("readonly", answer["readonly_key"])):
        save_connection(paths[role], url, key, backend="vault", ledger_id=answer["ledger_id"],
                        master_key_path=folder / "master.key")
    answer = None
    return {"ok": True, "ledger_id": crypto.canonical_uuid(json.loads(paths["ai"].read_text())["ledger_id"]),
            "configs": {role: str(path) for role, path in paths.items()},
            "next": "运行 vault init 开通（会生成恢复码）。"}


def run(connection, action, args, stdin):
    if connection.backend != "vault":
        raise VaultError("vault 命令只用于加密版配置（backend: vault）。", 400)
    if action == "init":
        return init(connection, empty=args.empty, book=args.book or "默认账本")
    if action == "recovery-code":
        return recovery_code(connection)
    if action == "restore":
        return restore(connection, stdin.readline().strip())
    if action == "check":
        return check(connection)
    if action == "import":
        if not args.file:
            raise VaultError("import 需要 --file 旧备份（ZIP 或快照 JSON）。", 400)
        compare = None
        if args.compare_with:
            from client_config import load_connection
            compare = load_connection(args.compare_with, allow_environment=False)
        return import_backup(connection, args.file, compare_with=compare)
    if action == "export":
        if not args.output:
            raise VaultError("export 需要 --output 输出 ZIP。", 400)
        return export_bundle(connection, args.output)
    raise VaultError("未知的 vault 命令。", 400)

