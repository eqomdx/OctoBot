from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def copy_tables(source: Path, destination: Path) -> None:
    with sqlite3.connect(destination) as dst:
        dst.execute("ATTACH DATABASE ? AS incoming", (str(source),))
        tables = dst.execute(
            "SELECT name, sql FROM incoming.sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()

        for name, create_sql in tables:
            exists = dst.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if not exists:
                dst.execute(create_sql)

            source_columns = [
                row[1] for row in dst.execute(f'PRAGMA incoming.table_info("{name}")')
            ]
            target_columns = {
                row[1] for row in dst.execute(f'PRAGMA main.table_info("{name}")')
            }
            common = [column for column in source_columns if column in target_columns]
            if not common:
                continue
            quoted = ", ".join(f'"{column}"' for column in common)
            dst.execute(
                f'INSERT OR IGNORE INTO main."{name}" ({quoted}) '
                f'SELECT {quoted} FROM incoming."{name}"'
            )

        dst.commit()
        dst.execute("DETACH DATABASE incoming")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge an OctoTracker DB and an OctoCop DB into one OctoBot SQLite DB."
    )
    parser.add_argument("tracker_db", type=Path)
    parser.add_argument("moderation_db", type=Path)
    parser.add_argument("output_db", type=Path)
    args = parser.parse_args()

    for path in (args.tracker_db, args.moderation_db):
        if not path.exists():
            raise SystemExit(f"Missing database: {path}")

    backup_database(args.tracker_db, args.output_db)
    copy_tables(args.moderation_db, args.output_db)

    with sqlite3.connect(args.output_db) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"Merged database failed integrity check: {integrity}")
    print(f"Merged database created: {args.output_db}")


if __name__ == "__main__":
    main()
