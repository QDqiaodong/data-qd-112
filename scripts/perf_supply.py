#!/usr/bin/env python3
"""Reproducible bounded-memory benchmark for service-path-supply.

Runs the *same fixed* synthetic v1 snapshot through two natural-day windows
(100 days and 3000 days) in separate, clean subprocesses.  Each child traces
Python allocations with :mod:`tracemalloc` across the whole build-and-publish
process and reports:

* departure-fact and hourly-summary row counts (the long window must hold at
  least 2,000,000 facts and 700,000 summary rows),
* wall-clock duration,
* the tracemalloc peak in MiB.

The design contract under test is that extra Python memory does not grow with
the number of facts or summary rows, so the two peaks may differ by at most
32 MiB despite the 3000-day window materialising 30x more rows.  The SQLite
files (the explicitly allowed on-disk cache and the published database) carry
the growth instead; only the standard library is used.

The script exits non-zero if the long-window row thresholds or the peak-memory
bound are not met.

Examples:
    python3 scripts/perf_supply.py
    python3 scripts/perf_supply.py --days 50 300
    python3 scripts/perf_supply.py --keep-db
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path

# Make ``src`` importable both when launched as a script and under ``python -I``
# in a worker (isolated mode ignores the PYTHONPATH the parent sets).
_SRC = str(Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from service_path.supply import build, write_database  # noqa: E402
from service_path.timetable import snapshot  # noqa: E402

SHORT_DAYS = 100
LONG_DAYS = 3000
STATIONS = 10
TRIPS_PER_DAY = 80
PEAK_TOLERANCE_MIB = 32
MIN_LONG_FACTS = 2_000_000
MIN_LONG_HOURLY = 700_000


def fixed_snapshot():
    """Build the one immutable snapshot both windows share.

    Ten stations and eighty daily trips, every trip calling every station, so
    each active service day emits ``80 * 9 = 720`` departure facts over ten
    stations.  The calendar covers the full benchmark span and runs every
    weekday, which keeps both windows dense and deterministic.
    """
    stop_ids = ['S%02d' % i for i in range(STATIONS)]
    calls = []
    for index, stop_id in enumerate(stop_ids):
        # Spread departures across the day (one every six minutes from 06:00).
        depart = 6 * 3600 + index * 360
        clock = '%02d:%02d:%02d' % (depart // 3600,
                                    (depart % 3600) // 60, depart % 60)
        calls.append({'stop': stop_id, 'arrival': clock, 'departure': clock})
    trips = [{'id': 'T%04d' % index, 'calendar': 'daily', 'calls': calls}
             for index in range(TRIPS_PER_DAY)]
    raw = {
        'stops': [{'id': stop_id, 'transfer_seconds': 0}
                  for stop_id in stop_ids],
        'calendars': [{
            'id': 'daily',
            'start': '2020-01-01',
            'end': '2035-12-31',
            'weekdays': [0, 1, 2, 3, 4, 5, 6],
            'added': [],
            'removed': [],
        }],
        'trips': trips,
        'query': {'date': '2026-01-01', 'origin': stop_ids[0],
                  'destination': stop_ids[-1], 'depart': '05:00:00'},
    }
    return snapshot(raw)


def run_window(days, database_path):
    """Child entry point: trace one build-and-publish over ``days`` days."""
    start = date(2026, 1, 1)
    end = start + timedelta(days=days - 1)
    snap = fixed_snapshot()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with build(snap, start, end) as built:
            write_database(database_path, built, start, end)
            # Stream every row so any accidental internal buffering is touched.
            fact_count = sum(1 for _ in built.iter_facts())
            hourly_count = sum(1 for _ in built.iter_hourly())
            stats = dict(built.stats)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result = {
        'days': days,
        'facts': fact_count,
        'hourly_rows': hourly_count,
        'elapsed_seconds': round(elapsed, 3),
        'peak_mib': round(peak / 1048576, 3),
        'stats': stats,
        'database': str(database_path),
    }
    print(json.dumps(result, sort_keys=True))
    return result


def _spawn(days, database_path):
    here = Path(__file__).resolve()
    env = dict(os.environ)
    env['PYTHONPATH'] = str(here.parents[1] / 'src') + (
        os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    completed = subprocess.run(
        [sys.executable, '-I', str(here), '--worker', str(days),
         str(database_path)],
        capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        raise SystemExit('worker for %d days failed (exit %d):\n%s'
                         % (days, completed.returncode,
                            completed.stderr.strip()))
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, nargs=2,
                        metavar=('SHORT', 'LONG'),
                        default=(SHORT_DAYS, LONG_DAYS),
                        help='the two window lengths to compare')
    parser.add_argument('--worker', nargs=2, metavar=('DAYS', 'DATABASE'),
                        help=argparse.SUPPRESS)
    parser.add_argument('--keep-db', action='store_true',
                        help='keep the published databases instead of removing them')
    arguments = parser.parse_args(argv)

    if arguments.worker is not None:
        days, database_path = arguments.worker
        run_window(int(days), Path(database_path))
        return 0

    short_days, long_days = arguments.days
    if not 0 < short_days < long_days:
        parser.error('window lengths must satisfy 0 < SHORT < LONG')

    workspace = Path(tempfile.mkdtemp(prefix='perf-supply-'))
    databases = {
        short_days: workspace / ('supply-%d.sqlite' % short_days),
        long_days: workspace / ('supply-%d.sqlite' % long_days),
    }
    failures = []
    try:
        # Fresh interpreter per window so neither run inherits the other's
        # allocator state; both still start from the identical snapshot.
        short = _spawn(short_days, databases[short_days])
        long = _spawn(long_days, databases[long_days])

        print('service-path-supply bounded-memory benchmark')
        print('snapshot: %d stations, %d trips/day, %d departure calls/trip'
              % (STATIONS, TRIPS_PER_DAY, STATIONS - 1))
        header = '%-8s %12s %12s %12s %12s' % (
            'days', 'facts', 'hourly', 'seconds', 'peak MiB')
        print(header)
        for result in (short, long):
            print('%-8d %12d %12d %12.3f %12.3f'
                  % (result['days'], result['facts'], result['hourly_rows'],
                     result['elapsed_seconds'], result['peak_mib']))

        if long['facts'] < MIN_LONG_FACTS:
            failures.append('long window has %d facts; need >= %d'
                            % (long['facts'], MIN_LONG_FACTS))
        if long['hourly_rows'] < MIN_LONG_HOURLY:
            failures.append('long window has %d hourly rows; need >= %d'
                            % (long['hourly_rows'], MIN_LONG_HOURLY))

        delta = long['peak_mib'] - short['peak_mib']
        print('peak difference (long - short): %.3f MiB (bound %.0f MiB)'
              % (delta, PEAK_TOLERANCE_MIB))
        if delta > PEAK_TOLERANCE_MIB:
            failures.append('peak difference %.3f MiB exceeds %.0f MiB'
                            % (delta, PEAK_TOLERANCE_MIB))

        # The short window still has to be consistent with its own stats.
        for result in (short, long):
            if result['stats']['departures'] != result['facts']:
                failures.append('window %d: stats departures != counted facts'
                                % result['days'])
            if result['stats']['hourly_rows'] != result['hourly_rows']:
                failures.append('window %d: stats hourly_rows != counted rows'
                                % result['days'])

        if arguments.keep_db:
            print('databases kept under %s' % workspace)
        if failures:
            for failure in failures:
                print('FAIL: %s' % failure, file=sys.stderr)
            return 1
        print('PASS: bounded-memory thresholds met')
        return 0
    finally:
        if not arguments.keep_db:
            for database in databases.values():
                for suffix in ('', '-journal', '-wal', '-shm'):
                    candidate = Path(str(database) + suffix)
                    if candidate.exists():
                        candidate.unlink()
            try:
                workspace.rmdir()
            except OSError:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
