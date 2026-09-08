"""Prepare a checked restoration patch; never restore credentials or business data.

One-time recovery helper. --backup snapshots the current source and SQLite data;
--patch prints an apply_patch patch of only the damaged application files.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "backups/pod-shop-vor-pod-bereinigung-20260908.zip"
RESTORE = {
    "aliexpress_mcp/__init__.py", "aliexpress_mcp/client.py", "aliexpress_mcp/server.py",
    "ebay_mcp/__init__.py", "ebay_mcp/client.py", "ebay_mcp/server.py",
    "app/config.py", "app/database.py", "app/main.py", "app/models.py",
    "app/scheduler.py", "app/static/index.html", "app/studio/__init__.py",
    "app/studio/guard.py", "app/studio/models.py", "app/studio/router.py",
    "app/studio/schemas.py", "app/studio/service.py",
    "app/studio/printify/__init__.py", "app/studio/printify/products.py",
    "app/studio/printify/service.py", "app/studio/radar/beschreibung.py",
    "app/studio/radar/ernte.py",
    "app/integrations/aliexpress_api.py", "app/integrations/aliexpress_store.py",
    "app/integrations/aliexpress.py", "app/integrations/autods.py",
    "app/integrations/ebay.py", "app/integrations/native_listing.py",
}


def backup() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = ROOT / "backups" / ("vor-dashboard-wiederherstellung-" + stamp)
    destination.mkdir()
    with zipfile.ZipFile(destination / "quellcode.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for directory in ("app", "scripts", "tests"):
            for path in (ROOT / directory).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    z.write(path, path.relative_to(ROOT))
        for name in ("requirements.txt", "START-DASHBOARD.bat", "README.md"):
            z.write(ROOT / name, name)
    for name in ("pod_studio.db", "ebay_store.db"):
        source = ROOT / "data" / name
        if source.exists():
            with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
                with sqlite3.connect(destination / name) as dst:
                    src.backup(dst)
    print(destination)


def patch(selected: str | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    with zipfile.ZipFile(ARCHIVE) as z:
        entries = {n.replace("\\", "/"): n for n in z.namelist()}
        print("*** Begin Patch")
        for name in sorted(RESTORE):
            if selected and name != selected:
                continue
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT):
                raise ValueError("Invalid recovery target")
            old = path.read_text(encoding="utf-8") if path.exists() else None
            new = z.read(entries[name]).decode("utf-8").replace("\r\n", "\n")
            if name == "app/studio/models.py" and old:
                # Keep the newer POD records accessible alongside the original models.
                marker = "# POD-Betrieb:"
                new += "\n\n" + old[old.index(marker):]
            if old is None:
                print("*** Add File: " + path.as_posix())
            else:
                print("*** Update File: " + path.as_posix())
                print("@@")
                for line in old.splitlines():
                    print("-" + line)
            for line in new.splitlines():
                print("+" + line)
        print("*** End Patch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--patch", action="store_true")
    parser.add_argument("--file", choices=sorted(RESTORE))
    args = parser.parse_args()
    if args.backup:
        backup()
    if args.patch:
        patch(args.file)
