"""Offline GTFS import into the v1 service-day snapshot format.

The importer reads a directory containing stops.txt, trips.txt, stop_times.txt
and at least one of calendar.txt / calendar_dates.txt.  Values are joined using
the standard GTFS column names; unknown columns are ignored.  The resulting
snapshot has exactly the fields understood by timetable.parse, so it can be fed
straight back to ``service-path`` in both the default and ``--pareto`` modes.

Everything is computed in memory before the caller publishes the snapshot, so a
failed import never replaces an existing output file.  All collections in the
snapshot are sorted, which makes the published bytes independent of the row
order in the source tables.
"""
import csv
import re
from datetime import date
from pathlib import Path

from .io import load as load_json
from .timetable import clock as query_clock, day as query_day

STOPS = 'stops.txt'
TRIPS = 'trips.txt'
STOP_TIMES = 'stop_times.txt'
CALENDAR = 'calendar.txt'
CALENDAR_DATES = 'calendar_dates.txt'

# GTFS service times are HH:MM:SS with hours up to 47.  A single-digit hour is
# accepted and normalised to two digits in the snapshot.
_TRIP_TIME = re.compile(r'([0-9]{1,2}):([0-5][0-9]):([0-5][0-9])')
_GTFS_DATE = re.compile(r'[0-9]{8}')
_SEQUENCE = re.compile(r'[0-9]+')
_WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


class _Row:
    __slots__ = ('name', 'record', 'line', 'values')

    def __init__(self, name, record, line, values):
        self.name = name
        self.record = record
        self.line = line
        self.values = values

    def location(self):
        keys = []
        for key in ('trip_id', 'stop_id', 'service_id', 'stop_sequence', 'date'):
            value = self.values.get(key)
            if value:
                keys.append('%s=%s' % (key, value))
        suffix = ' [' + ', '.join(keys) + ']' if keys else ''
        return '%s: record %d (line %d)%s' % (self.name, self.record, self.line, suffix)

    def fail(self, message):
        raise ValueError(self.location() + ': ' + message)


def _table(root, name, required_columns):
    path = root / name
    if not path.is_file():
        raise ValueError('%s: required file is missing from %s' % (name, root))
    record = 0
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError('%s: missing header row' % name) from None
            header = [column.strip() for column in header]
            seen = set()
            for column in header:
                if column and column in seen:
                    raise ValueError('%s: header has duplicate column %s' % (name, column))
                seen.add(column)
            index = {}
            for position, column in enumerate(header):
                if column and column not in index:
                    index[column] = position
            missing = [column for column in required_columns if column not in index]
            if missing:
                raise ValueError('%s: header is missing required columns: %s'
                                 % (name, ', '.join(missing)))
            for row in reader:
                line = reader.line_num
                if not any(cell.strip() for cell in row):
                    continue
                record += 1
                if len(row) > len(header) and any(cell.strip() for cell in row[len(header):]):
                    raise ValueError('%s: record %d (line %d): row has more fields than the header'
                                     % (name, record, line))
                values = {}
                for position, column in enumerate(header):
                    if column:
                        values[column] = row[position].strip() if position < len(row) else ''
                yield _Row(name, record, line, values)
    except UnicodeDecodeError as error:
        raise ValueError('%s: invalid UTF-8: %s' % (name, error)) from error
    except csv.Error as error:
        raise ValueError('%s: malformed CSV near record %d: %s'
                         % (name, record + 1, error)) from error


def _text(row, column):
    value = row.values.get(column, '')
    if not value:
        row.fail('%s is empty' % column)
    if len(value) > 256:
        row.fail('%s exceeds 256 characters' % column)
    if any(ord(char) < 32 for char in value):
        row.fail('%s contains a control character' % column)
    return value


def _service_time(row, column):
    value = row.values.get(column, '')
    if not value:
        row.fail('%s is empty' % column)
    match = _TRIP_TIME.fullmatch(value)
    if match is None:
        row.fail('%s must use HH:MM:SS with hour 00..47' % column)
    hour, minute, second = (int(part) for part in match.groups())
    if hour > 47:
        row.fail('%s hour must be <= 47' % column)
    return hour * 3600 + minute * 60 + second, '%02d:%02d:%02d' % (hour, minute, second)


def _gtfs_date(row, column):
    value = row.values.get(column, '')
    if not value:
        row.fail('%s is empty' % column)
    if _GTFS_DATE.fullmatch(value) is None:
        row.fail('%s must use YYYYMMDD' % column)
    try:
        parsed = date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        row.fail('%s is not a real date' % column)
    return parsed


def _stops(root):
    stops = {}
    for row in _table(root, STOPS, ('stop_id',)):
        stop_id = _text(row, 'stop_id')
        if stop_id in stops:
            row.fail('duplicate stop_id')
        stops[stop_id] = 0  # GTFS has no transfer time; snapshots use zero seconds.
    return stops


def _trips(root):
    trips = {}
    for row in _table(root, TRIPS, ('service_id', 'trip_id')):
        trip_id = _text(row, 'trip_id')
        service_id = _text(row, 'service_id')
        if trip_id in trips:
            row.fail('duplicate trip_id')
        trips[trip_id] = {'service': service_id, 'row': row}
    return trips


def _stop_times(root, stops, trips):
    calls = {}
    sequences = {}
    for row in _table(root, STOP_TIMES,
                      ('trip_id', 'arrival_time', 'departure_time',
                       'stop_id', 'stop_sequence')):
        trip_id = _text(row, 'trip_id')
        stop_id = _text(row, 'stop_id')
        if trip_id not in trips:
            row.fail('unknown trip_id')
        if stop_id not in stops:
            row.fail('unknown stop_id')
        sequence_text = row.values.get('stop_sequence', '')
        if not sequence_text:
            row.fail('stop_sequence is empty')
        if _SEQUENCE.fullmatch(sequence_text) is None:
            row.fail('stop_sequence must be a non-negative integer')
        sequence = int(sequence_text)
        seen = sequences.setdefault(trip_id, {})
        if sequence in seen:
            first = seen[sequence]
            row.fail('duplicate stop_sequence within trip (first at record %d, line %d)'
                     % (first.record, first.line))
        seen[sequence] = row
        arrival, arrival_text = _service_time(row, 'arrival_time')
        departure, departure_text = _service_time(row, 'departure_time')
        if departure < arrival:
            row.fail('departure_time is earlier than arrival_time')
        calls.setdefault(trip_id, []).append({
            'sequence': sequence,
            'stop': stop_id,
            'arrival': arrival,
            'arrival_text': arrival_text,
            'departure': departure,
            'departure_text': departure_text,
            'row': row,
        })
    for trip_id in sorted(trips):
        trip_calls = calls.get(trip_id)
        if not trip_calls:
            trips[trip_id]['row'].fail('trip has no stop_times rows')
        trip_calls.sort(key=lambda call: call['sequence'])
        if len(trip_calls) < 2:
            trip_calls[0]['row'].fail('trip needs at least two stop_times rows')
        repeated = set()
        previous_departure = None
        for call in trip_calls:
            if call['stop'] in repeated:
                call['row'].fail('repeated stop_id within trip')
            repeated.add(call['stop'])
            if previous_departure is not None and call['arrival'] <= previous_departure:
                call['row'].fail('arrival_time is not later than the previous departure_time')
            previous_departure = call['departure']
    return calls


def _calendar_rows(root):
    rows = {}
    for row in _table(root, CALENDAR,
                      ('service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
                       'friday', 'saturday', 'sunday', 'start_date', 'end_date')):
        service_id = _text(row, 'service_id')
        if service_id in rows:
            row.fail('duplicate service_id: calendar.txt supports one weekly row '
                     'per service_id in the v1 snapshot')
        weekdays = set()
        for day_number, column in enumerate(_WEEKDAYS):
            flag = row.values.get(column, '')
            if flag not in ('0', '1'):
                row.fail('%s must be 0 or 1' % column)
            if flag == '1':
                weekdays.add(day_number)
        start = _gtfs_date(row, 'start_date')
        end = _gtfs_date(row, 'end_date')
        if start > end:
            row.fail('start_date is later than end_date')
        rows[service_id] = {'start': start, 'end': end, 'weekdays': weekdays}
    return rows


def _calendar_exceptions(root):
    exceptions = {}
    for row in _table(root, CALENDAR_DATES, ('service_id', 'date', 'exception_type')):
        service_id = _text(row, 'service_id')
        day_value = _gtfs_date(row, 'date')
        kind = row.values.get('exception_type', '')
        if kind not in ('1', '2'):
            row.fail('exception_type must be 1 (added) or 2 (removed)')
        per_service = exceptions.setdefault(service_id, {})
        if day_value in per_service:
            first = per_service[day_value][1]
            row.fail('duplicate calendar_dates row for service_id=%s date=%s '
                     '(first at record %d, line %d)'
                     % (service_id, day_value.isoformat(), first.record, first.line))
        per_service[day_value] = ('added' if kind == '1' else 'removed', row)
    return exceptions


def _calendars(calendar_rows, exceptions):
    result = []
    services = set(calendar_rows) | set(exceptions)
    for service_id in sorted(services):
        rows = calendar_rows.get(service_id)
        service_exceptions = exceptions.get(service_id, {})
        added = {day_value for day_value, (kind, _) in service_exceptions.items()
                 if kind == 'added'}
        removed = {day_value for day_value, (kind, _) in service_exceptions.items()
                   if kind == 'removed'}
        if rows:
            row = rows
            start, end, weekdays = row['start'], row['end'], row['weekdays']
        else:
            # Exception-only service: no weekly base, just the listed dates.
            dates = sorted(service_exceptions)
            start, end = dates[0], dates[-1]
            weekdays = set()
        result.append({
            'id': service_id,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'weekdays': sorted(weekdays),
            'added': [day_value.isoformat() for day_value in sorted(added)],
            'removed': [day_value.isoformat() for day_value in sorted(removed)],
        })
    return result


def _query(path, stops):
    path = Path(path)
    if not path.is_file():
        raise ValueError('%s: query file is missing' % path)
    try:
        data = load_json(path)
    except ValueError as error:
        raise ValueError('%s: invalid query JSON: %s' % (path, error)) from error
    except OSError as error:
        raise ValueError('%s: cannot read query JSON: %s' % (path, error)) from error
    if not isinstance(data, dict):
        raise ValueError('%s: query must be a JSON object' % path)
    if 'query' in data:
        data = data['query']
        if not isinstance(data, dict):
            raise ValueError('%s: query must be a JSON object' % path)
    for key in ('date', 'origin', 'destination', 'depart'):
        if key not in data:
            raise ValueError('%s: query is missing %s' % (path, key))
    date_value = data['date']
    depart_value = data['depart']
    origin = data['origin']
    destination = data['destination']
    if not isinstance(origin, str) or not isinstance(destination, str):
        raise ValueError('%s: query origin and destination must be strings' % path)
    if not origin or not destination:
        raise ValueError('%s: query origin and destination must not be empty' % path)
    try:
        query_day(date_value)
    except ValueError as error:
        raise ValueError('%s: query date: %s' % (path, error)) from error
    try:
        query_clock(depart_value)
    except ValueError as error:
        raise ValueError('%s: query depart: %s' % (path, error)) from error
    if origin not in stops:
        raise ValueError('%s: query origin %s is not a stop' % (path, origin))
    if destination not in stops:
        raise ValueError('%s: query destination %s is not a stop' % (path, destination))
    return {'date': date_value, 'origin': origin,
            'destination': destination, 'depart': depart_value}


def import_snapshot(source, query_path):
    """Read a GTFS directory and return a service-path snapshot dictionary."""
    root = Path(source)
    if not root.is_dir():
        raise ValueError('%s: source must be a directory' % root)
    has_calendar = (root / CALENDAR).is_file()
    has_calendar_dates = (root / CALENDAR_DATES).is_file()
    if not has_calendar and not has_calendar_dates:
        raise ValueError('calendar.txt/calendar_dates.txt: at least one is required')

    stops = _stops(root)
    trips = _trips(root)
    calls = _stop_times(root, stops, trips)
    calendar_rows = {}
    exceptions = {}
    if has_calendar:
        calendar_rows = _calendar_rows(root)
    if has_calendar_dates:
        exceptions = _calendar_exceptions(root)

    for trip_id in sorted(trips):
        service_id = trips[trip_id]['service']
        if service_id not in calendar_rows and service_id not in exceptions:
            trips[trip_id]['row'].fail('unknown service_id')

    snapshot_stops = [{'id': stop_id, 'transfer_seconds': seconds}
                      for stop_id, seconds in sorted(stops.items())]
    snapshot_trips = []
    for trip_id in sorted(trips):
        snapshot_calls = [{'stop': call['stop'],
                           'arrival': call['arrival_text'],
                           'departure': call['departure_text']}
                          for call in calls[trip_id]]
        snapshot_trips.append({'id': trip_id,
                               'calendar': trips[trip_id]['service'],
                               'calls': snapshot_calls})
    return {
        'stops': snapshot_stops,
        'calendars': _calendars(calendar_rows, exceptions),
        'trips': snapshot_trips,
        'query': _query(query_path, stops),
    }
