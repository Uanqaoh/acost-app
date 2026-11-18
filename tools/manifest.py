#!/usr/bin/env python3
"""The files a bookkeeping machine needs, with their SHA-256, so a partial update shows.

  manifest.py --write    regenerate MANIFEST.json (run before committing a change to these files)
  manifest.py            list local files that differ from MANIFEST.json
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
FILES = ("ledgerctl.py", "client_config.py", "QUICK_CARD.md", "AGENT_USAGE.md", "design/template.html",
         "tools/recurring.py", "tools/checkup.py", "tools/record.py", "tools/dashboard_data.py",
         "tools/daily.py", "tools/manifest.py", "tools/categories.py", "categories.json",
         "tools/fix.py", "tools/budget.py", "tools/yearly.py", "tools/costco_watch.py",
         "acost_vault/__init__.py", "acost_vault/crypto.py", "acost_vault/rules.py", "acost_vault/sync.py",
         "acost_vault/backend.py", "acost_vault/provision.py", "acost_vault/admin.py",
         # vault import / export read and write the old backup format with it.
         "cloud_admin.py",
         # Friends' machines: download and update (fetch.py), first setup (install_muse.py).
         "tools/fetch.py", "tools/install_muse.py",
         # The problem inbox: Muse tells Hao what broke (no ledger content).
         "acost_vault/report.py", "tools/report.py",
         # What Muse sends the user once Acost is installed.
         "WELCOME.md")


def current(root=ROOT):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() if (root / name).is_file() else None
            for name in FILES}


def stale(root=ROOT):
    """Names whose local copy is missing or differs from the manifest; None if there is no manifest."""
    try:
        wanted = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        return None
    have = current(root)
    return sorted(name for name, digest in wanted.items() if have.get(name) != digest)


def main(argv=None):
    if (argv or sys.argv[1:]) == ["--write"]:
        (ROOT / "MANIFEST.json").write_text(json.dumps({"files": current()}, indent=1) + "\n", encoding="utf-8")
        return 0
    print(json.dumps({"stale": stale()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
