"""VaultBackend: what ledgerctl.api_request calls when the config says "backend": "vault".

It answers the same routes with the same bodies and errors as the old server (rules.py), so the tools and
Muse use ledgerctl exactly as before. Underneath, it syncs encrypted records (sync.py).
"""
import json
from pathlib import Path

from . import crypto
from .rules import Engine
from .sync import FRESH_SECONDS, HeadMoved, Transport, Vault, VaultError, read_master_key  # noqa: F401

READS = ("GET", "HEAD")
STATE_FREE = (403, 405, 428, 501)   # refusals that do not depend on what is in the ledger
ATTEMPTS = 5
LOST_WRITES = 2     # a write whose answer was lost is checked and, if it did not go in, sent again this often


def default_master_key_path():
    from client_config import config_directories
    return config_directories()[0] / "master.key"


class VaultBackend:
    def __init__(self, connection, *, transport=None, master_key=None, cache_dir=None, clock=None, now=None):
        if not connection.ledger_id:
            from client_config import ClientError
            raise ClientError("加密版配置缺少 ledger_id，请重新运行 tools/vault_setup.py。", 500)
        self.connection = connection
        self.transport = transport or Transport(connection.url, connection.token, use_env_proxy=connection.use_env_proxy)
        self._master_key = master_key
        self.cache_dir, self.clock, self.now = cache_dir, clock, now

    def master_key(self):
        if self._master_key is None:
            self._master_key = read_master_key(Path(self.connection.master_key_path or default_master_key_path()))
        return self._master_key

    def vault(self):
        options = {"directory": self.cache_dir}
        if self.clock:
            options["clock"] = self.clock
        return Vault(self.transport, self.connection.ledger_id, self.master_key(), **options)

    def answer(self, method, path, payload=None, query=None):
        """(status, body) exactly as the old server would answer."""
        method = method.upper()
        # A new entry or refund carries an idempotency key, so after a lost answer the ledger itself can tell
        # whether it went in: pull, run the rules again, and either they return the entry that is already
        # there or the write goes again on the new head. Other writes are not sent twice (an edit that went
        # in would come back as a version conflict); they say plainly that it is not known.
        replayable = method == "POST" and isinstance(payload, dict) and bool(payload.get("idempotency_key"))
        lost = 0
        try:
            with self.vault() as vault:
                vault.ensure()
                for _ in range(ATTEMPTS):
                    ledger = vault.ledger()
                    engine = Engine(ledger, vault.credential, **({"now": self.now} if self.now else {}))
                    status, body, docs = engine.handle(method, path, query, payload)
                    if method in READS:
                        return status, body
                    if not docs:
                        # Nothing to write (a refusal, a replay, a no-op): make sure it was judged on the
                        # server's latest state, not on a cache up to FRESH_SECONDS old.
                        if vault.fetched or status in STATE_FREE:
                            return status, body
                        vault.ensure(force=True)
                        continue
                    try:
                        vault.append(docs, ledger.head)
                    except HeadMoved:
                        vault.ensure(force=True)
                        continue
                    except VaultError as error:
                        if error.status != 503 or len(docs) > 500:
                            raise
                        if not replayable or lost >= LOST_WRITES:
                            raise VaultError(str(error) + "这次写入可能已经进了账本：先查一下再决定要不要重做。", 503) from None
                        lost += 1
                        vault.ensure(force=True)
                        continue
                    try:
                        vault.maybe_snapshot()
                    except (VaultError, crypto.VaultCryptoError):
                        pass        # a snapshot is only a shortcut; the records are written
                    return status, body
                raise VaultError("账本一直在被别的客户端修改，请稍后重试。", 503)
        except crypto.VaultCryptoError as error:
            raise VaultError(str(error), 502) from None

    def request(self, method, path, *, payload=None, query=None, max_bytes=None):
        """Drop-in for client_config.Connection.request: (raw JSON bytes, type), or ClientError like the old server."""
        from client_config import ClientError, duplicate_conflict_details
        try:
            status, body = self.answer(method, path, payload, query)
        except VaultError as error:
            raise ClientError(str(error), error.status) from None
        if status >= 400:
            message = str(body.get("message", "请求失败"))
            details = duplicate_conflict_details(body, self.connection.token) if status == 409 else None
            raise ClientError("HTTP " + str(status) + "：" + message[:1000], status, details=details)
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"
