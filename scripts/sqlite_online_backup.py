"""Create a consistent SQLite backup while the control plane is running."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if not args.source.is_file() or args.destination.exists():
        parser.error("source must exist and destination must not exist")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{args.source}?mode=ro", uri=True) as source:
        with sqlite3.connect(args.destination) as destination:
            source.backup(destination)
            check = destination.execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        raise RuntimeError("SQLite backup failed integrity check")
    print("SQLITE_BACKUP_CHECK=ok")


if __name__ == "__main__":
    main()
