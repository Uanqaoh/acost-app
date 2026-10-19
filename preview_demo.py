#!/usr/bin/env python3
"""Start a disposable shared API and readonly Mac viewer for visual acceptance."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from demo_data import create_demo
from ledger import Ledger
from client_config import Connection
from viewer import ViewerServer

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description='启动隔离演示，绝不读取正式账本')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    # Fail before creating any state when the shared runtime is not installed.
    import fastapi, uvicorn
    result = create_demo()
    data_dir = result if isinstance(result, (str, Path)) else result['data_dir']
    ledger = Ledger(data_dir)
    credential = ledger.create_credential('Mac visual preview', 'readonly')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        api_port = sock.getsockname()[1]
    api_url = f'http://127.0.0.1:{api_port}'
    process = subprocess.Popen([sys.executable, str(ROOT / 'shared_server.py'), '--data-dir', str(data_dir), 'serve', '--port', str(api_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('演示服务启动失败')
            try:
                with urllib.request.urlopen(api_url+'/api/health',timeout=1) as response:
                    if response.status==200: break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('演示服务启动超时')
        server = ViewerServer(('127.0.0.1', args.port), Connection(api_url, credential['token'], 'local'))
        server.check_role()
        print('隔离演示已启动：'+server.origin, flush=True)
        print('数据为虚构，每次启动新建演示库；关闭窗口或 Control-C 停止。', flush=True)
        if not args.no_open:
            webbrowser.open(server.origin)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        process.terminate()
        process.wait(timeout=10)

if __name__=='__main__':
    main()
