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
other dates alone.  Facts and the summary are written in one transaction.

Bounded-memory pipeline
-----------------------

The whole mart is *not* materialised in Python memory.  ``build`` spills the
facts into a private, throwaway SQLite staging cache as they are produced, in
fixed-size batches, and streams them back in key order to derive the hourly
summary.  The Python interpreter therefore only ever holds:

* one insert batch (``_BATCH_ROWS`` tuples),
* the active-calendar set of a single service day,
* the de-duplicated departure seconds of a single (natural day, stop, hour)
  group -- at most 3600 integers because an hour only has 3600 seconds,

so extra Python memory is independent of the total number of facts or summary
rows; only the input snapshot and the SQLite caches grow with the window.  The
staging cache is created after the snapshot has been fully validated and is
always deleted again, so a bad snapshot never touches an existing database and
no cache file survives the run.  Publishing attaches the validated cache to the
target database and copies both tables inside one transaction; a failed write
rolls back completely and a database that did not exist before the call is
removed together with its sidecar files.
"""
import os
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from .io import load
from .timetable import calendar_active, day, snapshot

DAY_SECONDS = 86400
HOURS_PER_DAY = 24
MAX_DAYS = 3660
MAX_HOURLY_ROWS = 5_000_000
MAX_FACTS = 5_000_000

# Maximum number of rows Python keeps in memory at once while streaming facts
# into the staging cache and rows back out of it.
_BATCH_ROWS = 1000

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

_STAGING_SCHEMA = (
    # No primary key on the cache: uniqueness is enforced by the published
    # table, and keeping the key off the cache lets a deliberately corrupted
    # cache exercise the target-side rollback path.
    'CREATE TABLE facts ('
    ' service_day TEXT NOT NULL, trip_id TEXT NOT NULL,'
    ' call_index INTEGER NOT NULL, natural_day TEXT NOT NULL,'
    ' stop_id TEXT NOT NULL, day_seconds INTEGER NOT NULL)',
    'CREATE TABLE hourly_seen ('
    ' natural_day TEXT NOT NULL, stop_id TEXT NOT NULL, hour INTEGER NOT NULL,'
    ' departure_count INTEGER NOT NULL, max_headway_seconds INTEGER NOT NULL)',
    'CREATE TABLE hourly ('
    ' natural_day TEXT NOT NULL, stop_id TEXT NOT NULL, hour INTEGER NOT NULL,'
    ' departure_count INTEGER NOT NULL, max_headway_seconds INTEGER NOT NULL)',
)


def _day_window(start, end):
    if start > end:
        raise ValueError('start date is later than end date')
    days = (end - start).days + 1
    if days > MAX_DAYS:
        raise ValueError('date range spans %d natural days; at most %d are supported'
                         % (days, MAX_DAYS))
    return days


def _max_headway(seconds, hour):
    """Largest gap between de-duplicated departure seconds and hour boundaries."""
    # Deduplicated departure seconds plus both hour boundaries.
    points = sorted({hour * 3600, (hour + 1) * 3600} | seconds)
    return max(later - earlier for earlier, later in zip(points, points[1:]))


class _OrderedPeek:
    """Lazy peekable iterator over a SQLite cursor, one fetch batch at a time."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._chunks = iter(lambda: cursor.fetchmany(_BATCH_ROWS), ())
        self._rows = ()
        self._index = 0
        self.head = None
        self.advance()

    def advance(self):
        while self._index >= len(self._rows):
            self._rows = next(self._chunks, ())
            self._index = 0
            if not self._rows:
                self.head = None
                return
        self.head = self._rows[self._index]
        self._index += 1


def _iter_hour_groups(connection):
    """Yield (natural_day, stop_id, hour, count, distinct-seconds) groups.

    The facts table is scanned in (natural_day, stop_id, day_seconds) order, so
    each group is completed before the next one starts and only the current
    group's state -- a counter and at most 3600 distinct seconds -- is held.
    """
    cursor = connection.execute(
        'SELECT natural_day, stop_id, day_seconds FROM facts'
        ' ORDER BY natural_day, stop_id, day_seconds')
    current_stop = None
    current_hour = None
    count = 0
    seconds = set()
    try:
        while True:
            rows = cursor.fetchmany(_BATCH_ROWS)
            if not rows:
                break
            for natural_day, stop_id, day_seconds in rows:
                hour = day_seconds // 3600
                if (natural_day, stop_id) != current_stop or hour != current_hour:
                    if current_stop is not None:
                        yield (current_stop[0], current_stop[1], current_hour,
                               count, seconds)
                    current_stop = (natural_day, stop_id)
                    current_hour = hour
                    count = 0
                    seconds = set()
                count += 1
                seconds.add(day_seconds)
    finally:
        cursor.close()
    if current_stop is not None:
        yield (current_stop[0], current_stop[1], current_hour, count, seconds)


class SupplyBuild:
    """A validated mart held in a private, bounded-memory SQLite staging cache.

    Instances are context managers.  ``close`` (or leaving the ``with`` block)
    closes and deletes the staging cache; no Python collection of facts or
    summary rows is ever exposed, so callers cannot accidentally materialise the
    whole window.  The published database is written from :data:`staging_path`.
    """

    def __init__(self, connection, staging_path, stations, stats):
        self._connection = connection
        self.staging_path = staging_path
        self.stations = stations
        self.stats = stats
        self._closed = False

    @property
    def connection(self):
        if self._closed:
            raise ValueError('supply build is closed')
        return self._connection

    def iter_facts(self):
        """Stream departure facts in primary-key order, one batch at a time."""
        cursor = self.connection.execute(
            'SELECT service_day, trip_id, call_index, natural_day,'
            ' stop_id, day_seconds FROM facts'
            ' ORDER BY service_day, trip_id, call_index')
        try:
            while True:
                rows = cursor.fetchmany(_BATCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    yield row
        finally:
            cursor.close()

    def iter_hourly(self):
        """Stream hourly rows in primary-key order, one batch at a time."""
        cursor = self.connection.execute(
            'SELECT natural_day, stop_id, hour, departure_count,'
            ' max_headway_seconds FROM hourly'
            ' ORDER BY natural_day, stop_id, hour')
        try:
            while True:
                rows = cursor.fetchmany(_BATCH_ROWS)
                if not rows:
                    break
                for row in rows:
                    yield row
        finally:
            cursor.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        path = Path(self.staging_path)
        try:
            self._connection.close()
        finally:
            for suffix in ('', '-journal', '-wal', '-shm'):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _open_staging_cache():
    descriptor, staging_path = tempfile.mkstemp(
        prefix='service-path-supply-', suffix='.sqlite')
    os.close(descriptor)
    connection = sqlite3.connect(staging_path)
    # The cache is disposable: keep its journal in memory so no sidecar file is
    # ever created next to it.
    connection.execute('PRAGMA journal_mode=MEMORY')
    connection.execute('PRAGMA synchronous=OFF')
    connection.execute('PRAGMA temp_store=MEMORY')
    for statement in _STAGING_SCHEMA:
        connection.execute(statement)
    return connection, staging_path


def build(snap, start, end):
    """Translate a normalised snapshot into a bounded-memory supply build.

    ``snap`` is the mapping returned by ``timetable.snapshot``.  ``start`` and
    ``end`` are ``datetime.date`` objects of a closed natural-day window.  The
    returned :class:`SupplyBuild` owns a temporary SQLite staging cache holding
    exactly the facts and hourly rows the window needs; the function itself only
    keeps fixed-size batches in Python memory.  The cache is deleted by
    :meth:`SupplyBuild.close`, including when the build raises.
    """
    days = _day_window(start, end)
    stations = sorted(snap['stops'])
    hourly_rows = days * len(stations) * HOURS_PER_DAY
    if hourly_rows > MAX_HOURLY_ROWS:
        raise ValueError('%d natural days x %d stations needs %d hourly rows; at most %d'
                         ' are supported' % (days, len(stations), hourly_rows,
                                             MAX_HOURLY_ROWS))

    connection = None
    staging_path = None
    try:
        connection, staging_path = _open_staging_cache()

        # Phase 1: stream departure facts into the staging cache.  Python only
        # ever holds ``batch`` (at most _BATCH_ROWS tuples) plus one day's
        # active-calendar set.
        fact_count = 0
        batch = []
        service_day = start - timedelta(days=1)
        last_service_day = end
        while service_day <= last_service_day:
            active = {calendar_id for calendar_id, calendar in snap['calendars'].items()
                      if calendar_active(calendar, service_day)}
            service_day_text = service_day.isoformat()
            for trip_id in sorted(snap['trips']):
                trip = snap['trips'][trip_id]
                if trip['calendar'] not in active:
                    continue
                calls = trip['calls']
                # The final call has no onward departure; only calls 0..n-2 do.
                for call_index in range(len(calls) - 1):
                    service_seconds = calls[call_index][2]
                    natural_day = service_day + timedelta(
                        days=service_seconds // DAY_SECONDS)
                    if start <= natural_day <= end:
                        if fact_count >= MAX_FACTS:
                            raise ValueError('window holds more than %d departures;'
                                             ' narrow the date range' % MAX_FACTS)
                        fact_count += 1
                        batch.append((service_day_text, trip_id, call_index,
                                      natural_day.isoformat(),
                                      calls[call_index][0],
                                      service_seconds % DAY_SECONDS))
                        if len(batch) == _BATCH_ROWS:
                            connection.executemany(
                                'INSERT INTO facts (service_day, trip_id,'
                                ' call_index, natural_day, stop_id, day_seconds)'
                                ' VALUES (?, ?, ?, ?, ?, ?)', batch)
                            batch.clear()
            service_day += timedelta(days=1)
        if batch:
            connection.executemany(
                'INSERT INTO facts (service_day, trip_id, call_index,'
                ' natural_day, stop_id, day_seconds)'
                ' VALUES (?, ?, ?, ?, ?, ?)', batch)
            batch.clear()

        # Index after the bulk load so ingestion stays a plain append.
        connection.execute(
            'CREATE INDEX facts_day_stop_seconds ON facts'
            ' (natural_day, stop_id, day_seconds)')

        # Phase 2: derive the served hourly groups by streaming the facts back
        # in key order.  A group's second set never exceeds the 3600 seconds of
        # an hour, whatever the window length.
        seen_batch = []
        for natural_day, stop_id, hour, count, seconds in _iter_hour_groups(connection):
            seen_batch.append((natural_day, stop_id, hour, count,
                               _max_headway(seconds, hour)))
            if len(seen_batch) == _BATCH_ROWS:
                connection.executemany(
                    'INSERT INTO hourly_seen (natural_day, stop_id, hour,'
                    ' departure_count, max_headway_seconds)'
                    ' VALUES (?, ?, ?, ?, ?)', seen_batch)
                seen_batch.clear()
        if seen_batch:
            connection.executemany(
                'INSERT INTO hourly_seen (natural_day, stop_id, hour,'
                ' departure_count, max_headway_seconds)'
                ' VALUES (?, ?, ?, ?, ?)', seen_batch)
            seen_batch.clear()
        connection.execute(
            'CREATE INDEX hourly_seen_key ON hourly_seen'
            ' (natural_day, stop_id, hour)')

        # Phase 3: every (natural day, station, hour) cell must exist.  Merge
        # the deterministic day x station x 24 grid with the served groups, both
        # in the same key order, in one bounded-memory pass.
        seen = _OrderedPeek(connection.execute(
            'SELECT natural_day, stop_id, hour, departure_count,'
            ' max_headway_seconds FROM hourly_seen'
            ' ORDER BY natural_day, stop_id, hour'))
        hourly_batch = []

        def flush_hourly():
            if hourly_batch:
                connection.executemany(
                    'INSERT INTO hourly (natural_day, stop_id, hour,'
                    ' departure_count, max_headway_seconds)'
                    ' VALUES (?, ?, ?, ?, ?)', hourly_batch)
                hourly_batch.clear()

        for offset in range(days):
            natural_day = (start + timedelta(days=offset)).isoformat()
            for stop_id in stations:
                for hour in range(HOURS_PER_DAY):
                    row = seen.head
                    if (row is not None and row[0] == natural_day
                            and row[1] == stop_id and row[2] == hour):
                        hourly_batch.append(row)
                        seen.advance()
                    else:
                        hourly_batch.append(
                            (natural_day, stop_id, hour, 0, 3600))
                    if len(hourly_batch) == _BATCH_ROWS:
                        flush_hourly()
        flush_hourly()
        connection.execute('DROP INDEX hourly_seen_key')
        connection.execute('DROP TABLE hourly_seen')
        connection.commit()

        stats = {
            'natural_days': days,
            'service_days': days + 1,
            'stations': len(stations),
            'departures': fact_count,
            'hourly_rows': days * len(stations) * HOURS_PER_DAY,
        }
        return SupplyBuild(connection, staging_path, stations, stats)
    except BaseException:
        if connection is not None:
            connection.close()
        if staging_path is not None:
            for suffix in ('', '-journal', '-wal', '-shm'):
                candidate = Path(str(staging_path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        raise


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

    Facts and the summary are inserted in one transaction, copied straight from
    the validated staging cache with ``ATTACH`` so no row list crosses Python
    memory.  An existing database is only modified after its schema has been
    verified; any failure rolls the transaction back.  A database file that did
    not exist before the call is removed if the write does not commit, together
    with its journal sidecars, so no half-built file is left behind.
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
        attached = False
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
            connection.execute('ATTACH DATABASE ? AS stage',
                               (str(built.staging_path),))
            attached = True
            connection.execute(
                'INSERT INTO departures (service_day, trip_id, call_index,'
                ' natural_day, stop_id, day_seconds)'
                ' SELECT service_day, trip_id, call_index, natural_day,'
                ' stop_id, day_seconds FROM stage.facts'
                ' ORDER BY service_day, trip_id, call_index')
            connection.execute(
                'INSERT INTO hourly_departures (natural_day, stop_id, hour,'
                ' departure_count, max_headway_seconds)'
                ' SELECT natural_day, stop_id, hour, departure_count,'
                ' max_headway_seconds FROM stage.hourly'
                ' ORDER BY natural_day, stop_id, hour')
            connection.execute('COMMIT')
            committed = True
        except BaseException:
            # The rollback ends the transaction that locks the attached cache;
            # DETACH is only legal once no transaction references it.
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            # Best-effort cleanup: never let a detach problem mask the original
            # failure that triggered the rollback.
            if attached:
                try:
                    connection.execute('DETACH DATABASE stage')
                except sqlite3.Error:
                    pass
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
    try:
        write_database(database, built, start, end)
        report = dict(built.stats)
    finally:
        built.close()
    report.update({'database': str(database),
                   'start': start.isoformat(), 'end': end.isoformat()})
    return report
