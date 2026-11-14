DESCRIPTION = 'Import an offline GTFS directory into a service-path snapshot'
import argparse
import json
import sys
from pathlib import Path

from .gtfs import CALENDAR, CALENDAR_DATES, STOPS, STOP_TIMES, TRIPS, import_snapshot
from .io import publish


def main():
    p = argparse.ArgumentParser(prog='service-path-import', description=DESCRIPTION)
    p.add_argument('--source', type=Path, required=True,
                   help='directory containing stops.txt, trips.txt, stop_times.txt '
                        'and calendar.txt/calendar_dates.txt')
    p.add_argument('--query', type=Path, required=True,
                   help='existing query JSON (a query object or a snapshot with a query field)')
    p.add_argument('--output', type=Path, required=True,
                   help='snapshot JSON consumable by service-path')
    a = p.parse_args()
    try:
        protected = (STOPS, TRIPS, STOP_TIMES, CALENDAR, CALENDAR_DATES)
        if any(a.output.resolve() == (a.source / name).resolve() for name in protected):
            raise ValueError('output must not overwrite a GTFS input file')
        if a.output.resolve() == a.query.resolve():
            raise ValueError('output must not overwrite the query file')
        result = import_snapshot(a.source, a.query)
        publish(a.output, result)
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
