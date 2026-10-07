"""Create and verify a consistent online SQLite backup."""

import sqlite3
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: backup_sqlite.py SOURCE DESTINATION")
    source, destination = map(Path, sys.argv[1:])
    if not source.is_file() or destination.exists():
        raise SystemExit("source missing or destination already exists")
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
        result = backup.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"backup integrity check failed: {result}")
    print(f"backup verified: {destination}")


if __name__ == "__main__":
    main()
