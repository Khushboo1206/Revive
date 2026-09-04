import argparse
import sys

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models
from app.config import get_database_url
from app.db import Base


TABLES = {table.name: table for table in Base.metadata.sorted_tables}


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove all rows from one Revive database table.")
    parser.add_argument("table", choices=sorted(TABLES), help="Table to clear.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--truncate", action="store_true", help="Use TRUNCATE instead of DELETE.")
    parser.add_argument("--cascade", action="store_true", help="With --truncate, also clear referencing tables (PostgreSQL only).")
    args = parser.parse_args()

    if args.cascade and not args.truncate:
        parser.error("--cascade requires --truncate")

    engine = create_engine(get_database_url(), pool_pre_ping=True)
    table = TABLES[args.table]
    with Session(engine) as db:
        row_count = db.scalar(select(func.count()).select_from(table)) or 0

    operation = "TRUNCATE" if args.truncate else "DELETE"
    cascade_text = " CASCADE" if args.cascade else ""
    print(f"Table: {args.table}")
    print(f"Rows to remove: {row_count}")
    print(f"Operation: {operation}{cascade_text}")

    if row_count == 0:
        print("Nothing to remove.")
        return 0
    if not args.yes:
        confirmation = input(f'Type "DELETE {args.table}" to continue: ')
        if confirmation != f"DELETE {args.table}":
            print("Cancelled.")
            return 1

    try:
        with engine.begin() as connection:
            if args.truncate:
                if connection.dialect.name != "postgresql":
                    print("--truncate is supported only for PostgreSQL.", file=sys.stderr)
                    return 1
                quoted_table = connection.dialect.identifier_preparer.quote(args.table)
                connection.execute(text(f"TRUNCATE TABLE {quoted_table} RESTART IDENTITY{cascade_text}"))
            else:
                connection.execute(table.delete())
    except SQLAlchemyError as exc:
        print(f"Database cleanup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Removed {row_count} rows from {args.table}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
