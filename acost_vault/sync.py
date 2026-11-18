"""Sync with the vault server and keep a local cache of ciphertext.

The cache (~/.cache/acost/<ledger_id>.json) holds exactly what the server sent: ciphertext, sequence
numbers and the wrapped data key, never plaintext. Lose it or break it and it is rebuilt from the newest
snapshot plus the changes after it. Reads use a cache synced in the last FRESH_SECONDS; every write is
checked by the server against the head it was computed on, so a slightly old cache can never write
something wrong: the server refuses, we pull and run the rules again.
"""
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import crypto
from .rules import Ledger

CACHE_FORMAT = 1
FRESH_SECONDS = 30
SNAPSHOT_EVERY = 500
PAGE = 1000


class VaultError(Exception):
    """A user-facing failure; `status` follows HTTP. Never carries a key."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class HeadMoved(Exception):
    pass


def cache_directory():
    override = os.environ.get("ACOST_VAULT_CACHE")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "acost"


def read_master_key(path):
    """64 hex characters in a file only its owner can read."""
    path = Path(path).expanduser()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise VaultError("master key 文件必须属于当前用户，权限 600。", 500)
            text = stream.read(200).strip()
    except FileNotFoundError:
        raise VaultError("找不到 master key 文件；请用恢复码恢复（tools/vault_setup.py restore）。", 500) from None
    except (OSError, UnicodeError):
        raise VaultError("无法安全读取 master key 文件。", 500) from None
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = b""
    if len(key) != crypto.KEY_BYTES:
        raise VaultError("master key 文件内容不对。", 500)
    return key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    """JSON over HTTPS with the ledger key. Same proxy and redirect rules as client_config.Connection."""

    def __init__(self, url, token, *, use_env_proxy=False, timeout=20):
        self.url, self.token, self.use_env_proxy, self.timeout = url.rstrip("/"), token, use_env_proxy, timeout
        self.calls = 0

    def call(self, method, path, body=None, query=None):
        target = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(None if self.use_env_proxy else {}), _NoRedirect())
        self.calls += 1
        try:
            with opener.open(urllib.request.Request(target, data=data, headers=headers, method=method),
                             timeout=self.timeout) as response:
                return response.status, json.loads(response.read(256 * 1024 * 1024) or b"{}")
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise VaultError("服务返回重定向，已拒绝转发凭据。", 502) from None
            try:
                body = json.loads(error.read(65536).decode())
            except (ValueError, UnicodeError, OSError, http.client.HTTPException):
                body = {}
            return error.code, body if isinstance(body, dict) else {}
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
            raise VaultError("无法连接共享账本（%s）。请检查网络与服务状态；不会切换到本地账本。" % _reason(error), 503) from None


def _reason(error):
    """A short, safe description of a network failure: no addresses or credentials, so it can be shown."""
    cause = getattr(error, "reason", None) or error
    text = re.sub(r"\S+://\S+", "<地址>", str(cause))[:120].strip()
    return type(cause).__name__ + (": " + text if text else "")


class Vault:
    """One ledger: its cache, its keys, and the server. Use as a context manager (holds a file lock)."""

    def __init__(self, transport, ledger_id, master_key, *, directory=None, clock=time.time):
        self.transport, self.master_key, self.clock = transport, master_key, clock
        self.ledger_id = crypto.canonical_uuid(ledger_id)
        self.directory = Path(directory or cache_directory())
        self.path = self.directory / (self.ledger_id + ".json")
        self.data = None
        self.fetched = False          # talked to the server during this run
        self._data_key = None
        # Several keys share one cache (Muse's ledger-ai and ledger-ro): who "I" am is kept per key.
        self.who = hashlib.sha256((getattr(transport, "token", None) or "").encode()).hexdigest()[:24]

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = open(self.directory / (self.ledger_id + ".lock"), "a")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        self.data = self._load()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()

    # --- cache file ---------------------------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("format") != CACHE_FORMAT or data.get("ledger_id") != self.ledger_id:
                raise ValueError()
            for field in ("head", "max_head", "key_version", "synced_at", "records", "credentials"):
                data[field]  # noqa: B018 - presence check
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.discard()          # broken: rebuild from the server
            return None

    def discard(self):
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.data = None
        self._data_key = None

    def _save(self):
        descriptor, temp = tempfile.mkstemp(prefix=".cache-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(self.data, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)

    # --- talking to the server ------------------------------------------------------------------
    def _call(self, method, path, body=None, query=None):
        status, answer = self.transport.call(method, path, body, query)
        self.fetched = True
        if status == 401:
            raise VaultError("HTTP 401：访问凭证无效或已撤销", 401)
        return status, answer

    def _expect(self, status, answer, wanted, what):
        if status != wanted:
            message = answer.get("message") if isinstance(answer, dict) else None
            raise VaultError("HTTP %d：%s" % (status, message or what), status if status >= 400 else 502)
        return answer

    def _check_head(self, head):
        if not isinstance(head, int) or head < 0:
            raise VaultError("服务器返回的序号无效。")
        if self.data and head < self.data["max_head"]:
            raise VaultError("服务器上的账本比这台机器见过的旧（序号变小），可能被回滚；先不要记账，联系管理员。", 502)

    def ensure(self, force=False):
        """Make the cache current: rebuild when missing, pull when older than FRESH_SECONDS (or forced)."""
        if self.data is None:
            self._rebuild()
        elif force or self.clock() - self.data["synced_at"] >= FRESH_SECONDS or self.who not in self.data["credentials"]:
            self._pull()

    def _rebuild(self):
        state = self._expect(*self._call("GET", "/api/state"), 200, "无法读取账本状态")
        if state.get("ledger_id") != self.ledger_id:
            raise VaultError("这把钥匙属于另一本账，和配置里的 ledger_id 不一致。", 409)
        if not state.get("wrapped_key"):
            raise VaultError("这本账还没有开通（没有数据钥匙）；先运行 tools/vault_setup.py init。", 409)
        self._check_head(state["head"])
        self.data = {"format": CACHE_FORMAT, "ledger_id": self.ledger_id, "head": 0, "max_head": 0,
                     "key_version": state["key_version"], "wrapped_key": state["wrapped_key"],
                     "credentials": {self.who: state["credential"]}, "synced_at": 0, "snapshot": None, "records": []}
        if state.get("snapshot_upto_seq"):
            snap = self._expect(*self._call("GET", "/api/snapshot"), 200, "无法读取快照")
            self.data["snapshot"] = {"upto_seq": snap["upto_seq"], "ciphertext": snap["ciphertext"]}
            self.data["head"] = self.data["max_head"] = snap["upto_seq"]
        self._pull()

    def _pull(self):
        while True:
            status, page = self._call("GET", "/api/changes", query={"since": self.data["head"], "limit": PAGE})
            if status == 409 and page.get("error") == "head_behind":
                raise VaultError("服务器上的账本比这台机器见过的旧（序号变小），可能被回滚；先不要记账，联系管理员。", 502)
            self._expect(status, page, 200, "无法读取账本变化")
            if page.get("ledger_id") != self.ledger_id:
                raise VaultError("服务器返回了另一本账的数据。", 502)
            self._check_head(page["head"])
            expected = self.data["head"]
            for record in page["records"]:
                expected += 1
                if record.get("seq") != expected:
                    raise VaultError("服务器返回的记录序号不连续，可能有记录被藏起来了。", 502)
                self.data["records"].append({k: record[k] for k in ("seq", "record_id", "version", "ciphertext")})
            self.data["head"] = expected
            self.data["max_head"] = max(self.data["max_head"], page["head"])
            self.data["credentials"][self.who] = page["credential"]
            if page["key_version"] != self.data["key_version"]:
                key = self._expect(*self._call("GET", "/api/key"), 200, "无法读取数据钥匙")
                self.data.update(key_version=key["key_version"], wrapped_key=key["wrapped_key"])
                self._data_key = None
            if page.get("next_since") is None:
                break
        if self.data["head"] != self.data["max_head"]:
            raise VaultError("服务器返回的记录少于它报的序号。", 502)
        self.data["synced_at"] = self.clock()
        self._save()

    # --- decrypting ----------------------------------------------------------------------------
    def data_key(self):
        if self._data_key is None:
            version, key = crypto.unwrap_key(self.master_key, crypto.unb64(self.data["wrapped_key"]), self.ledger_id)
            if version != self.data["key_version"]:
                raise VaultError("数据钥匙的版本对不上。", 502)
            self._data_key = key
        return self._data_key

    @property
    def credential(self):
        return self.data["credentials"][self.who]

    def ledger(self):
        """Decrypt and check everything in the cache into the current state."""
        key, ledger = self.data_key(), Ledger(self.ledger_id)
        seen = {}
        snapshot = self.data.get("snapshot")
        if snapshot:
            doc = crypto.decrypt_snapshot(key, self.ledger_id, snapshot["upto_seq"], crypto.unb64(snapshot["ciphertext"]))
            if doc.get("kind") != "snapshot" or doc.get("upto_seq") != snapshot["upto_seq"]:
                raise VaultError("快照内容和序号对不上。", 502)
            for item in sorted(doc["records"], key=lambda r: r["seq"]):
                if item["seq"] > snapshot["upto_seq"]:
                    raise VaultError("快照里有超出它序号的记录。", 502)
                seen[item["doc"]["id"]] = item["doc"]["version"]
                ledger.apply(item["doc"], item["seq"])
            ledger.head = snapshot["upto_seq"]
        for record in self.data["records"]:
            doc = crypto.decrypt_record(key, self.ledger_id, record["record_id"], record["version"],
                                        crypto.unb64(record["ciphertext"]))
            rid = crypto.canonical_uuid(record["record_id"])
            if doc.get("id") != rid or doc.get("version") != record["version"] or doc.get("schema") != 1:
                raise VaultError("记录内容和编号对不上。", 502)
            if record["version"] != seen.get(rid, 0) + 1:
                raise VaultError("记录的版本号不连续，可能有修改被藏起来了。", 502)
            seen[rid] = record["version"]
            ledger.apply(doc, record["seq"])
        ledger.head = self.data["head"]
        return ledger

    # --- writing --------------------------------------------------------------------------------
    def seal(self, docs):
        key, fingerprint = self.data_key(), crypto.idem_key(self.data_key())
        records = []
        for doc in docs:
            record = {"record_id": doc["id"], "version": doc["version"],
                      "ciphertext": crypto.b64(crypto.encrypt_record(key, self.data["key_version"], self.ledger_id,
                                                                     doc["id"], doc["version"], doc))}
            idem = doc["data"].get("idempotency_key") if doc["kind"] == "entry" else None
            if doc["version"] == 1 and idem:
                record["idem_tag"] = crypto.b64(crypto.idem_tag(fingerprint, idem))
            records.append(record)
        return records

    def append(self, docs, expected_head):
        records = self.seal(docs)
        total = 0
        for start in range(0, len(records), 500):
            batch = records[start:start + 500]
            status, answer = self._call("POST", "/api/append", {"expected_head": expected_head + total, "records": batch})
            if status == 409 and answer.get("error") in ("head_moved", "idem_conflict", "version_gap"):
                if total:
                    raise VaultError("分批写入时账本被别人改了，前面几批已经写进去；请重新运行。", 409)
                raise HeadMoved()
            self._expect(status, answer, 201, "写入失败")
            self._check_head(answer["head"])
            for record, seq in zip(batch, answer["seqs"]):
                self.data["records"].append({"seq": seq, "record_id": record["record_id"], "version": record["version"],
                                             "ciphertext": record["ciphertext"]})
            total += len(batch)
            self.data["head"] = self.data["max_head"] = answer["head"]
        self._save()

    def maybe_snapshot(self, ledger=None, force=False):
        """Upload a snapshot every SNAPSHOT_EVERY records, so a new device downloads one block and a little more."""
        last = (self.data.get("snapshot") or {}).get("upto_seq") or self.data.get("snapshot_uploaded") or 0
        if not force and self.data["head"] - last < SNAPSHOT_EVERY:
            return False
        ledger = ledger or self.ledger()
        doc = {"schema": 1, "kind": "snapshot", "upto_seq": self.data["head"],
               "records": [{"seq": seq, "doc": d} for seq, d in ledger.documents()]}
        blob = crypto.encrypt_snapshot(self.data_key(), self.data["key_version"], self.ledger_id, self.data["head"], doc)
        status, _ = self._call("POST", "/api/snapshot", {"upto_seq": self.data["head"], "ciphertext": crypto.b64(blob)})
        if status in (201, 409):
            self.data["snapshot_uploaded"] = self.data["head"]
            self._save()
        return status == 201

