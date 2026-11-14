DESCRIPTION = 'Build a station departure supply mart in SQLite from a v1 snapshot'
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .supply import build_supply


def main():
    p = argparse.ArgumentParser(prog='service-path-supply', description=DESCRIPTION)
    p.add_argument('--source', type=Path, required=True,
                   help='v1 service-day snapshot JSON')
    p.add_argument('--start', required=True,
                   help='first natural date of the closed window (YYYY-MM-DD)')
    p.add_argument('--end', required=True,
                   help='last natural date of the closed window (YYYY-MM-DD)')
    p.add_argument('--database', type=Path, required=True,
                   help='SQLite database to create or update')
    a = p.parse_args()
    try:
        if a.source.resolve() == a.database.resolve():
            raise ValueError('source and database must differ')
        report = build_supply(a.source, a.start, a.end, a.database)
    except (ValueError, OSError, KeyError, TypeError, OverflowError,
            RecursionError, sqlite3.Error) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
