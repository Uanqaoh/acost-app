#!/usr/bin/env python3
"""Mac loopback viewer: static UI plus a strict read-only HTTPS proxy."""
import argparse
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import urllib.parse
import webbrowser

from client_config import ClientError, Connection, default_config, load_connection, save_connection

ROOT = Path(__file__).resolve().parent
API_ROUTE = re.compile(r"/api/(?:meta|summary|entries(?:/[0-9a-fA-F-]{1,64})?|receipts/[0-9a-f]{32}\.(?:png|jpg|webp))\Z")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, connection, web_root=ROOT / "web"):
        if address[0] != "127.0.0.1":
            raise ClientError("展示程序只允许监听 127.0.0.1。")
        self.connection = connection
        self.web_root = Path(web_root)
        self.role_checked = False
        self.role_lock = threading.Lock()
        super().__init__(address, ViewerHandler)

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.server_address[1])

    def check_role(self):
        if self.role_checked:
            return
        with self.role_lock:
            if self.role_checked:
                return
            raw, _ = self.connection.request("GET", "/api/meta")
            try:
                meta = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ClientError("服务返回了无效账本信息。", 502) from None
            if meta.get("client", {}).get("role") != "readonly":
                raise ClientError("Mac 展示必须使用专属只读凭据，请重新配置。", 403)
            if self.connection.mode == "local" and meta.get("demo") is not True:
                raise ClientError("隔离预览只允许连接明确标记为演示的账本。", 403)
            self.role_checked = True


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "AcostViewer/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Request URLs may contain private search terms; omit them from logs.
        pass

    def send_content(self, content, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, data, status=200):
        self.send_content(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def same_origin(self):
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.origin.removeprefix("http://")]:
            raise ClientError("拒绝非本机展示地址。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise ClientError("不接受跨站请求。", 403)
        if self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
            raise ClientError("不接受跨站请求。", 403)
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            raise ClientError("只读请求不接受请求体。", 400)

    def dispatch(self):
        self.same_origin()
        if self.command not in {"GET", "HEAD"}:
            raise ClientError("Mac 展示只允许查看，不能写入账本。", 405)
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ClientError("请求路径无效。", 400)
        parts = urllib.parse.urlsplit(self.path)
        if parts.scheme or parts.netloc or "%" in parts.path or "\\" in parts.path:
            raise ClientError("请求路径无效。", 400)
        path = parts.path
        if path == "/api/viewer-status":
            return self.send_json({"configured": True, "readonly": True,
                                   "mode": "demo" if self.server.connection.mode == "local" else "shared"})
        if path.startswith("/api/"):
            if not API_ROUTE.fullmatch(path):
                raise ClientError("此接口不在只读展示范围内。", 404)
            self.server.check_role()
            query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise ClientError("不接受重复查询参数。", 400)
            query = {key: values[0] for key, values in query.items()}
            allowed = {"month", "q", "account", "category", "type", "limit", "offset"}
            if set(query) - allowed:
                raise ClientError("查询参数不在只读展示范围内。", 400)
            raw, content_type = self.server.connection.request("GET", path, query=query)
            if path.startswith("/api/receipts/"):
                if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ClientError("小票响应不是支持的图片。", 502)
            else:
                if content_type != "application/json":
                    raise ClientError("共享服务返回了非 JSON 响应。", 502)
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ClientError("共享服务返回了无效 JSON。", 502) from None
                # Defensive redaction against an upstream error reflecting credentials.
                from ledgerctl import redact_token
                raw = json.dumps(redact_token(data, self.server.connection.token), ensure_ascii=False).encode()
            return self.send_content(raw, content_type)
        if path not in STATIC:
            raise ClientError("页面不存在。", 404)
        filename, content_type = STATIC[path]
        return self.send_content((self.server.web_root / filename).read_bytes(), content_type)

    def handle_request(self):
        try:
            self.dispatch()
        except ClientError as error:
            self.send_json({"error": "viewer_error", "message": str(error)}, error.status or 500)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_json({"error": "viewer_error", "message": "展示程序暂时无法完成请求。"}, 500)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request


def configure(path):
    if not sys.stdin.isatty():
        raise ClientError("首次配置需要交互终端；请双击“配置共享账本.command”。")
    print("连接你的个人云端账本。Mac 只使用只读凭据；输入凭据时不会显示。")
    url = input("个人账本 HTTPS 地址：").strip()
    token = getpass.getpass("Mac 专属只读凭据：").strip()
    connection = Connection(url, token)
    raw, _ = connection.request("GET", "/api/meta")
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ClientError("服务返回无效账本信息。") from None
    if meta.get("client", {}).get("role") != "readonly":
        raise ClientError("此凭据不是只读凭据；尚未保存配置。")
    storage = save_connection(path, url, token)
    print("连接已验证，凭据已保存到" + ("macOS 钥匙串。" if storage == "keychain" else "仅当前用户可读的配置文件（600）。"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acost Mac 只读展示")
    parser.add_argument("--config", type=Path, default=default_config("viewer"))
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--allow-loopback", action="store_true", help="仅隔离演示；需 LEDGER_URL、LEDGER_TOKEN 且账本 demo=true")
    options = parser.parse_args(argv)
    try:
        if options.configure or (not options.config.exists() and not options.allow_loopback):
            configure(options.config)
            if options.configure:
                return 0
        if options.allow_loopback:
            connection = Connection(os.environ.get("LEDGER_URL", ""), os.environ.get("LEDGER_TOKEN", ""), "local")
        else:
            connection = load_connection(options.config, allow_environment=False)
            if connection.mode != "shared":
                raise ClientError("正式展示必须配置共享 HTTPS 地址；演示需显式 --allow-loopback。")
        server = ViewerServer(("127.0.0.1", options.port), connection)
        print("Acost 只读展示：" + server.origin, flush=True)
        print("保持此窗口打开；按 Control-C 关闭展示。", flush=True)
        if not options.no_open:
            webbrowser.open(server.origin)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ClientError, OSError) as error:
        message = str(error) if isinstance(error, ClientError) else "无法启动展示程序；请检查端口是否已占用。"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
