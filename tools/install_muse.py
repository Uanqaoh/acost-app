#!/usr/bin/env python3
"""Set up Acost on a Muse cloud computer: everything except what only the user may do.

  python3 ~/acost/tools/install_muse.py --invite ACOST-xxxx-xxxx-xxxx-xxxx-xxxx

Safe to run again; it only does what is missing:
  1. ~/acost-venv with the cryptography library (the system Python is left alone)
  2. the two commands ~/acost/bin/ledger-ai (bookkeeping) and ~/acost/bin/ledger-ro (dashboard)
  3. joins with the invite code: the server makes this person's own ledger and two keys,
     saved in ~/.config/acost/ (only this user can read them; never printed)
  4. opens the ledger: makes the master key (~/.config/acost/master.key), the starter accounts and categories
  5. an empty fixed-charges list (~/acost/recurring.json), then checks everything
Then the user must see the recovery code once:  ~/acost/bin/ledger-ai vault recovery-code
Prints one JSON line: ok, done (what it did), next (what to do now).
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HOME = Path.home()
ACOST = HOME / "acost"
VENV = HOME / "acost-venv"
PYTHON = VENV / "bin" / "python"
CONFIG = HOME / ".config" / "acost"
CRYPTOGRAPHY = "cryptography==50.0.2"
WRAPPER = """#!/bin/sh
# Acost {what}. Made by tools/install_muse.py.
export LEDGER_USE_ENV_PROXY=1
exec {python} {acost}/ledgerctl.py --config {config} "$@"
"""


class Stop(Exception):
    pass


def run(*command, stdin=None, env=None):
    done = subprocess.run([str(part) for part in command], capture_output=True, text=True, input=stdin, timeout=600, env=env)
    lines = (done.stdout if done.returncode == 0 else done.stderr).strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {"error": lines[-1] if lines else "没有输出"}
    return done.returncode == 0, result


def main(argv=None):
    parser = argparse.ArgumentParser(description="在 Muse 云电脑上装好 Acost（钥匙和恢复码除外）。")
    parser.add_argument("--invite", help="Hao 发的一次性邀请码；已经加入过就不用")
    parser.add_argument("--book", help="账本名；默认「默认账本」")
    parser.add_argument("--url", help="加密账本地址；默认 Acost 的服务（只在演练时改）")
    args = parser.parse_args(argv)
    done = []
    try:
        if not (ACOST / "ledgerctl.py").is_file():
            raise Stop("先运行 fetch.py 把程序文件下载到 ~/acost。")
        if sys.version_info < (3, 10):
            raise Stop("需要 Python 3.10 或更新的版本。")

        if not PYTHON.exists():
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, capture_output=True, timeout=600)
            done.append("建好 ~/acost-venv")
        if subprocess.run([str(PYTHON), "-c", "import cryptography"], capture_output=True).returncode:
            install = subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", CRYPTOGRAPHY], capture_output=True, text=True, timeout=600)
            if install.returncode:
                raise Stop("装 cryptography 没成功：" + install.stderr.strip()[-300:])
            done.append("装好 " + CRYPTOGRAPHY)

        (ACOST / "bin").mkdir(parents=True, exist_ok=True)
        for name, what, config in (("ledger-ai", "bookkeeping", "ai.json"), ("ledger-ro", "dashboard (read-only)", "readonly.json")):
            path, text = ACOST / "bin" / name, WRAPPER.format(what=what, python=PYTHON, acost=ACOST, config=CONFIG / config)
            if not path.exists() or path.read_text() != text:
                if path.exists():
                    path.rename(path.with_name(name + ".old"))
                path.write_text(text)
                path.chmod(0o700)
                done.append("写好 ~/acost/bin/" + name)
        ai, ro = ACOST / "bin" / "ledger-ai", ACOST / "bin" / "ledger-ro"

        if not (CONFIG / "ai.json").exists():
            if not args.invite:
                raise Stop("还没加入：需要 Hao 发的邀请码，加 --invite 再运行。")
            # Same proxy switch as the wrappers: a Muse VM only reaches the internet through its egress proxy.
            # Its connection sometimes drops for a moment, so a failure to connect is tried again (up to 3 times).
            # If the server did use the code but its answer was lost, the next try says "已经用过" and we say so.
            for attempt in range(3):
                if attempt:
                    time.sleep(3 * attempt)
                ok, result = run(PYTHON, ACOST / "ledgerctl.py", "--config", CONFIG / "ai.json", "vault", "join", "--invite", args.invite,
                                 *(["--url", args.url] if args.url else []), env={**os.environ, "LEDGER_USE_ENV_PROXY": "1"})
                if ok or "无法连接" not in str(result.get("error")):
                    break
            if not ok:
                error = str(result.get("error"))
                if "已经用过" in error and attempt:
                    raise Stop("邀请码已被这次安装用掉，但回复在路上丢了（网络断了一下）：请 Hao 在服务端删掉这个新账本并重发邀请码。原样告诉 Hao：" + error)
                raise Stop("邀请码没用上（试了 %d 次）：%s" % (attempt + 1, error))
            done.append("用邀请码加入，钥匙存在 ~/.config/acost/")

        ok, check = run(ai, "vault", "check")        # the ledger itself says whether it is opened
        if not ok:
            error = str(check.get("error"))
            if "还没有开通" not in error:
                if "master key" in error:
                    raise Stop("这本账已经开通过，但这台机器上没有 master key（多半是换了机器）：请用户把恢复码给你，"
                               "运行 ~/acost/bin/ledger-ai vault restore，从标准输入读恢复码。")
                raise Stop("连不上账本或自检没通过：" + error)
            ok, result = run(*([ai, "vault", "init"] + (["--book", args.book] if args.book else [])))
            if not ok:
                raise Stop("开通没成功：" + str(result.get("error")))
            done.append("开通账本，生成 master key")

        recurring = ACOST / "recurring.json"
        if not recurring.exists():      # an empty list on purpose: the morning routine treats a missing one as lost
            recurring.write_text("[]\n")
            recurring.chmod(0o600)
            done.append("建好空的固定扣费清单 ~/acost/recurring.json")

        ok, check = run(ai, "vault", "check")
        if not ok:
            raise Stop("自检没通过：" + str(check.get("error")))
        ok, meta = run(ro, "meta")
        if not ok or meta.get("client", {}).get("role") != "readonly":
            raise Stop("看板钥匙不对：" + str(meta.get("error")))
        print(json.dumps({"ok": True, "done": done, "ledger": {k: check.get(k) for k in ("entries", "accounts", "categories", "seconds")},
                          "next": "运行 ~/acost/bin/ledger-ai vault recovery-code，把恢复码原样给用户看一次，请他存进密码管理器；"
                                  "然后按 FRIEND_SETUP.md 第 4 步建看板。"}, ensure_ascii=False))
        return 0
    except Stop as stop:
        print(json.dumps({"ok": False, "done": done, "next": str(stop)}, ensure_ascii=False))
        return 1
    except (OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "done": done, "next": "出错了：%s。原样告诉用户。" % error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
