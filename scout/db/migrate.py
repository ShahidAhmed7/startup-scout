"""Create or update the database tables: python -m scout.db.migrate"""
from pathlib import Path

from scout.db import repo


def migrate() -> list[str]:
    sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with repo.connect() as conn:
        conn.execute(sql)  # no parameters, so psycopg sends all statements at once
        return repo.existing_tables(conn)


if __name__ == "__main__":
    tables = migrate()
    missing = [t for t in repo.TABLES if t not in tables]
    print(f"Database ready: {len(tables)} tables in schema '{repo.SCHEMA}'.")
    if missing:
        print("Missing tables:", ", ".join(missing))
        raise SystemExit(1)
