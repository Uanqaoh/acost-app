#!/usr/bin/env python3
"""Build reproducible Linux and macOS source archives from a strict allowlist."""
from __future__ import annotations

import argparse
import gzip
import io
import os
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "AGENT_USAGE.md", "API.md", "CLIENT_SETUP.md", "DISTRIBUTION.md",
    "README.md", "ICOST_IMPORT.md", "BACKUP_PLAN.md", "install-linux.sh", "打开演示.command", "打开Acost.command",
    "配置共享账本.command", "requirements.txt",
    "client_config.py", "ledgerctl.py", "viewer.py", "cloud_admin.py", "icost_import.py",
    "preview_demo.py", "demo_data.py", "ledger.py", "shared_server.py",
    "tools/package.json", "tools/package-lock.json", "tools/build_release.py",
    "supabase/config.toml", "supabase/README.md",
    "supabase/migrations/20261015050342_tenancy.sql",
    "supabase/migrations/20261015050412_finance.sql",
    "supabase/migrations/20261017033549_restrict_auto_rls_execute.sql",
    "supabase/migrations/20261018032335_import_snapshot_safeupdate.sql",
    "supabase/migrations/20261018125214_unknown_time_duplicate_guard.sql",
    "web/index.html", "web/app.js", "web/style.css",
)
DIRS = ("supabase/functions",)
FUNCTION_SUFFIXES = {".ts", ".mjs", ".json"}
EPOCH = (2020, 1, 1, 0, 0, 0)


def included_files() -> list[Path]:
    found: set[Path] = set()
    for name in FILES:
        p = ROOT / name
        if p.is_file() and not p.is_symlink():
            found.add(p)
    for name in DIRS:
        p = ROOT / name
        if p.is_dir() and not p.is_symlink():
            for child in p.rglob("*"):
                rel = child.relative_to(p)
                is_test = any("test" in part.lower() for part in rel.parts)
                if (child.is_file() and not child.is_symlink() and child.suffix in FUNCTION_SUFFIXES
                        and not is_test and not any(part in {"node_modules", "__pycache__", ".temp", ".venv"} for part in child.parts)):
                    found.add(child)
    return sorted(found, key=lambda p: p.relative_to(ROOT).as_posix())


def write_zip(files: list[Path], dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            name = "acost/" + path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(name, EPOCH)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o100755 if os.access(path, os.X_OK) else 0o100644
            info.external_attr = (mode & 0xFFFF) << 16
            z.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def write_tar(files: list[Path], dest: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            name = "acost/" + path.relative_to(ROOT).as_posix()
            info = tarfile.TarInfo(name)
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = 1577836800
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with dest.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(buffer.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list selected files without creating archives")
    ap.add_argument("--output-dir", type=Path, help="destination for release archives")
    args = ap.parse_args()
    files = included_files()
    if args.list:
        print("\n".join(p.relative_to(ROOT).as_posix() for p in files))
        return
    if not args.output_dir:
        ap.error("provide --output-dir or use --list")
    if not files:
        ap.error("allowlist selected no files")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_zip(files, args.output_dir / "acost-macos.zip")
    write_tar(files, args.output_dir / "acost-linux.tar.gz")
    print(f"Wrote {len(files)} files to {args.output_dir}")


if __name__ == "__main__":
    main()
