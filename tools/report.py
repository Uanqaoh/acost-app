#!/usr/bin/env python3
"""Tell Hao something is wrong: check this machine, then send a short report to Acost's problem inbox.

  python3 ~/acost/tools/report.py problem.json              check, send, print what was sent
  python3 ~/acost/tools/report.py problem.json --dry-run    check and show the report; send nothing
  python3 ~/acost/tools/report.py problem.json --invite ACOST-…   when the install never got a key

problem.json, written by Muse in a few words (no amounts, shop names, account names or other personal details):
  {"step": "record", "summary": "记小票时报错，重跑也一样", "error": "<the error text, as it was shown>"}
step is one of install, record, dashboard, daily, update, other.

What is checked and sent along: Python and system versions, which program files are out of date, whether the
commands, the keys' files and the master key are there (only whether, never their content), whether the server
answers, and the result of `vault check` and of the dashboard key (counts and errors only).
Amounts, entries, names, keys, invite and recovery codes never go: acost_vault/report.py takes them out.
Hao reads the report; he cannot read the ledger, which stays encrypted.
Prints one JSON line: ok, report_id, message, sent (exactly what went).
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from acost_vault import report  # noqa: E402
from acost_vault.sync import Transport, VaultError  # noqa: E402

HOME = Path.home()
CONFIG = HOME / ".config" / "acost"
VENV_PYTHON = HOME / "acost-venv" / "bin" / "python"


def run(command, timeout=120):
    """Run one of the ledger commands; its last JSON line, and whether it worked."""
    # Same proxy switch as the wrappers: a Muse VM only reaches the internet through its egress proxy.
    env = {**os.environ, "LEDGER_USE_ENV_PROXY": os.environ.get("LEDGER_USE_ENV_PROXY", "1")}
    try:
        done = subprocess.run([str(part) for part in command], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return False, {"error": type(error).__name__ + ": " + str(error)}
    lines = (done.stdout if done.returncode == 0 else done.stderr).strip().splitlines()
    try:
        return done.returncode == 0, json.loads(lines[-1]) if lines else {}
    except ValueError:
        return done.returncode == 0, {"error": lines[-1][-300:]}


def service_url(given):
    """--url, else the address this machine joined (only the url field of its config is read), else Acost's service."""
    if given:
        return given
    try:
        return json.loads((CONFIG / "ai.json").read_text(encoding="utf-8"))["url"]
    except (OSError, ValueError, KeyError, TypeError):
        return report.SERVICE_URL


def checks(ai, ro, url):
    found = {"python": platform.python_version(), "system": " ".join((platform.system(), platform.release(), platform.machine()))}
    try:
        import manifest
        found["stale_files"] = manifest.stale(ROOT)
    except Exception as error:       # a broken install is exactly when this runs: never let one check stop the rest
        found["stale_files"] = "error: " + str(error)
    found["have"] = {name: path.exists() for name, path in (
        ("ledger-ai", ai), ("ledger-ro", ro), ("acost-venv", VENV_PYTHON), ("ai.json", CONFIG / "ai.json"),
        ("readonly.json", CONFIG / "readonly.json"), ("master.key", CONFIG / "master.key"),
        ("recurring.json", ROOT / "recurring.json"))}
    if VENV_PYTHON.exists():
        ok, answer = run([VENV_PYTHON, "-c", "import json, cryptography; print(json.dumps({'v': cryptography.__version__}))"])
        found["cryptography"] = answer.get("v") if ok else "missing: " + str(answer.get("error"))
    try:
        link = Transport(url, None, use_env_proxy=os.environ.get("LEDGER_USE_ENV_PROXY", "1") == "1", timeout=15)
        link.READ_TRIES = 2     # it only tells whether the server answers; vault check below tries harder
        status, answer = link.call("GET", "/api/health")
        found["server"] = "ok" if status == 200 and answer.get("ok") else "HTTP %d" % status
    except VaultError as error:
        found["server"] = str(error)
    if ai.exists():
        ok, answer = run([ai, "vault", "check"])
        found["vault_check"] = ({key: answer.get(key) for key in ("role", "entries", "months", "accounts", "categories",
                                                                   "server_calls", "seconds", "cryptography")}
                                if ok else {"error": answer.get("error")})
    if ro.exists():
        ok, answer = run([ro, "meta"])
        found["dashboard_key"] = ({"role": (answer.get("client") or {}).get("role"), "accounts": len(answer.get("accounts") or []),
                                   "categories": len(answer.get("categories") or [])} if ok else {"error": answer.get("error")})
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="自检一下，再把问题报告发给 Hao。")
    parser.add_argument("problem", type=Path, help="JSON：step、summary、error")
    parser.add_argument("--invite", help="还没有钥匙（安装没走到加入那一步）时，用 Hao 发的邀请码")
    parser.add_argument("--ledger", default=str(ROOT / "bin" / "ledger-ai"), help="记账命令；默认 ~/acost/bin/ledger-ai")
    parser.add_argument("--url", help="加密账本地址；默认这台机器加入的那个（只在演练时改）")
    parser.add_argument("--dry-run", action="store_true", help="只自检、显示报告，不发送")
    args = parser.parse_args(argv)
    try:
        problem = json.loads(args.problem.expanduser().read_text(encoding="utf-8"))
        if not isinstance(problem, dict):
            raise ValueError("报告文件必须是 JSON 对象")
        ai = Path(args.ledger).expanduser()
        ro = ai.with_name("ledger-ro")
        url = service_url(args.url)
        body = report.clean({**problem, "checks": {**(problem.get("checks") or {}), **checks(ai, ro, url)}})
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "would_send": body}, ensure_ascii=False))
            return 0
        # Through the bookkeeping command when there is one (it holds the key); otherwise with the invite code.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False)
        try:
            ok, answer = run([ai, "vault", "report", "--file", handle.name]) if ai.exists() else (False, {"error": "还没有 " + str(ai)})
            if not ok and args.invite:      # no key yet, or it no longer works: the invite code is the pass
                ok, answer = run([sys.executable, ROOT / "ledgerctl.py", "vault", "report", "--file", handle.name,
                                  "--invite", args.invite, "--url", url])
            elif not ok and not ai.exists():
                raise ValueError("这台机器还没有钥匙：请向用户要 Hao 发的邀请码，加 --invite 再运行。")
        finally:
            os.unlink(handle.name)
        if not ok:
            raise ValueError("报告没发出去：%s" % answer.get("error"))
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except (OSError, ValueError, VaultError) as error:
        print(json.dumps({"ok": False, "error": report.scrub(str(error)),
                          "next": "把这个结果原样告诉用户，请他直接告诉 Hao。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
