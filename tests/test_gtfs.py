import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from service_path.gtfs import import_snapshot
from service_path.pareto import pareto
from service_path.route import route

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'fixtures' / 'gtfs' / 'sample'
QUERY = ROOT / 'fixtures' / 'gtfs' / 'query.json'
EXPECTED = json.loads((ROOT / 'fixtures' / 'timetable.json').read_text())


def run_cli(*args):
    return subprocess.run([sys.executable, '-m', 'service_path.import_cli', *map(str, args)],
                          capture_output=True)


class ImportSampleTests(unittest.TestCase):
    def test_sample_matches_handwritten_snapshot(self):
        snapshot = import_snapshot(SAMPLE, QUERY)
        self.assertEqual(snapshot['query'], EXPECTED['query'])
        # The sample adds one leading-zero stop beyond the handwritten fixture.
        self.assertEqual({s['id']: s['transfer_seconds'] for s in snapshot['stops']},
                         {s['id']: 0 for s in EXPECTED['stops']} | {'007': 0})
        self.assertEqual([t for t in snapshot['trips'] if t['id'] in ('N1', 'N2', 'N3')],
                         EXPECTED['trips'])
        daily = snapshot['calendars'][0]
        self.assertEqual(daily, {
            'id': 'daily', 'start': '2026-01-01', 'end': '2026-12-31',
            'weekdays': [0, 1, 2, 3, 4, 5, 6],
            'added': ['2027-01-02'], 'removed': ['2026-01-03']})
        self.assertEqual(snapshot['calendars'][1]['id'], 'holiday')
        self.assertEqual(snapshot['calendars'][1]['weekdays'], [])

    def test_bom_quoted_newline_extra_columns_and_leading_zero_ids(self):
        snapshot = import_snapshot(SAMPLE, QUERY)
        ids = [s['id'] for s in snapshot['stops']]
        self.assertIn('007', ids)
        # The quoted multi-line stop_name must stay inside its field, not split the row.
        self.assertEqual(len(ids), 5)
        self.assertEqual([t['id'] for t in snapshot['trips']], ['H1', 'N1', 'N2', 'N3'])

    def test_imported_snapshot_roundtrips_through_default_and_pareto(self):
        snapshot = import_snapshot(SAMPLE, QUERY)
        self.assertEqual(route(snapshot), route(EXPECTED))
        self.assertEqual(pareto(snapshot), pareto(EXPECTED))

    def test_imported_snapshot_feeds_both_service_path_cli_modes(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / 'snapshot.json'
            self.assertEqual(run_cli('--source', SAMPLE, '--query', QUERY,
                                     '--output', snapshot).returncode, 0)
            for extra, key in ((['--pareto'], 'journeys'), ([], 'segments')):
                out = Path(td) / ('report-%s.json' % key)
                result = subprocess.run(
                    [sys.executable, '-m', 'service_path.cli',
                     '--source', str(snapshot), '--output', str(out), *extra],
                    capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(key, json.loads(out.read_text()))
            default = json.loads((Path(td) / 'report-segments.json').read_text())
            self.assertEqual(default['arrival_seconds'], 87300)
            self.assertEqual(default['transfers'], 1)
            pareto_report = json.loads((Path(td) / 'report-journeys.json').read_text())
            self.assertEqual([j['arrival_seconds'] for j in pareto_report['journeys']],
                             [87300, 88200])

    def test_row_order_does_not_change_output_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            shuffled = Path(td) / 'gtfs'
            shutil.copytree(SAMPLE, shuffled)
            for name in ('stops.txt', 'trips.txt', 'stop_times.txt',
                         'calendar.txt', 'calendar_dates.txt'):
                path = shuffled / name
                with path.open('r', encoding='utf-8-sig', newline='') as handle:
                    rows = list(csv.reader(handle))
                header, body = rows[0], rows[1:]
                body.reverse()
                with path.open('w', encoding='utf-8', newline='') as handle:
                    csv.writer(handle, lineterminator='\n').writerows([header, *body])
            original = Path(td) / 'original.json'
            permuted = Path(td) / 'permuted.json'
            self.assertEqual(run_cli('--source', SAMPLE, '--query', QUERY,
                                     '--output', original).returncode, 0)
            self.assertEqual(run_cli('--source', shuffled, '--query', QUERY,
                                     '--output', permuted).returncode, 0)
            self.assertEqual(original.read_bytes(), permuted.read_bytes())

    def test_cli_output_matches_in_memory_result(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'snapshot.json'
            result = run_cli('--source', SAMPLE, '--query', QUERY, '--output', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text()),
                             json.loads(json.dumps(import_snapshot(SAMPLE, QUERY))))
            self.assertEqual(json.loads(result.stdout), json.loads(out.read_text()))

    def test_cli_requires_all_arguments(self):
        result = subprocess.run([sys.executable, '-m', 'service_path.import_cli'], capture_output=True)
        self.assertEqual(result.returncode, 2)


def write_gtfs(root, stops, trips, stop_times, calendar=None, calendar_dates=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'stops.txt').write_text(stops)
    (root / 'trips.txt').write_text(trips)
    (root / 'stop_times.txt').write_text(stop_times)
    if calendar is not None:
        (root / 'calendar.txt').write_text(calendar)
    if calendar_dates is not None:
        (root / 'calendar_dates.txt').write_text(calendar_dates)
    return root


STOPS = 'stop_id\nA\nB\nD\n'
TRIPS = 'service_id,trip_id\ndaily,N1\n'
CALENDAR = ('service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,'
            'start_date,end_date\ndaily,1,1,1,1,1,1,1,20260101,20261231\n')
TIMES = ('trip_id,arrival_time,departure_time,stop_id,stop_sequence\n'
         'N1,23:55:00,23:55:00,A,1\n'
         'N1,24:30:00,24:30:00,D,2\n')


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'gtfs'
        self.out = Path(self.tmp.name) / 'snapshot.json'
        self.out.write_text('previous')

    def assert_fails(self, fragment):
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertEqual(result.returncode, 2)
        self.assertIn(fragment, result.stderr.decode())
        self.assertEqual(self.out.read_text(), 'previous')

    def test_24_to_47_hour_times_are_preserved_and_normalised(self):
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('N1,23:55:00,23:55:00,A,1', 'N1,9:55:00,23:55:00,A,1')
                        .replace('N1,24:30:00,24:30:00,D,2', 'N1,25:30:00,27:30:00,D,2'),
                   CALENDAR)
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(self.out.read_text())['trips'][0]['calls']
        self.assertEqual(calls[0], {'stop': 'A', 'arrival': '09:55:00',
                                    'departure': '23:55:00'})
        self.assertEqual(calls[-1], {'stop': 'D', 'arrival': '25:30:00',
                                     'departure': '27:30:00'})

    def test_unknown_stop_reports_file_and_record(self):
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'Z,2'), CALENDAR)
        self.assert_fails('stop_times.txt: record 2')
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertIn('unknown stop_id', result.stderr.decode())

    def test_unknown_trip_and_unknown_service(self):
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('N1,24:30:00', 'N9,24:30:00'), CALENDAR)
        self.assert_fails('unknown trip_id')
        write_gtfs(self.root, STOPS, TRIPS.replace('daily', 'nope'),
                   TIMES, CALENDAR)
        self.assert_fails('trips.txt: record 1')
        self.assertIn('unknown service_id',
                      run_cli('--source', self.root, '--query', QUERY,
                              '--output', self.out).stderr.decode())

    def test_empty_or_invalid_times_are_rejected(self):
        for bad in ('N1,,24:30:00,D,2', 'N1,24:30:00,,D,2', 'N1,25:00:00,24:30:00,D,2',
                    'N1,24:75:00,24:75:00,D,2', 'N1,24:30:00,24:20:00,D,2'):
            write_gtfs(self.root, STOPS, TRIPS,
                       TIMES.replace('N1,24:30:00,24:30:00,D,2', bad), CALENDAR)
            with self.subTest(bad=bad):
                self.assert_fails('stop_times.txt: record 2')

    def test_duplicate_sequence_is_rejected_even_if_sorted(self):
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'D,1'), CALENDAR)
        self.assert_fails('duplicate stop_sequence')
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'D,x'), CALENDAR)
        self.assert_fails('stop_sequence must be a non-negative integer')

    def test_temporal_order_and_repeated_stop(self):
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'A,2'), CALENDAR)
        self.assert_fails('repeated stop_id')
        write_gtfs(self.root, STOPS,
                   TRIPS.replace('daily,N1', 'daily,N1\ndaily,N2'),
                   TIMES + 'N2,24:30:00,24:30:00,A,1\nN2,24:20:00,24:20:00,D,2\n',
                   CALENDAR)
        self.assert_fails('not later than the previous departure_time')

    def test_missing_calendar_files_and_malformed_rows(self):
        write_gtfs(self.root, STOPS, TRIPS, TIMES)
        self.assert_fails('at least one is required')
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR,
                   'service_id,date,exception_type\ndaily,20260102,1\ndaily,20260102,2\n')
        self.assert_fails('calendar_dates.txt: record 2')
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR,
                   'service_id,date,exception_type\ndaily,20260230,1\n')
        self.assert_fails('not a real date')
        write_gtfs(self.root, STOPS, TRIPS, TIMES,
                   CALENDAR.replace('20260101,20261231', '20261231,20260101'))
        self.assert_fails('start_date is later than end_date')

    def test_missing_required_column_and_duplicate_calendar_service(self):
        write_gtfs(self.root, STOPS, TRIPS, TIMES,
                   CALENDAR.replace(',sunday,', ','))
        self.assert_fails('header is missing required columns: sunday')
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR + CALENDAR.splitlines()[-1] + '\n')
        self.assert_fails('duplicate service_id')

    def test_stale_or_stopped_trips_are_still_validated(self):
        # The trip never runs on the query date, but bad structure must still fail.
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'Z,2'), CALENDAR)
        self.assert_fails('unknown stop_id')
        write_gtfs(self.root, STOPS, TRIPS,
                   TIMES.replace('D,2', 'D,2'), CALENDAR,
                   'service_id,date,exception_type\ndaily,20260102,2\n')
        self.assertEqual(run_cli('--source', self.root, '--query', QUERY,
                                 '--output', self.out).returncode, 0)

    def test_query_must_reference_imported_stops(self):
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR)
        query = Path(self.tmp.name) / 'query.json'
        query.write_text(json.dumps(dict(json.loads(QUERY.read_text()), destination='C')))
        result = run_cli('--source', self.root, '--query', query, '--output', self.out)
        self.assertEqual(result.returncode, 2)
        self.assertIn('query destination C is not a stop', result.stderr.decode())
        self.assertEqual(self.out.read_text(), 'previous')


class CalendarMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'gtfs'
        self.out = Path(self.tmp.name) / 'snapshot.json'

    def import_rows(self, calendar=None, exceptions=None):
        write_gtfs(self.root, STOPS, TRIPS, TIMES, calendar, exceptions)
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.out.read_text())['calendars'][0]

    def test_added_and_removed_exceptions_outside_regular_span(self):
        cal = self.import_rows(CALENDAR,
                               'service_id,date,exception_type\ndaily,20270102,1\n'
                               'daily,20260103,2\n')
        self.assertEqual(cal['added'], ['2027-01-02'])
        self.assertEqual(cal['removed'], ['2026-01-03'])

    def test_exception_only_service_has_no_weekly_mask(self):
        write_gtfs(self.root, STOPS, 'service_id,trip_id\nspecial,N1\n', TIMES, None,
                   'service_id,date,exception_type\nspecial,20260102,1\nspecial,20260201,1\n')
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(self.out.read_text())
        self.assertEqual(snapshot['calendars'], [{
            'id': 'special', 'start': '2026-01-02', 'end': '2026-02-01', 'weekdays': [],
            'added': ['2026-01-02', '2026-02-01'], 'removed': []}])
        self.assertEqual(route(snapshot)['arrival_seconds'], 88200)

    def test_exception_only_service_is_inactive_on_other_days(self):
        write_gtfs(self.root, STOPS, 'service_id,trip_id\nspecial,N1\n', TIMES, None,
                   'service_id,date,exception_type\nspecial,20260102,1\n')
        run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        snapshot = json.loads(self.out.read_text())
        snapshot['query'] = dict(snapshot['query'], date='2026-01-03')
        self.assertFalse(route(snapshot)['reachable'])

    def test_removed_exception_cancels_added_and_weekly_service(self):
        cal = self.import_rows(CALENDAR,
                               'service_id,date,exception_type\ndaily,20260102,2\n')
        self.assertEqual(cal['removed'], ['2026-01-02'])
        snapshot = json.loads(self.out.read_text())
        self.assertFalse(route(snapshot)['reachable'])

    def test_removal_only_exception_service_is_never_active(self):
        write_gtfs(self.root, 'stop_id\nA\nB\n',
                   'service_id,trip_id\ndaily,N1\n',
                   'trip_id,arrival_time,departure_time,stop_id,stop_sequence\n'
                   'N1,23:55:00,23:55:00,A,1\nN1,24:30:00,24:30:00,B,2\n',
                   None, 'service_id,date,exception_type\ndaily,20260102,2\n')
        query = Path(self.tmp.name) / 'query.json'
        query.write_text(json.dumps({'date': '2026-01-02', 'origin': 'A',
                                     'destination': 'B', 'depart': '23:50:00'}))
        run_cli('--source', self.root, '--query', query, '--output', self.out)
        snapshot = json.loads(self.out.read_text())
        self.assertEqual(snapshot['calendars'][0]['weekdays'], [])
        self.assertEqual(snapshot['calendars'][0]['added'], [])
        self.assertEqual(snapshot['calendars'][0]['removed'], ['2026-01-02'])
        self.assertFalse(route(snapshot)['reachable'])

    def test_duplicate_added_exception_is_rejected(self):
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR,
                   'service_id,date,exception_type\ndaily,20260102,1\ndaily,20260102,1\n')
        result = run_cli('--source', self.root, '--query', QUERY, '--output', self.out)
        self.assertEqual(result.returncode, 2)
        self.assertIn('duplicate calendar_dates row', result.stderr.decode())


class QueryAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'gtfs'
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR)

    def test_query_can_be_read_from_an_existing_snapshot(self):
        snapshot_path = Path(self.tmp.name) / 'existing.json'
        snapshot_path.write_text((ROOT / 'fixtures' / 'timetable.json').read_text())
        out = Path(self.tmp.name) / 'snapshot.json'
        result = run_cli('--source', self.root, '--query', snapshot_path, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text())['query'], EXPECTED['query'])
        # The query source must not be modified.
        self.assertEqual(snapshot_path.read_text(),
                         (ROOT / 'fixtures' / 'timetable.json').read_text())

    def test_output_may_not_overwrite_inputs(self):
        for target in (self.root / 'stops.txt', self.root / 'calendar.txt'):
            before = target.read_bytes()
            result = run_cli('--source', self.root, '--query', QUERY, '--output', target)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_bytes(), before)

    def test_missing_required_file_or_column(self):
        out = Path(self.tmp.name) / 'snapshot.json'
        (self.root / 'stops.txt').unlink()
        result = run_cli('--source', self.root, '--query', QUERY, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertIn('stops.txt: required file is missing', result.stderr.decode())
        self.assertFalse(out.exists())

    def test_invalid_utf8_reports_file(self):
        write_gtfs(self.root, STOPS, TRIPS, TIMES, CALENDAR)
        (self.root / 'stops.txt').write_bytes(b'stop_id\n\xff\n')
        out = Path(self.tmp.name) / 'snapshot.json'
        result = run_cli('--source', self.root, '--query', QUERY, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertIn('stops.txt: invalid UTF-8', result.stderr.decode())


class PackagingTests(unittest.TestCase):
    def test_import_entry_point_is_declared(self):
        text = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('service-path-import = "service_path.import_cli:main"', text)


if __name__ == '__main__':
    unittest.main()
