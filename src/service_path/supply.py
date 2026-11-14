"""Station departure supply mart built from a v1 service-day snapshot.

The mart answers "which stations are served, when, on which natural day" for a
closed range of calendar dates.  A v1 snapshot speaks in service days and
service times of 00:00:00..47:59:59; this module translates every scheduled
departure into a natural day (``service_day + time // 24h``) and a second of
that natural day, and keeps two SQLite tables:

``departures``
    one row per departure fact.  The natural primary key
    ``(service_day, trip_id, call_index)`` is unique.  ``natural_day`` and
    ``day_seconds`` are the translated wall-clock position, ``stop_id`` is the
    station and ``call_index`` the zero-based position of the departure call
    inside ``trip.calls`` (the last call of a trip is a terminal and therefore
    never produces a departure fact).

``hourly_departures``
    the per-natural-day / per-stop / per-hour summary.  Every snapshot stop
    gets a row for all 24 hours of every requested natural day, including hours
    with no service (``departure_count`` 0, ``max_headway_seconds`` 3600).
    ``departure_count`` counts facts, so two vehicles departing at the same
    second both count.  ``max_headway_seconds`` is the largest gap between
    consecutive de-duplicated departure seconds of that hour, measured between
    the hour's two boundary instants as well; a departure exactly on the right
    boundary belongs to the next hour.

Only a natural day window ``[start, end]`` is rebuilt: a rerun deletes and
re-inserts rows whose ``natural_day`` falls inside the window and leaves all
other dates alone.  Facts and the summary are written in one transaction, and
the whole mart is computed in memory before the database is opened, so a bad
snapshot never touches an existing database and a failed write rolls back
completely.
"""
import sqlite3
from datetime import timedelta
from pathlib import Path

from .io import load
from .timetable import calendar_active, day, snapshot

DAY_SECONDS = 86400
HOURS_PER_DAY = 24
MAX_DAYS = 3660
MAX_HOURLY_ROWS = 5_000_000
MAX_FACTS = 5_000_000

DEPARTURE_COLUMNS = ('service_day', 'trip_id', 'call_index',
                     'natural_day', 'stop_id', 'day_seconds')
HOURLY_COLUMNS = ('natural_day', 'stop_id', 'hour',
                  'departure_count', 'max_headway_seconds')
_DEPARTURE_PK = ('service_day', 'trip_id', 'call_index')
_HOURLY_PK = ('natural_day', 'stop_id', 'hour')

_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS departures ('
    ' service_day TEXT NOT NULL,'
    ' trip_id TEXT NOT NULL,'
    ' call_index INTEGER NOT NULL,'
    ' natural_day TEXT NOT NULL,'
    ' stop_id TEXT NOT NULL,'
    ' day_seconds INTEGER NOT NULL CHECK (day_seconds BETWEEN 0 AND 86399),'
    ' PRIMARY KEY (service_day, trip_id, call_index))',
    'CREATE TABLE IF NOT EXISTS hourly_departures ('
    ' natural_day TEXT NOT NULL,'
    ' stop_id TEXT NOT NULL,'
    ' hour INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),'
    ' departure_count INTEGER NOT NULL CHECK (departure_count >= 0),'
    ' max_headway_seconds INTEGER NOT NULL'
    ' CHECK (max_headway_seconds BETWEEN 1 AND 3600),'
    ' PRIMARY KEY (natural_day, stop_id, hour))',
    'CREATE INDEX IF NOT EXISTS departures_by_day_stop'
    ' ON departures (natural_day, stop_id, day_seconds)',
)


def _day_window(start, end):
    if start > end:
        raise ValueError('start date is later than end date')
    days = (end - start).days + 1
    if days > MAX_DAYS:
        raise ValueError('date range spans %d natural days; at most %d are supported'
                         % (days, MAX_DAYS))
    return days


def build(snap, start, end):
    """Translate a normalised snapshot into departure facts and hourly rows.

    ``snap`` is the mapping returned by ``timetable.snapshot``.  ``start`` and
    ``end`` are ``datetime.date`` objects of a closed natural-day window.
    """
    days = _day_window(start, end)
    stations = sorted(snap['stops'])
    hourly_rows = days * len(stations) * HOURS_PER_DAY
    if hourly_rows > MAX_HOURLY_ROWS:
        raise ValueError('%d natural days x %d stations needs %d hourly rows; at most %d'
                         ' are supported' % (days, len(stations), hourly_rows,
                                             MAX_HOURLY_ROWS))

    facts = []
    service_day = start - timedelta(days=1)
    while service_day <= end:
        active = {calendar_id for calendar_id, calendar in snap['calendars'].items()
                  if calendar_active(calendar, service_day)}
        for trip_id in sorted(snap['trips']):
            trip = snap['trips'][trip_id]
            if trip['calendar'] not in active:
                continue
            calls = trip['calls']
            # The final call has no onward departure; only calls 0..n-2 do.
            for call_index in range(len(calls) - 1):
                service_seconds = calls[call_index][2]
                natural_day = service_day + timedelta(days=service_seconds // DAY_SECONDS)
                if start <= natural_day <= end:
                    if len(facts) >= MAX_FACTS:
                        raise ValueError('window holds more than %d departures;'
                                         ' narrow the date range' % MAX_FACTS)
                    facts.append((service_day.isoformat(), trip_id, call_index,
                                  natural_day.isoformat(), calls[call_index][0],
                                  service_seconds % DAY_SECONDS))
        service_day += timedelta(days=1)
    facts.sort()

    stops = stations
    counts = {}
    seconds = {}
    for fact in facts:
        natural_day, stop_id, day_seconds = fact[3], fact[4], fact[5]
        hour = day_seconds // 3600
        key = (natural_day, stop_id, hour)
        counts[key] = counts.get(key, 0) + 1
        seconds.setdefault(key, set()).add(day_seconds)

    hourly = []
    for offset in range(days):
        natural_day = (start + timedelta(days=offset)).isoformat()
        for stop_id in stops:
            for hour in range(HOURS_PER_DAY):
                key = (natural_day, stop_id, hour)
                count = counts.get(key, 0)
                departures = seconds.get(key)
                if departures is None:
                    max_headway = 3600
                else:
                    # Deduplicated departure seconds plus both hour boundaries.
                    points = sorted({hour * 3600, (hour + 1) * 3600} | departures)
                    max_headway = max(later - earlier
                                      for earlier, later in zip(points, points[1:]))
                hourly.append((natural_day, stop_id, hour, count, max_headway))

    return {
        'facts': facts,
        'hourly': hourly,
        'stations': stops,
        'stats': {
            'natural_days': days,
            'service_days': days + 1,
            'stations': len(stops),
            'departures': len(facts),
            'hourly_rows': days * len(stops) * HOURS_PER_DAY,
        },
    }


def _existing_table_columns(connection, table):
    return [(row[1], row[5]) for row in
            connection.execute('PRAGMA table_info("%s")' % table)]


def _expected_columns(columns, primary_key):
    # PRAGMA table_info reports a column's 1-based position inside the primary
    # key, or 0 when the column is not part of it.
    return [(name, primary_key.index(name) + 1 if name in primary_key else 0)
            for name in columns]


def _prepare_schema(connection, path):
    existing = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('departures', 'hourly_departures')")}
    expected = {
        'departures': _expected_columns(DEPARTURE_COLUMNS, _DEPARTURE_PK),
        'hourly_departures': _expected_columns(HOURLY_COLUMNS, _HOURLY_PK),
    }
    for table in sorted(existing):
        if _existing_table_columns(connection, table) != expected[table]:
            raise ValueError('database %s: existing table %s has a different schema'
                             % (path, table))
    for statement in _SCHEMA:
        connection.execute(statement)


def _remove_db_files(path):
    for suffix in ('', '-journal', '-wal', '-shm'):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def write_database(path, built, start, end):
    """Write the mart to SQLite, replacing only the ``[start, end]`` partition.

    Facts and the summary are inserted in one transaction.  An existing
    database is only modified after its schema has been verified; any failure
    rolls the transaction back.  A database file that did not exist before the
    call is removed if the write does not commit, so no half-built file is left
    behind.
    """
    path = Path(path)
    if path.is_symlink():
        raise ValueError('database symlink is not allowed: %s' % path)
    if path.is_dir():
        raise ValueError('database path is a directory: %s' % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    committed = False
    try:
        connection = sqlite3.connect(str(path), timeout=30)
    except sqlite3.Error as error:
        raise ValueError('database %s: %s' % (path, error)) from error
    try:
        connection.isolation_level = None
        try:
            connection.execute('BEGIN IMMEDIATE')
            _prepare_schema(connection, path)
            start_text = start.isoformat()
            end_text = end.isoformat()
            connection.execute('DELETE FROM departures'
                               ' WHERE natural_day BETWEEN ? AND ?',
                               (start_text, end_text))
            connection.execute('DELETE FROM hourly_departures'
                               ' WHERE natural_day BETWEEN ? AND ?',
                               (start_text, end_text))
            connection.executemany(
                'INSERT INTO departures'
                ' (service_day, trip_id, call_index, natural_day, stop_id, day_seconds)'
                ' VALUES (?, ?, ?, ?, ?, ?)', built['facts'])
            connection.executemany(
                'INSERT INTO hourly_departures'
                ' (natural_day, stop_id, hour, departure_count, max_headway_seconds)'
                ' VALUES (?, ?, ?, ?, ?)', built['hourly'])
            connection.execute('COMMIT')
            committed = True
        except BaseException:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as error:
        raise ValueError('database %s: %s' % (path, error)) from error
    finally:
        connection.close()
        if not existed and not committed:
            _remove_db_files(path)


def build_supply(source, start_text, end_text, database):
    """Validate the snapshot, build the window and publish it to SQLite."""
    data = load(source)
    snap = snapshot(data)
    start = day(start_text)
    end = day(end_text)
    built = build(snap, start, end)
    write_database(database, built, start, end)
    report = dict(built['stats'])
    report.update({'database': str(database),
                   'start': start.isoformat(), 'end': end.isoformat()})
    return report
