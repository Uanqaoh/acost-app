#!/usr/bin/env python3
"""Single-instance shared ledger API; serve behind the host's HTTPS ingress."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers

from ledger import Ledger, LedgerError


MAX_BODY = 12 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
RECEIPT_NAME = re.compile(r"[a-f0-9]{32}\.(?:png|jpg|webp)")
ADMIN_PATHS = {"/api/credentials", "/api/audit", "/api/export", "/api/backup"}
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def error_response(error):
    return JSONResponse({"error": error.code, "message": str(error)}, status_code=error.status,
                        headers=SECURITY_HEADERS | ({"WWW-Authenticate": "Bearer"} if error.status == 401 else {}))


class BoundaryMiddleware:
    """Check every route before parsing requests, and bound streamed request bodies."""

    def __init__(self, app, ledger):
        self.app, self.ledger = app, ledger

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path, method = scope["path"], scope["method"]
        try:
            if "\x00" in path or "\\" in path or ".." in path.split("/"):
                raise LedgerError("无效路径")
            if path == "/api/health" and method in {"GET", "HEAD"}:
                pass
            else:
                # The shared API is used by server-side clients, never browser scripts.
                if headers.get("origin") or headers.get("sec-fetch-site") == "cross-site":
                    raise LedgerError("共享接口不接受浏览器跨站请求", 403, "forbidden")
                authorization = headers.get("authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
                principal = await run_in_threadpool(self.ledger.authenticate, token)
                scope.setdefault("state", {})["client"] = principal
                is_admin_path = path in ADMIN_PATHS or path.startswith("/api/credentials/")
                if is_admin_path and principal["role"] != "admin":
                    raise LedgerError("此操作需要管理凭证", 403, "forbidden")
                if method not in {"GET", "HEAD"} and principal["role"] == "readonly":
                    raise LedgerError("此凭证仅可查看账本", 403, "readonly")
                if method in {"POST", "PATCH", "DELETE"}:
                    length = headers.get("content-length")
                    if length is not None:
                        try:
                            length = int(length)
                        except ValueError:
                            raise LedgerError("Content-Length 无效")
                        if length < 0 or length > MAX_BODY:
                            raise LedgerError("请求最大 12 MB", 413, "payload_too_large")
                    body = bytearray()
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > MAX_BODY:
                            raise LedgerError("请求最大 12 MB", 413, "payload_too_large")
                        if not message.get("more_body", False):
                            break
                    if body:
                        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                            raise LedgerError("Content-Type 必须为 application/json", 415, "unsupported_media_type")
                        try:
                            payload = json.loads(body)
                        except (ValueError, UnicodeError):
                            raise LedgerError("无法解析 JSON")
                        if not isinstance(payload, dict):
                            raise LedgerError("请求内容必须是 JSON 对象")
                    else:
                        payload = {}
                    scope["state"]["payload"] = payload
        except LedgerError as error:
            return await error_response(error)(scope, receive, send)

        async def protected_send(message):
            if message["type"] == "http.response.start":
                existing = [(key, value) for key, value in message.get("headers", [])
                            if key.lower() not in {name.lower().encode() for name in SECURITY_HEADERS}]
                message["headers"] = existing + [(key.lower().encode(), value.encode()) for key, value in SECURITY_HEADERS.items()]
            await send(message)

        await self.app(scope, receive, protected_send)


def mutation(request, required=True):
    payload = dict(request.state.payload)
    expected_version = payload.pop("expected_version", None)
    header = request.headers.get("if-match")
    if header is not None:
        if not re.fullmatch(r'"?[1-9]\d*"?', header):
            raise LedgerError("If-Match 必须是账单版本号")
        header_version = int(header.strip('"'))
        if expected_version is not None and expected_version != header_version:
            raise LedgerError("expected_version 与 If-Match 不一致")
        expected_version = header_version
    if expected_version is None and required:
        raise LedgerError("请先读取账单，并提供 expected_version", 428, "version_required")
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise LedgerError("expected_version 必须为正整数")
    return payload, expected_version


def create_app(data_dir=None):
    configured_dir = data_dir or os.environ.get("LEDGER_DATA_DIR")
    if not configured_dir:
        raise RuntimeError("必须显式设置 LEDGER_DATA_DIR，指向唯一的持久账本目录")
    ledger = Ledger(configured_dir)
    app = FastAPI(title="Acost 共享接口", version="2.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ledger = ledger
    app.add_middleware(BoundaryMiddleware, ledger=ledger)

    @app.exception_handler(LedgerError)
    async def ledger_error(request, error):
        return error_response(error)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        # Avoid reflecting exception strings that could contain filesystem/data details.
        return error_response(LedgerError("服务器暂时无法处理请求", 500, "internal_error"))

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    def health():
        try:
            with ledger.connect() as con:
                con.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
            return {"ok": True}
        except sqlite3.Error:
            return JSONResponse({"ok": False}, status_code=503)

    @app.get("/api/meta")
    def meta(request: Request):
        return ledger.meta() | {"client": request.state.client}

    @app.get("/api/entries")
    def entries(request: Request):
        return ledger.entries(dict(request.query_params))

    @app.get("/api/entries/{entry_id}")
    def entry(entry_id: str, request: Request):
        flag = request.query_params.get("include_deleted", "false")
        if flag not in {"true", "false"}:
            raise LedgerError("include_deleted 必须是 true 或 false")
        return {"entry": ledger.entry(entry_id, include_deleted=flag == "true")}

    @app.get("/api/summary")
    def summary(request: Request):
        return ledger.summary(request.query_params.get("month"))

    @app.post("/api/entries", status_code=201)
    def create(request: Request):
        return {"entry": ledger.create(request.state.payload, actor=request.state.client["id"])}

    @app.patch("/api/entries/{entry_id}")
    def edit(entry_id: str, request: Request):
        payload, version = mutation(request)
        return {"entry": ledger.edit(entry_id, payload, actor=request.state.client["id"], expected_version=version)}

    @app.delete("/api/entries/{entry_id}")
    def delete(entry_id: str, request: Request):
        payload, version = mutation(request)
        if payload:
            raise LedgerError("删除请求只接受 expected_version")
        return {"deleted": True, "entry": ledger.delete(entry_id, actor=request.state.client["id"], expected_version=version)}

    @app.post("/api/entries/{entry_id}/restore")
    def restore(entry_id: str, request: Request):
        payload, version = mutation(request)
        if payload:
            raise LedgerError("恢复请求只接受 expected_version")
        return {"entry": ledger.restore(entry_id, actor=request.state.client["id"], expected_version=version)}

    @app.post("/api/entries/{entry_id}/refunds", status_code=201)
    def refund(entry_id: str, request: Request):
        payload, version = mutation(request, required=False)
        return {"entry": ledger.refund(entry_id, payload, actor=request.state.client["id"], expected_version=version)}

    @app.post("/api/receipts", status_code=201)
    def upload(request: Request):
        payload = request.state.payload
        encoded = payload.get("data_base64")
        if not isinstance(payload.get("filename"), str) or not isinstance(encoded, str):
            raise LedgerError("需要 filename 和 data_base64")
        if len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
            raise LedgerError("小票图片最大 8 MB", 413, "payload_too_large")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise LedgerError("图片编码不是有效 Base64")
        if not content or len(content) > MAX_IMAGE:
            raise LedgerError("小票图片为空或超过 8 MB", 413, "payload_too_large")
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif content.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            extension = "webp"
        else:
            raise LedgerError("仅支持 PNG、JPEG 或 WebP 图片")
        return {"receipt_url": ledger.save_receipt(content, extension, actor=request.state.client["id"])}

    @app.get("/api/receipts/{filename}")
    def receipt(filename: str):
        if not RECEIPT_NAME.fullmatch(filename):
            raise LedgerError("小票不存在", 404, "not_found")
        target = ledger.receipts_dir / filename
        if not target.is_file() or target.is_symlink():
            raise LedgerError("小票不存在", 404, "not_found")
        return FileResponse(target, media_type={"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[filename.rsplit(".", 1)[-1]])

    @app.get("/api/credentials")
    def credentials():
        return {"credentials": ledger.credentials()}

    @app.post("/api/credentials", status_code=201)
    def credential_create(request: Request):
        return ledger.create_credential(request.state.payload.get("name"), request.state.payload.get("role"), request.state.client["id"])

    @app.delete("/api/credentials/{credential_id}")
    def credential_revoke(credential_id: str, request: Request):
        return {"credential": ledger.revoke_credential(credential_id, request.state.client["id"])}

    @app.get("/api/audit")
    def audit(request: Request):
        return ledger.audit(dict(request.query_params))

    @app.get("/api/export")
    def export():
        return JSONResponse(ledger.export(), headers={"Content-Disposition": 'attachment; filename="acost.json"'})

    @app.get("/api/backup")
    def backup():
        directory = Path(tempfile.mkdtemp(prefix="qiao-backup-"))
        target = directory / "qiao-ledger-backup.zip"
        try:
            ledger.backup_bundle(target)
        except Exception:
            shutil.rmtree(directory)
            raise
        return FileResponse(target, media_type="application/zip", filename=target.name,
                            background=BackgroundTask(shutil.rmtree, directory))

    return app


def inspect_bundle(path, extract_to=None):
    """Validate hashes and financial migration facts before an empty-dir restore."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("备份内容重复或缺少清单")
        if len(names) > 100_000 or sum(info.file_size for info in archive.infolist()) > 2 * 1024 ** 3:
            raise ValueError("备份超过离线恢复上限")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "qiao-ledger-backup" or manifest.get("version") != 1:
            raise ValueError("未知备份格式")
        files = manifest.get("files", {})
        if set(names) != set(files) | {"manifest.json"} or "ledger.sqlite3" not in files:
            raise ValueError("备份文件与清单不一致")
        with tempfile.TemporaryDirectory(prefix="qiao-verify-") as temporary:
            extracted = Path(temporary)
            for name, expected in files.items():
                if name != "ledger.sqlite3" and not re.fullmatch(r"receipts/[a-f0-9]{32}\.(png|jpg|webp)", name):
                    raise ValueError("备份路径无效")
                content = archive.read(name)
                if len(content) != expected["size"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                    raise ValueError("备份文件校验失败")
                destination = extracted / name
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(content)
            restored = Ledger(extracted)
            with restored.connect() as con:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("备份数据库完整性验证失败")
            # Recompute the manifest from the restored files, not from archive claims.
            check = extracted / "verification.zip"
            actual = restored.backup_bundle(check)
            if actual["validation"] != manifest.get("validation") or actual["receipt_count"] != manifest.get("receipt_count"):
                raise ValueError("备份账单、余额、分类、小票或退款关联核对不一致")
            if extract_to is not None:
                target = Path(extract_to).expanduser().resolve()
                if target.exists():
                    raise ValueError("恢复目标必须是尚不存在的新目录，不能覆盖现有账本")
                target.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".qiao-restore-", dir=target.parent))
                try:
                    for name in files:
                        destination = staging / name
                        destination.parent.mkdir(exist_ok=True)
                        shutil.copy2(extracted / name, destination)
                    (staging / "receipts").mkdir(exist_ok=True)
                    staging.rename(target)
                except Exception:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Acost 共享服务与离线管理")
    parser.add_argument("--data-dir", default=os.environ.get("LEDGER_DATA_DIR"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    credential = commands.add_parser("credential-create", help="将新凭证写入仅当前用户可读的文件，不在终端输出密钥")
    credential.add_argument("--name", required=True)
    credential.add_argument("--role", choices=["readonly", "ai", "admin"], required=True)
    credential.add_argument("--output", required=True)
    revoke = commands.add_parser("credential-revoke")
    revoke.add_argument("credential_id")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True)
    verify = commands.add_parser("verify-backup")
    verify.add_argument("archive")
    restore = commands.add_parser("restore-backup")
    restore.add_argument("archive")
    restore.add_argument("--destination", required=True)
    options = parser.parse_args()
    try:
        if options.command in {"verify-backup", "restore-backup"}:
            manifest = inspect_bundle(options.archive, options.destination if options.command == "restore-backup" else None)
            print(json.dumps({"ok": True, "validation": manifest["validation"], "receipt_count": manifest["receipt_count"]}, ensure_ascii=False))
            return
        if not options.data_dir:
            parser.error("请提供 --data-dir 或 LEDGER_DATA_DIR")
        if options.command == "serve":
            import uvicorn
            # One persistent SQLite writer service, no access logs with private searches.
            uvicorn.run(create_app(options.data_dir), host=options.host, port=options.port, workers=1,
                        access_log=False, proxy_headers=False, limit_concurrency=100, timeout_keep_alive=5)
            return
        ledger = Ledger(options.data_dir)
        if options.command == "credential-create":
            target = Path(options.output).expanduser().resolve()
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w") as output:
                    result = ledger.create_credential(options.name, options.role)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                print(json.dumps({"credential": result["credential"], "saved_to": str(target)}, ensure_ascii=False))
            except Exception:
                target.unlink(missing_ok=True)
                raise
        elif options.command == "credential-revoke":
            print(json.dumps({"credential": ledger.revoke_credential(options.credential_id)}, ensure_ascii=False))
        elif options.command == "backup":
            manifest = ledger.backup_bundle(options.output)
            print(json.dumps({"ok": True, "receipt_count": manifest["receipt_count"], "validation": manifest["validation"]}, ensure_ascii=False))
    except (LedgerError, ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
