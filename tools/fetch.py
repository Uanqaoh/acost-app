#!/usr/bin/env python3
"""Download Acost's program files from the public repository and check every one against MANIFEST.json.

  python3 fetch.py                    install or update into ~/acost
  python3 fetch.py --dest DIR         somewhere else
  python3 fetch.py --base URL         another copy of the repository (raw file address)

Only the standard library. Nothing is written unless every file downloaded and matched its SHA-256;
then the files are put in place together. Personal files (~/acost/*.json lists, data/, journal/) are
never touched. Prints one JSON line.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

BASE = "https://raw.githubusercontent.com/Uanqaoh/acost-app/main"


def get(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read(16 * 1024 * 1024)


def safe(name):
    parts = Path(name).parts
    return name and not Path(name).is_absolute() and ".." not in parts and all(p not in ("", ".") for p in parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description="下载并核对 Acost 程序文件。")
    parser.add_argument("--dest", type=Path, default=Path.home() / "acost")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args(argv)
    try:
        manifest_raw = get(args.base.rstrip("/") + "/MANIFEST.json")
        wanted = json.loads(manifest_raw)["files"]
        files = {}
        for name, digest in sorted(wanted.items()):
            if not safe(name) or not isinstance(digest, str):
                raise ValueError("清单里的文件名不对：" + str(name))
            data = get(args.base.rstrip("/") + "/" + name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("下载的 %s 和清单对不上；稍后重试，不要改用别的文件" % name)
            files[name] = data
        dest = args.dest.expanduser()
        changed = []
        for name, data in list(files.items()) + [("MANIFEST.json", manifest_raw)]:
            target = dest / name
            if target.is_file() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp = tempfile.mkstemp(prefix=".fetch-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            os.chmod(temp, 0o755 if name.endswith((".py", ".sh")) else 0o644)
            os.replace(temp, target)
            changed.append(name)
        print(json.dumps({"ok": True, "dest": str(dest), "files": len(files), "changed": changed}, ensure_ascii=False))
        return 0
    except Exception as error:  # report, never half-install
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(error).__name__, error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
