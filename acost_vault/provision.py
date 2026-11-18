"""Opening a ledger (first data key, first documents) and moving an old backup in. Used by the tools in
tools/vault_setup.py and tools/vault_migrate.py, and by the tests. Never prints a key."""
from . import crypto
from .rules import Ledger, import_documents, ledger_from_snapshot
from .sync import Vault, VaultError

DEFAULT_ACCOUNTS = ("Spending", "Savings")


def set_first_key(transport, ledger_id, master_key):
    """Make the data key, wrap it with the master key and store it. Refused if the ledger already has one."""
    status, state = transport.call("GET", "/api/state")
    if status != 200:
        raise VaultError("无法读取账本状态：HTTP %d" % status, status)
    if state["ledger_id"] != crypto.canonical_uuid(ledger_id):
        raise VaultError("这把钥匙属于另一本账。", 409)
    if state["key_version"]:
        raise VaultError("这本账已经开通过了；用恢复码恢复（tools/vault_setup.py restore），不要重新开通。", 409)
    wrapped = crypto.wrap_key(master_key, crypto.new_key(), ledger_id, 1)
    status, answer = transport.call("PUT", "/api/key", {"wrapped_key": crypto.b64(wrapped), "key_version": 1,
                                                        "expected_key_version": 0})
    if status != 200:
        raise VaultError("保存数据钥匙失败：HTTP %d %s" % (status, answer.get("message", "")), status)


def new_ledger(categories=(), accounts=DEFAULT_ACCOUNTS, book="默认账本"):
    ledger = Ledger()
    ledger.settings["book"] = book
    for order, name in enumerate(accounts, 1):
        ledger.accounts[name] = {"name": name, "opening_balance_cents": 0, "sort_order": order}
    for order, (name, color) in enumerate(categories, 1):
        ledger.categories[name] = {"name": name, "color": color, "sort_order": order, "deleted": False}
    return ledger


def write_ledger(vault, ledger):
    """Append every object of a freshly built ledger to an empty vault ledger."""
    vault.ensure(force=True)
    if vault.data["head"]:
        raise VaultError("这本加密账本里已经有记录了；只能搬进一本空账。", 409)
    docs = import_documents(ledger)
    vault.append(docs, 0)
    return len(docs)


def import_backup(vault, snapshot):
    """An old backup (cloud_admin.py backup) into an empty vault ledger. Returns the built Ledger."""
    ledger = ledger_from_snapshot(snapshot, vault.ledger_id)
    write_ledger(vault, ledger)
    return ledger


def open_vault(transport, ledger_id, master_key, directory=None):
    return Vault(transport, ledger_id, master_key, directory=directory)
